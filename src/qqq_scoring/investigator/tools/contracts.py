"""Shared tool-result contract (ADR-2).

Every investigator tool returns results wrapped in this envelope so the judge
(ADR-1) gets a *uniform* grounding interface regardless of which tool produced
the evidence. `feature_history` is the first tool; the others reuse these types.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class FeatureStatus(str, Enum):
    """Mutually-exclusive outcomes of one (feature, period) lookup.

    A status enum, not a bag of booleans (ADR-2 principle 2): the three legal
    states are the only spellable ones, so illegal combinations can't be
    constructed. These map directly onto ADR-1's terminal states — mistaking
    PERIOD_NOT_FILED for a null/0 value is what would manufacture a false
    `refuted`/`failed` where the truth is `inconclusive`.
    """

    FOUND = "found"                       # value is present and real
    FEATURE_MISSING = "feature_missing"   # period was filed, but this ratio couldn't be computed
    PERIOD_NOT_FILED = "period_not_filed"  # the target quarter doesn't exist yet (future hasn't arrived)


@dataclass(frozen=True)
class Provenance:
    """The paper trail that makes a value verifiable (ADR-2 principle 3).

    Membership test: delete a field; if a skeptic can still independently
    re-reach the value and confirm it, the field didn't belong here. For
    structured (BigQuery) data, grounding is checked by *re-running* `query` and
    comparing with `==` — not by asking a model whether 0.87 equals 0.87.
    """

    source: str                 # golden table, e.g. "qqq_finance.period_features"
    resolved_report_date: date  # the quarter the tool ACTUALLY landed on after
    #                             applying period_offset — guards the calendar-math
    #                             trap: catches a claim citing the wrong period.
    query: str                  # the exact, re-runnable lookup (the verification handle)
    retrieved_at: datetime      # restatements change values, so the read is timestamped
    accession_number: str | None = None  # exact source row/filing; None when the
    #                                       row doesn't exist (PERIOD_NOT_FILED)


@dataclass(frozen=True)
class FeatureResult:
    """One feature's value at one resolved period, plus its provenance."""

    feature: str
    status: FeatureStatus
    value: float | None       # populated iff status is FOUND
    provenance: Provenance

    def __post_init__(self) -> None:
        # Enforce ADR-2 principle 2 at run-time, not just by convention: value is
        # present IFF status is FOUND. This makes the illegal states fail loudly at
        # construction instead of silently flowing downstream as a bad conclusion.
        if self.status is FeatureStatus.FOUND and self.value is None:
            raise ValueError("FOUND result must carry a value")
        if self.status is not FeatureStatus.FOUND and self.value is not None:
            raise ValueError(f"{self.status.value} result must not carry a value")
