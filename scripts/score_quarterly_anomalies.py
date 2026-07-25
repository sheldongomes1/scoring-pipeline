#!/usr/bin/env python3
"""Steps 1–7: Score all quarterly filings for anomalies."""

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.features import load_feature_keys, winsorize
from qqq_scoring.beneish import compute_beneish
from qqq_scoring.reference import load_sector_mapping
from qqq_scoring.scorer import (
    self_history_zscores,
    peer_zscores,
    combine_zscores,
    anomaly_score,
    to_percentile_scores,
    summary_scores,
    top_drivers,
    to_calendar_quarter,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score quarterly filings for anomalies")
    parser.add_argument("--feature-keys", default="output/feature_keys.json")
    parser.add_argument("--period-features", default="output/period_features.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--n-drivers", type=int, default=3, help="Number of top driver features to report")
    parser.add_argument("--gcs-bucket", default="qqq-anomaly-raw-sg", help="GCS bucket for reference data")
    parser.add_argument("--upload-bq", action="store_true", help="Upload quarterly_scores_detailed to BigQuery after scoring")
    args = parser.parse_args()

    scored_at = datetime.now(timezone.utc).isoformat()

    # Step 1 — load
    print("Step 1: Loading features...")
    df = pd.read_json(args.period_features, orient="records")
    before = len(df)
    df = df.drop_duplicates(subset=["ticker", "report_date"]).reset_index(drop=True)
    if before != len(df):
        print(f"  Dropped {before - len(df)} duplicate (ticker, report_date) rows.")

    # Dedup calendar-quarter collisions: when two filings for the same ticker
    # map to the same calendar quarter (e.g. fiscal year-end transitions where
    # report_dates Jul-01 and Sep-30 both fall in Q3), keep only the row with
    # the LATEST report_date — it represents the most recent economic period.
    df["report_date"] = pd.to_datetime(df["report_date"])
    df["_cq"] = to_calendar_quarter(df["report_date"])
    before_cq = len(df)
    df = (
        df.sort_values("report_date")
        .drop_duplicates(subset=["ticker", "_cq"], keep="last")
        .reset_index(drop=True)
    )
    df = df.drop(columns=["_cq"])
    if before_cq != len(df):
        print(f"  Dropped {before_cq - len(df)} calendar-quarter duplicate rows (kept latest report_date per ticker+quarter).")

    feature_keys = load_feature_keys(args.feature_keys)
    print(f"  {len(df)} records, {df['ticker'].nunique()} tickers, {len(feature_keys)} features")

    # Step 1b — join GICS sector mapping (pulled fresh from GCS each run)
    print("Step 1b: Joining sector mapping from GCS...")
    sectors = load_sector_mapping(args.gcs_bucket)
    df = df.merge(sectors[["ticker", "gics_sector", "gics_sub_industry"]], on="ticker", how="left")
    missing_sectors = df["gics_sector"].isna().sum()
    if missing_sectors:
        print(f"  Warning: {df[df['gics_sector'].isna()]['ticker'].unique().tolist()} missing sector — falling back to universe peers")
        df["gics_sector"] = df["gics_sector"].fillna("Unknown")

    # Step 2 — winsorize
    print("Step 2: Winsorizing (5th–95th percentile clip)...")
    df = winsorize(df, feature_keys)

    # Step 3 — self-history z-scores
    print("Step 3: Self-history robust z-scores per ticker...")
    zh = self_history_zscores(df, feature_keys)

    # Step 4 — peer z-scores
    print("Step 4: Peer-relative robust z-scores by calendar quarter...")
    zp = peer_zscores(df, feature_keys)

    # Step 5 — combine
    print("Step 5: Combining z-scores (±8.0 clip)...")
    zdf = combine_zscores(zh, zp, feature_keys)

    # Step 6 — MCD Mahalanobis distance (squared D²)
    print("Step 6: Computing MCD Mahalanobis distances...")
    distances = anomaly_score(df, feature_keys)

    # Step 6b — percentile-scale to 0-100
    scores_100 = to_percentile_scores(distances)

    # Step 7 — assemble output
    print("Step 7: Assembling output with top drivers...")
    drivers_df = top_drivers(zdf, feature_keys, n=args.n_drivers)
    summ = summary_scores(zh, zp, zdf, feature_keys)

    out = df[["ticker", "form_type", "report_date", "filing_date", "filing_url"]].copy()
    out.insert(1, "company_name", df.get("company_name", ""))
    out.insert(2, "cik", df.get("cik", ""))
    out["calendar_quarter"] = to_calendar_quarter(df["report_date"])
    out["gics_sector"] = df.get("gics_sector", "Unknown")

    out["anomaly_score_0_100"] = scores_100
    out["mahalanobis_distance"] = distances
    out["self_history_score"] = summ["self_history_score"]
    out["peer_relative_score"] = summ["peer_relative_score"]
    out["combined_signal_strength"] = summ["combined_signal_strength"]
    out["num_features_used"] = summ["num_features_used"]
    out["peer_count"] = zp["peer_count"]
    out = pd.concat([out, drivers_df], axis=1)
    out["model_type"] = "robust_mcd"

    for col in feature_keys:
        out[f"self_z__{col}"] = zh[f"zh_{col}"]
    for col in feature_keys:
        out[f"peer_z__{col}"] = zp[f"zp_{col}"]
    for col in feature_keys:
        out[f"combined_z__{col}"] = zdf[f"z_{col}"]

    # Beneish M-Score
    print("Beneish: Computing M-Score and manipulation flags...")
    beneish_df = compute_beneish(df)
    m_score_coverage = beneish_df["beneish_m_score"].notna().sum()
    flag_count = beneish_df["beneish_manipulation_flag"].sum()
    print(f"  M-Score computed for {m_score_coverage}/{len(df)} filings — {flag_count} flagged as likely manipulators (M > -2.22)")
    out = pd.concat([out, beneish_df], axis=1)

    out["scoring_version"] = "brick3_q_v5_beneish"
    out["scored_at"] = scored_at

    out = out.sort_values("anomaly_score_0_100", ascending=False).reset_index(drop=True)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "quarterly_scores_detailed.csv")
    out.to_csv(out_path, index=False)
    print(f"Saved → {out_path}  ({len(out)} rows)")
    print("\nTop 5 anomalies:")
    print(out[["ticker", "report_date", "anomaly_score_0_100", "top_driver_1", "top_driver_1_value"]].head(5).to_string(index=False))

    if args.upload_bq:
        print("\nUploading scores to BigQuery...")
        from qqq_scoring.upload import upload_scores_to_bigquery
        upload_scores_to_bigquery(out)


if __name__ == "__main__":
    main()
