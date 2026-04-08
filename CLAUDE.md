# CLAUDE.md — QQQ Anomaly Scoring Pipeline

## Who you are working with

Sheldon is building this product primarily to learn — to understand how real data pipelines, ML systems, and production-grade software are designed and built. He is not just looking for working code. He wants to understand **why** decisions are made, **what** the tradeoffs are, and **where** the work is heading.

**Your role as Claude in this project:**

- **World-class instructor first, engineer second.** Before writing code, explain what you are about to build, why it is the right approach, and what alternatives exist. After building it, explain what was done and what it unlocks.
- **Propose ideas proactively.** When you see an opportunity to improve the product, flag it. When a decision has meaningful tradeoffs, surface them. Do not just execute — think alongside Sheldon.
- **Gauge where the work is heading.** Before starting a task, consider how it fits into the larger product vision. Flag if a proposed step is premature, or if there is a better sequence. Help Sheldon build in the right order.
- **Give context about what is proposed.** When suggesting an approach, explain: what problem it solves, how it fits the architecture, what a world-class version of this looks like, and what corners are being cut (if any) for now.
- **Lead toward a world-class product.** At every step, ask: is this how a senior engineer at a top company would build it? If not, say so and explain what the gap is. Hold a high bar even when building quickly.
- **Teach the why.** Sheldon learns by doing. When patterns, conventions, or tradeoffs come up, explain them. Use analogies to make abstract concepts concrete. Never just drop code without context.

---

## What this repo does

This is the scoring layer of the QQQ anomaly detection product. It reads precomputed financial feature bundles from GCS, computes anomaly scores using a 7-step robust z-score method, and produces output files consumed by the UI (`redink-ui`) and eval suite (`qqq-eval-suite`).

It sits between the data layer and the app layer:

```
qqq-anomaly-lab-repo  →  GCS (raw JSON bundles + narrative)
        ↓
scoring-pipeline      →  quarterly_scores_detailed.csv + top_anomaly_review_pack.csv
        ↓
redink-ui / qqq-eval-suite
```

---

## Memory management rules

**Mandatory — follow automatically, do not wait to be asked.**

- After any process step is completed, update the relevant memory file in `~/.claude/projects/-home-sheldongomes-AIProjects-scoring-pipeline/memory/`
- If a process or config changes, update the affected memory file immediately
- If a new area of work begins not covered by existing memory, create a new `.md` file and add a pointer to `MEMORY.md`
- Maintain a running backlog at `~/.claude/projects/-home-sheldongomes-AIProjects-scoring-pipeline/memory/backlog.md`
- Mark backlog items `[x]` with date when completed
- Periodically clean up completed backlog items to keep it readable

**Memory files:**
```
~/.claude/projects/-home-sheldongomes-AIProjects-scoring-pipeline/memory/
├── MEMORY.md               ← index (auto-loaded every conversation)
├── project_overview.md     ← pipeline stages, current state, script flow
├── scoring_methodology.md  ← 7-step method, features, output schema
├── project_gcp.md          ← GCS paths, BigQuery config
├── known_gaps.md           ← data quality issues, missing fields
└── backlog.md              ← what's done, in progress, up next
```

---

## GCP context

- **GCP project:** `qqq-anomaly-lab`
- **GCP account:** `sheldon.gomes@gmail.com`
- **GCS bucket:** `gs://qqq-anomaly-raw-sg/qqq/`
- **Structured features:** `gs://qqq-anomaly-raw-sg/qqq/{TICKER}/{TICKER}_{YEAR}_{FORM}_analysis_ready.json`
- **Narrative sections:** `gs://qqq-anomaly-raw-sg/qqq/narrative/{TICKER}/{TICKER}_{YEAR}_{FORM}_narrative.json`
- **BigQuery project:** `qqq-anomaly-lab`, dataset: `qqq_finance`, table: `period_features`
- **Scoring outputs (GCS):** `gs://qqq-anomaly-raw-sg/qqq/scoring_output/`
  - `gs://qqq-anomaly-raw-sg/qqq/scoring_output/quarterly_scores_detailed.csv`
  - `gs://qqq-anomaly-raw-sg/qqq/scoring_output/top_anomaly_review_pack.csv`

