#!/usr/bin/env python3
"""Score narrative-quantitative divergence for all scored filings.

For each filing in quarterly_scores_detailed, loads the MD&A section from GCS,
calls Claude to assess whether management's narrative contradicts, corroborates,
or is neutral toward the quantitative anomaly signals, and writes results to
BigQuery (qqq_finance.narrative_divergence).

This is Step 4 in the pipeline and runs BEFORE conviction scoring (Step 5).
Its output feeds into the conviction score's transparency pillar, so it must
process ALL scored filings — not a filtered subset.

By default, runs incrementally: skips filings that already have a row in the
narrative_divergence table. Use --force for full regeneration.

Usage:
    # Batch mode — all scored filings (incremental, skips existing)
    python explanations/score_narrative_divergence.py

    # Force full regeneration
    python explanations/score_narrative_divergence.py --force

    # Single filing
    python explanations/score_narrative_divergence.py --ticker WBD --quarter 2022-Q2

    # Dry run — print prompts without calling Claude
    python explanations/score_narrative_divergence.py --ticker WBD --quarter 2022-Q2 --dry-run
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery, storage

sys.path.insert(0, os.path.dirname(__file__))
from divergence_prompt import build_divergence_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT      = "qqq-anomaly-lab"
BQ_DATASET      = "qqq_finance"
SCORES_TABLE    = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
OUTPUT_TABLE    = f"{BQ_PROJECT}.{BQ_DATASET}.narrative_divergence"

GCS_BUCKET      = "qqq-anomaly-raw-sg"
GCS_PREFIX      = "qqq/narrative"

MODEL           = "claude-sonnet-4-6"
MAX_TOKENS      = 1024
TEMPERATURE     = 0.2   # lower than explanations — we want precise attribution
API_DELAY_SEC   = 1.0


# ── BigQuery loaders ─────────────────────────────────────────────────────────

def load_scores(
    client: bigquery.Client,
    ticker: str | None = None,
    quarter: str | None = None,
    skip_existing: bool = True,
) -> pd.DataFrame:
    """Load scored filings from quarterly_scores_detailed.

    Processes ALL scored filings (no alert_score filter) because this step
    runs before conviction scoring and its output is an input to conviction.

    When skip_existing=True, excludes filings that already have a row in the
    narrative_divergence table (incremental mode).
    """
    # Single-filing mode
    if ticker and quarter:
        query = f"""
        SELECT *
        FROM `{SCORES_TABLE}`
        WHERE ticker = '{ticker}'
          AND calendar_quarter = '{quarter}'
        """
        print(f"Loading single filing: {ticker} {quarter}")
        df = client.query(query).to_dataframe()
        print(f"  Loaded {len(df)} rows")
        return df

    # Batch mode: all scored filings
    query = f"""
    SELECT *
    FROM `{SCORES_TABLE}`
    """

    conditions = []
    if ticker:
        conditions.append(f"ticker = '{ticker}'")

    if skip_existing:
        conditions.append(f"""NOT EXISTS (
        SELECT 1 FROM `{OUTPUT_TABLE}` nd
        WHERE nd.ticker = `{SCORES_TABLE}`.ticker
          AND nd.calendar_quarter = `{SCORES_TABLE}`.calendar_quarter
      )""")

    if conditions:
        query += "WHERE " + "\n  AND ".join(conditions) + "\n"

    query += "ORDER BY anomaly_score_0_100 DESC"

    print(f"Loading scored filings from BigQuery")
    print(f"  skip_existing={skip_existing}")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings to process ({df['ticker'].nunique()} tickers)")
    return df


# ── GCS narrative loader ──────────────────────────────────────────────────────

def _narrative_blob_path(ticker: str, report_date, form_type: str) -> str:
    """Construct the GCS blob path for a narrative JSON file.

    Pattern: qqq/narrative/{TICKER}/{TICKER}_{YEAR}_{FORM}_{REPORT_DATE}_narrative.json
    e.g.     qqq/narrative/WBD/WBD_2022_10Q_2022-06-30_narrative.json
    """
    dt = pd.to_datetime(report_date)
    year = dt.year
    date_str = dt.strftime("%Y-%m-%d")
    form_clean = form_type.replace("-", "")  # 10-Q → 10Q, 10-K → 10K
    return f"{GCS_PREFIX}/{ticker}/{ticker}_{year}_{form_clean}_{date_str}_narrative.json"


def load_mda(gcs_client: storage.Client, ticker: str, report_date, form_type: str) -> str | None:
    """Load the MD&A section from GCS for a given filing. Returns None if not found."""
    blob_path = _narrative_blob_path(ticker, report_date, form_type)
    try:
        bucket = gcs_client.bucket(GCS_BUCKET)
        blob = bucket.blob(blob_path)
        content = blob.download_as_text()
        data = json.loads(content)
        mda = data.get("sections", {}).get("mda", "")
        if not mda or len(mda.strip()) < 100:
            print(f"  Warning: MD&A for {ticker} {report_date} is empty or too short — skipping")
            return None
        return mda.strip()
    except Exception as e:
        print(f"  Could not load narrative for {ticker} {report_date}: {e}")
        return None


# ── Claude API call ───────────────────────────────────────────────────────────

def _repair_json(text: str) -> dict | None:
    """Attempt to fix common JSON issues from LLM output (unescaped quotes in strings)."""
    import re
    # Try to extract each field value using regex, rebuilding the object
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

    # Extract cited_passage and rationale (may contain quotes)
    for key in ["cited_passage", "rationale"]:
        m = re.search(rf'"{key}"\s*:\s*"(.*?)",?\s*\n\s*"', text, re.DOTALL)
        if m:
            result[key] = m.group(1).replace('\n', ' ').strip()

    if "divergence_label" in result:
        return result
    return None


def call_claude(prompt: str) -> dict | None:
    """Call Claude API and parse the JSON response. Returns None on error."""
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

        # Strip markdown code fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Attempt regex-based repair for unescaped quotes
            repaired = _repair_json(text)
            if repaired:
                print(f"  JSON repaired via regex fallback")
                return repaired
            print(f"  JSON parse error (repair also failed)")
            return None

    except Exception as e:
        print(f"  API error: {e}")
        return None


# ── BigQuery upload ───────────────────────────────────────────────────────────

def upload_divergence(bq_client: bigquery.Client, results: list[dict], force: bool = False) -> None:
    """Write divergence results to BigQuery.

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

    job = bq_client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    mode = "TRUNCATE" if force else "APPEND"
    print(f"Uploaded {len(df)} divergence results → {OUTPUT_TABLE} ({mode})")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Score narrative-quantitative divergence")
    parser.add_argument("--ticker",   default=None, help="Single ticker (e.g. WBD)")
    parser.add_argument("--quarter",  default=None, help="Single quarter (e.g. 2022-Q2)")
    parser.add_argument("--force",    action="store_true",
                        help="Full regeneration: reprocess all filings and WRITE_TRUNCATE the output table")
    parser.add_argument("--dry-run",  action="store_true", help="Print prompts without calling Claude")
    args = parser.parse_args()

    bq_client  = bigquery.Client(project=BQ_PROJECT)
    gcs_client = storage.Client()

    # Load scored filings from BQ
    # --force skips the incremental filter (reprocesses everything)
    skip_existing = not args.force
    df = load_scores(bq_client, args.ticker, args.quarter, skip_existing=skip_existing)
    if df.empty:
        print("No filings to process.")
        return

    print(f"\n{len(df)} filings to process")

    results = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for i, (_, row) in enumerate(df.iterrows(), 1):
        ticker      = row["ticker"]
        quarter     = row["calendar_quarter"]
        report_date = row["report_date"]
        form_type   = row.get("form_type", "10-Q")
        score       = float(row.get("anomaly_score_0_100", 0))

        print(f"\n[{i}/{len(df)}] {ticker} {quarter}  (anomaly_score={score:.1f})")

        # Load MD&A from GCS
        mda_text = load_mda(gcs_client, ticker, report_date, form_type)
        if mda_text is None:
            print(f"  Skipping — no MD&A available")
            continue

        print(f"  MD&A loaded: {len(mda_text):,} chars")

        prompt = build_divergence_prompt(row, mda_text)

        if args.dry_run:
            print("--- PROMPT (truncated) ---")
            print(prompt[:1200] + "\n..." if len(prompt) > 1200 else prompt)
            continue

        response = call_claude(prompt)
        if response is None:
            print(f"  Skipping {ticker} {quarter} — API call failed")
            continue

        divergence_label = response.get("divergence_label", "NEUTRAL")
        confidence       = response.get("confidence_score", 0.0)
        print(f"  Divergence: {divergence_label} (confidence={confidence:.2f})")
        print(f"  Tone: {response.get('mda_tone')} | Acknowledged: {response.get('anomaly_acknowledged')}")
        print(f"  Cited: \"{response.get('cited_passage', '')[:120]}\"")

        results.append({
            "ticker":               ticker,
            "report_date":          str(report_date),
            "calendar_quarter":     quarter,
            "gics_sector":          row.get("gics_sector", ""),
            "anomaly_score_0_100":  score,
            "divergence_label":     divergence_label,
            "confidence_score":     float(confidence),
            "cited_passage":        response.get("cited_passage", ""),
            "rationale":            response.get("rationale", ""),
            "mda_tone":             response.get("mda_tone", ""),
            "anomaly_acknowledged": bool(response.get("anomaly_acknowledged", False)),
            "model_used":           MODEL,
            "scoring_version":      row.get("scoring_version", ""),
            "generated_at":         generated_at,
        })

        time.sleep(API_DELAY_SEC)

    if not args.dry_run and results:
        print(f"\nUploading {len(results)} results to BigQuery...")
        upload_divergence(bq_client, results, force=args.force)

    print("\nDone.")


if __name__ == "__main__":
    main()
