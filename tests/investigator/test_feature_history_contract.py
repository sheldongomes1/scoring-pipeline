"""Contract tests for the feature_history tool (ADR-2).

These test the *contract*, not BigQuery — the stub raises NotImplementedError for
the data path. What we can (and must) verify now: the provenance envelope is
constructable, and the status/value invariant makes illegal states unspellable.

Runs with plain `python tests/investigator/test_feature_history_contract.py`
(no pytest dependency) or under pytest if installed.
"""
import sys
from datetime import date, datetime
from pathlib import Path

# Make src/ importable without an install step.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.tools.contracts import (  # noqa: E402
    FeatureResult,
    FeatureStatus,
    Provenance,
)
from qqq_scoring.investigator.tools.feature_history import feature_history  # noqa: E402


def _provenance(resolved: date = date(2025, 9, 30)) -> Provenance:
    return Provenance(
        source="qqq_finance.period_features",
        resolved_report_date=resolved,
        query="SELECT ocf_to_net_income FROM period_features WHERE ...",
        retrieved_at=datetime(2026, 7, 1, 12, 0, 0),
        accession_number="0000320193-25-000073",
    )


def test_found_result_requires_value():
    """A FOUND result carries the value (the happy path is constructable)."""
    r = FeatureResult(
        feature="ocf_to_net_income",
        status=FeatureStatus.FOUND,
        value=0.87,
        provenance=_provenance(),
    )
    assert r.value == 0.87
    assert r.provenance.resolved_report_date == date(2025, 9, 30)


def test_found_without_value_is_rejected():
    """Illegal state: FOUND with no value must fail at construction."""
    try:
        FeatureResult(
            feature="ocf_to_net_income",
            status=FeatureStatus.FOUND,
            value=None,
            provenance=_provenance(),
        )
    except ValueError:
        return
    raise AssertionError("FOUND with value=None should have raised ValueError")


def test_not_filed_with_value_is_rejected():
    """Illegal state: a 'not filed yet' period must not carry a value — this is
    the invariant that stops the agent reading a missing period as a 0/refutation."""
    try:
        FeatureResult(
            feature="ocf_to_net_income",
            status=FeatureStatus.PERIOD_NOT_FILED,
            value=0.0,
            provenance=_provenance(),
        )
    except ValueError:
        return
    raise AssertionError("PERIOD_NOT_FILED with a value should have raised ValueError")


def test_period_not_filed_is_constructable_without_value():
    """The 'future hasn't arrived' state is a first-class, legal result."""
    r = FeatureResult(
        feature="ocf_to_net_income",
        status=FeatureStatus.PERIOD_NOT_FILED,
        value=None,
        provenance=_provenance(),
    )
    assert r.status is FeatureStatus.PERIOD_NOT_FILED
    assert r.value is None


def test_stub_data_path_not_wired():
    """The data path is honestly stubbed — it raises, it does not fake success."""
    try:
        feature_history("AAPL", date(2025, 6, 30), period_offset=1, features=["ocf_to_net_income"])
    except NotImplementedError:
        return
    raise AssertionError("feature_history data path should raise NotImplementedError until wired")


def _run() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} contract tests passed.")


if __name__ == "__main__":
    _run()
