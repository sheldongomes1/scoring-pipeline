"""Disambiguation-graph tests (ADR-8) — proposer parsing + the steering handoff.

Deterministic: a scripted client stands in for the proposer's model call and for
the generator; a FakeJudge stands in for termination. Proves the fan-out produces
branches carrying a `predicate`, and that steering into a branch runs the existing
loop and stamps the branch's status from the loop's terminal state.

Run: `python tests/investigator/test_graph.py`
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.graph import (  # noqa: E402
    Branch,
    BranchStatus,
    ExpansionBudget,
    Flag,
    InvestigationGraph,
    expand,
    propose_branches,
    propose_children,
    run_branch,
)
from qqq_scoring.investigator.judge import Confirm, JudgeVerdict  # noqa: E402
from qqq_scoring.investigator.loop import TerminalReason  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools.feature_history import (  # noqa: E402
    parse_model_input,
    to_model_content,
    tool_definition,
)
from qqq_scoring.investigator.tools.feature_history_fake import feature_history_fake  # noqa: E402


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, name, input):
        self.id = "b1"
        self.name = name
        self.input = input


class _TextBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _Response:
    def __init__(self, stop_reason, content):
        self.stop_reason = stop_reason
        self.content = content


class _Messages:
    def __init__(self, c):
        self._c = c

    def create(self, **kwargs):
        self._c.calls.append(kwargs)
        t = self._c._turns[min(self._c._i, len(self._c._turns) - 1)]
        self._c._i += 1
        return t


class ScriptedClient:
    def __init__(self, turns):
        self.calls = []
        self._turns = turns
        self._i = 0
        self.messages = _Messages(self)


class FakeJudge:
    def __init__(self, verdict):
        self._v = verdict

    def evaluate(self, predicate, answer, evidence, key_evidence=None):
        return self._v


def _findings_turn(sentence="Recovered — timing swing."):
    """A scripted submit_findings proposal (ADR-18) with one citable probe."""
    return _Response("tool_use", [_ToolUseBlock("submit_findings", {
        "verdict_sentence": sentence,
        "rationale": "the series recovered",
        "key_evidence": [{"source": "feature_history", "ticker": "WBD",
                          "report_date": "2025-06-30", "period_offset": 1,
                          "feature": "ocf_to_net_income", "value": 0.52}],
        "caveats": [],
    })])


FOUR_HYPOTHESES = {
    "hypotheses": [
        {"hypothesis": "Temporary working-capital timing", "rationale": "receivables build near quarter-end", "predicate": "Did OCF/NI recover next quarter, consistent with a one-off timing swing?"},
        {"hypothesis": "Revenue quality deteriorating", "rationale": "cash lagging reported earnings", "predicate": "Is cash conversion depressed because revenue quality is weakening?"},
        {"hypothesis": "Structural margin pressure", "rationale": "sector in secular decline", "predicate": "Is the low cash conversion driven by a durable margin/amortization problem?"},
        {"hypothesis": "One-off cash outflow", "rationale": "possible settlement or capex", "predicate": "Was the cash-flow dip caused by a discrete non-recurring outflow?"},
    ]
}


def _flag():
    return Flag("WBD", date(2025, 6, 30), "10-Q", "Driver: ocf_to_net_income fell to 0.50. key_question: did it recover?")


def _registry():
    return ToolRegistry([
        ToolBinding(
            name="feature_history",
            callable=feature_history_fake,
            definition=tool_definition(["ocf_to_net_income"]),
            parse_input=parse_model_input,
            serialize=to_model_content,
        )
    ])


def test_propose_branches_fans_out_into_hypotheses():
    client = ScriptedClient([_Response("tool_use", [_ToolUseBlock("propose_hypotheses", FOUR_HYPOTHESES)])])
    graph = propose_branches(client, _flag(), n=4)
    assert isinstance(graph, InvestigationGraph)
    assert [b.id for b in graph.branches] == ["h1", "h2", "h3", "h4"]
    assert all(b.status is BranchStatus.PROPOSED for b in graph.branches)


def test_each_branch_carries_a_predicate_the_steering_wire():
    client = ScriptedClient([_Response("tool_use", [_ToolUseBlock("propose_hypotheses", FOUR_HYPOTHESES)])])
    graph = propose_branches(client, _flag(), n=4)
    h2 = graph.get("h2")
    assert h2.hypothesis == "Revenue quality deteriorating"
    assert "revenue quality" in h2.predicate.lower()   # the QUESTION, present and specific
    assert h2.result is None                            # not investigated yet (propose-then-steer)


def test_proposer_slices_to_n():
    """A model over-producing hypotheses is trimmed to the requested N."""
    client = ScriptedClient([_Response("tool_use", [_ToolUseBlock("propose_hypotheses", FOUR_HYPOTHESES)])])
    graph = propose_branches(client, _flag(), n=2)
    assert len(graph.branches) == 2


def test_run_branch_steers_the_loop_and_stamps_status():
    """Steering into a branch runs the existing loop with the branch's predicate and
    records the terminal state back onto the branch."""
    client = ScriptedClient([_Response("tool_use", [_ToolUseBlock("propose_hypotheses", FOUR_HYPOTHESES)])])
    graph = propose_branches(client, _flag(), n=4)

    generator = ScriptedClient([_findings_turn()])
    judge = FakeJudge(JudgeVerdict(True, Confirm.CONFIRMED, False, "confirmed"))

    branch = run_branch(graph.get("h1"), graph.flag, generator, _registry(), judge)
    assert branch.status is BranchStatus.RESOLVED
    assert branch.result.reason is TerminalReason.RESOLVED
    # the loop was steered by the branch's predicate, not the generic flag
    assert branch.predicate in generator.calls[0]["messages"][0]["content"]


# --- Phase 4: recursive expansion control logic (ADR-10, no LLM) ------------


def _count(branch):
    return 1 + sum(_count(c) for c in branch.children)


def _max_depth(branch):
    return branch.depth if not branch.children else max(_max_depth(c) for c in branch.children)


def _fake_investigate(status=BranchStatus.RESOLVED):
    class _R:
        final_text = "finding"
    def _inv(branch, flag, generator, registry, judge, *, system=""):
        branch.status = status
        branch.result = _R()
        return branch
    return _inv


def _fake_propose(per_node=2, calls=None):
    def _prop(client, flag, parent, n):
        if calls is not None:
            calls.append(parent.id)
        return [
            Branch(id=f"{parent.id}.{i+1}", hypothesis="h", rationale="r", predicate="p", depth=parent.depth + 1)
            for i in range(per_node)
        ]
    return _prop


def _root():
    return Branch("h1", "hyp", "why", "predicate?", depth=0)


def test_expand_respects_max_depth():
    """The tree never grows deeper than max_depth even when children keep coming."""
    tree = expand(_root(), _flag(), None, _registry(), None,
                  max_depth=2, budget=ExpansionBudget(100), breadth=2,
                  _investigate=_fake_investigate(), _propose=_fake_propose(2))
    assert _max_depth(tree) == 2
    assert _count(tree) == 1 + 2 + 4   # root, 2 children, 4 grandchildren; grandchildren are leaves


def test_expand_respects_total_budget():
    """The global node budget bites before depth — the hard backstop against
    exponential growth (ADR-10)."""
    tree = expand(_root(), _flag(), None, _registry(), None,
                  max_depth=10, budget=ExpansionBudget(4), breadth=3,
                  _investigate=_fake_investigate(), _propose=_fake_propose(3))
    assert _count(tree) == 4   # exactly the budget, no more


def test_only_resolved_branches_spawn():
    """An INCONCLUSIVE branch is a leaf — the proposer is never even consulted."""
    calls = []
    tree = expand(_root(), _flag(), None, _registry(), None,
                  budget=ExpansionBudget(100),
                  _investigate=_fake_investigate(BranchStatus.INCONCLUSIVE),
                  _propose=_fake_propose(2, calls=calls))
    assert tree.children == []
    assert calls == []   # grounding-as-precondition: no follow-up on an unresolved foundation


def test_empty_children_is_the_semantic_leaf():
    """When the follow-up proposer returns nothing, the branch is a leaf (converged)."""
    def _no_children(client, flag, parent, n):
        return []
    tree = expand(_root(), _flag(), None, _registry(), None,
                  budget=ExpansionBudget(100),
                  _investigate=_fake_investigate(), _propose=_no_children)
    assert tree.status is BranchStatus.RESOLVED
    assert tree.children == []


def test_propose_children_ids_encode_lineage():
    """Child ids carry the parent path (h2 -> h2.1) and depth increments."""
    parent = Branch("h2", "hyp", "why", "pred?", status=BranchStatus.RESOLVED, depth=0)
    parent.result = type("R", (), {"final_text": "content amortization is the driver"})()
    client = ScriptedClient([_Response("tool_use", [_ToolUseBlock("propose_hypotheses", {
        "hypotheses": [{"hypothesis": "amortization aggressive?", "rationale": "vs peers", "predicate": "is the schedule aggressive?"}]
    })])])
    children = propose_children(client, _flag(), parent, n=3)
    assert children[0].id == "h2.1"
    assert children[0].depth == 1


# --- audit #11: recursive graph.get -----------------------------------------


def test_get_finds_nested_branch_and_raises_on_missing():
    root = Branch("h2", "hyp", "why", "pred?", depth=0)
    root.children.append(Branch("h2.1", "c", "cw", "cp?", depth=1))
    graph = InvestigationGraph(flag=_flag(), branches=[root])
    assert graph.get("h2.1").id == "h2.1"     # recurses into the tree (was top-level only)
    try:
        graph.get("nope")
    except KeyError:
        return
    raise AssertionError("get() of a missing id should raise KeyError, not StopIteration")


# --- audit #6: CAP_REACHED is CAPPED (not INCONCLUSIVE) + verdict carried ----


def test_capped_branch_is_distinct_and_carries_verdict():
    from qqq_scoring.investigator.judge import Confirm
    client = ScriptedClient([_findings_turn("prelim")])   # auto-repeats last turn
    open_forever = JudgeVerdict(True, Confirm.INDETERMINATE, True, "still open")
    branch = Branch("h1", "hyp", "why", "pred?", depth=0)
    run_branch(branch, _flag(), client, _registry(), FakeJudge(open_forever))   # never resolves → ceiling
    assert branch.result.reason is TerminalReason.CAP_REACHED
    assert branch.status is BranchStatus.CAPPED           # NOT laundered into INCONCLUSIVE
    assert branch.result.verdict is not None              # last verdict preserved, not dropped


# --- audit #5: breadth-first expansion doesn't starve siblings --------------


def test_breadth_first_investigates_siblings_before_descending():
    tree = expand(_root(), _flag(), None, _registry(), None,
                  max_depth=3, budget=ExpansionBudget(3), breadth=2,
                  _investigate=_fake_investigate(), _propose=_fake_propose(2))
    assert len(tree.children) == 2                        # both level-1 siblings investigated
    assert all(c.status is BranchStatus.RESOLVED for c in tree.children)
    assert _max_depth(tree) == 1                          # budget spent on breadth, not one deep chain


def test_budget_capped_children_recorded_as_proposed():
    tree = expand(_root(), _flag(), None, _registry(), None,
                  max_depth=3, budget=ExpansionBudget(2), breadth=2,
                  _investigate=_fake_investigate(), _propose=_fake_propose(2))
    assert len(tree.children) == 2                        # attached, not dropped...
    assert sorted(c.status.value for c in tree.children) == ["proposed", "resolved"]  # ...one budget-capped


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} graph tests passed.")


if __name__ == "__main__":
    _run()
