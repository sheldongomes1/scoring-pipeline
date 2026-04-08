#!/usr/bin/env python3
"""Steps 1–7: Score all quarterly filings for anomalies."""

import argparse
import os
import sys
from datetime import datetime, timezone

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.features import load_feature_keys, winsorize
from qqq_scoring.scorer import (
    self_history_zscores,
    peer_zscores,
    combine_zscores,
    anomaly_score,
    to_percentile_scores,
    summary_scores,
    top_drivers,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Score quarterly filings for anomalies")
    parser.add_argument("--feature-keys", default="output/feature_keys.json")
    parser.add_argument("--period-features", default="output/period_features.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--n-drivers", type=int, default=3, help="Number of top driver features to report")
    args = parser.parse_args()

    scored_at = datetime.now(timezone.utc).isoformat()

    # Step 1 — load
    print("Step 1: Loading features...")
    df = pd.read_json(args.period_features, orient="records")
    before = len(df)
    df = df.drop_duplicates(subset=["ticker", "report_date"]).reset_index(drop=True)
    if before != len(df):
        print(f"  Dropped {before - len(df)} duplicate (ticker, report_date) rows.")
    feature_keys = load_feature_keys(args.feature_keys)
    print(f"  {len(df)} records, {df['ticker'].nunique()} tickers, {len(feature_keys)} features")

    # Step 2 — winsorize
    print("Step 2: Winsorizing (5th–95th percentile clip)...")
    df = winsorize(df, feature_keys)

    # Step 3 — self-history z-scores
    print("Step 3: Self-history robust z-scores per ticker...")
    zh = self_history_zscores(df, feature_keys)

    # Step 4 — peer z-scores
    print("Step 4: Peer-relative robust z-scores by report_date...")
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

    out["anomaly_score_0_100"] = scores_100
    out["mahalanobis_distance"] = distances
    out["self_history_score"] = summ["self_history_score"]
    out["peer_relative_score"] = summ["peer_relative_score"]
    out["combined_signal_strength"] = summ["combined_signal_strength"]
    out["num_features_used"] = summ["num_features_used"]
    out = pd.concat([out, drivers_df], axis=1)
    out["model_type"] = "robust_mcd"

    for col in feature_keys:
        out[f"self_z__{col}"] = zh[f"zh_{col}"]
    for col in feature_keys:
        out[f"peer_z__{col}"] = zp[f"zp_{col}"]
    for col in feature_keys:
        out[f"combined_z__{col}"] = zdf[f"z_{col}"]

    out["scoring_version"] = "brick3_q_v2_robust_mahalanobis_clipped"
    out["scored_at"] = scored_at

    out = out.sort_values("anomaly_score_0_100", ascending=False).reset_index(drop=True)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "quarterly_scores_detailed.csv")
    out.to_csv(out_path, index=False)
    print(f"Saved → {out_path}  ({len(out)} rows)")
    print(f"\nTop 5 anomalies:")
    print(out[["ticker", "report_date", "anomaly_score_0_100", "top_driver_1", "top_driver_1_value"]].head(5).to_string(index=False))


if __name__ == "__main__":
    main()
