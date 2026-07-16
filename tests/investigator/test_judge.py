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
        self.seen = []   # (answer, key_evidence) pairs — lets tests assert the wiring

    def evaluate(self, predicate, answer, evidence, key_evidence=None):
        self.seen.append((answer, key_evidence))
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


# A valid key_evidence citation of the fake backend's real probe:
# (AAPL, anchor 2025-06-30, offset +1) -> ocf_to_net_income 0.95 @ 2025-09-30.
_KE = {"source": "feature_history", "ticker": "AAPL", "report_date": "2025-06-30",
       "period_offset": 1, "feature": "ocf_to_net_income", "value": 0.95}


def _findings(verdict_sentence="recovered", key_evidence=(_KE,), caveats=(), **extra):
    """A scripted submit_findings turn — ADR-18: the structured termination proposal."""
    payload = {
        "verdict_sentence": verdict_sentence,
        "rationale": "the fetched series shows it",
        "key_evidence": list(key_evidence),
        "caveats": list(caveats),
        **extra,
    }
    return _Response("tool_use", [_ToolUseBlock("f1", "submit_findings", payload)])


def _tampered_result(value):
    """A FOUND result whose value does NOT match what the source will re-fetch.

    The request (anchor 2025-06-30, offset +1) replays to the real fixture value
    0.95 @ 2025-09-30; a tampered `value` (0.60) mismatches → grounding fails."""
    prov = Provenance(
        source="qqq_finance.period_features",   # ADR-9 logical source (was the stale fixture name)
        ticker="AAPL",
        resolved_report_date=date(2025, 9, 30),
        requested_report_date=date(2025, 6, 30),
        requested_offset=1,
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
    grounded, failed, _, _ = judge._check_grounding(evidence)
    assert grounded is True
    assert failed == []


def test_grounding_fails_for_tampered_value():
    """A cited figure the source does not confirm (0.60 vs real 0.95) is caught —
    deterministically, by re-fetch + ==, with no model in the loop."""
    judge = Judge(client=None, reverify=feature_history_fake)
    grounded, failed, _, _ = judge._check_grounding([_tampered_result(0.60)])
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
    """When integrity passes, the judge model runs TWICE: answer-support (ADR-13),
    then submit_judgment for C/O. Both turns scripted."""
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge_client = ScriptedClient([
        _Response("tool_use", [_ToolUseBlock("g1", "submit_grounding",
            {"supported": True, "violations": [], "advisories": [], "reasoning": "0.95 is in the evidence"})]),
        _Response("tool_use", [_ToolUseBlock("j1", "submit_judgment",
            {"confirm": "confirmed", "open_questions": False, "reasoning": "recovered to 0.95"})]),
    ])
    judge = Judge(client=judge_client, reverify=feature_history_fake)
    verdict = judge.evaluate("did ocf_to_net_income recover?", "yes, to 0.95", evidence)
    assert verdict.grounded is True
    assert verdict.confirm is Confirm.CONFIRMED
    assert verdict.open_questions is False


# --- Loop routing: every terminal path (FakeJudge, no model) ----------------


def _resolved_v():
    return JudgeVerdict(True, Confirm.CONFIRMED, False, "confirmed")


def _refuted_v():
    return JudgeVerdict(True, Confirm.REFUTED, False, "hypothesis rejected on evidence")


def _inconclusive_v():
    return JudgeVerdict(True, Confirm.INDETERMINATE, False, "ambiguous")


def _open_v():
    return JudgeVerdict(True, Confirm.INDETERMINATE, True, "need next quarter")


def _ungrounded_v():
    return JudgeVerdict(False, None, None, "did not verify", ungrounded_items=("x@y",))


def test_loop_stops_resolved():
    client = ScriptedClient([_findings("recovered")])
    judge = FakeJudge([_resolved_v()])
    res = run_investigation(client, _registry(), "task", judge=judge)
    assert res.reason is TerminalReason.RESOLVED
    assert res.verdict.confirm is Confirm.CONFIRMED
    # ADR-18: the structured findings ride on the result and the judge graded the
    # SERIALIZED fields (with the key_evidence handed to the integrity path).
    assert res.verdict_sentence == "recovered"
    assert res.rationale == "the fetched series shows it"
    assert res.key_evidence == (_KE,)
    assert res.caveats == ()
    answer, key_evidence = judge.seen[0]
    assert "VERDICT: recovered" in answer and "RATIONALE:" in answer
    assert key_evidence == [_KE]


def test_refuted_hypothesis_resolves_not_inconclusive():
    """ADR-11: a hypothesis REFUTED on the evidence is RESOLVED, not INCONCLUSIVE —
    the calibration bug the live tree run exposed (rejected branch graded inconclusive)."""
    client = ScriptedClient([_findings("hypothesis rejected on the evidence")])
    res = run_investigation(client, _registry(), "task", judge=FakeJudge([_refuted_v()]))
    assert res.reason is TerminalReason.RESOLVED
    assert res.verdict.confirm is Confirm.REFUTED


def test_loop_stops_inconclusive():
    client = ScriptedClient([_findings("ambiguous")])
    res = run_investigation(client, _registry(), "task", judge=FakeJudge([_inconclusive_v()]))
    assert res.reason is TerminalReason.INCONCLUSIVE


def test_loop_continues_on_open_questions_then_resolves():
    """Open questions re-inject guidance and the loop continues; a second proposal
    that resolves ends it. Proves the judge — not the model — owns the stop."""
    client = ScriptedClient([_findings("prelim"), _findings("final")])
    res = run_investigation(
        client, _registry(), "task", judge=FakeJudge([_open_v(), _resolved_v()])
    )
    assert res.reason is TerminalReason.RESOLVED
    assert res.iterations == 2
    assert res.verdict_sentence == "final"   # the ACCEPTED proposal's findings win
    # The first proposal did NOT stop the loop — the continue prompt rode back as
    # the submit_findings tool_result (the API requires one per tool_use).
    assert any(
        m["role"] == "user" and isinstance(m["content"], list)
        and any(b.get("type") == "tool_result" and "open questions" in str(b.get("content", ""))
                for b in m["content"])
        for m in res.messages
    )


def test_loop_abandons_after_repair_cap():
    """Persistent (repairable) grounding failure exhausts repair_cap and abandons (ADR-1)."""
    client = ScriptedClient([_findings("ungrounded")], repeat_last=True)
    res = run_investigation(
        client, _registry(), "task", judge=FakeJudge([_ungrounded_v()]), repair_cap=1
    )
    assert res.reason is TerminalReason.ABANDONED
    assert res.iterations == 2   # 1st proposal repairs, 2nd exceeds repair_cap


def _det_ungrounded_v():
    return JudgeVerdict(False, None, None, "integrity re-check failed",
                        ("ocf@2025-09-30",), deterministic_failure=True)


def test_deterministic_failure_abandons_immediately(_cap=3):
    """ADR-13 (#3): an INTEGRITY failure is unrepairable, so the loop terminates at
    once — it does NOT burn repair_cap on a retry that is guaranteed to re-fail."""
    client = ScriptedClient([_findings("ungrounded")], repeat_last=True)
    res = run_investigation(
        client, _registry(), "task", judge=FakeJudge([_det_ungrounded_v()]), repair_cap=_cap
    )
    assert res.reason is TerminalReason.ABANDONED
    assert res.iterations == 1   # immediate — no repair loop despite repair_cap=3


def test_answer_support_catches_numeric_hallucination_numbers_only():
    """ADR-13 (#2): the answer-support head runs for NUMBERS-ONLY investigations —
    a figure the generator never fetched is caught, not just narrative overreach."""
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])  # FOUND 0.95
    client = ScriptedClient([_Response("tool_use", [_ToolUseBlock("g1", "submit_grounding",
        {"supported": False, "violations": ["answer says 0.55 but evidence shows 0.95"],
         "advisories": [], "reasoning": "fabricated figure"})])])
    judge = Judge(client=client, reverify=feature_history_fake)
    grounded, failed, det, _ = judge._check_grounding(evidence, "OCF/NI was 0.55 — collapse confirmed.")
    assert grounded is False
    assert det is False               # a mis-statement, not an integrity failure → repairable
    assert client.calls               # the model WAS consulted even for a numbers-only answer
    assert "0.55" in failed[0]


