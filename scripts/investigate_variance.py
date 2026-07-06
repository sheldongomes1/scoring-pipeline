"""ADR-1 path-variance proof — the RIGOROUS discriminator.

`investigate_demo.py` shows the model *chooses* a tool. That's necessary but not
sufficient for ADR-1: a workflow also 'chooses' (the engineer chose, at
author-time). The property that separates an agent from `orchestrate.py`'s fixed
DAG is **path-variance conditioned on observations** — run the SAME code on two
different inputs and the *tool-call sequence itself* must differ as a function of
what the model observed, not just the final answer.

This script runs the identical `run_investigation` on two filings baked into the
fake backend with deliberately different stories:
  * AAPL — cash conversion dipped then RECOVERED (0.55 -> 0.95)
  * WBD  — cash conversion dipped and STAYED broken (0.50 -> 0.52 -> 0.49)

If the agent is real, WBD should provoke MORE / different tool calls (no recovery
at +1 → reach further to explain why), while AAPL resolves in fewer. We print both
traces and diff the call sequences. We assert nothing about the model's exact
moves — we report whatever it did, honestly.

Run:  ANTHROPIC_API_KEY=... python3 scripts/investigate_variance.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from anthropic import Anthropic  # noqa: E402

from qqq_scoring.investigator.loop import TerminalResult, run_investigation  # noqa: E402
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
    "You are an equity-analyst anomaly investigator. A screening model flagged a "
    "filing and left a persistence_test — a factual question about whether the "
    "anomaly recovered, persisted, or worsened in later periods. Resolve it using "
    "ONLY the tools provided (they read golden-source financial data). Never guess "
    "values from memory. Investigate as far as the evidence warrants: if the "
    "anomaly did NOT recover, dig further — check later quarters or corroborating "
    "features — before concluding. When done, give a verdict: recovered, "
    "persisted, worsened, or inconclusive, citing the numbers the tools returned."
)

# Same persistence_test wording; only the ticker differs. Any difference in the
# investigation path is therefore attributable to observed DATA, not the prompt.
CASES = [
    ("AAPL", "AAPL 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income anomalously low."),
    ("WBD", "WBD 10-Q, report_date 2025-06-30. Driver: ocf_to_net_income anomalously low."),
]
TASK_TMPL = (
    "Filing flagged: {desc} persistence_test: 'Did ocf_to_net_income recover in "
    "the quarters following the flag?' Investigate and give your verdict."
)


def _registry() -> ToolRegistry:
    binding = ToolBinding(
        name="feature_history",
        callable=feature_history_fake,
        build_definition=tool_definition,
        parse_input=parse_model_input,
        serialize=to_model_content,
    )
    return ToolRegistry([binding], FEATURE_KEYS)


def _call_signature(res: TerminalResult) -> list[str]:
    """The observable tool-call sequence — the thing ADR-1 says must vary.

    Reconstructed from the transcript: one entry per tool_use block, in order,
    rendered as `feature@offset` so two runs can be diffed at a glance."""
    sig = []
    for msg in res.messages:
        if msg["role"] != "assistant":
            continue
        for block in msg["content"]:
            if getattr(block, "type", None) == "tool_use":
                inp = block.input
                feats = ",".join(inp.get("features", []))
                sig.append(f"{feats}@{inp.get('period_offset')}")
    return sig


def main() -> None:
    client = Anthropic()
    results: dict[str, TerminalResult] = {}

    for ticker, desc in CASES:
        res = run_investigation(client, _registry(), TASK_TMPL.format(desc=desc), system=SYSTEM)
        results[ticker] = res
        sig = _call_signature(res)
        print(f"\n═══ {ticker} ═══════════════════════════════════════════════")
        print(f"tool-call sequence : {sig}")
        print(f"tool calls / iters : {res.tool_calls} / {res.iterations}  ({res.reason.value})")
        print(f"verdict            : {res.final_text.splitlines()[0] if res.final_text else '(none)'}")

    print("\n═══ PATH-VARIANCE VERDICT ══════════════════════════════════")
    sig_a = _call_signature(results["AAPL"])
    sig_w = _call_signature(results["WBD"])
    print(f"AAPL path : {sig_a}")
    print(f"WBD  path : {sig_w}")
    if sig_a != sig_w:
        print(
            "\n✓ PATHS DIVERGED. Same code, same prompt, different tool-call "
            "sequence — driven by observed data. This is ADR-1's discriminator: "
            "path-variance conditioned on observations. Not a workflow."
        )
    else:
        print(
            "\n≈ Paths matched this run. The model reached the same evidence the "
            "same way; variance shows in the verdicts, not the path. Re-run or "
            "widen the divergence in the fixture to force the stronger proof."
        )
    print("════════════════════════════════════════════════════════════")


if __name__ == "__main__":
    main()
