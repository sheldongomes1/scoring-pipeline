"""Unit tests for the REAL BigQuery backend's resolution logic (ADR-12).

The BigQuery I/O needs creds and a live table, but the RISKY part — positional
period resolution across an irregular fiscal calendar — is pure logic and is
tested here with an injected fake client (canned rows), no BQ required.

Run: `python tests/investigator/test_feature_history_bq.py`
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.tools.contracts import FeatureStatus  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_bq import feature_history_bq  # noqa: E402


class _FakeBQ:
    """Stands in for bigquery.Client — .query() returns canned rows (dicts)."""

    def __init__(self, rows):
        self._rows = rows

    def query(self, sql, job_config=None, timeout=None):
        return list(self._rows)


# AAPL's real irregular fiscal calendar: 3 10-Qs/year with the fiscal year-end 10-K
# (annual) sitting BETWEEN the June 10-Q and the December 10-Q (ADR-14).
_ROWS = [
    {"target_period_end": date(2025, 3, 29), "target_form": "10-Q", "ocf_to_net_income": 2.17, "net_margin": None},
    {"target_period_end": date(2025, 6, 28), "target_form": "10-Q", "ocf_to_net_income": 3.49, "net_margin": 0.24},
    {"target_period_end": date(2025, 9, 27), "target_form": "10-K", "ocf_to_net_income": 1.00, "net_margin": 0.30},
    {"target_period_end": date(2025, 12, 27), "target_form": "10-Q", "ocf_to_net_income": 1.28, "net_margin": 0.25},
]


def _fh(report_date, offset, features=("ocf_to_net_income",)):
    return feature_history_bq("AAPL", report_date, offset, list(features), client=_FakeBQ(_ROWS))


def test_positional_offset_resolves_across_gaps():
    """+1 lands on the next FILED period (Jun 28 -> Dec 27), skipping the missing
    September quarter — positional, not calendar (ADR-12)."""
    r = _fh(date(2025, 6, 28), 1)[0]
    assert r.status is FeatureStatus.FOUND
    assert r.provenance.resolved_report_date == date(2025, 12, 27)
    assert r.value == 1.28


def test_negative_offset():
    r = _fh(date(2025, 6, 28), -1)[0]
    assert r.provenance.resolved_report_date == date(2025, 3, 29)
    assert r.value == 2.17


def test_offset_off_the_end_is_period_not_filed():
    r = _fh(date(2025, 6, 28), 5)[0]
    assert r.status is FeatureStatus.PERIOD_NOT_FILED
    assert r.value is None


def test_anchor_not_in_history_is_period_not_filed():
    """A report_date the ticker never filed can't anchor the offset."""
    r = _fh(date(2025, 7, 15), 0)[0]
    assert r.status is FeatureStatus.PERIOD_NOT_FILED


def test_null_value_is_feature_missing_not_zero():
    r = _fh(date(2025, 3, 29), 0, features=["net_margin"])[0]
    assert r.status is FeatureStatus.FEATURE_MISSING
    assert r.value is None


def test_illegal_feature_identifier_rejected():
    """Defense in depth: a malformed feature name can't reach the SQL SELECT."""
    try:
        _fh(date(2025, 6, 28), 0, features=["ocf; DROP TABLE"])
    except ValueError:
        return
    raise AssertionError("illegal feature identifier should raise ValueError")


def test_fiscal_year_end_skip_is_counted_and_surfaced():
    """ADR-14: +1 from fiscal Q3 (Jun) to the next 10-Q (Dec) counts the 10-K annual
    period (Sep) that sits between — the model is warned it crossed a year-end rather
    than reading a ~6-month jump as two adjacent quarters."""
    from qqq_scoring.investigator.tools.feature_history import to_model_content
    r = _fh(date(2025, 6, 28), 1)[0]
    assert r.status is FeatureStatus.FOUND
    assert r.provenance.resolved_report_date == date(2025, 12, 27)   # next 10-Q, not the 10-K
    assert r.value == 1.28                                           # quarterly value, NOT the annual 1.00
    assert r.periods_skipped == 1
    assert "fiscal_periods_skipped" in to_model_content([r])         # the model actually sees it


def test_no_skip_within_the_same_fiscal_year():
    r = _fh(date(2025, 6, 28), -1)[0]   # Jun -> prior Mar 10-Q, no year-end between
    assert r.periods_skipped == 0


def test_authentic_period_not_filed_re_grounds():
    """ADR-13 (SEV-1 fix): a PERIOD_NOT_FILED probe (offset past the ticker's filed
    history — the canonical 'did it recover next quarter?' move) must re-ground as
    AUTHENTIC. The judge replays the REQUEST (anchor + offset), not resolved+0 —
    which previously landed on the anchor row, returned FOUND, and falsely failed
    grounding on real data, dooming the branch to ABANDONED."""
    client = _FakeBQ(_ROWS)
    reverify = lambda tk, rd, off, fs: feature_history_bq(tk, rd, off, fs, client=client)
    evidence = feature_history_bq("AAPL", date(2025, 12, 27), 3, ["ocf_to_net_income"], client=client)
    assert evidence[0].status is FeatureStatus.PERIOD_NOT_FILED     # offset past the latest filing
    grounded, failed, det, _ = Judge(client=None, reverify=reverify)._check_grounding(evidence)
    assert grounded is True     # authentic not-filed re-grounds cleanly (was False before the fix)
    assert det is False
    assert failed == []


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} bq-backend tests passed.")


if __name__ == "__main__":
    _run()
