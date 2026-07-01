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

---

## ADR-2: Tool-result contract standard (all Phase 1 tools)

**Date:** 2026-07-01
**Status:** Accepted

**Context:** Phase 1 defines the agent's action space as typed, isolated, testable
tools over the golden sources (BQ `period_features`, GCS narrative, FMP
fundamentals, scoring outputs). Before stubbing four tools we need one contract
standard they all obey — otherwise each tool invents its own shape and the judge
has to special-case every result. Derived by designing the first tool
(`feature_history`) end-to-end in a Socratic checkpoint; the principles that fell
out are reusable across all four.

**Decision — every tool result obeys three principles:**

1. **Complete but non-contradictory inputs.** Give a tool everything it needs and
   nothing it can contradict itself with. Concretely: no two input fields may
   encode the same fact (we dropped a `direction` field because a *signed*
   `period_offset` already encodes it — `offset=+1, direction="prior"` is an
   undefined state, and undefined states breed bugs).

2. **Make illegal states unrepresentable.** Model mutually-exclusive outcomes as a
   **status enum**, not a bag of boolean/nullable flags. N booleans encode 2^N
   combinations but the real world has only a few legal ones; every illegal cell
   is a latent bug. `feature_history` returns
   `status ∈ {found, feature_missing, period_not_filed}` — this protects ADR-1's
   three terminal states (a "not filed yet" period must not be mistaken for a
   `null`/`0` value, or the agent manufactures a false `refuted`/`failed` instead
   of a correct `inconclusive`).

3. **Every result carries provenance, verified by claim type.** A bare value is
   ungroundable — the judge (ADR-1) can't check "grounded in golden source only"
   without a thread to pull. So each result attaches provenance: the fields that
   let a skeptic independently re-reach the data (test: *if you delete the field,
   is the value still checkable?*). Verification is matched to the claim type:
   - **Structured data (BQ tables)** → provenance is a *re-runnable query* +
     source/row identity; grounding is checked **deterministically** (re-query,
     compare with `==`). No model call — using an LLM to verify `0.87 == 0.87` is
     wasteful and *weaker* than `==`.
   - **Unstructured data (GCS narrative prose)** → provenance is the *retrieved
     passage(s)* + source locator; grounding is checked by the **judge model**
     (semantic support), because there's no exact-match check for prose.

**Reference contract (`feature_history`, first tool):**
```
feature_history(ticker, report_date, period_offset: int, features: list[str])
  -> list[FeatureResult]
FeatureResult:
  feature, status{found|feature_missing|period_not_filed}, value|null,
  source, resolved_report_date, accession_number, query, retrieved_at
```
`resolved_report_date` is the provenance field that guards the calendar-math trap
— it exposes which quarter the tool *actually* landed on, so a claim of "next
quarter recovered" can be checked against the period the value truly belongs to.

**Alternatives considered:**
- *Per-tool ad hoc return shapes* — rejected. Forces the judge to special-case
  every tool; no uniform grounding check.
- *Boolean/nullable flags for outcomes* (`period_exists: bool, value: number|null`)
  — rejected in favour of a status enum. It works for 3 states but leaves an
  illegal quadrant (`exists=false, value=non-null`) representable; enums scale
  linearly and make the illegal state unspellable.
- *Hand the judge the whole reference set to re-scan ("RAG the table")* — rejected
  for structured data. Expensive, and it makes grounding itself fuzzy/gameable
  (the model can misread the table). Deterministic re-query is cheaper and
  unbeatable. NOTE: the RAG-style "give the judge the retrieved material" approach
  is *correct* for the unstructured narrative tool — structured vs unstructured
  provenance are deliberately different.

**Consequences:**
- All four Phase 1 tools implement the same `status`-enum + provenance envelope;
  the narrative tool differs only in provenance *content* (passages, not a query)
  and verification *mode* (model, not `==`).
- The judge gets a uniform grounding interface: structured results expose a
  re-runnable `query`; unstructured results expose cited passages.
- Next checkpoint (deferred): binding this Python contract to Claude's tool-use
  JSON-schema format — the model-facing schema is not the same artifact as the
  Python dataclass, and it's Anthropic-specific. → resolved in ADR-3.

