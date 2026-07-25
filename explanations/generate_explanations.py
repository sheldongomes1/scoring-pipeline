#!/usr/bin/env python3
"""Generate LLM analyst briefs for anomalous filings.

Reads scored filings from BigQuery (qqq_finance.quarterly_scores_detailed),
filters to those with a conviction tier (ALERT / FLAG / WATCH), calls Claude
API for each, and writes structured explanations back to BigQuery
(qqq_finance.anomaly_explanations).

Usage:
    # Batch mode — all tiered filings (skips those already explained)
    python explanations/generate_explanations.py

    # Force regeneration of all tiered filings (ignores existing)
    python explanations/generate_explanations.py --force

    # Single filing (always generates, regardless of tier or existing)
    python explanations/generate_explanations.py --ticker EA --quarter 2024-Q3

    # Dry run — print prompts without calling Claude
    python explanations/generate_explanations.py --dry-run
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

sys.path.insert(0, os.path.dirname(__file__))
from prompt_template import build_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT     = "qqq-anomaly-lab"
BQ_DATASET     = "qqq_finance"
SCORES_TABLE   = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
CONVICTION_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.conviction_scores"
OUTPUT_TABLE   = f"{BQ_PROJECT}.{BQ_DATASET}.anomaly_explanations"

MODEL          = "claude-sonnet-4-6"
MAX_TOKENS     = 1024
TEMPERATURE    = 0.3
API_DELAY_SEC  = 1.0   # delay between API calls


# ── BigQuery helpers ──────────────────────────────────────────────────────────

def load_tiered_scores(
    client: bigquery.Client,
    ticker: str | None,
    quarter: str | None,
    skip_existing: bool = True,
) -> pd.DataFrame:
    """Load scored filings that have a conviction tier (ALERT/FLAG/WATCH).

    Joins quarterly_scores_detailed with conviction_scores to filter only
    tiered filings. When skip_existing=True, excludes filings that already
    have an explanation in anomaly_explanations.
    """
    # Single-filing mode: load just that row from scores (no tier filter)
    if ticker and quarter:
        query = f"""
        SELECT qsd.*
        FROM `{SCORES_TABLE}` qsd
        WHERE qsd.ticker = '{ticker}'
          AND qsd.calendar_quarter = '{quarter}'
        """
        print(f"Loading single filing: {ticker} {quarter}")
        df = client.query(query).to_dataframe()
        print(f"  Loaded {len(df)} rows")
        return df

    # Batch mode: all tiered filings
    query = f"""
    SELECT qsd.*, cs.conviction_tier, cs.conviction_score
    FROM `{SCORES_TABLE}` qsd
    INNER JOIN `{CONVICTION_TABLE}` cs
      ON qsd.ticker = cs.ticker
      AND qsd.calendar_quarter = cs.calendar_quarter
    WHERE cs.conviction_tier IS NOT NULL
    """
    if ticker:
        query += f"  AND qsd.ticker = '{ticker}'\n"

    if skip_existing:
        query += f"""  AND NOT EXISTS (
        SELECT 1 FROM `{OUTPUT_TABLE}` ae
        WHERE ae.ticker = qsd.ticker
          AND ae.calendar_quarter = qsd.calendar_quarter
      )
    """

    query += "ORDER BY cs.conviction_score DESC"

    print(f"Loading tiered filings from BigQuery")
    print(f"  skip_existing={skip_existing}")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings to process")
    return df


def upload_explanations(client: bigquery.Client, results: list[dict], force: bool = False) -> None:
    """Write explanation results to BigQuery.

    force=True:  WRITE_TRUNCATE — replaces entire table (full regeneration).
    force=False: WRITE_APPEND  — adds new rows only (incremental, skip_existing
                 already filtered them so no duplicates).
    """
    if not results:
        print("No results to upload.")
        return

    df = pd.DataFrame(results)
    df["report_date"] = pd.to_datetime(df["report_date"], utc=True).dt.date

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    disposition = (
        bigquery.WriteDisposition.WRITE_TRUNCATE if force
        else bigquery.WriteDisposition.WRITE_APPEND
    )

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=disposition,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="report_date",
        ),
        clustering_fields=["ticker"],
    )

    job = client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    mode = "TRUNCATE" if force else "APPEND"
    print(f"Uploaded {len(df)} explanations → {OUTPUT_TABLE} ({mode})")


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
    parser = argparse.ArgumentParser(description="Generate analyst briefs for all tiered filings")
    parser.add_argument("--ticker",   default=None, help="Single ticker (e.g. EA)")
    parser.add_argument("--quarter",  default=None, help="Single quarter (e.g. 2024-Q3)")
    parser.add_argument("--force",    action="store_true",
                        help="Regenerate all tiered filings (ignore existing explanations, TRUNCATE table)")
    parser.add_argument("--dry-run",  action="store_true", help="Print prompts without calling Claude")
    args = parser.parse_args()

    bq_client = bigquery.Client(project=BQ_PROJECT)

    # Load tiered filings — skip existing unless --force
    is_single = args.ticker and args.quarter
    df = load_tiered_scores(
        bq_client,
        args.ticker,
        args.quarter,
        skip_existing=not args.force and not is_single,
    )
    if df.empty:
        print("Nothing to explain — all tiered filings already have explanations.")
        return

    print(f"\n{len(df)} filings to process")

    # Generate explanations
    results = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for i, (_, row) in enumerate(df.iterrows(), 1):
        ticker  = row["ticker"]
        quarter = row["calendar_quarter"]
        tier    = row.get("conviction_tier", "—")
        score   = row.get("conviction_score", "—")
        print(f"\n[{i}/{len(df)}] {ticker} {quarter}  ({tier}, conviction={score})")

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
            "beneish_manipulation_flag": bool(row["beneish_manipulation_flag"]) if pd.notna(row.get("beneish_manipulation_flag")) else False,
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
        upload_explanations(bq_client, results, force=args.force)

    print("\nDone.")


if __name__ == "__main__":
    main()
