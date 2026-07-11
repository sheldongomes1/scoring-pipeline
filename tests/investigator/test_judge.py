"""Judge tests (ADR-1 rubric, ADR-6 wiring) — deterministic, no live model.

Two layers:
  * Judge internals — grounding is real code (re-fetch + ==); the model half is
    exercised with a scripted judge client.
  * Loop routing — a FakeJudge returns canned verdicts so we can prove every
    terminal path (RESOLVED / INCONCLUSIVE / ABANDONED / continue / no-judge)
    without any model at all.

Run: `python tests/investigator/test_judge.py`
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Confirm, Judge, JudgeVerdict  # noqa: E402
from qqq_scoring.investigator.loop import TerminalReason, run_investigation  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.contracts import (  # noqa: E402
    FeatureResult,
    FeatureStatus,
    Provenance,
)
from qqq_scoring.investigator.tools.feature_history import (  # noqa: E402
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402

FEATURE_KEYS = ["ocf_to_net_income", "net_margin", "debt_to_assets", "accrual_ratio"]


# --- minimal scripted clients / blocks --------------------------------------


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id, name, input):
        self.id = id
        self.name = name
        self.input = input


class _Response:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _Messages:
    def __init__(self, client):
        self._c = client

    def create(self, **kwargs):
        return self._c._next(kwargs)


class ScriptedClient:
    """Generator OR judge fake. Returns scripted responses; repeats last if asked."""

    def __init__(self, turns, repeat_last=False):
        self._turns = turns
        self._i = 0
        self._repeat = repeat_last
        self.messages = _Messages(self)
        self.calls = []

    def _next(self, kwargs):
        self.calls.append({**kwargs, "messages": list(kwargs.get("messages", []))})
        if self._i < len(self._turns):
            t = self._turns[self._i]
            self._i += 1
            return t
        if self._repeat:
            return self._turns[-1]
        raise AssertionError("ScriptedClient ran out of scripted turns")


class ExplodingClient:
    """A judge model client that must NOT be called (proves G short-circuits)."""

    class _M:
        def create(self, **kwargs):
            raise AssertionError("judge model was called despite ungrounded evidence")

    messages = _M()


class FakeJudge:
    """Duck-typed judge: returns canned verdicts so loop routing is testable alone."""

    def __init__(self, verdicts):
        self._v = list(verdicts)
        self._i = 0

    def evaluate(self, predicate, answer, evidence):
        v = self._v[min(self._i, len(self._v) - 1)]
        self._i += 1
        return v


def _registry():
    binding = ToolBinding(
        name="feature_history",
        callable=feature_history_fake,
        definition=tool_definition(FEATURE_KEYS),
        parse_input=parse_model_input,
        serialize=to_model_content,
    )
    return ToolRegistry([binding])


def _end_turn(text="done"):
    return _Response("end_turn", [_TextBlock(text)])


def _tampered_result(value):
    """A FOUND result whose value does NOT match what the source will re-fetch."""
    prov = Provenance(
        source="qqq_finance.period_features (in-memory fixture)",
        ticker="AAPL",
        resolved_report_date=date(2025, 9, 30),
        query="SELECT ocf_to_net_income FROM period_features WHERE ...",
        retrieved_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
        accession_number="0000320193-25-000073",
    )
    return FeatureResult("ocf_to_net_income", FeatureStatus.FOUND, value, prov)


# --- Judge internals: grounding is real ------------------------------------


def test_grounding_passes_for_authentic_evidence():
    """Evidence produced by the backend re-verifies against that same source."""
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge = Judge(client=None, reverify=feature_history_fake)
    grounded, failed = judge._check_grounding(evidence)
    assert grounded is True
    assert failed == []


def test_grounding_fails_for_tampered_value():
    """A cited figure the source does not confirm (0.60 vs real 0.95) is caught —
    deterministically, by re-fetch + ==, with no model in the loop."""
    judge = Judge(client=None, reverify=feature_history_fake)
    grounded, failed = judge._check_grounding([_tampered_result(0.60)])
    assert grounded is False
    assert failed == ["ocf_to_net_income@2025-09-30"]


def test_ungrounded_evidence_short_circuits_the_model():
    """ADR-1: grounding is a precondition. If G fails, the judge MODEL is never
    consulted (C/O would be untrustworthy) — proven by an exploding client."""
    judge = Judge(client=ExplodingClient(), reverify=feature_history_fake)
    verdict = judge.evaluate("did it recover?", "it was 0.60", [_tampered_result(0.60)])
    assert verdict.grounded is False
    assert verdict.confirm is None          # not consulted
    assert verdict.open_questions is None   # not consulted


def test_grounded_evidence_gets_a_model_grade():
    """When G passes, the judge model is called and its submit_judgment drives C/O."""
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge_client = ScriptedClient(
        [_Response("tool_use", [_ToolUseBlock("j1", "submit_judgment",
            {"confirm": "resolved", "open_questions": False, "reasoning": "recovered to 0.95"})])]
    )
    judge = Judge(client=judge_client, reverify=feature_history_fake)
    verdict = judge.evaluate("did ocf_to_net_income recover?", "yes, to 0.95", evidence)
    assert verdict.grounded is True
    assert verdict.confirm is Confirm.RESOLVED
    assert verdict.open_questions is False


# --- Loop routing: every terminal path (FakeJudge, no model) ----------------


def _resolved_v():
    return JudgeVerdict(True, Confirm.RESOLVED, False, "resolved")


def _inconclusive_v():
    return JudgeVerdict(True, Confirm.UNRESOLVED, False, "ambiguous")


def _open_v():
    return JudgeVerdict(True, Confirm.UNRESOLVED, True, "need next quarter")


def _ungrounded_v():
    return JudgeVerdict(False, None, None, "did not verify", ungrounded_items=("x@y",))


def test_loop_stops_resolved():
    client = ScriptedClient([_end_turn("recovered")])
    res = run_investigation(client, _registry(), "task", judge=FakeJudge([_resolved_v()]))
    assert res.reason is TerminalReason.RESOLVED
    assert res.verdict.confirm is Confirm.RESOLVED


def test_loop_stops_inconclusive():
    client = ScriptedClient([_end_turn("ambiguous")])
    res = run_investigation(client, _registry(), "task", judge=FakeJudge([_inconclusive_v()]))
    assert res.reason is TerminalReason.INCONCLUSIVE


def test_loop_continues_on_open_questions_then_resolves():
    """Open questions re-inject guidance and the loop continues; a second proposal
    that resolves ends it. Proves the judge — not the model — owns the stop."""
    client = ScriptedClient([_end_turn("prelim"), _end_turn("final")])
    res = run_investigation(
        client, _registry(), "task", judge=FakeJudge([_open_v(), _resolved_v()])
    )
    assert res.reason is TerminalReason.RESOLVED
    assert res.iterations == 2
    # The model's first end_turn did NOT stop the loop — a continue prompt followed.
    assert any(m["role"] == "user" and isinstance(m["content"], str) and "open questions" in m["content"]
               for m in res.messages)


def test_loop_abandons_after_repair_cap():
    """Persistent grounding failure exhausts repair_cap and abandons (ADR-1)."""
    client = ScriptedClient([_end_turn("ungrounded")], repeat_last=True)
    res = run_investigation(
        client, _registry(), "task", judge=FakeJudge([_ungrounded_v()]), repair_cap=1
    )
    assert res.reason is TerminalReason.ABANDONED
    assert res.iterations == 2   # 1st proposal repairs, 2nd exceeds repair_cap


def test_no_judge_preserves_model_stopped():
    """Regression: with no judge injected, ADR-4 behaviour is unchanged."""
    client = ScriptedClient([_end_turn("done")])
    res = run_investigation(client, _registry(), "task")  # judge=None
    assert res.reason is TerminalReason.MODEL_STOPPED
    assert res.verdict is None


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} judge tests passed.")


if __name__ == "__main__":
    _run()
