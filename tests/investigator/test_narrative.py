"""Narrative (unstructured) tool + semantic grounding tests (ADR-7).

Proves the sibling result type, the section-addressed fake, the generator/receipts
split for prose, and the judge's SECOND grounding head (model-checked semantic
support) — all deterministic, with a scripted judge client for the model call.

Run: `python tests/investigator/test_narrative.py`
"""
import sys
import threading
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.contracts import (  # noqa: E402
    GroundingMode,
    NarrativeProvenance,
    NarrativeResult,
    NarrativeStatus,
)
from qqq_scoring.investigator.tools.feature_history import tool_definition as feat_def  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402
from qqq_scoring.investigator.tools.narrative_sections import (  # noqa: E402
    SECTION_KEYS,
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.narrative_sections_fake import narrative_sections_fake  # noqa: E402


# --- scripted judge client (for the semantic grounding model call) ----------


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, name, input):
        self.id = "g1"
        self.name = name
        self.input = input


class _Response:
    def __init__(self, content):
        self.stop_reason = "tool_use"
        self.content = content


class _Messages:
    def __init__(self, c):
        self._c = c

    def create(self, **kwargs):
        self._c.calls.append(kwargs)
        return self._c._turn


class ScriptedJudge:
    def __init__(self, supported, unsupported=()):
        self.calls = []
        self.messages = _Messages(self)
        self._turn = _Response([_ToolUseBlock(
            "submit_grounding",
            # ADR-21 per-claim schema: gating breaches are violation-classed claims.
            {"claims": (
                [{"claim": u, "classification": "violation", "basis": ""} for u in unsupported]
                or [{"claim": "answer matches evidence", "classification": "supported", "basis": "test"}]
             ),
             "supported": supported, "reasoning": "test"},
        )])


class _SeqMessages:
    def __init__(self, c):
        self._c = c

    def create(self, **kwargs):
        with self._c._lock:   # 3 parallel answer-support votes (ADR-21 step 2b)
            self._c.calls.append(kwargs)
            turn = self._c._turns[self._c._i]
            self._c._i += 1
            return turn


class SeqJudge:
    """Returns scripted turns in order — for evaluate(): grounding votes, then C/O."""

    def __init__(self, turns):
        self.calls = []
        self._turns = turns
        self._i = 0
        self._lock = threading.Lock()
        self.messages = _SeqMessages(self)


def _prov(section="mdna"):
    return NarrativeProvenance(
        source="gs://.../AAPL_2025_10Q_narrative.json",
        ticker="AAPL",
        report_date=date(2025, 6, 30),
        form="10-Q",
        section=section,
        retrieved_at=datetime(2026, 7, 11, tzinfo=timezone.utc),
    )


# --- contract: sibling type, illegal states unspellable ---------------------


def test_found_narrative_requires_passage():
    try:
        NarrativeResult("mdna", NarrativeStatus.FOUND, None, _prov())
    except ValueError:
        return
    raise AssertionError("FOUND narrative with no passage should raise")


def test_absent_narrative_with_passage_rejected():
    try:
        NarrativeResult("mdna", NarrativeStatus.SECTION_ABSENT, "text", _prov())
    except ValueError:
        return
    raise AssertionError("SECTION_ABSENT carrying a passage should raise")


def test_section_absent_is_constructable():
    r = NarrativeResult("risk_factors", NarrativeStatus.SECTION_ABSENT, None, _prov("risk_factors"))
    assert r.passage is None


def test_grounding_modes_are_tagged():
    """The discriminator the judge dispatches on: narrative=SEMANTIC, structured=DETERMINISTIC."""
    narr = NarrativeResult("mdna", NarrativeStatus.FOUND, "text", _prov())
    struct = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])[0]
    assert narr.grounding_mode is GroundingMode.SEMANTIC
    assert struct.grounding_mode is GroundingMode.DETERMINISTIC


# --- tool definition + fake backend -----------------------------------------


def test_tool_definition_strict_and_enum_tracks_sections():
    td = tool_definition(SECTION_KEYS)
    assert td["strict"] is True
    schema = td["input_schema"]
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    assert schema["properties"]["sections"]["items"]["enum"] == SECTION_KEYS


def test_tool_definition_exposes_only_inputs():
    props = tool_definition(SECTION_KEYS)["input_schema"]["properties"]
    assert set(props) == {"ticker", "report_date", "form", "sections"}
    for leaked in ("passage", "status", "source", "retrieved_at"):
        assert leaked not in props


def test_fake_returns_passage_for_found_section():
    res = narrative_sections_fake("AAPL", date(2025, 6, 30), "10-Q", ["mdna"])
    assert res[0].status is NarrativeStatus.FOUND
    assert "temporary" in res[0].passage


