#!/usr/bin/env python3
"""
Beneish-Only Study — Does the 1999 Earnings Manipulation Model Predict Returns?

Tests whether Beneish M-Score flags alone — with no score tier filter — predict
underperformance in QQQ holdings in subsequent quarters.

This is a clean directional test. Unlike the anomaly score (unsigned magnitude),
Beneish is inherently negative-directional: it flags specific accounting quality
deterioration patterns associated with earnings manipulation.

Three angles:
  1. Flagged vs non-flagged excess/abnormal return comparison
  2. Continuous M-Score Spearman correlation (higher M = worse quality = worse returns?)
  3. M-Score quartile breakdown within flagged events
  4. Period stratification: does the signal hold in Bull markets?

The manipulation threshold is M > -2.22 (Beneish 1999).
Higher M-Score (less negative / positive) = higher manipulation probability.

Input:
  analysis/results/events_with_returns.csv  ← pre-computed from price_action_study.py
  output/quarterly_scores_detailed.csv      ← beneish_m_score (continuous)

Output:
  analysis/results/beneish_only_summary.md
  analysis/results/beneish_only_events.csv
"""

import warnings
warnings.filterwarnings("ignore")

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
from scipy import stats

# ── Paths ──────────────────────────────────────────────────────────────────────
ROOT       = Path(__file__).parent.parent
EVENTS_CSV = Path(__file__).parent / "results" / "events_with_returns.csv"
SCORES_CSV = ROOT / "output" / "quarterly_scores_detailed.csv"
RESULTS    = Path(__file__).parent / "results"

BENEISH_THRESHOLD = -2.22   # Beneish 1999 manipulation flag threshold
FORWARD_Qs        = [1, 2, 4]
TIER_ORDER        = ["HIGH", "MID-HIGH", "MID-LOW", "LOW"]
REGIME_ORDER      = ["Bull", "Neutral", "Bear"]


# ── Load and merge ─────────────────────────────────────────────────────────────
print("=" * 62)
print("  RedInk Beneish-Only Study")
print("=" * 62)

events = pd.read_csv(EVENTS_CSV)
scores = pd.read_csv(SCORES_CSV)

scores["beneish_manipulation_flag"] = scores["beneish_manipulation_flag"].map(
    {True: True, False: False, "True": True, "False": False}
)

keep   = ["ticker", "calendar_quarter", "beneish_m_score", "beneish_components_available"]
merged = events.merge(scores[keep], on=["ticker", "calendar_quarter"], how="left")

flagged   = merged[merged["beneish_flag"] == True]
unflagged = merged[merged["beneish_flag"] == False]
no_data   = merged[merged["beneish_flag"].isna()]

print(f"\nEvents loaded: {len(merged):,}")
print(f"  Beneish flagged (M > {BENEISH_THRESHOLD}): {len(flagged)}")
print(f"  Beneish clean:                            {len(unflagged)}")
print(f"  Beneish not computed (< 5 components):   {len(no_data)}")
print(f"\nContinuous M-Score stats (flagged events):")
if len(flagged["beneish_m_score"].dropna()) > 0:
    ms = flagged["beneish_m_score"].dropna()
    print(f"  Mean: {ms.mean():.3f}  Std: {ms.std():.3f}  Min: {ms.min():.3f}  Max: {ms.max():.3f}")


# ── Helper ─────────────────────────────────────────────────────────────────────
def mean_ret(df, col):
    sub = df[col].dropna()
    return f"{sub.mean():+.1%} ({len(sub)})" if len(sub) > 0 else "n/a"


# ── Build report ───────────────────────────────────────────────────────────────
lines = []

def h(t): lines.append(f"\n{t}")
def p(t): lines.append(t)
def sep(): lines.append("")

lines.append("# RedInk — Beneish M-Score Predictive Validity Study")
lines.append(f"Run date: {date.today()}")
sep()
p("Tests whether the Beneish M-Score (Beneish 1999) predicts stock underperformance in QQQ.")
p("Unlike the anomaly score, Beneish is directionally negative: it flags specific accounting")
p("deterioration patterns tied to earnings manipulation. This is the cleanest directional test.")
sep()

h("## Universe")
sep()
p(f"- Total events: {len(merged):,} | Tickers: {merged['ticker'].nunique()}")
p(f"- Beneish flagged (M > {BENEISH_THRESHOLD}): **{len(flagged)}** events across "
  f"{flagged['ticker'].nunique()} tickers")
p(f"- Beneish clean: {len(unflagged)} events")
p(f"- Beneish not computed: {len(no_data)} events (insufficient balance sheet components)")
sep()
p("Flagged event breakdown by tier:")
p("| Tier | Flagged | Clean | Not computed |")
p("|---|---|---|---|")
for t in TIER_ORDER:
    nf = (flagged["tier"] == t).sum()
    nc = (unflagged["tier"] == t).sum()
    nn = (no_data["tier"] == t).sum()
    p(f"| {t} | {nf} | {nc} | {nn} |")
