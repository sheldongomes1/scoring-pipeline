"""In-memory fixture backend for `feature_history` (Phase 2 loop proof).

WHY THIS EXISTS: proving "the tool-use loop closes" is a function of the
tool-call round-trip, not of where the data lives (the same reasoning that let us
defer real BigQuery). So we inject this fake as the callable. It returns real,
contract-correct `FeatureResult`s — including a `PERIOD_NOT_FILED` — so the model
meets a genuine 3-state result and the harness is exercised end-to-end without a
live BigQuery dependency, credentials, or cost.

Swapping this for the real BigQuery body later touches ZERO lines of loop logic:
same signature, same return type. That is the seam working.

The tiny story in the data (AAPL): OCF/NI dipped to 0.55 at the flagged quarter
(2025-06-30) and recovered to 0.95 the next quarter (2025-09-30) — a textbook
`persistence_test` the agent can resolve with one `period_offset=+1` call.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .contracts import FeatureResult, FeatureStatus, Provenance

_SOURCE = "qqq_finance.period_features (in-memory fixture)"

# Quarter-end calendar for the fixture ticker. The tool — not the model — owns
# this calendar math: it resolves report_date + period_offset to a real quarter.
_QUARTER_ENDS = [
    date(2025, 3, 31),
    date(2025, 6, 30),   # <- the flagged filing (anchor)
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 3, 31),
]
# Everything filed up to and including this date exists; later quarters haven't
# been filed yet — that's the PERIOD_NOT_FILED case, distinct from a null value.
_FILED_THROUGH = date(2025, 12, 31)

# The fixture table: (ticker, resolved_quarter) -> {feature: value}.
# Two tickers with DELIBERATELY DIFFERENT stories, so the same loop code produces
# path-variance (ADR-1) — the model's tool-call sequence should diverge as a
# function of what it observes, not of the code:
#   AAPL  — cash conversion DIPS at the anchor then RECOVERS (0.55 -> 0.95). One
#           +1 lookup answers it; a diligent agent stops early.
#   WBD   — cash conversion dips and STAYS broken (0.50 -> 0.52 -> 0.49) while
#           accruals climb (0.09 -> 0.11 -> 0.13). Seeing no recovery at +1, a
#           diligent agent has reason to reach further — a later quarter, or the
#           corroborating accrual_ratio — to explain WHY. More edges, driven by
#           the observation, not by us.
# debt_to_assets is absent at AAPL 2025-09-30 to exercise FEATURE_MISSING (the
# period WAS filed, but that one ratio couldn't be computed).
_DATA: dict[tuple[str, date], dict[str, float]] = {
    ("AAPL", date(2025, 3, 31)): {"ocf_to_net_income": 1.10, "net_margin": 0.24, "debt_to_assets": 0.31},
    ("AAPL", date(2025, 6, 30)): {"ocf_to_net_income": 0.55, "net_margin": 0.19, "debt_to_assets": 0.33},
    ("AAPL", date(2025, 9, 30)): {"ocf_to_net_income": 0.95, "net_margin": 0.23},  # debt_to_assets missing
    ("AAPL", date(2025, 12, 31)): {"ocf_to_net_income": 0.98, "net_margin": 0.24, "debt_to_assets": 0.32},
    ("WBD", date(2025, 3, 31)): {"ocf_to_net_income": 0.72, "net_margin": 0.08, "accrual_ratio": 0.02},
    ("WBD", date(2025, 6, 30)): {"ocf_to_net_income": 0.50, "net_margin": 0.05, "accrual_ratio": 0.09},
    ("WBD", date(2025, 9, 30)): {"ocf_to_net_income": 0.52, "net_margin": 0.04, "accrual_ratio": 0.11},
    ("WBD", date(2025, 12, 31)): {"ocf_to_net_income": 0.49, "net_margin": 0.03, "accrual_ratio": 0.13},
}
_ACCESSION = {
    ("AAPL", date(2025, 3, 31)): "0000320193-25-000041",
    ("AAPL", date(2025, 6, 30)): "0000320193-25-000057",
    ("AAPL", date(2025, 9, 30)): "0000320193-25-000073",
    ("AAPL", date(2025, 12, 31)): "0000320193-25-000089",
    ("WBD", date(2025, 3, 31)): "0001437107-25-000012",
    ("WBD", date(2025, 6, 30)): "0001437107-25-000024",
    ("WBD", date(2025, 9, 30)): "0001437107-25-000036",
    ("WBD", date(2025, 12, 31)): "0001437107-25-000048",
}


def _resolve_quarter(report_date: date, period_offset: int) -> date | None:
    """report_date + period_offset -> the actual quarter-end, or None if off-calendar."""
    if report_date not in _QUARTER_ENDS:
        return None
    idx = _QUARTER_ENDS.index(report_date) + period_offset
    if 0 <= idx < len(_QUARTER_ENDS):
        return _QUARTER_ENDS[idx]
    return None


def feature_history_fake(
    ticker: str,
    report_date: date,
    period_offset: int,
    features: list[str],
) -> list[FeatureResult]:
    """Drop-in fake for `feature_history` — same signature, same return type."""
    resolved = _resolve_quarter(report_date, period_offset)
    retrieved_at = datetime.now(timezone.utc)
    results: list[FeatureResult] = []

    for feature in features:
        # Off-calendar or future quarter -> the period simply isn't filed yet.
        if resolved is None or resolved > _FILED_THROUGH:
            prov = Provenance(
                source=_SOURCE,
                ticker=ticker,
                resolved_report_date=resolved or report_date,
                query=f"SELECT {feature} FROM period_features WHERE ticker='{ticker}' AND report_date='{resolved}'",
                retrieved_at=retrieved_at,
                accession_number=None,  # no row exists to cite
            )
            results.append(FeatureResult(feature, FeatureStatus.PERIOD_NOT_FILED, None, prov))
            continue

        row = _DATA.get((ticker, resolved), {})
        prov = Provenance(
            source=_SOURCE,
            ticker=ticker,
            resolved_report_date=resolved,
            query=f"SELECT {feature} FROM period_features WHERE ticker='{ticker}' AND report_date='{resolved}'",
            retrieved_at=retrieved_at,
            accession_number=_ACCESSION.get((ticker, resolved)),
        )
        if feature in row:
            results.append(FeatureResult(feature, FeatureStatus.FOUND, row[feature], prov))
        else:
            # Period was filed, but this ratio couldn't be computed — NOT a zero.
            results.append(FeatureResult(feature, FeatureStatus.FEATURE_MISSING, None, prov))

    return results