def test_fake_distinguishes_section_absent_from_filing_not_found():
    absent = narrative_sections_fake("WBD", date(2025, 6, 30), "10-Q", ["risk_factors"])[0]
    missing = narrative_sections_fake("ZZZZ", date(2025, 6, 30), "10-Q", ["mdna"])[0]
    assert absent.status is NarrativeStatus.SECTION_ABSENT     # filing exists, section doesn't
    assert missing.status is NarrativeStatus.FILING_NOT_FOUND  # no filing at all


def test_to_model_content_shows_passage_hides_receipts():
    res = narrative_sections_fake("AAPL", date(2025, 6, 30), "10-Q", ["mdna"])
    content = to_model_content(res)
    assert "temporary" in content            # the payload IS shown
    for receipt in ("source", "retrieved_at", "gs://"):
        assert receipt not in content, f"receipt leaked to model: {receipt}"


# --- the judge's SEMANTIC grounding head ------------------------------------


def test_semantic_grounding_passes_when_model_says_supported():
    narrative = narrative_sections_fake("AAPL", date(2025, 6, 30), "10-Q", ["mdna"])
    judge = Judge(client=ScriptedJudge(supported=True), reverify=feature_history_fake)
    grounded, failed, _, _ = judge._check_grounding(narrative, "AAPL called it a temporary timing effect.")
    assert grounded is True
    assert failed == []


def test_semantic_grounding_fails_when_model_flags_a_claim():
    narrative = narrative_sections_fake("AAPL", date(2025, 6, 30), "10-Q", ["mdna"])
    judge = Judge(
        client=ScriptedJudge(supported=False, unsupported=["claims management admitted structural weakness"]),
        reverify=feature_history_fake,
    )
    grounded, failed, _, _ = judge._check_grounding(narrative, "AAPL management admitted structural weakness.")
    assert grounded is False
    assert failed == ["claims management admitted structural weakness"]


def test_mixed_evidence_uses_both_grounding_heads():
    """One number + one passage in one pile: the numeric re-verifies (real fake),
    the passage is model-checked (scripted). Both must pass for grounded=True."""
    structured = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    narrative = narrative_sections_fake("AAPL", date(2025, 6, 30), "10-Q", ["mdna"])
    judge = Judge(client=ScriptedJudge(supported=True), reverify=feature_history_fake)
    grounded, failed, _, _ = judge._check_grounding(structured + narrative, "recovered; timing was temporary")
    assert grounded is True
    assert failed == []


def test_evaluate_full_path_with_mixed_evidence():
    """The regression guard for the C/O grader crash: evaluate() over a MIXED pile
    (number + passage) must render both types and return a verdict, not AttributeError."""
    structured = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    narrative = narrative_sections_fake("AAPL", date(2025, 6, 30), "10-Q", ["mdna"])
    judge = Judge(
        client=SeqJudge(
            [_Response([_ToolUseBlock("submit_grounding",
                {"claims": [{"claim": "answer matches evidence",
                             "classification": "supported", "basis": "supported"}],
                 "reasoning": "supported"})])] * 3   # one per vote (ADR-21 step 2b)
            + [_Response([_ToolUseBlock("submit_judgment",
                {"confirm": "confirmed", "open_questions": False, "reasoning": "recovered, timing temporary"})])],
        ),
        reverify=feature_history_fake,
    )
    verdict = judge.evaluate(
        "Did cash conversion recover and did management explain it?",
        "OCF/NI recovered to 0.95; MD&A framed the dip as temporary timing.",
        structured + narrative,
    )
    assert verdict.grounded is True
    assert verdict.confirm.value == "confirmed"
    assert verdict.open_questions is False


# --- registry serves the narrative tool with zero registry changes ----------


def test_registry_dispatches_narrative_tool():
    binding = ToolBinding(
        name="narrative_sections",
        callable=narrative_sections_fake,
        definition=tool_definition(SECTION_KEYS),
        parse_input=parse_model_input,
        serialize=to_model_content,
    )
    reg = ToolRegistry([binding])
    outcome = reg.dispatch(
        "narrative_sections",
        {"ticker": "AAPL", "report_date": "2025-06-30", "form": "10-Q", "sections": ["mdna"]},
    )
    assert outcome.raw_results[0].status is NarrativeStatus.FOUND
    assert "temporary" in outcome.model_content
    # and it composes with a second (structured) tool in the same registry
    feat_binding = ToolBinding(
        name="feature_history",
        callable=feature_history_fake,
        definition=feat_def(["ocf_to_net_income"]),
        parse_input=lambda i: i,  # unused here
        serialize=lambda r: "",
    )
    two = ToolRegistry([binding, feat_binding])
    assert {d["name"] for d in two.tool_definitions()} == {"narrative_sections", "feature_history"}


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} narrative tests passed.")


if __name__ == "__main__":
    _run()
