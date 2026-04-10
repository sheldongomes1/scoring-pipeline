# Implementation Plan: LLM-Generated Anomaly Explanation Layer

## Context for the Agent

RedInk is an anomaly detection tool for QQQ 10Q filings. The BigQuery table is `qqq-anomaly-lab.qqq_finance.period_features`. The dataset covers ~30 QQQ companies over a rolling 5-year window. Anomaly scores include: z-score IQR per metric, sector z-scores, self-history z-scores, Robust Mahalanobis Distance, and Beneish M-Score. The pipeline also performs narrative divergence detection using MD&A sections from 10Q filings.

The goal is to build a module that takes a (ticker, quarter) anomaly profile — the raw scores — and generates a human-readable, analyst-grade explanation of what's anomalous and why it matters. This converts a row of z-scores into a testable investment hypothesis.

---

## Step 1: Inspect the Schema and Identify All Score Columns

**Task:** Write a script `explanations/inspect_schema.py` that:

1. Connects to BigQuery and runs `SELECT * FROM qqq-anomaly-lab.qqq_finance.period_features LIMIT 5`.
2. Prints every column name, data type, and a sample value.
3. Groups columns into categories and prints the grouping:
   - **Identifier columns:** ticker, period_end_date, fiscal_quarter, sector, etc.
   - **Raw financial metrics:** revenue, net_income, SGA, operating_margin, etc.
   - **Z-score IQR columns:** any column containing "z" or "iqr" or "zscore".
   - **Sector z-score columns:** any column containing "sector" and "z".
   - **Self-history z-score columns:** any column containing "self" or "history" and "z".
   - **Mahalanobis Distance:** column(s) containing "mahal".
   - **Beneish M-Score:** column(s) containing "beneish" or "mscore", plus component variables (DSRI, GMI, AQI, SGI, DEPI, SGAI, TATA, LVGI).
   - **Narrative divergence columns:** any column related to MD&A, sentiment, divergence, or narrative scoring.
   - **Unknown / uncategorized:** anything that doesn't fit the above.
4. Saves this mapping as `explanations/column_map.json` with the structure:
   ```json
   {
     "identifiers": ["ticker", "period_end_date", ...],
     "raw_metrics": ["revenue", "net_income", ...],
     "z_iqr": ["revenue_z_iqr", ...],
     "z_sector": ["revenue_sector_z", ...],
     "z_self_history": ["revenue_self_z", ...],
     "mahalanobis": ["robust_mahal_distance", ...],
     "beneish": ["beneish_m_score", "dsri", ...],
     "narrative": ["mda_divergence_score", ...],
     "uncategorized": [...]
   }
   ```

**This step is critical.** Every subsequent step depends on this column map. Do not proceed until this is complete and reviewed.

---

## Step 2: Build the Anomaly Profile Extractor

**Task:** Write a module `explanations/profile_extractor.py` that:

1. Loads `column_map.json`.
2. Exposes a function `get_anomaly_profile(ticker: str, quarter: str) -> dict` that:
   - Queries BigQuery for the given (ticker, quarter).
   - Returns a structured dict:
     ```python
     {
       "ticker": "FANG",
       "quarter": "2023-Q2",
       "sector": "Energy",
       "top_z_flags": [
         {"metric": "sga_pct_revenue", "z_iqr": 2.8, "sector_z": 1.9, "self_z": 3.1, "raw_value": 0.18, "direction": "high"},
         ...
       ],  # Sorted by max(abs(z_iqr), abs(sector_z), abs(self_z)), top 5 only
       "mahalanobis": {"score": 14.2, "percentile": 97},
       "beneish": {
         "m_score": -1.42,
         "flagged": true,
         "top_drivers": [
           {"variable": "DSRI", "value": 1.34, "interpretation": "Days sales receivable increasing faster than revenue"},
           {"variable": "SGAI", "value": 0.72, "interpretation": "SGA declining relative to revenue"}
         ]
       },
       "narrative_divergence": {
         "score": 0.78,  # or whatever the column contains
         "flagged": true  # if score exceeds a threshold
       },
       "context": {
         "prior_quarter_alert_score": 3,  # alert score from the previous quarter for trend context
         "current_alert_score": 9
       }
     }
     ```
3. Also exposes `get_peer_context(ticker: str, quarter: str) -> dict` that:
   - Queries the same quarter for all tickers in the same sector.
   - Returns the sector median for each of the top flagged metrics.
   - Example: if FANG's `sga_pct_revenue` z_iqr is 2.8, what's the sector median raw value and z_iqr for that metric in the same quarter?

