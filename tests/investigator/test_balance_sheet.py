"""Tool #3 (balance-sheet line items) + source-routed grounding (ADR-9).

Proves: the line-item tool REUSES FeatureResult (same deterministic grounding),
and the judge routes its deterministic re-fetch to the backend that produced each
item — by provenance.source. The routing is the real work of tool #3: a single
reverify was a hidden one-backend assumption the 2nd structured tool exposed.

Run: `python tests/investigator/test_balance_sheet.py`
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet import (  # noqa: E402
    ITEM_KEYS,
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.balance_sheet_fake import SOURCE as BS_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_fake import balance_sheet_items as bs_fake  # noqa: E402
from qqq_scoring.investigator.tools.contracts import FeatureStatus, GroundingMode  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import SOURCE as FH_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake as fh_fake  # noqa: E402


# --- reuse of the structured contract ---------------------------------------


def test_line_item_reuses_featureresult_deterministic():
    """ADR-9: a line item is a FeatureResult, grounded deterministically like a ratio."""
    r = bs_fake("WBD", date(2025, 6, 30), 0, ["accounts_receivable"])[0]
    assert r.feature == "accounts_receivable"
    assert r.value == 7150.0
    assert r.grounding_mode is GroundingMode.DETERMINISTIC
    assert r.provenance.source == BS_SOURCE


def test_fake_exercises_all_three_states():
    found = bs_fake("WBD", date(2025, 6, 30), 0, ["accounts_receivable"])[0]
    missing = bs_fake("WBD", date(2025, 6, 30), 0, ["inventory"])[0]      # media co, no inventory
    not_filed = bs_fake("WBD", date(2025, 6, 30), 3, ["accounts_receivable"])[0]  # past filed window
    assert found.status is FeatureStatus.FOUND
    assert missing.status is FeatureStatus.FEATURE_MISSING
    assert not_filed.status is FeatureStatus.PERIOD_NOT_FILED


def test_tool_definition_strict_enum_and_inputs_only():
    td = tool_definition(ITEM_KEYS)
    assert td["name"] == "balance_sheet_items"
    assert td["strict"] is True
    schema = td["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert schema["properties"]["items"]["items"]["enum"] == ITEM_KEYS
    for leaked in ("value", "status", "source", "query"):
        assert leaked not in schema["properties"]


def test_registry_dispatches_the_tool():
    binding = ToolBinding("balance_sheet_items", bs_fake, tool_definition(ITEM_KEYS),
                          parse_model_input, to_model_content)
    outcome = ToolRegistry([binding]).dispatch(
        "balance_sheet_items",
        {"ticker": "WBD", "report_date": "2025-06-30", "period_offset": 0, "items": ["accounts_payable"]},
    )
    assert outcome.raw_results[0].value == 3150.0
    assert "3150" in outcome.model_content


# --- ADR-9: the judge routes re-verification by source ----------------------


def test_map_grounds_mixed_structured_sources():
    """A pile from TWO structured tools grounds only when each item is re-fetched
    from its OWN backend (routed by provenance.source)."""
    features = fh_fake("WBD", date(2025, 6, 30), 0, ["ocf_to_net_income"])
    line_items = bs_fake("WBD", date(2025, 6, 30), 0, ["accounts_receivable"])
    judge = Judge(client=None, reverify={FH_SOURCE: fh_fake, BS_SOURCE: bs_fake})
    grounded, failed, _, _ = judge._check_grounding(features + line_items)
    assert grounded is True
    assert failed == []


def test_missing_backend_for_a_source_fails_grounding():
    """If the reverify map lacks the balance-sheet source, that item cannot be
    re-verified — the routing gap surfaces as ungrounded, not a silent pass."""
    line_items = bs_fake("WBD", date(2025, 6, 30), 0, ["accounts_receivable"])
    judge = Judge(client=None, reverify={FH_SOURCE: fh_fake})  # feature backend only
    grounded, failed, _, _ = judge._check_grounding(line_items)
    assert grounded is False
    assert "no reverify backend" in failed[0]


def test_routing_to_the_wrong_backend_is_caught():
    """The whole point of routing: a balance-sheet item re-fetched against the
    FEATURE backend won't match (that backend has no such line item), so a
    mis-routed map fails grounding rather than falsely passing."""
    line_items = bs_fake("WBD", date(2025, 6, 30), 0, ["accounts_receivable"])
    judge = Judge(client=None, reverify={BS_SOURCE: fh_fake})  # bs source -> WRONG backend
    grounded, failed, _, _ = judge._check_grounding(line_items)
    assert grounded is False


def test_single_callable_shorthand_still_works():
    """Backwards compatibility: one structured backend can still be a lone callable."""
    line_items = bs_fake("WBD", date(2025, 6, 30), 0, ["accounts_receivable"])
    judge = Judge(client=None, reverify=bs_fake)  # single callable, no map
    grounded, failed, _, _ = judge._check_grounding(line_items)
    assert grounded is True


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} balance-sheet tests passed.")


if __name__ == "__main__":
    _run()
