#!/usr/bin/env python3
"""Generate LLM analyst briefs for anomalous filings.

Reads scored filings from BigQuery (qqq_finance.quarterly_scores_detailed),
computes alert scores, calls Claude API for qualifying filings, and writes
structured explanations back to BigQuery (qqq_finance.anomaly_explanations).

Usage:
    # Batch mode — explain all filings with alert_score >= 5
    python explanations/generate_explanations.py --min-alert-score 5

    # Single filing
    python explanations/generate_explanations.py --ticker EA --quarter 2024-Q3

    # Dry run — print prompts without calling Claude
    python explanations/generate_explanations.py --min-alert-score 5 --dry-run
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.beneish import MANIPULATION_THRESHOLD

sys.path.insert(0, os.path.dirname(__file__))
from prompt_template import build_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT     = "qqq-anomaly-lab"
BQ_DATASET     = "qqq_finance"
SCORES_TABLE   = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
OUTPUT_TABLE   = f"{BQ_PROJECT}.{BQ_DATASET}.anomaly_explanations"

MODEL          = "claude-sonnet-4-6"
MAX_TOKENS     = 1024
TEMPERATURE    = 0.3
API_DELAY_SEC  = 1.0   # delay between API calls

# Alert score thresholds (per plan)
Z_FLAG_THRESHOLD   = 2.0   # |combined_z| > this counts as a flag
MAHAL_FLAG_SCORE   = 80.0  # anomaly_score_0_100 >= this triggers +3
Z_FLAG_WEIGHT      = 1
MAHAL_WEIGHT       = 3
BENEISH_WEIGHT     = 5

# ── Alert score computation ───────────────────────────────────────────────────

def compute_alert_score(row: pd.Series) -> int:
    """alert_score = z_flag_count×1 + mahal_flag×3 + beneish_flag×5."""
    z_cols = [c for c in row.index if c.startswith("combined_z__")]
    z_flag_count = sum(
        1 for c in z_cols
        if pd.notna(row[c]) and abs(float(row[c])) > Z_FLAG_THRESHOLD
    )
    mahal_flag  = int(float(row.get("anomaly_score_0_100", 0)) >= MAHAL_FLAG_SCORE)
    beneish_val  = row.get("beneish_manipulation_flag", False)
    beneish_flag = int(bool(beneish_val) if pd.notna(beneish_val) else False)
    return (z_flag_count * Z_FLAG_WEIGHT) + (mahal_flag * MAHAL_WEIGHT) + (beneish_flag * BENEISH_WEIGHT)


# ── BigQuery helpers ──────────────────────────────────────────────────────────

def load_scores(client: bigquery.Client, ticker: str | None, quarter: str | None) -> pd.DataFrame:
    """Query quarterly_scores_detailed from BigQuery."""
    query = f"SELECT * FROM `{SCORES_TABLE}`"
    conditions = []
    if ticker:
        conditions.append(f"ticker = '{ticker}'")
    if quarter:
        conditions.append(f"calendar_quarter = '{quarter}'")
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    print(f"Loading scores from BigQuery: {SCORES_TABLE}")
    df = client.query(query).to_dataframe()
    print(f"  Loaded {len(df)} rows")
    return df


def upload_explanations(client: bigquery.Client, results: list[dict]) -> None:
    """Write explanation results to BigQuery, replacing existing rows for same (ticker, report_date)."""
    if not results:
        print("No results to upload.")
        return

    df = pd.DataFrame(results)
    df["report_date"] = pd.to_datetime(df["report_date"], utc=True).dt.date

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    # Use WRITE_TRUNCATE to replace the full table on each batch run.
    # For incremental runs (single ticker), use WRITE_APPEND + dedup in BQ.
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="report_date",
        ),
        clustering_fields=["ticker"],
    )

    job = client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    print(f"Uploaded {len(df)} explanations → {OUTPUT_TABLE}")


# ── Claude API call ───────────────────────────────────────────────────────────

def call_claude(prompt: str) -> dict | None:
    """Call Claude API and parse the JSON response. Returns None on error."""
    try:
        import anthropic
        client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            messages=[{"role": "user", "content": prompt}],
        )
        text = message.content[0].text.strip()

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        return json.loads(text)

    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"  API error: {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate analyst briefs for anomalous filings")
    parser.add_argument("--ticker",           default=None, help="Single ticker (e.g. EA)")
    parser.add_argument("--quarter",          default=None, help="Single quarter (e.g. 2024-Q3)")
    parser.add_argument("--min-alert-score",  type=int, default=5, help="Minimum alert score to explain (default: 5)")
    parser.add_argument("--dry-run",          action="store_true", help="Print prompts without calling Claude")
    args = parser.parse_args()

    bq_client = bigquery.Client(project=BQ_PROJECT)

    # Load scored filings
    df = load_scores(bq_client, args.ticker, args.quarter)
    if df.empty:
        print("No filings found. Check --ticker / --quarter arguments.")
        return

    # Compute alert scores
    df["alert_score"] = df.apply(compute_alert_score, axis=1)

    # Filter
    if args.ticker and args.quarter:
        qualifying = df  # single filing mode — no score filter
    else:
        qualifying = df[df["alert_score"] >= args.min_alert_score].copy()

    qualifying = qualifying.sort_values("alert_score", ascending=False).reset_index(drop=True)
    print(f"\n{len(qualifying)} filings qualify (alert_score >= {args.min_alert_score})")

    if qualifying.empty:
        print("Nothing to explain.")
        return

    # Generate explanations
    results = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for i, (_, row) in enumerate(qualifying.iterrows(), 1):
        ticker  = row["ticker"]
        quarter = row["calendar_quarter"]
        alert   = int(row["alert_score"])
        print(f"\n[{i}/{len(qualifying)}] {ticker} {quarter}  (alert_score={alert})")

        prompt = build_prompt(row)

        if args.dry_run:
            print("--- PROMPT ---")
            print(prompt[:800] + "..." if len(prompt) > 800 else prompt)
            continue

        response = call_claude(prompt)
        if response is None:
            print(f"  Skipping {ticker} {quarter} — API call failed")
            continue

        results.append({
            "ticker":                  ticker,
            "report_date":             str(row.get("report_date", "")),
            "calendar_quarter":        quarter,
            "gics_sector":             row.get("gics_sector", ""),
            "anomaly_score_0_100":     float(row.get("anomaly_score_0_100", 0)),
            "beneish_m_score":         float(row["beneish_m_score"]) if pd.notna(row.get("beneish_m_score")) else None,
            "beneish_manipulation_flag": bool(row.get("beneish_manipulation_flag", False)),
            "alert_score":             alert,
            "pattern_name":            response.get("pattern_name", ""),
            "pattern_confidence":      response.get("pattern_confidence", ""),
            "pattern_summary":         response.get("pattern_summary", ""),
            "explanation_brief":       response.get("explanation_brief", ""),
            "model_used":              MODEL,
            "scoring_version":         row.get("scoring_version", ""),
            "generated_at":            generated_at,
        })
        print(f"  Pattern: {response.get('pattern_name')} ({response.get('pattern_confidence')})")
        print(f"  Summary: {response.get('pattern_summary', '')[:120]}")

        time.sleep(API_DELAY_SEC)

    # Upload to BigQuery
    if not args.dry_run and results:
        print(f"\nUploading {len(results)} explanations to BigQuery...")
        upload_explanations(bq_client, results)

    print("\nDone.")


if __name__ == "__main__":
    main()
