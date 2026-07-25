"""REAL SEC EDGAR companyfacts backend for `balance_sheet_items` — tool #3 goes live.

Drop-in for `balance_sheet_fake`: same signature, same `FeatureResult` return
(ADR-9 — a line item is a number; the `feature` field holds the item key). The
judge's reverify map gains one entry keyed by this module's `SOURCE`.

WHY EDGAR (source hunt, 2026-07-13): the GCS `analysis_ready.json` bundles carry
only 7 high-level concepts in `companyfacts_feature_history` (assets/equity/
liabilities/net_income/ocf/revenue/shares) and `beneish_raw_features` covers just
receivables + long_term_debt of this tool's vocabulary — no payables, inventory,
cash, deferred revenue, or content assets. `qqq_finance` in BigQuery holds ratios,
not line items. So the golden source is the UPSTREAM source itself: SEC's XBRL
companyfacts API (`data.sec.gov`) — one JSON per company with every as-filed
us-gaap fact, free, no key, and exactly what the bundles were derived from.

Period resolution is POSITIONAL, mirroring `feature_history_bq` (ADR-12/14):
`period_offset` counts FILED 10-Q periods in the ticker's own history; fiscal
year-end (10-K) periods sitting between anchor and resolved are counted into
`periods_skipped`, never mixed into the quarterly axis. The filed-period axis is
built from an anchor concept every filer reports (Assets): a period end belongs to
the form of its EARLIEST filing — a prior-FY-end appearing as a comparative in a
later 10-Q was originally filed in the 10-K, so it classifies as annual.

Values are AS ORIGINALLY FILED: for a (concept, period) with facts from several
filings (original + later comparatives/restatements), the earliest-filed fact wins
— deterministic, and it is the number the market saw at the time. Each item maps
to an ordered list of us-gaap concept candidates (filers tag the same line
differently: AAPL `AccountsReceivableNetCurrent`, WBD `ReceivablesNetCurrent`);
the first candidate with a fact at the resolved period wins and is recorded in the
provenance query. No candidate present → FEATURE_MISSING (honest: NFLX folds
receivables into other current assets; AAPL has no content assets).

ADR-13 grounding replay: provenance carries `requested_report_date` +
`requested_offset` — the judge re-fetches by replaying the REQUEST, so FOUND,
FEATURE_MISSING and PERIOD_NOT_FILED all re-ground correctly.

Politeness: one cached HTTP fetch per company per 15 min (the ticker→CIK map for
24 h), a floor between requests, and the SEC-required User-Agent
(override via SEC_EDGAR_USER_AGENT).
"""
from __future__ import annotations

import json
import os
import threading
import time
import urllib.request
from datetime import date, datetime, timezone

from .contracts import FeatureResult, FeatureStatus, Provenance

SOURCE = "sec_edgar.companyfacts"  # logical golden source; the judge's reverify-map key

_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"
_USER_AGENT = os.environ.get("SEC_EDGAR_USER_AGENT", "qqq-anomaly-lab sheldon.gomes@gmail.com")

_FORMS = ("10-Q", "10-K")  # exact match; amendments (10-Q/A) excluded — the original filing is the record

# item key -> ordered us-gaap concept candidates (first with a fact at the period
# wins). Validated live against WBD, AAPL, NFLX companyfacts (2026-07-13).
_CONCEPTS: dict[str, list[str]] = {
    "accounts_receivable": [
        "AccountsReceivableNetCurrent",             # AAPL et al.
        "ReceivablesNetCurrent",                    # WBD
        "AccountsNotesAndLoansReceivableNetCurrent",
    ],
    "accounts_payable": [
        "AccountsPayableCurrent",
        "AccountsPayableAndAccruedLiabilitiesCurrent",
        "AccountsPayableTradeCurrent",
    ],
    "inventory": [
        "InventoryNet",
        "InventoryFinishedGoodsNetOfReserves",
    ],
    "content_assets": [
        "FilmCosts",                                # WBD's capitalized content (ASU 2019-02)
        "FiniteLivedMediaContentGross",
        "CapitalizedContentCostNet",
    ],
    # CFA note: no single us-gaap tag reliably means "total debt". Prefer the
    # combined tag; fall back to LongTermDebt (which for most filers INCLUDES the
    # current portion), then the noncurrent-only tag. May understate filers with
    # separate short-term borrowings — provenance records which concept was used.
    "total_debt": [
        "DebtLongtermAndShorttermCombinedAmount",
        "LongTermDebt",
        "LongTermDebtNoncurrent",
    ],
    "cash_and_equivalents": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
    "deferred_revenue": [
        "ContractWithCustomerLiabilityCurrent",     # post-ASC-606 spelling (current portion)
        "DeferredRevenueCurrent",
        "ContractWithCustomerLiability",
        "DeferredRevenue",
    ],
}

