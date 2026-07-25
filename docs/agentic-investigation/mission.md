# Mission — Agentic Investigator (working name)

**Status:** Design phase (Phase 0). Started 2026-06-24.
**Mode:** Socratic build (tutor). Goal is to learn the *why*, not just ship code.

---

## The two goals (one task)

1. **Learn query disambiguation** — from Isaac Flath's deep-dive (notes in
   `query-disambiguation.md`). Core thesis: *a prompt is the least visible place
   to put context. A good interface exposes the hidden assumption the system is
   about to act on, so the user can see and correct it before the system commits.*
   Sheldon especially liked the **NotebookLM mind-map** surface: generate a graph
   from the user's own material, let the user point at a branch.

2. **Build a truly agentic AI process** — not a workflow / fixed DAG (that is what
   `scripts/orchestrate.py` already is), but a loop where **the model chooses the
   next action at run-time** from a real action space (tools/harness), with a
   termination condition and a budget.

## The fused idea

Today, the `analyst_actions` layer (`explanations/analyst_actions_prompt.py`,
Step 8) produces four fields per flagged filing that are all **instructions to a
human**: `investigation_path`, `key_question`, `persistence_test`,
`priority_section`. Each one says *"go look over there."*

Insight: **the system can usually reach "over there" itself.** Next quarter's
OCF/NI is in BigQuery; the MD&A section is in GCS narrative; latest fundamentals
are one FMP call away. So the human errand is often unnecessary.

**The synthesis:** the NotebookLM mind-map *is* the disambiguation surface for an
agentic investigation. Each node = a hypothesis / sub-question (a branch of "what
would escalate"). The agent doesn't just *suggest* a branch — it *expands* it by
calling tools and bringing back evidence. The human points at the branch worth
pursuing; the agent runs it autonomously; results may spawn new branches. That
loop is where Goal 1 (disambiguation graph) and Goal 2 (truly agentic) become the
same system.

## Where it likely lands in the product

- Built on top of the `analyst_actions` output (Step 8 today).
- **Probably the first piece of the product that is NOT batch.** A user picking a
  branch is request-time and interactive — that breaks the "everything is a step
  in orchestrate.py" assumption. To be decided in design (batch pre-compute of
  level-1 branches vs. fully interactive service in `redink-ui`).

## Scope guardrails (so we don't sprawl)

- Build the smallest thing that is genuinely agentic end-to-end first (one branch,
  real tools, real termination), THEN add the disambiguation graph on top.
- Every architectural decision gets an ADR in `decisions.md` before code.
- New pipeline elements must be reflected in `orchestrate.py` per CLAUDE.md — but
  the interactive parts may live outside the batch DAG (open design question).

## Source material

- `query-disambiguation.md` (repo root) — Isaac Flath session notes.
- `explanations/analyst_actions_prompt.py` — the "what would escalate" producer.
- `scripts/orchestrate.py` — the existing workflow (the contrast case).
