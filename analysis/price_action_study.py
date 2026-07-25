#!/usr/bin/env python3
"""
Price-Action Correlation Study — RedInk Predictive Validity Test

Tests whether RedInk anomaly scores predict future stock underperformance vs QQQ.

Three analyses:
  A. Average excess and beta-adjusted abnormal returns by score tier
  B. Spearman correlation: anomaly_score vs negative forward excess return
  C. Beneish-confirmed subset (directionally negative anomalies)
  D. Period stratification: Bull / Neutral / Bear (market regime check)

Event anchor: filing_date — when the information became public (not report_date).
Beta: estimated from 2 years of weekly returns pre-event via CAPM.
Abnormal return = actual_stock_return − (beta × QQQ_return)

Output:
  analysis/results/events_with_returns.csv  ← raw per-event data
  analysis/results/price_action_summary.md  ← findings report
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats
import yfinance as yf

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
SCORES_CSV = ROOT / "output" / "quarterly_scores_detailed.csv"
RESULTS    = Path(__file__).parent / "results"
RESULTS.mkdir(exist_ok=True)

# ── Config ─────────────────────────────────────────────────────────────────────
BETA_WINDOW_WEEKS = 104   # 2 years of weekly data for beta estimation
BETA_MIN_WEEKS    = 52    # minimum 1 year required
DOWNLOAD_START    = "2019-01-01"
FORWARD_Qs        = [1, 2, 4]

# Score tiers — uniform distribution so quartile cuts are clean
TIER_HIGH   = 75   # top quartile    → "HIGH"
TIER_MID_HI = 50   # 50th–75th pct   → "MID-HIGH"
TIER_MID_LO = 25   # 25th–50th pct   → "MID-LOW"
# below 25                            → "LOW" (control group)

# Market regime thresholds on QQQ quarterly return
BULL = 0.05    # > +5%
BEAR = -0.05   # < -5%

# ── Quarter helpers ────────────────────────────────────────────────────────────
_Q_END   = {1: (3, 31), 2: (6, 30), 3: (9, 30), 4: (12, 31)}
_Q_START = {1: (1, 1),  2: (4, 1),  3: (7, 1),  4: (10, 1)}


def qend(year: int, q: int) -> date:
    m, d = _Q_END[q]
    return date(year, m, d)


def qstart(year: int, q: int) -> date:
    m, d = _Q_START[q]
    return date(year, m, d)


def advance_q(year: int, q: int, n: int) -> tuple:
    """Advance (year, q) by n calendar quarters."""
    idx = year * 4 + q - 1 + n
    return idx // 4, idx % 4 + 1


def parse_cq(cq: str) -> tuple:
    """'2023-Q3' → (2023, 3)"""
    y, q = cq.split("-Q")
    return int(y), int(q)


def assign_tier(score: float) -> str:
    if score >= TIER_HIGH:   return "HIGH"
    if score >= TIER_MID_HI: return "MID-HIGH"
    if score >= TIER_MID_LO: return "MID-LOW"
    return "LOW"


def assign_regime(qqq_qret) -> str:
    if pd.isna(qqq_qret): return "Unknown"
    if qqq_qret > BULL:    return "Bull"
    if qqq_qret < BEAR:    return "Bear"
    return "Neutral"


# ── 1. Load scoring data ───────────────────────────────────────────────────────
print("=" * 62)
print("  RedInk Price-Action Correlation Study")
print("=" * 62)

df = pd.read_csv(SCORES_CSV)
df["filing_date"] = pd.to_datetime(df["filing_date"]).dt.date

# Normalise beneish flag — CSV may store as bool or string
df["beneish_manipulation_flag"] = df["beneish_manipulation_flag"].map(
    {True: True, False: False, "True": True, "False": False}
)

df["_year"] = df["calendar_quarter"].map(lambda x: parse_cq(x)[0])
df["_q"]    = df["calendar_quarter"].map(lambda x: parse_cq(x)[1])
df["cq_end"]   = df.apply(lambda r: qend(r["_year"], r["_q"]), axis=1)
df["cq_start"] = df.apply(lambda r: qstart(r["_year"], r["_q"]), axis=1)

for n in FORWARD_Qs:
    df[f"fwd_end_q{n}"] = df.apply(
        lambda r, n=n: qend(*advance_q(r["_year"], r["_q"], n)), axis=1
    )

print(f"\nScoring data loaded")
print(f"  {len(df):,} events | {df['ticker'].nunique()} tickers")
print(f"  Date range: {df['filing_date'].min()} → {df['filing_date'].max()}")


# ── 2. Download price data ─────────────────────────────────────────────────────
tickers_list = sorted(df["ticker"].unique().tolist()) + ["QQQ"]
print(f"\nDownloading prices for {len(tickers_list)} tickers ({DOWNLOAD_START} → today)...")

raw = yf.download(tickers_list, start=DOWNLOAD_START, progress=False, auto_adjust=True)

if isinstance(raw.columns, pd.MultiIndex):
    px = raw["Close"].copy()
else:
    px = raw[["Close"]].copy()

px.index = pd.to_datetime(px.index)
print(f"  {px.shape[0]} trading days | {px.shape[1]} tickers in price data")
print(f"  Price range: {px.index.min().date()} → {px.index.max().date()}")

# Weekly close for beta estimation
px_weekly = px.resample("W-FRI").last()


# ── 3. Price and beta helpers ──────────────────────────────────────────────────

def get_px(series: pd.Series, target_date: date) -> float:
    """Last available adjusted close at or before target_date."""
    ts = pd.Timestamp(target_date)
    subset = series.dropna().loc[:ts]
    return float(subset.iloc[-1]) if len(subset) > 0 else np.nan


def compute_beta(ticker: str, event_date: date) -> float:
    """CAPM beta estimated from pre-event weekly returns."""
    if ticker not in px_weekly.columns:
        return np.nan
    ts = pd.Timestamp(event_date)
    window = px_weekly.loc[: ts - pd.Timedelta(weeks=1), [ticker, "QQQ"]].tail(
        BETA_WINDOW_WEEKS
    )
    if len(window) < BETA_MIN_WEEKS:
        return np.nan
    rets = window.pct_change().dropna()
    if len(rets) < BETA_MIN_WEEKS // 2:
        return np.nan
    rets = rets.dropna()
    var_qqq = rets["QQQ"].var()
    if var_qqq == 0:
        return np.nan
    return float(rets[ticker].cov(rets["QQQ"]) / var_qqq)


# ── 4. Build events table ──────────────────────────────────────────────────────
print("\nBuilding events table...")

records = []
for _, row in df.iterrows():
    ticker     = row["ticker"]
    event_date = row["filing_date"]
    score      = row["anomaly_score_0_100"]

    rec = {
        "ticker":           ticker,
        "calendar_quarter": row["calendar_quarter"],
        "filing_date":      event_date,
        "anomaly_score":    score,
        "beneish_flag":     row["beneish_manipulation_flag"],
        "tier":             assign_tier(score),
        "gics_sector":      row.get("gics_sector", ""),
    }

    if ticker not in px.columns:
        records.append(rec)
        continue

    px_event  = get_px(px[ticker], event_date)
    qqq_event = get_px(px["QQQ"], event_date)
    if pd.isna(px_event) or pd.isna(qqq_event) or px_event <= 0 or qqq_event <= 0:
        records.append(rec)
        continue

    rec["px_event"]  = px_event
    rec["qqq_event"] = qqq_event

    # QQQ return during event quarter → regime classification
    qqq_qs = get_px(px["QQQ"], row["cq_start"])
    qqq_qe = get_px(px["QQQ"], row["cq_end"])
    qqq_qret = (qqq_qe / qqq_qs - 1) if (qqq_qs and qqq_qe and qqq_qs > 0) else np.nan
    rec["qqq_quarter_return"] = qqq_qret
    rec["regime"] = assign_regime(qqq_qret)

    # Beta
    beta = compute_beta(ticker, event_date)
    rec["beta"] = beta

    # Forward returns
    for n in FORWARD_Qs:
        fwd_date = row[f"fwd_end_q{n}"]
        px_fwd   = get_px(px[ticker], fwd_date)
        qqq_fwd  = get_px(px["QQQ"], fwd_date)
        if pd.isna(px_fwd) or pd.isna(qqq_fwd) or px_fwd <= 0 or qqq_fwd <= 0:
            continue

        stock_ret = px_fwd / px_event - 1
        qqq_ret   = qqq_fwd / qqq_event - 1
        excess    = stock_ret - qqq_ret
        abnormal  = (stock_ret - beta * qqq_ret) if not pd.isna(beta) else np.nan

        rec[f"stock_ret_q{n}"]    = stock_ret
        rec[f"qqq_ret_q{n}"]      = qqq_ret
        rec[f"excess_ret_q{n}"]   = excess
        rec[f"abnormal_ret_q{n}"] = abnormal

    records.append(rec)

events = pd.DataFrame(records)
print(f"  Events built: {len(events):,}")
for n in FORWARD_Qs:
    col = f"excess_ret_q{n}"
    if col in events.columns:
        print(f"  Events with Q+{n} forward data: {events[col].notna().sum():,}")


# ── 5. Analysis ────────────────────────────────────────────────────────────────
TIER_ORDER   = ["HIGH", "MID-HIGH", "MID-LOW", "LOW"]
REGIME_ORDER = ["Bull", "Neutral", "Bear"]
TIER_RANGES  = {
    "HIGH":     f"≥ {TIER_HIGH}",
    "MID-HIGH": f"{TIER_MID_HI}–{TIER_HIGH}",
    "MID-LOW":  f"{TIER_MID_LO}–{TIER_MID_HI}",
    "LOW":      f"< {TIER_MID_LO}",
}


def fmt(val, n=0):
    """Format mean ± std or 'n/a'."""
    subset = val.dropna()
    if len(subset) == 0:
        return "n/a", 0
    return f"{subset.mean():+.1%}", len(subset)


lines = []

def h(text): lines.append(f"\n{text}")
def p(text): lines.append(text)
def sep(): lines.append("")


# ── Header ─────────────────────────────────────────────────────────────────────
lines.append("# RedInk — Price-Action Correlation Study")
lines.append(f"Run date: {date.today()}")
sep()
p("Tests whether the RedInk anomaly score predicts forward stock underperformance vs QQQ.")
p("Event anchor = filing_date (not report_date). Beta = 2-year rolling weekly CAPM pre-event.")
sep()

# ── Universe ───────────────────────────────────────────────────────────────────
h("## Universe")
p(f"- Scored filings: {len(df):,} | Tickers: {df['ticker'].nunique()}")
p(f"- Date range: {df['filing_date'].min()} → {df['filing_date'].max()}")
for n in FORWARD_Qs:
    col = f"excess_ret_q{n}"
    if col in events.columns:
        p(f"- Events with Q+{n} forward return: {events[col].notna().sum():,}")
sep()

p("| Tier | Score Range | Events |")
p("|---|---|---|")
for t in TIER_ORDER:
    p(f"| {t} | {TIER_RANGES[t]} | {(events['tier']==t).sum():,} |")
sep()

p(f"Beta available for {events['beta'].notna().sum():,} of {len(events):,} events "
  f"(requires ≥{BETA_MIN_WEEKS}w pre-event history).")

# ── A. Average returns by tier ─────────────────────────────────────────────────
h("## A. Average Returns by Score Tier")
sep()
p("Negative = underperformed QQQ after filing date.")
sep()

for metric, label in [
    ("excess_ret",   "Excess Return (stock − QQQ, market-adjusted)"),
    ("abnormal_ret", "Abnormal Return (stock − β·QQQ, beta-adjusted)"),
]:
    p(f"### {label}")
    sep()
    header = "| Tier |" + "".join(f" Q+{n} (N) |" for n in FORWARD_Qs)
    p(header)
    p("|---|" + "---|" * len(FORWARD_Qs))
    for t in TIER_ORDER:
        mask = events["tier"] == t
        cells = [f"| {t}"]
        for n in FORWARD_Qs:
            col = f"{metric}_q{n}"
            if col not in events.columns:
                cells.append("| n/a ")
                continue
            sub = events.loc[mask, col].dropna()
            cells.append(f"| {sub.mean():+.1%} ({len(sub)}) " if len(sub) else "| n/a ")
        cells.append("|")
        p("".join(cells))
    sep()

# ── B. Spearman correlation ────────────────────────────────────────────────────
h("## B. Spearman Correlation — anomaly_score vs Negative Forward Return")
sep()
p("ρ > 0 means higher anomaly score → worse forward return (signal is predictive).")
p("* = p < 0.05. With large N, even small ρ will be significant — focus on magnitude.")
p("Full sample is bidirectional (high score ≠ bad). Beneish subset is directionally negative.")
sep()
p("| Window | N (full) | ρ full | p full | N (Beneish) | ρ Beneish | p Beneish |")
p("|---|---|---|---|---|---|---|")

for n in FORWARD_Qs:
    col = f"excess_ret_q{n}"
    if col not in events.columns:
        p(f"| Q+{n} | — | — | — | — | — | — |")
        continue

    full = events[["anomaly_score", col]].dropna()
    bsub = events.loc[events["beneish_flag"] == True, ["anomaly_score", col]].dropna()

    if len(full) > 10:
        rho_f, p_f = stats.spearmanr(full["anomaly_score"], -full[col])
        sig_f = " *" if p_f < 0.05 else ""
        fs = f"{rho_f:+.3f} | {p_f:.3f}{sig_f}"
    else:
        fs = "n/a | n/a"

    if len(bsub) > 10:
        rho_b, p_b = stats.spearmanr(bsub["anomaly_score"], -bsub[col])
        sig_b = " *" if p_b < 0.05 else ""
        bs = f"{rho_b:+.3f} | {p_b:.3f}{sig_b}"
    else:
        bs = "n/a | n/a"

    p(f"| Q+{n} | {len(full)} | {fs} | {len(bsub)} | {bs} |")

sep()

# ── C. Period stratification ───────────────────────────────────────────────────
h("## C. Period Stratification — Beta-Adjusted Abnormal Return at Q+4")
sep()
p("Key test: if HIGH tier underperforms in Bull quarters too, the signal is alpha.")
p("If underperformance only appears in Bear quarters, it may be beta-exposure, not alpha.")
sep()
p(f"Regime defined by QQQ return in the event quarter: Bull >+5%, Bear <−5%, Neutral otherwise.")
sep()

col4 = "abnormal_ret_q4"
if col4 in events.columns:
    p("| Regime | Tier | N | Avg Abnormal Ret Q+4 |")
    p("|---|---|---|---|")
    for regime in REGIME_ORDER:
        for t in TIER_ORDER:
            mask = (events["regime"] == regime) & (events["tier"] == t) & events[col4].notna()
            sub = events.loc[mask, col4]
            if len(sub) > 0:
                p(f"| {regime} | {t} | {len(sub)} | {sub.mean():+.1%} |")
    sep()

    # Regime counts
    p("Regime distribution of all events:")
    for regime in REGIME_ORDER:
        n_r = (events["regime"] == regime).sum()
        p(f"  {regime}: {n_r} events")
else:
    p("Q+4 data not available.")

sep()

# ── D. Beneish incremental test ────────────────────────────────────────────────
h("## D. Beneish Flag Incremental Test (HIGH tier only)")
sep()
p("Within HIGH-tier events: does Beneish flag add directional predictive power?")
p("Beneish=TRUE rows are directionally negative anomalies (accounting quality concern).")
sep()
p("| Beneish | N | Excess Q+1 | Excess Q+2 | Excess Q+4 |")
p("|---|---|---|---|---|")

high = events[events["tier"] == "HIGH"]
for flag_val, label in [(True, "TRUE (flagged)"), (False, "FALSE (clean)"), (None, "NaN (no data)")]:
    if flag_val is None:
        mask = high["beneish_flag"].isna()
    else:
        mask = high["beneish_flag"] == flag_val
    sub = high[mask]
    cells = [f"| {label} | {len(sub)}"]
    for n in FORWARD_Qs:
        col = f"excess_ret_q{n}"
        if col not in events.columns:
            cells.append("| n/a")
            continue
        valid = sub[col].dropna()
        cells.append(f"| {valid.mean():+.1%} ({len(valid)})" if len(valid) else "| n/a")
    cells.append("|")
    p("".join(cells))

sep()

# ── Caveats ────────────────────────────────────────────────────────────────────
h("## Caveats & Limitations")
sep()
p("1. **Survivorship bias** — QQQ rebalances quarterly. Tickers removed from the index "
  "may be absent from later price history, biasing surviving-ticker returns upward.")
p("2. **Filing lag built in** — event anchor = filing_date, not report_date. "
  "Approx 30–45 day lag already embedded in the design.")
p("3. **Directionality** — anomaly_score is unsigned magnitude (Mahalanobis distance). "
  "High score = anomalous in ANY direction. Beneish subset (Section D) is the "
  "directionally negative test.")
p("4. **Pillar 3 absent** — narrative divergence (MD&A vs. numbers) is not in local CSV. "
  "This study tests Pillar 1 (statistical anomaly) + Pillar 2 (Beneish) only. "
  "Full conviction_tier would be stronger.")
p("5. **Beta estimation data** — early events (2021) require price history back to 2019. "
  "Tickers with limited history will have NaN beta and no abnormal return computation.")

sep()
sep()
lines.append("---")
p(f"Generated by analysis/price_action_study.py | {date.today()}")

# ── Write outputs ──────────────────────────────────────────────────────────────
report = "\n".join(lines)

md_path = RESULTS / "price_action_summary.md"
md_path.write_text(report)

csv_path = RESULTS / "events_with_returns.csv"
events.to_csv(csv_path, index=False)

print("\n" + "=" * 62)
print("RESULTS")
print("=" * 62)
print(report)
print(f"\nReport → {md_path}")
print(f"Raw data → {csv_path}")
