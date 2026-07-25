#!/usr/bin/env python3
"""Parallel backfill — generates explanations for all tiered filings using
concurrent API workers with incremental BQ uploads.

Usage:
    python explanations/backfill_parallel.py                # 5 workers (default)
    python explanations/backfill_parallel.py --workers 8    # 8 workers
    python explanations/backfill_parallel.py --dry-run      # preview without API calls
"""

import argparse
import io
import json
import os
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, os.path.dirname(__file__))
from prompt_template import build_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT       = "qqq-anomaly-lab"
BQ_DATASET       = "qqq_finance"
SCORES_TABLE     = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
CONVICTION_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.conviction_scores"
OUTPUT_TABLE     = f"{BQ_PROJECT}.{BQ_DATASET}.anomaly_explanations"

MODEL            = "claude-sonnet-4-6"
MAX_TOKENS       = 1024
TEMPERATURE      = 0.3
BATCH_SIZE       = 50   # upload to BQ every N completions

# ── Thread-safe counters ─────────────────────────────────────────────────────

lock = threading.Lock()
completed = 0
failed = 0
results_buffer: list[dict] = []


# ── BQ helpers ───────────────────────────────────────────────────────────────

def load_tiered_scores(client: bigquery.Client) -> pd.DataFrame:
    query = f"""
    SELECT qsd.*, cs.conviction_tier, cs.conviction_score
    FROM `{SCORES_TABLE}` qsd
    INNER JOIN `{CONVICTION_TABLE}` cs
      ON qsd.ticker = cs.ticker
      AND qsd.calendar_quarter = cs.calendar_quarter
    WHERE cs.conviction_tier IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM `{OUTPUT_TABLE}` ae
        WHERE ae.ticker = qsd.ticker
          AND ae.calendar_quarter = qsd.calendar_quarter
      )
    ORDER BY cs.conviction_score DESC
    """
    print("Loading tiered filings missing explanations...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings to process")
    return df


def flush_to_bq(client: bigquery.Client, batch: list[dict]) -> None:
    """Append a batch of results to BQ."""
    if not batch:
        return
    df = pd.DataFrame(batch)
    df["report_date"] = pd.to_datetime(df["report_date"], utc=True).dt.date

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="report_date",
        ),
        clustering_fields=["ticker"],
    )
    job = client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    print(f"  [UPLOAD] {len(batch)} explanations → BQ", flush=True)


# ── Claude API call ──────────────────────────────────────────────────────────

def call_claude(prompt: str) -> dict | None:
    try:
        import anthropic
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        return json.loads(text)
    except json.JSONDecodeError as e:
        print(f"    JSON parse error: {e}", flush=True)
        return None
    except Exception as e:
        print(f"    API error: {e}", flush=True)
        return None


# ── Worker function ──────────────────────────────────────────────────────────

def process_filing(row: pd.Series, idx: int, total: int, generated_at: str) -> dict | None:
    global completed, failed

    ticker  = row["ticker"]
    quarter = row["calendar_quarter"]
    tier    = row.get("conviction_tier", "—")
    score   = row.get("conviction_score", "—")

    print(f"[{idx}/{total}] {ticker} {quarter}  ({tier}, conviction={score})", flush=True)

    prompt = build_prompt(row)
    response = call_claude(prompt)

    if response is None:
        with lock:
            failed += 1
        print(f"  SKIP {ticker} {quarter} — API call failed", flush=True)
        return None

    with lock:
        completed += 1

    print(f"  OK   {ticker} {quarter} — {response.get('pattern_name', '?')}  [{completed}/{total}]", flush=True)

    return {
        "ticker":                  ticker,
        "report_date":             str(row.get("report_date", "")),
        "calendar_quarter":        quarter,
        "gics_sector":             row.get("gics_sector", ""),
        "anomaly_score_0_100":     float(row.get("anomaly_score_0_100", 0)),
        "beneish_m_score":         float(row["beneish_m_score"]) if pd.notna(row.get("beneish_m_score")) else None,
        "beneish_manipulation_flag": bool(row["beneish_manipulation_flag"]) if pd.notna(row.get("beneish_manipulation_flag")) else False,
        "pattern_name":            response.get("pattern_name", ""),
        "pattern_confidence":      response.get("pattern_confidence", ""),
        "pattern_summary":         response.get("pattern_summary", ""),
        "explanation_brief":       response.get("explanation_brief", ""),
        "model_used":              MODEL,
        "scoring_version":         row.get("scoring_version", ""),
        "generated_at":            generated_at,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parallel explanation backfill")
    parser.add_argument("--workers",  type=int, default=5, help="Number of parallel API workers (default: 5)")
    parser.add_argument("--dry-run",  action="store_true", help="Load filings but don't call API")
    args = parser.parse_args()

    bq_client = bigquery.Client(project=BQ_PROJECT)
    df = load_tiered_scores(bq_client)

    if df.empty:
        print("Nothing to do — all tiered filings already have explanations.")
        return

    total = len(df)
    generated_at = datetime.now(timezone.utc).isoformat()

    if args.dry_run:
        print(f"\n[DRY RUN] Would process {total} filings with {args.workers} workers")
        for tier in ["ALERT", "FLAG", "WATCH"]:
            count = len(df[df.conviction_tier == tier])
            if count:
                print(f"  {tier}: {count}")
        return

    print(f"\nStarting {args.workers} workers for {total} filings")
    print(f"Uploading to BQ every {BATCH_SIZE} completions\n")

    t_start = time.time()
    pending_results: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i, (_, row) in enumerate(df.iterrows(), 1):
            future = executor.submit(process_filing, row, i, total, generated_at)
            futures[future] = i

        for future in as_completed(futures):
            result = future.result()
            if result:
                pending_results.append(result)

                # Flush to BQ every BATCH_SIZE
                if len(pending_results) >= BATCH_SIZE:
                    flush_to_bq(bq_client, pending_results)
                    pending_results.clear()

    # Final flush
    if pending_results:
        flush_to_bq(bq_client, pending_results)

    elapsed = time.time() - t_start
    print(f"\nDone in {elapsed/60:.1f} minutes")
    print(f"  Completed: {completed}")
    print(f"  Failed:    {failed}")
    print(f"  Rate:      {completed/elapsed*60:.1f} filings/min")


if __name__ == "__main__":
    main()
