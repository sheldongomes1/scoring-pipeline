#!/usr/bin/env python3
"""Step 0 (BQ variant): Flatten BigQuery filings table into a period feature table."""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from qqq_scoring.flatten_bq import load_bq_filings
from qqq_scoring.features import discover_feature_keys


def main() -> None:
    parser = argparse.ArgumentParser(description="Flatten BQ filings table to period_features.json")
    parser.add_argument("--project", default="qqq-anomaly-lab")
    parser.add_argument("--form-type", default="10-Q")
    parser.add_argument("--output-dir", default="output")
    args = parser.parse_args()

    df = load_bq_filings(project=args.project, form_type=args.form_type)

    os.makedirs(args.output_dir, exist_ok=True)

    out_features = os.path.join(args.output_dir, "period_features.json")
    df.to_json(out_features, orient="records", indent=2, date_format="iso")
    print(f"Saved → {out_features}")

    keys = discover_feature_keys(df)
    out_keys = os.path.join(args.output_dir, "feature_keys.json")
    with open(out_keys, "w") as f:
        json.dump(keys, f, indent=2)
    print(f"Saved → {out_keys}  ({len(keys)} feature keys: {keys})")


if __name__ == "__main__":
    main()