sep()
p("Continuous M-Score distribution (flagged events only):")
ms_flagged = flagged["beneish_m_score"].dropna()
if len(ms_flagged) > 0:
    for pct, label in [(25, "25th pct"), (50, "Median"), (75, "75th pct"), (100, "Max")]:
        val = np.percentile(ms_flagged, pct)
        p(f"  {label}: {val:.3f}")


# ── A. Flagged vs clean excess returns ────────────────────────────────────────
h("## A. Flagged vs Clean — Average Excess and Abnormal Return")
sep()
p("Core question: do Beneish-flagged companies underperform QQQ in subsequent quarters?")
sep()
p("| Group | Q+1 Excess | Q+2 Excess | Q+4 Excess | Q+4 Abnormal |")
p("|---|---|---|---|---|")
for label, subset in [
    (f"Flagged (N={len(flagged)})", flagged),
    (f"Clean (N={len(unflagged)})", unflagged),
    (f"All events (N={len(merged)})", merged),
]:
    cells = [f"| {label}"]
    for n in FORWARD_Qs:
        cells.append(f"| {mean_ret(subset, f'excess_ret_q{n}')}")
    cells.append(f"| {mean_ret(subset, 'abnormal_ret_q4')}")
    cells.append("|")
    p("".join(cells))


# ── B. Spearman: continuous M-Score vs forward return ────────────────────────
h("## B. Spearman Correlation — Continuous M-Score vs Negative Forward Return")
sep()
p(f"Higher M-Score (less negative) = higher manipulation probability.")
p(f"ρ > 0 means higher M-Score → worse forward return. * = p < 0.05.")
p(f"Tested on: (i) all events with M-Score, (ii) flagged only (M > {BENEISH_THRESHOLD}).")
sep()
p("| Window | N (all w/ M) | ρ all | p | N (flagged) | ρ flagged | p |")
p("|---|---|---|---|---|---|---|")

for n in FORWARD_Qs:
    col = f"excess_ret_q{n}"
    all_m  = merged[["beneish_m_score", col]].dropna()
    flag_m = flagged[["beneish_m_score", col]].dropna()

    if len(all_m) > 10:
        rho_a, p_a = stats.spearmanr(all_m["beneish_m_score"], -all_m[col])
        sig_a = " *" if p_a < 0.05 else ""
        a_str = f"{rho_a:+.3f} | {p_a:.3f}{sig_a}"
    else:
        a_str = "n/a | n/a"

    if len(flag_m) > 10:
        rho_f, p_f = stats.spearmanr(flag_m["beneish_m_score"], -flag_m[col])
        sig_f = " *" if p_f < 0.05 else ""
        f_str = f"{rho_f:+.3f} | {p_f:.3f}{sig_f}"
    else:
        f_str = f"n/a (N={len(flag_m)}) | n/a"

    p(f"| Q+{n} | {len(all_m)} | {a_str} | {len(flag_m)} | {f_str} |")


# ── C. M-Score quartile breakdown (flagged only) ──────────────────────────────
h("## C. M-Score Severity Quartiles — Flagged Events Only")
sep()
p("Within flagged events: does a worse (higher) M-Score predict worse returns?")
p("Q4 = most extreme M-Score (highest manipulation probability).")
sep()

flag_valid = flagged[["beneish_m_score", "excess_ret_q4", "abnormal_ret_q4"]].dropna()
if len(flag_valid) >= 20:
    q_labels = ["Q1 (mildly flagged)", "Q2", "Q3", "Q4 (most extreme)"]
    flag_valid = flag_valid.copy()
    flag_valid["m_quartile"] = pd.qcut(flag_valid["beneish_m_score"], 4, labels=q_labels)
    p("| M-Score Quartile | N | Avg Excess Q+4 | Avg Abnormal Q+4 |")
    p("|---|---|---|---|")
    for label in q_labels:
        sub = flag_valid[flag_valid["m_quartile"] == label]
        exc = sub["excess_ret_q4"].dropna()
        abn = sub["abnormal_ret_q4"].dropna()
        exc_str = f"{exc.mean():+.1%}" if len(exc) else "n/a"
        abn_str = f"{abn.mean():+.1%}" if len(abn) else "n/a"
        p(f"| {label} | {len(sub)} | {exc_str} | {abn_str} |")
else:
    p(f"Only {len(flag_valid)} flagged events with M-Score + Q+4 data — insufficient for quartile split.")


