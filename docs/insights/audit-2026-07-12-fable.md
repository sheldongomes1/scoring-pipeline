# Adversarial audit — Agentic Investigator (2026-07-12)

Independent review by a **Fable-model** agent, run before committing to the
520-call Step-10 batch (the generator/judge-decorrelation principle, ADR-1, applied
to our own work). Verdict: do NOT run the batch until the SEV-1 + batch-robustness
items are fixed — not for cost (~$3–8) but correctness. This file tracks every
finding and its status so deferred items aren't lost.

Status legend: **FIXED** (ADR-13 / same-day) · **DEFERRED** (tracked, with rationale).

## SEV-1 — break the core value proposition

| # | Finding | Status |
|---|---------|--------|
| 1 | `PERIOD_NOT_FILED` probe re-grounds as `FOUND` against real BQ (reverify used `resolved+0`, landing on the anchor row) → authentic evidence fails grounding → ABANDONED. Fakes masked it. | **FIXED** — ADR-13: provenance carries the request; reverify replays `(requested_report_date, requested_offset)`. Regression test in `test_feature_history_bq.py`. |
| 2 | Grounding checked evidence↔source, not answer↔numbers. Answer-support ran only when narrative evidence existed → numbers-only investigations had zero answer-level grounding (fetch 0.95, write 0.55 → passes). | **FIXED** — ADR-13: answer-support head always runs (numbers + prose). Regression test in `test_judge.py`. |
| 3 | Repair could never fix a deterministic failure (`evidence` append-only) → guaranteed ABANDONED after burning `repair_cap`. | **FIXED** — ADR-13: integrity failures are unrepairable → immediate ABANDONED; only answer-support failures repair. Regression test in `test_judge.py`. |

## SEV-2 — will bite soon

| # | Finding | Status |
|---|---------|--------|
| 4 | 10-Q-only positional offset silently skips the fiscal Q4 (it's a 10-K) → "+1" from fiscal Q3 lands ~6 months later. A CFA would reject this. | **DEFERRED** — real methodology gap; fix = include 10-K periods in the ordered history or expose `periods_skipped`. Quality item for the interactive deep-dive, not the batch. |
| 5 | `expand()` is depth-first over a shared budget → first-child starvation; sibling order is arbitrary LLM emission; dropped children vanish silently. | **DEFERRED** — conceded (breadth-first is better). Fix = frontier expansion + record budget-capped children as PROPOSED. Interactive-service quality. |
| 6 | `CAP_REACHED` laundered into `INCONCLUSIVE`; the last verdict is dropped on that path. | **DEFERRED** — fix = distinct `BranchStatus`/`capped` flag + carry the verdict. Low risk. |
| 7 | Step 10 all-or-nothing: one API error at filing #500 loses 499. | **FIXED** — per-filing try/except + flush every 25 + incremental resume. (Message Batches API = future optimization, noted.) |
| 8 | Idempotency: `--ticker` skips incremental + `WRITE_APPEND` → duplicate rows; no `prompt_version`; zero-hypothesis filings silently retried. | **FIXED** (first two) — `--ticker` delete-then-append; `prompt_version` column. Zero-hypothesis: logged + retried (rare; **DEFERRED** enforcing ≥1 in the schema). |
| 9 | Sonnet proposer never A/B'd vs the Opus tier `graph.py` prescribes; batch context is thin. | **PARTIAL** — `--model` flag added for A/B. **ACTION BEFORE BATCH:** run `--limit 15` Sonnet vs Opus, hand-review distinctness / predicate-not-method / plausibility, freeze the prompt, then run 520. |

## SEV-3 — hygiene

| # | Finding | Status |
|---|---------|--------|
| 10 | Judge no-payload response fabricates `grounded=True` (wrong failure direction). | **DEFERRED** — low prob (`tool_choice` forced); make the fallback fail closed. |
| 11 | `InvestigationGraph.get` bare `next()` raises + searches only top-level (misses `h2.1`). | **DEFERRED** — recursive lookup + default. |
| 12 | Reverify cost O(evidence × evaluations); each bq call pulls full history; duplicate evidence re-verified. | **DEFERRED** — dedupe evidence by `(source, ticker, date, feature)`. |
| 13 | Stale pre-ADR-9 fixture source name in `test_judge.py`. | **FIXED** — updated to the logical source. |
| 14 | `load_flags` interpolates `--ticker` into SQL (inconsistent with parameterized queries). | **DEFERRED** — parameterize (low risk: not user-facing input). |
| 15 | No observability: no token/usage logging; batch transcripts not persisted. | **DEFERRED** — log per-call usage in Step 10 before the at-scale run for calibration. |

## What the audit affirmed as sound
Generator/judge receipt-withholding (ADR-5, test-enforced); status enums paying off
live; independent caps; propose-then-steer over investigate-all; method kept out of
the predicate; ADR-11's three-valued confirm; injectable hooks → 70 deterministic
tests. The lessons log was called "unusually honest."

## Meta
The audit found a SEV-1 the fakes hid — the exact risk of proving a mechanism on
fakes. Auditing before spend paid for itself. See `lessons.md` 2026-07-12.
