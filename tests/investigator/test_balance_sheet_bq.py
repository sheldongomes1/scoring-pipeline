"""Unit tests for the BQ-backed balance_sheet resolution logic (no BQ creds needed).

Mirrors test_feature_history_bq: an injected fake client returns canned rows; the
risky positional/ADR-13/ADR-14 logic is exercised without network.
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_bq import balance_sheet_items  # noqa: E402
from qqq_scoring.investigator.tools.contracts import FeatureStatus  # noqa: E402


class _FakeBQ:
    def __init__(self, rows):
        self._rows = rows

    def query(self, sql, job_config=None):
        return list(self._rows)


# WBD-like: 10-Qs with a 10-K (annual) between Jun and Dec (ADR-14 skip case).
_ROWS = [
    {"target_period_end": date(2024, 3, 31), "target_form": "10-Q", "accession_number": "a1", "accounts_receivable": 6000000000.0, "inventory": None},
    {"target_period_end": date(2024, 6, 30), "target_form": "10-Q", "accession_number": "a2", "accounts_receivable": 6166000000.0, "inventory": None},
    {"target_period_end": date(2024, 12, 31), "target_form": "10-K", "accession_number": "a3", "accounts_receivable": 6300000000.0, "inventory": None},
    {"target_period_end": date(2025, 3, 31), "target_form": "10-Q", "accession_number": "a4", "accounts_receivable": 6100000000.0, "inventory": None},
]


def _bs(report_date, offset, items=("accounts_receivable",)):
    return balance_sheet_items("WBD", report_date, offset, list(items), client=_FakeBQ(_ROWS))


def test_positional_offset_and_found_value():
    r = _bs(date(2024, 6, 30), 0)[0]
    assert r.status is FeatureStatus.FOUND
    assert r.value == 6166000000.0
    assert r.provenance.resolved_report_date == date(2024, 6, 30)


def test_null_item_is_feature_missing():
    r = _bs(date(2024, 6, 30), 0, items=["inventory"])[0]
    assert r.status is FeatureStatus.FEATURE_MISSING
    assert r.value is None


def test_offset_off_the_end_is_period_not_filed():
    r = _bs(date(2024, 6, 30), 9)[0]
    assert r.status is FeatureStatus.PERIOD_NOT_FILED


def test_fiscal_year_end_skip_counted():
    """ADR-14: Jun 10-Q +1 → next 10-Q (Mar 2025), skipping the Dec 10-K between."""
    r = _bs(date(2024, 6, 30), 1)[0]
    assert r.provenance.resolved_report_date == date(2025, 3, 31)
    assert r.periods_skipped == 1


def test_adr13_authentic_not_filed_re_grounds():
    """A PERIOD_NOT_FILED probe re-grounds via request-replay (not resolved+0)."""
    client = _FakeBQ(_ROWS)
    reverify = lambda tk, rd, off, its: balance_sheet_items(tk, rd, off, its, client=client)
    ev = balance_sheet_items("WBD", date(2025, 3, 31), 5, ["accounts_receivable"], client=client)
    assert ev[0].status is FeatureStatus.PERIOD_NOT_FILED
    grounded, failed, det = Judge(client=None, reverify=reverify)._check_grounding(ev)
    assert grounded is True and det is False and failed == []


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} balance-sheet-bq tests passed.")


if __name__ == "__main__":
    _run()
