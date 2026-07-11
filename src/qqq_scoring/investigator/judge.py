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
    RESOLVED = "resolved"       # the evidence answers the predicate
    UNRESOLVED = "unresolved"   # evidence is clean but genuinely ambiguous


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's grade on one proposed conclusion. `grounded` is the gate; when
    it is False, `confirm`/`open_questions` are not populated (never trusted)."""

    grounded: bool
    confirm: Confirm | None
    open_questions: bool | None
    reasoning: str
    ungrounded_items: tuple[str, ...] = ()  # which evidence failed re-verification


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
                    "description": "'resolved' if the evidence answers the predicate; 'unresolved' if evidence is clean but genuinely ambiguous.",
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

    def _backend_for(self, item) -> "ReverifyFn | None":
        if callable(self._reverify):
            return self._reverify
        return self._reverify.get(item.provenance.source)

    # --- G: two-headed, dispatched by the evidence's declared mode (ADR-7) ----

    def _check_grounding(self, evidence: list, answer: str = "") -> tuple[bool, list[str]]:
        """Grounding gate over MIXED evidence. Each item declares how to verify it
        (`grounding_mode`), so the judge dispatches instead of `isinstance`-branching.
        Overall grounded = deterministic items re-verify AND narrative items
        semantically support the answer. Either head failing → not grounded."""
        deterministic = [e for e in evidence if e.grounding_mode is GroundingMode.DETERMINISTIC]
        semantic = [e for e in evidence if e.grounding_mode is GroundingMode.SEMANTIC]
        failed: list[str] = []

        # Head 1 — structured: re-fetch by identity, compare ==. No model call.
        # Route to the backend that produced the item, by provenance.source (ADR-9).
        for item in deterministic:
            prov = item.provenance
            backend = self._backend_for(item)
            if backend is None:
                failed.append(f"{item.feature}@{prov.resolved_report_date} (no reverify backend for source {prov.source!r})")
                continue
            fresh = backend(prov.ticker, prov.resolved_report_date, 0, [item.feature])
            match = next((r for r in fresh if r.feature == item.feature), None)
            if match is None or match.status != item.status or match.value != item.value:
                failed.append(f"{item.feature}@{prov.resolved_report_date}")

        # Head 2 — narrative: a model checks the passages support the answer's
        # claims. You cannot == a paragraph; this catches the answer twisting the
        # prose ("management admitted weakness" when it said "we remain confident").
        if semantic:
            supported, unsupported = self._check_semantic_grounding(answer, semantic)
            if not supported:
                failed.extend(unsupported or ["narrative claims not supported by passages"])

        return (len(failed) == 0, failed)

    def _check_semantic_grounding(self, answer: str, narrative: list) -> tuple[bool, list[str]]:
        """The SEMANTIC head of G: does every characterization of the narrative in
        the answer follow from the retrieved passages? A model call — grounding is
        deterministic only for structured data (ADR-2/ADR-7)."""
        passages = json.dumps(
            [{"section": r.section, "status": r.status.value, "passage": r.passage} for r in narrative]
        )
        prompt = (
            "An investigator retrieved these EXACT filing passages (verbatim from "
            f"source):\n{passages}\n\n"
            f"It then wrote this answer:\n{answer}\n\n"
            "Does every claim the answer makes ABOUT THE NARRATIVE follow from those "
            "passages? Flag any characterization the passages do not support (e.g. "
            "claiming management conceded a problem when the passage frames it as "
            "temporary). Call submit_grounding."
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
        return (bool(payload["supported"]), list(payload["unsupported_claims"]))

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
            # The judge failing to grade is itself a grounding-style failure of the
            # judgment step — treat as unresolved+open so the loop doesn't stop clean.
            return JudgeVerdict(True, Confirm.UNRESOLVED, True, "judge produced no verdict")
        return JudgeVerdict(
            grounded=True,
            confirm=Confirm(payload["confirm"]),
            open_questions=bool(payload["open_questions"]),
            reasoning=payload["reasoning"],
        )

    # --- public: G first, then C/O ------------------------------------------

    def evaluate(self, predicate: str, answer: str, evidence: list[FeatureResult]) -> JudgeVerdict:
        """Grade a proposed conclusion. Grounding gates everything (ADR-1)."""
        grounded, failed = self._check_grounding(evidence, answer)
        if not grounded:
            # Short-circuit: C and O would be produced by the same untrustworthy
            # reasoning, so they are not consulted. Route to repair.
            return JudgeVerdict(
                grounded=False,
                confirm=None,
                open_questions=None,
                reasoning=f"grounding failed: {failed} did not re-verify against source",
                ungrounded_items=tuple(failed),
            )
        return self._grade_semantics(predicate, answer, evidence)
