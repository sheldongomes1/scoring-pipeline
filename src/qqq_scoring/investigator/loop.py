"""The single-branch agent loop (Phase 2) — proves the tool-use loop CLOSES.

The one property under test (ADR-1): the model owns the next edge. We send it a
task + a tool menu; it emits a `tool_use`; we run the tool and hand back a
`tool_result`; it reads that and decides the NEXT move — another tool call, or
stop. Path-variance conditioned on observations is what separates this from
`orchestrate.py`'s fixed DAG.

CLIENT-AGNOSTIC ON PURPOSE. `run_investigation` takes the `client` as an
argument. The loop's mechanical closure is a function of message-handling, not of
which model answers — the same reasoning that let us inject a fake data backend.
So one harness yields two proofs:
  * scripted fake client  -> proves the HARNESS closes (deterministic, no key)
  * real claude-opus-4-8   -> proves the AGENTIC property (the model varies path)

Sync, single branch. Fan-out across hypotheses is Phase 3's problem, not here.
Termination here is deliberately thin: stop on the model's `end_turn` OR the
`investigation_cap` budget (ADR-1). The judge (ADR-1's real stop condition) grades
output quality; it does not *close* the loop, so it's a later slice.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .registry import ToolRegistry

DEFAULT_MODEL = "claude-opus-4-8"  # generator tier (ADR-3)
DEFAULT_MAX_TOKENS = 2048
DEFAULT_INVESTIGATION_CAP = 5      # ADR-1 budget: bounds evidence-gathering loops


class TerminalReason(str, Enum):
    """Why the loop stopped. Two thin reasons for this slice; the judge's
    resolved/inconclusive/failed verdicts (ADR-1) layer on in a later slice."""

    MODEL_STOPPED = "model_stopped"                 # model chose to end (stop_reason != tool_use)
    CAP_REACHED = "investigation_cap_reached"       # hit the ADR-1 budget without stopping


@dataclass
class TerminalResult:
    """The outcome of one investigation, with the judge's raw material set aside.

    `evidence` holds the FULL tool results (incl. provenance) — retained for the
    judge, never shown to the model (ADR-5). `messages` is the transcript;
    `final_text` is the model's closing answer."""

    reason: TerminalReason
    iterations: int
    final_text: str
    tool_calls: int
    messages: list[dict] = field(default_factory=list)
    evidence: list[Any] = field(default_factory=list)


def _text_from(content: Any) -> str:
    """Concatenate the text blocks of an assistant response (skip tool_use blocks)."""
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def run_investigation(
    client: Any,
    registry: ToolRegistry,
    task: str,
    *,
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    investigation_cap: int = DEFAULT_INVESTIGATION_CAP,
) -> TerminalResult:
    """Run one investigation to termination and return the outcome.

    `client` is anything exposing Anthropic's `messages.create(...)` — the real
    SDK or a scripted fake. That injection is the whole reason the loop is testable
    without a live model.
    """
    messages: list[dict] = [{"role": "user", "content": task}]
    tools = registry.tool_definitions()
    evidence: list[Any] = []
    tool_calls = 0

    for iteration in range(1, investigation_cap + 1):
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        # Record the assistant turn verbatim so the next request carries full
        # history — the model can only "read the tool_result and decide" if the
        # prior turn (its tool_use) is in the transcript it sees.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            # The model chose to stop: it has its answer, no further edge to pick.
            return TerminalResult(
                reason=TerminalReason.MODEL_STOPPED,
                iterations=iteration,
                final_text=_text_from(resp.content),
                tool_calls=tool_calls,
                messages=messages,
                evidence=evidence,
            )

        # The model picked one or more tools — run each, collect a tool_result.
        # A turn may contain several tool_use blocks (parallel tool calls); the
        # API requires a tool_result for EVERY one before the next turn.
        tool_result_blocks = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            tool_calls += 1
            outcome = registry.dispatch(block.name, block.input)
            evidence.extend(outcome.raw_results)   # full envelope -> judge (ADR-5)
            tool_result_blocks.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": outcome.model_content,  # generator-facing subset only
                }
            )
        messages.append({"role": "user", "content": tool_result_blocks})

    # Budget exhausted before the model stopped — a real terminal state (ADR-1),
    # not an error. The last assistant turn's text is the best available answer.
    last_text = _text_from(messages[-2]["content"]) if len(messages) >= 2 else ""
    return TerminalResult(
        reason=TerminalReason.CAP_REACHED,
        iterations=investigation_cap,
        final_text=last_text,
        tool_calls=tool_calls,
        messages=messages,
        evidence=evidence,
    )
