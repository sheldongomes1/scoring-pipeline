#!/usr/bin/env python3
"""Build the company_trend BQ table for UI time-series chart rendering.

Produces one row per (ticker, calendar_quarter) with all metric columns
pre-joined and pre-formatted. The UI queries:

    SELECT * FROM `qqq_finance.company_trend`
    WHERE ticker = 'INSM'
    ORDER BY report_date

and gets a flat array ready to render in a chart library — no pivoting,
no joins, no computation on the client side.

Columns included:
  - Identity: ticker, calendar_quarter, report_date, gics_sector
  - Conviction: conviction_score, conviction_tier, pillar_anomaly,
                pillar_earnings, pillar_transparency
  - Anomaly: anomaly_score_0_100, beneish_m_score, beneish_manipulation_flag
  - Feature z-scores (combined_z per feature, renamed z_* for brevity)
  - Narrative: divergence_label, mda_tone, pattern_name

Table is clustered by ticker — BQ skips all other ticker data on each query,
making per-ticker lookups very fast regardless of total table size.

Usage:
    python scripts/build_trend_table.py
    python scripts/build_trend_table.py --dry-run
"""

import argparse
import io

import pandas as pd
from google.cloud import bigquery

BQ_PROJECT   = "qqq-anomaly-lab"
BQ_DATASET   = "qqq_finance"
OUTPUT_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.company_trend"

FEATURE_KEYS = [
    "debt_to_assets",
    "net_margin",
    "ocf_to_net_income",
    "ocf_to_assets",
    "accrual_ratio",
    "revenue_growth_yoy",
    "assets_growth_yoy",
    "net_income_growth_yoy",
]

# Build the combined_z__ select list — rename to z_* for clean UI keys
Z_SCORE_SELECTS = "\n".join(
    f"  s.combined_z__{feat}  AS z_{feat},"
    for feat in FEATURE_KEYS
)

QUERY = f"""
SELECT
  -- ── Identity ──────────────────────────────────────────────────────────────
  s.ticker,
  s.calendar_quarter,
  s.report_date,
  s.gics_sector,
  s.filing_url,

  -- ── Conviction pillars ────────────────────────────────────────────────────
  c.conviction_score,
  c.conviction_tier,
  c.pillar_anomaly,
  c.pillar_earnings,
  c.pillar_transparency,

  -- ── Anomaly & Beneish ─────────────────────────────────────────────────────
  s.anomaly_score_0_100,
  s.beneish_m_score,
  s.beneish_manipulation_flag,

  -- ── Feature z-scores (combined self+peer) — one per metric ───────────────
{Z_SCORE_SELECTS}

  -- ── Narrative signals ─────────────────────────────────────────────────────
  n.divergence_label,
  n.mda_tone,
  e.pattern_name

FROM `{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed` s
LEFT JOIN `{BQ_PROJECT}.{BQ_DATASET}.conviction_scores` c
  ON s.ticker = c.ticker AND s.report_date = c.report_date
LEFT JOIN `{BQ_PROJECT}.{BQ_DATASET}.narrative_divergence` n
  ON s.ticker = n.ticker AND s.report_date = n.report_date
LEFT JOIN `{BQ_PROJECT}.{BQ_DATASET}.anomaly_explanations` e
  ON s.ticker = e.ticker AND s.report_date = e.report_date
ORDER BY s.ticker, s.report_date
"""


def main() -> None:
    parser = argparse.ArgumentParser(description="Build company_trend table")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    client = bigquery.Client(project=BQ_PROJECT)

    print("Querying filing_intelligence join for trend data...")
    df = client.query(QUERY).to_dataframe()
    print(f"  {len(df)} rows, {df['ticker'].nunique()} tickers")

    if args.dry_run:
        print("\nSample (INSM):")
        sample = df[df["ticker"] == "INSM"][[
            "ticker", "calendar_quarter", "conviction_score", "conviction_tier",
            "anomaly_score_0_100", "z_net_margin", "z_revenue_growth_yoy",
            "divergence_label", "pattern_name"
        ]]
        print(sample.to_string(index=False))
        print("\nDry run — not uploading.")
        return

    # Prepare for upload
    upload_df = df.copy()
    upload_df["report_date"] = pd.to_datetime(upload_df["report_date"], utc=True).dt.date

    buf = io.BytesIO()
    upload_df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        clustering_fields=["ticker"],   # fast per-ticker queries
    )

    print(f"Uploading to {OUTPUT_TABLE}...")
    job = client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    print(f"Uploaded {len(upload_df)} rows → {OUTPUT_TABLE}")
    print(f"\nUI query pattern:")
    print(f"  SELECT * FROM `{OUTPUT_TABLE}` WHERE ticker = ? ORDER BY report_date")
    print("Done.")


if __name__ == "__main__":
    main()