def test_grounding_dedupes_repeated_evidence():
    """audit #12: the same fact appearing twice in evidence is re-verified ONCE."""
    calls = []
    def counting(ticker, rd, off, feats):
        calls.append((ticker, rd, off, tuple(feats)))
        return feature_history_fake(ticker, rd, off, feats)
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    grounded, failed, _, _ = Judge(client=None, reverify=counting)._check_grounding(ev + ev)  # duped
    assert grounded is True
    assert len(calls) == 1            # verified once despite the duplicate


def test_judge_no_payload_fails_closed():
    """audit #10: a broken C/O grade (no tool payload) terminates INCONCLUSIVE with
    open_questions=False — it does NOT loop the budget away rubber-stamping."""
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    client = ScriptedClient([
        _Response("tool_use", [_ToolUseBlock("g", "submit_grounding",
            {"supported": True, "violations": [], "advisories": [], "reasoning": "ok"})]),  # answer-support passes
        _Response("end_turn", [_TextBlock("no tool call this time")]),            # C/O grade: no payload
    ])
    v = Judge(client=client, reverify=feature_history_fake).evaluate(
        "did it recover?", "recovered to 0.95", ev)
    assert v.grounded is True
    assert v.confirm is Confirm.INDETERMINATE
    assert v.open_questions is False   # fail-closed — stop, don't spin


