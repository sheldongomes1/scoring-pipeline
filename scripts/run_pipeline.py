#!/usr/bin/env python3
"""Full pipeline orchestrator — runs all steps in dependency order.

This is the single entry point for a complete pipeline re-run. Every major
pipeline step is registered here. When a new step is added to the pipeline,
it MUST be added to this script.

Step execution order (each step depends on the previous):

  Step 1  flatten_bq.py                    BQ filings → local period_features.json
  Step 2  score_quarterly_anomalies.py     Score anomalies → BQ quarterly_scores_detailed
  Step 3  generate_explanations.py         LLM analyst briefs → BQ anomaly_explanations
  Step 4  score_narrative_divergence.py    MD&A divergence → BQ narrative_divergence
  Step 5  compute_conviction.py            Three-pillar synthesis → BQ conviction_scores
  Step 6  build_master_output.py           BQ view + review pack → BQ top_anomaly_review_pack

Usage:
    # Full run (all steps)
    python scripts/run_pipeline.py

    # Start from a specific step (skip earlier steps)
    python scripts/run_pipeline.py --from-step 3

    # Run only specific steps
    python scripts/run_pipeline.py --steps 3,4,5

    # Dry run — print what would execute without running
    python scripts/run_pipeline.py --dry-run
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent

# ── Pipeline step registry ────────────────────────────────────────────────────
# IMPORTANT: When adding a new step to the pipeline, register it here.
# Order matters — each step may depend on the output of previous steps.

STEPS = [
    {
        "num":    1,
        "name":   "Flatten BQ → period_features",
        "script": "scripts/flatten_bq.py",
        "args":   ["--output-dir", "output"],
        "note":   "Reads qqq_anomaly.filings from BQ, writes output/period_features.json",
    },
    {
        "num":    2,
        "name":   "Score anomalies → quarterly_scores_detailed",
        "script": "scripts/score_quarterly_anomalies.py",
        "args":   [
            "--feature-keys", "output/feature_keys.json",
            "--period-features", "output/period_features.json",
            "--output-dir", "output",
            "--upload-bq",
        ],
        "note":   "Z-scores + Mahalanobis + Beneish → BQ quarterly_scores_detailed",
    },
    {
        "num":    3,
        "name":   "Generate LLM analyst briefs → anomaly_explanations",
        "script": "explanations/generate_explanations.py",
        "args":   ["--min-alert-score", "5"],
        "note":   "Pattern classification + 3-paragraph brief per anomalous filing → BQ anomaly_explanations",
    },
    {
        "num":    4,
        "name":   "Score narrative divergence → narrative_divergence",
        "script": "explanations/score_narrative_divergence.py",
        "args":   ["--min-alert-score", "5"],
        "note":   "MD&A vs. numbers: CONTRADICTS/CORROBORATES/NEUTRAL → BQ narrative_divergence",
    },
    {
        "num":    5,
        "name":   "Compute conviction scores → conviction_scores",
        "script": "explanations/compute_conviction.py",
        "args":   [],
        "note":   "Three-pillar synthesis (anomaly + Beneish + narrative) → BQ conviction_scores",
    },
    {
        "num":    6,
        "name":   "Build master output → filing_intelligence view + top_anomaly_review_pack",
        "script": "scripts/build_master_output.py",
        "args":   [],
        "note":   "BQ view joining all tables + materialised review pack → BQ top_anomaly_review_pack",
    },
    {
        "num":    7,
        "name":   "Build trend table → company_trend",
        "script": "scripts/build_trend_table.py",
        "args":   [],
        "note":   "Per-ticker time-series table for UI chart rendering → BQ company_trend (clustered by ticker)",
    },
]


# ── Runner ────────────────────────────────────────────────────────────────────

def run_step(step: dict, dry_run: bool) -> bool:
    """Execute a single pipeline step. Returns True on success."""
    script = ROOT / step["script"]
    cmd = [sys.executable, str(script)] + step["args"]
    label = f"Step {step['num']}: {step['name']}"

    print(f"\n{'='*70}")
    print(f"{label}")
    print(f"  {step['note']}")
    print(f"  Command: {' '.join(cmd)}")
    print(f"{'='*70}")

    if dry_run:
        print("  [DRY RUN — skipping]")
        return True

    start = time.time()
    result = subprocess.run(cmd, cwd=ROOT)
    elapsed = time.time() - start

    if result.returncode == 0:
        print(f"\n  ✓ {label} completed in {elapsed:.0f}s")
        return True
    else:
        print(f"\n  ✗ {label} FAILED (exit code {result.returncode}) after {elapsed:.0f}s")
        return False


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the full scoring pipeline")
    parser.add_argument("--from-step", type=int, default=1,
                        help="Start from this step number (default: 1 = beginning)")
    parser.add_argument("--steps", default=None,
                        help="Comma-separated step numbers to run, e.g. --steps 3,4,5")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print steps without executing")
    args = parser.parse_args()

    # Determine which steps to run
    if args.steps:
        selected = set(int(s.strip()) for s in args.steps.split(","))
        steps_to_run = [s for s in STEPS if s["num"] in selected]
    else:
        steps_to_run = [s for s in STEPS if s["num"] >= args.from_step]

    if not steps_to_run:
        print("No steps selected.")
        return

    print(f"\nPipeline: {len(steps_to_run)} step(s) to run")
    for s in steps_to_run:
        print(f"  Step {s['num']}: {s['name']}")

    # Execute
    failed_at = None
    pipeline_start = time.time()

    for step in steps_to_run:
        success = run_step(step, dry_run=args.dry_run)
        if not success:
            failed_at = step["num"]
            break

    elapsed_total = time.time() - pipeline_start
    print(f"\n{'='*70}")
    if failed_at:
        print(f"Pipeline FAILED at step {failed_at}. "
              f"Fix the error and re-run with --from-step {failed_at}")
    else:
        if args.dry_run:
            print("Dry run complete — no steps were executed.")
        else:
            print(f"Pipeline complete in {elapsed_total:.0f}s.")
    print(f"{'='*70}\n")


if __name__ == "__main__":
    main()
