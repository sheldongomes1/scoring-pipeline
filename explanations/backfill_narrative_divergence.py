#!/usr/bin/env python3
"""Parallel backfill — scores narrative divergence for all filings missing entries.

Usage:
    python explanations/backfill_narrative_divergence.py                # 5 workers
    python explanations/backfill_narrative_divergence.py --workers 8
    python explanations/backfill_narrative_divergence.py --dry-run
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
from google.cloud import bigquery, storage

sys.path.insert(0, os.path.dirname(__file__))
from divergence_prompt import build_divergence_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT   = "qqq-anomaly-lab"
BQ_DATASET   = "qqq_finance"
SCORES_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
OUTPUT_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.narrative_divergence"
GCS_BUCKET   = "qqq-anomaly-raw-sg"
GCS_PREFIX   = "qqq/narrative"

MODEL        = "claude-sonnet-4-6"
MAX_TOKENS   = 1024
TEMPERATURE  = 0.2
BATCH_SIZE   = 50

# ── Thread-safe counters ─────────────────────────────────────────────────────

lock = threading.Lock()
completed = 0
failed = 0
skipped_no_mda = 0


# ── BQ + GCS helpers ────────────────────────────────────────────────────────

def load_missing(client: bigquery.Client) -> pd.DataFrame:
    query = f"""
    SELECT *
    FROM `{SCORES_TABLE}` qsd
    WHERE NOT EXISTS (
      SELECT 1 FROM `{OUTPUT_TABLE}` nd
      WHERE nd.ticker = qsd.ticker
        AND nd.calendar_quarter = qsd.calendar_quarter
    )
    ORDER BY qsd.anomaly_score_0_100 DESC
    """
    print("Loading filings missing narrative divergence...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings to process")
    return df


def load_mda(gcs_client: storage.Client, ticker: str, report_date, form_type: str) -> str | None:
    dt = pd.to_datetime(report_date)
    form_clean = form_type.replace("-", "")
    blob_path = f"{GCS_PREFIX}/{ticker}/{ticker}_{dt.year}_{form_clean}_{dt.strftime('%Y-%m-%d')}_narrative.json"
    try:
        blob = gcs_client.bucket(GCS_BUCKET).blob(blob_path)
        data = json.loads(blob.download_as_text())
        mda = data.get("sections", {}).get("mda", "")
        return mda.strip() if mda and len(mda.strip()) >= 100 else None
    except Exception:
        return None


def flush_to_bq(client: bigquery.Client, batch: list[dict]) -> None:
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
            type_=bigquery.TimePartitioningType.DAY, field="report_date",
        ),
        clustering_fields=["ticker"],
    )
    client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config).result()
    print(f"  [UPLOAD] {len(batch)} divergence results → BQ", flush=True)


# ── Claude API call ──────────────────────────────────────────────────────────

def _repair_json(text: str) -> dict | None:
    """Attempt to fix common JSON issues from LLM output (unescaped quotes)."""
    import re
    patterns = {
        "divergence_label": r'"divergence_label"\s*:\s*"([^"]*)"',
        "confidence_score": r'"confidence_score"\s*:\s*([\d.]+)',
        "mda_tone": r'"mda_tone"\s*:\s*"([^"]*)"',
        "anomaly_acknowledged": r'"anomaly_acknowledged"\s*:\s*(true|false)',
    }
    result = {}
    for key, pat in patterns.items():
        m = re.search(pat, text)
        if m:
            val = m.group(1)
            if key == "confidence_score":
                val = float(val)
            elif key == "anomaly_acknowledged":
                val = val == "true"
            result[key] = val
    for key in ["cited_passage", "rationale"]:
        m = re.search(rf'"{key}"\s*:\s*"(.*?)",?\s*\n\s*"', text, re.DOTALL)
        if m:
            result[key] = m.group(1).replace('\n', ' ').strip()
    if "divergence_label" in result:
        return result
    return None


def call_claude(prompt: str) -> dict | None:
    try:
        import anthropic
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=MODEL, max_tokens=MAX_TOKENS, temperature=TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            repaired = _repair_json(text)
            if repaired:
                print(f"    JSON repaired via regex fallback", flush=True)
                return repaired
            print(f"    JSON parse error (repair also failed)", flush=True)
            return None
    except Exception as e:
        print(f"    API error: {e}", flush=True)
        return None


# ── Worker function ──────────────────────────────────────────────────────────

def process_filing(row: pd.Series, idx: int, total: int,
                   gcs_client: storage.Client, generated_at: str) -> dict | None:
    global completed, failed, skipped_no_mda

    ticker      = row["ticker"]
    quarter     = row["calendar_quarter"]
    report_date = row["report_date"]
    form_type   = row.get("form_type", "10-Q")
    score       = float(row.get("anomaly_score_0_100", 0))

    # Load MD&A from GCS
    mda_text = load_mda(gcs_client, ticker, report_date, form_type)
    if mda_text is None:
        with lock:
            skipped_no_mda += 1
        return None

    prompt = build_divergence_prompt(row, mda_text)
    response = call_claude(prompt)

    if response is None:
        with lock:
            failed += 1
        print(f"  FAIL {ticker} {quarter}", flush=True)
        return None

    with lock:
        completed += 1

    label = response.get("divergence_label", "NEUTRAL")
    conf = response.get("confidence_score", 0.0)
    print(f"  OK   {ticker} {quarter} — {label} ({conf:.2f})  [{completed}/{total}]", flush=True)

    return {
        "ticker":               ticker,
        "report_date":          str(report_date),
        "calendar_quarter":     quarter,
        "gics_sector":          row.get("gics_sector", ""),
        "anomaly_score_0_100":  score,
        "divergence_label":     label,
        "confidence_score":     float(conf),
        "cited_passage":        response.get("cited_passage", ""),
        "rationale":            response.get("rationale", ""),
        "mda_tone":             response.get("mda_tone", ""),
        "anomaly_acknowledged": bool(response.get("anomaly_acknowledged", False)),
        "model_used":           MODEL,
        "scoring_version":      row.get("scoring_version", ""),
        "generated_at":         generated_at,
    }


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Parallel narrative divergence backfill")
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    bq_client = bigquery.Client(project=BQ_PROJECT)
    gcs_client = storage.Client()
    df = load_missing(bq_client)

    if df.empty:
        print("Nothing to do — all filings have narrative divergence scores.")
        return

    total = len(df)

    if args.dry_run:
        print(f"\n[DRY RUN] Would process {total} filings with {args.workers} workers")
        return

    print(f"\nStarting {args.workers} workers for {total} filings")
    print(f"Uploading to BQ every {BATCH_SIZE} completions\n")

    t_start = time.time()
    generated_at = datetime.now(timezone.utc).isoformat()
    pending: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i, (_, row) in enumerate(df.iterrows(), 1):
            future = executor.submit(process_filing, row, i, total, gcs_client, generated_at)
            futures[future] = i

        for future in as_completed(futures):
            result = future.result()
            if result:
                pending.append(result)
                if len(pending) >= BATCH_SIZE:
                    flush_to_bq(bq_client, pending)
                    pending.clear()

    if pending:
        flush_to_bq(bq_client, pending)

    elapsed = time.time() - t_start
    print(f"\nDone in {elapsed/60:.1f} minutes")
    print(f"  Completed:    {completed}")
    print(f"  Failed:       {failed}")
    print(f"  No MD&A:      {skipped_no_mda}")
    print(f"  Rate:         {completed/elapsed*60:.1f} filings/min")


if __name__ == "__main__":
    main()
