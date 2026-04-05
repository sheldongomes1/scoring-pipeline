#!/usr/bin/env python3
"""Step 0: Flatten GCS analysis_ready.json bundles into a period feature table."""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.flatten import load_gcs_bundles
from qqq_scoring.features import discover_feature_keys
import json


def main() -> None:
    parser = argparse.ArgumentParser(description="Flatten GCS bundles to period_features.json")
    parser.add_argument("--gcs-bucket", default="qqq-anomaly-raw-sg")
    parser.add_argument("--gcs-prefix", default="qqq")
    parser.add_argument("--form-type", default="10-Q")
    parser.add_argument("--output-dir", default="output")
    parser.add_argument("--max-tickers", type=int, default=None, help="Limit to N tickers (for testing)")
    parser.add_argument("--tickers", default=None, help="Comma-separated ticker list, e.g. AAPL,MSFT")
    args = parser.parse_args()

    tickers = [t.strip() for t in args.tickers.split(",")] if args.tickers else None

    print(f"Reading gs://{args.gcs_bucket}/{args.gcs_prefix}/ (form={args.form_type}) ...")
    df = load_gcs_bundles(
        bucket_name=args.gcs_bucket,
        prefix=args.gcs_prefix,
        form_type=args.form_type,
        max_tickers=args.max_tickers,
        tickers=tickers,
    )
    print(f"Loaded {len(df)} records across {df['ticker'].nunique()} tickers")

    os.makedirs(args.output_dir, exist_ok=True)

    out_features = os.path.join(args.output_dir, "period_features.json")
    df.to_json(out_features, orient="records", indent=2, date_format="iso")
    print(f"Saved → {out_features}")

    # Also save feature keys
    keys = discover_feature_keys(df)
    out_keys = os.path.join(args.output_dir, "feature_keys.json")
    with open(out_keys, "w") as f:
        json.dump(keys, f, indent=2)
    print(f"Saved → {out_keys}  ({len(keys)} feature keys: {keys})")


if __name__ == "__main__":
    main()
