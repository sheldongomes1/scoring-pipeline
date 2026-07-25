"""GCS and BigQuery upload utilities for scoring pipeline outputs."""

import io
import os

import pandas as pd
from google.cloud import bigquery, storage

GCS_BUCKET = "qqq-anomaly-raw-sg"
GCS_OUTPUT_PREFIX = "qqq/scoring_output"

OUTPUT_FILES = [
    "quarterly_scores_detailed.csv",
    "top_anomaly_review_pack.csv",
]


def upload_file(local_path: str, bucket_name: str = GCS_BUCKET, gcs_prefix: str = GCS_OUTPUT_PREFIX) -> str:
    """Upload a single local file to GCS. Returns the gs:// URI."""
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    filename = os.path.basename(local_path)
    blob_name = f"{gcs_prefix}/{filename}"
    blob = bucket.blob(blob_name)
    blob.upload_from_filename(local_path)
    uri = f"gs://{bucket_name}/{blob_name}"
    print(f"Uploaded {local_path} → {uri}")
    return uri


BQ_PROJECT = "qqq-anomaly-lab"
BQ_DATASET = "qqq_finance"
BQ_SCORES_TABLE = "quarterly_scores_detailed"


def upload_scores_to_bigquery(
    df: pd.DataFrame,
    project: str = BQ_PROJECT,
    dataset: str = BQ_DATASET,
    table: str = BQ_SCORES_TABLE,
) -> str:
    """Upload quarterly_scores_detailed DataFrame to BigQuery.

    Overwrites the table on every run (WRITE_TRUNCATE) so BQ always reflects
    the latest scoring run. Partitioned by report_date, clustered by ticker.

    Returns the fully qualified table ID.
    """
    client = bigquery.Client(project=project)
    table_id = f"{project}.{dataset}.{table}"

    # Ensure report_date is a proper date type for BQ partitioning
    upload_df = df.copy()
    upload_df["report_date"] = pd.to_datetime(upload_df["report_date"], utc=True).dt.date

    # Convert to parquet in memory — cleanest type inference via pyarrow
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
        clustering_fields=["ticker", "gics_sector"],
    )

    job = client.load_table_from_file(buf, table_id, job_config=job_config)
    job.result()  # wait for completion
    print(f"Uploaded {len(upload_df)} rows → {table_id}")
    return table_id


def upload_outputs(
    output_dir: str,
    bucket_name: str = GCS_BUCKET,
    gcs_prefix: str = GCS_OUTPUT_PREFIX,
) -> dict[str, str]:
    """Upload all scoring output CSVs from output_dir to GCS.

    Returns a dict mapping filename → gs:// URI for each uploaded file.
    Skips files that don't exist locally.
    """
    uploaded = {}
    for filename in OUTPUT_FILES:
        local_path = os.path.join(output_dir, filename)
        if os.path.exists(local_path):
            uri = upload_file(local_path, bucket_name=bucket_name, gcs_prefix=gcs_prefix)
            uploaded[filename] = uri
        else:
            print(f"Skipping {filename} — not found at {local_path}")
    return uploaded
