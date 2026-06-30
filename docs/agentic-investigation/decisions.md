# Decisions Log — Agentic Investigator (ADRs)

Michael Nygard ADR format. One entry per resolved Socratic checkpoint. An open
checkpoint is logged as **PENDING** so we resume exactly where we left off.

---

## ADR-1: What makes the investigation loop agentic (not a workflow)

**Date:** 2026-06-30
**Status:** Accepted

**Context:** Goal #2 is a *truly agentic* AI process, explicitly contrasted with a
workflow. `scripts/orchestrate.py` is already a fixed DAG (author-time control
flow). To build the new thing correctly we must first define the property that
separates an agent from that workflow, the agent's action space, and how the loop
terminates. Without a real action space and a stopping condition, "agentic" is
just an LLM call with extra steps.

**Decision (three parts):**

**1. The line — ownership of the next edge.** In `orchestrate.py` the engineer
fixes the edges at author-time; in the investigator the model picks the next
action at run-time from observed intermediate results.
- *Trace-observable test:* run both systems on two different filings. The
  workflow's step sequence is identical (only the data differs); the agent's
  tool-call sequence *varies as a function of what it observed*. **Path-variance
  conditioned on observations** is the discriminator.
- Fan-out is explicitly **not** the discriminator — workflows fan out too
  (`orchestrate.py` runs Step 6 ∥ Step 7). Fan-out belongs to the
  disambiguation-graph layer (a product feature), not the agentic-loop layer (an
  execution property). These are deliberately kept separate per the mission
  (agentic loop = load-bearing wall; graph = facade).

**2. Termination — cross-family judge over a 3-axis rubric.** A *separate* judge,
ideally a different model family, grades each agent output on three axes:
- **C** — does the output confirm/refute the predicate?
- **G** — is the output grounded in golden-source data *only*?
- **O** — do open questions remain?

Grounding (**G**) is a **precondition, not a co-equal axis**: if G fails, C and O
were produced by the same ungrounded reasoning and are untrustworthy, so they
cannot steer the decision. Resulting policy (collapses the 8-row truth table):

```
if not grounded:             → rerun (repair), up to repair_cap, then abandon
else if resolved:            → open_questions ? loop : stop(resolved)
else (grounded, unresolved): → open_questions ? loop : stop(inconclusive)
```

Three terminal states, not two: **resolved**, **inconclusive** (clean evidence,
genuinely ambiguous — a *useful* outcome, surfaced as such in the graph), and
**failed/abandoned**. The confirm/refute axis is checked *deterministically* where
possible via the existing `persistence_test` against BigQuery actuals (no model
call, not gameable) — the pipeline already wrote the exit predicate.

**3. Budget — two independent caps.** `investigation_cap` bounds evidence-gathering
loops; `repair_cap` bounds grounding reruns *separately* (repair is output-quality
retry, not evidence-gathering — conflating them hides a second explosion vector).
Proposed starting values: `investigation_cap = 5`, `repair_cap = 2`, unit =
iterations (one iteration = generate → tool calls → judge). The *structure* (two
separate ceilings) is the decision; the numbers are tunable parameters.

**Action space (third original facet) — deferred to Phase 1.** Golden sources are
sketched (BigQuery `period_features`, GCS narrative, FMP fundamentals, scoring
outputs); the golden-source allowlist *is* the action space. Typed tool I/O is
specified in Phase 1, which ADR-1 hands the allowlist.

**Alternatives considered:**
- *Agent self-judging the exit predicate* — rejected. Same model that wants to
  continue decides whether it's done; errors are correlated, so it rubber-stamps
  its own hallucinations or never converges. Cross-family judge decorrelates blind
  spots, most valuable on the grounding axis. (Honest cost: doubles operational
  surface — second key, format, failure mode, latency — accepted as worth it.)
- *Single "done?" bit* — rejected. Un-evalable. A 3-axis rubric is.
- *Shared budget for repair + investigation* — rejected. Hides a second
  budget-explosion vector behind the first.
- *Grounding as a co-equal axis* — rejected. Steelman acknowledged (real outputs
  aren't cleanly binary; a mostly-grounded output can carry usable C/O signal),
  but routing on C/O when G has failed means routing on signals just declared
  untrustworthy, and "loop on ungrounded output" burns the whole budget stacking
  garbage on garbage. Grounding-as-precondition fails fast and cheap instead.
- *Fan-out as the agentic discriminator* — rejected. Workflows fan out too; it
  fails as a trace-observable test.

**Consequences:**
- Commits us to a **generator/judge split** — two model integrations, cross-family
  operational cost accepted for the grounding-decorrelation win.
- The **golden-source allowlist defines Phase 1's tool action space** — the judge's
  "grounded in golden source only" check and the agent's available tools are the
  same set.
- Three terminal states (not two) flow downstream: the disambiguation graph must
  render `inconclusive` as a first-class, useful branch outcome.
- Deterministic `persistence_test` checks tie the agent back into the existing
  pipeline rather than inventing a new judge.