**Beneish component interpretations** — hardcode these as a lookup dict:
- DSRI > 1.0: "Days sales receivable growing faster than revenue — possible aggressive revenue recognition"
- GMI > 1.0: "Gross margin deteriorating — cost pressures or pricing erosion"
- AQI > 1.0: "Asset quality declining — potential capitalization of expenses"
- SGI > 1.0: "Sales growth index elevated — unsustainable growth or channel stuffing risk"
- DEPI > 1.0: "Depreciation rate slowing — extending asset lives to inflate earnings"
- SGAI < 1.0: "SGA declining relative to revenue — potential underinvestment"
- TATA > 0: "Total accruals to total assets positive — earnings quality concern"
- LVGI > 1.0: "Leverage increasing — growing debt burden"

---

## Step 3: Build the Prompt Template

**Task:** Create a file `explanations/prompt_template.py` that contains a function `build_explanation_prompt(profile: dict, peer_context: dict) -> str` which constructs a prompt for Claude.

The prompt must follow this structure:

```
You are a senior equity research analyst writing an internal anomaly brief for your portfolio manager. You are direct, specific, and avoid filler language. Every sentence must either state a fact or a testable hypothesis.

## Company
{ticker} — {sector} — {quarter}

## Anomaly Data

### Flagged Metrics (sorted by severity)
{For each entry in top_z_flags:}
- {metric_name}: Raw value = {raw_value}. Z-score (IQR): {z_iqr}. Sector Z: {sector_z}. Self-history Z: {self_z}. Direction: {direction}.
  Sector median for this metric this quarter: {peer_context value}.

### Mahalanobis Distance
Score: {score}. Percentile: {percentile}th across full dataset.

### Beneish M-Score
M-Score: {m_score}. Flagged: {yes/no}.
Top contributing variables:
{For each driver:}
- {variable}: {value} — {interpretation}

### Narrative Divergence
Score: {score}. Flagged: {yes/no}.

### Trend Context
Prior quarter alert score: {prior_quarter_alert_score}. Current: {current_alert_score}. Trend: {increasing/decreasing/stable}.

## Your Task

Write a 3-paragraph anomaly brief:

**Paragraph 1 — What's happening:** State the 2-3 most significant anomalies in plain English. Be specific about which metrics are anomalous, by how much, and in which direction. Reference the sector comparison.

**Paragraph 2 — Why it matters:** Generate 1-2 testable hypotheses about what could be driving these anomalies. Connect the quantitative flags to plausible business explanations. If the Beneish M-Score is flagged, state which component is driving it and what that implies about earnings quality. If narrative divergence is flagged, note that management's language is not aligned with the numbers.

**Paragraph 3 — What to do next:** Recommend 2-3 specific actions the analyst should take — e.g., "Compare receivables growth to revenue growth in the 10Q footnotes," or "Check the earnings call transcript for management commentary on SGA changes," or "Monitor whether this metric normalizes next quarter or accelerates."

Do not use bullet points. Do not hedge with phrases like "it's worth noting" or "it may be worth exploring." Be direct.
```

**Do not modify this prompt structure without explicit instruction.** The specificity of the output format is intentional.

---

## Step 4: Build the Explanation Generator

**Task:** Write a script `explanations/generate_explanations.py` that:

1. Accepts command-line arguments: `--ticker FANG --quarter 2023-Q2` for single-company mode, or `--all --min-alert-score 5` for batch mode.
2. In single-company mode:
   - Calls `get_anomaly_profile(ticker, quarter)`.
   - Calls `get_peer_context(ticker, quarter)`.
   - Calls `build_explanation_prompt(profile, peer_context)`.
   - Sends the prompt to the Anthropic API using `claude-sonnet-4-20250514` (model string: `claude-sonnet-4-20250514`). Max tokens: 1024. Temperature: 0.3.
   - Prints the explanation to stdout.
   - Saves to `explanations/output/{ticker}_{quarter}.md`.
3. In batch mode:
   - Queries BigQuery for all (ticker, quarter) pairs.
   - Computes alert scores using the same logic from the backtest plan (num_z_flags * 1 + mahal_flag * 3 + beneish_flag * 5).
   - Filters to rows where alert_score >= the specified minimum.
   - Generates explanations for each, with a 1-second delay between API calls.
   - Saves each to `explanations/output/{ticker}_{quarter}.md`.
   - Saves an index file `explanations/output/index.csv` with columns: ticker, quarter, alert_score, output_file.
