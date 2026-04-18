#!/usr/bin/env python3
"""Parallel backfill — generates analyst action briefs for tiered filings
that are missing from the analyst_actions table.

Reads from filing_intelligence, finds rows with a conviction_tier that have
no matching entry in analyst_actions, and processes them with concurrent
Claude API workers.  Results are incrementally appended to BQ in batches.

Usage:
    python explanations/backfill_analyst_actions.py                # 5 workers (default)
    python explanations/backfill_analyst_actions.py --workers 8    # 8 workers
    python explanations/backfill_analyst_actions.py --dry-run      # preview without API calls
"""

import argparse
import io
import json
import os
import re
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, os.path.dirname(__file__))
from analyst_actions_prompt import SYSTEM_PROMPT, build_analyst_actions_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT       = "qqq-anomaly-lab"
BQ_DATASET       = "qqq_finance"
SOURCE_VIEW      = f"{BQ_PROJECT}.{BQ_DATASET}.filing_intelligence"
OUTPUT_TABLE     = f"{BQ_PROJECT}.{BQ_DATASET}.analyst_actions"
REJECTED_TABLE   = f"{BQ_PROJECT}.{BQ_DATASET}.analyst_actions_rejected"

MODEL            = "claude-sonnet-4-6"
MAX_TOKENS       = 1024
TEMPERATURE      = 0.0
BATCH_SIZE       = 25   # upload to BQ every N completions

# Tiers to process
TARGET_TIERS     = ("ALERT", "FLAG", "WATCH")

# Valid urgency tier values
VALID_URGENCY    = {"CRITICAL", "INVESTIGATE", "CONTEXTUAL"}

# Required non-empty string fields in the response
REQUIRED_FIELDS  = ["investigation_path", "key_question", "persistence_test",
                    "priority_section", "urgency_tier"]

# Banned phrases that indicate hallucination or policy violation
BANNED_PATTERNS  = [
    r"\bfraud\b",
    r"\bbuying?\b",
    r"\bselling?\b",
    r"\boverweight\b",
    r"\bunderweight\b",
]

# ── Thread-safe counters ─────────────────────────────────────────────────────

lock = threading.Lock()
completed = 0
failed = 0


# ── BQ helpers ───────────────────────────────────────────────────────────────

def load_missing_filings(client: bigquery.Client) -> pd.DataFrame:
    """Find tiered filings in filing_intelligence that have no analyst_actions."""
    tiers_str = ", ".join(f"'{t}'" for t in TARGET_TIERS)
    query = f"""
    SELECT fi.*
    FROM `{SOURCE_VIEW}` fi
    LEFT JOIN `{OUTPUT_TABLE}` aa
      ON fi.ticker = aa.ticker
      AND fi.calendar_quarter = aa.calendar_quarter
    WHERE fi.conviction_tier IN ({tiers_str})
      AND aa.ticker IS NULL
    ORDER BY fi.conviction_score DESC
    """
    print("Loading tiered filings missing analyst_actions...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings to process")
    if len(df):
        print(f"  Breakdown: {df['conviction_tier'].value_counts().to_dict()}")
    return df


def flush_to_bq(client: bigquery.Client, batch: list[dict],
                table_id: str, date_col: str = "report_date") -> None:
    """Append a batch of results to BQ."""
    if not batch:
        return
    df = pd.DataFrame(batch)
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], utc=True).dt.date

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field=date_col,
        ),
        clustering_fields=["ticker"],
    )
    job = client.load_table_from_file(buf, table_id, job_config=job_config)
    job.result()
    print(f"  [UPLOAD] {len(batch)} rows → {table_id}", flush=True)


# ── Claude API ───────────────────────────────────────────────────────────────

