"""Phase-2 harness tests: the tool-use loop CLOSES, deterministically, no key.

We inject a SCRIPTED fake client (mimics Anthropic's `messages.create`) so we can
assert the mechanical loop — round-trip a tool_use/tool_result and terminate —
without a live model. The AGENTIC property (real path-variance) is proved
separately by a live run against claude-opus-4-8; that's a demo, not a unit test.

Run: `python tests/investigator/test_loop_closes.py` (no pytest needed).
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.loop import (  # noqa: E402
    TerminalReason,
    run_investigation,
)
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.contracts import FeatureStatus  # noqa: E402
from qqq_scoring.investigator.tools.feature_history import (  # noqa: E402
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.feature_history_fake import (  # noqa: E402
    feature_history_fake,
)

FEATURE_KEYS = ["ocf_to_net_income", "net_margin", "debt_to_assets"]


def _registry() -> ToolRegistry:
    """Wire the fake data backend into a live-shaped registry."""
    binding = ToolBinding(
        name="feature_history",
        callable=feature_history_fake,      # <- fake backend; loop can't tell
        definition=tool_definition(FEATURE_KEYS),
        parse_input=parse_model_input,
        serialize=to_model_content,
    )
    return ToolRegistry([binding])


# --- scripted fake Anthropic client -----------------------------------------


class _TextBlock:
    type = "text"

    def __init__(self, text: str) -> None:
        self.text = text


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, id: str, name: str, input: dict) -> None:
        self.id = id
        self.name = name
        self.input = input


class _Response:
    def __init__(self, stop_reason: str, content: list) -> None:
        self.stop_reason = stop_reason
        self.content = content


class _Messages:
    def __init__(self, client: "ScriptedClient") -> None:
        self._c = client

    def create(self, **kwargs):
        return self._c._next(kwargs)


class ScriptedClient:
    """Returns pre-scripted responses turn by turn; records the args it saw."""

    def __init__(self, turns: list, repeat_last: bool = False) -> None:
        self._turns = turns
        self._i = 0
        self._repeat = repeat_last
        self.messages = _Messages(self)
        self.calls: list[dict] = []

    def _next(self, kwargs: dict):
        # Snapshot the messages LIST at call time — the loop reuses one list
        # object and mutates it, so storing the raw reference would show every
        # call the final state (that's a test-harness trap, not a loop bug).
        self.calls.append({**kwargs, "messages": list(kwargs["messages"])})
        if self._i < len(self._turns):
            turn = self._turns[self._i]
            self._i += 1
            return turn
        if self._repeat:
            return self._turns[-1]
        raise AssertionError("ScriptedClient ran out of scripted turns")


def _tool_use_turn(offset: int) -> _Response:
    return _Response(
        "tool_use",
        [
            _ToolUseBlock(
                "tu_1",
                "feature_history",
                {
                    "ticker": "AAPL",
                    "report_date": "2025-06-30",
                    "period_offset": offset,
                    "features": ["ocf_to_net_income"],
                },
            )
        ],
    )


# --- tests ------------------------------------------------------------------


def test_loop_closes_on_tool_use_then_stop():
    """The happy path: model calls a tool, reads the result, then ends the turn."""
    client = ScriptedClient(
        [
            _tool_use_turn(offset=1),
            _Response("end_turn", [_TextBlock("OCF/NI recovered to 0.95 next quarter.")]),
        ]
    )
    res = run_investigation(client, _registry(), "Did OCF/NI recover?")
    assert res.reason is TerminalReason.MODEL_STOPPED
    assert res.iterations == 2      # one tool turn + one closing turn
    assert res.tool_calls == 1
    assert "recovered" in res.final_text


def test_dispatch_hit_the_fake_backend_with_a_parsed_date():
    """Proves the parse adapter (string -> date) worked: the fake resolved the
    +1 quarter to 2025-09-30 and returned the real value. If the date parse had
    failed, the fake would never land on the right row."""
    client = ScriptedClient(
        [_tool_use_turn(offset=1), _Response("end_turn", [_TextBlock("done")])]
    )
    res = run_investigation(client, _registry(), "task")
    ev = res.evidence[0]
    assert ev.status is FeatureStatus.FOUND
    assert ev.value == 0.95
    assert ev.provenance.resolved_report_date == date(2025, 9, 30)


def test_model_never_sees_provenance_receipts():
    """ADR-5: the tool_result content handed back to the model carries the value
    but NOT the receipts (query / retrieved_at / accession / source)."""
    client = ScriptedClient(
        [_tool_use_turn(offset=1), _Response("end_turn", [_TextBlock("done")])]
    )
    run_investigation(client, _registry(), "task")
    # The 2nd create() call is the one that carries the tool_result back to the model.
    second_call_messages = client.calls[1]["messages"]
    tool_result = second_call_messages[-1]["content"][0]
    content = tool_result["content"]
    assert tool_result["type"] == "tool_result"
    assert "0.95" in content                     # the value IS shown
    assert "resolved_report_date" in content     # the landed quarter IS shown
    for receipt in ("query", "SELECT", "retrieved_at", "accession", "period_features"):
        assert receipt not in content, f"provenance leaked to model: {receipt!r}"


def test_full_provenance_retained_for_judge():
    """The receipts the model never saw ARE retained in evidence (for the judge)."""
    client = ScriptedClient(
        [_tool_use_turn(offset=1), _Response("end_turn", [_TextBlock("done")])]
    )
    res = run_investigation(client, _registry(), "task")
    prov = res.evidence[0].provenance
    assert "SELECT" in prov.query
    assert prov.accession_number == "0000320193-25-000073"


def test_loop_terminates_on_investigation_cap():
    """A model that never stops must be stopped by the budget (ADR-1), and that
    is a real terminal state, not an error."""
    client = ScriptedClient([_tool_use_turn(offset=1)], repeat_last=True)
    res = run_investigation(client, _registry(), "task", investigation_cap=3)
    assert res.reason is TerminalReason.CAP_REACHED
    assert res.iterations == 3
    assert res.tool_calls == 3


def test_period_not_filed_flows_through_as_null_not_zero():
    """Reaching past the filed calendar returns PERIOD_NOT_FILED with value null —
    the model must not read a missing future quarter as a 0/refutation."""
    outcome = _registry().dispatch(
        "feature_history",
        {"ticker": "AAPL", "report_date": "2025-06-30", "period_offset": 3, "features": ["ocf_to_net_income"]},
    )
    assert outcome.raw_results[0].status is FeatureStatus.PERIOD_NOT_FILED
    assert '"value": null' in outcome.model_content


def test_unknown_tool_name_fails_loud():
    """A tool_use for an unbound name is a bug, not 'no data' — dispatch raises."""
    try:
        _registry().dispatch("nonexistent_tool", {})
    except KeyError:
        return
    raise AssertionError("dispatch of an unknown tool name should raise KeyError")


def test_wall_clock_guard_bails_before_running():
    """eval #2: a passed deadline terminates CAP_REACHED before spending a turn."""
    client = ScriptedClient([_tool_use_turn(offset=1)], repeat_last=True)  # would loop forever
    res = run_investigation(client, _registry(), "task", max_seconds=0)
    assert res.reason is TerminalReason.CAP_REACHED
    assert res.iterations == 0
    assert client.calls == []   # guard tripped before any model call


