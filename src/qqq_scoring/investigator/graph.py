"""The disambiguation graph — propose-then-steer fan-out (ADR-8).

A flag is ambiguous: a collapse in OCF/NI could be benign timing, revenue-quality
rot, structural margin pressure, or a one-off. The single-branch loop (Phase 2)
silently commits to one of these, hiding the choice in its first tool call. This
layer drags that choice into the light: a CHEAP model call names N competing
hypotheses (no tool calls, no investigation budget); the human points at the branch
worth pursuing; only THEN does the full loop run on the chosen branch, going deep.

The fan-out is a *steering surface*, not parallelism — it exists so the human can
correct the system's assumption BEFORE it commits. It is a PRODUCT layer over the
loop (mission: graph = facade, loop = load-bearing wall); the loop is unchanged.

The load-bearing field on a branch is `predicate`: the reframed QUESTION handed to
`run_investigation`. A branch names the question, never the method — hardcoding
which metrics to fetch would collapse the focused investigation back into a
workflow and destroy the run-time tool-choice (ADR-1) Phase 2 exists to guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Any

from .loop import DEFAULT_MODEL, TerminalReason, run_investigation
from .registry import ToolRegistry

# The proposer names hypotheses — a reasoning task whose quality steers the entire
# downstream investigation, so it defaults to the generator tier. It is "cheap" in
# ADR-8's sense (one call, zero tool/investigation budget), not necessarily a cheap
# model; drop to a smaller tier here only if proposal quality holds.
PROPOSER_MODEL = DEFAULT_MODEL


class BranchStatus(str, Enum):
    PROPOSED = "proposed"            # named, not yet investigated (the steering state)
    INVESTIGATING = "investigating"  # the loop is running on this branch
    RESOLVED = "resolved"            # investigated → judge confirmed OR refuted
    INCONCLUSIVE = "inconclusive"    # investigated → grounded, clean, but genuinely ambiguous
    CAPPED = "capped"                # investigated → ran out of turn budget mid-gathering
    #                                  (NOT the same as a clean 'inconclusive' — audit #6)
    ABANDONED = "abandoned"          # investigated → grounding failed / integrity failure


@dataclass
class Flag:
    """The ambiguous anomaly the graph fans out from (the root node)."""

    ticker: str
    report_date: date
    form: str
    summary: str  # human-readable: the drivers + the key_question from analyst_actions


@dataclass
class Branch:
    """One hypothesis node in the tree. `predicate` is the steering wire (ADR-8);
    `result` is the loop's TerminalResult once investigated; `children` are the
    deeper branches a RESOLVED finding spawned (ADR-10)."""

    id: str
    hypothesis: str   # the one-line causal story
    rationale: str    # why it's plausible given the flag — lets the human steer with sight
    predicate: str    # the reframed question handed to run_investigation (NOT a metric list)
    status: BranchStatus = BranchStatus.PROPOSED
    result: Any = None
    depth: int = 0                      # 0 = a root branch off the flag
    children: list["Branch"] = field(default_factory=list)


@dataclass
class InvestigationGraph:
    flag: Flag
    branches: list[Branch] = field(default_factory=list)

    def get(self, branch_id: str) -> Branch:
        """Find a branch anywhere in the TREE by id (audit #11 — recurses into
        children so `h2.1` is reachable; raises a clear KeyError if absent, not
        a bare StopIteration)."""
        found = _find_branch(self.branches, branch_id)
        if found is None:
            raise KeyError(f"no branch {branch_id!r} in graph")
        return found


def _find_branch(branches: list["Branch"], branch_id: str) -> "Branch | None":
    for b in branches:
        if b.id == branch_id:
            return b
        hit = _find_branch(b.children, branch_id)
        if hit is not None:
            return hit
    return None


def _proposer_tool(n: int) -> dict:
    """Strict tool forcing N distinct hypotheses (dogfoods ADR-3 on the proposer)."""
    return {
        "name": "propose_hypotheses",
        "description": (
            f"Propose exactly {n} DISTINCT, competing causal explanations for the "
            "flagged anomaly — genuinely different economic stories, not rephrasings. "
            "For each: the hypothesis (one line), a rationale (why it's plausible "
            "given the flag, so a human can choose), and a predicate (a specific "
            "investigative QUESTION naming the economic mechanism to test). The "
            "predicate must NOT list which metrics or tools to pull — that is the "
            "investigator's run-time choice; name the question, not the method."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "hypotheses": {
                    "type": "array",
                    # NB: the Anthropic strict-tool schema rejects array minItems/maxItems
                    # other than 0/1, so N is requested via the prompt + description and
                    # enforced by slicing to n in propose_branches (not by the schema).
                    "items": {
                        "type": "object",
                        "properties": {
                            "hypothesis": {"type": "string", "description": "The one-line causal story."},
                            "rationale": {"type": "string", "description": "Why plausible given the flag (for the human to steer by)."},
                            "predicate": {"type": "string", "description": "The investigative question to test — a mechanism, not a metric list."},
                        },
                        "required": ["hypothesis", "rationale", "predicate"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["hypotheses"],
            "additionalProperties": False,
        },
    }


def propose_branches(client: Any, flag: Flag, n: int = 4, model: str = PROPOSER_MODEL,
                     return_usage: bool = False):
    """The fan-out: one cheap call naming N hypotheses. No tools, no investigation.

    `return_usage=True` also returns a `{input_tokens, output_tokens}` dict (audit
    #15 — lets the batch log per-call token usage for cost calibration). Default
    False keeps the interactive callers' return type unchanged."""
    prompt = (
        f"A screening model flagged this filing:\n"
        f"  Company: {flag.ticker}   Filing: {flag.form} @ {flag.report_date}\n"
        f"  {flag.summary}\n\n"
        f"Propose {n} competing explanations for what is driving this anomaly, so an "
        f"analyst can choose which to investigate. Call propose_hypotheses."
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        tools=[_proposer_tool(n)],
        tool_choice={"type": "tool", "name": "propose_hypotheses"},
        messages=[{"role": "user", "content": prompt}],
    )
    payload = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    hypotheses = (payload or {}).get("hypotheses", [])[:n]
    branches = [
        Branch(
            id=f"h{i + 1}",
            hypothesis=h["hypothesis"],
            rationale=h["rationale"],
            predicate=h["predicate"],
        )
        for i, h in enumerate(hypotheses)
    ]
    graph = InvestigationGraph(flag=flag, branches=branches)
    if return_usage:
        u = getattr(resp, "usage", None)
        usage = {"input_tokens": getattr(u, "input_tokens", 0) or 0,
                 "output_tokens": getattr(u, "output_tokens", 0) or 0}
        return graph, usage
    return graph


_STATUS_FROM_REASON = {
    TerminalReason.RESOLVED: BranchStatus.RESOLVED,
    TerminalReason.INCONCLUSIVE: BranchStatus.INCONCLUSIVE,
    TerminalReason.ABANDONED: BranchStatus.ABANDONED,
    TerminalReason.CAP_REACHED: BranchStatus.CAPPED,       # budget-exhausted ≠ ambiguous (audit #6)
    TerminalReason.MODEL_STOPPED: BranchStatus.INCONCLUSIVE,  # shouldn't occur with a judge
}


def run_branch(
    branch: Branch,
    flag: Flag,
    generator: Any,
    registry: ToolRegistry,
    judge: Any,
    *,
    system: str = "",
) -> Branch:
    """Steer into one branch: run the EXISTING loop with the branch's predicate.

    This is where propose-then-steer pays off — the loop needs no Phase-3 change,
    only the branch's `predicate` as its steering input. Mutates and returns the
    branch (status + result)."""
    branch.status = BranchStatus.INVESTIGATING
    task = (
        f"Flagged filing: {flag.ticker} {flag.form} @ {flag.report_date}. {flag.summary}\n"
        f"Investigate this specific hypothesis and resolve it:\n{branch.predicate}"
    )
    result = run_investigation(
        generator, registry, task, judge=judge, predicate=branch.predicate, system=system
    )
    branch.result = result
    branch.status = _STATUS_FROM_REASON.get(result.reason, BranchStatus.INCONCLUSIVE)
    return branch


# ---------------------------------------------------------------------------
# Phase 4 — recursive expansion (ADR-10). A RESOLVED branch's finding can raise a
# NEW deeper question → a child branch. The tree grows, bounded by a semantic stop
# (the follow-up proposer returns nothing) AND two independent hard caps
# (max_depth, max_total_branches) — ADR-1's termination, lifted a level.
# ---------------------------------------------------------------------------


def propose_children(client: Any, flag: Flag, parent: Branch, n: int = 3, model: str = PROPOSER_MODEL) -> list[Branch]:
    """Seeded proposer: given a RESOLVED branch's finding, what NEW deeper questions
    warrant their own investigation? Returns [] when the finding is a satisfying
    endpoint — that empty return IS the semantic leaf signal (ADR-10)."""
    finding = parent.result.final_text if parent.result else ""
    prompt = (
        f"An investigation into {flag.ticker} {flag.form} @ {flag.report_date} resolved a hypothesis.\n"
        f"  Hypothesis: {parent.hypothesis}\n"
        f"  Question tested: {parent.predicate}\n"
        f"  Finding: {finding}\n\n"
        f"What NEW, DEEPER questions does this finding raise that each warrant their own "
        f"investigation (not a rephrasing of the above)? Propose up to {n}. If the finding "
        f"is a satisfying endpoint that raises no deeper answerable question, return an "
        f"empty list. Call propose_hypotheses."
    )
    resp = client.messages.create(
        model=model,
        max_tokens=1500,
        tools=[_proposer_tool(n)],
        tool_choice={"type": "tool", "name": "propose_hypotheses"},
        messages=[{"role": "user", "content": prompt}],
    )
    payload = next((b.input for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    hypotheses = (payload or {}).get("hypotheses", [])[:n]
    return [
        Branch(
            id=f"{parent.id}.{i + 1}",           # e.g. h2.1, h2.2 — path encodes lineage
            hypothesis=h["hypothesis"],
            rationale=h["rationale"],
            predicate=h["predicate"],
            depth=parent.depth + 1,
        )
        for i, h in enumerate(hypotheses)
    ]


class ExpansionBudget:
    """Global node budget for one expansion — the hard backstop against exponential
    breadth^depth growth, independent of max_depth and of the semantic stop (ADR-10)."""

    def __init__(self, max_total_branches: int) -> None:
        self.max_total = max_total_branches
        self.spent = 0

    def spend(self) -> None:
        self.spent += 1

    def remaining(self) -> int:
        return max(0, self.max_total - self.spent)


def expand(
    branch: Branch,
    flag: Flag,
    generator: Any,
    registry: ToolRegistry,
    judge: Any,
    *,
    system: str = "",
    max_depth: int = 2,
    budget: "ExpansionBudget | None" = None,
    breadth: int = 3,
    _investigate=run_branch,   # injectable so the tree-control logic is unit-testable
    _propose=propose_children,  # without any LLM (defaults call the real thing)
) -> Branch:
    """Investigate `branch`, then expand it BREADTH-FIRST within the caps (ADR-10, #5).

    Breadth-first (a level-by-level frontier), not depth-first: all siblings at a
    level are investigated before descending, so a shared node budget doesn't let
    the first hypothesis's subtree starve its siblings (whose order is arbitrary LLM
    emission). Only a RESOLVED branch spawns children (grounding-as-precondition, one
    level up); ABANDONED/INCONCLUSIVE/CAPPED are leaves. Stops at max_depth, when the
    node budget is exhausted (budget-capped children are recorded as PROPOSED, not
    dropped — audit #5), or when the proposer returns nothing (semantic stop)."""
    if budget is None:
        budget = ExpansionBudget(max_total_branches=7)

    _investigate(branch, flag, generator, registry, judge, system=system)
    budget.spend()

    frontier = [branch]                                  # nodes whose children we may expand
    while frontier:
        next_frontier = []
        for node in frontier:
            if node.status is not BranchStatus.RESOLVED:  # only a resolved foundation spawns
                continue
            if node.depth >= max_depth:                   # hard cap: depth
                continue
            if budget.remaining() <= 0:                   # no budget to investigate any child → don't propose
                continue
            for child in _propose(generator, flag, node, breadth):  # semantic: [] → leaf
                node.children.append(child)               # attach ALL proposed children...
                if budget.remaining() > 0:                # ...but only investigate within budget
                    _investigate(child, flag, generator, registry, judge, system=system)
                    budget.spend()
                    next_frontier.append(child)
                # else: child stays PROPOSED — budget-capped, recorded so the UI can
                # render "expansion stopped here" rather than silently vanishing.
        frontier = next_frontier
    return branch
