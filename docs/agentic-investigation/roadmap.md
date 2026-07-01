# Roadmap — Agentic Investigator

> **CURRENT POSITION (update this block every session before closing):**
>
> - **Date:** 2026-07-01
> - **Phase:** 1 — Harness / action space (in progress; tool #1 complete).
> - **Landed so far:** ADR-1 (agentic line + termination + budget), ADR-2
>   (tool-result contract: status enum, provenance envelope, deterministic-vs-model
>   grounding by claim type), ADR-3 (tool-binding standard: `strict:true`,
>   inputs-only/provenance-never-exposed, enums from canonical lists, prescriptive
>   description, generator = `claude-opus-4-8`).
> - **Tool #1 (`feature_history`) is complete end-to-end as a contract:** typed
>   input, 3-state output (found/feature_missing/period_not_filed), provenance,
>   `__post_init__` invariant, AND the Claude `tool_definition()`. Lives in
>   `src/qqq_scoring/investigator/tools/`. 8 contract tests pass (run:
>   `python3 tests/investigator/test_feature_history_contract.py`). BigQuery body
>   still stubbed (raises NotImplementedError) — deliberate.
> - **Two seams deferred to Phase 2:** (a) string→date parse adapter for
>   `report_date`; (b) `{tool_name → callable}` dispatch registry.
> - **Right now — pick one:** stub tool #2 (GCS narrative — the RAG/unstructured
>   one, where provenance = retrieved passages + model-mode grounding, to feel the
>   structured-vs-unstructured split), OR jump to Phase 2 (single-branch agent loop
>   end-to-end with just `feature_history` to prove the tool-use loop works).
> - **Next after that:** Phase 2 single-branch loop → Phase 3 disambiguation graph.

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
