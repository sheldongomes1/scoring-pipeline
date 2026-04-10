# QQQ Anomaly Scoring Pipeline — Task Definition

## What this product does

This pipeline ingests quarterly SEC filings (10-Q) from the QQQ universe (94 companies), scores them for financial anomalies, and produces a structured intelligence layer that tells an equity analyst: **which companies are behaving unusually, why, and whether management is being straight about it**.

The output is not a prediction. It is a signal — a ranked, multi-dimensional view of which companies warrant closer scrutiny, backed by three independent evidence streams that an analyst can act on immediately.

---

## The problem being solved

Equity analysts cover dozens of companies. They cannot read every 10-Q line by line every quarter. They need a way to surface the filings that deserve attention — not just "the stock moved" but "the underlying financial profile is statistically unusual in ways that matter."

Existing screeners surface individual metrics (e.g. "P/E ratio changed"). This pipeline is different in three ways:

1. **Multivariate**: It looks at all financial metrics simultaneously and asks how unusual the combination is — not just one ratio
2. **Context-aware**: It compares each company against itself over time AND against sector peers in the same economic period
3. **Narrative-aware**: It reads what management actually says in the MD&A and asks whether it matches the numbers

---

## Universe

- **94 QQQ companies** (Invesco QQQ ETF constituents — the 100 largest non-financial Nasdaq companies)
- **1,352 quarterly filings** (10-Q) scored across multiple years
- Excludes MSTR (bitcoin treasury company — not an operating business; ratios are meaningless)
- Source data: SEC EDGAR via upstream `qqq-anomaly-lab-repo` → GCS + BigQuery

---

## Pipeline architecture

```
qqq-anomaly-lab-repo  (upstream)
  └── SEC EDGAR ingestion
  └── Feature extraction
  └── Narrative extraction (MD&A, risk factors)
  └── GCS storage + BigQuery (qqq_anomaly.filings)
          │
          ▼
scoring-pipeline  (this repo)
  Step 1  flatten_bq.py                     BQ → period_features.json
  Step 2  score_quarterly_anomalies.py       Anomaly scoring → BQ quarterly_scores_detailed
  Step 3  generate_explanations.py           LLM analyst briefs → BQ anomaly_explanations
  Step 4  score_narrative_divergence.py      MD&A divergence → BQ narrative_divergence
  Step 5  compute_conviction.py              Three-pillar synthesis → BQ conviction_scores
  Step 6  build_master_output.py             BQ view + review pack → BQ filing_intelligence + top_anomaly_review_pack
  Step 7  build_trend_table.py               Time-series table → BQ company_trend
          │
          ▼
redink-ui          ← queries company_trend, filing_intelligence, top_anomaly_review_pack
qqq-eval-suite     ← queries top_anomaly_review_pack
```

**Run the full pipeline:**
```bash
python scripts/run_pipeline.py
```

---

## Step-by-step detail

### Step 1 — Flatten BQ → period_features.json
**Script:** `scripts/flatten_bq.py`
**Input:** `qqq_anomaly.filings` (BigQuery)
**Output:** `output/period_features.json`, `output/feature_keys.json`

Reads all 10-Q filings from the upstream BigQuery table. Each row is one quarterly filing for one company, containing 10 engineered financial ratios plus 24 raw balance sheet fields (for Beneish). Writes to a local JSON file for the scoring step.

---

### Step 2 — Anomaly scoring → quarterly_scores_detailed
**Script:** `scripts/score_quarterly_anomalies.py`
**Input:** `output/period_features.json`
**Output:** `output/quarterly_scores_detailed.csv` → BQ `quarterly_scores_detailed`

This is the core quantitative scoring step. Seven sub-steps:

**Step 2a — Winsorize**
Each feature is clipped at the 5th and 95th percentile across the full dataset. This prevents a single extreme outlier from distorting the z-score distribution.

**Step 2b — Self-history z-scores**
For each company, compute how unusual this quarter's metrics are relative to that company's own historical distribution. Uses robust statistics: median and IQR rather than mean/std, making the score resistant to the company's own past outliers.

Formula: `z = (value − median) / (IQR / 1.35)`

The 1.35 factor normalises IQR to approximate standard deviation units for a normal distribution. This is the same approach used in robust statistical process control.

Result: How unusual is this quarter for *this company* compared to its own history?

