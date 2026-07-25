"""Live JUDGED investigation — the two-model termination proof (ADR-6).

`investigate_demo.py` let the model stop itself (ADR-4). This attaches the ADR-1
judge: the generator's `end_turn` becomes a *proposal*, and a separate Sonnet
judge owns the stop decision — grounding each cited figure by independent re-fetch
(deterministic), then grading whether the evidence resolves the predicate.

Two model integrations run here: generator `claude-opus-4-8`, judge
`claude-sonnet-5`. Data backend is still the fake (real BQ deferred); the judge's
`reverify` re-fetches from that same backend, so grounding is exercised end-to-end.

Run:  ANTHROPIC_API_KEY=... python3 scripts/investigate_judged.py [AAPL|WBD]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.loop import run_investigation  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.feature_history import (  # noqa: E402
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())

SYSTEM = (
    "You are an equity-analyst anomaly investigator. Resolve the persistence_test "
    "using ONLY the tools provided. Never state a figure you have not fetched. When "
    "confident, propose your conclusion — a separate reviewer will verify your "
    "figures against source and decide whether the investigation is complete."
)

PREDICATE = "Did ocf_to_net_income recover in the quarters following the flag?"
TASKS = {
    "AAPL": "Filing flagged: AAPL 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income anomalously low. "
            f"persistence_test: '{PREDICATE}' Investigate and give your verdict.",
    "WBD": "Filing flagged: WBD 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income anomalously low. "
           f"persistence_test: '{PREDICATE}' Investigate and give your verdict.",
}


def _registry() -> ToolRegistry:
    binding = ToolBinding(
        name="feature_history",
        callable=feature_history_fake,
        definition=tool_definition(FEATURE_KEYS),
        parse_input=parse_model_input,
        serialize=to_model_content,
    )
    return ToolRegistry([binding])


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "WBD").upper()
    task = TASKS[ticker]

    generator = Anthropic()
    judge = Judge(client=Anthropic(), reverify=feature_history_fake)  # G re-fetches from source

    res = run_investigation(
        generator, _registry(), task, judge=judge, predicate=PREDICATE, system=SYSTEM
    )

    print(f"\n═══ JUDGED INVESTIGATION — {ticker} ════════════════════════")
    print(f"terminal state : {res.reason.value.upper()}")
    print(f"turns / tools  : {res.iterations} / {res.tool_calls}")
    if res.verdict is not None:
        print(f"judge grounded : {res.verdict.grounded}")
        print(f"judge confirm  : {res.verdict.confirm.value if res.verdict.confirm else '—'}")
        print(f"judge open Qs  : {res.verdict.open_questions}")
        print(f"judge reason   : {res.verdict.reasoning}")
    print(f"\nverdict (generator):\n{res.final_text}")
    print("\nevidence re-verified by judge (provenance withheld from generator):")
    for ev in res.evidence:
        print(f"  - {ev.provenance.ticker} {ev.feature} @ {ev.provenance.resolved_report_date} "
              f"= {ev.value} [{ev.status.value}]")
    print("════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()
