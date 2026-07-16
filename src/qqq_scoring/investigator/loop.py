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

import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from .judge import Confirm, JudgeVerdict
from .registry import ToolRegistry

DEFAULT_MODEL = "claude-opus-4-8"  # generator tier (ADR-3)
# 4096, not 2048: since ADR-18 the final turn is a large JSON tool call
# (verdict_sentence + rationale + 3-6 key_evidence objects + caveats), and a
# max_tokens cut mid-tool_use is a protocol event, not a prose truncation.
DEFAULT_MAX_TOKENS = 4096
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
    judge, never shown to the model (ADR-5). `messages` is the transcript.

    ADR-18: the answer is BORN structured — `verdict_sentence` / `rationale` /
    `key_evidence` / `caveats` come from the generator's `submit_findings` tool
    call and are what the judge verified. `final_text` (the generator's last prose
    turn) is DEMOTED to an audit attachment — kept, never rendered as the verdict."""

    reason: TerminalReason
    iterations: int
    final_text: str
    tool_calls: int
    messages: list[dict] = field(default_factory=list)
    evidence: list[Any] = field(default_factory=list)
    verdict: JudgeVerdict | None = None   # the judge's final grade (None on the no-judge path)
    # ADR-18 structured findings (empty when the model never called submit_findings).
    verdict_sentence: str = ""
    rationale: str = ""
    key_evidence: tuple = ()   # probe references (source, ticker, report_date, period_offset,
    #                            feature, value) — verified by the judge's INTEGRITY head
    caveats: tuple = ()        # claims of absence / limitations — verified by answer-support
    #                            against the full pile incl. failed probes

    @property
    def advisories(self) -> tuple:
        """ADR-19 judge notes (non-gating), read off the final verdict."""
        return self.verdict.advisories if self.verdict is not None else ()

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


class OpTracker:
    """Names the network call in flight (2026-07-16 FTNT lesson: `max_seconds` is
    checked BETWEEN turns, so one hung client call is unbounded by it — 21.4h once).
    The hard bound now lives on each client (SDK request timeout / BQ+GCS deadlines);
    this tracker is the diagnostic half: `last_operation` always names the most
    recent blocking call, and when a single call exceeds the loop's per-turn share
    of `max_seconds` it logs WHICH call ate the budget."""

    def __init__(self, per_op_budget: float | None = None, log: Callable[[str], None] | None = None) -> None:
        self.per_op_budget = per_op_budget   # the loop's per-turn share of max_seconds
        self.last_operation: str | None = None
        self._t0: float | None = None
        self._log = log if log is not None else (lambda msg: print(msg, file=sys.stderr))

    def begin(self, op: str) -> None:
        self.last_operation = op
        self._t0 = time.monotonic()

    def end(self) -> None:
        if self._t0 is None:
            return
        elapsed = time.monotonic() - self._t0
        self._t0 = None
        if self.per_op_budget is not None and elapsed > self.per_op_budget:
            try:
                self._log(
                    f"[investigator] slow call: {self.last_operation!r} took {elapsed:.1f}s "
                    f"(per-turn share of max_seconds is {self.per_op_budget:.1f}s)"
                )
            except Exception:  # noqa: BLE001 — diagnostics must never break the loop
                pass


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


FINDINGS_TOOL_NAME = "submit_findings"


def _submit_findings_tool() -> dict:
    """The forced structured final turn (ADR-18): calling this tool IS how the
    investigation concludes — it replaces free-text end_turn as the termination
    proposal the judge adjudicates. Every field is verified: verdict_sentence /
    rationale / caveats by the answer-support head, key_evidence by the INTEGRITY
    head's batched == re-fetch. References make ungroundable evidence
    unrepresentable — there is deliberately NO prose field on key_evidence."""
    return {
        "name": FINDINGS_TOOL_NAME,
        "description": (
            "Submit your final findings — calling this tool is the ONLY way to "
            "conclude the investigation. Call it when your gathered evidence "
            "resolves the hypothesis (confirmed, refuted, or genuinely "
            "indeterminate). Cite in key_evidence the exact probes (as you "
            "requested them: ticker, report_date anchor, period_offset, feature, "
            "and the value the tool returned) that your verdict rests on — a "
            "reviewer independently re-fetches each one. Use caveats for claims "
            "of absence or limitations (e.g. a metric that was missing)."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "verdict_sentence": {
                    "type": "string",
                    "description": "Your verdict on the hypothesis, exactly one sentence.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why the evidence supports that verdict, 2-4 sentences.",
                },
                "key_evidence": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source": {
                                "type": "string",
                                "description": "The tool that returned this value, e.g. 'feature_history' or 'balance_sheet_items'.",
                            },
                            "ticker": {"type": "string", "description": "Company ticker as requested."},
                            "report_date": {
                                "type": "string",
                                "format": "date",
                                "description": "The ANCHOR report_date you passed to the tool (YYYY-MM-DD), not the resolved period.",
                            },
                            "period_offset": {
                                "type": "integer",
                                "description": "The signed period_offset you passed to the tool.",
                            },
                            "feature": {"type": "string", "description": "The feature / line-item key requested."},
                            "value": {
                                "type": ["number", "null"],
                                "description": "The value the tool returned (null for a missing/not-filed probe).",
                            },
                        },
                        "required": ["source", "ticker", "report_date", "period_offset", "feature", "value"],
                        "additionalProperties": False,
                    },
                    "description": (
                        "3-6 probe references your verdict rests on. Each must be a probe "
                        "you actually made this investigation, cited exactly as requested "
                        "and returned. No prose here."
                    ),
                },
                "caveats": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": (
                        "Claims of absence / limitations (e.g. 'ocf_to_assets was "
                        "feature_missing for every quarter checked'). Empty if none."
                    ),
                },
            },
            "required": ["verdict_sentence", "rationale", "key_evidence", "caveats"],
            "additionalProperties": False,
        },
    }


def _parse_findings(payload: Any) -> dict:
    """Defensive parse of the model's submit_findings input — `required` in a tool
    schema is a hint, not a runtime contract (2026-07-14 lesson). Missing `caveats`
    → []; missing/empty `key_evidence` is preserved as [] and the LOOP fails it
    closed (an answer citing nothing is unverifiable by construction)."""
    payload = payload if isinstance(payload, dict) else {}
    key_evidence = [k for k in (payload.get("key_evidence") or []) if isinstance(k, dict)]
    caveats = [str(c) for c in (payload.get("caveats") or [])]
    return {
        "verdict_sentence": str(payload.get("verdict_sentence") or "").strip(),
        "rationale": str(payload.get("rationale") or "").strip(),
        "key_evidence": key_evidence,
        "caveats": caveats,
    }


def _serialize_findings(findings: dict) -> str:
    """The judge-facing rendering of the structured findings (ADR-18): the
    answer-support and C/O heads read THESE fields, not the generator's free prose.
    Caveats are claims of absence — checkable because the judge's evidence view
    already includes failed probes (feature_missing / period_not_filed)."""
    lines = [
        f"VERDICT: {findings['verdict_sentence']}",
        f"RATIONALE: {findings['rationale']}",
    ]
    if findings["caveats"]:
        lines.append("CAVEATS (claims of absence / limitations):")
        lines.extend(f"- {c}" for c in findings["caveats"])
    return "\n".join(lines)


_NUDGE_FINDINGS = (
    "Do not end the turn with prose. When you are ready to conclude, call "
    "submit_findings with your verdict_sentence, rationale, key_evidence and "
    "caveats — that is the only way the investigation concludes. If you still "
    "need data, make another tool call."
)

_MISSING_KEY_EVIDENCE = (
    "Your submit_findings cited no key_evidence, so nothing in it can be "
    "verified. Re-submit citing the exact probes (ticker, report_date anchor, "
    "period_offset, feature, value) your verdict rests on — values exactly as "
    "the tools returned them."
)


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
    # ADR-18: the task carries the instruction that submit_findings IS the exit.
    messages: list[dict] = [{"role": "user", "content": (
        f"{task}\n\nWhen you have gathered enough evidence, conclude by calling the "
        f"{FINDINGS_TOOL_NAME} tool — that is the only way to finish the investigation."
    )}]
    tools = registry.tool_definitions() + [_submit_findings_tool()]
    evidence: list[Any] = []
    tool_calls = 0
    repairs = 0
    turns = 0
    last_answer = ""
    last_verdict = None   # audit #6: preserve the most recent judge verdict for CAP_REACHED
    findings: dict = {"verdict_sentence": "", "rationale": "", "key_evidence": [], "caveats": []}

    def _mk(reason: TerminalReason, verdict: JudgeVerdict | None) -> TerminalResult:
        """Build a TerminalResult carrying the latest structured findings (ADR-18) —
        reads the enclosing locals at call time, so every return path is uniform."""
        return TerminalResult(
            reason, turns, last_answer, tool_calls, messages, evidence, verdict,
            verdict_sentence=findings["verdict_sentence"],
            rationale=findings["rationale"],
            key_evidence=tuple(findings["key_evidence"]),
            caveats=tuple(findings["caveats"]),
        )
    # ADR-1's two caps are INDEPENDENT, not additive: investigation_cap bounds
    # total model turns (the backstop against any runaway, incl. propose<->continue
    # with no gathering); repair_cap is a SEPARATE sub-ceiling that trips ABANDONED
    # on its own count (`repairs`), so a grounding-repair explosion can't hide
    # behind the investigation budget. Summing them would hand repair headroom to a
    # no-judge run that can never repair.
    ceiling = investigation_cap
    started = time.monotonic()
    # Diagnostic marker for the FTNT-hang class (2026-07-16): name the call in
    # flight, and log when one call eats more than the loop's per-turn share.
    tracker = OpTracker(
        per_op_budget=(max_seconds / max(1, investigation_cap)) if max_seconds else None
    )

    while turns < ceiling:
        # Wall-clock guard (eval #2): bail before a rate-limit/retry storm turns one
        # investigation into a 38-minute run. Terminates CAP_REACHED (budget exhausted)
        # carrying the last verdict, exactly like the turn ceiling.
        if max_seconds is not None and (time.monotonic() - started) > max_seconds:
            return _mk(TerminalReason.CAP_REACHED, last_verdict)
        turns += 1
        _emit(on_event, {"type": "turn", "n": turns})
        tracker.begin(f"anthropic messages.create (turn {turns})")
        resp = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
        )
        tracker.end()
        # Record the assistant turn verbatim so the next request carries full
        # history — the model can only "read the tool_result and decide" if the
        # prior turn (its tool_use) is in the transcript it sees.
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason == "tool_use":
            # The model picked one or more tools — run each, collect a tool_result.
            # A turn may contain several tool_use blocks (parallel tool calls); the
            # API requires a tool_result for EVERY one before the next turn.
            # `submit_findings` blocks are NOT dispatched — they are the termination
            # proposal (ADR-18) and are adjudicated after the data tools ran.
            tool_result_blocks = []
            findings_blocks = []
            for block in resp.content:
                if getattr(block, "type", None) != "tool_use":
                    continue
                if block.name == FINDINGS_TOOL_NAME:
                    findings_blocks.append(block)
                    continue
                tool_calls += 1
                _emit(on_event, {
                    "type": "tool",
                    "name": block.name,
                    "summary": _tool_call_summary(block.name, block.input),
                })
                tracker.begin(f"tool dispatch {block.name}")
                outcome = registry.dispatch(block.name, block.input)
                tracker.end()
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

            if not findings_blocks:
                messages.append({"role": "user", "content": tool_result_blocks})
                continue

            # --- the model PROPOSED a conclusion via submit_findings (ADR-18) ----
            proposal = findings_blocks[0]
            findings = _parse_findings(proposal.input)
            _emit(on_event, {
                "type": "tool",
                "name": FINDINGS_TOOL_NAME,
                "summary": f"{FINDINGS_TOOL_NAME}: {findings['verdict_sentence'][:160]}",
            })
            text = _text_from(resp.content)
            if text:
                last_answer = text   # audit attachment (demoted, not deleted)

            def _reject(feedback: str) -> None:
                """Continue the loop: the API requires a tool_result for EVERY
                tool_use block, so the judge's feedback rides back as the
                submit_findings tool_result (plus results for any data tools that
                shared the turn)."""
                rejected = tool_result_blocks + [
                    {"type": "tool_result", "tool_use_id": b.id, "content": feedback}
                    for b in findings_blocks
                ]
                messages.append({"role": "user", "content": rejected})

            if judge is None:
                # ADR-4 path: no judge — the structured proposal is terminal.
                return _mk(TerminalReason.MODEL_STOPPED, None)

            if not findings["key_evidence"]:
                # FAIL CLOSED (ADR-18): an answer citing nothing is unverifiable by
                # construction — it never reaches the judge and can never terminate
                # trusted. Repairable: the model may re-submit with citations.
                repairs += 1
                if repairs > repair_cap:
                    return _mk(TerminalReason.ABANDONED, last_verdict)
                _reject(_MISSING_KEY_EVIDENCE)
                continue

            # ADR-6: the judge adjudicates the proposal. Grounding (G) gates first.
            # The judge heads read the SERIALIZED structured fields (ADR-18), never
            # the generator's free prose; key_evidence goes to the INTEGRITY head.
            _emit(on_event, {"type": "grounding", "status": "checking", "facts": len(evidence)})
            tracker.begin("judge.evaluate (grounding + grade)")
            verdict: JudgeVerdict = judge.evaluate(
                predicate or task, _serialize_findings(findings), evidence,
                key_evidence=findings["key_evidence"],
            )
            tracker.end()
            last_verdict = verdict
            if verdict.grounded:
                _emit(on_event, {"type": "grounding", "status": "verified", "facts": len(evidence)})
            else:
                # Item names only (the same strings the repair prompt shows the
                # model) — no provenance receipts.
                _emit(on_event, {
                    "type": "grounding",
                    "status": "failed",
                    "reasons": list(verdict.ungrounded_items)[:5],
                })

            if not verdict.grounded:
                # ADR-13: an INTEGRITY failure (re-fetch mismatch / missing backend)
                # is a source-drift or config event the generator cannot fix by
                # re-answering — terminate immediately rather than burn repair_cap.
                if verdict.deterministic_failure:
                    return _mk(TerminalReason.ABANDONED, verdict)
                # Otherwise it's an answer-support / citation failure (the generator
                # mis-stated a figure or mis-cited a probe) — that IS repairable.
                repairs += 1
                if repairs > repair_cap:
                    return _mk(TerminalReason.ABANDONED, verdict)
                _reject(_repair_prompt(verdict))
                continue

            if verdict.open_questions:
                # Grounded but incomplete — send it back to gather more (ADR-1 loop).
                _reject(_continue_prompt(verdict))
                continue

            # Grounded, no open questions. The predicate is answered either way —
            # CONFIRMED or REFUTED are BOTH resolutions (ADR-11). Only a genuinely
            # INDETERMINATE verdict is the (useful) inconclusive terminal state.
            if verdict.confirm in (Confirm.CONFIRMED, Confirm.REFUTED):
                return _mk(TerminalReason.RESOLVED, verdict)
            return _mk(TerminalReason.INCONCLUSIVE, verdict)

        # --- the model stopped WITHOUT calling submit_findings ------------------
        text = _text_from(resp.content)
        if text:
            last_answer = text

        # Protocol guard: a non-"tool_use" stop_reason can still carry tool_use
        # blocks — max_tokens truncating the model mid-call is the common case
        # (ADR-18 made the final turn a large JSON tool call, so the token limit
        # now truncates protocol, not prose). The API requires a tool_result for
        # EVERY tool_use id in the next message; a bare text nudge here poisons
        # the transcript and 400s every subsequent request.
        dangling = [b for b in resp.content if getattr(b, "type", None) == "tool_use"]
        if dangling:
            messages.append({"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": b.id, "is_error": True,
                 "content": ("This tool call was cut off before it completed "
                             f"(stop_reason={resp.stop_reason}). Re-issue the call, "
                             "more concisely if it was long.")}
                for b in dangling
            ]})
            continue

        if judge is None:
            # ADR-4 path: no judge, a bare end_turn is terminal (MODEL_STOPPED).
            return _mk(TerminalReason.MODEL_STOPPED, None)

        # With a judge attached, prose end_turn is the model STALLING (ADR-18):
        # there is no structured proposal to adjudicate, so nudge it toward
        # submit_findings and continue — the turn ceiling still bounds this.
        messages.append({"role": "user", "content": _NUDGE_FINDINGS})
        continue

    # Turn ceiling hit before any terminal verdict — a real terminal state, not an error.
    # Carry the last judge verdict if one exists (audit #6: don't drop it).
    return _mk(TerminalReason.CAP_REACHED, last_verdict)
