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
from typing import Any, Callable

from .judge import Confirm, JudgeVerdict
from .registry import ToolRegistry

DEFAULT_MODEL = "claude-opus-4-8"  # generator tier (ADR-3)
DEFAULT_MAX_TOKENS = 2048
# Raised 5→12 (Fable health check): real investigations spend ~4 turns gathering
# before the first proposal, so at cap=5 the repair loop was UNREACHABLE (repair_cap
# was dead code) — every real run terminated CAPPED with an ungraded/rejected answer.
# 12 leaves room for gather → propose → repair. The `max_seconds` wall-clock guard
# bounds the latency this could add.
DEFAULT_INVESTIGATION_CAP = 12     # ADR-1 budget: bounds total model turns
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

    @property
    def trusted(self) -> bool:
        """Did `final_text` actually pass the grounding gate on a clean terminal?

        The enforcement seam (Fable health check). ONLY `RESOLVED` and `INCONCLUSIVE`
        are reached AFTER grounding passed. `CAPPED` (budget exhausted) and
        `ABANDONED` (grounding failed) carry an answer that was rejected or never
        finally graded — consumers (UI, DTO, eval) must NOT present it as a verified
        finding. `MODEL_STOPPED` has no judge at all. This is the boundary that
        stops an ungrounded capped answer from shipping as a conclusion."""
        return self.reason in (TerminalReason.RESOLVED, TerminalReason.INCONCLUSIVE)


def _text_from(content: Any) -> str:
    """Concatenate the text blocks of an assistant response (skip tool_use blocks)."""
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(parts).strip()


def _emit(on_event: Callable[[dict], None] | None, event: dict) -> None:
    """Fire the optional step-stream observer. An observer failure must NEVER
    alter the investigation (it is a spectator, not a participant), so any
    exception it raises is swallowed here."""
    if on_event is None:
        return
    try:
        on_event(event)
    except Exception:  # noqa: BLE001 — observers cannot break the loop
        pass


def _tool_call_summary(name: str, model_input: Any) -> str:
    """Short human-facing description of a tool call for the step stream.

    Built ONLY from the model's own tool_use input (its request), never from tool
    output or provenance — the same no-leak discipline as the DTO (ADR-5).
    e.g. 'feature_history: equity_multiplier @ 2023-04-30 (-4q)'."""
    try:
        parts: list[str] = []
        # The subject of the request: features / items / sections list.
        for key in ("features", "items", "sections"):
            v = model_input.get(key)
            if isinstance(v, list) and v:
                subject = ", ".join(str(s) for s in v[:4])
                if len(v) > 4:
                    subject += f" (+{len(v) - 4} more)"
                parts.append(subject)
                break
        rd = model_input.get("report_date")
        if rd:
            anchor = f"@ {rd}"
            offset = model_input.get("period_offset")
            if isinstance(offset, int) and offset:
                anchor += f" ({offset:+d}q)"
            parts.append(anchor)
        detail = " ".join(parts)[:160]
        return f"{name}: {detail}" if detail else name
    except Exception:  # noqa: BLE001 — a summary failure must not break dispatch
        return name


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
    on_event: Callable[[dict], None] | None = None,  # OPTIONAL step-stream observer —
    #   called with small human-facing dicts ({"type": "turn"|"tool"|"tool_result"|
    #   "grounding"|"verdict", ...}) as the loop progresses, so a caller can stream
    #   progress (SSE) instead of blanking for the whole run. Pure observer: when
    #   None (the default — eval harness, batch, existing endpoint) behavior and
    #   return value are byte-identical to before this hook existed.
) -> TerminalResult:
    """Run one investigation to termination and return the outcome.

    `client` is anything exposing Anthropic's `messages.create(...)` — the real
    SDK or a scripted fake. `judge` (optional) owns termination when present: the
    model's `end_turn` becomes a *proposal* the judge adjudicates (ADR-6). With no
    judge, `end_turn` is terminal (ADR-4) and the reason is `MODEL_STOPPED`.
    """
    result = _run_loop(
        client, registry, task, judge=judge, predicate=predicate, system=system,
        model=model, max_tokens=max_tokens, investigation_cap=investigation_cap,
        repair_cap=repair_cap, max_seconds=max_seconds, on_event=on_event,
    )
    # One terminal event regardless of WHICH return path ended the loop.
    _emit(on_event, {"type": "verdict", "status": result.reason.value, "trusted": result.trusted})
    return result


def _run_loop(
    client: Any,
    registry: ToolRegistry,
    task: str,
    *,
    judge: Any = None,
    predicate: str | None = None,
    system: str = "",
    model: str = DEFAULT_MODEL,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    investigation_cap: int = DEFAULT_INVESTIGATION_CAP,
    repair_cap: int = DEFAULT_REPAIR_CAP,
    max_seconds: float | None = None,
    on_event: Callable[[dict], None] | None = None,
) -> TerminalResult:
    """The loop body (see `run_investigation`). Split out so the terminal `verdict`
    event can be emitted exactly once around the loop's several return points."""
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
        _emit(on_event, {"type": "turn", "n": turns})
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
                _emit(on_event, {
                    "type": "tool",
                    "name": block.name,
                    "summary": _tool_call_summary(block.name, block.input),
                })
                outcome = registry.dispatch(block.name, block.input)
                evidence.extend(outcome.raw_results)   # full envelope -> judge (ADR-5)
                # Status only (found / period_not_filed / ...), never payloads —
                # the step stream is human-facing, not a data channel (ADR-5).
                statuses: list[str] = []
                for r in outcome.raw_results:
                    s = getattr(getattr(r, "status", None), "value", None)
                    if s and s not in statuses:
                        statuses.append(s)
                _emit(on_event, {
                    "type": "tool_result",
                    "name": block.name,
                    "status": "; ".join(statuses) if statuses else "ok",
                })
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
        _emit(on_event, {"type": "grounding", "status": "checking", "facts": len(evidence)})
        verdict: JudgeVerdict = judge.evaluate(predicate or task, last_answer, evidence)
        last_verdict = verdict
        if verdict.grounded:
            _emit(on_event, {"type": "grounding", "status": "verified", "facts": len(evidence)})
        else:
            # Item names only (the same strings the repair prompt shows the model) —
            # no provenance receipts.
            _emit(on_event, {
                "type": "grounding",
                "status": "failed",
                "reasons": list(verdict.ungrounded_items)[:5],
            })

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
