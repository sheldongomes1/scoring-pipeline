"""Live recursive investigation tree — Phase 4 (ADR-10).

Watch a single flag grow into a multi-level investigation:

  1. FAN OUT   — propose N root hypotheses (ADR-8, cheap).
  2. STEER     — a human points at one root (CLI arg).
  3. EXPAND    — that branch investigates, and each RESOLVED finding autonomously
                 spawns deeper child branches, bounded by TWO independent hard caps
                 (max_depth, node budget) + the semantic stop (no new questions).

The human steered once, at the root; the subtree grows itself within the caps.
Mock-but-real: fake backends, real recursion + grounding.

Run:  ANTHROPIC_API_KEY=... python3 scripts/investigate_tree.py [root_id]
Caps are deliberately tight (budget 4, depth 2) to bound live cost.
"""
from __future__ import annotations

import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

from qqq_scoring.investigator.graph import (  # noqa: E402
    ExpansionBudget,
    Flag,
    expand,
    propose_branches,
)
from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools import balance_sheet as bs  # noqa: E402
from qqq_scoring.investigator.tools import feature_history as fh  # noqa: E402
from qqq_scoring.investigator.tools import narrative_sections as ns  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_fake import SOURCE as BS_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_fake import balance_sheet_items as balance_sheet_fake  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import SOURCE as FH_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402
from qqq_scoring.investigator.tools.narrative_sections_fake import narrative_sections_fake  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())

SYSTEM = (
    "You are an equity-analyst investigator. Resolve the specific hypothesis you are "
    "given using ONLY the tools provided (ratios, balance-sheet line items, and "
    "management's MD&A). Never state a figure you have not fetched. Give a verdict; "
    "a reviewer verifies your evidence before the branch is closed."
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
        ToolBinding("balance_sheet_items", balance_sheet_fake, bs.tool_definition(bs.ITEM_KEYS),
                    bs.parse_model_input, bs.to_model_content),
        ToolBinding("narrative_sections", narrative_sections_fake, ns.tool_definition(ns.SECTION_KEYS),
                    ns.parse_model_input, ns.to_model_content),
    ])


def _print_tree(branch, prefix="") -> None:
    head = branch.result.final_text.splitlines()[0] if branch.result and branch.result.final_text else ""
    print(f"{prefix}({branch.id}) [{branch.status.value.upper()}] {branch.hypothesis}")
    print(f"{prefix}    ? {branch.predicate}")
    if head:
        print(f"{prefix}    → {head[:110]}")
    for c in branch.children:
        _print_tree(c, prefix + "    ")


def main() -> None:
    root_id = sys.argv[1] if len(sys.argv) > 1 else "h1"
    generator = Anthropic()
    judge = Judge(client=Anthropic(), reverify={FH_SOURCE: feature_history_fake, BS_SOURCE: balance_sheet_fake})

    print("\n══ 1. FAN OUT (cheap: naming root hypotheses) ══")
    graph = propose_branches(generator, FLAG, n=4)
    for b in graph.branches:
        print(f"  ({b.id}) {b.hypothesis}")

    root = graph.get(root_id)
    print(f"\n══ 2. STEER into {root_id}, then 3. EXPAND autonomously (depth≤2, budget 4) ══")
    budget = ExpansionBudget(max_total_branches=4)
    expand(root, FLAG, generator, _registry(), judge, system=SYSTEM,
           max_depth=2, budget=budget, breadth=2)

    print(f"\n══ THE GROWN TREE  (nodes investigated: {budget.spent}) ══\n")
    _print_tree(root)
    print("\n(The human steered once, at the root; the subtree grew itself within the caps.)")


if __name__ == "__main__":
    main()
