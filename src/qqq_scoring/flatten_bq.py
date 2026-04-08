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
          -- MSTR excluded: bitcoin holding company, not an operating business.
          -- Its financials (margin, growth, leverage) are driven by BTC price movements,
          -- not operations, making it non-comparable to every other QQQ constituent.
          -- Including it would contaminate peer z-scores for companies sharing its report_date.
          AND ticker != 'MSTR'
    """

    print(f"Querying BigQuery: {project}.qqq_anomaly.filings (form={form_type}) ...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} rows returned, {df['ticker'].nunique()} tickers")
    print(f"  Columns: {list(df.columns)}")

    # Normalise 'form' → 'form_type' to match pipeline expectations
    if "form" in df.columns and "form_type" not in df.columns:
        df = df.rename(columns={"form": "form_type"})

    return df
