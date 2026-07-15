# Decisions Log (ADRs)

Canonical architecture decision records. See `README.md` for the entry format an
external parser expects. Each entry begins `## ADR-N: <decision name>` followed by
Context / Decision / Consequence sections. Michael Nygard ADR format.

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

---

## ADR-4: Build sequencing — loop-first (walking skeleton) over breadth-first

**Date:** 2026-07-05
**Status:** Accepted

**Context:** Phase 1 left one tool (`feature_history`) complete as a contract but
with a stubbed body. Two forward paths: (a) stub tool #2 (GCS narrative, the
unstructured one) to widen the action space, or (b) jump to Phase 2 and wire the
single-branch agent loop end-to-end with the one tool we have. Only one core
mechanism — the tool-use loop — is currently unproven, and nothing downstream
works until it does.

**Decision — two coupled sub-decisions:**

**1. Loop-first, not breadth-first.** Build the end-to-end loop with the single
existing tool before adding a second tool. A second tool that conforms to a
contract we have never run against a live loop is *speculative generality* — if
the loop run reveals the contract is wrong, breadth-first means reworking two
tools instead of one. A foundation crack found *after* the loop works is cheap to
patch (add a tool that fits the proven loop); a loop that never closes makes every
tool worthless. Walking-skeleton dominates when the core mechanism is unproven.

**2. Fake backends, because the property is a function of the round-trip.** The
loop-closing property is a function of the tool-call round-trip (`tool_use →
dispatch → tool_result → next edge`), NOT of where the data came from or which
model answered. So we inject two fakes: an in-memory `feature_history_fake` (no
BigQuery) and a scripted fake Anthropic client (no key). This buys two distinct
proofs from one harness: the scripted client proves the *harness* closes
(deterministic, testable), and a later live `claude-opus-4-8` run proves the
*agentic property* (real path-variance, ADR-1). Swapping either fake for the real
thing touches zero lines of loop logic — that is the seam working.

**Scope of this slice (what is deliberately deferred):** termination is thin —
stop on the model's `end_turn` OR the `investigation_cap` budget (ADR-1). The
ADR-1 judge (grounding/resolved/inconclusive) is NOT built here: the judge grades
output *quality*, it does not *close* the loop, so it is a later slice. Real
BigQuery and tool #2 are deferred, not cancelled.

**Alternatives considered:**
- *Breadth-first (stub tool #2 next)* — rejected. Its one real merit is that the
  narrative tool is the only thing exercising ADR-2's unstructured-provenance half
  (currently 100% theory). But that crack is cheap to patch post-loop; an unclosed
  loop is not. Steelman logged, call stands.
- *Real BigQuery in the loop slice* — rejected. Tangles a live dependency,
  credentials, latency, and a query to debug together with the loop logic under
  test. The fake isolates the variable.
- *Real model call in the unit test* — rejected for the *harness* proof
  (non-deterministic, costs money, needs a key). Retained as a separate demo for
  the *agentic-property* proof.

**Consequences:**
- Phase 2 lands as five artifacts: `loop.py`, `registry.py`,
  `tools/feature_history_fake.py`, the two adapters added to `feature_history.py`
  (`parse_model_input`, `to_model_content`), and `tests/investigator/test_loop_closes.py`
  (7 tests). The two ADR-3 seams (parse adapter, dispatch registry) are now built.
- The next slice is the live demo (`scripts/investigate_demo.py`) — same loop,
  real client — to prove path-variance, then the judge, then tool #2.
- Registry is generic (`ToolBinding` carries all tool-specific functions), so
  adding tool #2 = adding one binding; loop and registry stay untouched.

---

## ADR-5: Generator-facing vs judge-facing result visibility

**Date:** 2026-07-05
**Status:** Accepted

**Context:** ADR-3 rule 1 established that provenance is never exposed to the model
*on input* (the tool definition describes only what the agent chooses). When the
loop runs, the tool's *output* comes back as a `tool_result` — and the result
envelope (ADR-2) carries both reasoning-relevant fields and provenance receipts.
We must decide what subset of that envelope the model (generator) sees, versus
what is retained for the judge.

**Decision — give the generator the answer, give the judge the receipts.** The
`tool_result` handed back to the model carries only what it needs to pick its next
edge: `feature`, `status`, `value`, and `resolved_report_date` (it must know
*which* quarter it landed on). The receipts — `query`, `retrieved_at`,
`accession_number`, `source` — are withheld from the model and retained in
`TerminalResult.evidence` for the judge. This extends ADR-3 rule 1 from input to
output: provenance is the judge's concern end-to-end, never the generator's.

Note the split is *not* "provenance vs not." `resolved_report_date` is technically
a provenance field but is also reasoning-relevant (the model needs to know it
asked for +1 and landed on 2025-09-30), so it crosses to the generator. The real
axis is: **what does the generator need to choose its next edge** vs **what does
the judge need to verify grounding.**

**Why it matters (the failure it prevents):** if the model can see the receipt
(`query: "SELECT ocf_to_net_income ..."`), it can parrot the receipt into its
answer and *look* grounded without the value being real — turning the judge's
grounding check into theater (this is exactly the `lessons.md` warning: "check
provenance first or you're scoring hallucinations"). By withholding the receipt,
the only way the answer can match the paper trail is if the model genuinely used
the real value. It also keeps the provider seam clean (ADR-3): the judge can move
to a different provider without the generator's tools touching provenance.

**Alternatives considered:**
- *Send the whole envelope back to the model* — rejected. Lets the model launder
  receipts into its answer, defeating the grounding check, and bloats context with
  fields it never reasons on.
- *Withhold `resolved_report_date` too (strict "no provenance to model")* —
  rejected. The model then can't tell which quarter a value belongs to and would
  do date arithmetic itself (a reliability leak the tool exists to prevent).

**Consequences:**
- `registry.dispatch` returns a `DispatchOutcome(model_content, raw_results)` — the
  two halves kept structurally separate, not merely by convention.
- Every tool supplies its own `serialize` (generator-facing) function via its
  `ToolBinding`; provenance retention is uniform in the loop (`evidence.extend`).
- Enforced by `test_model_never_sees_provenance_receipts` +
  `test_full_provenance_retained_for_judge`.

---

## ADR-6: The judge — termination authority, cadence, and grounding mechanism

**Date:** 2026-07-05
**Status:** Accepted

**Context:** ADR-1 designed the judge's rubric (3 axes C/G/O, grounding as a
precondition, three terminal states, two budget caps) but wrote its policy as a
*controller* (`open_questions ? loop : stop`). The Phase-2 loop we actually built
lets the *model* own termination (it stops on `end_turn`). Those two were never
reconciled: with both able to say "keep going," they can oscillate or duplicate
control. Building the judge forces the reconciliation, its run cadence, and what
"grounded" concretely checks against a backend that is currently a fake.

**Decision — three parts:**

**1. The judge owns termination; `end_turn` becomes a proposal.** Once a judge is
attached, the model's `end_turn` is no longer terminal — it is a *proposal* ("I
think I'm done"). The loop hands the proposed answer to the judge, which
adjudicates: accept → stop, or reject → re-inject guidance and continue. One
authority, no fight: the model may propose stopping as often as it likes; only the
judge's verdict stops the loop. (The judge is *injected* — `judge=None` preserves
the ADR-4 harness behaviour: stop on `end_turn`, terminal state `MODEL_STOPPED`.
The judge is a pluggable termination policy, like the client and the data backend.)