**Step 2c — Peer z-scores (sector-adjusted)**
For each feature, compute z-scores relative to all companies in the same GICS sector reporting in the same calendar quarter. Grouping by calendar quarter (not exact report date) ensures companies with different fiscal year-ends are still compared as peers if they represent the same economic period.

Fallback: when a sector has fewer than 5 companies in a given quarter, fall back to universe-wide peers to preserve coverage.

Result: How unusual is this company compared to sector peers *at the same point in time*?

**Step 2d — Combine z-scores**
Simple average of self-history and peer z-scores per feature, clipped to ±8.0 to prevent any single unstable ratio from dominating.

**Step 2e — Mahalanobis distance (MCD)**
The combined z-scores per feature are fed into a Minimum Covariance Determinant (MCD) Mahalanobis distance computation. This produces a single scalar anomaly score that captures the multivariate unusualness of a filing — taking into account correlations between features rather than treating each in isolation.

The MCD estimator (support_fraction=0.75) is robust to outliers: it fits the covariance matrix on the cleanest 75% of the data.

**Step 2f — Percentile-scale to 0–100**
The Mahalanobis distances are converted to percentile ranks across the full dataset and scaled to 0–100. A score of 90 means this filing is more unusual than 90% of all filings.

**Step 2g — Beneish M-Score**
For each filing, compute the Beneish M-Score (Beneish, 1999) — an accounting-based earnings manipulation model using 8 financial ratios comparing current period to prior year same period:

- DSRI: Days sales receivable index — rising receivables faster than revenue
- GMI: Gross margin index — margin deterioration
- AQI: Asset quality index — capitalising operating expenses
- SGI: Sales growth index — high growth = more incentive to manipulate
- DEPI: Depreciation index — slowing depreciation rate
- SGAI: SG&A expense index — operating leverage deterioration
- TATA: Total accruals to total assets — earnings not backed by cash
- LVGI: Leverage index — rising debt burden

M = −4.84 + 0.920×DSRI + 0.528×GMI + 0.404×AQI + 0.892×SGI + 0.115×DEPI − 0.172×SGAI + 4.679×TATA − 0.327×LVGI

**M > −2.22 = likely manipulator** (Beneish threshold)

Coverage: 911/1,352 filings scored (67%). Prior-year data unavailable for first year of coverage per ticker.

**Output columns include:**
- `anomaly_score_0_100` — MCD percentile score (0–100)
- `self_history_score`, `peer_relative_score`, `combined_signal_strength`
- `top_driver_1/2/3` + values — features with highest |combined_z|
- `peer_count` — size of the peer group used (low = lower confidence)
- `combined_z__*`, `self_z__*`, `peer_z__*` — per-feature z-scores (30 columns)
- `beneish_m_score`, `beneish_manipulation_flag`, `beneish_dsri/gmi/...` — Beneish components

---

### Step 3 — LLM analyst briefs → anomaly_explanations
**Script:** `explanations/generate_explanations.py`
**Input:** BQ `quarterly_scores_detailed` (alert_score ≥ 5)
**Output:** BQ `anomaly_explanations`
**Model:** claude-sonnet-4-6, temperature=0.3

For each qualifying filing, Claude receives the top 5 anomalous features (with z-scores and direction), the Beneish M-Score status, and the alert score. It returns structured JSON:

- `pattern_name` — one of 7 named patterns: Earnings Quality Risk, Growth Bubble, Financial Distress, Aggressive Asset Expansion, Recovery, M&A Distortion, Idiosyncratic
- `pattern_confidence` — high / medium / low
- `pattern_summary` — one sentence for a portfolio manager scanning a list
- `explanation_brief` — three paragraphs: (1) what is happening, (2) why it matters / testable hypotheses, (3) what to do next

**Alert score threshold (minimum 5 to qualify):**
```
alert_score = (z_flag_count × 1) + (mahal_flag × 3) + (beneish_flag × 5)
```
Where z_flag_count = features with |combined_z| > 2.0, mahal_flag = anomaly_score ≥ 80, beneish_flag = M > −2.22.

163 filings qualify. 163 briefs generated.

---

### Step 4 — Narrative divergence → narrative_divergence
**Script:** `explanations/score_narrative_divergence.py`
**Input:** BQ `quarterly_scores_detailed` + MD&A text from GCS (`qqq/narrative/{TICKER}/`)
**Output:** BQ `narrative_divergence`
**Model:** claude-sonnet-4-6, temperature=0.2

