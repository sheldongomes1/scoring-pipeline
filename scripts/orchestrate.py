#!/usr/bin/env python3
"""DAG-aware pipeline orchestrator — runs independent steps in parallel.

Dependency graph:

  Step 1 ──► Step 2 ──► Step 3 ──────────────────────────────────────► Step 6
                    └──► Step 4 ──► Step 5 ────────────────────────────► Step 7

Parallel phases:
  Phase 1: Step 1 → Step 2           (sequential — each depends on the previous)
  Phase 2: Step 3 ∥ Step 4           (parallel — both depend only on Step 2)
  Phase 3: Step 5                    (starts as soon as Step 4 done, not Step 3)
  Phase 4: Step 6 ∥ Step 7           (parallel — both depend on Steps 3 + 5)

Wall-clock savings vs sequential:
  - Steps 3 + 4 run simultaneously  (~3 min saved on LLM batch calls)
  - Steps 6 + 7 run simultaneously  (~30s saved on BQ writes)

Each step's stdout/stderr is written to logs/pipeline/step_N_TIMESTAMP.log
so parallel output doesn't interleave on the terminal.

Usage:
    # Full run
    python scripts/orchestrate.py

    # Skip steps already done — start from step N
    python scripts/orchestrate.py --from-step 3

    # Run specific steps only (dependencies must already be satisfied in BQ)
    python scripts/orchestrate.py --steps 3,4

    # Preview execution plan without running anything
    python scripts/orchestrate.py --dry-run
"""

import argparse
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

ROOT    = Path(__file__).parent.parent
LOG_DIR = ROOT / "logs" / "pipeline"

# ── Step registry ─────────────────────────────────────────────────────────────
# depends_on: direct dependencies only (transitive deps resolved automatically)
# IMPORTANT: When adding a new pipeline step, register it here.

STEPS = [
    {
        "num":        1,
        "name":       "Flatten BQ → period_features",
        "script":     "scripts/flatten_bq.py",
        "args":       ["--output-dir", "output"],
        "depends_on": [],
        "note":       "qqq_anomaly.filings → output/period_features.json",
    },
    {
        "num":        2,
        "name":       "Score anomalies → quarterly_scores_detailed",
        "script":     "scripts/score_quarterly_anomalies.py",
        "args":       ["--feature-keys", "output/feature_keys.json",
                       "--period-features", "output/period_features.json",
                       "--output-dir", "output", "--upload-bq"],
        "depends_on": [1],
        "note":       "Z-scores + Mahalanobis + Beneish → BQ quarterly_scores_detailed",
    },
    {
        "num":        3,
        "name":       "Generate analyst briefs → anomaly_explanations",
        "script":     "explanations/generate_explanations.py",
        "args":       ["--min-alert-score", "5"],
        "depends_on": [2],
        "note":       "LLM pattern + 3-para brief per filing → BQ anomaly_explanations",
    },
    {
        "num":        4,
        "name":       "Score narrative divergence → narrative_divergence",
        "script":     "explanations/score_narrative_divergence.py",
        "args":       ["--min-alert-score", "5"],
        "depends_on": [2],                          # parallel with Step 3
        "note":       "MD&A vs numbers → BQ narrative_divergence",
    },
    {
        "num":        5,
        "name":       "Compute conviction scores → conviction_scores",
        "script":     "explanations/compute_conviction.py",
        "args":       [],
        "depends_on": [4],                          # needs narrative, not briefs
        "note":       "Three-pillar synthesis → BQ conviction_scores",
    },
    {
        "num":        6,
        "name":       "Build master output → filing_intelligence + review pack",
        "script":     "scripts/build_master_output.py",
        "args":       [],
        "depends_on": [3, 5],                       # parallel with Step 7
        "note":       "BQ view + materialised review pack → top_anomaly_review_pack",
    },
    {
        "num":        7,
        "name":       "Build trend table → company_trend",
        "script":     "scripts/build_trend_table.py",
        "args":       [],
        "depends_on": [3, 5],                       # parallel with Step 6
        "note":       "Per-ticker time-series for UI charts → BQ company_trend",
    },
]


# ── Status tracking ───────────────────────────────────────────────────────────

WAITING  = "WAITING"
RUNNING  = "RUNNING"
DONE     = "DONE   "
FAILED   = "FAILED "
SKIPPED  = "SKIPPED"


