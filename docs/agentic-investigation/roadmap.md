# Roadmap — Agentic Investigator

> **CURRENT POSITION (update this block every session before closing):**
>
> - **Date:** 2026-07-05
> - **Phase:** 2 — Single-branch agent loop (in progress; harness proven, live run next).
> - **Landed so far:** ADR-1 (agentic line + termination + budget), ADR-2
>   (tool-result contract), ADR-3 (tool-binding standard), ADR-4 (build
>   sequencing: loop-first over breadth-first, fake backends because the
>   loop-closing property is a function of the round-trip), ADR-5 (generator sees
>   the answer, judge gets the receipts — result-visibility split).
> - **Phase-2 harness is complete and proven.** The tool-use loop CLOSES: five
>   artifacts landed —
>   - `src/qqq_scoring/investigator/loop.py` — `run_investigation(client, ...)`,
>     client-agnostic, sync, single branch. Terminates on `end_turn` OR
>     `investigation_cap` (thin; judge deferred).
>   - `src/qqq_scoring/investigator/registry.py` — `ToolBinding` + `ToolRegistry`
>     (the `{name → callable}` dispatch seam; generic).
>   - `src/qqq_scoring/investigator/tools/feature_history_fake.py` — in-memory data
>     backend (AAPL story: OCF/NI dips to 0.55 at 2025-06-30, recovers to 0.95 at
>     +1 quarter; includes a PERIOD_NOT_FILED and a FEATURE_MISSING).
>   - `feature_history.py` — added the two ADR-3 seams: `parse_model_input`
>     (str→date) and `to_model_content` (generator-facing serializer).
>   - `tests/investigator/test_loop_closes.py` — 7 tests, all green (run:
>     `python3 tests/investigator/test_loop_closes.py`). Scripted fake client, no
>     key. Proves closure, both termination paths, the parse seam, and the ADR-5
>     provenance non-leak.
> - **Live proofs both landed.** `scripts/investigate_demo.py` — model chose the
>   tool, grounded verdict (single case). `scripts/investigate_variance.py` — the
>   RIGOROUS ADR-1 proof: same code, two filings (AAPL recovers / WBD stays
>   broken), tool-call *sequences diverged* (AAPL = single-feature time-series
>   sweep; WBD = multi-feature corroboration sweep). Path-variance conditioned on
>   observations is demonstrated, not just asserted. Second fixture ticker (WBD)
>   added to `feature_history_fake.py`; `_FILED_THROUGH` moved to 2025-12-31.
> - **JUDGE landed & proven live (ADR-6).** `src/qqq_scoring/investigator/judge.py`
>   — `Judge` owns termination; `end_turn` is a proposal it adjudicates. G is
>   deterministic re-fetch + `==` (never trusts in-process evidence); C/O use a
>   strict `submit_judgment` tool on `claude-sonnet-5`; G gates first. Loop
>   rewritten: `judge`/`predicate`/`repair_cap` params, TerminalReason gains
>   RESOLVED/INCONCLUSIVE/ABANDONED (MODEL_STOPPED preserved for judge=None).
>   Caps are independent (investigation_cap = total-turn ceiling; repair_cap = its
>   own ABANDONED sub-ceiling — do NOT sum them). Tests: 24 green across
>   contract(8)/loop(7)/judge(9). Live proof: `scripts/investigate_judged.py WBD`
>   → Opus proposed "no recovery", Sonnet grounded all 9 evidence items, returned
>   RESOLVED, correctly treated feature_missing as a limitation not an open question.
> - **TOOL #2 (GCS narrative) landed & proven live (ADR-7).** Sibling result type
>   `NarrativeResult` (prose has no numeric value → separate type, illegal states
>   unspellable). Grounding is now TWO-HEADED, dispatched by a `grounding_mode` tag
>   each evidence item carries ("tell, don't ask"): `==` re-fetch for structured,
>   a judge-model semantic support-check for prose. Retrieval is section-addressed
>   (dict lookup), RAG-swappable behind the same contract (corpus fits in context;
>   RAG deferred until cross-filing/huge-section need). Registry refactored to
>   self-contained bindings (each carries its own built schema; the old shared
>   `feature_keys` was a hidden structured-only assumption the 2nd tool exposed).
>   New files: `tools/narrative_sections.py` + `_fake.py`; `contracts.py` gains
>   `GroundingMode`/`NarrativeStatus`/`NarrativeProvenance`/`NarrativeResult`.
>   Tests: 38 green (contract 8 / loop 7 / judge 9 / narrative 14). Live proof:
>   `scripts/investigate_narrative.py WBD` — Opus used BOTH tools, judge grounded a
>   mixed pile (9 numeric by ==, 1 passage by model), RESOLVED "said matches
>   showed" (candid disclosure). This is backlog #1 "Said vs Showed" as an agent.
> - **Phase 2 is functionally COMPLETE:** agentic loop + judge + one structured
>   tool + one unstructured tool, all proven live, 38 tests. Real BQ/GCS bodies
>   still stubbed (deliberate — fakes prove the mechanism).
> - **PHASE 3 (disambiguation graph) landed & proven live (ADR-8).** Propose-then-
>   steer fan-out: a cheap `propose_branches` call names N competing hypotheses (no
>   investigation budget); the human steers into one; `run_branch` runs the EXISTING
>   loop with the branch's `predicate` as the only new input. New `graph.py`
>   (`Flag`, `Branch`, `InvestigationGraph`, `propose_branches`, `run_branch`,
>   `BranchStatus`). The load-bearing field is `predicate` (the question), NEVER a
>   metric list (that would collapse the branch into a workflow, killing ADR-1
>   agency). 42 tests green (contract 8 / loop 7 / judge 9 / narrative 14 / graph 4).
>   Live: `scripts/investigate_fanout.py h2` — WBD flag fanned into 4
>   analyst-grade hypotheses; steering into working-capital ran a focused deep-dive;
>   the grounding judge forced the agent to RETRACT an unsupported inference and
>   return an honest "cannot confirm with available tools" instead of hallucinating.
> - **Product signal from the live run:** the toolset has no balance-sheet line
>   items (receivables/payables/content assets), which capped the working-capital
>   and revenue-recognition branches. The investigation surfaced its own next tool.
> - **Open calibration question (note for later):** the judge returned RESOLVED for
>   a branch whose answer was "hypothesis rejected / not fully testable" — defensible
>   (the branch question got a grounded answer with a clear lean) but the
>   resolved/inconclusive boundary is worth tuning as more branches run.
> - **TOOL #3 (balance-sheet line items) landed & proven live (ADR-9).** Closes the
>   gap the ADR-8 run exposed. REUSES `FeatureResult` (a line item is a number,
>   grounded deterministically like a ratio — data difference, not behaviour, so
>   ADR-7's rule says reuse). New `tools/balance_sheet.py` + `_fake.py`; feature
>   fake `_SOURCE`→public `SOURCE` (clean logical table name). The honest catch:
>   "zero judge change" (ADR-7) was optimistic by ONE thing — the judge's single
>   `reverify` was a hidden one-backend assumption (same shape as the registry's
>   `feature_keys`), so grounding now routes by `provenance.source`
>   (`reverify: callable | {source: callable}`). Loop/registry/generator unchanged.
>   50 tests green (…/ balance-sheet 8). Live: `investigate_fanout.py h2` now
>   RESOLVES the working-capital branch on real line items — receivables/payables a
>   small cash source, hypothesis rejected, content amortization identified as driver.
> - **The self-healing loop:** agent hit a wall → wall named the missing tool → we
>   built it → identical branch resolved. Logged as a lesson (portfolio gold).
> - **PHASE 4 (recursive expansion) landed & PROVEN LIVE (ADR-10); 55 tests green.**
>   `investigate_tree.py h3` grew a tree 2 levels / 4 nodes autonomously off one
>   human steer (h3 → h3.1 → {h3.1.1, h3.1.2}); node budget (4) and max_depth (2)
>   both bit exactly; the grounding repair loop fired at EVERY node. (Steering into
>   an INCONCLUSIVE root (h1) correctly spawned nothing — the leaf rule, live.)
>   A RESOLVED branch's finding spawns deeper CHILD branches — the graph
>   grows into a tree. `Branch` gained `children` + `depth`; new `propose_children`
>   (seeded follow-up proposer), `ExpansionBudget` (global node cap), and `expand`
>   (recursive orchestrator with injectable `_investigate`/`_propose` so the control
>   logic is unit-testable with NO LLM). Termination = ADR-1 lifted a level: semantic
>   stop (proposer returns no new questions) + TWO independent hard caps (`max_depth`,
>   `max_total_branches` — not summed). Only RESOLVED spawns (grounding-as-precondition
>   up a level; ABANDONED/INCONCLUSIVE = leaves). Auto-expands within caps (human
>   steered once at the root). New `scripts/investigate_tree.py`. Tests: graph 4→9
>   (5 new control-logic tests). Fakes stay ("mock data but real").
> - **(a) DONE — ADR-11 calibration fix:** three-valued `Confirm`
>   (CONFIRMED/REFUTED/INDETERMINATE); refuted → RESOLVED. 56→ tests.
> - **(b) DONE — ADR-12 real BigQuery backend:** `tools/feature_history_bq.py`,
>   positional period resolution across irregular fiscal calendars, verified live
>   against `qqq_finance.period_features` (AAPL) + 6 unit tests. 62 tests total.
>   Fake→real swap = one-line binding change; SOURCE key unchanged so grounding
>   re-queries real BQ.
> - **(c) IN PROGRESS — Phase 5 prod surface.** Cut resolved (batch/interactive split):
>   BATCH `propose_branches` per flagged filing = new **Step 10**
>   (`explanations/propose_investigation_branches.py`) → BQ `investigation_branches`;
>   interactive steer + deep-dive = a request-time redink-ui service (UI half = a
>   separate, larger effort, not built). Step 10 registered in orchestrate.py; dry-run
>   shows 520 flagged filings.
> - **FABLE AUDIT (2026-07-12) — ran before the 520-call batch; found real bugs.**
>   Full tracker: `docs/insights/audit-2026-07-12-fable.md`.
>   - **FIXED (ADR-13):** SEV-1 PERIOD_NOT_FILED re-grounds as FOUND vs real BQ (fakes
>     masked it); grounding checked evidence↔source not answer↔numbers (answer-support
>     head now always-on); repair couldn't fix deterministic failures (now unrepairable
>     → immediate ABANDONED). Provenance gained `requested_report_date`+`requested_offset`
>     (reverify replays the request). 70 tests (3 new regressions).
>   - **FIXED (batch hardening):** per-filing try/except + flush-every-25 + resume;
>     `--ticker` idempotent; `prompt_version` col; `--model` A/B flag.
>   - **DEFERRED (tracked):** #4 fiscal-Q4/10-K skip, #5 breadth-first expand, #6
>     CAP→INCONCLUSIVE, #10–12/#14–15 hygiene — none block the batch; #4–6 are
>     interactive-service quality.
> - **GATE before the 520-call batch (audit #9):** run
>   `propose_investigation_branches.py --limit 15` with Sonnet AND Opus, hand-review
>   hypothesis distinctness / predicate-not-method / plausibility, freeze the prompt,
>   THEN run the full 520 (`--full-refresh` after any prompt change). Cost ~$3–8; the
>   issue is proposal quality on the thin batch context, not cost.
> - **After (c):** Phase 6 eval in qqq-eval-suite. Interview deferred until live in prod.

---

## Phases (provisional — will firm up as ADRs land)

- [ ] **Phase 0 — Design & framing** *(in progress)*
  - [ ] ADR-1: agentic loop definition (action space / termination / the line)
  - [ ] ADR-2: batch vs. interactive boundary (where the human checkpoint sits)
  - [ ] ADR-3: what a "branch" is (data model for a node in the graph)

- [ ] **Phase 1 — Harness / action space**
  - Define and stub the tools the agent can call (BigQuery feature history, GCS
    narrative sections, FMP fundamentals, scoring outputs). Each tool = typed
    input/output, isolated, testable.

- [ ] **Phase 2 — Single-branch agent loop**
  - One investigation end-to-end (CLI/batch): take one flagged filing +
    its `key_question`, let the model pick tools, gather evidence, terminate,
    return a grounded answer. This is the minimum "truly agentic" proof.

- [ ] **Phase 3 — Disambiguation graph**
  - Agent proposes N branches (hypotheses) for a flag instead of one path.
    Render NotebookLM-style. User points at a branch.

- [ ] **Phase 4 — Loop / expansion**
  - Chosen branch runs autonomously; results may spawn child branches. Define
    depth/breadth limits and convergence.

- [ ] **Phase 5 — Integration**
  - Decide: orchestrate.py step (pre-compute level-1) + interactive service in
    `redink-ui`. Wire per CLAUDE.md orchestrator rule.

- [ ] **Phase 6 — Eval**
  - Quality gates in `qqq-eval-suite`: is the agent's evidence grounded? Does it
    answer the key_question? Cost/latency per investigation.

---

## Log discipline

- `decisions.md` — append an ADR the moment a checkpoint resolves.
- `lessons.md` — append the moment a lesson/rework/surprise surfaces (not at end).
- This file — update the CURRENT POSITION block before any session ends.
