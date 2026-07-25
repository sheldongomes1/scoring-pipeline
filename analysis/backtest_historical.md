# Historical Backtest: "Would RedInk Have Caught X?"

**Owner:** Scoring Pipeline Team
**Purpose:** Validate that the anomaly detector fires on known blow-ups before collapse.
**Output:** Marketing + validation artifact. Publishable as a standalone research note.

---

## What This Is Not

This is **not** an LLM eval. This tests the scoring pipeline, not the explanation quality.
- LLM evals answer: *"Is the explanation good?"*
- This answers: *"Is the underlying signal real?"*

---

## Target Blow-Ups

Run the model against the following known blow-ups. For each, the collapse date and probable detection window are listed.

**QQQ-confirmed cases (from qqq-blowup-case-studies.md — within dataset window):**

| Ticker | Direction | Event | Target Signal Quarter | In Snapshot? | Tier in Snapshot |
|---|---|---|---|---|---|
| META | Negative | -74% drawdown (2022) | Q1–Q2 2022 | No — snapshot starts 2024-Q1 | — |
| NFLX | Negative | -72% drawdown (2022) | Q4 2021–Q1 2022 | No — snapshot starts 2024-Q3 | — |
| INTC | Negative | -60% drawdown (2024) | Q4 2023–Q1 2024 | Yes (12 rows) | All WATCH |
| PYPL | Negative | -75% drawdown (2022) | Q3–Q4 2021 | Yes but signal quarters missing | FLAG only |
| APP | Positive | +3500% rally (2023–24) | Q2–Q3 2023 | Yes — 2024-Q1 is ALERT | **ALERT** ✓ |
| NVDA | Positive | +750% rally (2023–24) | Q1 FY2024 (May 2023) | Yes (12 rows) | All WATCH |
| TSLA | Positive | +740% rally (2020) | Q2–Q3 2020 | Yes but 2020 not in window | — |

**Note:** Most blow-up signal quarters predate the current snapshot window. This backtest requires historical pipeline run archives. Recommend: `bq ls qqq-anomaly-lab:qqq_finance` to check for dated archive tables.

**Non-QQQ cases (for extended backtest if pipeline covers them):**

| Company | Ticker | Collapse | Target Detection Window | Failure Type |
|---|---|---|---|---|
| Silicon Valley Bank | SIVB | Mar 2023 | Q3 2022 – Q4 2022 | Duration mismatch, deposit concentration |
| Valeant Pharmaceuticals | VRX | Oct 2015 | Q2–Q3 2015 | Acquisition-driven revenue inflation, accrual manipulation |
| Enron | ENE | Dec 2001 | Q2–Q3 2001 | Off-balance-sheet liabilities, accrual ratio extreme |
| Luckin Coffee | LK | Apr 2020 | Q3–Q4 2019 | Revenue fabrication, accrual anomaly |
| Bed Bath & Beyond | BBBY | Apr 2023 | Q2–Q3 2022 | Inventory destruction, negative OCF |
| WeWork | WE | Oct 2019 (IPO pull) | Q1–Q2 2019 | Negative margin at scale, asset inflation |

Add more if available in the BQ pipeline history.

---

## Methodology

### Step 1 — Pull anomaly scores for target tickers

```sql
SELECT
  ticker,
  calendar_quarter,
  conviction_score,
  conviction_tier,
  pattern_name,
  divergence_label,
  beneish_manipulation_flag
FROM `qqq-anomaly-lab.qqq_finance.top_anomaly_review_pack`
WHERE ticker IN ('SIVB', 'VRX', 'ENE', 'LK', 'BBBY', 'WE')
ORDER BY ticker, calendar_quarter
```

If the pipeline only covers current QQQ holdings, pull from historical snapshots or archived runs.

### Step 2 — Map scores to timeline

For each ticker, build a quarter-by-quarter conviction_score series. Align to the blow-up date. Look for:
- Was `conviction_tier = 'ALERT'` in any quarter within 2 quarters before collapse?
- Did `beneish_manipulation_flag = TRUE` fire before the collapse?
- Did `conviction_score` trend upward across the 4 quarters preceding collapse?

### Step 3 — Define detection criteria

**Caught (strong):** `conviction_tier = 'ALERT'` in ≥1 quarter within the target detection window.
**Caught (weak):** `conviction_tier = 'FLAG'` in ≥2 consecutive quarters within the window.
**Missed:** Only WATCH or no entry in the detection window.

### Step 4 — Produce the result table

| Company | Detection Window | Max Conviction Tier | Beneish Flag | Verdict |
|---|---|---|---|---|
| SVB | Q3–Q4 2022 | ? | ? | CAUGHT / MISSED |
| Valeant | Q2–Q3 2015 | ? | ? | CAUGHT / MISSED |
| … | … | … | … | … |

---

## Connection to Golden Dataset

**Every CAUGHT case is an anchor row candidate for `data/golden_dataset.csv`.**

Why: Blow-up quarters have unambiguous direction labels. `direction = NEGATIVE_ANOMALY` is provably correct — the SEC filings exist, the outcome is historical record. These are the strongest possible ground truth rows for LLM eval calibration.

**Action:** For each CAUGHT case, pull the `cited_passage` from the corresponding 10-Q and add the row to the golden dataset with `divergence_label` manually verified.

---

## Presentation Format

Publish results as a one-page research note in `results/backtest_summary.md`:

```
RedInk Historical Backtest — [Date]

Out of [N] known blow-ups tested:
- [X] CAUGHT at ALERT level within the detection window
- [Y] CAUGHT at FLAG level
- [Z] MISSED

Highlight case: SVB
  Q3 2022: conviction_score = [X], conviction_tier = ALERT, beneish_manipulation_flag = TRUE
  Q4 2022: ...
  Collapse: March 2023

[chart: conviction_score timeline vs. collapse date for each ticker]
```

This format is Demo Day ready and publishable as a LinkedIn post.

---

## Notes for Scoring Pipeline Team

1. The pipeline needs to retain historical snapshots — if the table is WRITE_TRUNCATE on every run, historical blow-up data will not be available unless archived. Recommend: write each pipeline run to a dated partition or archive table.
2. If ENE (Enron) is too old for pipeline coverage, consider substituting a more recent case (e.g., Wirecard `WDI.DE` 2020, or FTX if crypto coverage exists).
3. The detection window assumption (2 quarters before collapse) is conservative. Some blow-ups have 4–6 quarters of leading signal. Extend the window if results are weak.
