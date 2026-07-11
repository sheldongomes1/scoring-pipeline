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
    RESOLVED = "resolved"            # investigated → judge resolved
    INCONCLUSIVE = "inconclusive"    # investigated → clean but ambiguous / cap reached
    ABANDONED = "abandoned"          # investigated → grounding failed past repair_cap


@dataclass
class Flag:
    """The ambiguous anomaly the graph fans out from (the root node)."""

    ticker: str
    report_date: date
    form: str
    summary: str  # human-readable: the drivers + the key_question from analyst_actions


@dataclass
class Branch:
    """One hypothesis node. `predicate` is the steering wire (ADR-8); `result` is
    the loop's TerminalResult once this branch is investigated."""

    id: str
    hypothesis: str   # the one-line causal story
    rationale: str    # why it's plausible given the flag — lets the human steer with sight
    predicate: str    # the reframed question handed to run_investigation (NOT a metric list)
    status: BranchStatus = BranchStatus.PROPOSED
    result: Any = None


@dataclass
class InvestigationGraph:
    flag: Flag
    branches: list[Branch] = field(default_factory=list)

    def get(self, branch_id: str) -> Branch:
        return next(b for b in self.branches if b.id == branch_id)


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


def propose_branches(client: Any, flag: Flag, n: int = 4, model: str = PROPOSER_MODEL) -> InvestigationGraph:
    """The fan-out: one cheap call naming N hypotheses. No tools, no investigation."""
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
    return InvestigationGraph(flag=flag, branches=branches)


_STATUS_FROM_REASON = {
    TerminalReason.RESOLVED: BranchStatus.RESOLVED,
    TerminalReason.INCONCLUSIVE: BranchStatus.INCONCLUSIVE,
    TerminalReason.ABANDONED: BranchStatus.ABANDONED,
    TerminalReason.CAP_REACHED: BranchStatus.INCONCLUSIVE,  # ran out of budget → ambiguous
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
