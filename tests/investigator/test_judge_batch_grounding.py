"""Batched-reverify equivalence tests (latency #1/#2/#4).

Proves the batched, memoized grounding gate returns verdicts BYTE-IDENTICAL to the
per-item loop for a mixed-source, multi-offset evidence pile — with far fewer
backend queries. Grounding must be an optimization, not a behavior change.

Run: `python tests/investigator/test_judge_batch_grounding.py`
"""
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.tools.contracts import (  # noqa: E402
    FeatureResult,
    FeatureStatus,
    Provenance,
)
from qqq_scoring.investigator.tools import balance_sheet_fake as bsf  # noqa: E402
from qqq_scoring.investigator.tools import feature_history_fake as fhf  # noqa: E402

FH_SOURCE = fhf.SOURCE
BS_SOURCE = bsf.SOURCE


# ── query-counting backends: a plain per-item callable and a `.batch`-capable one ──


def _counting_per_item(underlying):
    """Wrap a fake with a call counter — the PER-ITEM baseline (no `.batch`)."""
    calls = []

    def backend(ticker, rd, off, feats):
        calls.append(1)
        return underlying(ticker, rd, off, list(feats))

    backend.calls = calls
    return backend


def _counting_batch(underlying):
    """Wrap a fake with a `.batch` that resolves many probes but counts ONE query per
    (source, ticker) invocation — mirrors the real BQ batch backends."""
    per_calls = []
    batch_calls = []

    def backend(ticker, rd, off, feats):
        per_calls.append(1)
        return underlying(ticker, rd, off, list(feats))

    def batch(ticker, probes):
        batch_calls.append(1)  # ONE "query" per batch invocation
        return {(rd, off): underlying(ticker, rd, off, list(feats)) for (rd, off, feats) in probes}

    backend.batch = batch
    backend.per_calls = per_calls
    backend.batch_calls = batch_calls
    return backend


def _mixed_evidence() -> list:
    """A realistic pile: two sources, multiple offsets, duplicates across cycles, and
    one TAMPERED item — the same shape the live PANW deep-dive produced."""
    ev = []
    # feature_history: anchor + a prior-year offset, several features.
    ev += fhf.feature_history_fake("WBD", date(2025, 6, 30), 0, ["ocf_to_net_income", "net_margin"])
    ev += fhf.feature_history_fake("WBD", date(2025, 6, 30), -1, ["ocf_to_net_income"])
    # balance_sheet: same anchor + prior offset, several items.
    ev += bsf.balance_sheet_items("WBD", date(2025, 6, 30), 0, ["total_debt", "deferred_revenue"])
    ev += bsf.balance_sheet_items("WBD", date(2025, 6, 30), -1, ["total_debt"])
    # duplicate the whole pile (as continue/repair cycles do) — must be re-verified once.
    ev = ev + list(ev)
    # one TAMPERED feature-history value so the failed list is non-empty and asserted.
    tampered_prov = Provenance(
        source=FH_SOURCE, ticker="WBD",
        resolved_report_date=date(2025, 9, 30),
        requested_report_date=date(2025, 6, 30), requested_offset=1,
        query="SELECT ocf_to_net_income FROM period_features WHERE ...",
        retrieved_at=datetime(2026, 7, 5, tzinfo=timezone.utc),
        accession_number=None,
    )
    ev.append(FeatureResult("ocf_to_net_income", FeatureStatus.FOUND, 9.99, tampered_prov))  # real is 0.52
    return ev


def _reverify_map(factory):
    return {FH_SOURCE: factory(fhf.feature_history_fake), BS_SOURCE: factory(bsf.balance_sheet_items)}


def test_batched_verdict_equals_per_item_verdict():
    """The core guarantee: identical (grounded, failed, det) whether the backend
    batches or not."""
    ev = _mixed_evidence()

    per = Judge(client=None, reverify=_reverify_map(_counting_per_item))
    g1, f1, d1 = per._check_grounding(ev)

    bat = Judge(client=None, reverify=_reverify_map(_counting_batch))
    g2, f2, d2 = bat._check_grounding(ev)

    assert (g1, f1, d1) == (g2, f2, d2), f"per-item {(g1, f1, d1)} != batched {(g2, f2, d2)}"
    assert g1 is False and d1 is True                       # the tampered item fails
    assert f1 == ["ocf_to_net_income@2025-09-30"]           # exactly the tampered figure
    print(f"    verdict identical: grounded={g1} failed={f1} det={d1}")


def test_batched_issues_far_fewer_queries():
    """Batching collapses per-feature × per-offset queries into ONE per (source, ticker)."""
    ev = _mixed_evidence()

    per = Judge(client=None, reverify=(m := _reverify_map(_counting_per_item)))
    per._check_grounding(ev)
    per_queries = len(m[FH_SOURCE].calls) + len(m[BS_SOURCE].calls)

    bat = Judge(client=None, reverify=(mb := _reverify_map(_counting_batch)))
    bat._check_grounding(ev)
    bat_queries = len(mb[FH_SOURCE].batch_calls) + len(mb[BS_SOURCE].batch_calls)
    assert len(mb[FH_SOURCE].per_calls) == 0 and len(mb[BS_SOURCE].per_calls) == 0  # batch path used

    # Per-item: one call per distinct (source, ticker, rd, offset, feature). Batched:
    # exactly one query per (source, ticker) that has work → 2.
    assert bat_queries == 2, bat_queries
    assert per_queries > bat_queries
    print(f"    queries: per-item={per_queries}  batched={bat_queries}")


def test_session_memo_skips_re_fetch_across_evaluate_cycles():
    """latency #2: a second grounding pass over the SAME evidence issues ZERO new
    queries — the identities are already memoized on the Judge instance."""
    ev = _mixed_evidence()
    bat = Judge(client=None, reverify=(m := _reverify_map(_counting_batch)))

    bat._check_grounding(ev)
    first = len(m[FH_SOURCE].batch_calls) + len(m[BS_SOURCE].batch_calls)
    bat._check_grounding(ev)                 # pass 2 — same pile
    second = len(m[FH_SOURCE].batch_calls) + len(m[BS_SOURCE].batch_calls)

    assert first == 2
    assert second == first, f"pass 2 re-queried: {second} total vs {first} after pass 1"
    print(f"    memo held: pass1={first} queries, pass2 added {second - first}")


def test_new_offsets_in_later_pass_query_only_the_new_work():
    """A later pass that adds NEW offsets fetches only those — pass 1's identities stay
    memoized (mirrors the live repair cycle reaching for more history)."""
    bat = Judge(client=None, reverify=(m := _reverify_map(_counting_batch)))

    pass1 = fhf.feature_history_fake("WBD", date(2025, 6, 30), 0, ["ocf_to_net_income"])
    bat._check_grounding(pass1)
    after1 = len(m[FH_SOURCE].batch_calls)

    pass2 = pass1 + fhf.feature_history_fake("WBD", date(2025, 6, 30), -1, ["ocf_to_net_income"])
    bat._check_grounding(pass2)
    after2 = len(m[FH_SOURCE].batch_calls)

    assert after1 == 1
    assert after2 == 2, f"expected 1 new query for the new offset, got {after2 - after1}"
    print(f"    incremental: pass1={after1}, pass2 added {after2 - after1} (only the new offset)")


def _run():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        t()
        print(f"  ok  {t.__name__}")
    print(f"\n{len(tests)} batch-grounding tests passed.")


if __name__ == "__main__":
    _run()