def test_resolved_and_inconclusive_are_trusted():
    """Fable health check: only clean grounding-passed terminals are trusted."""
    for verdict, reason in [(_resolved_v(), TerminalReason.RESOLVED),
                            (_inconclusive_v(), TerminalReason.INCONCLUSIVE)]:
        res = run_investigation(ScriptedClient([_findings("answer")]), _registry(),
                                "task", judge=FakeJudge([verdict]))
        assert res.reason is reason
        assert res.trusted is True


def test_capped_result_is_not_trusted():
    """The enforcement seam: a budget-exhausted answer never passed grounding cleanly,
    so it must NOT be surfaced as a verified finding (Fable health check)."""
    client = ScriptedClient([_findings("prelim")], repeat_last=True)
    res = run_investigation(client, _registry(), "task",
                            judge=FakeJudge([_open_v()]), investigation_cap=2)
    assert res.reason is TerminalReason.CAP_REACHED
    assert res.trusted is False


def test_abandoned_result_is_not_trusted():
    client = ScriptedClient([_findings("ungrounded")], repeat_last=True)
    res = run_investigation(client, _registry(), "task",
                            judge=FakeJudge([_ungrounded_v()]), repair_cap=1)
    assert res.reason is TerminalReason.ABANDONED
    assert res.trusted is False


def test_no_judge_preserves_model_stopped():
    """Regression: with no judge injected, ADR-4 behaviour is unchanged."""
    client = ScriptedClient([_end_turn("done")])
    res = run_investigation(client, _registry(), "task")  # judge=None
    assert res.reason is TerminalReason.MODEL_STOPPED
    assert res.verdict is None


# --- ADR-18: submit_findings termination -------------------------------------


def test_findings_without_key_evidence_fails_closed_to_untrusted():
    """ADR-18: an answer citing nothing is unverifiable by construction — the
    proposal never reaches the judge, and a persistent citation-free model ends
    ABANDONED (never trusted)."""
    judge = FakeJudge([_resolved_v()])   # would accept — must never be consulted
    client = ScriptedClient([_findings("no citations", key_evidence=())], repeat_last=True)
    res = run_investigation(client, _registry(), "task", judge=judge, repair_cap=1)
    assert res.reason is TerminalReason.ABANDONED
    assert res.trusted is False
    assert judge.seen == []   # the judge was never handed the citation-free proposal


