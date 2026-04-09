#!/usr/bin/env python3
"""Score narrative-quantitative divergence for anomalous filings.

For each qualifying filing, loads the MD&A section from GCS, calls Claude to
assess whether management's narrative contradicts, corroborates, or is neutral
toward the quantitative anomaly signals, and writes results to BigQuery.

Usage:
    # Batch mode — score all filings with alert_score >= 5
    python explanations/score_narrative_divergence.py --min-alert-score 5

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

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.beneish import MANIPULATION_THRESHOLD

sys.path.insert(0, os.path.dirname(__file__))
from generate_explanations import compute_alert_score, load_scores
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

        return json.loads(text)

    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"  API error: {e}")
        return None


# ── BigQuery upload ───────────────────────────────────────────────────────────

def upload_divergence(bq_client: bigquery.Client, results: list[dict]) -> None:
    """Write divergence results to BigQuery."""
    if not results:
        print("No results to upload.")
        return

    df = pd.DataFrame(results)
    df["report_date"] = pd.to_datetime(df["report_date"], utc=True).dt.date

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="report_date",
        ),
        clustering_fields=["ticker"],
    )

    job = bq_client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    print(f"Uploaded {len(df)} divergence results → {OUTPUT_TABLE}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Score narrative-quantitative divergence")
    parser.add_argument("--ticker",          default=None, help="Single ticker (e.g. WBD)")
    parser.add_argument("--quarter",         default=None, help="Single quarter (e.g. 2022-Q2)")
    parser.add_argument("--min-alert-score", type=int, default=5, help="Minimum alert score to process (default: 5)")
    parser.add_argument("--dry-run",         action="store_true", help="Print prompts without calling Claude")
    args = parser.parse_args()

    bq_client  = bigquery.Client(project=BQ_PROJECT)
    gcs_client = storage.Client()

    # Load scored filings from BQ
    df = load_scores(bq_client, args.ticker, args.quarter)
    if df.empty:
        print("No filings found.")
        return

    df["alert_score"] = df.apply(compute_alert_score, axis=1)

    if args.ticker and args.quarter:
        qualifying = df.copy()
    else:
        qualifying = df[df["alert_score"] >= args.min_alert_score].copy()

    qualifying = qualifying.sort_values("alert_score", ascending=False).reset_index(drop=True)
    print(f"\n{len(qualifying)} filings qualify (alert_score >= {args.min_alert_score})")

    if qualifying.empty:
        print("Nothing to process.")
        return

    results = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for i, (_, row) in enumerate(qualifying.iterrows(), 1):
        ticker      = row["ticker"]
        quarter     = row["calendar_quarter"]
        report_date = row["report_date"]
        form_type   = row.get("form_type", "10-Q")
        alert       = int(row["alert_score"])

        print(f"\n[{i}/{len(qualifying)}] {ticker} {quarter}  (alert_score={alert})")

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
            "anomaly_score_0_100":  float(row.get("anomaly_score_0_100", 0)),
            "alert_score":          alert,
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
        upload_divergence(bq_client, results)

    print("\nDone.")


if __name__ == "__main__":
    main()
