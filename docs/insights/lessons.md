# Lessons Log

Canonical, dated lessons log. See `README.md` for the entry format an external
parser expects. Each entry begins `## YYYY-MM-DD — <one-line hook>` followed by
the lesson body. Chronological order.

---

## 2026-06-24 — The "go look over there" tell that a feature should be an agent

- Situation: Reviewing the `analyst_actions` layer, which tells a human analyst
  what to investigate after an anomaly flag (key_question, persistence_test,
  priority_section).
- What we assumed: that surfacing the right *instruction* to the user was the
  product. The four output fields are all errands handed to a human.
- Lesson: when your product's output is a list of "go check X over there"
  instructions, and the system already has a path to reach X (BigQuery, GCS
  narrative, an API), that errand is a candidate for an agent. The instruction is
  a smell that you stopped one harness short.
- Fix / rework: reframe the escalation section as an agentic investigation that
  reaches the data itself, with a disambiguation graph letting the user steer.
- Post angle: "If your AI feature ends every answer with 'now go check X
  yourself,' you probably stopped one tool call short of an agent."

## 2026-06-30 — Grounding is a precondition, not a co-equal axis

- Situation: Designing the judge that decides when an agentic investigation stops.
  The judge grades each output on three axes — Confirm/refute (C), Grounded (G),
  Open-questions (O) — giving an 8-state truth table to route on (loop / rerun /
  stop).
- What we assumed: that C, G, and O are three independent, co-equal signals you
  read off and combine. We filled the truth table treating an ungrounded output's
  "confirm/refute" verdict and "open questions" list as usable signal.
- Lesson: grounding is a **gate**, not a peer. If the output isn't traceable to
  source data (G=No), then C and O were produced by the *same* untrustworthy
  reasoning that failed the grounding check — so routing on them means routing on
  signals you just declared unreliable. Worse, "loop to gather more" on an
  ungrounded output burns the entire budget stacking garbage on garbage. The fix
  collapses 8 rows to one rule: if not grounded → repair (own small cap) then
  abandon; only *after* grounding passes do C and O decide loop-vs-stop.
- Fix / rework: reordered the policy so grounding short-circuits first. Surfaced a
  bonus third terminal state in the process — "inconclusive" (clean evidence,
  genuinely ambiguous) is distinct from "failed" and is a *useful* answer.
- Post angle: "Your LLM judge's 'is this correct?' score is worthless if the
  'is this grounded?' score failed — check provenance first, or you're scoring
  hallucinations."

## 2026-07-05 — The loop was fine; my test was lying to me (Python aliasing)

- Situation: Building the Phase-2 agent loop. To test it without a live model I
  wrote a scripted fake Anthropic client that records the args of every
  `messages.create()` call, so I could assert "on the 2nd call, the model was
  handed a tool_result that did NOT contain the provenance receipts."
- What broke / what we assumed: the assertion blew up with `'_TextBlock' object
  is not subscriptable`. I assumed the loop had mangled the message it sent. It
  hadn't. The agent loop reuses ONE `messages` list and mutates it in place
  (append assistant turn, append tool_result, loop). My fake stored
  `kwargs["messages"]` by reference — so after the run, every recorded call
  pointed at the *same* final list. Call #1's "snapshot" was actually showing
  call #4's state. The loop was correct; the observer was aliased to the thing it
  was observing.
- Lesson: a list in Python is a handle, not a value. Anywhere you log mutable
  state for *later* inspection — audit logs, event sourcing, test spies, undo
  stacks — you must snapshot at capture time (`list(x)` / `copy.deepcopy`), or the
  log silently reports the present as if it were the past. The bug hid as a loop
  bug for a minute because the symptom surfaced downstream of the real cause.
- Fix / rework: snapshot the messages list in the fake client's `_next`
  (`{**kwargs, "messages": list(kwargs["messages"])}`). 7/7 tests green.
- Post angle: "My agent-loop test failed and I spent a minute blaming the loop.
  The loop was perfect. My test spy was aliased to the mutable state it was spying
  on — it kept overwriting its own evidence. A list is a handle, not a value;
  snapshot before you log."

## 2026-07-05 — Proving 'agentic' with a diff, not a demo

- Situation: The loop closes and the model calls tools — but "it called a tool"
  doesn't prove *agentic*. A workflow calls tools too; the engineer just chose the
  calls at author-time. I needed the rigorous version of ADR-1's claim
  (path-variance conditioned on observations), not a feel-good single run.
- What we assumed / the trap: it's tempting to declare victory at the first live
  tool call. That's the workflow-in-disguise trap — a fixed DAG also emits tool
  calls; the data differs but the *step sequence* is identical run to run.
- Lesson: you prove an agent with a DIFF, not a demo. Run the SAME code on two
  inputs engineered to warrant different investigations, and show the tool-call
  *sequence itself* diverges as a function of what the model observed. If the two
  traces are identical, you built a workflow no matter how fancy the wrapper.
  Ours diverged sharply: on the recovered ticker (AAPL) the model ran a
  single-feature time-series sweep (offsets 0,−1,+1,+2,+3); on the still-broken
  ticker (WBD) it batched *multiple corroborating features* per call to explain
  *why* — different features, offsets, and batching, from one prompt.
- Fix / rework: `scripts/investigate_variance.py` runs both cases, reconstructs
  each tool-call signature from the transcript, and diffs them. The assertion is
  on the *paths*, not the answers.
