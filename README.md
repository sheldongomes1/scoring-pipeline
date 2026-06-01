# QQQ Anomaly Scoring Pipeline

Scores quarterly SEC filings (10-Q) from the QQQ universe for financial anomalies and produces a structured intelligence layer that tells an equity analyst: **which companies are behaving unusually, why, and whether management is being straight about it.**

Built on top of the `qqq-anomaly-lab-repo` data layer. Output is consumed by `redink-ui` (analyst UI) and `qqq-eval-suite` (quality evaluation).

---

## What this does

Equity analysts cover dozens of companies and cannot read every 10-Q line by line. This pipeline surfaces the filings that deserve attention — not just "the stock moved" but "the underlying financial profile is statistically unusual in ways that matter."

Three independent evidence streams are combined into a single conviction score:

| Pillar | Data | Method |
|---|---|---|
| Statistical anomaly | Financial ratio time series | MCD Mahalanobis distance |
| Earnings quality | Raw balance sheet fundamentals | Beneish M-Score (1999) |
| Narrative transparency | Management MD&A language | LLM (Claude Sonnet 4.6) |

When all three pillars fire simultaneously, the probability of a false positive drops dramatically.

---

## Universe

- **94 QQQ companies** — Invesco QQQ ETF constituents (largest non-financial Nasdaq companies)
- **1,352 quarterly filings** (10-Q) scored across multiple years
- Source: SEC EDGAR via `qqq-anomaly-lab-repo` → GCS + BigQuery

---

## Pipeline architecture

```
qqq-anomaly-lab-repo  (upstream)
  └── SEC EDGAR ingestion → feature extraction → GCS + BigQuery (qqq_anomaly.filings)
          │
          ▼
scoring-pipeline  (this repo)
  Step 1  flatten_bq.py                     BQ → period_features.json
  Step 2  score_quarterly_anomalies.py       Anomaly scoring → BQ quarterly_scores_detailed
  Step 3  generate_explanations.py           LLM analyst briefs → BQ anomaly_explanations    ┐ parallel
  Step 4  score_narrative_divergence.py      MD&A divergence → BQ narrative_divergence        ┘
  Step 5  compute_conviction.py              Three-pillar synthesis → BQ conviction_scores
  Step 6  build_master_output.py             BQ view + review pack → BQ filing_intelligence   ┐ parallel
  Step 7  build_trend_table.py               Time-series table → BQ company_trend             ┘
          │
          ▼
redink-ui          ← queries company_trend, filing_intelligence, top_anomaly_review_pack
qqq-eval-suite     ← queries top_anomaly_review_pack
```

---

## Quickstart

```bash
# Install dependencies
pip install -e .

# Full pipeline run
python scripts/orchestrate.py

# Resume from a specific step (e.g. after a failure)
python scripts/orchestrate.py --from-step 3

# Run specific steps only (BQ deps must already exist)
python scripts/orchestrate.py --steps 5,6,7

# Preview execution plan without running anything
python scripts/orchestrate.py --dry-run
```

Steps 3+4 run in parallel, as do steps 6+7 — the orchestrator handles this automatically via a DAG dependency graph.

---

## Step-by-step detail

### Step 1 — Flatten BQ → `period_features.json`
Reads all 10-Q filings from `qqq_anomaly.filings` in BigQuery. Writes a flat feature table to `output/period_features.json` and `output/feature_keys.json` for the scoring step.

### Step 2 — Anomaly scoring → `quarterly_scores_detailed`
Core quantitative scoring. Seven sub-steps:

1. **Winsorize** — clip each feature at 5th/95th percentile to suppress outliers
2. **Self-history z-scores** — how unusual is this quarter vs this company's own history (median/IQR, not mean/std)
3. **Peer z-scores** — how unusual vs sector peers in the same calendar quarter
4. **Combine** — simple average of the two z-score sets, clipped to ±8
5. **Mahalanobis distance (MCD)** — single scalar capturing multivariate unusualness, accounting for feature correlations
6. **Percentile-scale to 0–100** — anomaly score: 90 = more unusual than 90% of all filings
7. **Beneish M-Score** — accounting manipulation model (M > −2.22 = likely manipulator)

### Step 3 — LLM analyst briefs → `anomaly_explanations`
For filings with `alert_score ≥ 5`, Claude classifies the anomaly pattern and generates a three-paragraph analyst brief. One of 7 named patterns: Earnings Quality Risk, Growth Bubble, Financial Distress, Aggressive Asset Expansion, Recovery, M&A Distortion, Idiosyncratic.