---

## ADR-3: Tool-binding standard — Python contract → Claude tool-use schema

**Date:** 2026-07-01
**Status:** Accepted

**Context:** The action space (ADR-2) is defined as typed Python contracts, but
the agent picks tools at run-time through Claude's tool-use API — which consumes a
JSON-Schema tool definition, not a Python dataclass. The model-facing schema is a
*separate artifact* from the internal contract, and it's Anthropic-specific. We
need one binding standard all four tools follow so the model sees a consistent,
safe, minimal action space. Verified against the current Anthropic tool-use spec
(via the claude-api skill) rather than written from memory.

**Decision — every tool exposes a `tool_definition(...)` returning a Claude
tool-use dict, following four rules:**

1. **Inputs only — provenance is never exposed to the model.** The tool
   definition's `input_schema` describes *only what the agent chooses* (for
   `feature_history`: `ticker`, `report_date`, `period_offset`, `features`). The
   output envelope from ADR-2 (`status`, `value`, `source`, `resolved_report_date`,
   `query`, `retrieved_at`) is **absent** — it flows back separately as a
   `tool_result`, and is the judge's concern, not the model's. A test enforces the
   provenance fields cannot leak into the schema. This is the clean seam that lets
   the judge move to a different provider later without touching generator tools.

2. **`strict: true`.** Extends ADR-2 principle 2 ("make illegal states
   unrepresentable") from output to *input*: with `additionalProperties: false`
   and every property in `required`, the API guarantees `tool_use.input` validates
   exactly — a hallucinated/malformed argument cannot reach the BigQuery query.
   Cost: all fields must be required + `additionalProperties: false` (wanted
   anyway).

3. **Enums sourced from canonical lists, not hardcoded.** `tool_definition()`
   takes `feature_keys` as an argument and builds the `features` enum from it, so
   the model-facing allowlist stays in sync with `output/feature_keys.json` — one
   source of truth, not a drifting copy.

4. **Prescriptive `description` (the trigger, not just the "what").** The
   description states *when* to call the tool ("call this when you need to check
   whether a feature persisted/recovered… e.g. to resolve a persistence_test"),
   because Opus 4.8 reaches for tools conservatively — a description that only
   states what the tool does under-triggers.

**Model IDs:** generator = `claude-opus-4-8` (current default Opus tier). Judge
model deferred to the termination phase. Honest caveat recorded for that ADR:
ADR-1 wanted a *different-family* judge to decorrelate blind spots, but within
Anthropic every model shares a family — a true cross-family judge requires a
second provider. Do not pretend Sonnet-judging-Opus is cross-family.

**The tool-use loop this binds (executed in Phase 2, not yet built):**
```
send tools + messages → Claude returns tool_use{id, name, input}
  → parse input → run the Python callable → send tool_result{tool_use_id, content}
  → Claude reads it and chooses its next edge (ADR-1 path-variance)
```

**Alternatives considered:**
- *Expose the full contract (incl. provenance) to the model* — rejected. The model
  neither chooses nor should reason about `retrieved_at`/`query`; exposing them
  bloats the schema and blurs the generator/judge seam.
- *`strict: false`* — rejected. Lets malformed arguments through to a financial
  query; strict is nearly free given we want all-required anyway.
- *Hardcode the `features` enum* — rejected. Drifts from `feature_keys.json`.
- *Generate the schema from the Python dataclass automatically (Pydantic)* —
  deferred, not rejected. No Pydantic dependency yet; the hand-written
  `tool_definition()` is explicit and dependency-free. Revisit if tool count grows.

**Consequences:**
- Two seams remain for Phase 2: (a) a parse adapter (`report_date` arrives as a
  string, the callable takes a `date`); (b) a `{tool_name → callable}` dispatch
  registry so the loop routes any tool by name.
- All four tools implement `tool_definition(...)` the same way; the narrative tool
  differs only in its input shape, not in these four rules.
- The generator/judge provider split is now enforceable in code (the leak test),
  not just intended.
