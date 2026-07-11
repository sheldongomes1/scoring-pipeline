"""In-memory fixture backend for `balance_sheet_items` (ADR-9).

Closes the gap the live disambiguation run found: the working-capital and
revenue-quality branches now have receivables/payables/content-asset figures to
test against. The numbers are written to make the WBD story RESOLVABLE and honest:

  - accounts_receivable FLAT→DOWN (7200 → 7050) and accounts_payable slightly UP
    (3100 → 3250): working capital is NOT trapping cash — it's a mild source of
    cash — so the working-capital hypothesis (h2) should be grounded-REJECTED.
  - content_assets amortizing DOWN (33000 → 30000): consistent with the MD&A's
    "elevated content amortization" — the non-cash charge IS the real driver.
  - inventory absent for a media company → exercises FEATURE_MISSING.

Values are $M. Swapping this for the real FMP/BQ read touches zero judge/loop code.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .contracts import FeatureResult, FeatureStatus, Provenance

SOURCE = "qqq_finance.balance_sheet"  # logical golden source; the judge's reverify-map key

_QUARTER_ENDS = [
    date(2025, 3, 31),
    date(2025, 6, 30),
    date(2025, 9, 30),
    date(2025, 12, 31),
    date(2026, 3, 31),
]
_FILED_THROUGH = date(2025, 12, 31)

# (ticker, resolved_quarter) -> {item: value_$M}. inventory omitted for WBD (media
# co) to exercise FEATURE_MISSING.
_DATA: dict[tuple[str, date], dict[str, float]] = {
    ("WBD", date(2025, 3, 31)): {"accounts_receivable": 7200, "accounts_payable": 3100, "content_assets": 33000, "total_debt": 40000, "cash_and_equivalents": 3000, "deferred_revenue": 1200},
    ("WBD", date(2025, 6, 30)): {"accounts_receivable": 7150, "accounts_payable": 3150, "content_assets": 32000, "total_debt": 39500, "cash_and_equivalents": 2800, "deferred_revenue": 1250},
    ("WBD", date(2025, 9, 30)): {"accounts_receivable": 7100, "accounts_payable": 3200, "content_assets": 31000, "total_debt": 39000, "cash_and_equivalents": 2600, "deferred_revenue": 1300},
    ("WBD", date(2025, 12, 31)): {"accounts_receivable": 7050, "accounts_payable": 3250, "content_assets": 30000, "total_debt": 38500, "cash_and_equivalents": 2500, "deferred_revenue": 1350},
    ("AAPL", date(2025, 6, 30)): {"accounts_receivable": 60000, "accounts_payable": 62000, "inventory": 6500, "total_debt": 100000, "cash_and_equivalents": 30000},
    ("AAPL", date(2025, 9, 30)): {"accounts_receivable": 66000, "accounts_payable": 65000, "inventory": 7000, "total_debt": 98000, "cash_and_equivalents": 34000},
}
_ACCESSION = {
    ("WBD", date(2025, 6, 30)): "0001437107-25-000024",
    ("WBD", date(2025, 9, 30)): "0001437107-25-000036",
}


def _resolve_quarter(report_date: date, period_offset: int) -> date | None:
    if report_date not in _QUARTER_ENDS:
        return None
    idx = _QUARTER_ENDS.index(report_date) + period_offset
    return _QUARTER_ENDS[idx] if 0 <= idx < len(_QUARTER_ENDS) else None


def balance_sheet_items(
    ticker: str,
    report_date: date,
    period_offset: int,
    items: list[str],
) -> list[FeatureResult]:
    """Drop-in fake for `balance_sheet_items` — same signature and FeatureResult type."""
    resolved = _resolve_quarter(report_date, period_offset)
    retrieved_at = datetime.now(timezone.utc)
    results: list[FeatureResult] = []

    for item in items:
        if resolved is None or resolved > _FILED_THROUGH:
            prov = Provenance(
                source=SOURCE,
                ticker=ticker,
                resolved_report_date=resolved or report_date,
                query=f"SELECT {item} FROM balance_sheet WHERE ticker='{ticker}' AND report_date='{resolved}'",
                retrieved_at=retrieved_at,
                accession_number=None,
            )
            results.append(FeatureResult(item, FeatureStatus.PERIOD_NOT_FILED, None, prov))
            continue

        row = _DATA.get((ticker, resolved), {})
        prov = Provenance(
            source=SOURCE,
            ticker=ticker,
            resolved_report_date=resolved,
            query=f"SELECT {item} FROM balance_sheet WHERE ticker='{ticker}' AND report_date='{resolved}'",
            retrieved_at=retrieved_at,
            accession_number=_ACCESSION.get((ticker, resolved)),
        )
        if item in row:
            results.append(FeatureResult(item, FeatureStatus.FOUND, float(row[item]), prov))
        else:
            results.append(FeatureResult(item, FeatureStatus.FEATURE_MISSING, None, prov))

    return results