- Post angle: "How do you prove your AI is an 'agent' and not a workflow with
  extra steps? Run the same code on two different inputs. If the tool-call
  sequence doesn't change, you built a workflow. Prove it with a diff, not a demo."

## 2026-07-05 — 'Two independent caps' means independent, not two numbers you add

- Situation: Building the ADR-1 judge, which owns loop termination. ADR-1 specified
  TWO budget caps — investigation_cap (bounds evidence-gathering) and repair_cap
  (bounds grounding reruns) — and stressed they must be *independent* so a repair
  explosion can't hide behind the investigation budget.
- What broke / what we assumed: I implemented "independent" as
  `ceiling = investigation_cap + repair_cap` — one summed turn limit. A unit test
  caught it: a no-judge run with investigation_cap=3 ran 5 turns. The bug: a run
  that can *never repair* (no judge attached) was still handed the repair headroom.
  Summing two budgets doesn't make them independent — it makes one bigger budget.
- Lesson: independence between two limits is a routing property, not an arithmetic
  one. investigation_cap must bound TOTAL turns (the real backstop); repair_cap is
  a SEPARATE sub-ceiling that trips its own terminal state (ABANDONED) on its own
  counter, regardless of remaining total budget. That's what stops a repair loop
  hiding behind the investigation budget — the thing ADR-1 actually asked for. If
  you add two caps together, neither is independent; you just renamed their sum.
- Fix / rework: `ceiling = investigation_cap`; `repairs` counted separately and
  compared to `repair_cap` for the ABANDONED route. 24/24 tests green.
- Post angle: "Spec said 'two independent budget caps.' I added them together.
  Independence between limits is about *which one trips and what happens* — not
  arithmetic. Add two caps and you don't have two caps, you have one bigger one."

## 2026-07-05 — The judge that owns 'stop' catches a distinction a flag can't

- Situation: First live run of the two-model loop — Opus generator proposes, Sonnet
  judge owns the stop decision (grounds each figure by re-fetch, then grades). Ran
  it on WBD, whose cash conversion stayed broken and whose ocf_to_assets was
  feature_missing for every quarter.
- What we assumed / the risk: a judge deciding "are there open questions?" would
  see missing data and demand it — looping forever on data that doesn't exist.
- Lesson: giving the judge the 3-state enum (found / feature_missing /
  period_not_filed) rather than a null lets it reason about WHY something is absent.
  Sonnet correctly ruled the missing ocf_to_assets a "noted limitation, not an
  open answerable question central to the predicate" and returned RESOLVED. A
  bare-null result would have looked identical to a fetchable-but-missing value,
  and the judge would have looped. The enum you designed three ADRs ago is what
  makes the termination logic correct under a second model.
- Fix / rework: none — it worked first try because the contract carried the
  distinction. This is the ADR-2 status-enum decision cashing out downstream.
- Post angle: "My AI investigator's judge saw missing data and had to decide: is
  this a dead end or a to-do? It got it right — because three design decisions ago
  I refused to represent 'missing' as null. Contracts you set early decide whether
  your agent loops forever."

## 2026-07-11 — The 'generic' component that was secretly fitted to one case

- Situation: Adding the second tool (narrative sections) to an agent that already
  had one (numeric feature history). The tool registry looked fully generic —
  `ToolRegistry(bindings, feature_keys)` routed any tool by name.
- What broke / what we assumed: it passed ONE `feature_keys` list to every tool's
  schema builder. Fine with one tool. The second tool's vocabulary is *section
  names*, not feature names — so the "generic" registry was silently assuming all
  tools share one enum. It only looked generic because there had only ever been one
  tool to fit.
- Lesson: an abstraction validated against a single case isn't proven generic —
  it's proven to fit that case. The second instance is the real test, and it
  routinely exposes a shared assumption the first one hid. Fix: make each binding
  self-contained (it carries its own already-built schema, enum baked in by whoever
  knows that tool's vocabulary); the registry then holds no vocabulary at all.
- Post angle: "My tool registry looked perfectly generic — until the second tool.
  One shared config field was quietly assuming every tool spoke the same
  vocabulary. An abstraction tested on one case isn't generic; it just fits. The
  second case is the test."

## 2026-07-11 — I made grounding polymorphic and forgot its twin

- Situation: The narrative tool returns prose, so the judge's grounding gained a
  second 'head' — deterministic `==` for numbers, a model semantic-check for
  passages — dispatched by a `grounding_mode` tag on each evidence item.
- What broke / what we assumed: I upgraded the grounding path to handle both
  evidence types, felt done, and started the live demo. But the judge's *other*
  step — the confirm/open grader — still built its evidence view assuming every
  item was a numeric result (`e.feature`, `e.value`, `e.resolved_report_date`). The
  moment a real investigation gathered a number AND a passage, that grader would
  `AttributeError`. My unit tests missed it because they called the grounding
  method directly, never the full grade path with mixed evidence.
- Lesson: when you make one code path polymorphic over a new type, grep for EVERY
  path that touches that type — the loud one you're editing and the quiet siblings
  that still assume the old shape. And write the test at the level that actually
  exercises the integration (the full `evaluate()`, not just the sub-method), or
  the gap hides. Caught it building the demo; added the mixed-evidence regression.
- Post angle: "I taught my judge to verify two kinds of evidence and shipped it.
  It would've crashed on the first real run — I upgraded the grounding step and
  forgot its twin, the grader, still assumed one type. Make one path polymorphic,
  audit all of them. Test at the integration level or the gap hides."
