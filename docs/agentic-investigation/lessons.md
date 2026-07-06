# Lessons Log — Agentic Investigator

Portfolio / LinkedIn fuel. Append the moment a lesson, rework, reversed decision,
or surprise surfaces during the build — not at phase end. Capture the story.

Format:
```
## {date} — {one-line hook}
- Situation: what we were doing when it surfaced
- What broke / what we assumed: the issue or wrong assumption
- Lesson: the insight, stated for the audience
- Fix / rework: what we changed
- Post angle: the one-sentence hook
```

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
