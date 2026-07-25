# docs/insights — canonical learnings

This directory is the single, parseable home for the project's accumulated
learnings. Two logs, one fixed format each, so an external parser can read them
without guessing.

- `lessons.md` — dated lessons log (things learned, reworks, reversed decisions,
  surprises).
- `decisions.md` — architecture decision records (ADRs).

Older `lessons*/decisions*` files elsewhere in the repo now point here; do not
append to them. Add every new entry to the files in this directory.

## lessons.md format

Each entry begins with a level-2 heading in exactly this form, then 2–5
sentences (or a short bulleted body) capturing the lesson:

```
## YYYY-MM-DD — <one-line hook>
<2–5 sentences: what surfaced, the insight, and why it matters>
```

Rules a parser can rely on:
- The date is ISO `YYYY-MM-DD`, followed by a space, an em dash, a space, then
  the one-line hook.
- Entries are in chronological order (oldest first).
- The body runs until the next `## ` heading.

## decisions.md format (ADRs)

Michael Nygard ADR format. Each entry begins with a level-2 heading in exactly
this form, then Context / Decision / Consequence sections:

```
## ADR-N: <decision name>
**Date:** YYYY-MM-DD
**Status:** Accepted | Proposed | Superseded | PENDING

**Context:** <why the decision was needed>

**Decision:** <what was decided>

**Consequences:** <what follows from it>
```

Rules a parser can rely on:
- `N` is the ADR number; numbers are unique and stable once assigned (renumber
  only on a genuine collision across merged sources).
- The heading is `## ADR-N: <name>`; the body runs until the next `## ` heading.

## Adding an entry

- A lesson (rework, surprise, reversed call) → append to `lessons.md` the moment
  it surfaces, keeping the heading format exact.
- A resolved architecture decision → append the next ADR number to
  `decisions.md`, keeping the Context / Decision / Consequence structure.
