"""Shared tool-result contract (ADR-2).

Every investigator tool returns results wrapped in this envelope so the judge
(ADR-1) gets a *uniform* grounding interface regardless of which tool produced
the evidence. `feature_history` is the first tool; the others reuse these types.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum


class GroundingMode(str, Enum):
    """How a piece of evidence must be verified (ADR-7).

    The evidence carries this so the judge dispatches on it instead of branching on
    Python type ("tell, don't ask") — a new tool declares its mode and the judge is
    untouched. The two modes exist because a number and a paragraph are verified
    differently in KIND, which is exactly why they are separate result types.
    """

    DETERMINISTIC = "deterministic"  # structured: re-fetch by identity, compare `==`
    SEMANTIC = "semantic"            # unstructured: a model checks passages support the claim


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
    ticker: str                 # source identity (with the request fields below, the
    #                             backend-agnostic re-dispatch handle the JUDGE uses).
    resolved_report_date: date  # the quarter the tool ACTUALLY landed on after
    #                             applying the offset — for the model/human to read.
    #                             NOT the reverify key (ADR-13): for a not-filed
    #                             period this is a fallback, so replaying it lands on
    #                             the wrong row. Reverify replays the REQUEST, below.
    requested_report_date: date  # the ANCHOR the agent asked from — the reverify key
    requested_offset: int        # the signed offset the agent asked for — the reverify key.
    #                             Replaying (requested_report_date, requested_offset)
    #                             reproduces the EXACT original probe, so re-grounding
    #                             is correct for FOUND, FEATURE_MISSING, AND
    #                             PERIOD_NOT_FILED alike (ADR-13 fixes the SEV-1 bug
    #                             where a not-filed probe re-grounded as FOUND).
    query: str                  # the exact, re-runnable lookup (the human/BQ-facing handle)
    retrieved_at: datetime      # restatements change values, so the read is timestamped
    accession_number: str | None = None  # exact source row/filing; None when the
    #                                       row doesn't exist (PERIOD_NOT_FILED)


@dataclass(frozen=True)
class FeatureResult:
    """One feature's value at one resolved period, plus its provenance (structured)."""

    feature: str
    status: FeatureStatus
    value: float | None       # populated iff status is FOUND
    provenance: Provenance
    grounding_mode: GroundingMode = GroundingMode.DETERMINISTIC  # ADR-7: verify by re-fetch + ==

    def __post_init__(self) -> None:
        # Enforce ADR-2 principle 2 at run-time, not just by convention: value is
        # present IFF status is FOUND. This makes the illegal states fail loudly at
        # construction instead of silently flowing downstream as a bad conclusion.
        if self.status is FeatureStatus.FOUND and self.value is None:
            raise ValueError("FOUND result must carry a value")
        if self.status is not FeatureStatus.FOUND and self.value is not None:
            raise ValueError(f"{self.status.value} result must not carry a value")


# ---------------------------------------------------------------------------
# Unstructured (narrative) evidence — a SIBLING type, not a generalization of
# FeatureResult (ADR-7). Prose has no numeric value, its provenance is a passage
# locator (not a re-runnable query), and it is grounded SEMANTICALLY. Because it
# is verified differently in kind, it is a different type — the type system then
# forbids a float ever landing in a passage slot.
# ---------------------------------------------------------------------------


class NarrativeStatus(str, Enum):
    """Mutually-exclusive outcomes of one (filing, section) lookup.

    Distinct reasons-for-absence from FeatureStatus: a SECTION can be absent from a
    filing that otherwise exists, which is different from a whole filing not being
    found — and neither must be mistaken for an empty passage the judge would grade
    as unsupported."""

    FOUND = "found"                       # the section exists; passage returned
    SECTION_ABSENT = "section_absent"     # the filing exists but omits this section
    FILING_NOT_FOUND = "filing_not_found"  # no narrative document for this ticker/period


@dataclass(frozen=True)
class NarrativeProvenance:
    """Locator that lets a skeptic re-reach the passage (ADR-2 principle 3, prose).

    Not a re-runnable query — a crisp human-verifiable address: which section of
    which filing. That address is a *cleaner* grounding handle than an embedding
    chunk id, which is one reason ADR-7 chose section-addressed retrieval."""

    source: str            # the golden document, e.g. the GCS narrative json path
    ticker: str
    report_date: date
    form: str              # e.g. "10-Q"
    section: str           # the named section addressed, e.g. "mdna"
    retrieved_at: datetime


@dataclass(frozen=True)
class NarrativeResult:
    """One section's prose at one filing, plus its provenance (unstructured)."""

    section: str
    status: NarrativeStatus
    passage: str | None    # populated iff status is FOUND
    provenance: NarrativeProvenance
    grounding_mode: GroundingMode = GroundingMode.SEMANTIC  # ADR-7: verify by model support-check

    def __post_init__(self) -> None:
        # Same illegal-state guard as FeatureResult, one type over: a passage is
        # present IFF the section was FOUND. An absent section must not carry text.
        if self.status is NarrativeStatus.FOUND and self.passage is None:
            raise ValueError("FOUND narrative result must carry a passage")
        if self.status is not NarrativeStatus.FOUND and self.passage is not None:
            raise ValueError(f"{self.status.value} narrative result must not carry a passage")