# ── D. Period stratification ───────────────────────────────────────────────────
h("## D. Period Stratification — Flagged Events by Market Regime")
sep()
p("Key test: do Beneish-flagged companies underperform in Bull markets too?")
p("If yes: the signal is alpha (real predictive power), not beta exposure.")
sep()
p("| Regime | Group | N | Excess Q+1 | Excess Q+2 | Excess Q+4 |")
p("|---|---|---|---|---|---|")
for regime in REGIME_ORDER:
    for label, subset in [("Flagged", flagged), ("Clean", unflagged)]:
        mask = subset["regime"] == regime
        sub = subset[mask]
        cells = [f"| {regime} | {label} | {len(sub)}"]
        for n in FORWARD_Qs:
            cells.append(f"| {mean_ret(sub, f'excess_ret_q{n}')}")
        cells.append("|")
        p("".join(cells))


# ── E. Flagged events by ticker — top repeated offenders ─────────────────────
h("## E. Most Frequently Flagged Tickers")
sep()
p("Tickers flagged in the most quarterly filings.")
sep()
ticker_counts = flagged.groupby("ticker").agg(
    quarters_flagged=("calendar_quarter", "count"),
    avg_excess_q4=("excess_ret_q4", "mean"),
    avg_abnormal_q4=("abnormal_ret_q4", "mean"),
).sort_values("quarters_flagged", ascending=False).head(15)

p("| Ticker | Quarters Flagged | Avg Excess Q+4 | Avg Abnormal Q+4 |")
p("|---|---|---|---|")
for ticker, row in ticker_counts.iterrows():
    exc = f"{row['avg_excess_q4']:+.1%}" if pd.notna(row['avg_excess_q4']) else "n/a"
    abn = f"{row['avg_abnormal_q4']:+.1%}" if pd.notna(row['avg_abnormal_q4']) else "n/a"
    p(f"| {ticker} | {int(row['quarters_flagged'])} | {exc} | {abn} |")


# ── F. Beneish flag + HIGH anomaly score — the convergence case ───────────────
h("## F. Convergence: Beneish Flagged AND HIGH Anomaly Score")
sep()
p("The product's strongest signal: statistical anomaly AND accounting quality concern.")
p("Pillar 1 + Pillar 2 firing simultaneously.")
sep()

both_high = merged[(merged["beneish_flag"] == True) & (merged["tier"] == "HIGH")]
both_clean = merged[(merged["beneish_flag"] == False) & (merged["tier"] == "HIGH")]

p("| Condition | N | Excess Q+1 | Excess Q+2 | Excess Q+4 | Abnormal Q+4 |")
p("|---|---|---|---|---|---|")
for label, subset in [
    ("HIGH + Beneish flagged (both pillars)", both_high),
    ("HIGH + Beneish clean (Pillar 1 only)", both_clean),
]:
    cells = [f"| {label} | {len(subset)}"]
    for n in FORWARD_Qs:
        cells.append(f"| {mean_ret(subset, f'excess_ret_q{n}')}")
    cells.append(f"| {mean_ret(subset, 'abnormal_ret_q4')}")
    cells.append("|")
    p("".join(cells))


# ── Caveats ────────────────────────────────────────────────────────────────────
h("## Caveats")
sep()
p(f"1. **Small N for flagged events** — {len(flagged)} total flagged events. "
  "Statistical inference is weak; treat ρ values and averages as directional, not definitive.")
p("2. **Beneish designed for industrials** — the M-Score uses ratios that behave differently "
  "for high-growth SaaS, biotech, and asset-light businesses common in QQQ. "
  "False positives are expected in these sectors.")
p("3. **Continuous M-Score availability** — only 911/1,352 events have M-Score computed "
  "(requires ≥5 Beneish components). Flagged events may skew toward companies with richer data.")
p("4. **Survivorship bias and filing lag** — same as main study.")
p("5. **Pillar 3 absent** — narrative divergence would sharpen the signal, especially "
  "for distinguishing intentional manipulation from structural accounting differences.")

sep()
lines.append("---")
p(f"Generated by analysis/beneish_only_study.py | {date.today()}")


# ── Write outputs ──────────────────────────────────────────────────────────────
report = "\n".join(lines)
md_path = RESULTS / "beneish_only_summary.md"
md_path.write_text(report)

out_cols = ["ticker", "calendar_quarter", "filing_date", "tier", "anomaly_score",
            "beneish_flag", "beneish_m_score", "beneish_components_available",
            "regime", "beta",
            "excess_ret_q1", "excess_ret_q2", "excess_ret_q4",
            "abnormal_ret_q1", "abnormal_ret_q2", "abnormal_ret_q4"]
merged[[c for c in out_cols if c in merged.columns]].to_csv(
    RESULTS / "beneish_only_events.csv", index=False
)

print("\n" + "=" * 62)
print("RESULTS")
print("=" * 62)
print(report)
print(f"\nReport  → {md_path}")
print(f"Raw CSV → {RESULTS / 'beneish_only_events.csv'}")
