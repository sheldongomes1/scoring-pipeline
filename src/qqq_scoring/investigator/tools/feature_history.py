"""Tool 1 of the action space: BigQuery feature history.

Answers "what was feature F for ticker T, N quarters from the flagged filing?" —
the move that resolves a `persistence_test` (ADR-1). This is a STUB: the contract
(typed I/O + provenance envelope, ADR-2) is real, importable, and tested; the
BigQuery body is not wired yet.
"""
from __future__ import annotations

from datetime import date

from .contracts import FeatureResult


def feature_history(
    ticker: str,
    report_date: date,
    period_offset: int,      # signed: +1 = next quarter, -1 = prior, 0 = the filing's own quarter
    features: list[str],
) -> list[FeatureResult]:
    """Return one FeatureResult per requested feature at report_date + period_offset.

    The agent chooses these arguments at run-time; that run-time choice is what
    makes the loop agentic (ADR-1: the model owns the next edge). The tool — not
    the agent — owns the calendar math: it resolves `report_date + period_offset`
    to an actual quarter, so the model never does date arithmetic (a reliability
    leak). The resolved quarter is echoed back in `Provenance.resolved_report_date`
    so the judge can verify the value belongs to the period the agent claims.

    Input cardinality matches output cardinality: `features` is a list, so the
    return is one FeatureResult per requested feature (never a bare scalar).
    """
    raise NotImplementedError(
        "feature_history contract is stubbed; BigQuery lookup not wired yet. "
        "Contract standard: docs/agentic-investigation/decisions.md (ADR-2)."
    )
