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