def call_claude(user_prompt: str) -> dict | None:
    """Call Claude with separate system + user message. Returns parsed JSON or None."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = message.content[0].text.strip()

        # Strip markdown fences if present
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


# ── Validation ───────────────────────────────────────────────────────────────

def validate_response(response: dict) -> tuple[bool, str]:
    """Validate a Claude response against spec rules.

    Returns (is_valid, rejection_reason). rejection_reason is empty if valid.
    """
    for field in REQUIRED_FIELDS:
        val = response.get(field)
        if not val or not isinstance(val, str) or not val.strip():
            return False, f"Missing or empty required field: {field}"

    if response.get("urgency_tier") not in VALID_URGENCY:
        return False, f"Invalid urgency_tier: {response.get('urgency_tier')!r}"

    # Scan all string fields for banned patterns
    full_text = " ".join(
        str(v) for v in response.values() if isinstance(v, str)
    ).lower()
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE):
            return False, f"Banned pattern found: {pattern!r}"

    return True, ""


# ── Worker function ──────────────────────────────────────────────────────────

def process_filing(row: pd.Series, idx: int, total: int,
                   generated_at: str) -> tuple[dict | None, dict | None]:
    """Process a single filing. Returns (result, rejected) — one will be None."""
    global completed, failed

    ticker  = row["ticker"]
    quarter = row["calendar_quarter"]
    tier    = row.get("conviction_tier", "?")
    score   = row.get("conviction_score", 0)

    print(f"[{idx}/{total}] {ticker} {quarter}  ({tier}, score={score:.1f})", flush=True)

    user_prompt = build_analyst_actions_prompt(row)
    response = call_claude(user_prompt)

    if response is None:
        with lock:
            failed += 1
        print(f"  SKIP {ticker} {quarter} -- API call failed", flush=True)
        return None, {
            "ticker":           ticker,
            "report_date":      str(row.get("report_date", "")),
            "calendar_quarter": quarter,
            "rejection_reason": "API call failed or JSON parse error",
            "raw_response":     "",
            "generated_at":     generated_at,
        }

    is_valid, reason = validate_response(response)

    if not is_valid:
        with lock:
            failed += 1
        print(f"  REJECTED {ticker} {quarter} -- {reason}", flush=True)
        return None, {
            "ticker":           ticker,
            "report_date":      str(row.get("report_date", "")),
            "calendar_quarter": quarter,
            "rejection_reason": reason,
            "raw_response":     json.dumps(response),
            "generated_at":     generated_at,
        }

    with lock:
        completed += 1

    print(f"  OK   {ticker} {quarter} -- urgency={response['urgency_tier']}  "
          f"[{completed}/{total}]", flush=True)

    return {
        "ticker":                   ticker,
        "report_date":              str(row.get("report_date", "")),
        "calendar_quarter":         quarter,
        "conviction_tier":          str(row.get("conviction_tier", "")),
        "conviction_score":         float(row.get("conviction_score", 0) or 0),
        "investigation_path":       response["investigation_path"],
        "key_question":             response["key_question"],
        "persistence_test":         response["persistence_test"],
        "priority_section":         response["priority_section"],
        "urgency_tier":             response["urgency_tier"],
        "filing_section_rationale": response.get("filing_section_rationale") or None,
        "model_version":            MODEL,
        "generated_at":             generated_at,
    }, None


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Parallel backfill for missing analyst_actions")
    parser.add_argument("--workers", type=int, default=5,
                        help="Number of parallel API workers (default: 5)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Load filings but don't call API")
    args = parser.parse_args()

    bq_client = bigquery.Client(project=BQ_PROJECT)
    df = load_missing_filings(bq_client)

    if df.empty:
        print("Nothing to do -- all tiered filings already have analyst_actions.")
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
    pending_results:  list[dict] = []
    pending_rejected: list[dict] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = {}
        for i, (_, row) in enumerate(df.iterrows(), 1):
            future = executor.submit(process_filing, row, i, total, generated_at)
            futures[future] = i

        for future in as_completed(futures):
            result, rejected = future.result()
            if result:
                pending_results.append(result)
            if rejected:
                pending_rejected.append(rejected)

            # Flush results to BQ every BATCH_SIZE
            if len(pending_results) >= BATCH_SIZE:
                flush_to_bq(bq_client, pending_results, OUTPUT_TABLE)
                pending_results.clear()

            # Flush rejected less frequently
            if len(pending_rejected) >= BATCH_SIZE:
                flush_to_bq(bq_client, pending_rejected, REJECTED_TABLE)
                pending_rejected.clear()

    # Final flush
    if pending_results:
        flush_to_bq(bq_client, pending_results, OUTPUT_TABLE)
    if pending_rejected:
        flush_to_bq(bq_client, pending_rejected, REJECTED_TABLE)

    elapsed = time.time() - t_start
    print(f"\nDone in {elapsed/60:.1f} minutes")
    print(f"  Completed: {completed}")
    print(f"  Failed:    {failed}")
    if elapsed > 0:
        print(f"  Rate:      {completed/elapsed*60:.1f} filings/min")

    # Print urgency distribution
    if pending_results or completed:
        print("\n  (Urgency distribution logged per-filing above)")


if __name__ == "__main__":
    main()
