#!/usr/bin/env python3
"""
Directional Split Study — RedInk Predictive Validity (Negative Anomalies Only)

The main price_action_study.py found that HIGH anomaly scores correlate with
OUTPERFORMANCE, not underperformance. The reason: Mahalanobis distance is unsigned
— it captures extremity in any direction, positive or negative.

This study splits events by anomaly direction using the signed z-scores:
  - NEGATIVE anomaly: net z-score weighted toward distress signals (bad direction)
  - POSITIVE anomaly: net z-score weighted toward strength signals (good direction)

The hypothesis being tested here is specifically:
  "Companies with NEGATIVE-direction anomalies underperform QQQ in subsequent quarters."

Direction classification uses combined_z__ columns with explicit feature polarity:
  Positive z = BAD:  debt_to_assets, accrual_ratio, equity_multiplier
  Negative z = BAD:  equity_to_assets, net_margin, ocf_to_net_income, ocf_to_assets
  Ambiguous (skip):  revenue_growth_yoy, assets_growth_yoy, net_income_growth_yoy

Distress score = sum of (z * direction_sign) for 7 directional features.
  distress_score > 0 → NEGATIVE anomaly (things are worse than expected)
  distress_score ≤ 0 → POSITIVE anomaly (things are better than expected)

Input:
  analysis/results/events_with_returns.csv  ← pre-computed from price_action_study.py
  output/quarterly_scores_detailed.csv      ← z-score columns + metadata

Output:
  analysis/results/directional_split_summary.md
  analysis/results/directional_split_events.csv
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

# ── Feature polarity map ───────────────────────────────────────────────────────
# +1  → higher z = BAD (distress increases with positive z)
# -1  → lower z  = BAD (distress increases with negative z, so flip sign)
POLARITY = {
    "combined_z__debt_to_assets":    +1,   # more debt = bad
    "combined_z__equity_to_assets":  -1,   # less equity = bad
    "combined_z__net_margin":        -1,   # lower margin = bad
    "combined_z__ocf_to_net_income": -1,   # weaker cash conversion = bad
    "combined_z__ocf_to_assets":     -1,   # lower operating cash yield = bad
    "combined_z__accrual_ratio":     +1,   # higher accruals = earnings quality concern
    "combined_z__equity_multiplier": +1,   # higher leverage = bad
    # revenue_growth_yoy, assets_growth_yoy, net_income_growth_yoy: ambiguous, excluded
}

FORWARD_Qs   = [1, 2, 4]
TIER_ORDER   = ["HIGH", "MID-HIGH", "MID-LOW", "LOW"]
REGIME_ORDER = ["Bull", "Neutral", "Bear"]


# ── Load and merge ─────────────────────────────────────────────────────────────
print("=" * 62)
print("  RedInk Directional Split Study")
print("=" * 62)

events = pd.read_csv(EVENTS_CSV)
scores = pd.read_csv(SCORES_CSV)

# Normalise beneish flag
scores["beneish_manipulation_flag"] = scores["beneish_manipulation_flag"].map(
    {True: True, False: False, "True": True, "False": False}
)

z_cols = list(POLARITY.keys())
keep   = ["ticker", "calendar_quarter"] + z_cols
merged = events.merge(scores[keep], on=["ticker", "calendar_quarter"], how="left")

print(f"\nEvents loaded: {len(merged):,}")
print(f"Z-score columns available: {sum(c in merged.columns for c in z_cols)}/{len(z_cols)}")


# ── Compute distress score and direction label ─────────────────────────────────
def compute_distress(row):
    total = 0.0
    n_valid = 0
    for col, sign in POLARITY.items():
        val = row.get(col, np.nan)
        if pd.notna(val):
            total += sign * float(val)
            n_valid += 1
    return total if n_valid >= 4 else np.nan   # require at least 4 of 7 features

merged["distress_score"] = merged.apply(compute_distress, axis=1)
merged["direction"] = merged["distress_score"].apply(
    lambda d: "NEGATIVE" if pd.notna(d) and d > 0 else ("POSITIVE" if pd.notna(d) else "UNKNOWN")
)

neg = merged[merged["direction"] == "NEGATIVE"]
pos = merged[merged["direction"] == "POSITIVE"]

print(f"\nDirection classification:")
print(f"  NEGATIVE anomaly (distress > 0): {len(neg):,}")
print(f"  POSITIVE anomaly (distress ≤ 0): {len(pos):,}")
print(f"  UNKNOWN (< 4 z-scores available): {(merged['direction']=='UNKNOWN').sum()}")


# ── Helper ─────────────────────────────────────────────────────────────────────
def mean_ret(df, col):
    sub = df[col].dropna()
    return f"{sub.mean():+.1%} ({len(sub)})" if len(sub) > 0 else "n/a"


# ── Build report ───────────────────────────────────────────────────────────────
lines = []

def h(t): lines.append(f"\n{t}")
def p(t): lines.append(t)
def sep(): lines.append("")

lines.append("# RedInk — Directional Split Study")
lines.append(f"Run date: {date.today()}")
sep()
p("Tests whether NEGATIVE-direction anomalies (distress signals) predict underperformance.")
p("Splits the unsigned anomaly score into directional components using z-score polarity.")
sep()

h("## Direction Classification Method")
sep()
p("Distress score = Σ (z_i × polarity_i) across 7 directional features:")
sep()
p("| Feature | Polarity | Direction logic |")
p("|---|---|---|")
polarity_desc = {
    "combined_z__debt_to_assets":    ("BAD if high", "more leverage"),
    "combined_z__equity_to_assets":  ("BAD if low",  "less equity cushion"),
    "combined_z__net_margin":        ("BAD if low",  "margin compression"),
    "combined_z__ocf_to_net_income": ("BAD if low",  "weak cash conversion"),
    "combined_z__ocf_to_assets":     ("BAD if low",  "low operating cash yield"),
    "combined_z__accrual_ratio":     ("BAD if high", "earnings quality concern"),
    "combined_z__equity_multiplier": ("BAD if high", "leverage amplification"),
}
for col, (direction, logic) in polarity_desc.items():
    feature = col.replace("combined_z__", "")
    p(f"| {feature} | {direction} | {logic} |")
sep()
p("*Excluded (ambiguous): revenue_growth_yoy, assets_growth_yoy, net_income_growth_yoy*")
sep()
p(f"- NEGATIVE anomaly (distress_score > 0): **{len(neg):,} events**")
p(f"- POSITIVE anomaly (distress_score ≤ 0): **{len(pos):,} events**")
p(f"- Required ≥ 4 of 7 features to classify (otherwise UNKNOWN).")

# Direction × Tier breakdown
sep()
h("## Direction × Tier Breakdown")
sep()
p("| Direction | Tier | N |")
p("|---|---|---|")
for direction, subset in [("NEGATIVE", neg), ("POSITIVE", pos)]:
    for t in TIER_ORDER:
        n = (subset["tier"] == t).sum()
        p(f"| {direction} | {t} | {n} |")


# ── A. NEGATIVE vs POSITIVE average excess returns ────────────────────────────
h("## A. NEGATIVE vs POSITIVE — Average Excess Return vs QQQ")
sep()
p("Core test: do NEGATIVE anomalies underperform and POSITIVE anomalies outperform?")
sep()
p("| Direction | Q+1 | Q+2 | Q+4 |")
p("|---|---|---|---|")
for label, subset in [("NEGATIVE", neg), ("POSITIVE", pos), ("ALL events", merged)]:
    cells = [f"| {label}"]
    for n in FORWARD_Qs:
        cells.append(f"| {mean_ret(subset, f'excess_ret_q{n}')}")
    cells.append("|")
    p("".join(cells))

sep()
h("## A2. NEGATIVE vs POSITIVE — Beta-Adjusted Abnormal Return")
sep()
p("| Direction | Q+1 | Q+2 | Q+4 |")
p("|---|---|---|---|")
for label, subset in [("NEGATIVE", neg), ("POSITIVE", pos), ("ALL events", merged)]:
    cells = [f"| {label}"]
    for n in FORWARD_Qs:
        cells.append(f"| {mean_ret(subset, f'abnormal_ret_q{n}')}")
    cells.append("|")
    p("".join(cells))


# ── B. NEGATIVE tier breakdown ────────────────────────────────────────────────
h("## B. NEGATIVE Anomalies — Excess Return by Score Tier")
sep()
p("Within the NEGATIVE direction subset: does a higher anomaly score mean worse returns?")
sep()
p("| Tier | N | Q+1 Excess | Q+2 Excess | Q+4 Excess | Q+4 Abnormal |")
p("|---|---|---|---|---|---|")
for t in TIER_ORDER:
    sub = neg[neg["tier"] == t]
    cells = [f"| {t} | {len(sub)}"]
    for n in FORWARD_Qs:
        cells.append(f"| {mean_ret(sub, f'excess_ret_q{n}')}")
    cells.append(f"| {mean_ret(sub, 'abnormal_ret_q4')}")
    cells.append("|")
    p("".join(cells))


# ── C. Spearman on NEGATIVE subset ────────────────────────────────────────────
h("## C. Spearman Correlation — NEGATIVE Anomalies Only")
sep()
p("Within NEGATIVE events: does higher anomaly_score predict worse forward returns?")
p("ρ > 0 = higher score → worse return. * = p < 0.05.")
sep()
p("| Window | N | ρ (anomaly_score) | p | ρ (distress_score) | p |")
p("|---|---|---|---|---|---|")
for n in FORWARD_Qs:
    col = f"excess_ret_q{n}"
    sub = neg[["anomaly_score", "distress_score", col]].dropna()
    if len(sub) > 10:
        rho_a, p_a = stats.spearmanr(sub["anomaly_score"], -sub[col])
        rho_d, p_d = stats.spearmanr(sub["distress_score"], -sub[col])
        sig_a = " *" if p_a < 0.05 else ""
        sig_d = " *" if p_d < 0.05 else ""
        p(f"| Q+{n} | {len(sub)} "
          f"| {rho_a:+.3f} | {p_a:.3f}{sig_a} "
          f"| {rho_d:+.3f} | {p_d:.3f}{sig_d} |")
    else:
        p(f"| Q+{n} | {len(sub)} | n/a | n/a | n/a | n/a |")


# ── D. Period stratification — NEGATIVE subset ────────────────────────────────
h("## D. Period Stratification — NEGATIVE Anomalies, Beta-Adjusted Q+4")
sep()
p("Key test: does the negative-direction signal hold in Bull markets?")
p("If yes, it's genuine alpha. If only in Bear markets, it may be high-beta exposure.")
sep()
p("| Regime | Tier | N | Avg Abnormal Q+4 |")
p("|---|---|---|---|")
for regime in REGIME_ORDER:
    for t in TIER_ORDER:
        mask = (neg["regime"] == regime) & (neg["tier"] == t) & neg["abnormal_ret_q4"].notna()
        sub = neg.loc[mask, "abnormal_ret_q4"]
        if len(sub) > 0:
            p(f"| {regime} | {t} | {len(sub)} | {sub.mean():+.1%} |")

sep()
p("Regime counts within NEGATIVE events:")
for regime in REGIME_ORDER:
    n_r = (neg["regime"] == regime).sum()
    p(f"  {regime}: {n_r}")


# ── E. Distress score vs excess return scatter summary ────────────────────────
h("## E. Distress Score Quartiles — Excess Return Q+4")
sep()
p("Does a higher distress score (more negative signals) predict worse returns?")
sep()
q_labels = ["Q1 (least distress)", "Q2", "Q3", "Q4 (most distress)"]
neg_valid = neg[["distress_score", "excess_ret_q4"]].dropna()
if len(neg_valid) > 40:
    neg_valid["distress_q"] = pd.qcut(neg_valid["distress_score"], 4, labels=q_labels)
    p("| Distress Quartile | N | Avg Excess Q+4 |")
    p("|---|---|---|")
    for label in q_labels:
        sub = neg_valid[neg_valid["distress_q"] == label]["excess_ret_q4"]
        p(f"| {label} | {len(sub)} | {sub.mean():+.1%} |")
else:
    p("Insufficient data for quartile split.")


# ── Caveats ────────────────────────────────────────────────────────────────────
h("## Caveats")
sep()
p("1. **Direction classification is heuristic.** Feature polarities are CFA-reasoned "
  "but not exhaustive. Three growth features (revenue, assets, net income growth) excluded as ambiguous.")
p("2. **Distress score is unweighted.** All 7 features contribute equally. "
  "A PCA-weighted version might separate signal from noise better.")
p("3. **Signed anomaly ≠ confirmed distress.** A company with mildly negative z-scores "
  "across 7 features scores as NEGATIVE even if the deviation is small.")
p("4. **Pillar 3 absent.** Narrative divergence would sharpen direction classification "
  "— management tone is the strongest directional signal.")
p("5. **Survivorship bias and filing lag** — same as main study.")

sep()
lines.append("---")
p(f"Generated by analysis/directional_split_study.py | {date.today()}")


# ── Write outputs ──────────────────────────────────────────────────────────────
report = "\n".join(lines)
md_path = RESULTS / "directional_split_summary.md"
md_path.write_text(report)

csv_path = RESULTS / "directional_split_events.csv"
merged[["ticker", "calendar_quarter", "filing_date", "anomaly_score", "tier",
        "distress_score", "direction", "beneish_flag", "regime",
        "beta", "excess_ret_q1", "excess_ret_q2", "excess_ret_q4",
        "abnormal_ret_q1", "abnormal_ret_q2", "abnormal_ret_q4"]].to_csv(csv_path, index=False)

print("\n" + "=" * 62)
print("RESULTS")
print("=" * 62)
print(report)
print(f"\nReport  → {md_path}")
print(f"Raw CSV → {csv_path}")