For each qualifying filing, Claude receives the full MD&A text (typically 20,000–80,000 characters) plus the quantitative anomaly profile and asks: **does management's language match what the numbers show?**

Output:
- `divergence_label` — `CONTRADICTS` / `CORROBORATES` / `NEUTRAL`
- `confidence_score` — 0.0–1.0
- `cited_passage` — verbatim quote from the MD&A that supports the label
- `rationale` — 2–3 sentences explaining the divergence
- `mda_tone` — `BULLISH` / `CAUTIOUS` / `NEUTRAL` / `MIXED`
- `anomaly_acknowledged` — did management explicitly discuss the anomalous metric?

**Why this matters:**
`CONTRADICTS` is the high-value signal. When a company's metrics are deteriorating but management's MD&A is upbeat and evasive — that divergence is one of the most actionable signals in equity research. `CORROBORATES` means management disclosed the problem (lower concern — acknowledged risk is priced). `NEUTRAL` means no signal either way.

No embeddings, no vector database. The full MD&A is passed directly to Claude in one prompt. Claude Sonnet 4.6 has a 200K token context window; even an 80,000-character MD&A fits easily. Chunking would lose narrative arc.

161 filings scored. Majority return CONTRADICTS.

---

### Step 5 — Three-pillar conviction score → conviction_scores
**Script:** `explanations/compute_conviction.py`
**Input:** BQ `quarterly_scores_detailed` + BQ `narrative_divergence`
**Output:** BQ `conviction_scores`

Synthesises all three independent evidence streams into a single `conviction_score` (0–100) and `conviction_tier` (ALERT / FLAG / WATCH).

**Why three pillars?** Each was built on completely different data and methodology:

| Pillar | Data | Method |
|---|---|---|
| Statistical anomaly | Financial ratio time series | MCD Mahalanobis |
| Earnings quality | Raw balance sheet fundamentals | Beneish M-Score (1999 academic formula) |
| Narrative transparency | Management MD&A language | LLM / NLP |

When three independent systems agree, the probability of a false positive drops dramatically. This is signal convergence, not noise.

**Pillar formulas:**

**Pillar 1 — Statistical Anomaly (0–40 pts)**
`= anomaly_score_0_100 × 0.40`

**Pillar 2 — Earnings Quality (0–35 pts)**
Uses the continuous Beneish M-Score (not just the binary flag). Normalised between M=−6.0 (very clean, 0 pts) and M=+2.0 (maximum risk, 35 pts). A company at M=−2.23 (barely flagged) scores very differently from M=+1.5 (deeply suspicious).

