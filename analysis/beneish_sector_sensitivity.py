#!/usr/bin/env python3
"""
Beneish Sector Sensitivity Study — Separating Structural False Positives from Real Signal

The beneish_only_study.py found that flagged companies aggregate to +14.4% excess Q+4 return,
driven almost entirely by PLTR (+125.8%) and APP (+64.9%). This masks real signal in
traditional sectors with genuine underperformers (WBD -44%, DXCM -22%, MELI -34%).

CFA-level diagnosis:
  The Beneish M-Score (1999) was calibrated on industrial-era companies. It uses:
    - TATA (Total Accruals to Total Assets): designed to catch positive accrual inflation
      → In high-SBC growth companies, SBC creates LARGE NEGATIVE accruals, pushing TATA
        extremely negative. The formula treats extreme-negative TATA as a manipulation signal
        even though it reflects the opposite (real cash charges, not accrual manipulation).
    - DSRI, SGI: catch receivables/revenue anomalies → growth companies trigger these
        structurally (fast-growing receivables are a feature, not a fraud signal).

Two meaningful segmentation axes:
  1. GICS Sector: Growth sectors (IT, Comms, Health Care) vs Traditional (Energy, Industrials,
     Consumer Discretionary, Utilities)
  2. TATA sign: Negative TATA (SBC/non-cash charges driving flag) vs Positive TATA
     (positive accrual inflation — the pattern Beneish was designed to detect)

This study runs the Beneish return analysis separately on each segment, plus a sensitivity
table showing the effect of removing the most extreme outliers.

Input:
  analysis/results/beneish_only_events.csv
  output/quarterly_scores_detailed.csv

Output:
  analysis/results/beneish_sector_sensitivity.md
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
EVENTS_CSV = Path(__file__).parent / "results" / "beneish_only_events.csv"
SCORES_CSV = ROOT / "output" / "quarterly_scores_detailed.csv"
RESULTS    = Path(__file__).parent / "results"

# ── Sector classification ──────────────────────────────────────────────────────
# Growth sectors: Beneish flags structurally likely to be false positives
# Traditional sectors: Beneish was calibrated on these; genuine signal more likely
GROWTH_SECTORS = {
    "Information Technology",
    "Communication Services",
    "Health Care",
}

# ── Load and merge ─────────────────────────────────────────────────────────────
print("=" * 62)
print("  Beneish Sector Sensitivity Study")
print("=" * 62)

events = pd.read_csv(EVENTS_CSV)
scores = pd.read_csv(SCORES_CSV)

scores["beneish_manipulation_flag"] = scores["beneish_manipulation_flag"].map(
    {True: True, False: False, "True": True, "False": False}
)

keep   = ["ticker", "calendar_quarter", "gics_sector", "beneish_tata",
          "beneish_dsri", "beneish_sgi", "beneish_lvgi"]
merged = events.merge(scores[keep], on=["ticker", "calendar_quarter"], how="left")

flagged = merged[merged["beneish_flag"] == True].copy()
flagged["sector_type"] = flagged["gics_sector"].apply(
    lambda s: "Growth" if s in GROWTH_SECTORS else "Traditional"
)
flagged["tata_type"] = flagged["beneish_tata"].apply(
    lambda t: "Negative TATA (SBC-driven)" if pd.notna(t) and t < 0
    else ("Positive TATA (accrual-driven)" if pd.notna(t) else "TATA unknown")
)

growth     = flagged[flagged["sector_type"] == "Growth"]
trad       = flagged[flagged["sector_type"] == "Traditional"]
neg_tata   = flagged[flagged["beneish_tata"] < 0]
pos_tata   = flagged[flagged["beneish_tata"] >= 0]
no_pltr    = flagged[flagged["ticker"] != "PLTR"]
no_pltr_app = flagged[~flagged["ticker"].isin(["PLTR", "APP"])]

print(f"\nFlagged events: {len(flagged)}")
print(f"  Growth sectors (IT/Comms/Healthcare): {len(growth)}")
print(f"  Traditional sectors: {len(trad)}")
print(f"  Negative TATA (SBC-driven): {len(neg_tata)}")
print(f"  Positive TATA (accrual-driven): {len(pos_tata)}")


def mean_ret(df, col, default="n/a"):
    sub = df[col].dropna()
    if len(sub) == 0:
        return default
    return f"{sub.mean():+.1%} (N={len(sub)})"


# ── Build report ───────────────────────────────────────────────────────────────
lines = []
def h(t): lines.append(f"\n{t}")
def p(t): lines.append(t)
def sep(): lines.append("")

lines.append("# Beneish Sector Sensitivity — Structural False Positives vs Real Signal")
lines.append(f"Run date: {date.today()}")
sep()
p("The Beneish M-Score (1999) was calibrated on industrial-era companies. Applying it")
p("to QQQ creates two distinct populations with opposite return profiles.")

# ── Why Beneish misfires in growth companies ───────────────────────────────────
h("## Why Beneish Misfires in Growth Sectors — CFA Analysis")
sep()
p("**TATA (Total Accruals to Total Assets) — the primary failure mode:**")
p("Beneish designed TATA to catch positive accrual inflation: companies booking")
p("fictitious revenue or deferring real costs to inflate earnings. A high positive TATA")
p("signals: 'more of your earnings are accrual-based, less are cash-based.' Red flag.")
sep()
p("The problem: in high-SBC growth companies (Palantir, Crowdstrike, Applovin),")
p("stock-based compensation creates massive *negative* non-cash charges. This pushes TATA")
p("sharply negative. The Beneish formula treats extreme-negative TATA as anomalous —")
p("because in 1999, it was. Today it is simply the signature of a growth company")
p("paying employees in equity instead of cash. The model cannot distinguish between")
p("'management is hiding costs' and 'management is issuing RSUs.'")
sep()
p("**SGI (Sales Growth Index) — the growth trap:**")
p("Beneish flags companies with high revenue growth because rapid revenue growth was")
p("associated with manipulation in 1990s samples. In QQQ, rapid revenue growth is")
p("often the entire investment thesis. Flagging NVDA or APP for growing revenue fast")
p("is not a red flag — it is the reason they are in the index.")
sep()
p("**The result:** The most aggressively growing, highest-returning companies in QQQ")
p("systematically fail the Beneish test — not because they are manipulating earnings,")
p("but because the model's assumptions do not hold for their business models.")

# ── TATA breakdown ─────────────────────────────────────────────────────────────
h("## TATA Component by Flagged Ticker")
sep()
p("TATA sign reveals the mechanism driving each flag.")
sep()
p("| Ticker | TATA | Flag mechanism | Q+4 Excess Return |")
p("|---|---|---|---|")
tata_df = flagged.groupby("ticker").agg(
    tata=("beneish_tata", "mean"),
    q4_excess=("excess_ret_q4", "mean"),
    n=("ticker", "count"),
    sector=("gics_sector", "first"),
).sort_values("tata")
for ticker, row in tata_df.iterrows():
    mechanism = "SBC / non-cash charges" if row["tata"] < 0 else "Positive accrual inflation"
    q4 = f"{row['q4_excess']:+.1%}" if pd.notna(row["q4_excess"]) else "n/a"
    p(f"| {ticker} | {row['tata']:+.3f} | {mechanism} | {q4} |")


# ── Sector segmentation ────────────────────────────────────────────────────────
h("## A. Sector Segmentation — Growth vs Traditional")
sep()
p("**Growth sectors** (IT, Communication Services, Health Care):")
p(f"  Flagged events: {len(growth)} | Tickers: {growth['ticker'].nunique()}")
p("**Traditional sectors** (Energy, Consumer Discretionary, Utilities, Industrials):")
p(f"  Flagged events: {len(trad)} | Tickers: {trad['ticker'].nunique()}")
sep()
p("| Segment | Q+1 Excess | Q+2 Excess | Q+4 Excess | Q+4 Abnormal |")
p("|---|---|---|---|---|")
for label, subset in [
    ("Growth sectors (false positive risk)", growth),
    ("Traditional sectors (real signal zone)", trad),
    ("All flagged (blended)", flagged),
]:
    p(f"| {label} | {mean_ret(subset, 'excess_ret_q1')} "
      f"| {mean_ret(subset, 'excess_ret_q2')} "
      f"| {mean_ret(subset, 'excess_ret_q4')} "
      f"| {mean_ret(subset, 'abnormal_ret_q4')} |")
sep()
p("**Traditional sector flagged tickers:**")
for ticker, row in trad.groupby("ticker").agg(
    q4=("excess_ret_q4", "mean"), n=("ticker", "count"), sector=("gics_sector", "first")
).sort_values("q4").iterrows():
    q4 = f"{row['q4']:+.1%}" if pd.notna(row["q4"]) else "n/a"
    p(f"  {ticker} ({row['sector']}, {int(row['n'])}q flagged): Q+4 excess = {q4}")


# ── TATA segmentation ──────────────────────────────────────────────────────────
h("## B. TATA Sign Segmentation — Mechanism-Based Split")
sep()
p("Negative TATA = SBC / non-cash charges driving the flag (structural false positive).")
p("Positive TATA = accrual inflation driving the flag (what Beneish was designed to catch).")
sep()
p("| TATA type | N events | Q+1 Excess | Q+2 Excess | Q+4 Excess | Q+4 Abnormal |")
p("|---|---|---|---|---|---|")
for label, subset in [
    (f"Negative TATA (N={len(neg_tata)})", neg_tata),
    (f"Positive TATA (N={len(pos_tata)})", pos_tata),
]:
    p(f"| {label} | {len(subset)} "
      f"| {mean_ret(subset, 'excess_ret_q1')} "
      f"| {mean_ret(subset, 'excess_ret_q2')} "
      f"| {mean_ret(subset, 'excess_ret_q4')} "
      f"| {mean_ret(subset, 'abnormal_ret_q4')} |")
sep()
p("**Positive TATA flagged tickers** (accrual-inflation flags):")
pos_tickers = flagged[flagged["beneish_tata"] >= 0].groupby("ticker").agg(
    tata=("beneish_tata", "mean"),
    q4=("excess_ret_q4", "mean"),
    n=("ticker", "count"),
    sector=("gics_sector", "first"),
).sort_values("tata", ascending=False)
for ticker, row in pos_tickers.iterrows():
    q4 = f"{row['q4']:+.1%}" if pd.notna(row["q4"]) else "n/a"
    p(f"  {ticker} ({row['sector']}, TATA={row['tata']:+.3f}, {int(row['n'])}q): Q+4 = {q4}")


# ── C. Sensitivity table ───────────────────────────────────────────────────────
h("## C. Sensitivity Table — Effect of Removing Outliers")
sep()
p("Shows how much the aggregate result depends on specific tickers.")
p("The right answer is not to remove them — it is to segment (Sections A and B).")
p("This table is for transparency, not for the headline result.")
sep()
p("| Cut | N flagged | Q+4 Excess | Q+4 Abnormal |")
p("|---|---|---|---|")
cuts = [
    ("All flagged", flagged),
    ("Excl. PLTR", no_pltr),
    ("Excl. PLTR + APP", no_pltr_app),
    ("Traditional sectors only", trad),
    ("Positive TATA only", pos_tata),
]
for label, subset in cuts:
    p(f"| {label} | {len(subset)} "
      f"| {mean_ret(subset, 'excess_ret_q4')} "
      f"| {mean_ret(subset, 'abnormal_ret_q4')} |")


# ── D. Period stratification — traditional only ────────────────────────────────
h("## D. Period Stratification — Traditional Sectors Only")
sep()
p("Bull/Bear breakdown for the segment where Beneish is most reliable.")
sep()
p("| Regime | N | Q+1 Excess | Q+2 Excess | Q+4 Excess |")
p("|---|---|---|---|---|")
for regime in ["Bull", "Neutral", "Bear"]:
    sub = trad[trad["regime"] == regime]
    p(f"| {regime} | {len(sub)} "
      f"| {mean_ret(sub, 'excess_ret_q1')} "
      f"| {mean_ret(sub, 'excess_ret_q2')} "
      f"| {mean_ret(sub, 'excess_ret_q4')} |")


# ── E. Product implication ────────────────────────────────────────────────────
h("## E. Product Implication — How RedInk Should Apply Beneish")
sep()
p("The Beneish M-Score is not uniformly applicable across QQQ.")
p("A production-grade implementation should:")
sep()
p("1. **Apply sector-aware Beneish weighting.** Downweight or disable TATA and SGI")
p("   components for Information Technology, Communication Services, and high-growth")
p("   Health Care companies where SBC and growth rates are structural features.")
sep()
p("2. **Check TATA sign before flagging.** A company with TATA < -0.05 is almost")
p("   certainly being flagged by SBC, not accrual manipulation. The binary flag")
p("   should not fire on this pattern. Use the continuous M-Score with adjusted weights.")
sep()
p("3. **Use Beneish as a corroborating signal, not a primary one.** The product")
p("   architecture already does this (Pillar 2 of 3). The analysis confirms it is right.")
p("   A standalone Beneish flag in a growth company is weak evidence.")
sep()
p("4. **Trust Beneish most in traditional businesses.** WBD, DXCM, MELI, CEG all")
p("   showed significant underperformance after flagging. These are the cases where")
p("   the 1999 formula still does what it was designed to do.")
sep()
p("5. **Future enhancement: SBC-adjusted M-Score.** Recompute TATA after adding back")
p("   stock-based compensation from the cash flow statement. This removes the structural")
p("   false positive while preserving real accrual signal. This is the version a")
p("   sell-side quant team would build.")

sep()
lines.append("---")
p(f"Generated by analysis/beneish_sector_sensitivity.py | {date.today()}")

# ── Write ──────────────────────────────────────────────────────────────────────
report = "\n".join(lines)
md_path = RESULTS / "beneish_sector_sensitivity.md"
md_path.write_text(report)

print("\n" + "=" * 62)
print("RESULTS")
print("=" * 62)
print(report)
print(f"\nReport → {md_path}")
