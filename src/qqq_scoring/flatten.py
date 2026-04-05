"""GCS reading and flattening logic — Step 0."""

import json
import re

import pandas as pd
from google.cloud import storage

METADATA_KEYS = {"target_year", "target_period_end", "target_form", "years_covered", "feature_count"}


def _safe_col(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9_]", "_", name)
    if name and name[0].isdigit():
        name = f"f_{name}"
    return name


def load_gcs_bundles(
    bucket_name: str,
    prefix: str = "qqq",
    form_type: str = "10-Q",
    max_tickers: int | None = None,
    tickers: list[str] | None = None,
) -> pd.DataFrame:
    """Read all analysis_ready.json files from GCS and return a flat DataFrame.

    Each row = one filing period for one company.
    Filtered to form_type (default 10-Q).
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)

    allowed_tickers = set(tickers) if tickers else None
    seen_tickers: set[str] = set()
    records = []

    # If specific tickers requested, list each ticker's prefix directly
    if allowed_tickers:
        all_blobs = []
        for ticker in allowed_tickers:
            ticker_prefix = f"{prefix}/{ticker}/"
            all_blobs.extend(bucket.list_blobs(prefix=ticker_prefix))
    else:
        all_blobs = bucket.list_blobs(prefix=prefix)

    for blob in all_blobs:
        if not blob.name.endswith("_analysis_ready.json"):
            continue

        try:
            data = json.loads(blob.download_as_text())
        except Exception as exc:
            print(f"  Warning: failed to parse {blob.name}: {exc}")
            continue

        filing = data.get("selected_filing", {})
        if filing.get("form") != form_type:
            continue

        ticker = data.get("extraction_input", {}).get("ticker")
        if not ticker:
            continue

        if allowed_tickers and ticker not in allowed_tickers:
            continue

        if max_tickers is not None:
            if ticker not in seen_tickers and len(seen_tickers) >= max_tickers:
                continue
            seen_tickers.add(ticker)

        eaf = data.get("engineered_anomaly_features", {})
        if not eaf:
            continue

        record: dict = {
            "ticker": ticker,
            "form_type": filing.get("form"),
            "report_date": filing.get("report_date"),
            "filing_date": filing.get("filing_date"),
            "accession_number": filing.get("accession_number"),
            "filing_url": data.get("selected_filing_url"),
        }

        for k, v in eaf.items():
            if k in METADATA_KEYS:
                continue
            record[_safe_col(k)] = v

        records.append(record)

    return pd.DataFrame(records)