def test_missing_key_evidence_key_is_parsed_as_empty_and_fails_closed():
    """'required' is a hint, not a runtime contract (2026-07-14): a payload that
    OMITS key_evidence entirely parses to [] and takes the same fail-closed path."""
    payload = {"verdict_sentence": "v", "rationale": "r"}   # no key_evidence, no caveats
    client = ScriptedClient(
        [_Response("tool_use", [_ToolUseBlock("f1", "submit_findings", payload)])],
        repeat_last=True,
    )
    res = run_investigation(client, _registry(), "task", judge=FakeJudge([_resolved_v()]), repair_cap=0)
    assert res.reason is TerminalReason.ABANDONED
    assert res.caveats == ()   # missing caveats defaulted to empty, no KeyError


def test_prose_end_turn_with_judge_is_nudged_not_graded():
    """ADR-18: end_turn without submit_findings is the model stalling — the loop
    nudges it toward the tool; the judge grades only structured proposals."""
    judge = FakeJudge([_resolved_v()])
    client = ScriptedClient([_end_turn("prose only"), _findings("now structured")])
    res = run_investigation(client, _registry(), "task", judge=judge)
    assert res.reason is TerminalReason.RESOLVED
    assert len(judge.seen) == 1   # only the structured proposal was graded
    assert any(m["role"] == "user" and isinstance(m["content"], str) and "submit_findings" in m["content"]
               for m in res.messages)


def test_model_never_calls_submit_findings_hits_the_cap_untrusted():
    """ADR-18: a model that only ever emits prose can never terminate trusted —
    the ceiling trips CAP_REACHED and the judge is never consulted."""
    judge = FakeJudge([_resolved_v()])
    client = ScriptedClient([_end_turn("just prose")], repeat_last=True)
    res = run_investigation(client, _registry(), "task", judge=judge, investigation_cap=3)
    assert res.reason is TerminalReason.CAP_REACHED
    assert res.trusted is False
    assert judge.seen == []


def test_findings_with_judge_none_is_model_stopped_with_fields():
    """No-judge path: a submit_findings proposal is terminal (MODEL_STOPPED) and
    the structured fields are still captured."""
    client = ScriptedClient([_findings("done", caveats=("one caveat",))])
    res = run_investigation(client, _registry(), "task")   # judge=None
    assert res.reason is TerminalReason.MODEL_STOPPED
    assert res.verdict_sentence == "done"
    assert res.caveats == ("one caveat",)
    assert res.trusted is False


def test_submit_findings_tool_offered_alongside_data_tools():
    """The generator's tool menu carries submit_findings (ADR-18)."""
    client = ScriptedClient([_end_turn("done")])
    run_investigation(client, _registry(), "task")
    names = [t["name"] for t in client.calls[0]["tools"]]
    assert "feature_history" in names and "submit_findings" in names


# --- ADR-18: key_evidence INTEGRITY verification (real Judge, no C/O model) ---


def _passing_support_client():
    """A judge client whose answer-support head always passes and whose C/O grade
    confirms — isolates the key_evidence citation check."""
    return ScriptedClient([
        _Response("tool_use", [_ToolUseBlock("g1", "submit_grounding",
            {"supported": True, "violations": [], "advisories": [], "reasoning": "ok"})]),
        _Response("tool_use", [_ToolUseBlock("j1", "submit_judgment",
            {"confirm": "confirmed", "open_questions": False, "reasoning": "ok"})]),
    ], repeat_last=True)


def test_key_evidence_tampered_value_fails_grounding():
    """A citation whose value does not match the FETCHED evidence fails — repairable
    (a generator mis-citation), not a deterministic/source failure."""
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])  # 0.95
    judge = Judge(client=_passing_support_client(), reverify=feature_history_fake)
    tampered = {**_KE, "value": 0.60}
    v = judge.evaluate("did it recover?", "VERDICT: recovered", evidence, key_evidence=[tampered])
    assert v.grounded is False
    assert v.deterministic_failure is False
    assert any("0.6" in item for item in v.ungrounded_items)


def test_key_evidence_citing_unfetched_probe_fails_grounding():
    """A citation of a probe the loop never made fails, even if it would be true at
    source — an answer may only cite evidence it actually fetched (ADR-18)."""
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge = Judge(client=_passing_support_client(), reverify=feature_history_fake)
    unfetched = {**_KE, "period_offset": 0, "value": 0.55}   # never probed this run
    v = judge.evaluate("did it recover?", "VERDICT: recovered", evidence, key_evidence=[unfetched])
    assert v.grounded is False
    assert any("never fetched" in item for item in v.ungrounded_items)