# Filed-period axis anchors — concepts every 10-Q/10-K balance sheet reports.
_AXIS_CONCEPTS = ["Assets", "LiabilitiesAndStockholdersEquity", "StockholdersEquity"]

# ── polite HTTP with in-process caching ──────────────────────────────────────

_cache: dict[str, tuple[float, dict]] = {}
_cache_lock = threading.Lock()
_last_fetch = 0.0
_MIN_INTERVAL = 0.15   # ≤ ~7 req/s, under SEC's 10 req/s ceiling
_FACTS_TTL = 900.0     # companyfacts re-fetched at most every 15 min per company
_TICKER_TTL = 86400.0  # ticker→CIK map is near-static


def _http_json(url: str) -> dict:
    global _last_fetch
    with _cache_lock:
        wait = _MIN_INTERVAL - (time.monotonic() - _last_fetch)
        if wait > 0:
            time.sleep(wait)
        _last_fetch = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())
    except Exception:
        time.sleep(1.0)  # one polite retry (transient 429/5xx/network)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read())


def _get_json(url: str, ttl: float, fetcher=None) -> dict:
    if fetcher is not None:          # injected in tests — no network, no cache
        return fetcher(url)
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    data = _http_json(url)
    with _cache_lock:
        _cache[url] = (time.monotonic(), data)
    return data


def _cik_for(ticker: str, fetcher=None) -> str | None:
    """Resolve ticker → zero-padded 10-digit CIK via SEC's official map."""
    mapping = _get_json(_TICKER_MAP_URL, _TICKER_TTL, fetcher)
    want = ticker.upper()
    for row in mapping.values():
        if row.get("ticker", "").upper() == want:
            return f"{int(row['cik_str']):010d}"
    return None


# ── fact selection ────────────────────────────────────────────────────────────


def _usd_facts(gaap: dict, concept: str) -> list[dict]:
    units = gaap.get(concept, {}).get("units", {})
    return [f for f in units.get("USD", []) if f.get("form") in _FORMS and f.get("end")]


def _original(facts: list[dict]) -> dict:
    """The as-originally-filed fact: earliest `filed` date wins (deterministic;
    later appearances are comparatives/restatements in subsequent filings)."""
    return min(facts, key=lambda f: (f.get("filed") or "9999-99-99", f.get("accn") or ""))


def _period_axis(gaap: dict, form: str) -> tuple[list[date], list[date]]:
    """(quarterly period ends, annual period ends), each sorted ascending.

    A period end is classified by the form of its EARLIEST filing: a fiscal
    year-end shows up in later 10-Qs as a comparative, but it was FILED in the
    10-K — so min(filed) recovers the true form (mirrors period_features'
    target_form)."""
    for concept in _AXIS_CONCEPTS:
        facts = _usd_facts(gaap, concept)
        if facts:
            break
    else:
        return [], []
    by_end: dict[str, list[dict]] = {}
    for f in facts:
        by_end.setdefault(f["end"], []).append(f)
    quarterly: list[date] = []
    annual: list[date] = []
    for end, fs in by_end.items():
        d = date.fromisoformat(end)
        orig_form = _original(fs).get("form")
        if orig_form == form:
            quarterly.append(d)
        elif orig_form == "10-K":
            annual.append(d)
    return sorted(quarterly), sorted(annual)


# ── the tool ──────────────────────────────────────────────────────────────────


