"""Unit tests for the batch Step 10 pure helpers (ADR-8 / Phase 5 batch half).

The BQ read/write and LLM call need creds; the row-shaping logic
(flag_from_row / branch_rows) is pure and tested here with plain dicts + fake
branches — no BQ, no LLM.

Run: `python tests/investigator/test_propose_branches_step.py`
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "explanations"))

from qqq_scoring.investigator.graph import Branch, BranchStatus, Flag  # noqa: E402
from propose_investigation_branches import branch_rows, flag_from_row  # noqa: E402


def _row(**over):
    base = {
        "ticker": "WBD",
        "report_date": date(2025, 6, 30),
        "calendar_quarter": "2025-Q2",
        "form_type": "10-Q",
        "anomaly_score_0_100": 87,
        "conviction_tier": "ALERT",
        "top_driver_1": "ocf_to_net_income", "top_driver_1_value": 0.50,
        "top_driver_2": "accrual_ratio", "top_driver_2_value": 0.09,
        "top_driver_3": None, "top_driver_3_value": None,
        "key_question": "why did cash conversion collapse?",
    }
    base.update(over)
    return base


def test_flag_from_row_composes_summary():
    flag = flag_from_row(_row())
    assert flag.ticker == "WBD"
    assert flag.report_date == date(2025, 6, 30)
    assert flag.form == "10-Q"
    assert "ocf_to_net_income (0.5)" in flag.summary
    assert "accrual_ratio (0.09)" in flag.summary
    assert "why did cash conversion collapse?" in flag.summary
    assert "ALERT" in flag.summary


def test_flag_from_row_handles_missing_key_question():
    flag = flag_from_row(_row(key_question=None))
    assert "will the anomaly persist" in flag.summary   # fallback question


def test_flag_from_row_skips_empty_drivers():
    flag = flag_from_row(_row(top_driver_2=None, top_driver_2_value=None))
    assert "accrual_ratio" not in flag.summary
    assert "ocf_to_net_income" in flag.summary


def test_branch_rows_shape_and_status():
    flag = flag_from_row(_row())
    branches = [
        Branch("h1", "impairment", "goodwill heavy", "was there a non-cash charge?"),
        Branch("h2", "working capital", "receivables build", "did WC absorb the cash?"),
    ]
    ts = datetime(2026, 7, 12, tzinfo=timezone.utc)
    rows = branch_rows(flag, branches, _row(), ts)
    assert len(rows) == 2
    r = rows[0]
    assert r["ticker"] == "WBD"
    assert r["branch_id"] == "h1"
    assert r["predicate"] == "was there a non-cash charge?"   # the steering wire is persisted
    assert r["status"] == BranchStatus.PROPOSED.value          # never investigated in batch
    assert r["conviction_tier"] == "ALERT"
    assert r["generated_at"] == ts


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} step-10 tests passed.")


if __name__ == "__main__":
    _run()
