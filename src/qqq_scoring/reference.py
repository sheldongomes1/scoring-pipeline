"""Reference data loading — static lookup tables pulled from GCS each pipeline run."""

import io

import pandas as pd
from google.cloud import storage

GCS_SECTOR_PATH = "qqq/reference/ticker_sectors.csv"


def load_sector_mapping(bucket_name: str) -> pd.DataFrame:
    """Download ticker → GICS sector mapping from GCS.

    Returns a DataFrame with columns: ticker, gics_sector, gics_sub_industry.
    The file lives at gs://{bucket_name}/{GCS_SECTOR_PATH} and is pulled fresh
    on every pipeline run so sector updates (e.g. QQQ rebalances) take effect
    without code changes — just overwrite the file in GCS.
    """
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(GCS_SECTOR_PATH)
    content = blob.download_as_text()
    df = pd.read_csv(io.StringIO(content))
    print(f"  Loaded sector mapping: {len(df)} tickers, "
          f"{df['gics_sector'].nunique()} sectors from gs://{bucket_name}/{GCS_SECTOR_PATH}")
    return df