def test_key_evidence_valid_citation_passes_grounding():
    evidence = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge = Judge(client=_passing_support_client(), reverify=feature_history_fake)
    v = judge.evaluate("did it recover?", "VERDICT: recovered", evidence, key_evidence=[_KE])
    assert v.grounded is True
    assert v.confirm is Confirm.CONFIRMED


# --- ADR-19: two-tier answer-support (violations gate, advisories inform) ----


def _support_client(payload):
    return ScriptedClient([_Response("tool_use", [_ToolUseBlock("g1", "submit_grounding", payload)])])


def test_violations_gate_the_run():
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge = Judge(client=_support_client(
        {"supported": False, "violations": ["figure 0.55 not in evidence"], "advisories": [], "reasoning": "x"}),
        reverify=feature_history_fake)
    grounded, failed, det, advisories = judge._check_grounding(ev, "it was 0.55")
    assert grounded is False and det is False
    assert failed == ["figure 0.55 not in evidence"]


def test_advisories_do_not_gate_and_ride_the_verdict():
    """ADR-19: style notes on supported facts inform, never gate — they flow onto
    the JudgeVerdict for the UI's 'judge notes'."""
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    client = ScriptedClient([
        _Response("tool_use", [_ToolUseBlock("g1", "submit_grounding",
            {"supported": True, "violations": [],
             "advisories": ["hedged inference is labeled as inference"], "reasoning": "ok"})]),
        _Response("tool_use", [_ToolUseBlock("j1", "submit_judgment",
            {"confirm": "confirmed", "open_questions": False, "reasoning": "ok"})]),
    ])
    v = Judge(client=client, reverify=feature_history_fake).evaluate(
        "did it recover?", "recovered to 0.95 (consistent with, not confirmed)", ev)
    assert v.grounded is True
    assert v.advisories == ("hedged inference is labeled as inference",)


def test_bool_list_disagreement_is_not_grounded():
    """supported=true with a non-empty violations list is a judge inconsistency —
    fail closed (ADR-19 gate condition: supported AND empty violations)."""
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    judge = Judge(client=_support_client(
        {"supported": True, "violations": ["quote appears in no passage"], "advisories": [], "reasoning": "x"}),
        reverify=feature_history_fake)
    grounded, failed, _, _ = judge._check_grounding(ev, "management said 'we are doomed'")
    assert grounded is False
    assert failed == ["quote appears in no passage"]


def test_missing_supported_or_violations_fails_closed():
    """'required' is a hint (2026-07-14): a payload missing `supported` or missing
    `violations` cannot be told apart from a dropped field — not grounded."""
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    for payload in ({"violations": [], "advisories": [], "reasoning": "x"},      # no supported
                    {"supported": True, "advisories": [], "reasoning": "x"}):    # no violations
        judge = Judge(client=_support_client(payload), reverify=feature_history_fake)
        grounded, failed, _, _ = judge._check_grounding(ev, "recovered to 0.95")
        assert grounded is False
        assert any("incomplete" in f for f in failed)


def test_missing_advisories_defaults_to_empty():
    ev = feature_history_fake("AAPL", date(2025, 6, 30), 1, ["ocf_to_net_income"])
    client = ScriptedClient([
        _Response("tool_use", [_ToolUseBlock("g1", "submit_grounding",
            {"supported": True, "violations": [], "reasoning": "ok"})]),   # advisories omitted
        _Response("tool_use", [_ToolUseBlock("j1", "submit_judgment",
            {"confirm": "confirmed", "open_questions": False, "reasoning": "ok"})]),
    ])
    v = Judge(client=client, reverify=feature_history_fake).evaluate(
        "did it recover?", "recovered to 0.95", ev)
    assert v.grounded is True
    assert v.advisories == ()


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} judge tests passed.")


if __name__ == "__main__":
    _run()