def balance_sheet_items(
    ticker: str,
    report_date: date,
    period_offset: int,
    items: list[str],
    *,
    fetcher=None,
    form: str = "10-Q",
) -> list[FeatureResult]:
    """Return one FeatureResult per requested line item at the ticker's
    report_date + offset-th FILED period, read from SEC EDGAR companyfacts.

    `fetcher(url) -> dict` is injectable for tests (no network); default is a
    cached, rate-limited HTTP read of data.sec.gov."""
    for item in items:  # defense in depth — the enum constrains the model, not callers
        if item not in _CONCEPTS:
            raise ValueError(f"unknown balance-sheet item: {item!r}")

    cik = _cik_for(ticker, fetcher)
    facts_url = _FACTS_URL.format(cik=cik) if cik else f"{_FACTS_URL.format(cik='?')} (ticker {ticker!r} not in SEC map)"
    gaap: dict = {}
    if cik:
        gaap = _get_json(_FACTS_URL.format(cik=cik), _FACTS_TTL, fetcher).get("facts", {}).get("us-gaap", {})

    quarterly, annual = _period_axis(gaap, form)

    # Positional resolution over the QUARTERLY axis only (ADR-12/14).
    resolved: date | None = None
    if report_date in quarterly:
        idx = quarterly.index(report_date) + period_offset
        if 0 <= idx < len(quarterly):
            resolved = quarterly[idx]

    # Fiscal year-end (10-K) periods strictly between anchor and resolved (ADR-14).
    periods_skipped = 0
    if resolved is not None:
        lo, hi = sorted((report_date, resolved))
        periods_skipped = sum(1 for d in annual if lo < d < hi)

    retrieved_at = datetime.now(timezone.utc)
    results: list[FeatureResult] = []
    for item in items:
        candidates = _CONCEPTS[item]
        value = accession = concept_used = None
        if resolved is not None:
            end_iso = resolved.isoformat()
            for concept in candidates:
                hits = [f for f in _usd_facts(gaap, concept) if f["end"] == end_iso]
                if hits:
                    fact = _original(hits)
                    value, accession, concept_used = float(fact["val"]), fact.get("accn"), concept
                    break
        prov = Provenance(
            source=SOURCE,
            ticker=ticker,
            resolved_report_date=resolved or report_date,
            requested_report_date=report_date,   # ADR-13: reverify replays the REQUEST,
            requested_offset=period_offset,       # not resolved+0 (which mis-grounds not-filed)
            query=(
                f"GET {facts_url} -> us-gaap/{concept_used or '|'.join(candidates)} "
                f"@ end={resolved.isoformat() if resolved else 'UNRESOLVED'} "
                f"(earliest-filed {form}/10-K fact = as originally reported)"
            ),
            retrieved_at=retrieved_at,
            accession_number=accession,
        )
        if resolved is None:
            # ticker unknown to SEC, anchor not in its filed history, or offset off the end
            results.append(FeatureResult(item, FeatureStatus.PERIOD_NOT_FILED, None, prov))
        elif value is None:
            results.append(FeatureResult(item, FeatureStatus.FEATURE_MISSING, None, prov, periods_skipped=periods_skipped))
        else:
            results.append(FeatureResult(item, FeatureStatus.FOUND, value, prov, periods_skipped=periods_skipped))
    return results


def extract_all(ticker: str, items: list[str] | None = None, *, fetcher=None) -> list[dict]:
    """Ingestion primitive: EVERY filed period's line items for a ticker, from ONE
    companyfacts fetch. Returns rows for the BQ `balance_sheet_items` table:
    `{ticker, target_period_end: date, target_form, accession_number, <item>: value|None}`.
    Reuses the same concept selection + as-originally-filed rule as the live tool, so
    the BQ-backed tool resolves over an axis identical to what the live tool produced."""
    items = items or list(_CONCEPTS)
    cik = _cik_for(ticker, fetcher)
    if not cik:
        return []
    gaap = _get_json(_FACTS_URL.format(cik=cik), _FACTS_TTL, fetcher).get("facts", {}).get("us-gaap", {})
    quarterly, annual = _period_axis(gaap, "10-Q")

    rows: list[dict] = []
    for form_label, dates in (("10-Q", quarterly), ("10-K", annual)):
        for d in dates:
            end_iso = d.isoformat()
            row: dict = {"ticker": ticker, "target_period_end": d, "target_form": form_label}
            accn = None
            for item in items:
                val = None
                for concept in _CONCEPTS[item]:
                    hits = [f for f in _usd_facts(gaap, concept) if f["end"] == end_iso]
                    if hits:
                        fact = _original(hits)
                        val, accn = float(fact["val"]), (accn or fact.get("accn"))
                        break
                row[item] = val
            row["accession_number"] = accn
            rows.append(row)
    return rows