**2. Cadence — per proposed conclusion, not per iteration.** The judge fires only
when the model *proposes a conclusion* (stops emitting `tool_use`), NOT on every
loop iteration. Rationale: most iterations end in a `tool_use` (the model is
mid-gathering), where there is no candidate answer to grade — the C and O axes
presuppose a claim, so grading a tool-gathering step is incoherent, not merely
expensive. Judge calls ≈ number of times the model thinks it's done (1–2),
bounded by the caps. (Rejected: a cheap per-iteration guard rail — real value,
but that is a *supervisor*, a separate component with a different job; folding it
into the termination judge is scope creep. Scoped out, not adopted.)

**3. Grounding = deterministic re-provenance, never trust in-process state.** G is
**code, not a model call** (ADR-2: don't use an LLM to verify `0.87 == 0.87`).
For every retained evidence item, the judge *independently re-fetches from the
golden source* using the item's provenance (ticker + `resolved_report_date` +
feature) and compares `(status, value)` with `==`. It does NOT trust the
`evidence` dict handed forward — "we are not giving it our memory state." Against
the fake backend today the re-fetch hits the fake; when BigQuery lands the same
re-fetch hits BQ — mechanism identical, backend swaps (the injected
`reverify` callable). Only C (does the evidence resolve the predicate?) and O
(open questions remain?) use the judge *model*, and only *after* G passes.

**Policy (ADR-1, collapsed), evaluated per proposed conclusion:**
```
not grounded          → repair (own repair_cap) then ABANDONED
grounded + resolved + no open_q   → STOP(RESOLVED)
grounded + open_q (either C)      → re-inject, continue (investigation ceiling)
grounded + unresolved + no open_q → STOP(INCONCLUSIVE)
```

**Judge model:** `claude-sonnet-5` (cheaper tier than the Opus generator; different
tier, partial blind-spot decorrelation). The ADR-3 honest caveat stands: same
Anthropic family ≠ true cross-family; a real cross-family judge needs a second
provider. Not pretended.

**Alternatives considered:**
- *Model owns termination (judge is advisory)* — rejected. Reintroduces the
  fight and lets an ungrounded/incomplete answer end the investigation.
- *Judge every iteration* — rejected (part 2): incoherent on tool-gathering steps.
- *Grounding by re-running the `query` SQL string* — deferred. Works only against
  BQ; our backend is the fake. The re-dispatch-by-identity path (ticker +
  resolved date + feature) is backend-agnostic and works today. `query` stays as
  the human/BQ-facing handle.
- *Model-graded grounding* — rejected. ADR-1/ADR-2: structured grounding is
  deterministic and un-gameable; a model grading its sibling's grounding
  re-introduces correlated blind spots.

**Consequences:**
- `Provenance` gains a `ticker` field — needed for backend-agnostic deterministic
  re-fetch (parsing the SQL `query` string would be worse). Envelope change ripples
  to the fake backend and the contract-test fixture.
- `run_investigation` gains `judge`, `predicate`, and `repair_cap` params; the
  `TerminalReason` enum gains `RESOLVED`, `INCONCLUSIVE`, `ABANDONED` alongside the
  no-judge `MODEL_STOPPED` and the ceiling `CAP_REACHED`.
- New `judge.py`: `Judge` (deterministic G via injected `reverify` + model C/O via
  a strict `submit_judgment` tool) and `JudgeVerdict`. The judge is the first
  consumer of the receipts ADR-5 withheld from the generator.
- Two model integrations now live (generator Opus + judge Sonnet).

---

## ADR-7: The narrative (unstructured) tool — sibling result type, two-headed grounding, section-addressed retrieval

**Date:** 2026-07-11
**Status:** Accepted

**Context:** Tool #2 reads the *written* sections of a filing (MD&A, risk factors)
and returns **prose**, not numbers. This is the first test of ADR-2's claim that
the tool-result contract generalizes to unstructured data — 100% theory until now.
Three things break the comfortable numeric world the harness was built in: the
result has no numeric `value`; grounding cannot use `==`; and the tool must
*locate* a passage rather than look up a row. Decided in a Socratic checkpoint.

**Decision — three parts:**

**1. A sibling result type, not a generalized envelope.** `NarrativeResult` is a
distinct type from `FeatureResult`, not `FeatureResult` with `value → passage`.
The deciding principle: *when two kinds of evidence differ only in their data, one
type with a tag suffices; when they differ in their **behavior**, give each its
own type.* A number and a passage are **verified differently** (below), so they
are different types. Bonus: separate types make the illegal state unspellable
(ADR-2 principle 2) — a `NarrativeResult` cannot hold a float. This **refines
ADR-2, does not overturn it**: the envelope generalizes as an *interface*
(`status` + `provenance` + a self-declared grounding mode) and forks as *concrete
types*. Shape:
```
NarrativeResult: section, status{found|section_absent|filing_not_found},
                 passage|null, provenance(NarrativeProvenance), grounding_mode=SEMANTIC
NarrativeProvenance: source(GCS path), ticker, report_date, form, section, retrieved_at
```

