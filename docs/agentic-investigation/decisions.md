# Decisions Log — Agentic Investigator (ADRs)

Michael Nygard ADR format. One entry per resolved Socratic checkpoint. An open
checkpoint is logged as **PENDING** so we resume exactly where we left off.

---

## ADR-1: What makes the investigation loop agentic (not a workflow)

**Date:** 2026-06-24
**Status:** PENDING — awaiting Sheldon's answer in Socratic checkpoint.

**Context:** Goal #2 is a *truly agentic* AI process, explicitly contrasted with a
workflow. `scripts/orchestrate.py` is already a fixed DAG (author-time control
flow). To build the new thing correctly we must first define the property that
separates an agent from that workflow, the agent's action space, and how the loop
terminates. Without a real action space and a stopping condition, "agentic" is
just an LLM call with extra steps.

**Decision:** _(to be filled in once Sheldon answers — three parts:)_
1. Action space — the concrete tools/harnesses the agent can call.
2. Termination — the exit condition + budget cap.
3. The line — the one property that makes this an agent and orchestrate.py a workflow.

**Alternatives considered:** _(to be filled in — incl. the steelman of any
rejected position)_

**Consequences:** _(to be filled in)_
