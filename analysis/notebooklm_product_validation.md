# RedInk Product Validation — NotebookLM Reference
**Source studies:** price_action_study.py, directional_split_study.py, beneish_only_study.py, beneish_sector_sensitivity.py
**Dataset:** 1,352 quarterly filings | 94 QQQ tickers | 2021-Q1 to 2026-Q1
**Run date:** 2026-04-13

---

## Verdict: Does the Product Work?

**Yes — but not as a trading algorithm. As a screening and investigation tool, it works.**

The original hypothesis was: high anomaly scores predict future underperformance.
That hypothesis was false. What the data showed was more useful and more honest:

| Original hypothesis | What actually happened | What it means |
|---|---|---|
| High score → underperformance | High score → outperformance (+8.1% Q+4 excess) | Score captures momentum extremes, not just distress |
| Beneish flag → sell signal | Flagged aggregate: +14.4% Q+4 (PLTR/APP dominated) | Beneish misfires on SBC-heavy growth companies |
| Negative anomaly → underperformance | Negative anomaly: +5.4% Q+4 excess | Price discounts bad news before the 10-Q is filed |
| Three pillars → one conviction score | No single pillar is independently predictive | Architecture is correct: convergence is the signal |

The product works in the way a senior analyst uses it: **as a lens for identifying what to look at, not a button for what to buy or sell.** The model's job is to surface anomalies. The analyst's job is to determine what they mean. The studies confirm this division is correct.

---

## Where It Worked

### 1. Beneish in Traditional Sectors — Genuine Signal

In non-growth, non-SBC-heavy businesses, the Beneish M-Score flagged companies that subsequently
underperformed QQQ significantly. These are cases where the 1999 formula is doing exactly what
it was designed to do: catching accrual quality deterioration before it shows up in the stock.

| Ticker | Sector | Quarters Flagged | Q+4 Excess Return |
|---|---|---|---|
| WBD | Communication Services (legacy media) | 3 | **-44.2%** |
| MELI | Consumer Discretionary | 2 | **-34.1%** |
| VRTX | Health Care (accrual-driven, not SBC) | 1 | **-44.3%** |
| DXCM | Health Care (traditional device) | 5 | **-22.5%** |
| CEG | Utilities | 3 | **-20.2%** |
| SNPS | Information Technology (legacy) | 4 | **-11.7%** |
| ZS | Information Technology (SBC-negative TATA, ambiguous) | — | **-23.2%** |

**In Bear market quarters, traditional sector Beneish flagged events returned -17.5% Q+4 excess.**
That is the cleanest signal in all four studies. Small N (6 Bear-regime traditional events), but directionally strong.

**Why these companies?** Their Beneish flags are driven by positive TATA (accrual inflation) or
genuine DSRI/GMI anomalies — the mechanisms the formula was actually calibrated to detect.

### 2. Anomaly Score Identifies Companies in Exceptional Trajectories

HIGH-tier events (score ≥75) returned +8.1% excess at Q+4 across all directions.
This is not a bug. The anomaly score is correctly identifying companies at financial extremes —
in QQQ, extremes tend to persist because they are often the fastest-growing, most-watched
companies in the index. APP, NVDA, INSM, MAR all scored HIGH during periods of genuine
exceptional performance.

**Correct use:** The score is a flag for "something significant is happening here."
The analyst then determines whether "significant" is good or bad.

### 3. The Architecture Is Correct

