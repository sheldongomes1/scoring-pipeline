"""REAL BigQuery backend for `feature_history` — makes one tool production-real.

Drop-in for `feature_history_fake`: same signature, same `FeatureResult` return,
same `SOURCE` key (so the judge's reverify-map entry doesn't change on the
fake→real swap — the payoff of cleaning the fake's source to the logical table
name in ADR-9). Reads `qqq_finance.period_features`.

Period resolution is POSITIONAL, not calendar (see module note): `period_offset`
counts FILED periods in the ticker's own history, because real fiscal calendars are
irregular (AAPL files 3 10-Qs/yr on shifting dates; adding 3 months lands on a
quarter that doesn't exist for that company). "+1" = "the next filed 10-Q."
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .contracts import FeatureResult, FeatureStatus, Provenance

SOURCE = "qqq_finance.period_features"                 # logical source == the fake's key (ADR-9)
_TABLE = "qqq-anomaly-lab.qqq_finance.period_features"  # fully-qualified for cross-project read


def _quote_ident(name: str) -> str:
    """Guard the feature name reaching the SQL SELECT (defense in depth — the model
    only ever picks from the enum, but the backend shouldn't trust that)."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"illegal feature identifier: {name!r}")
    return name


def feature_history_bq(
    ticker: str,
    report_date: date,
    period_offset: int,
    features: list[str],
    *,
    client=None,
    form: str = "10-Q",
) -> list[FeatureResult]:
    """Return one FeatureResult per feature at the ticker's report_date + offset-th
    FILED period. One query: pull the ticker's ordered filing history, resolve the
    target period positionally in Python, read the values off the resolved row."""
    from google.cloud import bigquery  # lazy import so the module loads without creds

    client = client or bigquery.Client()
    cols = ", ".join(_quote_ident(f) for f in features)
    # Pull BOTH quarterly (10-Q) and annual (10-K) filings so we can (a) index the
    # offset over 10-Qs only — annual figures aren't quarterly-comparable — and
    # (b) count the fiscal year-end filings the jump skipped (ADR-14).
    sql = (
        f"SELECT target_period_end, target_form, {cols} FROM `{_TABLE}` "
        f"WHERE ticker=@ticker AND target_form IN (@form, '10-K') "
        f"ORDER BY target_period_end"
    )
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter("ticker", "STRING", ticker),
                bigquery.ScalarQueryParameter("form", "STRING", form),
            ]
        ),
    )
    rows = list(job)
    retrieved_at = datetime.now(timezone.utc)

    # Positional resolution over the QUARTERLY rows only (time-base consistency).
    q_rows = [r for r in rows if r["target_form"] == form]
    annual_dates = [r["target_period_end"] for r in rows if r["target_form"] == "10-K"]
    periods = [r["target_period_end"] for r in q_rows]
    resolved: date | None = None
    if report_date in periods:
        idx = periods.index(report_date) + period_offset
        if 0 <= idx < len(periods):
            resolved = periods[idx]

    # Count fiscal year-end (10-K) periods strictly between anchor and resolved (ADR-14).
    periods_skipped = 0
    if resolved is not None:
        lo, hi = sorted((report_date, resolved))
        periods_skipped = sum(1 for d in annual_dates if lo < d < hi)

    row = q_rows[periods.index(resolved)] if resolved in periods else None

    results: list[FeatureResult] = []
    for feature in features:
        prov = Provenance(
            source=SOURCE,
            ticker=ticker,
            resolved_report_date=resolved or report_date,
            requested_report_date=report_date,   # ADR-13: reverify replays the REQUEST,
            requested_offset=period_offset,       # not resolved+0 (which mis-grounds not-filed)
            query=(
                f"SELECT {feature} FROM {SOURCE} WHERE ticker='{ticker}' "
                f"AND target_form='{form}' AND target_period_end='{resolved}'"
            ),
            retrieved_at=retrieved_at,
            accession_number=None,  # not carried in period_features; provenance rests on the query
        )
        if row is None:
            # anchor not in this ticker's filed history, or the offset ran off the end
            results.append(FeatureResult(feature, FeatureStatus.PERIOD_NOT_FILED, None, prov))
            continue
        value = row[feature]
        if value is None:
            results.append(FeatureResult(feature, FeatureStatus.FEATURE_MISSING, None, prov, periods_skipped=periods_skipped))
        else:
            results.append(FeatureResult(feature, FeatureStatus.FOUND, float(value), prov, periods_skipped=periods_skipped))
    return results
