#!/usr/bin/env python3
"""DAG-aware pipeline orchestrator — runs independent steps in parallel.

Dependency graph:

  Step 1 ─► Step 2 ─► Step 4 ─► Step 5 ─┬─► Step 3 ─► Step 6 ─► Step 8
                                         │           └► Step 7
                                         └─► Step 9 (score_explanation, parallel with Step 3)

Parallel phases:
  Phase 1: Step 1 → Step 2           (sequential — each depends on the previous)
  Phase 2: Step 4                    (narrative divergence — depends on Step 2)
  Phase 3: Step 5                    (conviction scores — depends on Step 4)
  Phase 4: Step 3 ∥ Step 9           (parallel — both depend on Step 5)
  Phase 5: Step 6 ∥ Step 7           (parallel — both depend on Step 3)
  Phase 6: Step 8                    (depends on Step 6 — filing_intelligence must exist)

Note: Step 3 (analyst briefs) moved after Step 5 (conviction) so it can
filter by conviction tier — every ALERT/FLAG/WATCH filing gets an explanation.
This costs the parallelism of Steps 3+4 but ensures no tiered filing is missed.

Each step's stdout/stderr is written to logs/pipeline/step_N_TIMESTAMP.log
so parallel output doesn't interleave on the terminal.

Post-run freshness verification:
  After all steps succeed, the orchestrator queries BQ table metadata to
  verify that downstream tables are at least as recent as their upstream
  dependencies.  If any table is stale the run exits non-zero and prints
  a suggested --from-step fix.

Usage:
    # Full run
    python scripts/orchestrate.py

    # Skip steps already done — start from step N
    python scripts/orchestrate.py --from-step 3

    # Run specific steps only — cascades to downstream dependents by default
    python scripts/orchestrate.py --steps 3,4

    # Run specific steps WITHOUT cascading (expert: you know BQ is consistent)
    python scripts/orchestrate.py --steps 3,4 --no-cascade

    # Preview execution plan without running anything
    python scripts/orchestrate.py --dry-run

    # Only verify BQ freshness (no pipeline execution)
    python scripts/orchestrate.py --verify

    # Skip freshness check after pipeline run
    python scripts/orchestrate.py --skip-verify
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
        "args":       [],
        "depends_on": [5],                          # needs conviction tiers to filter
        "note":       "LLM brief per tiered filing (ALERT/FLAG/WATCH) → BQ anomaly_explanations",
    },
    {
        "num":        4,
        "name":       "Score narrative divergence → narrative_divergence",
        "script":     "explanations/score_narrative_divergence.py",
        "args":       [],
        "depends_on": [2],                          # runs after scoring, before conviction
        "note":       "MD&A vs numbers for all scored filings → BQ narrative_divergence",
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
        "depends_on": [3],                          # parallel with Step 7
        "note":       "BQ view + materialised review pack → top_anomaly_review_pack",
    },
    {
        "num":        7,
        "name":       "Build trend table → company_trend",
        "script":     "scripts/build_trend_table.py",
        "args":       [],
        "depends_on": [3],                          # parallel with Step 6
        "note":       "Per-ticker time-series for UI charts → BQ company_trend",
    },
    {
        "num":        8,
        "name":       "Generate analyst actions → analyst_actions",
        "script":     "explanations/generate_analyst_actions.py",
        "args":       [],
        "depends_on": [6],                          # needs filing_intelligence view
        "note":       "CFA-grade next_step / key_question / watch_signal per filing → BQ analyst_actions",
    },
    {
        "num":        9,
        "name":       "Compute score_explanation (UI transparency)",
        "script":     "explanations/compute_score_explanation.py",
        "args":       [],
        "depends_on": [5],                          # joins scores+conviction+divergence
        "note":       "Per-filing pillar+feature breakdown for the UI \"Explain the numbers\" modal → BQ score_explanation",
    },
]


# ── Status tracking ───────────────────────────────────────────────────────────

WAITING  = "WAITING"
RUNNING  = "RUNNING"
DONE     = "DONE   "
FAILED   = "FAILED "
SKIPPED  = "SKIPPED"


def _cascade_downstream(selected: set[int], steps: list[dict]) -> set[int]:
    """Expand selected steps to include all transitive downstream dependents.

    If step 2 is selected and step 3 depends on 2, step 3 is added.
    If step 6 depends on 3, step 6 is added too, etc.
    """
    # Build reverse adjacency: parent → children
    children: dict[int, list[int]] = {s["num"]: [] for s in steps}
    for s in steps:
        for dep in s["depends_on"]:
            children[dep].append(s["num"])

    result = set(selected)
    frontier = list(selected)
    while frontier:
        parent = frontier.pop()
        for child in children.get(parent, []):
            if child not in result:
                result.add(child)
                frontier.append(child)
    return result


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
        self._log("  Phase 2: Step 4")
        self._log("  Phase 3: Step 5")
        self._log("  Phase 4: Step 3 ∥ Step 9   (parallel — briefs + score_explanation)")
        self._log("  Phase 5: Step 6 ∥ Step 7   (parallel)")
        self._log("  Phase 6: Step 8")
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

    # ── Post-run verification ────────────────────────────────────────────────

    def verify_freshness(self) -> bool:
        """Run verify_freshness.py and return True if all tables are fresh."""
        self._log("── Verifying BQ table freshness ──")
        script = ROOT / "scripts" / "verify_freshness.py"
        result = subprocess.run(
            [sys.executable, str(script), "--verbose"],
            cwd=ROOT,
        )
        if result.returncode != 0:
            self._log("")
            self._log("FAIL  BQ freshness check failed — downstream tables may be stale.")
            self._log("      Run the suggested --from-step command above to fix.")
            return False
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
        help="Run these steps + all downstream dependents, e.g. --steps 2",
    )
    parser.add_argument(
        "--no-cascade", action="store_true",
        help="With --steps: run ONLY the listed steps, skip downstream dependents",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print execution plan without running anything",
    )
    parser.add_argument(
        "--verify", action="store_true",
        help="Only run BQ freshness verification (no pipeline execution)",
    )
    parser.add_argument(
        "--skip-verify", action="store_true",
        help="Skip post-run BQ freshness verification",
    )
    args = parser.parse_args()

    orchestrator = Orchestrator(STEPS, dry_run=args.dry_run)

    # --verify: only check freshness, don't run pipeline
    if args.verify:
        ok = orchestrator.verify_freshness()
        sys.exit(0 if ok else 1)

    only_steps = None
    if args.steps:
        only_steps = [int(s.strip()) for s in args.steps.split(",")]
        if not args.no_cascade:
            before = set(only_steps)
            only_steps = sorted(_cascade_downstream(before, STEPS))
            added = set(only_steps) - before
            if added:
                print(f"[cascade] --steps {','.join(str(s) for s in sorted(before))} "
                      f"expanded to include downstream: {','.join(str(s) for s in sorted(added))}")
                print(f"          Use --no-cascade to suppress this.\n")

    success = orchestrator.run(
        skip_before=args.from_step,
        only_steps=only_steps,
    )

    # Post-run freshness verification
    if success and not args.dry_run and not args.skip_verify:
        if not orchestrator.verify_freshness():
            sys.exit(1)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
