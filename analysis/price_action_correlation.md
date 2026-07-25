# Anomaly-to-Price-Action Correlation Study

**Owner:** Scoring Pipeline Team
**Purpose:** Validate that flagged anomalies precede negative price moves in subsequent quarters.
**Output:** Transforms RedInk from a screening tool into a predictive analytics platform.

---

## What This Is Not

This is **not** an LLM eval. This tests whether the scoring pipeline's signal has real predictive power.
- LLM evals answer: *"Is the explanation good?"*
- This answers: *"Does a high conviction_score mean something bad is coming?"*

If this study is positive, it validates the entire product premise, not just the LLM layer.

---

## Research Question

> Do QQQ holdings flagged at `conviction_tier = 'ALERT'` or `'FLAG'` underperform the QQQ index in the 1, 2, and 4 quarters following the flag?

Secondary questions:
- Does a higher `conviction_score` predict a larger drawdown?
- Does `beneish_manipulation_flag = TRUE` add incremental predictive power beyond `conviction_score` alone?
- Are NEGATIVE_ANOMALY flags more predictive of price decline than POSITIVE_ANOMALY flags?

---

## Data Requirements

### From the Scoring Pipeline
```sql
SELECT
  ticker,
  calendar_quarter,
  conviction_score,
  conviction_tier,
  beneish_manipulation_flag,
  divergence_label
FROM `qqq-anomaly-lab.qqq_finance.top_anomaly_review_pack`
ORDER BY ticker, calendar_quarter
```

Needs historical snapshots — not just the current run. This study requires multiple quarters of pipeline history.

### Price Data (separate source)
For each flagged `(ticker, calendar_quarter)` pair, you need:
- Price at end of flagged quarter (event date)
- Price at end of Q+1, Q+2, Q+4 (forward return windows)
- QQQ index price at same dates (benchmark)

**Suggested source:** Yahoo Finance via `yfinance` Python package, or any internal price data warehouse.

---

## Methodology

### Step 1 — Define the event

An **event** is any row where `conviction_tier IN ('ALERT', 'FLAG')`.

For each event:
- `event_date` = last trading day of `calendar_quarter`
- `forward_return_1q` = (price at end of Q+1 / price at event_date) - 1
- `forward_return_2q` = (price at end of Q+2 / price at event_date) - 1
- `forward_return_4q` = (price at end of Q+4 / price at event_date) - 1
- `excess_return_Xq` = `forward_return_Xq` - QQQ return over same window

### Step 2 — Segment by conviction tier

Compute average excess return by tier and window:

| conviction_tier | N events | Avg Excess Return Q+1 | Avg Excess Return Q+2 | Avg Excess Return Q+4 |
|---|---|---|---|---|
| ALERT | — | — | — | — |
| FLAG | — | — | — | — |
| WATCH (control) | — | — | — | — |

WATCH tier is your control group — if there's no predictive power, all tiers should show ~0 excess return.

### Step 3 — Spearman rank correlation

Test whether `conviction_score` rank-correlates with negative forward excess return.

```python
from scipy.stats import spearmanr
rho, p_value = spearmanr(events['conviction_score'], -events['excess_return_4q'])
```

**Interpretation:**
- `rho > 0.2` and `p < 0.05`: meaningful predictive signal
- `rho > 0.3`: strong signal, lead with this in Demo Day
- `rho < 0.1` or `p > 0.10`: signal is weak, investigate why before publishing

### Step 4 — Beneish flag incremental test

Among ALERT rows only, split by `beneish_manipulation_flag`:

| beneish_manipulation_flag | N | Avg Excess Return Q+4 |
|---|---|---|
| TRUE | — | — |
| FALSE | — | — |

If TRUE rows have meaningfully worse forward returns, `beneish_manipulation_flag` adds predictive value beyond the score alone. That's a product insight worth featuring.

### Step 5 — Event study chart

Plot average cumulative excess return from event date to Q+4, by conviction tier. This is the Demo Day chart.

```
Cumulative excess return
0%  ─────────────────────────── (QQQ baseline)
     Q0    Q+1   Q+2   Q+3   Q+4
      \
  ALERT ──────────────────────── (should trend negative)
   FLAG ──────────────────────── (should trend slightly negative)
  WATCH ──────────────────────── (should stay near 0)
```

---

## Statistical Caveats to Document

1. **Survivorship bias**: QQQ rebalances quarterly. Tickers that got flagged and then removed from the index may be absent from later price history. Note this as a limitation.
2. **Look-ahead bias**: Make sure the `calendar_quarter` field reflects when the filing was available, not the reporting period end date. Filing dates can lag by 30–45 days.
3. **Small sample for ALERT tier**: 32 ALERT rows is a small N for statistical inference. Report confidence intervals, not just point estimates.
4. **Market regime**: If all ALERT flags cluster in a bear market quarter, the negative returns may reflect market beta, not alpha signal. Consider market-adjusting or running a period-stratified analysis.

---

## Presentation Format

Publish in `results/price_action_summary.md`:

```
RedInk Predictive Validity Study — [Date]

Core finding: Holdings flagged at ALERT conviction underperformed QQQ
by an average of [X]% over the following 4 quarters (N=[32], p=[0.0X]).

Spearman correlation between conviction_score and 4Q forward underperformance: ρ = [X], p = [X]

[event study chart]

Beneish flag adds [X]% incremental predictive power among ALERT cases.
```

---

## Notes for Scoring Pipeline Team

1. **Historical archive is prerequisite.** This study cannot run if WRITE_TRUNCATE has erased prior pipeline runs. Recommend implementing a dated archive table (e.g., `top_anomaly_review_pack_YYYYMMDD`) before the next pipeline run.
2. **Forward return calculation needs to be careful about ex-dividend dates** — use adjusted close prices.
3. If the QQQ universe changes significantly quarter-to-quarter, the control group (WATCH tier) comparisons may be noisy. Consider using a static benchmark instead (e.g., Russell 1000).
4. A positive result here is the strongest possible Demo Day opening: *"Before we evaluated whether the LLM explains the signal well, we validated that the signal itself predicts real outcomes."*
