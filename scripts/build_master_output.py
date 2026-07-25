#!/usr/bin/env python3
"""Create the filing_intelligence BQ view and materialise the top anomaly review pack.

Step 1 — Create (or replace) the BQ view `qqq_finance.filing_intelligence`:
    Joins all four pipeline output tables into one flat row per filing:
      quarterly_scores_detailed  (quantitative scores, z-scores, Beneish)
      conviction_scores          (three-pillar conviction score + tier)
      anomaly_explanations       (LLM pattern classification + analyst brief)
      narrative_divergence       (MD&A CONTRADICTS / CORROBORATES / NEUTRAL)

Step 2 — Materialise the top anomaly review pack:
    Queries the view for ALERT + FLAG tier filings, sorted by conviction_score.
    Writes to `qqq_finance.top_anomaly_review_pack` (WRITE_TRUNCATE).

Usage:
    python scripts/build_master_output.py
    python scripts/build_master_output.py --dry-run   # print preview, don't write
"""

import argparse
import io

import pandas as pd
from google.cloud import bigquery

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT    = "qqq-anomaly-lab"
BQ_DATASET    = "qqq_finance"

SCORES_TABLE      = f"`{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed`"
CONVICTION_TABLE  = f"`{BQ_PROJECT}.{BQ_DATASET}.conviction_scores`"
EXPLANATION_TABLE = f"`{BQ_PROJECT}.{BQ_DATASET}.anomaly_explanations`"
DIVERGENCE_TABLE  = f"`{BQ_PROJECT}.{BQ_DATASET}.narrative_divergence`"

VIEW_ID       = f"{BQ_PROJECT}.{BQ_DATASET}.filing_intelligence"
PACK_TABLE_ID = f"{BQ_PROJECT}.{BQ_DATASET}.top_anomaly_review_pack"

# Include ALERT + FLAG tiers in the review pack
PACK_TIERS = ("ALERT", "FLAG", "WATCH")


# ── View DDL ──────────────────────────────────────────────────────────────────