class Orchestrator:
    def __init__(self, steps: list[dict], dry_run: bool = False):
        self.steps       = {s["num"]: s for s in steps}
        self.dry_run     = dry_run
        self.status      = {n: WAITING for n in self.steps}
        self.start_times = {}
        self.end_times   = {}
        # Each step gets a threading.Event that fires when the step completes
        # (regardless of success/failure — dependents check self.any_failed)
        self.done_events = {n: threading.Event() for n in self.steps}
        self.any_failed  = threading.Event()
        self.lock        = threading.Lock()
        self.run_ts      = datetime.now().strftime("%Y%m%d_%H%M%S")
        LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ── Logging ───────────────────────────────────────────────────────────────

    def _ts(self) -> str:
        return datetime.now().strftime("%H:%M:%S")

    def _log(self, msg: str) -> None:
        print(f"[{self._ts()}] {msg}", flush=True)

    def _step_label(self, num: int) -> str:
        return f"Step {num}  {self.steps[num]['name']}"

    # ── Execution ─────────────────────────────────────────────────────────────

    def _run_step(self, step: dict) -> None:
        num  = step["num"]
        deps = step["depends_on"]

        # Wait for every dependency to complete
        for dep in deps:
            self.done_events[dep].wait()

        # If any upstream step failed, skip this one
        if self.any_failed.is_set():
            with self.lock:
                self.status[num] = SKIPPED
            self._log(f"  SKIP   {self._step_label(num)} — upstream failure")
            self.done_events[num].set()
            return

        # Mark running
        with self.lock:
            self.status[num]      = RUNNING
            self.start_times[num] = time.time()
        self._log(f"  START  {self._step_label(num)}")
        self._log(f"         {step['note']}")

        if self.dry_run:
            time.sleep(0.05)
            with self.lock:
                self.status[num]    = DONE
                self.end_times[num] = time.time()
            self._log(f"  DONE   {self._step_label(num)}  [dry run]")
            self.done_events[num].set()
            return

        # Run the subprocess, capturing output to a log file
        script   = ROOT / step["script"]
        cmd      = [sys.executable, str(script)] + step["args"]
        log_path = LOG_DIR / f"step_{num:02d}_{self.run_ts}.log"

        with open(log_path, "w") as log_f:
            result = subprocess.run(
                cmd,
                cwd=ROOT,
                stdout=log_f,
                stderr=subprocess.STDOUT,
            )

        elapsed = time.time() - self.start_times[num]

        if result.returncode == 0:
            with self.lock:
                self.status[num]    = DONE
                self.end_times[num] = time.time()
            self._log(f"  DONE   {self._step_label(num)}  ({elapsed:.0f}s)  log→ {log_path.name}")
        else:
            with self.lock:
                self.status[num]    = FAILED
                self.end_times[num] = time.time()
            self.any_failed.set()
            self._log(f"  FAIL   {self._step_label(num)}  ({elapsed:.0f}s)  log→ {log_path.name}")
            self._log(f"         See {log_path} for details")

        self.done_events[num].set()

    # ── Entry point ───────────────────────────────────────────────────────────

    def run(self, skip_before: int = 1, only_steps: list[int] | None = None) -> bool:
        """Run the pipeline. Returns True if all selected steps succeeded."""

        # Steps to execute
        if only_steps:
            selected = set(only_steps)
        else:
            selected = {n for n in self.steps if n >= skip_before}

        # Pre-mark skipped steps as done so their dependents aren't blocked
        for num in self.steps:
            if num not in selected:
                self.status[num] = DONE
                self.done_events[num].set()

        # Print plan
        self._log(f"Pipeline — {len(selected)} step(s) selected")
        for num, step in self.steps.items():
            marker = "►" if num in selected else "·"
            self._log(f"  {marker} Step {num}: {step['name']}")
        self._log(f"")

        # Show dependency diagram
        self._log("Execution order (parallel where shown on same line):")
        self._log("  Phase 1: Step 1 → Step 2")
        self._log("  Phase 2: Step 3 ∥ Step 4   (parallel)")
        self._log("  Phase 3: Step 5")
        self._log("  Phase 4: Step 6 ∥ Step 7   (parallel)")
        self._log("")

        # Launch all selected steps as threads — each waits on its own deps
        t_start = time.time()
        threads = []
        for num in selected:
            t = threading.Thread(
                target=self._run_step,
                args=(self.steps[num],),
                name=f"step-{num}",
                daemon=True,
            )
            threads.append(t)
            t.start()

        for t in threads:
            t.join()

        # Summary
        elapsed_total = time.time() - t_start
        self._log("")
        self._log(f"Pipeline finished in {elapsed_total:.0f}s")
        for num, step in self.steps.items():
            if num in selected:
                dur = ""
                if num in self.start_times and num in self.end_times:
                    dur = f"  ({self.end_times[num] - self.start_times[num]:.0f}s)"
                self._log(f"  {self.status[num]}  Step {num}: {step['name']}{dur}")

        failed = [n for n in selected if self.status[n] == FAILED]
        if failed:
            self._log(f"\nFailed steps: {failed}")
            self._log(f"Re-run with: --from-step {min(failed)}")
            return False

        self._log("\nAll steps completed successfully.")
        return True


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="DAG orchestrator — runs independent pipeline steps in parallel"
    )
    parser.add_argument(
        "--from-step", type=int, default=1,
        help="Skip steps before N and start from step N (default: 1 = full run)",
    )
    parser.add_argument(
        "--steps", default=None,
        help="Run only these steps, e.g. --steps 3,4  (BQ deps must already exist)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print execution plan without running anything",
    )
    args = parser.parse_args()

    only_steps = None
    if args.steps:
        only_steps = [int(s.strip()) for s in args.steps.split(",")]

    orchestrator = Orchestrator(STEPS, dry_run=args.dry_run)
    success = orchestrator.run(
        skip_before=args.from_step,
        only_steps=only_steps,
    )
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
