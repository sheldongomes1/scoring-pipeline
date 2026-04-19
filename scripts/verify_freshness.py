#!/usr/bin/env python3
"""Verify that all BigQuery pipeline tables are fresh and consistent.

Checks:
  1. All pipeline tables exist and have rows
  2. Downstream tables are not older than their upstream dependencies
  3. Row counts are plausible (downstream <= upstream base table)

Exit code 0 = all fresh, 1 = stale or missing tables.

Usage:
    python scripts/verify_freshness.py            # Quick check
    python scripts/verify_freshness.py --verbose   # Show all timestamps
"""

import argparse
import sys
from datetime import timedelta

from google.cloud import bigquery

BQ_PROJECT = "qqq-anomaly-lab"
BQ_DATASET = "qqq_finance"

# ── Pipeline tables in dependency order ──────────────────────────────────────
# (table_id, step_label, upstream_table_ids)
# filing_intelligence is a VIEW — always live, no staleness possible.

PIPELINE_TABLES = [
    ("quarterly_scores_detailed",  "Step 2", []),
    ("anomaly_explanations",       "Step 3", ["quarterly_scores_detailed", "conviction_scores"]),
    ("narrative_divergence",       "Step 4", ["quarterly_scores_detailed"]),
    ("conviction_scores",          "Step 5", ["narrative_divergence"]),
    ("top_anomaly_review_pack",    "Step 6", ["quarterly_scores_detailed", "conviction_scores",
                                              "anomaly_explanations", "narrative_divergence"]),
    ("company_trend",              "Step 7", ["quarterly_scores_detailed", "conviction_scores",
                                              "anomaly_explanations", "narrative_divergence"]),
    ("analyst_actions",            "Step 8", ["top_anomaly_review_pack"]),
    ("score_explanation",          "Step 9", ["quarterly_scores_detailed", "conviction_scores",
                                              "narrative_divergence"]),
]

# Staleness tolerance — BQ write timestamps can differ by a few seconds
# even within the same pipeline run (parallel uploads, clock skew).
TOLERANCE = timedelta(seconds=30)


def check_freshness(verbose: bool = False) -> bool:
    """Query BQ metadata and verify all tables are fresh. Returns True if OK."""

    client = bigquery.Client(project=BQ_PROJECT)

    table_ids = [t[0] for t in PIPELINE_TABLES]
    table_ids_str = ", ".join(f"'{t}'" for t in table_ids)

    query = f"""
        SELECT
            table_id,
            TIMESTAMP_MILLIS(last_modified_time) AS last_modified,
            row_count
        FROM `{BQ_PROJECT}.{BQ_DATASET}.__TABLES__`
        WHERE table_id IN ({table_ids_str})
    """
    rows = list(client.query(query).result())
    info = {r.table_id: {"modified": r.last_modified, "rows": r.row_count} for r in rows}

    all_ok = True
    problems = []

    # ── 1. Check existence and row counts ────────────────────────────────────

    for table_id, step, _ in PIPELINE_TABLES:
        if table_id not in info:
            problems.append(f"MISSING  {table_id} ({step}) — table does not exist")
            all_ok = False
        elif info[table_id]["rows"] == 0:
            problems.append(f"EMPTY    {table_id} ({step}) — 0 rows")
            all_ok = False

    # ── 2. Check freshness: downstream must not be older than upstream ───────

    stale_tables = set()
    for table_id, step, upstreams in PIPELINE_TABLES:
        if table_id not in info:
            continue
        downstream_time = info[table_id]["modified"]
        for upstream_id in upstreams:
            if upstream_id not in info:
                continue
            upstream_time = info[upstream_id]["modified"]
            if downstream_time < (upstream_time - TOLERANCE):
                lag = upstream_time - downstream_time
                lag_str = str(lag).split(".")[0]  # drop microseconds
                problems.append(
                    f"STALE    {table_id} ({step}) is {lag_str} behind {upstream_id}"
                )
                stale_tables.add(table_id)
                all_ok = False

    # ── 3. Check row-count plausibility ──────────────────────────────────────

    base_rows = info.get("quarterly_scores_detailed", {}).get("rows", 0)
    if base_rows > 0:
        for table_id in ("top_anomaly_review_pack", "company_trend"):
            if table_id in info and info[table_id]["rows"] > base_rows:
                problems.append(
                    f"SUSPECT  {table_id} has {info[table_id]['rows']} rows "
                    f"but base table has {base_rows} — possible stale join"
                )

    # ── Report ───────────────────────────────────────────────────────────────

    if problems:
        print("── Freshness problems ──")
        for p in problems:
            print(f"  {p}")

    if verbose or not all_ok:
        print("\n── Table status ──")
        for table_id, step, _ in PIPELINE_TABLES:
            if table_id in info:
                mod = info[table_id]["modified"].strftime("%Y-%m-%d %H:%M:%S UTC")
                row_count = info[table_id]["rows"]
                marker = "STALE" if table_id in stale_tables else "OK"
                print(f"  {marker:5s}  {table_id:35s}  {row_count:>6,} rows  {mod}")
            else:
                print(f"  MISS   {table_id:35s}  —")

    if all_ok:
        print("OK  All pipeline tables are fresh and consistent.")
    else:
        # Suggest fix: find the earliest stale step number
        stale_step_nums = []
        for table_id, step, _ in PIPELINE_TABLES:
            if table_id in stale_tables or table_id not in info:
                try:
                    stale_step_nums.append(int(step.split()[1]))
                except (IndexError, ValueError):
                    pass
        if stale_step_nums:
            min_step = min(stale_step_nums)
            print(f"\nFix:  python scripts/orchestrate.py --from-step {min_step}")

    return all_ok


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify BQ pipeline table freshness")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show all timestamps")
    args = parser.parse_args()

    ok = check_freshness(verbose=args.verbose)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