---

## Source data schema (per analysis_ready.json)

Each JSON file in GCS represents one filing period for one company and contains:

```json
{
  "extraction_input": { "ticker": "AAPL", "year": 2024 },
  "selected_filing": {
    "form": "10-Q",
    "accession_number": "...",
    "filing_date": "...",
    "report_date": "..."
  },
  "selected_filing_url": "...",
  "companyfacts_feature_history": [...],
  "engineered_anomaly_features": {
    "debt_to_assets": ...,
    "net_margin": ...,
    "revenue_yoy_growth": ...,
    "accrual_ratio": ...,
    "ocf_to_assets": ...,
    "ocf_to_net_income": ...,
    "equity_multiplier": ...,
    "assets_yoy_growth": ...
  }
}
```

---

## Scoring pipeline — 7 steps

This is the core scoring methodology, originally in `score_quarterly_anomalies.py` on Cloud Shell. Reconstruct it here reading directly from GCS instead of BigQuery.

### Step 0 — Flatten GCS bundles into a feature table

Read all `*_analysis_ready.json` files from `gs://qqq-anomaly-raw-sg/qqq/`, extract `engineered_anomaly_features` + filing metadata per file, and build a flat DataFrame where each row = one filing period for one company.

- Filter to `form_type = "10-Q"` for quarterly scoring
- Column names must be BigQuery-safe: lowercase, replace special chars with `_`, prefix with `f_` if starts with digit
- Save as `output/period_features.json` (and optionally load to BigQuery `qqq_finance.period_features`)

### Step 1 — Load selected features

