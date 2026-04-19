#!/usr/bin/env python3
"""Compute the `score_explanation` table — sibling to conviction_scores.

Phase 1 (v1): pure derivation from already-persisted BigQuery tables. Reads
quarterly_scores_detailed + conviction_scores + narrative_divergence, joins
on (ticker, report_date), and emits one row per filing into
`qqq_finance.score_explanation` with the full pillar-by-pillar breakdown.

NO changes to scorer.py / compute_conviction.py. NO changes to existing tables.
This script is purely additive — designed so it cannot regress production scoring.

Self/peer medians + IQRs and winsorization caps are NOT in BQ today; those
fields render as null and the row is tagged `baseline_availability =
"baseline_unavailable_v1"`. Phase 2 will add scorer.py hooks to persist them
and bump explanation_version → "v2".

Usage:
    # Full refresh (default — overwrites all rows with WRITE_TRUNCATE)
    python explanations/compute_score_explanation.py

    # Single filing (for spot-checking; still writes to BQ)
    python explanations/compute_score_explanation.py --ticker PLTR --quarter 2024-Q2

    # Dry run — print sample rows, do not touch BQ
    python explanations/compute_score_explanation.py --dry-run
    python explanations/compute_score_explanation.py --ticker PLTR --quarter 2024-Q2 --dry-run
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, os.path.dirname(__file__))
from score_explanation_helpers import (
    FEATURE_KEYS,
    beneish_threshold_for_sector,
    build_components_array,
    build_earnings_transform_note,
    build_features_array,
    build_final_equation,
    build_missing_components,
    build_narrative_transform_note,
    build_statistical_transform_note,
    _b,
    _f,
    _i,
    _s,
)


# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT  = "qqq-anomaly-lab"
BQ_DATASET  = "qqq_finance"
SCORES_T    = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
CONVICTION  = f"{BQ_PROJECT}.{BQ_DATASET}.conviction_scores"
DIVERGENCE  = f"{BQ_PROJECT}.{BQ_DATASET}.narrative_divergence"
OUTPUT_T    = f"{BQ_PROJECT}.{BQ_DATASET}.score_explanation"

EXPLANATION_VERSION = "v1"
MODEL_VERSION       = "brick3_q_v5_beneish"


# ── BigQuery schema for score_explanation ─────────────────────────────────────

def build_schema() -> list[bigquery.SchemaField]:
    """Explicit schema for the score_explanation table.

    Defined here (not inferred from the data) so the table shape is stable
    and reviewable in code review. Bump EXPLANATION_VERSION when this changes.
    """
    feature_fields = [
        bigquery.SchemaField("name",             "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("display_name",     "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("self_z",           "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("self_median",      "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("self_iqr",         "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("peer_z",           "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("peer_median",      "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("peer_iqr",         "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("combined_z",       "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("z_clip_applied",   "BOOLEAN", mode="NULLABLE"),
        bigquery.SchemaField("contribution_pct", "FLOAT",   mode="NULLABLE"),
    ]

    component_fields = [
        bigquery.SchemaField("name",           "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("display_name",   "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("value",          "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("coefficient",    "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("contribution",   "FLOAT",   mode="NULLABLE"),
        bigquery.SchemaField("interpretation", "STRING",  mode="NULLABLE"),
        bigquery.SchemaField("available",      "BOOLEAN", mode="NULLABLE"),
    ]

    return [
        bigquery.SchemaField("ticker",                 "STRING",    mode="REQUIRED"),
        bigquery.SchemaField("calendar_quarter",       "STRING",    mode="REQUIRED"),
        bigquery.SchemaField("report_date",            "DATE",      mode="REQUIRED"),

        bigquery.SchemaField("conviction_score",       "FLOAT",     mode="NULLABLE"),
        bigquery.SchemaField("conviction_tier",        "STRING",    mode="NULLABLE"),
        bigquery.SchemaField("final_equation",         "STRING",    mode="REQUIRED"),

        bigquery.SchemaField("pillar_contributions",   "RECORD",    mode="REQUIRED", fields=[
            bigquery.SchemaField("statistical", "FLOAT", mode="NULLABLE"),
            bigquery.SchemaField("earnings",    "FLOAT", mode="NULLABLE"),
            bigquery.SchemaField("narrative",   "FLOAT", mode="NULLABLE"),
        ]),

        bigquery.SchemaField("statistical_pillar",     "RECORD",    mode="REQUIRED", fields=[
            bigquery.SchemaField("anomaly_score_0_100",  "FLOAT",   mode="NULLABLE"),
            bigquery.SchemaField("mahalanobis_distance", "FLOAT",   mode="NULLABLE"),
            bigquery.SchemaField("num_features_used",    "INTEGER", mode="NULLABLE"),
            bigquery.SchemaField("peer_count",           "INTEGER", mode="NULLABLE"),
            bigquery.SchemaField("gics_sector",          "STRING",  mode="NULLABLE"),
            bigquery.SchemaField("features",             "RECORD",  mode="REPEATED",
                                 fields=feature_fields),
            bigquery.SchemaField("score_transform_note", "STRING",  mode="NULLABLE"),
            bigquery.SchemaField("baseline_availability","STRING",  mode="NULLABLE"),
        ]),

        bigquery.SchemaField("earnings_pillar",        "RECORD",    mode="NULLABLE", fields=[
            bigquery.SchemaField("beneish_m_score",      "FLOAT",   mode="NULLABLE"),
            bigquery.SchemaField("threshold_used",       "FLOAT",   mode="NULLABLE"),
            bigquery.SchemaField("threshold_rationale",  "STRING",  mode="NULLABLE"),
            bigquery.SchemaField("manipulation_flag",    "BOOLEAN", mode="NULLABLE"),
            bigquery.SchemaField("components_available", "INTEGER", mode="NULLABLE"),
            bigquery.SchemaField("components_missing",   "STRING",  mode="REPEATED"),
            bigquery.SchemaField("components",           "RECORD",  mode="REPEATED",
                                 fields=component_fields),
            bigquery.SchemaField("score_transform_note", "STRING",  mode="NULLABLE"),
        ]),

        bigquery.SchemaField("narrative_pillar",       "RECORD",    mode="NULLABLE", fields=[
            bigquery.SchemaField("divergence_label",     "STRING",  mode="NULLABLE"),
            bigquery.SchemaField("confidence_score",     "FLOAT",   mode="NULLABLE"),
            bigquery.SchemaField("mda_tone",             "STRING",  mode="NULLABLE"),
            bigquery.SchemaField("anomaly_acknowledged", "BOOLEAN", mode="NULLABLE"),
            bigquery.SchemaField("cited_passage",        "STRING",  mode="NULLABLE"),
            bigquery.SchemaField("score_transform_note", "STRING",  mode="NULLABLE"),
        ]),

        bigquery.SchemaField("model_version",          "STRING",    mode="REQUIRED"),
        bigquery.SchemaField("explanation_version",    "STRING",    mode="REQUIRED"),
        bigquery.SchemaField("computed_at",            "TIMESTAMP", mode="REQUIRED"),
    ]


# ── BigQuery I/O ──────────────────────────────────────────────────────────────

def load_joined(client: bigquery.Client,
                ticker: str | None,
                quarter: str | None) -> pd.DataFrame:
    """Pull scores ⋈ conviction ⋈ narrative for the requested rows.

    Default (no ticker/quarter): returns ALL rows in quarterly_scores_detailed
    so the script's default mode is "rebuild the entire explanation table."
    """
    where_parts: list[str] = []
    if ticker:
        where_parts.append(f"s.ticker = '{ticker}'")
    if quarter:
        where_parts.append(f"s.calendar_quarter = '{quarter}'")
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

    query = f"""
        SELECT
            s.ticker, s.calendar_quarter, s.report_date, s.gics_sector,
            s.anomaly_score_0_100, s.mahalanobis_distance,
            s.num_features_used, s.peer_count,
            s.beneish_m_score, s.beneish_manipulation_flag, s.beneish_components_available,
            s.beneish_dsri, s.beneish_gmi, s.beneish_aqi, s.beneish_sgi,
            s.beneish_depi, s.beneish_sgai, s.beneish_tata, s.beneish_lvgi,
            {", ".join(f"s.self_z__{f}" for f in FEATURE_KEYS)},
            {", ".join(f"s.peer_z__{f}" for f in FEATURE_KEYS)},
            {", ".join(f"s.combined_z__{f}" for f in FEATURE_KEYS)},

            c.conviction_score, c.conviction_tier,
            c.pillar_anomaly, c.pillar_earnings, c.pillar_transparency,

            n.divergence_label, n.confidence_score, n.mda_tone,
            n.anomaly_acknowledged, n.cited_passage
        FROM `{SCORES_T}` s
        LEFT JOIN `{CONVICTION}` c
            USING (ticker, calendar_quarter)
        LEFT JOIN `{DIVERGENCE}` n
            ON s.ticker = n.ticker AND DATE(s.report_date) = DATE(n.report_date)
        {where_clause}
        ORDER BY s.ticker, s.report_date
    """
    print(f"Loading joined data from BigQuery...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} filings loaded")
    return df


def ensure_table(client: bigquery.Client) -> None:
    """Create score_explanation table if it doesn't exist (with explicit schema)."""
    table_ref = bigquery.TableReference.from_string(OUTPUT_T)
    try:
        client.get_table(table_ref)
        return
    except Exception:
        pass

    table = bigquery.Table(table_ref, schema=build_schema())
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY,
        field="report_date",
    )
    table.clustering_fields = ["ticker"]
    client.create_table(table)
    print(f"Created table {OUTPUT_T}")


