"""BigQuery reading and flattening logic — Step 0 (BQ variant)."""

import pandas as pd
from google.cloud import bigquery


def load_bq_filings(
    project: str = "qqq-anomaly-lab",
    form_type: str = "10-Q",
) -> pd.DataFrame:
    """Query BigQuery filings table and return a flat DataFrame.

    Each row = one filing period for one company.
    Column 'form' is renamed to 'form_type' for pipeline consistency.
    """
    client = bigquery.Client(project=project)

    query = f"""
        SELECT *
        FROM `{project}.qqq_anomaly.filings`
        WHERE form = '{form_type}'
    """

    print(f"Querying BigQuery: {project}.qqq_anomaly.filings (form={form_type}) ...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} rows returned, {df['ticker'].nunique()} tickers")
    print(f"  Columns: {list(df.columns)}")

    # Normalise 'form' → 'form_type' to match pipeline expectations
    if "form" in df.columns and "form_type" not in df.columns:
        df = df.rename(columns={"form": "form_type"})

    return df
