"""The termination judge (ADR-1 rubric, ADR-6 wiring).

The judge decides when an investigation stops. It grades a *proposed conclusion*
(the generator's answer when it stops calling tools) on ADR-1's three axes:

  G — Grounded?  Is every retained evidence value real, verified independently?
  C — Confirm?   Does the grounded evidence resolve the predicate?
  O — Open?      Do open questions remain?

Two hard rules from ADR-1/ADR-2/ADR-6:
  * G is a **precondition**, checked FIRST. If G fails, C and O are untrustworthy
    (same reasoning produced them) and are not consulted — route to repair.
  * G is **deterministic code, not a model call**. The judge re-fetches each
    evidence item from the golden source via its provenance and compares with `==`.
    It never trusts the in-process `evidence` values. C and O — semantic — use the
    judge *model*, and only after G passes.

The judge is the first consumer of the receipts ADR-5 withheld from the generator.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from enum import Enum
from typing import Any, Callable

from .tools.contracts import FeatureResult, GroundingMode

JUDGE_MODEL = "claude-sonnet-5"  # cheaper tier than the Opus generator (ADR-6);
#                                  same family, so only PARTIAL decorrelation.

# A function that independently re-fetches from the golden source. Same signature
# as `feature_history` — inject the fake today, the real BQ tool later (ADR-6).
ReverifyFn = Callable[[str, date, int, list], list[FeatureResult]]


class Confirm(str, Enum):
    """How the evidence bears on the predicate (ADR-11 — three-valued, restoring
    ADR-1's confirm/refute axis). A two-valued resolved/unresolved collapsed
    'refuted' into 'unresolved', so the judge graded rejected hypotheses
    inconsistently. CONFIRMED and REFUTED are BOTH resolutions; only INDETERMINATE
    is genuine ambiguity."""

    CONFIRMED = "confirmed"          # evidence supports the predicate (a definite yes)
    REFUTED = "refuted"              # evidence contradicts the predicate (a definite no — a RESOLUTION)
    INDETERMINATE = "indeterminate"  # evidence genuinely cannot decide either way


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's grade on one proposed conclusion. `grounded` is the gate; when
    it is False, `confirm`/`open_questions` are not populated (never trusted)."""

    grounded: bool
    confirm: Confirm | None
    open_questions: bool | None
    reasoning: str
    ungrounded_items: tuple[str, ...] = ()  # which evidence/claims failed grounding
    deterministic_failure: bool = False     # a re-fetch integrity failure (ADR-13): the
    #                                         generator CANNOT fix this (source drift /
    #                                         config), so the loop must not waste repair
    #                                         cycles on it — route straight to ABANDONED.


def _judgment_tool() -> dict:
    """Strict tool schema for the model half (C, O) — dogfoods ADR-3 on the judge.

    Note what is ABSENT: no `grounded` field. Grounding is decided by code before
    the model is ever called, so we do not let the model opine on it."""
    return {
        "name": "submit_judgment",
        "description": (
            "Submit your grade of the investigator's proposed conclusion. Judge "
            "ONLY whether the provided evidence resolves the stated predicate and "
            "whether open questions remain. Do not assess whether the numbers are "
            "real — that has already been verified independently."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "confirm": {
                    "type": "string",
                    "enum": [c.value for c in Confirm],
                    "description": (
                        "How the evidence bears on the predicate: 'confirmed' (evidence supports it), "
                        "'refuted' (evidence contradicts it — a definite NO, which IS a resolution), or "
                        "'indeterminate' (evidence genuinely cannot decide either way). A hypothesis you "
                        "REJECTED on the evidence is 'refuted', NOT 'indeterminate'."
                    ),
                },
                "open_questions": {
                    "type": "boolean",
                    "description": "True if a specific, answerable question remains that more tool calls could resolve.",
                },
                "reasoning": {
                    "type": "string",
                    "description": "One or two sentences citing the evidence that drove the verdict.",
                },
            },
            "required": ["confirm", "open_questions", "reasoning"],
            "additionalProperties": False,
        },
    }


def _grounding_tool() -> dict:
    """Strict schema for the SEMANTIC grounding head (ADR-7). The model reports
    whether the passages support the answer's narrative claims — it does not opine
    on structured figures (those are checked by `==`, never by a model)."""
    return {
        "name": "submit_grounding",
        "description": (
            "Report whether the investigator's answer faithfully characterizes the "
            "provided filing passages. List any narrative claim the passages do not support."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "supported": {
                    "type": "boolean",
                    "description": "True only if every claim about the narrative follows from the passages.",
                },
                "unsupported_claims": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Each answer-claim about the narrative that the passages do not support (empty if all supported).",
                },
                "reasoning": {"type": "string", "description": "One or two sentences justifying the call."},
            },
            "required": ["supported", "unsupported_claims", "reasoning"],
            "additionalProperties": False,
        },
    }