def upload(client: bigquery.Client,
           rows: list[dict],
           full_refresh: bool) -> None:
    """Write rows to score_explanation.

    full_refresh=True (default): WRITE_TRUNCATE — replace all rows.
    full_refresh=False (single filing): WRITE_APPEND — used for spot-checks.

    Uses newline-delimited JSON load with the explicit schema; this is the
    cleanest path for nested STRUCT/ARRAY rows.
    """
    if not rows:
        print("  No rows to upload.")
        return

    disposition = (
        bigquery.WriteDisposition.WRITE_TRUNCATE if full_refresh
        else bigquery.WriteDisposition.WRITE_APPEND
    )

    # NDJSON in memory
    buf = io.BytesIO()
    for row in rows:
        buf.write(json.dumps(row, default=str).encode("utf-8"))
        buf.write(b"\n")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        write_disposition=disposition,
        schema=build_schema(),
    )
    job = client.load_table_from_file(buf, OUTPUT_T, job_config=job_config)
    job.result()
    mode = "TRUNCATE" if full_refresh else "APPEND"
    print(f"  Uploaded {len(rows)} rows → {OUTPUT_T}  [{mode}]")


# ── Row builder ───────────────────────────────────────────────────────────────

def build_row(r: pd.Series, computed_at_iso: str) -> dict:
    """Translate one joined row into the score_explanation schema."""
    sector = _s(r.get("gics_sector"))
    threshold, threshold_rationale = beneish_threshold_for_sector(sector)

    # Statistical pillar (always present — every filing has scoring data)
    statistical_pillar = {
        "anomaly_score_0_100":  _f(r.get("anomaly_score_0_100")),
        "mahalanobis_distance": _f(r.get("mahalanobis_distance")),
        "num_features_used":    _i(r.get("num_features_used")),
        "peer_count":           _i(r.get("peer_count")),
        "gics_sector":          sector,
        "features":             build_features_array(r),
        "score_transform_note": build_statistical_transform_note(r),
        "baseline_availability": "baseline_unavailable_v1",
    }

    # Earnings pillar — only when Beneish actually computed
    if pd.notna(r.get("beneish_m_score")):
        earnings_pillar: dict | None = {
            "beneish_m_score":      _f(r.get("beneish_m_score")),
            "threshold_used":       threshold,
            "threshold_rationale":  threshold_rationale,
            "manipulation_flag":    _b(r.get("beneish_manipulation_flag")),
            "components_available": _i(r.get("beneish_components_available")),
            "components_missing":   build_missing_components(r),
            "components":           build_components_array(r),
            "score_transform_note": build_earnings_transform_note(r, threshold),
        }
    else:
        earnings_pillar = None

    # Narrative pillar — only when divergence has been scored
    if _s(r.get("divergence_label")) is not None:
        narrative_pillar: dict | None = {
            "divergence_label":     _s(r.get("divergence_label")),
            "confidence_score":     _f(r.get("confidence_score")),
            "mda_tone":             _s(r.get("mda_tone")),
            "anomaly_acknowledged": _b(r.get("anomaly_acknowledged")),
            "cited_passage":        _s(r.get("cited_passage")),
            "score_transform_note": build_narrative_transform_note(r),
        }
    else:
        narrative_pillar = None

    return {
        "ticker":                 r["ticker"],
        "calendar_quarter":       r["calendar_quarter"],
        "report_date":            str(pd.to_datetime(r["report_date"]).date()),

        "conviction_score":       _f(r.get("conviction_score")),
        "conviction_tier":        _s(r.get("conviction_tier")),
        "final_equation":         build_final_equation(r),
        "pillar_contributions":   {
            "statistical": _f(r.get("pillar_anomaly")),
            "earnings":    _f(r.get("pillar_earnings")),
            "narrative":   _f(r.get("pillar_transparency")),
        },

        "statistical_pillar":  statistical_pillar,
        "earnings_pillar":     earnings_pillar,
        "narrative_pillar":    narrative_pillar,

        "model_version":       MODEL_VERSION,
        "explanation_version": EXPLANATION_VERSION,
        "computed_at":         computed_at_iso,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Build the score_explanation table")
    parser.add_argument("--ticker",  default=None, help="Single ticker (e.g. PLTR)")
    parser.add_argument("--quarter", default=None, help="Single quarter (e.g. 2024-Q2)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print sample row(s) instead of uploading to BQ")
    args = parser.parse_args()

    client = bigquery.Client(project=BQ_PROJECT)

    if not args.dry_run:
        ensure_table(client)

    df = load_joined(client, args.ticker, args.quarter)
    if df.empty:
        print("No rows to process.")
        return

    computed_at_iso = datetime.now(timezone.utc).isoformat()
    rows = [build_row(r, computed_at_iso) for _, r in df.iterrows()]
    print(f"Built {len(rows)} explanation rows")

    if args.dry_run:
        sample = rows[0]
        print("\n── DRY RUN — first row ──")
        print(json.dumps(sample, indent=2, default=str))
        if len(rows) > 1:
            print(f"\n... and {len(rows) - 1} more row(s) suppressed.")
        return

    # Default to full refresh; for single-filing spot-checks, append instead
    full_refresh = (args.ticker is None and args.quarter is None)
    upload(client, rows, full_refresh=full_refresh)

    # Summary stats
    pillars = [r.get("conviction_tier") for r in rows]
    tier_counts: dict[str, int] = {}
    for t in pillars:
        tier_counts[t or "(none)"] = tier_counts.get(t or "(none)", 0) + 1
    print("\n── Conviction tier distribution in explanation table ──")
    for tier in ["ALERT", "FLAG", "WATCH", "(none)"]:
        if tier in tier_counts:
            print(f"  {tier:8s}: {tier_counts[tier]:4d}")
    print("\nDone.")


if __name__ == "__main__":
    main()