**Pillar 3 — Management Transparency (−10 to +25 pts)**
- `CONTRADICTS`: +confidence × 25 (concealment amplifies conviction)
- `CORROBORATES`: −confidence × 10 (acknowledged risk reduces conviction — it's disclosed)
- `NEUTRAL` / no narrative: 0

`conviction_score = pillar_1 + pillar_2 + pillar_3`, clamped to [0, 100]

**Conviction tiers:**
- `ALERT` (≥ 65): All three pillars firing. Lowest false-positive probability. 32 filings.
- `FLAG` (40–64): Two pillars contributing meaningfully. 274 filings.
- `WATCH` (20–39): One meaningful signal. Monitor. 731 filings.
- No tier (< 20): Below noise threshold. 315 filings.

---

### Step 6 — Master output → filing_intelligence view + top_anomaly_review_pack
**Script:** `scripts/build_master_output.py`
**Output:** BQ view `filing_intelligence` + BQ table `top_anomaly_review_pack`

**`filing_intelligence` (BQ view, 1,352 rows):**
A live SQL view joining all four pipeline output tables. Always reflects the latest pipeline run. 50 columns covering identity, quantitative scores, Beneish, conviction, analyst brief, and narrative divergence in one flat row per filing. Used for ad-hoc analysis and full universe lookups.

**`top_anomaly_review_pack` (BQ table, 1,037 rows):**
Materialised subset: ALERT + FLAG + WATCH tier filings sorted by conviction_score. Stored table — fast to query, stable snapshot. Used by `redink-ui` (UI) and `qqq-eval-suite` (eval). Replaced on each pipeline run.

---

### Step 7 — Trend table → company_trend
**Script:** `scripts/build_trend_table.py`
**Output:** BQ table `company_trend` (clustered by ticker)

One row per (ticker, calendar_quarter) for all 1,352 filings. Pre-joined, pre-formatted. The UI queries a single ticker and gets a flat array ready to feed directly into a chart library — no pivoting, no joins, no client-side computation.

Columns: identity, conviction pillars, anomaly score, Beneish, 10 feature z-scores (z_net_margin etc.), narrative signals (divergence_label, mda_tone, pattern_name).

**UI query:**
```sql
SELECT * FROM `qqq-anomaly-lab.qqq_finance.company_trend`
WHERE ticker = 'INSM'
ORDER BY report_date
```

Clustered by ticker — BQ skips all other ticker data on query, making lookups fast regardless of total table size.

---

## BigQuery output tables

| Table | Rows | Description | Primary consumer |
|---|---|---|---|
| `quarterly_scores_detailed` | 1,352 | Full quantitative scores + z-scores + Beneish | Pipeline internal |
| `anomaly_explanations` | 163 | LLM analyst briefs (pattern + 3-paragraph brief) | Pipeline internal |
| `narrative_divergence` | 161 | MD&A vs. numbers: label + cited passage + rationale | Pipeline internal |
| `conviction_scores` | 1,352 | Three-pillar conviction score + tier | Pipeline internal |
| `filing_intelligence` | 1,352 | BQ view joining all four tables (50 cols) | Ad-hoc analysis |
| `top_anomaly_review_pack` | 1,037 | Materialised ALERT+FLAG+WATCH filings | `qqq-eval-suite` |
| `company_trend` | 1,352 | Per-ticker time-series for chart rendering | `redink-ui` |

---

## Scoring methodology principles

**Robust statistics over classical statistics**
All z-scores use median and IQR rather than mean and standard deviation. This makes scores resistant to outliers — a company with one extreme quarter doesn't distort its own baseline.

**Self-history AND peer comparison**
Combining both perspectives catches different kinds of anomalies. Self-history alone misses sector-wide shifts. Peer comparison alone misses idiosyncratic company changes. The combination catches both.

**Calendar quarter grouping for peers**
Companies are compared as peers if their fiscal quarters represent the same economic period (Q1 = Jan–Mar, regardless of fiscal year end) — not just if they filed on the same date. A company with a Jan 31 year-end and one with a Mar 31 year-end are both Q1 peers.

**Sector-adjusted peer groups**
Primary peer group: same (calendar_quarter, GICS sector). Fallback to universe when sector group < 5 companies. peer_count column records the actual group size used.

**Continuous Beneish, not binary**
The Beneish M-Score is used as a continuous variable in the conviction score — not just the binary flag. A company at M = −2.23 (barely flagged) is treated very differently from M = +1.5 (deeply in manipulation territory).

**Narrative transparency reduces conviction**
CORROBORATES (management acknowledged the anomaly) reduces conviction by up to 10 points. Acknowledged risk is disclosed risk — the market has likely already processed it. The most dangerous case is CONTRADICTS: bad numbers, upbeat management.

---

## Key design decisions

**No RAG / no embeddings for narrative analysis**
The full MD&A (~4,000–6,000 tokens) is passed directly to Claude in one prompt. Chunking into smaller pieces for retrieval would lose narrative arc — a reassuring statement in paragraph 2 might contradict an admission in paragraph 8. With Claude's 200K token context window, there is no reason to chunk.

**BQ as the single source of truth**
All inputs and outputs are in BigQuery. No dependency on local files for production runs. The scoring pipeline reads from BQ, writes to BQ, and the UI/eval suite query BQ directly.

**Separation of scoring and explanation layers**
The quantitative scoring (Steps 1–2) is fully independent of the LLM explanation layers (Steps 3–4). Scoring can be re-run without re-generating explanations. Explanations can be refined without re-scoring. This keeps the pipeline modular and the expensive LLM calls optional.

---

## Related repositories

| Repo | Role |
|---|---|
| `qqq-anomaly-lab-repo` | Data layer — SEC EDGAR ingestion, feature extraction, GCS/BQ upload |
| `scoring-pipeline` | This repo — scoring, explanation, and intelligence layer |
| `qqq-eval-suite` | Eval layer — quality gates, explanation quality testing |
| `redink-ui` | UI layer — serves scores and intelligence to analysts |