VIEW_SQL = f"""
SELECT
  -- ── Identity ──────────────────────────────────────────────────────────────
  s.ticker,
  s.company_name,
  s.cik,
  s.report_date,
  s.calendar_quarter,
  s.gics_sector,
  s.form_type,
  SAFE_CAST(s.filing_date AS DATE)   AS filing_date,
  s.filing_url,

  -- ── Quantitative anomaly ──────────────────────────────────────────────────
  s.anomaly_score_0_100,
  s.mahalanobis_distance,
  s.self_history_score,
  s.peer_relative_score,
  s.combined_signal_strength,
  s.peer_count,

  -- Top drivers
  s.top_driver_1,
  s.top_driver_1_value,
  s.top_driver_2,
  s.top_driver_2_value,
  s.top_driver_3,
  s.top_driver_3_value,

  -- ── Beneish ───────────────────────────────────────────────────────────────
  s.beneish_m_score,
  s.beneish_manipulation_flag,
  s.beneish_components_available,
  s.beneish_dsri,
  s.beneish_gmi,
  s.beneish_aqi,
  s.beneish_sgi,
  s.beneish_depi,
  s.beneish_sgai,
  s.beneish_tata,
  s.beneish_lvgi,

  -- ── Conviction (three-pillar synthesis) ──────────────────────────────────
  c.pillar_anomaly,
  c.pillar_earnings,
  c.pillar_transparency,
  c.conviction_score,
  c.conviction_tier,

  -- ── Pattern classification (LLM analyst brief) ───────────────────────────
  e.pattern_name,
  e.pattern_confidence,
  e.pattern_summary,
  e.explanation_brief,

  -- ── Narrative divergence (MD&A vs numbers) ───────────────────────────────
  n.divergence_label,
  n.confidence_score        AS divergence_confidence,
  n.mda_tone,
  n.anomaly_acknowledged,
  n.cited_passage,
  n.rationale               AS divergence_rationale,

  -- ── Metadata ──────────────────────────────────────────────────────────────
  s.scoring_version,
  s.scored_at,
  c.computed_at             AS conviction_computed_at

FROM {SCORES_TABLE} s
LEFT JOIN {CONVICTION_TABLE} c
  ON s.ticker = c.ticker AND s.report_date = c.report_date
LEFT JOIN {EXPLANATION_TABLE} e
  ON s.ticker = e.ticker AND s.report_date = e.report_date
LEFT JOIN {DIVERGENCE_TABLE} n
  ON s.ticker = n.ticker AND s.report_date = n.report_date
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def create_view(client: bigquery.Client) -> None:
    """Create or replace the filing_intelligence view."""
    view = bigquery.Table(VIEW_ID)
    view.view_query = VIEW_SQL.strip()
    try:
        client.delete_table(VIEW_ID, not_found_ok=True)
        client.create_table(view)
        print(f"View created: {VIEW_ID}")
    except Exception as e:
        print(f"Error creating view: {e}")
        raise


def build_review_pack(client: bigquery.Client) -> pd.DataFrame:
    """Query the view for ALERT + FLAG tier filings, sorted by conviction_score."""
    tiers_str = ", ".join(f"'{t}'" for t in PACK_TIERS)
    query = f"""
        SELECT *
        FROM `{VIEW_ID}`
        WHERE conviction_tier IN ({tiers_str})
        ORDER BY conviction_score DESC, anomaly_score_0_100 DESC
    """
    print(f"Querying view for tiers: {PACK_TIERS}...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings in review pack")
    return df


def upload_review_pack(client: bigquery.Client, df: pd.DataFrame) -> None:
    """Write the review pack to BQ, replacing previous version."""
    upload_df = df.copy()
    upload_df["report_date"] = pd.to_datetime(upload_df["report_date"], utc=True).dt.date

    # filing_date may also be date type
    if "filing_date" in upload_df.columns:
        upload_df["filing_date"] = pd.to_datetime(upload_df["filing_date"], errors="coerce", utc=True).dt.date

    buf = io.BytesIO()
    upload_df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="report_date",
        ),
        clustering_fields=["conviction_tier", "ticker"],
    )

    job = client.load_table_from_file(buf, PACK_TABLE_ID, job_config=job_config)
    job.result()
    print(f"Uploaded {len(upload_df)} rows → {PACK_TABLE_ID}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Create BQ view + materialise review pack")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing to BQ")
    args = parser.parse_args()

    client = bigquery.Client(project=BQ_PROJECT)

    # Step 1 — create the view
    print("Step 1: Creating filing_intelligence view...")
    if args.dry_run:
        print("  (dry run — skipping view creation)")
        print(f"  View SQL preview ({len(VIEW_SQL)} chars):\n{VIEW_SQL[:400]}...")
    else:
        create_view(client)

    # Step 2 — build and upload review pack
    print("\nStep 2: Building top anomaly review pack...")
    if args.dry_run:
        print("  (dry run — skipping query and upload)")
        return

    df = build_review_pack(client)

    # Print summary
    print("\n── Review pack summary ──")
    print(df.groupby("conviction_tier")["ticker"].count().rename("filings").to_string())

    print("\n── Top 20 (conviction_score) ──")
    cols = ["ticker", "calendar_quarter", "conviction_score", "conviction_tier",
            "divergence_label", "pattern_name", "anomaly_score_0_100"]
    # pattern_name may be null if explanations batch hasn't run
    available_cols = [c for c in cols if c in df.columns]
    print(df[available_cols].head(20).to_string(index=False))

    print(f"\nStep 3: Uploading review pack to BigQuery...")
    upload_review_pack(client, df)

    print("\nDone.")
    print(f"\nDownstream consumers can now query:")
    print(f"  Full universe:    SELECT * FROM `{VIEW_ID}` ORDER BY conviction_score DESC")
    print(f"  Review pack only: SELECT * FROM `{PACK_TABLE_ID}` ORDER BY conviction_score DESC")


if __name__ == "__main__":
    main()