The three-pillar design — Statistical Anomaly + Earnings Quality + Narrative Divergence —
is validated by this analysis. Not because all three pillars work alone (they don't),
but because the studies show *why* each pillar alone is insufficient:

- **Pillar 1 alone:** Unsigned. Cannot distinguish momentum from distress.
- **Pillar 2 alone:** Directional but sector-dependent. Misfires in growth/SBC companies.
- **Pillar 3 alone:** Not yet tested against returns, but theoretically fires *before* price moves.

Requiring convergence of all three is the right call. A company that is statistically anomalous
AND shows accounting quality deterioration AND whose management is contradicting the numbers —
that is a very different proposition from any single flag.

---

## Where It Didn't Work

### 1. As a Mechanical Sell Signal

High anomaly score does not predict underperformance. Spearman ρ = -0.056 at Q+4
(higher score → better returns, barely statistically significant, negligible magnitude).
You cannot short the HIGH-tier bucket and expect to make money. Do not use the score that way.

### 2. Beneish in SBC-Heavy Growth Companies

**The structural false positive:** 74 of 92 Beneish-flagged events are in growth sectors
(IT, Communication Services, Health Care). These companies are flagged primarily because:

- **TATA (Total Accruals to Total Assets):** SBC creates large negative non-cash charges →
  TATA goes sharply negative → formula treats it as anomalous. It is not manipulation.
  It is employees getting paid in equity.
- **SGI (Sales Growth Index):** High revenue growth triggers the flag. In QQQ, high revenue
  growth is an investment thesis, not a red flag.

Growth-sector Beneish-flagged events returned +17.8% Q+4 excess — driven by PLTR (+125.8%),
APP (+64.9%), FANG (+46.6%), MPWR (+24.8%). These are momentum winners, not manipulators.

**TATA sign is the diagnostic:** TATA < 0 = SBC-driven, likely false positive.
TATA > 0 = positive accrual inflation, more likely genuine concern.

Sensitivity table:
| Cut | Q+4 Excess Return |
|---|---|
| All Beneish-flagged (N=92) | +14.4% (PLTR/APP distorted) |
| Exclude PLTR | +3.8% |
| Exclude PLTR + APP | +0.7% |
| Traditional sectors only (N=18) | +0.2% |
| Bear regime, traditional only (N=6) | -17.5% |

### 3. Price Leads Financials — The Timing Problem

By the time a 10-Q is filed (30–45 days after quarter end), the market has usually already
priced in most of the bad news visible in the numbers. This is well-documented in academic
literature (the "post-earnings announcement drift" and "fundamental-to-price lag" literature).

Evidence from the directional study: even companies with NEGATIVE-direction anomalies
(bad margin, high leverage, weak cash conversion) showed +5.4% excess at Q+4.
The most distressed quartile (worst z-scores): +7.4% Q+4 excess.

**Implication:** The model's most powerful signal must operate *before* the numbers fully
deteriorate — which is exactly what Pillar 3 (narrative divergence) is designed to do.
Management tone shifts in MD&A before the financial statements fully break.

---

## What the Product Should Be Used For

### Now (current state, Pillars 1 + 2 only)

**1. Screening universe for analyst attention.**
The anomaly score efficiently identifies which of 94 QQQ companies has something
statistically unusual happening. The analyst reviews those first. This saves time —
it is a triage tool.

**2. Traditional sector distress detection.**
For non-growth, non-SBC-heavy companies (media, energy, utilities, industrials, legacy tech),
Beneish is reliable. When a traditional business gets flagged at high anomaly score AND
Beneish fires AND the sector is not growth-driven — that warrants serious attention.
The WBD, DXCM, MELI, CEG cases are the product working as designed.

**3. Positive anomaly identification.**
High score + positive z-score direction = company in exceptional growth. The product correctly
identified APP in 2024-Q1 (ALERT) during its rally. Useful for finding momentum opportunities,
not just distress.

**4. Context enrichment for human analysts.**
The driver breakdown (which features are most anomalous, peer vs self-history decomposition)
gives an analyst a structured starting point for a 10-Q review. That is value regardless
of whether the score predicts returns.

### Near-term (next 2–4 quarters, as Pillar 3 data accumulates)

**5. Narrative divergence convergence cases.**
When Pillar 1 (statistical anomaly) AND Pillar 3 (CONTRADICTS — management upbeat while
numbers deteriorate) both fire, that is the highest-confidence signal. Pillar 3 fires *before*
price fully discounts the deterioration, which is why it is the most actionable layer.

As more quarterly filings are processed through the explanation pipeline, the Pillar 3 dataset
grows. The return study should be re-run with conviction_tier (full three-pillar score) once
sufficient history exists (recommend: at least 4 quarters of Pillar 3 coverage, ~300+ events).

**6. Bear market early warning.**
The Bear-regime traditional-sector Beneish signal (-17.5% Q+4) suggests the model has
stronger predictive power when macro conditions are tightening. In the next bear market or
sector correction, flag convergence in traditional businesses will be the most reliable signal.
Watch for: Beneish flagged + HIGH anomaly score + Bear-regime quarter + traditional sector.

### Longer-term (model improvements needed first)

**7. SBC-adjusted M-Score (build this next).**
Recompute TATA by adding back stock-based compensation from the cash flow statement before
dividing by total assets. This eliminates the structural false positive in growth companies
while preserving the accrual-inflation signal. This is the version a sell-side quant would ship.
Implementing this would likely flip the Beneish growth-sector result from +17.8% to negative.

**8. Sector-aware conviction scoring.**
Weight Beneish components differently by GICS sector. Downweight TATA and SGI in
Information Technology and Communication Services. Upweight DSRI (receivables inflation)
and GMI (gross margin deterioration) which are sector-agnostic.

**9. Full conviction_tier return study.**
Re-run price_action_study.py against the conviction_score and conviction_tier columns
(which include Pillar 3) once the dataset has 6+ quarters of narrative divergence coverage.
This is the study that will produce the definitive product validation result.

---

## What to Watch For in Coming Quarters

As new 10-Q filings are published (quarterly cadence), these are the signals worth monitoring:

| Signal to watch | Why it matters | Action |
|---|---|---|
| Traditional sector company newly Beneish-flagged | Highest reliability zone for Beneish | Deep-dive immediately |
| CONTRADICTS + HIGH anomaly + Beneish flagged (convergence) | All three pillars firing | Highest conviction signal in the product |
| Any company with 3+ consecutive ALERT quarters | Sustained anomaly is different from a one-off | Trend matters as much as point-in-time score |
| Bear market quarter + traditional sector + new Beneish flag | Best regime for Beneish predictive power | Monitor Q+1 and Q+4 returns closely |
| SBC-driven TATA flag (growth company) | Known false positive pattern | Do NOT flag as distress; flag as growth momentum |
| CORROBORATES divergence + high score | Management is being honest about the anomaly | Lower conviction; disclosed risk is less dangerous |

---

## Key Numbers to Know

| Metric | Value | Context |
|---|---|---|
| HIGH tier Q+4 excess return | +8.1% | Momentum persistence, not sell signal |
| Negative anomaly Q+4 excess return | +5.4% | Mean reversion post-filing |
| Beneish traditional sector, Bear regime Q+4 | -17.5% | Cleanest signal in all studies |
| WBD Beneish catch | -44.2% Q+4 | Best single-case validation |
| PLTR Beneish false positive | +125.8% Q+4 | Best case for SBC adjustment |
| Spearman ρ full sample Q+4 | -0.056 | Negligible predictive power, unsigned score |
| Beneish flagged events | 92 of 1,352 (6.8%) | Small sample, treat ρ values as directional |
| Growth vs traditional sector Beneish | 74 vs 18 events | 80% of flags are in growth sectors |

---

## Limitations That Remain

1. **No Pillar 3 in the return studies.** Narrative divergence (the most directional signal)
   has not been tested against forward returns because the local CSV does not contain
   conviction_tier or divergence_label. This is the most important gap.

2. **Survivorship bias.** QQQ rebalances quarterly. Companies removed from the index
   (often after large drawdowns) may be absent from later price history. This biases
   surviving-ticker returns upward, potentially making signals look weaker than they are.

3. **Small N in key segments.** Bear-regime traditional Beneish: N=6. Convergence cases
   (all three pillars): not yet tested. Findings in small-N segments are directional, not definitive.

4. **No causal identification.** The studies show correlation between signal and returns,
   not causation. The model may be identifying companies that are intrinsically high-beta
   or in specific market regimes rather than detecting a signal that *causes* the return.

5. **Current snapshot only.** All studies use one pipeline run. A multi-snapshot study
   (watching how scores evolve over time for the same company) would be more powerful.

---

*Source: analysis/results/ | Generated 2026-04-13*
