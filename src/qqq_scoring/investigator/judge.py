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

from .tools.contracts import FeatureResult

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


class Judge:
    def __init__(self, client: Any, reverify: ReverifyFn, model: str = JUDGE_MODEL) -> None:
        self._client = client       # the judge MODEL client (C, O only)
        self._reverify = reverify   # independent re-fetch for G (deterministic)
        self._model = model

    # --- G: deterministic, no model call ------------------------------------

    def _check_grounding(self, evidence: list[FeatureResult]) -> tuple[bool, list[str]]:
        """Re-fetch each evidence item from source; it is grounded iff every
        (status, value) still matches. Trusts provenance, not the handed value."""
        failed: list[str] = []
        for item in evidence:
            prov = item.provenance
            # Re-dispatch by identity (ticker + resolved quarter + feature), offset 0.
            # Backend-agnostic: fake today, BQ later — the query string is not parsed.
            fresh = self._reverify(prov.ticker, prov.resolved_report_date, 0, [item.feature])
            match = next((r for r in fresh if r.feature == item.feature), None)
            if match is None or match.status != item.status or match.value != item.value:
                failed.append(f"{item.feature}@{prov.resolved_report_date}")
        return (len(failed) == 0, failed)

    # --- C, O: the judge model ----------------------------------------------

    def _grade_semantics(self, predicate: str, answer: str, evidence: list[FeatureResult]) -> JudgeVerdict:
        # The model sees the GENERATOR-facing view of the (now-verified) evidence —
        # it does not need provenance to judge C/O; provenance was G's concern.
        evidence_view = json.dumps(
            [
                {
                    "feature": e.feature,
                    "status": e.status.value,
                    "value": e.value,
                    "resolved_report_date": e.provenance.resolved_report_date.isoformat(),
                }
                for e in evidence
            ]
        )
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
        grounded, failed = self._check_grounding(evidence)
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
