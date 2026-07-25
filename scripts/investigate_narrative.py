"""Live "said vs showed" investigation — the two-tool / two-headed-grounding proof.

The capstone for ADR-7. The agent now has BOTH tools and must decide, at run-time,
to use each: `feature_history` (what the numbers SHOWED) and `narrative_sections`
(what management SAID). The judge then grounds a MIXED evidence pile — numbers by
deterministic re-fetch (`==`), management's prose by a semantic model check — before
grading whether the said-vs-showed question is resolved.

Generator `claude-opus-4-8`; judge `claude-sonnet-5`. Backends are fakes (real BQ /
GCS deferred); the judge's deterministic head re-fetches from the numeric fake, its
semantic head reads the narrative passages.

Run:  ANTHROPIC_API_KEY=... python3 scripts/investigate_narrative.py [WBD|AAPL]
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
from qqq_scoring.investigator.tools import feature_history as fh  # noqa: E402
from qqq_scoring.investigator.tools import narrative_sections as ns  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402
from qqq_scoring.investigator.tools.narrative_sections_fake import narrative_sections_fake  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())

SYSTEM = (
    "You are an equity-analyst investigator. Compare what management SAID against "
    "what the numbers SHOW. Use feature_history for the metrics and "
    "narrative_sections for management's own words (MD&A). Never state a figure you "
    "have not fetched, and never paraphrase management beyond what the passage says. "
    "Propose a verdict on whether the narrative matches the numbers; a reviewer "
    "verifies both before the investigation is complete."
)

PREDICATE = (
    "Did ocf_to_net_income recover after the flag, and does management's MD&A "
    "characterization of the cash-flow situation match what the numbers show?"
)
TASKS = {
    "WBD": "Filing flagged: WBD 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income low. "
           f"{PREDICATE} Investigate using both the numbers and management's MD&A, then give your verdict.",
    "AAPL": "Filing flagged: AAPL 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income low. "
            f"{PREDICATE} Investigate using both the numbers and management's MD&A, then give your verdict.",
}


def _registry() -> ToolRegistry:
    return ToolRegistry([
        ToolBinding(
            name="feature_history",
            callable=feature_history_fake,
            definition=fh.tool_definition(FEATURE_KEYS),
            parse_input=fh.parse_model_input,
            serialize=fh.to_model_content,
        ),
        ToolBinding(
            name="narrative_sections",
            callable=narrative_sections_fake,
            definition=ns.tool_definition(ns.SECTION_KEYS),
            parse_input=ns.parse_model_input,
            serialize=ns.to_model_content,
        ),
    ])


def _signature(messages: list) -> list[str]:
    sig = []
    for m in messages:
        if m["role"] != "assistant":
            continue
        for b in m["content"]:
            if getattr(b, "type", None) == "tool_use":
                sig.append(b.name)
    return sig


def main() -> None:
    ticker = (sys.argv[1] if len(sys.argv) > 1 else "WBD").upper()
    generator = Anthropic()
    judge = Judge(client=Anthropic(), reverify=feature_history_fake)

    res = run_investigation(
        generator, _registry(), TASKS[ticker], judge=judge, predicate=PREDICATE, system=SYSTEM
    )

    print(f"\n═══ SAID vs SHOWED — {ticker} ══════════════════════════════")
    print(f"tools used     : {_signature(res.messages)}")
    print(f"terminal state : {res.reason.value.upper()}   ({res.iterations} turns, {res.tool_calls} tool calls)")
    if res.verdict is not None:
        print(f"judge grounded : {res.verdict.grounded}  (numbers by ==, prose by model)")
        print(f"judge confirm  : {res.verdict.confirm.value if res.verdict.confirm else '—'}")
    print(f"\nverdict:\n{res.final_text}")
    print("\nmixed evidence the judge grounded:")
    for ev in res.evidence:
        mode = ev.grounding_mode.value
        if mode == "semantic":
            body = (ev.passage[:80] + "…") if ev.passage else f"[{ev.status.value}]"
            print(f"  - [prose/{mode}] {ev.section}: {body}")
        else:
            print(f"  - [num/{mode}] {ev.feature} @ {ev.provenance.resolved_report_date} = {ev.value} [{ev.status.value}]")
    print("════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()
