"""BQ-backed `balance_sheet_items` — reads the ingested `balance_sheet_items` table
instead of hitting SEC EDGAR live (built by scripts/ingest_balance_sheet.py from the
same EDGAR companyfacts, so values are identical but a fast BQ read, not a slow HTTP
fetch — the fix for the eval's #1 latency finding).

Drop-in for `balance_sheet_edgar`: same signature, same `FeatureResult`, same ADR-9/
13/14 contract. Only SOURCE differs (`qqq_finance.balance_sheet_items`), so the
judge's reverify-map key changes with the golden source (the table, now, not EDGAR).
Mirrors `feature_history_bq` exactly (positional offset, periods_skipped, request-replay).

LATENCY (reverify batching #1): mirrors feature_history_bq — the query pulls the
ticker's whole ordered history, so `balance_sheet_items_batch` runs it ONCE and
resolves many (report_date, offset) probes off the single row set, and a module-level
`bigquery.Client` is reused instead of rebuilt per call. `_resolve` is byte-identical
to the per-item path (statuses/values/accession unchanged).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timezone

from .contracts import FeatureResult, FeatureStatus, Provenance

SOURCE = "qqq_finance.balance_sheet_items"
_TABLE = "qqq-anomaly-lab.qqq_finance.balance_sheet_items"

# Hard per-call deadline — see feature_history_bq.BQ_DEADLINE_SECONDS (2026-07-16
# FTNT lesson: one hung call is unbounded by the loop's between-turns guard).
BQ_DEADLINE_SECONDS = 60.0

# Module-level shared client (latency #3) — see feature_history_bq for rationale.
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
    if not name.replace("_", "").isalnum():
        raise ValueError(f"illegal balance-sheet item identifier: {name!r}")
    return name


def _query_history(ticker: str, form: str, items: list[str], client) -> list:
    """One query pulling the ticker's whole ordered filing history for the requested
    line items (kept separate from resolution so a batch can fetch once, resolve many)."""
    from google.cloud import bigquery  # lazy import so the module loads without creds

    cols = ", ".join(_quote_ident(i) for i in items)
    sql = (
        f"SELECT target_period_end, target_form, accession_number, {cols} FROM `{_TABLE}` "
        f"WHERE ticker=@ticker AND target_form IN (@form, '10-K') "
        f"ORDER BY target_period_end"
    )
    job = client.query(
        sql,
        job_config=bigquery.QueryJobConfig(query_parameters=[
            bigquery.ScalarQueryParameter("ticker", "STRING", ticker),
            bigquery.ScalarQueryParameter("form", "STRING", form),
        ]),
        timeout=BQ_DEADLINE_SECONDS,   # bound the query REQUEST (hung-socket guard)
    )
    # Bound the wait for results too — `list(job)` alone waits forever. Injected
    # test fakes return a plain list (no .result), so fall through for those.
    if hasattr(job, "result"):
        job = job.result(timeout=BQ_DEADLINE_SECONDS)
    return list(job)


def _resolve(
    rows: list,
    ticker: str,
    form: str,
    report_date: date,
    period_offset: int,
    items: list[str],
    retrieved_at: datetime,
) -> list[FeatureResult]:
    """Pure positional resolution over already-fetched rows (ADR-14 skip, ADR-13
    request-replay). Byte-identical to the old inline body, factored out for the batch."""
    # Positional resolution over the QUARTERLY axis only (ADR-14); count skipped 10-Ks.
    q_rows = [r for r in rows if r["target_form"] == form]
    annual_dates = [r["target_period_end"] for r in rows if r["target_form"] == "10-K"]
    periods = [r["target_period_end"] for r in q_rows]
    resolved: date | None = None
    if report_date in periods:
        idx = periods.index(report_date) + period_offset
        if 0 <= idx < len(periods):
            resolved = periods[idx]
    periods_skipped = 0
    if resolved is not None:
        lo, hi = sorted((report_date, resolved))
        periods_skipped = sum(1 for d in annual_dates if lo < d < hi)
    row = q_rows[periods.index(resolved)] if resolved in periods else None

    results: list[FeatureResult] = []
    for item in items:
        prov = Provenance(
            source=SOURCE,
            ticker=ticker,
            resolved_report_date=resolved or report_date,
            requested_report_date=report_date,   # ADR-13 request replay
            requested_offset=period_offset,
            query=(
                f"SELECT {item} FROM {SOURCE} WHERE ticker='{ticker}' "
                f"AND target_form='{form}' AND target_period_end='{resolved}'"
            ),
            retrieved_at=retrieved_at,
            accession_number=(row["accession_number"] if row is not None else None),
        )
        if row is None:
            results.append(FeatureResult(item, FeatureStatus.PERIOD_NOT_FILED, None, prov))
            continue
        value = row[item]
        if value is None:
            results.append(FeatureResult(item, FeatureStatus.FEATURE_MISSING, None, prov, periods_skipped=periods_skipped))
        else:
            results.append(FeatureResult(item, FeatureStatus.FOUND, float(value), prov, periods_skipped=periods_skipped))
    return results


def balance_sheet_items(
    ticker: str,
    report_date: date,
    period_offset: int,
    items: list[str],
    *,
    client=None,
    form: str = "10-Q",
) -> list[FeatureResult]:
    """Return one FeatureResult per line item at report_date + offset-th FILED period,
    read from the BQ `balance_sheet_items` table."""
    client = client or _shared_client()
    rows = _query_history(ticker, form, items, client)
    return _resolve(rows, ticker, form, report_date, period_offset, items, datetime.now(timezone.utc))


def balance_sheet_items_batch(
    ticker: str,
    probes: list[tuple[date, int, list[str]]],
    *,
    client=None,
    form: str = "10-Q",
) -> dict[tuple[date, int], list[FeatureResult]]:
    """Reverify batch (latency #1): resolve MANY (report_date, offset, items) probes
    from ONE query — see feature_history_bq_batch. Each per-probe result is
    byte-identical to a standalone `balance_sheet_items` call for that probe."""
    client = client or _shared_client()
    probes = list(probes)
    all_items = sorted({i for _, _, its in probes for i in its})
    rows = _query_history(ticker, form, all_items, client)
    retrieved_at = datetime.now(timezone.utc)
    return {
        (rd, off): _resolve(rows, ticker, form, rd, off, list(its), retrieved_at)
        for (rd, off, its) in probes
    }


# Discovery hook for the judge's grounding gate (see feature_history_bq).
balance_sheet_items.batch = balance_sheet_items_batch
