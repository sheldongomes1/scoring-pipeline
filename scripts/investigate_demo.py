"""Live single-branch investigation — the AGENTIC-PROPERTY proof (ADR-1).

The unit tests (`test_loop_closes.py`) prove the harness *closes* using a scripted
fake client. But a scripted client can't *choose* — it replays a fixed sequence.
Only a live model demonstrates ADR-1's actual claim: **the model owns the next
edge**, picking the tool at run-time in reaction to what it observed.

This script swaps the ScriptedClient for the real Anthropic SDK and runs one
investigation against `claude-opus-4-8`. Everything else is identical to the
tested harness — same `run_investigation`, same `ToolRegistry`, same fake data
backend (real BigQuery is still deferred per ADR-4; the property under test here
is tool-*choice*, which is independent of where the data lives).

Run:  ANTHROPIC_API_KEY=... python3 scripts/investigate_demo.py
Cost: one Opus turn per loop iteration; the task is answerable in ~1 tool call.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

from qqq_scoring.investigator.loop import run_investigation  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.feature_history import (  # noqa: E402
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.feature_history_fake import (  # noqa: E402
    feature_history_fake,
)

REPO = Path(__file__).resolve().parents[1]

# ADR-3 rule 3: the features enum is sourced from the canonical list, not hardcoded.
FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())

SYSTEM = (
    "You are an equity-analyst anomaly investigator. A screening model has flagged "
    "a filing and left a persistence_test — a specific factual question about "
    "whether the anomaly persisted, recovered, or worsened in a later period. "
    "Your job: resolve the persistence_test using ONLY the tools provided (they "
    "read golden-source financial data). Do not guess values from memory. When you "
    "have the evidence, state a clear verdict — recovered, persisted, or "
    "inconclusive — and cite the numbers the tool returned."
)

# The flagged filing = the AAPL story baked into the fake backend: ocf_to_net_income
# fell to 0.55 at the 2025-06-30 quarter. The persistence_test asks about the NEXT
# quarter — which the model must fetch itself (period_offset = +1).
TASK = (
    "Filing flagged: AAPL 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income "
    "was anomalously low (cash conversion dropped). persistence_test: 'Did "
    "ocf_to_net_income recover in the following quarter?' Investigate and give your "
    "verdict."
)


def _registry() -> ToolRegistry:
    binding = ToolBinding(
        name="feature_history",
        callable=feature_history_fake,   # real BQ deferred (ADR-4); tool-choice is what we test
        definition=tool_definition(FEATURE_KEYS),
        parse_input=parse_model_input,
        serialize=to_model_content,
    )
    return ToolRegistry([binding])


def _print_trace(messages: list) -> None:
    """Render the transcript so the model's run-time CHOICES are visible."""
    print("\n─── investigation trace ─────────────────────────────────────")
    for msg in messages:
        role = msg["role"]
        content = msg["content"]
        if role == "user" and isinstance(content, str):
            print(f"\n[user] {content}")
            continue
        if role == "user":  # a list of tool_result dicts
            for block in content:
                print(f"  [tool_result -> model] {block['content']}")
            continue
        # assistant turn: a list of SDK blocks
        for block in content:
            btype = getattr(block, "type", None)
            if btype == "text" and block.text.strip():
                print(f"\n[assistant] {block.text.strip()}")
            elif btype == "tool_use":
                print(f"\n[assistant CHOOSES tool] {block.name}({json.dumps(block.input)})")
    print("─────────────────────────────────────────────────────────────")


def main() -> None:
    client = Anthropic()
    res = run_investigation(client, _registry(), TASK, system=SYSTEM)

    _print_trace(res.messages)

    print("\n─── outcome ─────────────────────────────────────────────────")
    print(f"terminal reason : {res.reason.value}")
    print(f"iterations      : {res.iterations}")
    print(f"tool calls      : {res.tool_calls}")
    print(f"final answer    : {res.final_text}")
    print(f"evidence rows   : {len(res.evidence)} (full provenance, judge-only)")
    for ev in res.evidence:
        print(
            f"    - {ev.feature} @ {ev.provenance.resolved_report_date} "
            f"= {ev.value} [{ev.status.value}]  src={ev.provenance.accession_number}"
        )
    print("─────────────────────────────────────────────────────────────")


if __name__ == "__main__":
    main()
