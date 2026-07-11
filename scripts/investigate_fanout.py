"""Live disambiguation graph — propose-then-steer, end to end (ADR-8).

Watch a single flag fan out into competing hypotheses, then steer into one:

  1. FAN OUT (cheap): one model call names N hypotheses — no tools, no
     investigation budget. This is the steering surface.
  2. STEER: a human (here, a CLI arg) points at the branch worth pursuing.
  3. DEEP DIVE: the FULL Phase-2 loop runs on that branch alone — its `predicate`
     the only steering input — with both tools and the grounding judge.

The other branches stay PROPOSED: the budget was never spent committing to them.

Run:  ANTHROPIC_API_KEY=... python3 scripts/investigate_fanout.py [branch_id]
      (branch_id defaults to h1; run once to see the branches, then pick.)
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

from qqq_scoring.investigator.graph import BranchStatus, Flag, propose_branches, run_branch  # noqa: E402
from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools import feature_history as fh  # noqa: E402
from qqq_scoring.investigator.tools import narrative_sections as ns  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402
from qqq_scoring.investigator.tools.narrative_sections_fake import narrative_sections_fake  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())

SYSTEM = (
    "You are an equity-analyst investigator. Resolve the specific hypothesis you are "
    "given using ONLY the tools provided (numbers via feature_history, management's "
    "words via narrative_sections). Never state a figure you have not fetched. Give "
    "a verdict; a reviewer verifies your evidence before the branch is closed."
)

FLAG = Flag(
    ticker="WBD",
    report_date=date(2025, 6, 30),
    form="10-Q",
    summary=(
        "Anomaly drivers: ocf_to_net_income = 0.50 (depressed cash conversion), "
        "net_margin = 0.05, accrual_ratio = 0.09 (elevated). "
        "key_question: why did cash conversion collapse, and will it persist?"
    ),
)


def _registry() -> ToolRegistry:
    return ToolRegistry([
        ToolBinding("feature_history", feature_history_fake, fh.tool_definition(FEATURE_KEYS),
                    fh.parse_model_input, fh.to_model_content),
        ToolBinding("narrative_sections", narrative_sections_fake, ns.tool_definition(ns.SECTION_KEYS),
                    ns.parse_model_input, ns.to_model_content),
    ])


def _print_graph(graph, chosen_id=None) -> None:
    print(f"\n┌─ FLAG: {graph.flag.ticker} {graph.flag.form} @ {graph.flag.report_date}")
    print(f"│  {graph.flag.summary}")
    print("│")
    for i, b in enumerate(graph.branches):
        tee = "└─" if i == len(graph.branches) - 1 else "├─"
        mark = " ◀── STEERING HERE" if b.id == chosen_id else ""
        print(f"{tee} ({b.id}) [{b.status.value.upper()}] {b.hypothesis}{mark}")
        pad = "   " if i == len(graph.branches) - 1 else "│  "
        print(f"{pad}   why: {b.rationale}")
        print(f"{pad}   ?  : {b.predicate}")


def main() -> None:
    chosen_id = sys.argv[1] if len(sys.argv) > 1 else "h1"
    generator = Anthropic()
    judge = Judge(client=Anthropic(), reverify=feature_history_fake)

    # 1 + 2 — FAN OUT and render the steering surface.
    print("\n══ FANNING OUT (cheap: naming hypotheses, no investigation) ══")
    graph = propose_branches(generator, FLAG, n=4)
    _print_graph(graph, chosen_id)

    # 3 — STEER into the chosen branch; the full loop runs on it alone.
    branch = graph.get(chosen_id)
    print(f"\n══ STEERING into {chosen_id}: {branch.hypothesis} ══")
    run_branch(branch, FLAG, generator, _registry(), judge, system=SYSTEM)

    print(f"\n── branch {chosen_id} result ─────────────────────────────────")
    print(f"terminal state : {branch.status.value.upper()}")
    print(f"tools used     : {branch.result.tool_calls} calls over {branch.result.iterations} turns")
    if branch.result.verdict is not None:
        print(f"judge          : grounded={branch.result.verdict.grounded}, "
              f"confirm={branch.result.verdict.confirm.value if branch.result.verdict.confirm else '—'}")
    print(f"\nverdict:\n{branch.result.final_text}")

    print("\n══ GRAPH AFTER STEERING (others never spent budget) ══")
    _print_graph(graph, chosen_id)
    unspent = [b.id for b in graph.branches if b.status is BranchStatus.PROPOSED]
    print(f"\nBudget spent on 1 branch; {len(unspent)} still PROPOSED ({', '.join(unspent)}) — "
          "steer into another anytime.")


if __name__ == "__main__":
    main()
