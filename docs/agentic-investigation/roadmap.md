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
> - **Right now — the JUDGE (ADR-1 termination).** This is the next checkpoint,
>   NOT auto-build: it's the second model integration + the grounding gate
>   (deterministic re-query for structured tools) + repair loop. Open questions to
>   resolve before code: judge I/O contract; per-iteration vs at-termination;
>   how the deterministic grounding re-query reads against the (currently fake)
>   backend; judge model choice (Sonnet — same-family caveat from ADR-3 stands).
> - **Next after that:** tool #2 (GCS narrative — unstructured provenance) →
>   Phase 3 disambiguation graph.

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