def test_op_tracker_marks_and_flags_slow_calls():
    """2026-07-16 FTNT hang: the tracker names the call in flight and logs when a
    single call exceeds the per-turn share of max_seconds. No sleeps: a negative
    budget makes any real elapsed time 'over budget'."""
    from qqq_scoring.investigator.loop import OpTracker

    logged = []
    t = OpTracker(per_op_budget=-1.0, log=logged.append)
    t.begin("anthropic messages.create (turn 1)")
    assert t.last_operation == "anthropic messages.create (turn 1)"
    t.end()
    assert len(logged) == 1 and "messages.create" in logged[0]

    quiet = []
    t2 = OpTracker(per_op_budget=None, log=quiet.append)   # no max_seconds → no flagging
    t2.begin("tool dispatch feature_history")
    t2.end()
    assert quiet == []
    assert t2.last_operation == "tool dispatch feature_history"

    t3 = OpTracker(per_op_budget=-1.0, log=quiet.append)
    t3.end()   # end without begin is a no-op, not a crash
    assert quiet == []


def test_loop_tracks_in_flight_operations():
    """The loop marks each blocking call (model turn, tool dispatch) so a blown
    budget can name the suspect."""
    from qqq_scoring.investigator import loop as loop_mod

    ops: list[str] = []
    orig = loop_mod.OpTracker

    class Recording(orig):
        def begin(self, op):
            ops.append(op)
            super().begin(op)

    loop_mod.OpTracker = Recording
    try:
        client = ScriptedClient(
            [_tool_use_turn(offset=1), _Response("end_turn", [_TextBlock("done")])]
        )
        run_investigation(client, _registry(), "task", max_seconds=300)
    finally:
        loop_mod.OpTracker = orig
    assert any("messages.create" in o for o in ops)
    assert any("feature_history" in o for o in ops)