class Judge:
    # Sentinel for "no reverify backend for this source" cached in the memo, kept
    # distinct from a legitimately-cached None (feature absent from the fresh row).
    _NO_BACKEND = object()

    def __init__(self, client: Any, reverify: "ReverifyFn | dict[str, ReverifyFn]", model: str = JUDGE_MODEL) -> None:
        self._client = client       # the judge MODEL client (C, O only)
        # Deterministic re-fetch backend(s) for G (ADR-9). A single callable is the
        # ONE-structured-backend shorthand; a {source: callable} map routes each item
        # to the backend that produced it, keyed by provenance.source. The map is
        # REQUIRED once >1 structured tool is in play — re-fetching a balance-sheet
        # value against the feature backend finds nothing (the lone reverify was a
        # hidden one-backend assumption the 2nd structured tool exposed).
        self._reverify = reverify
        self._model = model
        # Session-level reverify memo (latency #2): keyed by full re-fetch identity
        # (source, ticker, requested_report_date, requested_offset, feature) → the
        # fresh source-side FeatureResult (or None / _NO_BACKEND). Evidence accumulates
        # across a run's continue/repair cycles, so `evaluate()` was re-grounding pass
        # 1's items again on pass 2. This cache makes each identity re-fetched at most
        # ONCE per Judge instance. It caches only the SOURCE truth, never the
        # generator's (possibly tampered) value, so tamper detection is unaffected.
        self._verify_cache: dict = {}

    def _backend_for(self, item) -> "ReverifyFn | None":
        if callable(self._reverify):
            return self._reverify
        return self._reverify.get(item.provenance.source)

    def _reverify_deterministic(self, deterministic: list) -> tuple[list[str], bool]:
        """INTEGRITY head of G, batched + memoized. Returns (failed_items, det_failure).

        Semantics are byte-identical to the original per-item loop — each deterministic
        item is re-fetched by replaying its EXACT request (requested_report_date +
        requested_offset, routed by provenance.source) and compared on (status, value).
        Only the QUERY COUNT changes:
          * de-dup by re-fetch identity, first-seen order (audit #12);
          * skip anything already re-verified this run (session memo, latency #2);
          * fold all of a (source, ticker)'s remaining probes into ONE query when the
            backend exposes `.batch` — the backend already pulls the ticker's whole
            history, so one round-trip grounds every offset (latency #1);
          * fetch the (source, ticker) groups in parallel (latency #4).
        The failed list is built by iterating the de-duped items in first-seen order,
        so its contents/order do NOT depend on thread scheduling."""
        # 1) De-dup by identity, preserving first-seen order.
        unique: list[tuple[tuple, Any]] = []
        seen: set = set()
        for item in deterministic:
            p = item.provenance
            identity = (p.source, p.ticker, p.requested_report_date, p.requested_offset, item.feature)
            if identity in seen:
                continue
            seen.add(identity)
            unique.append((identity, item))

        # 2) Group the not-yet-memoized identities by (source, ticker) for batched fetch.
        groups: dict[tuple[str, str], list[tuple[tuple, Any]]] = {}
        for identity, item in unique:
            if identity in self._verify_cache:
                continue
            groups.setdefault((item.provenance.source, item.provenance.ticker), []).append((identity, item))

        # 3) Fetch each group; parallelize across groups (distinct sources/tickers).
        def _fetch(key_members: tuple) -> tuple:
            (source, ticker), members = key_members
            backend = self._backend_for(members[0][1])
            if backend is None:
                return key_members[0], None  # no reverify backend for this source
            # One probe per (requested_report_date, requested_offset); features batched.
            by_probe: dict[tuple, list[str]] = {}
            for _identity, it in members:
                pp = it.provenance
                by_probe.setdefault((pp.requested_report_date, pp.requested_offset), []).append(it.feature)
            batch = getattr(backend, "batch", None)
            if batch is not None:
                grouped = batch(ticker, [(rd, off, feats) for (rd, off), feats in by_probe.items()])
            else:
                # Plain callables / fakes / injected lambdas: per-(rd, off) call, features batched.
                grouped = {(rd, off): backend(ticker, rd, off, list(feats)) for (rd, off), feats in by_probe.items()}
            return key_members[0], grouped

        if len(groups) > 1:
            with ThreadPoolExecutor(max_workers=len(groups)) as pool:
                fetched = list(pool.map(_fetch, groups.items()))
        else:
            fetched = [_fetch(kv) for kv in groups.items()]

        # 4) Fold fetch results into the session memo, keyed by full identity.
        for key, grouped in fetched:
            for identity, item in groups[key]:
                if grouped is None:
                    self._verify_cache[identity] = self._NO_BACKEND
                    continue
                pp = item.provenance
                fresh_list = grouped.get((pp.requested_report_date, pp.requested_offset), [])
                self._verify_cache[identity] = next((r for r in fresh_list if r.feature == item.feature), None)

        # 5) Compare — first-seen order, byte-identical to the per-item path.
        failed: list[str] = []
        deterministic_failure = False
        for identity, item in unique:
            cached = self._verify_cache.get(identity)
            prov = item.provenance
            if cached is self._NO_BACKEND:
                failed.append(f"{item.feature}@{prov.resolved_report_date} (no reverify backend for {prov.source!r})")
                deterministic_failure = True
                continue
            match = cached
            if match is None or match.status != item.status or match.value != item.value:
                failed.append(f"{item.feature}@{prov.resolved_report_date}")
                deterministic_failure = True
        return failed, deterministic_failure

    # --- G: two-headed, dispatched by the evidence's declared mode (ADR-7) ----

    def _check_grounding(self, evidence: list, answer: str = "") -> tuple[bool, list[str], bool]:
        """Grounding gate. Returns (grounded, failed_items, deterministic_failure).

        Two heads (ADR-13 hardened):
          1. INTEGRITY (deterministic, per structured item): re-fetch by REPLAYING
             the original request (requested_report_date + requested_offset), route
             by provenance.source, compare (status, value). Confirms the evidence is
             authentic. A failure here is a source-drift/config event the generator
             CANNOT fix → `deterministic_failure=True` (unrepairable).
          2. ANSWER-SUPPORT (model, ALWAYS runs when there's an answer + evidence):
             does every factual claim in the ANSWER — every figure and every
             characterization — follow from the (now-authentic) evidence? This is
             the guard against the generator writing a number it never fetched, and
             it runs for numbers-only investigations too (ADR-13 fix: previously it
             only ran when narrative evidence happened to exist). A failure here is
             the generator's mis-statement → repairable."""
        deterministic = [e for e in evidence if e.grounding_mode is GroundingMode.DETERMINISTIC]

        # Head 1 — INTEGRITY: replay the exact request and compare, byte-identical to
        # the per-item loop but BATCHED (latency #1) — one query per (source, ticker)
        # instead of one per feature × offset — and MEMOIZED across evaluate() cycles
        # (latency #2). See `_reverify_deterministic`.
        failed, deterministic_failure = self._reverify_deterministic(deterministic)

        # Head 2 — ANSWER-SUPPORT: does the answer's every claim follow from the
        # evidence? Always runs (numbers AND prose) — the real anti-hallucination
        # gate. Skipped when integrity already failed: the evidence is untrustworthy,
        # so checking the answer against it is pointless, and this preserves ADR-1's
        # "don't consult the model on ungrounded output" (no wasted model call).
        if not deterministic_failure and answer.strip() and evidence:
            supported, unsupported = self._check_answer_support(answer, evidence)
            if not supported:
                failed.extend(unsupported or ["answer claims not supported by evidence"])

        return (len(failed) == 0, failed, deterministic_failure)

    def _check_answer_support(self, answer: str, evidence: list) -> tuple[bool, list[str]]:
        """The ANSWER-SUPPORT head of G (ADR-13): does every factual claim in the
        answer — numeric or narrative — follow from the verified evidence? A model
        call, because it must understand derived figures (0.55→0.95 is +0.40) and
        prose paraphrase, neither of which survives an `==`. Catches the generator
        citing a number it never fetched or mischaracterizing a passage."""
        view = json.dumps([self._view(e) for e in evidence])
        prompt = (
            "An investigator was given ONLY this verified evidence (numbers re-fetched "
            f"from source, passages verbatim):\n{view}\n\n"
            f"It then wrote this answer:\n{answer}\n\n"
            "Does EVERY factual claim in the answer follow from this evidence — every "
            "figure (allowing correct arithmetic on the given numbers) and every "
            "characterization of the narrative? Flag any claim not supported: a number "
            "that is not in the evidence and is not derivable from it, or a "
            "characterization the passages do not support. Call submit_grounding."
        )
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            tools=[_grounding_tool()],
            tool_choice={"type": "tool", "name": "submit_grounding"},
            messages=[{"role": "user", "content": prompt}],
        )
        payload = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        if payload is None:
            return (False, ["semantic grounding check produced no verdict"])
        # Parse defensively: `required` in the tool schema is a hint to the model,
        # not a runtime guarantee — a "supported: true" verdict routinely omits the
        # empty `unsupported_claims` array. A missing `supported` key fails closed
        # (treat as not-supported), matching the no-payload branch above; a missing
        # claims list is just an empty list. (Was a bare `payload["..."]` → KeyError
        # → the service's blanket except → 500 that killed the whole investigation.)
        supported = bool(payload.get("supported", False))
        unsupported = list(payload.get("unsupported_claims") or [])
        return (supported, unsupported)

    # --- C, O: the judge model ----------------------------------------------

    @staticmethod
    def _view(e) -> dict:
        """Render one evidence item for the C/O grader — mode-aware, so a MIXED pile
        of numbers and passages renders without assuming one type (ADR-7). Same
        'tell, don't ask' dispatch the grounding gate uses."""
        if e.grounding_mode is GroundingMode.SEMANTIC:
            return {"kind": "narrative", "section": e.section, "status": e.status.value, "passage": e.passage}
        return {
            "kind": "metric",
            "feature": e.feature,
            "status": e.status.value,
            "value": e.value,
            "quarter": e.provenance.resolved_report_date.isoformat(),
        }

    def _grade_semantics(self, predicate: str, answer: str, evidence: list) -> JudgeVerdict:
        # The model sees the GENERATOR-facing view of the (now-verified) evidence —
        # it does not need provenance to judge C/O; provenance was G's concern.
        evidence_view = json.dumps([self._view(e) for e in evidence])
        prompt = (
            f"PREDICATE (the question the investigation must resolve):\n{predicate}\n\n"
            f"INVESTIGATOR'S PROPOSED CONCLUSION:\n{answer}\n\n"
            f"VERIFIED EVIDENCE (already confirmed grounded):\n{evidence_view}\n\n"
            "Grade this conclusion. Does the evidence resolve the predicate? Do open "
            "questions remain? Call submit_judgment."
        )
        resp = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            tools=[_judgment_tool()],
            tool_choice={"type": "tool", "name": "submit_judgment"},  # force the grade
            messages=[{"role": "user", "content": prompt}],
        )
        payload = next(
            (b.input for b in resp.content if getattr(b, "type", None) == "tool_use"),
            None,
        )
        if payload is None:
            # Audit #10 — FAIL CLOSED. A broken judge response (no grade) must not
            # `open_questions=True` and loop: that burns the whole budget rubber-
            # stamping a non-functioning judge until CAP_REACHED. Terminate clean as
            # INDETERMINATE with no open questions → the loop stops INCONCLUSIVE, an
            # honest "the judge couldn't decide," rather than spinning.
            return JudgeVerdict(True, Confirm.INDETERMINATE, False, "judge produced no verdict")
        return JudgeVerdict(
            grounded=True,
            confirm=Confirm(payload["confirm"]),
            open_questions=bool(payload["open_questions"]),
            reasoning=payload["reasoning"],
        )

    # --- public: G first, then C/O ------------------------------------------

    def evaluate(self, predicate: str, answer: str, evidence: list[FeatureResult]) -> JudgeVerdict:
        """Grade a proposed conclusion. Grounding gates everything (ADR-1)."""
        grounded, failed, deterministic_failure = self._check_grounding(evidence, answer)
        if not grounded:
            # Short-circuit: C and O would be produced by the same untrustworthy
            # reasoning, so they are not consulted. `deterministic_failure` tells the
            # loop whether this is repairable (answer mis-statement) or not (integrity).
            kind = "integrity re-check failed" if deterministic_failure else "answer not supported by evidence"
            return JudgeVerdict(
                grounded=False,
                confirm=None,
                open_questions=None,
                reasoning=f"grounding failed ({kind}): {failed}",
                ungrounded_items=tuple(failed),
                deterministic_failure=deterministic_failure,
            )
        return self._grade_semantics(predicate, answer, evidence)