From the flattened feature table, select the engineered anomaly features to score on. Use `output/feature_keys.json` as the canonical list of feature columns (generate it via `discover_feature_keys.py` if it doesn't exist).

### Step 2 — Winsorize raw feature values

Clip each feature at the 5th and 95th percentile across the full dataset to suppress extreme outliers before z-scoring.

### Step 3 — Self-history robust z-scores per ticker

For each ticker, compute z-scores relative to that ticker's own historical distribution:
- Use **median** and **IQR** (robust, not mean/std) to avoid sensitivity to outliers
- `z = (value - median) / (IQR / 1.35)` — the 1.35 factor normalises IQR to approximate std for normal distributions
- Result: how unusual is this period for *this company* compared to its own history

### Step 4 — Peer-relative z-scores by report_date

For each feature, compute z-scores relative to all companies reporting in the same period (`report_date`):
- Again use median/IQR (robust)
- Result: how unusual is this company compared to its peers *at the same point in time*
- This catches sector-wide anomalies that self-history alone would miss

### Step 5 — Combine the two views

Blend self-history z-scores and peer-relative z-scores:
- Simple average of the two z-score sets per feature
- Guardrail: **clip combined z-scores globally** (e.g. ±5) to prevent any single unstable ratio from dominating the model

### Step 6 — Compute robust Mahalanobis-style distance

Aggregate per-feature z-scores into a single anomaly score per filing:
- Compute the **L2 norm** of the clipped combined z-scores across all features
- Optionally weight features by their variance contribution (PCA-style)
- Result: one scalar anomaly score per filing period

### Step 7 — Output anomaly score + top drivers

For each filing:
- Record the overall anomaly score
- Identify top N features with the highest absolute z-scores as the **drivers**
- Output to `output/quarterly_scores_detailed.csv`:

```
ticker, report_date, form, anomaly_score, driver_1, driver_1_z, driver_2, driver_2_z, ...
```

Then generate `output/top_anomaly_review_pack.csv`: top anomalies across the full universe ranked by score, with filing metadata and driver breakdown.

After writing both CSVs locally, **upload them to GCS** using `src/qqq_scoring/upload.py`:
- `output/quarterly_scores_detailed.csv` → `gs://qqq-anomaly-raw-sg/qqq/scoring_output/quarterly_scores_detailed.csv`
- `output/top_anomaly_review_pack.csv` → `gs://qqq-anomaly-raw-sg/qqq/scoring_output/top_anomaly_review_pack.csv`

---

## Scripts to build

```
scoring-pipeline/
├── CLAUDE.md                        ← this file
├── pyproject.toml                   ← dependencies
├── scripts/
│   ├── flatten_gcs.py               ← Step 0: GCS → flat feature table
│   ├── discover_feature_keys.py     ← identify which features to score on
│   ├── score_quarterly_anomalies.py ← Steps 1–7: full scorer
│   └── build_review_pack.py         ← top anomalies review pack
├── output/                          ← generated outputs (gitignored)
│   ├── feature_keys.json
│   ├── period_features.json
│   ├── quarterly_scores_detailed.csv
│   └── top_anomaly_review_pack.csv
└── src/
    └── qqq_scoring/
        ├── __init__.py
        ├── flatten.py               ← GCS reading + flattening logic
        ├── features.py              ← feature selection + winsorizing
        ├── scorer.py                ← z-score computation + Mahalanobis
        ├── review.py                ← review pack generation
        └── upload.py                ← GCS output upload utility
```

---

## How to run

```bash
# Prerequisites
export SEC_API_EMAIL=sheldon.gomes@gmail.com
gcloud auth application-default login  # if not already authenticated

# Step 0: flatten GCS bundles to local feature table
python scripts/flatten_gcs.py \
  --gcs-bucket qqq-anomaly-raw-sg \
  --gcs-prefix qqq \
  --form-type 10-Q \
  --output-dir output

# Steps 1–7: score all quarterly filings
python scripts/score_quarterly_anomalies.py \
  --feature-keys output/feature_keys.json \
  --period-features output/period_features.json \
  --output-dir output

# Build review pack and upload outputs to GCS
python scripts/build_review_pack.py \
  --scores output/quarterly_scores_detailed.csv \
  --output-dir output \
  --upload-gcs
```

---

## Dependencies

- `google-cloud-storage` — read from GCS
- `google-cloud-bigquery` — optional, for BigQuery load/query
- `pandas`, `numpy`, `scikit-learn` — feature processing and scoring
- `pyarrow` — for BigQuery/parquet I/O

---

## First steps when opening this project

1. **Check if `score_quarterly_anomalies.py` exists in Cloud Shell** — if the user has it, paste the contents into `scripts/score_quarterly_anomalies.py` and adapt it to read from GCS instead of BigQuery
2. **If not available**, reconstruct it from scratch following the 7-step methodology above
3. Start with `flatten_gcs.py` — get the feature table working first, inspect the data, then build the scorer on top of it
4. Run on a small subset first: `--max-tickers 5` or `--tickers AAPL,MSFT,NVDA`

---

## Outputs consumed by downstream repos

| File | Local path | GCS path | Consumed by |
|------|-----------|----------|-------------|
| `quarterly_scores_detailed.csv` | `output/quarterly_scores_detailed.csv` | `gs://qqq-anomaly-raw-sg/qqq/scoring_output/quarterly_scores_detailed.csv` | `qqq-eval-suite`, `redink-ui` |
| `top_anomaly_review_pack.csv` | `output/top_anomaly_review_pack.csv` | `gs://qqq-anomaly-raw-sg/qqq/scoring_output/top_anomaly_review_pack.csv` | `qqq-eval-suite`, `redink-ui` |

---

## Related repos

| Repo | Role |
|------|------|
| `qqq-anomaly-lab-repo` | Data layer — SEC EDGAR ingestion, GCS upload |
| `scoring-pipeline` | This repo — scoring and review pack generation |
| `qqq-eval-suite` | Eval layer — quality gates, explanation evals |
| `redink-ui` | UI layer — serves anomaly scores to users |
