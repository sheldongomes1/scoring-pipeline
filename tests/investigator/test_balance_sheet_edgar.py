"""Unit tests for the REAL SEC EDGAR companyfacts backend for balance_sheet_items.

The HTTP I/O needs the network, but the RISKY parts — positional period
resolution over an irregular fiscal calendar (ADR-12/14), form classification of
period ends via the earliest filing, concept-candidate fallback, and the ADR-13
request-replay provenance — are pure logic and are tested here with an injected
fetcher (canned companyfacts JSON), no network required.

Run: `python3 tests/investigator/test_balance_sheet_edgar.py`
"""
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet import ITEM_KEYS  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_edgar import (  # noqa: E402
    SOURCE,
    _FACTS_URL,
    _TICKER_MAP_URL,
    balance_sheet_items,
)
from qqq_scoring.investigator.tools.contracts import FeatureStatus  # noqa: E402

# AAPL-shaped irregular fiscal calendar: 3 10-Qs/year, the fiscal year-end 10-K
# sitting BETWEEN the June 10-Q and the December 10-Q (ADR-14). The Sep 27 end
# also appears as a 10-Q comparative FILED LATER — the axis must classify it as
# annual because its EARLIEST filing is the 10-K.
def _fact(end, val, form, filed, accn="0000000000-25-000001"):
    return {"end": end, "val": val, "form": form, "filed": filed, "accn": accn, "fy": 2025, "fp": "Q1"}


_ASSETS = [
    _fact("2025-03-29", 100.0, "10-Q", "2025-05-01", "accn-q2"),
    _fact("2025-06-28", 110.0, "10-Q", "2025-08-01", "accn-q3"),
    _fact("2025-09-27", 120.0, "10-K", "2025-10-30", "accn-k"),
    _fact("2025-09-27", 120.0, "10-Q", "2026-01-30", "accn-q1n"),   # comparative in the later 10-Q
    _fact("2025-12-27", 130.0, "10-Q", "2026-01-30", "accn-q1n"),
]

_FACTS_DOC = {
    "cik": 320193,
    "facts": {
        "us-gaap": {
            "Assets": {"units": {"USD": _ASSETS}},
            # receivables under the SECOND candidate spelling — exercises fallback
            "ReceivablesNetCurrent": {"units": {"USD": [
                _fact("2025-06-28", 6100.0, "10-Q", "2025-08-01", "accn-q3"),
                # restated in the later comparative — the ORIGINAL (earliest-filed) must win
                _fact("2025-06-28", 9999.0, "10-Q", "2026-01-30", "accn-q1n"),
                _fact("2025-12-27", 6600.0, "10-Q", "2026-01-30", "accn-q1n"),
            ]}},
            "AccountsPayableCurrent": {"units": {"USD": [
                _fact("2025-06-28", 3100.0, "10-Q", "2025-08-01", "accn-q3"),
            ]}},
            # inventory absent entirely -> FEATURE_MISSING
        }
    },
}

_TICKER_MAP = {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}


def _fetcher(url):
    if url == _TICKER_MAP_URL:
        return _TICKER_MAP
    if url == _FACTS_URL.format(cik="0000320193"):
        return _FACTS_DOC
    raise AssertionError(f"unexpected URL fetched: {url}")


def _bs(report_date, offset, items=("accounts_receivable",), ticker="AAPL"):
    return balance_sheet_items(ticker, report_date, offset, list(items), fetcher=_fetcher)


def test_found_value_is_the_original_filing():
    """The as-originally-filed fact wins over a later restated comparative."""
    r = _bs(date(2025, 6, 28), 0)[0]
    assert r.status is FeatureStatus.FOUND
    assert r.value == 6100.0
    assert r.provenance.accession_number == "accn-q3"
    assert r.provenance.source == SOURCE


def test_concept_candidate_fallback_is_recorded():
    """AR is tagged ReceivablesNetCurrent (candidate #2); the query names it."""
    r = _bs(date(2025, 6, 28), 0)[0]
    assert "us-gaap/ReceivablesNetCurrent" in r.provenance.query


def test_positional_offset_skips_the_fiscal_year_end():
    """ADR-14: +1 from the June 10-Q lands on the December 10-Q (next FILED 10-Q),
    NOT the September fiscal year-end — which is counted into periods_skipped.
    The Sep end appearing as a later 10-Q comparative must not pollute the axis."""
    r = _bs(date(2025, 6, 28), 1)[0]
    assert r.status is FeatureStatus.FOUND
    assert r.provenance.resolved_report_date == date(2025, 12, 27)
    assert r.value == 6600.0
    assert r.periods_skipped == 1


def test_negative_offset_no_skip():
    r = _bs(date(2025, 6, 28), -1)[0]
    assert r.provenance.resolved_report_date == date(2025, 3, 29)
    assert r.status is FeatureStatus.FEATURE_MISSING   # no AR fact at Q2
    assert r.periods_skipped == 0


def test_offset_off_the_end_is_period_not_filed():
    r = _bs(date(2025, 12, 27), 3)[0]
    assert r.status is FeatureStatus.PERIOD_NOT_FILED
    assert r.value is None


def test_anchor_not_in_history_is_period_not_filed():
    r = _bs(date(2025, 7, 15), 0)[0]
    assert r.status is FeatureStatus.PERIOD_NOT_FILED


def test_unknown_ticker_is_period_not_filed():
    r = _bs(date(2025, 6, 28), 0, ticker="NOPE")[0]
    assert r.status is FeatureStatus.PERIOD_NOT_FILED


def test_absent_item_is_feature_missing_not_zero():
    r = _bs(date(2025, 6, 28), 0, items=["inventory"])[0]
    assert r.status is FeatureStatus.FEATURE_MISSING
    assert r.value is None


def test_illegal_item_key_rejected():
    """Defense in depth: an unmapped item can't silently return garbage."""
    try:
        _bs(date(2025, 6, 28), 0, items=["total_assets; DROP"])
    except ValueError:
        return
    raise AssertionError("unknown item key should raise ValueError")


def test_all_canonical_item_keys_are_mapped():
    """Every key the Claude enum offers has a concept mapping — the model can never
    pick an item the backend refuses."""
    for r in _bs(date(2025, 6, 28), 0, items=list(ITEM_KEYS)):
        assert r.status in (FeatureStatus.FOUND, FeatureStatus.FEATURE_MISSING)


def test_provenance_carries_the_request_and_replay_re_grounds():
    """ADR-13: provenance holds the REQUESTED anchor + offset, and the judge's
    replay of the request re-grounds FOUND and PERIOD_NOT_FILED alike."""
    found = _bs(date(2025, 6, 28), 1)[0]
    assert found.provenance.requested_report_date == date(2025, 6, 28)
    assert found.provenance.requested_offset == 1

    reverify = {SOURCE: lambda tk, rd, off, its: balance_sheet_items(tk, rd, off, its, fetcher=_fetcher)}
    not_filed = _bs(date(2025, 12, 27), 3)  # offset past the filed history
    assert not_filed[0].status is FeatureStatus.PERIOD_NOT_FILED
    for evidence in ([found], not_filed):
        grounded, failed, det, _ = Judge(client=None, reverify=reverify)._check_grounding(evidence)
        assert grounded is True
        assert det is False
        assert failed == []


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} edgar-backend tests passed.")


if __name__ == "__main__":
    _run()
