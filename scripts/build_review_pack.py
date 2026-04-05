#!/usr/bin/env python3
"""Build top anomaly review pack and upload outputs to GCS."""

import argparse
import json
import os
import sys

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.review import build_review_pack
from qqq_scoring.upload import upload_outputs


def main() -> None:
    parser = argparse.ArgumentParser(description="Build review pack and upload to GCS")
    parser.add_argument("--scores", default="output/quarterly_scores_detailed.csv")
    parser.add_argument("--period-features", default="output/period_features.json")
    parser.add_argument("--feature-keys", default="output/feature_keys.json")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--top-n", type=int, default=20)
    parser.add_argument("--upload-gcs", action="store_true", help="Upload outputs to GCS after saving")
    args = parser.parse_args()

    scores_df = pd.read_csv(args.scores)
    raw_df = pd.read_json(args.period_features, orient="records")
    with open(args.feature_keys) as f:
        feature_keys = json.load(f)

    review = build_review_pack(scores_df, raw_df, feature_keys, top_n=args.top_n)

    os.makedirs(args.output_dir, exist_ok=True)
    out_path = os.path.join(args.output_dir, "top_anomaly_review_pack.csv")
    review.to_csv(out_path, index=False)
    print(f"Saved → {out_path}  ({len(review)} rows)")

    print("\nTop anomaly review pack:")
    print(review[["ticker", "report_date", "anomaly_score_0_100", "top_driver_1", "top_driver_1_value"]].to_string(index=False))

    if args.upload_gcs:
        print("\nUploading outputs to GCS...")
        uploaded = upload_outputs(args.output_dir)
        for fname, uri in uploaded.items():
            print(f"  {fname} → {uri}")


if __name__ == "__main__":
    main()