### Step 4 — Narrative divergence → `narrative_divergence`
For qualifying filings, Claude receives the full MD&A text and asks: does management's language match what the numbers show? Returns `CONTRADICTS` / `CORROBORATES` / `NEUTRAL` plus a verbatim cited passage and rationale. The full MD&A is passed directly — no chunking, no RAG.

`CONTRADICTS` is the highest-value signal: bad numbers, upbeat management.

### Step 5 — Conviction score → `conviction_scores`
Synthesises all three pillars into a single `conviction_score` (0–100) and tier:

- **ALERT** (≥ 65): All three pillars firing. 32 filings.
- **FLAG** (40–64): Two pillars contributing meaningfully. 274 filings.
- **WATCH** (20–39): One meaningful signal. Monitor. 731 filings.

### Step 6 — Master output → `filing_intelligence` + `top_anomaly_review_pack`
Creates a live BQ view (`filing_intelligence`) joining all four output tables — 50 columns, 1,352 rows. Also materialises `top_anomaly_review_pack`: ALERT + FLAG + WATCH filings sorted by conviction score (1,037 rows). Used by `redink-ui` and `qqq-eval-suite`.

### Step 7 — Trend table → `company_trend`
One row per (ticker, calendar_quarter) with all metrics pre-joined. The UI queries a single ticker and gets a flat array ready to render in a chart library — no pivoting, no joins, no computation on the client side. Clustered by ticker for fast lookups.

---

## BigQuery output tables

| Table | Rows | Description |
|---|---|---|
| `quarterly_scores_detailed` | 1,352 | Full quantitative scores, z-scores, Beneish |
| `anomaly_explanations` | 163 | LLM analyst briefs (pattern + 3-paragraph brief) |
| `narrative_divergence` | 161 | MD&A vs. numbers: label + cited passage + rationale |
| `conviction_scores` | 1,352 | Three-pillar conviction score + tier |
| `filing_intelligence` | 1,352 | BQ view joining all four tables (50 cols) |
| `top_anomaly_review_pack` | 1,037 | Materialised ALERT+FLAG+WATCH filings |
| `company_trend` | 1,352 | Per-ticker time-series for UI chart rendering |

All tables in GCP project `qqq-anomaly-lab`, dataset `qqq_finance`.

---

## Repo structure

```
scoring-pipeline/
├── scripts/
│   ├── orchestrate.py               ← PRIMARY ENTRY POINT — DAG orchestrator
│   ├── run_pipeline.py              ← sequential runner (reference only)
│   ├── flatten_bq.py                ← Step 1
│   ├── score_quarterly_anomalies.py ← Step 2
│   ├── build_master_output.py       ← Step 6
│   └── build_trend_table.py         ← Step 7
├── explanations/
│   ├── generate_explanations.py     ← Step 3
│   ├── score_narrative_divergence.py← Step 4
│   ├── compute_conviction.py        ← Step 5
│   ├── prompt_template.py           ← prompt builder for analyst briefs
│   └── divergence_prompt.py         ← prompt builder for divergence analysis
├── src/
│   └── qqq_scoring/
│       ├── flatten.py               ← BQ reading + flattening logic
│       ├── features.py              ← feature selection + winsorizing
│       ├── scorer.py                ← z-score computation + Mahalanobis
│       ├── beneish.py               ← Beneish M-Score computation
│       ├── reference.py             ← GICS sector mapping loader
│       └── upload.py                ← GCS/BQ upload utilities
├── docs/
│   └── task_definition.md           ← full pipeline documentation
├── output/                          ← generated outputs (gitignored)
└── pyproject.toml
```

---

## Dependencies

```
google-cloud-storage
google-cloud-bigquery
anthropic
pandas
numpy
scikit-learn
pyarrow
```

Install with `pip install -e .`

Requires GCP credentials with read access to `qqq-anomaly-lab` (BigQuery + GCS). Authenticate with `gcloud auth application-default login`.

---

## Related repositories

| Repo | Role |
|---|---|
| `qqq-anomaly-lab-repo` | Data layer — SEC EDGAR ingestion, feature extraction, GCS/BQ upload |
| `scoring-pipeline` | This repo — scoring, explanation, and intelligence layer |
| `qqq-eval-suite` | Eval layer — quality gates, explanation quality testing |
| `redink-ui` | UI layer — serves scores and intelligence to analysts |
