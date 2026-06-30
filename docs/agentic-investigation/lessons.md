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
