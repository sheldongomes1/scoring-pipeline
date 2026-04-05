"""GCS upload utility for scoring pipeline outputs."""

import os
from google.cloud import storage

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