def test_max_tokens_truncated_tool_use_gets_a_tool_result():
    """Protocol guard: a max_tokens stop that carries a (truncated) tool_use block
    must be answered with an is_error tool_result — a bare text nudge would 400
    every subsequent request (live INSM failure, 2026-07-16). The model then
    re-issues the call and the loop completes normally."""
    truncated = _Response(
        "max_tokens",
        [_ToolUseBlock("tu_cut", "submit_findings", {"verdict_sentence": "half a sen"})],
    )
    client = ScriptedClient(
        [
            _tool_use_turn(offset=1),
            truncated,
            _Response("end_turn", [_TextBlock("done after retry")]),
        ]
    )
    res = run_investigation(client, _registry(), "Did OCF/NI recover?")
    assert res.reason is TerminalReason.MODEL_STOPPED
    # The user message immediately after the truncated assistant turn must
    # answer tu_cut.
    after = client.calls[2]["messages"]
    idx = next(i for i, m in enumerate(after)
               if m["role"] == "assistant" and any(
                   getattr(b, "id", None) == "tu_cut" for b in m["content"]))
    reply = after[idx + 1]
    assert reply["role"] == "user"
    blocks = reply["content"]
    assert any(b.get("tool_use_id") == "tu_cut" and b.get("is_error") for b in blocks)


def test_hard_deadline_bounds_a_hung_client_call():
    """2026-07-24 (STX r3): a messages.create call ran 3674.8s under
    `Anthropic(timeout=120)` — SDK/socket timeouts bound byte-gaps, not total call
    duration, so they are cooperative in exactly the way `max_seconds` was (FTNT
    lesson, one level down). The loop now enforces its own out-of-band deadline:
    a call that outlives the remaining `max_seconds` budget is abandoned in a
    daemon worker and the run terminates CAP_REACHED promptly."""
    import threading
    import time

    release = threading.Event()

    class _HungMessages:
        def create(self, **kwargs):
            release.wait(30)  # a "wedged" call: alive, yielding nothing
            return _tool_use_turn(0)

    class HungClient:
        messages = _HungMessages()

    t0 = time.monotonic()
    res = run_investigation(HungClient(), _registry(), "task", max_seconds=1.0)
    elapsed = time.monotonic() - t0
    release.set()  # unpark the abandoned worker so it exits promptly
    assert res.reason is TerminalReason.CAP_REACHED
    assert res.trusted is False
    assert elapsed < 5.0, f"hard deadline did not bind: {elapsed:.1f}s elapsed"


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} loop tests passed.")


if __name__ == "__main__":
    _run()
