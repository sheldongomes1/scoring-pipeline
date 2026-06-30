# Roadmap — Agentic Investigator

> **CURRENT POSITION (update this block every session before closing):**
>
> - **Date:** 2026-06-30
> - **Phase:** 1 — Harness / action space (entering).
> - **Just landed:** **ADR-1 accepted.** The line = ownership of the next edge
>   (path-variance conditioned on observations; fan-out is NOT it). Termination =
>   cross-family judge over a 3-axis rubric (C/G/O) with grounding as a
>   *precondition*, collapsing to: not-grounded→rerun(repair_cap); resolved→loop-or-stop;
>   grounded-unresolved→loop-or-stop(inconclusive). Three terminal states:
>   resolved / inconclusive / failed. Budget = two independent caps
>   (investigation_cap=5, repair_cap=2). `persistence_test` = deterministic
>   confirm/refute check against BQ. See ADR-1 in `decisions.md`.
> - **Right now:** Phase 1 — define the tool harness. The golden-source allowlist
>   from ADR-1 (BQ `period_features`, GCS narrative, FMP fundamentals, scoring
>   outputs) is the action space. Next checkpoint: typed tool I/O — what each tool
>   takes/returns, isolation, testability.
> - **Next after that:** Phase 2 — single-branch agent loop end-to-end.

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
