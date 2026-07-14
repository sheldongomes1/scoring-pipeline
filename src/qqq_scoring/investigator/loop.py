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

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .judge import Confirm, JudgeVerdict
from .registry import ToolRegistry

DEFAULT_MODEL = "claude-opus-4-8"  # generator tier (ADR-3)
DEFAULT_MAX_TOKENS = 2048
DEFAULT_INVESTIGATION_CAP = 5      # ADR-1 budget: bounds evidence-gathering loops
DEFAULT_REPAIR_CAP = 2             # ADR-1 budget: bounds grounding reruns, SEPARATELY


class TerminalReason(str, Enum):
    """Why the loop stopped.

    `MODEL_STOPPED` is the no-judge path (ADR-4): the model's `end_turn` is
    terminal. With a judge attached (ADR-6), `end_turn` is only a *proposal* and
    the judge assigns one of the ADR-1 terminal states instead."""

    MODEL_STOPPED = "model_stopped"                 # no judge: model chose to end
    RESOLVED = "resolved"                           # judge: grounded, resolved, no open Qs
    INCONCLUSIVE = "inconclusive"                   # judge: grounded, clean, genuinely ambiguous
    ABANDONED = "abandoned"                         # judge: grounding failed past repair_cap
    CAP_REACHED = "investigation_cap_reached"       # turn ceiling hit before a terminal verdict


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
    verdict: JudgeVerdict | None = None   # the judge's final grade (None on the no-judge path)


def _text_from(content: Any) -> str:
    """Concatenate the text blocks of an assistant response (skip tool_use blocks)."""
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _repair_prompt(verdict: JudgeVerdict) -> str:
    items = ", ".join(verdict.ungrounded_items) or "one or more figures"
    return (
        f"Your proposed conclusion was NOT accepted: {items} could not be "
        "independently verified against the source. Re-investigate and base your "
        "answer only on values the tools actually returned — do not state a figure "
        "you have not fetched."
    )


def _continue_prompt(verdict: JudgeVerdict) -> str:
    return (
        f"Not done yet — open questions remain: {verdict.reasoning} Make additional "
        "tool calls to resolve them, then propose your conclusion again."
    )


def run_investigation(
    client: Any,
    registry: ToolRegistry,
    task: str,
    *,
    judge: Any = None,          # ADR-6: injected termination policy; None = ADR-4 behaviour
    predicate: str | None = None,  # the exact question the judge grades (defaults to task)
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    investigation_cap: int = DEFAULT_INVESTIGATION_CAP,
    repair_cap: int = DEFAULT_REPAIR_CAP,
    max_seconds: float | None = None,   # wall-clock guard (eval #2): bound latency so a
    #                                     rate-limit/retry storm can't run 38 minutes.
) -> TerminalResult:
    """Run one investigation to termination and return the outcome.

    `client` is anything exposing Anthropic's `messages.create(...)` — the real
    SDK or a scripted fake. `judge` (optional) owns termination when present: the
    model's `end_turn` becomes a *proposal* the judge adjudicates (ADR-6). With no
    judge, `end_turn` is terminal (ADR-4) and the reason is `MODEL_STOPPED`.
    """
    messages: list[dict] = [{"role": "user", "content": task}]
    tools = registry.tool_definitions()
    evidence: list[Any] = []
    tool_calls = 0
    repairs = 0
    turns = 0
    last_answer = ""
    last_verdict = None   # audit #6: preserve the most recent judge verdict for CAP_REACHED
    # ADR-1's two caps are INDEPENDENT, not additive: investigation_cap bounds
    # total model turns (the backstop against any runaway, incl. propose<->continue
    # with no gathering); repair_cap is a SEPARATE sub-ceiling that trips ABANDONED
    # on its own count (`repairs`), so a grounding-repair explosion can't hide
    # behind the investigation budget. Summing them would hand repair headroom to a
    # no-judge run that can never repair.
    ceiling = investigation_cap
    started = time.monotonic()

    while turns < ceiling:
        # Wall-clock guard (eval #2): bail before a rate-limit/retry storm turns one
        # investigation into a 38-minute run. Terminates CAP_REACHED (budget exhausted)
        # carrying the last verdict, exactly like the turn ceiling.
        if max_seconds is not None and (time.monotonic() - started) > max_seconds:
            return TerminalResult(
                TerminalReason.CAP_REACHED, turns, last_answer, tool_calls, messages, evidence, last_verdict
            )
        turns += 1
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

        if resp.stop_reason == "tool_use":
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
            continue

        # --- the model PROPOSED a conclusion (stopped calling tools) -----------
        last_answer = _text_from(resp.content)

        if judge is None:
            # ADR-4 path: no judge, the proposal is terminal.
            return TerminalResult(
                TerminalReason.MODEL_STOPPED, turns, last_answer, tool_calls, messages, evidence
            )

        # ADR-6: the judge adjudicates the proposal. Grounding (G) gates first.
        verdict: JudgeVerdict = judge.evaluate(predicate or task, last_answer, evidence)
        last_verdict = verdict

        if not verdict.grounded:
            # ADR-13: an INTEGRITY failure (re-fetch mismatch / missing backend) is a
            # source-drift or config event the generator cannot fix by re-answering.
            # Terminating immediately avoids burning the whole repair_cap on a loop
            # that is structurally guaranteed to keep failing on the same stale item.
            if verdict.deterministic_failure:
                return TerminalResult(
                    TerminalReason.ABANDONED, turns, last_answer, tool_calls, messages, evidence, verdict
                )
            # Otherwise it's an answer-support failure (the generator mis-stated a
            # figure or mischaracterized a passage) — that IS repairable.
            repairs += 1
            if repairs > repair_cap:
                return TerminalResult(
                    TerminalReason.ABANDONED, turns, last_answer, tool_calls, messages, evidence, verdict
                )
            messages.append({"role": "user", "content": _repair_prompt(verdict)})
            continue

        if verdict.open_questions:
            # Grounded but incomplete — send it back to gather more (ADR-1 loop).
            messages.append({"role": "user", "content": _continue_prompt(verdict)})
            continue

        # Grounded, no open questions. The predicate is answered either way —
        # CONFIRMED or REFUTED are BOTH resolutions (ADR-11). Only a genuinely
        # INDETERMINATE verdict is the (useful) inconclusive terminal state.
        if verdict.confirm in (Confirm.CONFIRMED, Confirm.REFUTED):
            return TerminalResult(
                TerminalReason.RESOLVED, turns, last_answer, tool_calls, messages, evidence, verdict
            )
        return TerminalResult(
            TerminalReason.INCONCLUSIVE, turns, last_answer, tool_calls, messages, evidence, verdict
        )

    # Turn ceiling hit before any terminal verdict — a real terminal state, not an error.
    # Carry the last judge verdict if one exists (audit #6: don't drop it).
    return TerminalResult(
        TerminalReason.CAP_REACHED, turns, last_answer, tool_calls, messages, evidence, last_verdict
    )