4. Uses the Anthropic Python SDK:
   ```python
   import anthropic
   client = anthropic.Anthropic()  # uses ANTHROPIC_API_KEY env var
   message = client.messages.create(
       model="claude-sonnet-4-20250514",
       max_tokens=1024,
       temperature=0.3,
       messages=[{"role": "user", "content": prompt}]
   )
   explanation = message.content[0].text
   ```

**Error handling:** If the API call fails, log the error, skip that (ticker, quarter), and continue. Do not crash the batch.

---

## Step 5: Build a Quality Check

**Task:** Write a script `explanations/quality_check.py` that:

1. Reads all generated explanations from `explanations/output/`.
2. For each explanation, checks:
   - **Length:** Is it between 150 and 500 words? Flag if outside this range.
   - **Specificity:** Does it mention at least 2 specific metric names from the anomaly profile? (String match against the metric names in the profile.) Flag if fewer than 2.
   - **Hypothesis presence:** Does paragraph 2 contain language suggesting a hypothesis? (Check for phrases like "suggests," "could indicate," "driven by," "consistent with.") Flag if absent.
   - **Action items:** Does paragraph 3 contain at least 2 concrete actions? (Check for imperative verbs: "compare," "check," "monitor," "review," "examine.") Flag if fewer than 2.
   - **Filler detection:** Flag if the explanation contains any of: "it's worth noting," "it may be worth," "interestingly," "notably," "it should be noted." These indicate the prompt constraints leaked.
3. Outputs a quality report `explanations/quality_report.csv` with columns: ticker, quarter, word_count, metrics_mentioned, has_hypothesis, action_count, filler_detected, pass/fail.
4. Prints summary: "X/Y explanations passed all checks. Z had filler language. W were too short."

---

## Step 6: Build a Demo-Ready Output

**Task:** Write a script `explanations/build_demo.py` that:

1. Reads `explanations/output/index.csv`.
2. Selects the top 5 (ticker, quarter) pairs by alert_score.
3. For each, reads the generated explanation from `explanations/output/{ticker}_{quarter}.md`.
4. Produces a single combined markdown file `explanations/demo_explanations.md` with:
   - A title: "RedInk Anomaly Briefs — Top Flagged Filings"
   - For each case:
     - `## {ticker} — {quarter}` as a header.
     - `**Alert Score: {score}** | Mahalanobis: {percentile}th percentile | Beneish: {flagged/clean}`
     - The full generated explanation.
     - A horizontal rule separator.
5. Also produces `explanations/demo_explanations.html` — a clean, styled HTML version using inline CSS that looks presentable in a browser. Use a monochrome palette. No external dependencies.

---

## File Structure When Complete

```
explanations/
├── inspect_schema.py
├── column_map.json
├── profile_extractor.py
├── prompt_template.py
├── generate_explanations.py
├── quality_check.py
├── build_demo.py
├── quality_report.csv
├── demo_explanations.md
├── demo_explanations.html
└── output/
    ├── index.csv
    ├── EA_2023-Q2.md
    ├── FANG_2023-Q3.md
    └── ... (one per flagged ticker-quarter)
```

---

## Dependencies

```
pip install anthropic google-cloud-bigquery pandas
```

Ensure `ANTHROPIC_API_KEY` is set as an environment variable. Ensure BigQuery credentials are configured.

---

## Notes for the Agent

- **Schema first.** Step 1 must complete before anything else. Every downstream script depends on `column_map.json`.
- **Do not modify the prompt template structure** unless the output quality check reveals a systematic problem. The prompt is designed to produce direct, filler-free output matching Sheldon's communication preferences.
- **Temperature 0.3 is intentional.** These explanations should be factual and grounded in the data, not creative. If output is too repetitive across companies, increase to 0.5 — but no higher.
- **Beneish component interpretations are hardcoded.** Do not ask the LLM to interpret what DSRI or GMI mean — use the lookup dict in Step 2.
- **The quality check is not optional.** Run it after every batch generation. If pass rate is below 80%, inspect the failing explanations and adjust the prompt before regenerating.
- **API cost estimate:** ~30 tickers × ~20 quarters = ~600 possible rows. At min-alert-score 5, expect ~50-100 to qualify. At ~500 tokens per explanation, that's ~50K output tokens — roughly $0.75 on Sonnet. Batch mode is cheap.
- **Print progress throughout.** "Generating explanation for FANG 2023-Q2... (14/47 complete)"
