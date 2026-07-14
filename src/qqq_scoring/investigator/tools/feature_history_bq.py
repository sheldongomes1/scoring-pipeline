"""REAL BigQuery backend for `feature_history` — makes one tool production-real.

Drop-in for `feature_history_fake`: same signature, same `FeatureResult` return,
same `SOURCE` key (so the judge's reverify-map entry doesn't change on the
fake→real swap — the payoff of cleaning the fake's source to the logical table
name in ADR-9). Reads `qqq_finance.period_features`.

Period resolution is POSITIONAL, not calendar (see module note): `period_offset`
counts FILED periods in the ticker's own history, because real fiscal calendars are
irregular (AAPL files 3 10-Qs/yr on shifting dates; adding 3 months lands on a
quarter that doesn't exist for that company). "+1" = "the next filed 10-Q."

LATENCY (reverify batching #1): the SQL already pulls the ticker's WHOLE ordered
history — resolution is pure Python on those rows. So `feature_history_bq_batch`
runs that query ONCE and resolves many (report_date, offset) probes off the single
row set (the judge's grounding gate replays several offsets per ticker). A
module-level `bigquery.Client` is reused across calls — the ~1.3s per-call client
construction was being thrown away on every reverify. Both are pure optimizations:
`_resolve` is byte-identical to the per-item path, so statuses and values are
unchanged (ADR-13 request-replay + ADR-14 positional resolution preserved exactly).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

from .contracts import FeatureResult, FeatureStatus, Provenance

SOURCE = "qqq_finance.period_features"                 # logical source == the fake's key (ADR-9)
_TABLE = "qqq-anomaly-lab.qqq_finance.period_features"  # fully-qualified for cross-project read

# Module-level shared client (latency #3): constructing bigquery.Client() is ~1.3s of
# auth/discovery; the reverify gate calls the backend dozens of times per investigation,
# so a fresh client per call was pure waste. Built lazily+once, thread-safe, and only
# when no client is injected (tests still pass their _FakeBQ untouched).
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _shared_client():
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                from google.cloud import bigquery  # lazy import so the module loads without creds

                _CLIENT = bigquery.Client()
    return _CLIENT


def _quote_ident(name: str) -> str:
    """Guard the feature name reaching the SQL SELECT (defense in depth — the model
    only ever picks from the enum, but the backend shouldn't trust that)."""
    if not name.replace("_", "").isalnum():
        raise ValueError(f"illegal feature identifier: {name!r}")
    return name


def _query_history(ticker: str, form: str, features: list[str], client) -> list:
    """Run the ONE query that pulls the ticker's whole ordered filing history for the
    requested columns. Kept separate from resolution so a batch can fetch once and
    resolve many probes off the result (latency #1)."""
    from google.cloud import bigquery  # lazy import so the module loads without creds

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
    return list(job)


def _resolve(
    rows: list,
    ticker: str,
    form: str,
    report_date: date,
    period_offset: int,
    features: list[str],
    retrieved_at: datetime,
) -> list[FeatureResult]:
    """Pure positional resolution over already-fetched rows — the risky calendar
    logic (ADR-12/13/14). Byte-identical to the old inline body, just factored out so
    the batch path can reuse it against a single fetched row set."""
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
    client = client or _shared_client()
    rows = _query_history(ticker, form, features, client)
    return _resolve(rows, ticker, form, report_date, period_offset, features, datetime.now(timezone.utc))


def feature_history_bq_batch(
    ticker: str,
    probes: list[tuple[date, int, list[str]]],
    *,
    client=None,
    form: str = "10-Q",
) -> dict[tuple[date, int], list[FeatureResult]]:
    """Reverify batch (latency #1): resolve MANY (report_date, offset, features) probes
    from ONE query. The query pulls the ticker's whole history for the UNION of the
    probes' features, then each probe is resolved off that single row set — so N
    reverify offsets for one ticker cost one round-trip, not N. Each per-probe result
    is byte-identical to calling `feature_history_bq` for that probe alone (resolution
    reads only the columns it asked for; the extra union columns are inert)."""
    client = client or _shared_client()
    probes = list(probes)
    all_features = sorted({f for _, _, feats in probes for f in feats})
    rows = _query_history(ticker, form, all_features, client)
    retrieved_at = datetime.now(timezone.utc)
    return {
        (rd, off): _resolve(rows, ticker, form, rd, off, list(feats), retrieved_at)
        for (rd, off, feats) in probes
    }


# Discovery hook: the judge's grounding gate checks for a `.batch` on the reverify
# backend and, when present, folds all of that source's probes into one query. Plain
# per-item callables / fakes / injected lambdas have no `.batch`, so they transparently
# fall back to the per-offset path — no wiring change at any call site.
feature_history_bq.batch = feature_history_bq_batch