**2. Two-headed grounding, dispatched by a self-declared mode.** The judge's G
axis, pure deterministic code until now, becomes mode-dependent. Each evidence
type carries an explicit `grounding_mode` (`DETERMINISTIC` | `SEMANTIC`) — the
evidence *tells* the judge how to verify it ("tell, don't ask"); the judge does
not `isinstance`-branch. So a third tool (FMP) declares its mode and the judge is
untouched. The two heads:
- `DETERMINISTIC` (structured): re-fetch by identity, compare `==` (ADR-6).
- `SEMANTIC` (narrative): a judge-**model** call — "do the cited passages support
  the claims the investigator's answer makes about the narrative?" Catches the
  real unstructured failure: the model asserting "management admitted weakness"
  when the passage says "we remain confident." Passages are returned verbatim by
  the tool, so authenticity is by construction; the semantic check guards
  *faithful characterization*, which has no `==`.
G stays a **precondition**: overall grounded = (structured items re-verify) AND
(narrative items semantically support the answer). Either head failing → repair.

**3. Section-addressed retrieval now; RAG swappable behind the same contract.** The
tool returns whole *named sections* by label (`narrative["mdna"]`), a dict lookup —
NOT embedding search. Rationale: the corpus for one investigation is a single
filing's narrative (a few thousand tokens, already section-split by ingestion) —
it fits in context, so you hand over the haystack rather than build a needle-finder.
Real RAG's costs (an embedding dependency, a chunking strategy with its own bugs,
a vector store to run and keep in sync with GCS, re-embedding on restatement) buy
nothing for a corpus that fits in a prompt, and section provenance ("the MD&A of
AAPL 2025-Q2") is a *cleaner* grounding locator than "chunk 47 @ cosine 0.83". The
tool *contract* (input = filing identity + section names; output = `NarrativeResult`)
is identical whether retrieval is a dict lookup today or vector search later — same
seam as fake-backend-now / real-BQ-later. RAG is built WHEN the need arrives
(cross-filing search, genuinely huge sections), not speculatively.

**Alternatives considered:**
- *Cram prose into `FeatureResult` (`value → passage`)* — rejected: forcing
  behavior-divergent evidence into one type leaves a float-in-passage illegal
  state spellable; the shared thing is an interface, not a concrete type.
- *Model-graded grounding for everything / deterministic for everything* —
  rejected: you cannot `==` a paragraph, and using a model to check `0.87==0.87`
  is wasteful and weaker (ADR-2). Grounding must be two-headed.
- *Real embedding RAG now* — rejected as premature: needle-finder for a haystack
  that fits in hand; four standing costs for zero current benefit; fuzzier
  provenance. Correct once the corpus outgrows context — build it then.

**Consequences:**
- `contracts.py` gains `GroundingMode`, `NarrativeStatus`, `NarrativeProvenance`,
  `NarrativeResult`; `FeatureResult` gains `grounding_mode = DETERMINISTIC`.
- `judge.py`'s `_check_grounding` splits by `grounding_mode`; a new semantic head
  makes a judge-model call over the narrative passages + the answer.
- New `tools/narrative_sections.py` (contract + `tool_definition` + adapters, GCS
  body stubbed) and `tools/narrative_sections_fake.py` (section lookup).
- The loop's `evidence` list is now heterogeneous; no loop change needed — dispatch
  and serialization are already per-`ToolBinding` (ADR-4/5).
- ADR-2's "envelope generalizes" is refined to "generalizes as an interface,
  forks as concrete types."

---

## ADR-8: The disambiguation graph — propose-then-steer fan-out, and what a branch is

**Date:** 2026-07-11
**Status:** Accepted

**Context:** Phase 3 turns a single flagged filing into a *graph* of competing
hypotheses the user can steer (the mission's NotebookLM disambiguation surface).
The flag is ambiguous — a collapse in OCF/NI could be benign working-capital
timing, revenue-quality rot, structural margin pressure, or a one-off. The
single-branch loop (Phase 2) silently commits to one of these, burying the choice
in how it phrases its first tool call — exactly the "hidden assumption in the
prompt" the disambiguation thesis (Isaac Flath) says to expose. Two questions had
to be settled before code: the *shape* of the fan-out, and the *data model* of a
branch.

**Decision — two parts:**

**1. Propose-then-steer, not investigate-all.** The fan-out is a *steering surface*,
not a parallelism optimization. On a flag, a CHEAP model call NAMES N hypotheses
(one line each + a plausibility rationale) — no tool calls, no investigation
budget. The graph is rendered; the human points at the branch worth pursuing; only
THEN does the full `run_investigation` loop run, on the chosen branch alone, going
deep. Rejected: *investigate-all* (run the loop on all N in parallel, then present
findings). It doesn't just cost N× — it *defeats the purpose*: presenting N finished
mini-investigations is a fait accompli, not a steering moment. It spends budget to
*remove* the human's chance to correct the assumption, which is the one thing the
graph exists to create. (A)'s only real merit — that bare hypotheses leave the human
steering blind — is answered by the `rationale` field below, not by investigating.

**2. A branch carries the QUESTION, never the method.** A `Branch` is:
`id`, `hypothesis` (the one-line causal story), `rationale` (why it's plausible —
the field that lets the human steer with sight, not blind), **`predicate`** (the
steering wire), `status`, and — once run — its `TerminalResult`. The load-bearing
field is `predicate`: the reframed question the branch hands to
`run_investigation` (e.g. H2 → "Is the depressed cash conversion driven by
deteriorating revenue quality — receivables outrunning revenue, aggressive
recognition?"). Picking a branch swaps the generic predicate for the branch's, and
the agent — at RUN-TIME — decides that answering it means fetching receivables,
accrual ratio, DSO, etc. **The branch must NOT carry the tools/metrics to fetch:**
hardcoding the method turns the branch into a fixed recipe and destroys the
run-time tool-choice (ADR-1 path-variance) that Phase 2 exists to guarantee. Name
the question; the agent still chooses the tools. (Rejected: a branch carrying an
explicit evidence/tool checklist — it collapses the focused investigation back into
a workflow.)

**Consequences:**
- New `graph.py`: `Branch` + `InvestigationGraph` data models and
  `propose_branches(client, flag, n)` — a strict-tool model call emitting the N
  hypotheses. This is the fan-out; it is a PRODUCT layer over the loop (mission:
  graph = facade, loop = load-bearing wall), consistent with ADR-1 keeping fan-out
  out of the agentic-loop definition.
- A chosen branch runs the EXISTING loop unchanged — its `predicate` is the only
  new input. The loop needs no Phase-3 changes.
- Branch `status` reuses the loop's terminal states
  (resolved/inconclusive/abandoned) once investigated; `proposed` is the new
  pre-investigation state.
- Sets up Phase 4: an investigated branch's `TerminalResult` may spawn child
  branches (the graph grows); depth/breadth limits deferred to that phase.
- Interactivity (human picks a branch) is request-time — the first non-batch piece
  (mission), so orchestrate.py wiring is deferred; the batch pre-compute of level-1
  proposals vs. a `redink-ui` service is an open Phase-5 question.

---

## ADR-9: Tool #3 (balance-sheet line items) — reuse FeatureResult, and source-routed grounding

**Date:** 2026-07-11
**Status:** Accepted

**Context:** The live disambiguation run (ADR-8) hit a wall: the working-capital
and revenue-quality branches for WBD could not be resolved because the toolset had
no receivables / payables / content-asset figures — the agent's own reasoning
surfaced the missing tool. Tool #3 returns those raw balance-sheet line items
(dollar amounts). Two decisions: does it get its own result type, and does the
"add a tool, zero judge change" claim (ADR-7) hold?

**Decision — two parts:**

**1. Reuse `FeatureResult`, no new type.** A balance-sheet line item is a number
verified the SAME way as an engineered ratio: re-fetch from the golden source,
compare `==`. Identical DETERMINISTIC grounding *behaviour*. By ADR-7's own rule
(behaviour difference → new type; data difference → reuse), dollars-vs-ratios is a
*data* difference, so tool #3 reuses the structured contract — `FeatureResult`
with the `feature` field holding the line-item key. A `LineItemResult` would create
a type for exactly the reason ADR-7 forbids. (If `feature`-naming ever becomes a
real problem, generalize to `StructuredResult`/`key` — a rename, deferred.)

**2. Source-routed grounding — "zero judge change" was optimistic by one thing.**
Reusing `FeatureResult` exposed a hidden assumption: the judge's deterministic
grounding re-fetched through a SINGLE injected `reverify` callable — and that
callable was the `feature_history` backend. A balance-sheet `FeatureResult`
re-verified against the feature backend finds nothing (wrong source). So the lone
`reverify` was a hidden one-backend assumption — the *same shape* as the registry's
`feature_keys` (ADR-7), now the second occurrence of that pattern. Fix: `reverify`
becomes `callable | {source: callable}`; the deterministic head routes each item to
its backend by `provenance.source`. A single callable remains the one-backend
shorthand (all prior sites unchanged); a map is REQUIRED once >1 structured tool is
in play. This refines ADR-7: adding a tool of an *existing grounding mode* needs no
new grounding *logic*, but a new structured *backend* needs a routing *entry*
(config/data, not code) — the loop, registry, and generator are still zero-change.

**Alternatives considered:**
- *New `LineItemResult` type* — rejected (part 1): a type for a data difference,
  which ADR-7's rule exists to prevent.
- *Keep the lone `reverify`, ignore source* — rejected: silently re-verifies
  balance-sheet values against the feature backend, which finds nothing — a false
  ungrounded, or worse a false pass if keys collided. `test_routing_to_the_wrong_backend_is_caught`
  guards this.
- *Re-verify by re-dispatching through the registry (by tool name)* — deferred, not
  rejected: more elegant (one routing concept for calls and re-checks) but needs
  evidence→tool-name linkage the envelope doesn't carry yet. Source-map is the
  simpler correct fix now.

**Consequences:**
- New `tools/balance_sheet.py` (contract stub + schema + adapters; reuses
  `feature_history.to_model_content`) and `tools/balance_sheet_fake.py`.
- `feature_history_fake._SOURCE` → public `SOURCE`, cleaned to the logical table
  name (`qqq_finance.period_features`, no "fixture" suffix) so it is a stable
  reverify-map key across fake→real backend swaps.
- `Judge.__init__` accepts `reverify: callable | {source: callable}`; new
  `_backend_for(item)` routes by `provenance.source`; unresolved source →
  ungrounded (not a silent pass).
- 50 tests green (contract 8 / loop 7 / judge 9 / narrative 14 / graph 4 /
  balance-sheet 8). Live: `investigate_fanout.py h2` now RESOLVES the working-capital
  branch on real line items — the gap the prior run exposed is closed.

---

## ADR-10: Phase 4 — recursive expansion, and graph-level termination

**Date:** 2026-07-12
**Status:** Accepted

**Context:** A resolved branch's finding can raise a NEW, deeper question ("content
amortization is the driver" → "is the amortization schedule aggressive vs peers?"),
which warrants its own investigation — a child branch. The graph grows into a tree.
Unbounded, this recurses forever, and growth is EXPONENTIAL (breadth^depth), each
node a full LLM investigation. Phase 4 needs a graph-level termination condition —
the same problem ADR-1 solved for the single loop, one level up.

**Decision — four parts:**

**1. Termination = semantic stop + independent hard backstop (ADR-1, lifted a
level).** Relying only on the semantic "no new questions" signal is the exact bet
ADR-1 rejected ("trust the model's done-bit") — a chain can plausibly emit "one
more question" forever. So, mirroring ADR-1's two independent caps:
- *Semantic stop:* a resolved branch is offered to a follow-up proposer; if it
  returns no genuinely-new questions → leaf.
- *Hard backstop:* `max_depth` (levels) AND `max_total_branches` (global node
  budget), each an independent ceiling — NOT summed (the cap-independence lesson).
  Because growth is exponential, the hard cap is even more load-bearing here than
  at the loop level.

**2. Only a RESOLVED branch spawns — grounding-as-precondition, one level up.** You
do not build a deeper investigation on a foundation you couldn't ground.
`ABANDONED` (grounding failed) → **leaf always** (its finding is untrustworthy;
children would stack garbage on garbage — ADR-1's rejected "loop on ungrounded
output"). `INCONCLUSIVE` → **leaf** for v1 (no solid finding to extend; "reframe
and try a differently-angled child" is a defensible later refinement). Only
`RESOLVED` earns a follow-up proposal.

**3. Children come from a fresh follow-up proposer, NOT the loop's `open_questions`
flag.** A branch that terminates RESOLVED has, by ADR-6's rule, NO within-loop open
questions (that is the termination condition). The graph-level question is
different: "what NEW, deeper line of inquiry does this *resolved finding* raise?"
So children are proposed by a separate `propose_children` call seeded with the
parent's finding + verdict — reusing the ADR-8 proposer, allowed to return empty
(empty = leaf, the semantic stop).

**4. Auto-expand within the caps; the human steered once, at the root.** Per the
mission ("chosen branch runs autonomously; results may spawn child branches"), the
chosen subtree expands autonomously, bounded by the hard caps — the human's steering
happened once in Phase 3 (picking the root). Propose-and-steer at *every* level is
a defensible UX (keeps a human in each fork) but is deferred; the hard caps are the
safety that makes autonomous expansion acceptable.

**Alternatives considered:**
- *Semantic stop only (no hard cap)* — rejected: ADR-1's rejected done-bit, worse
  because growth is exponential.
- *Spawn from any terminal state* — rejected: extending ABANDONED/INCONCLUSIVE
  branches builds on ungrounded/absent findings (violates grounding-as-precondition).
- *Reuse the loop's `open_questions` to decide spawning* — rejected: a RESOLVED
  branch has none by construction; spawning needs a NEW-question signal, not an
  unfinished-within-loop one.
- *Steer at every level* — deferred, not rejected (a Phase-5 UX choice).

**Consequences:**
- `Branch` gains `children: list[Branch]` and `depth: int`; the graph is now a tree.
- New `propose_children` (seeded proposer), `ExpansionBudget` (node counter), and
  `expand` (recursive orchestrator) in `graph.py`. `expand` takes injectable
  `_investigate` / `_propose` hooks so the tree-growth CONTROL logic (caps,
  which-status-spawns) is unit-testable without any LLM.
- New `scripts/investigate_tree.py` — steer into a root, then autonomously expand
  it within tight caps, print the grown tree. Mock-but-real (fakes stay).
- Caps are config, not code — tuning breadth/depth/budget never touches logic.

---

## ADR-11: Three-valued confirm axis — 'refuted' is a resolution, not ambiguity

**Date:** 2026-07-12
**Status:** Accepted

**Context:** Two live runs graded a REJECTED hypothesis inconsistently: the
fan-out's working-capital rejection → `RESOLVED`, the tree's impairment rejection →
`INCONCLUSIVE`. Same *kind* of answer ("this hypothesis is refuted"), different
terminal state — and it now changes the graph's shape, because only `RESOLVED`
branches spawn children (ADR-10). Root cause: the judge's `Confirm` axis was
two-valued (`RESOLVED` / `UNRESOLVED`), which collapsed ADR-1's original
*confirm-vs-refute* distinction. The model had nowhere clean to put "refuted," so
it drifted — sometimes to RESOLVED, sometimes to UNRESOLVED.

**Decision:** Restore ADR-1's axis with THREE values: `Confirm =
{CONFIRMED, REFUTED, INDETERMINATE}`. A predicate is a question; **both** a definite
yes (CONFIRMED) and a definite no (REFUTED) *answer* it, so both map to the
`RESOLVED` terminal state. Only genuine INDETERMINATE (evidence can't decide) maps
to `INCONCLUSIVE`. The judgment tool's `confirm` description now instructs
explicitly: "a hypothesis you REJECTED on the evidence is 'refuted', NOT
'indeterminate'." Loop routing checks open-questions first, then
`confirm ∈ {CONFIRMED, REFUTED} → RESOLVED`, else `INCONCLUSIVE`.

**Alternatives considered:**
- *Keep two values, clarify the prompt* — rejected: the enum itself lacked a slot
  for "refuted", so no prompt wording reliably prevents the drift; the fix must be
  structural (a value to put it in).
- *Sub-type RESOLVED into resolved-yes/resolved-no downstream* — deferred: not
  needed yet; the graph only needs the RESOLVED/INCONCLUSIVE distinction to decide
  spawning. The confirm/refute detail is preserved on the verdict for later use
  (e.g. rendering "hypothesis confirmed" vs "ruled out" in the UI).

**Consequences:**
- `judge.py`: `Confirm` enum (3 values), the `submit_judgment` schema description,
  and the no-verdict fallback (`INDETERMINATE`) updated.
- `loop.py`: routing maps CONFIRMED/REFUTED → RESOLVED, INDETERMINATE → INCONCLUSIVE.
- Tests updated to the new enum; new `test_refuted_hypothesis_resolves_not_inconclusive`
  guards the exact bug. 56 tests green.
- Downstream: a "ruled out" branch is now consistently RESOLVED, so a refutation can
  legitimately spawn deeper children (e.g. "if not working capital, then what?") —
  the graph shape stops depending on a coin-flip.

---

## ADR-12: Real BigQuery backend for feature_history — positional period resolution

**Date:** 2026-07-12
**Status:** Accepted

**Context:** Making one tool production-real (`feature_history` → BigQuery
`qqq_finance.period_features`). The one genuinely-new piece the fake hand-waved:
resolving `report_date + period_offset` to an actual quarter against REAL filings.

**Decision — positional offset, not calendar.** `period_offset` counts FILED
periods in the ticker's own history (index into its ordered `target_period_end`
list), NOT calendar months. Real fiscal calendars are irregular: AAPL files 3
10-Qs/year on shifting dates (Mar/Jun/Dec, no September 10-Q), so "+1 quarter" =
"the next FILED 10-Q," resolved by index. Calendar arithmetic (`+3 months`) would
land on a quarter that doesn't exist for that company and silently break every
non-December fiscal year. This is also what an analyst means by "next quarter's
filing." One query pulls the ticker's ordered history + values; resolution happens
in Python; the value is read off the resolved row.

**Alternatives considered:**
- *Calendar arithmetic (`report_date + 3·offset months`)* — rejected: breaks on
  irregular fiscal calendars and filing gaps; lands on non-existent quarters.
- *Two queries (periods, then values)* — rejected: one query returns both; resolve
  in Python.

**Consequences:**
- New `tools/feature_history_bq.py` — drop-in for the fake: same signature, same
  `FeatureResult`, same `SOURCE` key (`qqq_finance.period_features`), so the
  fake→real swap is a ONE-LINE binding change and the judge's reverify-map entry is
  unchanged (the ADR-9 source-cleanup paying off). Grounding re-queries real BQ.
- Parameterized query + a `_quote_ident` guard on feature names (defense in depth,
  though the enum already constrains the model).
- Verified live (AAPL: −1/0/+1 resolve to 2025-03-29 / 06-28 / 12-27; +99 →
  PERIOD_NOT_FILED; null net_margin → FEATURE_MISSING). Resolution logic unit-tested
  with an injected fake client (6 tests, no creds needed). 62 tests total.
- Real BQ body still lives beside the `NotImplementedError` stub in
  `feature_history.py` (the contract reference); the callable is injected, so both
  coexist — swap per environment.

---

## ADR-13: Grounding hardening — replay-the-request, always-on answer support, unrepairable integrity

**Date:** 2026-07-12
**Status:** Accepted (revises ADR-2, ADR-6)

**Context:** An independent adversarial audit (a Fable-model reviewer, run before
committing to a costly batch) found three real defects in the grounding gate — the
mechanism the whole product's "won't hallucinate" promise rests on. Two were
masked by the fakes and only appear against real data / numbers-only runs. This
ADR fixes all three and honestly corrects an oversold claim in ADR-2/ADR-6.

**The three defects:**
1. **SEV-1 — `PERIOD_NOT_FILED` re-grounds as `FOUND` against real BigQuery.** For
   an offset past a ticker's filed history (the canonical "did it recover next
   quarter?" move), `feature_history_bq` set `resolved_report_date = report_date`
   (the anchor). The judge re-verified at `(resolved_report_date, offset=0)` →
   landed on the anchor row → `FOUND` ≠ `PERIOD_NOT_FILED` → grounding failed on
   AUTHENTIC evidence → branch ABANDONED. The fake masked it (its calendar extends
   past its filed window).
2. **The gate checked evidence↔source, not answer↔numbers.** The deterministic head
   re-fetched the evidence the loop recorded (authenticity) but never checked that
   the figures the generator WROTE match the evidence. The answer↔evidence check
   ran only when narrative evidence happened to exist, so numbers-only
   investigations (the majority) had zero answer-level grounding — a generator that
   fetched 0.95 and wrote "0.55" passed.
3. **Repair could never fix a deterministic failure.** `evidence` is append-only, so
   a failing item stays forever; every repair re-fails on it → guaranteed
   `ABANDONED` after burning the whole `repair_cap`.

**Decision — three fixes:**
1. **Provenance carries the REQUEST; reverify replays it.** `Provenance` gains
   `requested_report_date` + `requested_offset` (the anchor + signed offset the
   agent asked for). The integrity head re-fetches `backend(ticker,
   requested_report_date, requested_offset, [feature])` — reproducing the EXACT
   original probe — so re-grounding is correct for FOUND, FEATURE_MISSING, AND
   PERIOD_NOT_FILED alike. `resolved_report_date` remains a human/model-facing
   output, NOT the reverify key.
2. **Answer-support head, always on.** A model call (the generalized former
   "semantic" head) now runs on EVERY evaluation with an answer + evidence, over
   the full mixed evidence view: "does every factual claim in the answer — every
   figure (allowing correct arithmetic) and every characterization — follow from
   this evidence?" This is the real anti-hallucination gate; it catches a fabricated
   number in a numbers-only answer. Skipped only when the integrity head already
   failed (evidence untrustworthy; also preserves ADR-1's "no model call on
   ungrounded output").
3. **Split repairable vs unrepairable.** `_check_grounding` returns a
   `deterministic_failure` flag; `JudgeVerdict` carries it. An INTEGRITY failure
   (re-fetch mismatch / missing backend = source drift or config) is unrepairable →
   the loop terminates ABANDONED immediately. An ANSWER-SUPPORT failure (the
   generator mis-stated a figure/passage) is repairable → the repair loop, as before.

**Honest correction to ADR-2/ADR-6:** their "deterministic re-fetch is un-gameable
grounding" framing conflated evidence *authenticity* (which `==` does verify, and
which in-process is nearly tautological) with answer *groundedness* (which needs a
model and was not implemented for numbers). Grounding is now explicitly TWO things:
authenticity (deterministic `==`) AND answer-support (model). The "un-gameable"
claim applies only to the first.

**AMENDMENT (2026-07-13, Fable health check — re-scoping the guarantee).** ADR-13's
"won't hallucinate" holds ONLY for the `RESOLVED` and `INCONCLUSIVE` terminals —
the two reached *after* grounding passes. It does NOT hold for `CAPPED` or
`ABANDONED`: the first real eval showed grounding *detected* the failures correctly
(the answer-support head rejected the STX hallucination) but the loop's `CAP_REACHED`
exit returned the *rejected* answer as the result anyway, and at `investigation_cap=5`
the repair loop was unreachable (first proposal landed on the last turn), so every
real investigation terminated CAPPED — the one path with no guarantee. Fixes: (1)
`TerminalResult.trusted` (True only for RESOLVED/INCONCLUSIVE) is now the enforcement
seam — the DTO, UI, and eval key off it and never present an untrusted answer as a
verified finding; (2) `investigation_cap` raised 5→12 so repair is reachable; (3)
`max_seconds` wall-clock guard bounds the added latency. Lesson: a grounding gate
that *detects* but doesn't *enforce at every exit* is not a guarantee — audit the
force-terminated paths, not just the clean ones.

**Consequences:**
- `contracts.py` `Provenance` +2 fields; all structured backends (fake, bq,
  balance_sheet) set them. `judge.py`: `_check_grounding` → 3-tuple,
  `_check_answer_support` always-on, `JudgeVerdict.deterministic_failure`.
  `loop.py`: integrity failure → immediate ABANDONED.
- 3 regression tests: authentic PERIOD_NOT_FILED re-grounds (bq), numeric
  hallucination caught numbers-only, integrity failure abandons in 1 iteration. 70
  tests total. Also fixed the stale pre-ADR-9 fixture source name (audit #13).
- **Batch (Step 10) hardened** per audit #7–9: per-filing try/except + flush every
  25 + incremental resume; `--ticker` idempotent (delete-then-append); a
  `prompt_version` column; `--model` for tier A/B.
- Cost of the always-on answer-support head: one extra Sonnet call per proposed
  conclusion. Accepted — it IS the core guarantee.
- **Deferred (tracked in `docs/insights/audit-2026-07-12-fable.md`):** #4 fiscal-Q4
  10-K skip, #5 breadth-first expansion, #6 CAP_REACHED→INCONCLUSIVE laundering,
  #10–12/#14–15 hygiene. None block the batch; #4–6 are quality items for the
  interactive service.

---

## ADR-14: Fiscal year-end (10-K) handling in positional offsets — expose the skip

**Date:** 2026-07-12
**Status:** Accepted (resolves audit #4)

**Context:** ADR-12's positional offset indexes over 10-Qs only. Real data
(`period_features`) confirms the CFA hazard: a company files 3 10-Qs/year plus an
annual 10-K whose `target_period_end` sits *between* two 10-Qs (AAPL: Jun 10-Q →
Sep 10-K → Dec 10-Q). So "+1" from fiscal Q3 lands on the next 10-Q ~6 months later,
silently skipping the fiscal year-end — exactly where a persistence anomaly may
resolve or blow up.

**Decision — keep the 10-Q-only comparison, but EXPOSE the skip.** The data forces
this: the 10-K row's figures are ANNUAL (AAPL `ocf_to_net_income` ≈ 1.0 vs quarterly
0.8–4.3), so including the 10-K in the offset sequence would compare a quarterly
value to an annual one — a fake "collapse" from a time-base switch. And the true fix
(derive a Q4 *quarterly* figure = annual − Q1−Q2−Q3) is blocked: `period_features`
carries the engineered *ratio*, which is not additive, so it cannot be de-annualized
without raw flows. Therefore: resolve the offset over 10-Qs only (time-base
consistent), AND count the 10-K periods strictly between the anchor and the resolved
quarter, surfaced as `periods_skipped` on the result and `fiscal_periods_skipped` in
the model-facing content when > 0. The model is told "this jump crossed a fiscal
year-end," instead of silently reading a 6-month gap as two adjacent quarters.

**Alternatives considered:**
- *Include 10-K rows in the positional sequence* — rejected: mixes annual and
  quarterly time bases → a spurious deterioration signal.
- *Derive a true Q4 quarterly figure (annual − Q1−Q2−Q3)* — deferred: needs raw
  flow components; `period_features` has non-additive ratios. Revisit if raw flows
  land upstream (the gold-standard fix).

**Consequences:**
- `FeatureResult` gains `periods_skipped: int = 0`; `feature_history_bq` queries
  both forms, indexes over 10-Qs, counts skipped 10-Ks; `feature_history.to_model_content`
  emits `fiscal_periods_skipped` when > 0. Fakes keep tidy calendars → 0.
- Verified live (AAPL Jun 10-Q +1 → resolved Dec 10-Q, value 1.28 quarterly not the
  1.00 annual, `periods_skipped=1`). 2 regression tests. 72 tests total.

## ADR-15: Reverify batching + session memo — grounding latency as a correctness fix

**Date:** 2026-07-14
**Status:** Accepted

**Context:** An instrumented run put 48% of a single investigation's wall-clock (124.9s of
262s) in the judge's INTEGRITY head: it re-fetched each evidence item from BigQuery one at
a time — 39 sequential queries, a fresh `bigquery.Client()` built and discarded per query,
and the whole accumulated pile re-grounded on every repair cycle. This alone blew the 240s
`max_seconds` guard, terminating runs `CAPPED`/`trusted=False`. The latency defect was
manufacturing untrusted results.

**Decision:** Batch the deterministic reverify. De-dup probes by re-fetch identity
(source, ticker, requested_report_date, requested_offset, feature) in first-seen order;
group the survivors by (source, ticker) and issue ONE whole-history query per source (the
backends already pull the ticker's full history and resolve the offset positionally);
memoize source-side results on the `Judge` instance across `evaluate()` cycles; share a
lazy module-level BQ/GCS client; thread-pool the remaining groups. The `==` comparison and
the failed-list contents/order are unchanged — only the query count drops (39 → 2).

**Alternatives considered:** (a) Cache the whole verdict — rejected: caches the generator's
possibly-tampered value, defeating tamper detection; the memo stores only SOURCE truth. (b)
Raise `max_seconds` — rejected: treats the symptom, leaves the cost. (c) Skip re-grounding
already-seen items without a memo — the old per-call `seen` set did this within a call but
not across repair cycles, which was the actual duplication.

**Consequences:** Reverify 124.9s → 3.6s (measured, real BQ). The `CAPPED`/untrusted failure
mode disappears — the same PANW branch that returned CAPPED/untrusted now returns
resolved/trusted. `_judge()` is instantiated per-request in the service, so the memo is
bounded to one investigation (no cross-request staleness). New test file
`test_judge_batch_grounding.py` proves batched verdict == per-item verdict on a mixed-source,
multi-offset, duplicated, tampered pile. 102 investigator tests green.

## ADR-16: Model routing — cheap generator behind an enforcing judge; keep Sonnet judge; no Gemini Flash

**Date:** 2026-07-14
**Status:** Accepted — implementation pending an eval-gated A/B (trusted-rate must hold)

**Context:** With reverify fixed, the remaining latency is dominated by model turns: ~7
Opus generator turns + ~4 Sonnet answer-support calls per run. The owner wants cheaper/faster
calls without cutting iterations, and asked whether to adopt Gemini Flash.

**Decision:** Route the GENERATOR loop turns (incl. final synthesis) to Haiku 4.5; keep BOTH
judge heads (answer-support, C/O grade) on Sonnet 5. The generator does not need to be the
smartest model because correctness is enforced DOWNSTREAM — the integrity gate catches any
un-fetched figure, answer-support catches mis-statements, and the repair loop is the recovery
path. A Haiku mis-statement costs one cheap repair turn, not a wrong answer shipped. Gate the
switch on `eval_capture_investigations.py` (n=6): Haiku's `trusted`-rate and terminal-state
distribution must match Opus before committing.

**Alternatives considered:** (a) Gemini Flash — rejected for now: the loop, registry, and
judge are written against the Anthropic message shape (content blocks, `stop_reason`,
`tool_result`, strict tool schemas); Flash means a second SDK, a different function-call wire
format, an adapter layer, a second vendor/credential, separate caching semantics, and a fresh
eval — weeks-equivalent to beat Haiku 4.5, which is already fast/cheap and is a one-line model
string change. Reach for Flash only if Haiku proves insufficient. (b) Cheapen the judge —
rejected: it is the correctness backstop and the one place quality is not itself backstopped;
Haiku-generator + Sonnet-judge is also BETTER decorrelated than Opus/Sonnet, strengthening the
judge's independence.

**Consequences:** Expected generator cost ~82s → ~22s once shipped. The switch is deferred
behind the eval gate, so the released build still runs Opus (slower but proven). `effort` is
unsupported on Haiku — must not be sent.

## ADR-17: Step-streaming via SSE — make the wait legible without altering the loop

**Date:** 2026-07-14
**Status:** Accepted

**Context:** Even with a faster backend, an agentic loop has a hard latency floor (5-7
sequential model turns). A single blocking POST returning only the final DTO ~200s later
reads as a hang. Perceived latency is the higher-leverage fight.

**Decision:** The loop takes an OPTIONAL `on_event` callback and emits turn/tool/grounding/
verdict events (built only from the model's own tool INPUT — ADR-5 no-leak — never tool
output or provenance). A new `POST /investigate/stream` returns `text/event-stream`: the
blocking loop runs in a worker thread, `on_event` pushes onto a `queue.Queue`, an async
generator drains it with periodic heartbeats and closes on a sentinel; the final frame is
the SAME DTO the blocking endpoint returns. The worker owns semaphore release so a client
disconnect cannot leak a permit. The UI reads the stream (`fetch` + `getReader`, not
`EventSource`, so it can POST + set the token header), renders a live step feed, and falls
back to the blocking POST on stream failure with NO auto-retry (a mid-run failure means Opus
is still spending; instant retry would double-spend and 429).

**Alternatives considered:** (a) Replace the blocking endpoint — rejected: the eval harness
and a fallback path need it; streaming is additive. (b) `EventSource` — rejected: can't POST
or set headers, and the proxy must inject the API token. (c) Poll a job-status endpoint —
more moving parts than SSE for the same result. (d) Emit from inside `run_branch` in
`graph.py` — deferred (file was owned by a parallel workstream); a service-side shim mirrors
the 6-line wrapper and imports `graph._STATUS_FROM_REASON` read-only. Fold into `run_branch`
when `graph.py` next opens.

**Consequences:** `run_branch` behavior is byte-identical when `on_event is None`. Verified:
SSE frames stream incrementally through Cloud Run (first frame 0.5s, 43 events, heartbeats
holding the connection) — not buffered. The wait is now a watchable investigation. Rough
edges logged: client disconnect ≠ cancellation (worker finishes; same spend as today); a
~4s cold-import before the first `turn` event (UI caption covers it).

## ADR-18: Structured final turn (`submit_findings`) — every field verified, verifier chosen by field type

**Date:** 2026-07-15
**Status:** Accepted — implementation pending

**Context:** The served investigation output is illegible. The live ALNY run rendered: a
self-contradictory verdict (`ABANDONED / not grounded` banner over a bold "Verdict: NOT
SUPPORTED"), a final text that is a compliance affidavit addressed to the JUDGE (opens
"Understood. I will state only figures the tools returned…" — a repair-prompt artifact),
the judge's raw reasoning list (4 of 5 items say "supported"; the fatal one is
indistinguishable), a 24-item evidence dump inlining the entire MD&A (~5k words), raw
floats (`-0.5343633538029784`), a mojibake bug (UTF-8 em dash decoded as cp1252), and a
final text truncated mid-bullet. Root cause: the DTO is an engineering trace and the UI
renders the trace. One free-text document is serving three audiences (judge, user, UI)
with opposite needs. Deterministic formatting fixes most defects, but no deterministic
transform can extract a reliable verdict from free markdown authored for the wrong
audience — the answer must be BORN structured, inside the gate.

**Decision:** The loop's final turn becomes a forced tool call, `submit_findings`, replacing
free-text `end_turn` as the termination proposal the judge adjudicates. Schema and
verification map — nothing is exempt; the split is WHICH verifier, not whether:
- `verdict_sentence` (1 sentence) → answer-support head. The central claim; skipping it
  re-imports the post-hoc-summarizer trap (unverified text under a trusted badge).
- `rationale` (2–4 sentences) → answer-support. The SINGLE home for interpretation.
- `key_evidence[]` (3–6 probe references `(source, ticker, date, feature, value)`, no
  prose) → INTEGRITY head, batched `==` re-fetch (ADR-15 path). References make
  ungroundable evidence unrepresentable.
- `caveats[]` (claims of absence / limitations) → answer-support against the full pile
  including failed probes (misses are receipts too, e.g. `feature_missing`).
Contract changes riding along: judge heads (answer-support, C/O) read the serialized
fields instead of `final_text`; "required is a hint" (2026-07-14) applies to our own
schema — missing `caveats` defaults to `[]`, missing `key_evidence` FAILS CLOSED to
untrusted (an answer citing nothing is unverifiable by construction); the generator's
provenance-heavy reasoning is demoted to a collapsed audit attachment in the DTO, not
deleted. Post-gate rule made explicit: after the grounding gate, only deterministic code
may touch content — never a model. Number formatting, delta computation, evidence
collapsing/excerpting, state-label mapping, step-feed wording, and the mojibake fix are
UI-layer deterministic transforms.

**Alternatives considered:** (a) UI-only formatting of the existing DTO — rejected as
sufficient: handles every deterministic defect but cannot un-write judge-addressed prose
or reliably extract a verdict from free markdown; retained for everything post-gate.
(c) Post-hoc summarizer pass — rejected: any model rewrite AFTER the gate produces
unverified text wearing a verified badge; the flaw is pipeline position, not model
quality (an Opus summarizer breaks the guarantee identically). Per-item `why_it_matters`
annotation on evidence — DEFERRED, not rejected: it would multiply the judge's
interpretive surface exactly where answer-support is suspected miscalibrated (the ALNY
run failed grounding on "interpretive leap… though a reasonable inference"); deterministic
deltas (`+35.7% q/q`) supply most of the value free. Reopen once the eval quantifies and
fixes inference-vs-fabrication calibration.

**Consequences:** The verdict card renders verified fields only; the affidavit becomes
progressive-disclosure detail. Answer-support receives pre-parsed claims (the verdict IS
the claim, the evidence IS the citation list) instead of extracting them from markdown —
the judge's job gets crisper, not looser. Termination semantics change (tool-call, not
`end_turn`), touching `loop.py`, `judge.py`, the service DTO, and the eval harness's
capture format. Known open risk logged separately: answer-support's over-strictness on
reasoned inference (false ABANDONED on honest refutations, e.g. ALNY 2025-06-30) —
measure prevalence in the 6-ticker eval BEFORE tuning the rubric; it also contaminates
the ADR-16 Haiku A/B baseline if unfixed.
