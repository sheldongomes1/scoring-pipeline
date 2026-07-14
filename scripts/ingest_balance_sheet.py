"""Ingest SEC EDGAR balance-sheet line items → BQ `qqq_finance.balance_sheet_items`.

The persistence layer for tool #3. Fetches every universe ticker's companyfacts ONCE
(concurrently), extracts all filed 10-Q/10-K periods' line items via
`balance_sheet_edgar.extract_all` (same concept logic as the live tool), and loads a
table the investigator's `balance_sheet_bq` backend reads instead of hitting EDGAR
live — the fix for the eval's #1 latency finding (BQ read ≪ live HTTP fetch + parse).

Universe = distinct tickers in `period_features` (the same axis the scorer uses).

Registered as a step in scripts/orchestrate.py (independent EDGAR ingestion).

Usage:
    python scripts/ingest_balance_sheet.py                 # full refresh, all tickers
    python scripts/ingest_balance_sheet.py --ticker WBD    # one ticker
    python scripts/ingest_balance_sheet.py --limit 5       # cheap test
"""
from __future__ import annotations

import argparse
import io
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qqq_scoring.investigator.tools.balance_sheet import ITEM_KEYS  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_edgar import extract_all  # noqa: E402

BQ_PROJECT = "qqq-anomaly-lab"
OUTPUT_TABLE = f"{BQ_PROJECT}.qqq_finance.balance_sheet_items"
CONCURRENCY = 6   # EDGAR is I/O-bound; the tool's own rate-limiter keeps us <10 req/s


def universe_tickers(client: bigquery.Client, ticker: str | None, limit: int | None) -> list[str]:
    if ticker:
        return [ticker.upper()]
    lim = f"LIMIT {int(limit)}" if limit else ""
    q = f"SELECT DISTINCT ticker FROM `{BQ_PROJECT}.qqq_finance.period_features` ORDER BY ticker {lim}"
    return [r["ticker"] for r in client.query(q)]


def _upload(client: bigquery.Client, rows: list[dict], truncate: bool) -> None:
    if not rows:
        print("  No balance-sheet rows to upload.")
        return
    df = pd.DataFrame(rows)
    df["target_period_end"] = pd.to_datetime(df["target_period_end"], utc=True).dt.date
    df["ingested_at"] = datetime.now(timezone.utc)
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=(bigquery.WriteDisposition.WRITE_TRUNCATE if truncate
                           else bigquery.WriteDisposition.WRITE_APPEND),
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field="target_period_end"),
        clustering_fields=["ticker"],
    )
    client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config).result()
    print(f"  Uploaded {len(df)} period rows → {OUTPUT_TABLE}  [{'TRUNCATE' if truncate else 'APPEND'}]")


def main() -> None:
    ap = argparse.ArgumentParser(description="Ingest EDGAR balance-sheet line items to BQ")
    ap.add_argument("--ticker", help="Only this ticker")
    ap.add_argument("--limit", type=int, help="Cap tickers (cheap test)")
    ap.add_argument("--concurrency", type=int, default=CONCURRENCY)
    args = ap.parse_args()

    bq = bigquery.Client(project=BQ_PROJECT)
    tickers = universe_tickers(bq, args.ticker, args.limit)
    print(f"Ingesting balance-sheet history for {len(tickers)} tickers (concurrency={args.concurrency})…")

    all_rows: list[dict] = []
    ok = fail = 0
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {ex.submit(extract_all, t, ITEM_KEYS): t for t in tickers}
        for i, fut in enumerate(as_completed(futures), 1):
            t = futures[fut]
            try:
                rows = fut.result()
                all_rows.extend(rows)
                ok += 1
                if not rows:
                    print(f"  WARN {t}: no periods (ticker not in SEC map?)")
            except Exception as e:
                fail += 1
                print(f"  ERROR {t}: {type(e).__name__}: {e}")
            if i % 20 == 0:
                print(f"  … {i}/{len(tickers)} tickers  (rows so far: {len(all_rows)})")

    # Single truncate-load (full rebuild) — the table is small and this is idempotent.
    _upload(bq, all_rows, truncate=(not args.ticker))
    print(f"Done. tickers ok={ok} fail={fail}, {len(all_rows)} period rows.")


if __name__ == "__main__":
    main()
