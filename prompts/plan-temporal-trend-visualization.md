# Implementation Plan: Temporal Trend Visualization

## Context for the Agent

RedInk is an anomaly detection tool for QQQ 10Q filings. The BigQuery table is `qqq-anomaly-lab.qqq_finance.period_features`. The dataset covers ~30 QQQ companies over a rolling 5-year window. Anomaly scores include: z-score IQR per metric, sector z-scores, self-history z-scores, Robust Mahalanobis Distance, and Beneish M-Score.

The goal is to build a visualization layer that shows how a company's anomaly profile evolves across quarters. A single-quarter spike is noise. A 3-quarter escalating trend is a pattern. Without temporal visualization, the analyst can't distinguish the two. This module must work as a standalone tool and also integrate as a component in the broader RedInk UI.

---

## Step 1: Inspect Schema and Build the Data Layer

**Task:** Write a script `trends/build_trend_data.py` that:

1. **First, inspect the BigQuery schema.** Run `bq show --schema qqq-anomaly-lab.qqq_finance.period_features` and print every column name and type. Save the schema to `trends/schema.json`.
2. Query BigQuery for ALL rows — every (ticker, quarter) pair — pulling:
   - Identifier columns: ticker, period_end_date (or fiscal_quarter), sector.
   - All z-score columns (IQR, sector, self-history).
   - Mahalanobis Distance.
   - Beneish M-Score and its 8 components (DSRI, GMI, AQI, SGI, DEPI, SGAI, TATA, LVGI) if available.
   - Narrative divergence score if available.
3. Compute the composite `alert_score` for each row using the standard formula:
   - `num_z_flags`: count of z-score columns where |value| > 2.0.
   - `mahal_flag`: 1 if Mahalanobis Distance > 90th percentile of full dataset.
   - `beneish_flag`: 1 if Beneish M-Score > -1.78.
   - `alert_score = num_z_flags * 1 + mahal_flag * 3 + beneish_flag * 5`.
4. Sort by (ticker, period_end_date) ascending.
5. Save to `trends/trend_data.csv`.
6. Print: total rows, unique tickers, quarter range, and the distribution of alert_score (mean, median, max, 90th percentile).

---

## Step 2: Build the Single-Company Timeline Chart

**Task:** Write a script `trends/single_company_timeline.py` that:

1. Accepts a command-line argument: `--ticker FANG`.
2. Reads `trends/trend_data.csv`, filters to the given ticker.
3. Generates a **multi-panel timeline chart** using `matplotlib` with the following layout (4 rows, 1 column, shared x-axis):

### Panel 1: Composite Alert Score Over Time
- X-axis: quarter (formatted as "Q1 '21", "Q2 '21", etc.).
- Y-axis: `alert_score`.
- Bar chart, colored by alert level:
  - GREEN for alert_score 0.
  - YELLOW for 1–3.
  - ORANGE for 4–7.
  - RED for 8+.
- Horizontal dashed line at alert_score = 5 (the "attention threshold").

### Panel 2: Top 3 Z-Score Metrics Over Time
- For this ticker, identify the 3 z-score columns that have the highest max absolute value across all quarters.
- Plot these 3 metrics as lines over time.
- Add horizontal dashed lines at +2.0 and -2.0 (the flag threshold).
- Legend with metric names.

### Panel 3: Mahalanobis Distance Over Time
- Line chart of Mahalanobis Distance across quarters.
- Horizontal dashed line at the 90th percentile threshold.
- Shade the area above the threshold in light red.

### Panel 4: Beneish M-Score Over Time
- Line chart of Beneish M-Score across quarters.
- Horizontal dashed line at -1.78 (manipulation threshold).
- Shade the area above -1.78 in light red.

**Formatting:**
- Chart title: "{TICKER} — RedInk Anomaly Timeline".
- Figure size: 14 x 16 inches.
- Use `plt.style.use('seaborn-v0_8-whitegrid')` or similar clean style.
- X-axis labels at 45° angle if more than 10 quarters.
- Save as `trends/charts/{ticker}_timeline.png` at 150 DPI.

4. Also save the chart as `trends/charts/{ticker}_timeline.pdf` for print-quality.

---

## Step 3: Build the Multi-Company Heatmap

**Task:** Write a script `trends/multi_company_heatmap.py` that:

1. Reads `trends/trend_data.csv`.
2. Generates a **heatmap** where:
   - Y-axis: tickers (sorted by max alert_score descending — highest-risk companies at top).
   - X-axis: quarters (chronological left to right).
   - Cell color: alert_score, using a diverging colormap (white → yellow → orange → red).
   - Cell annotation: the alert_score value, displayed as text inside each cell.
3. Figure size: 18 x 12 inches (adjustable based on ticker and quarter count).
4. Title: "RedInk — Anomaly Heatmap Across QQQ Universe".
5. Save as `trends/charts/universe_heatmap.png` at 150 DPI.

**This chart is the "command center" view** — at a glance, the analyst can see which companies are currently hot, which are trending hotter, and which are clean.

---

## Step 4: Build the Trend Detection Module

**Task:** Write a module `trends/trend_detector.py` that:

1. Reads `trends/trend_data.csv`.
2. For each ticker, computes:
   - **Streak detection:** How many consecutive quarters has the alert_score been >= 4? Store as `consecutive_elevated_quarters`.
   - **Acceleration detection:** Is the alert_score increasing quarter-over-quarter for 3+ quarters? Compute the slope of alert_score over the last 4 quarters using linear regression. Store as `alert_trend_slope`.
   - **New flag detection:** Are there metrics that crossed the |2.0| z-score threshold this quarter that were *not* flagged last quarter? Store as `new_flags` (list of metric names).
   - **Resolved flag detection:** Are there metrics that were flagged last quarter but are now below |2.0|? Store as `resolved_flags` (list of metric names).
3. Classifies each (ticker, latest_quarter) into a trend category:
   - `ESCALATING`: alert_trend_slope > 0.5 AND consecutive_elevated_quarters >= 2.
   - `PERSISTENT`: consecutive_elevated_quarters >= 3 AND alert_trend_slope between -0.5 and 0.5.
   - `NEW_SPIKE`: alert_score >= 5 this quarter AND alert_score < 3 last quarter.
   - `IMPROVING`: alert_trend_slope < -0.5 AND prior quarter alert_score >= 5.
   - `STABLE`: all other cases.
4. Outputs `trends/trend_classifications.csv` with columns: ticker, latest_quarter, alert_score, consecutive_elevated_quarters, alert_trend_slope, trend_category, new_flags, resolved_flags.
5. Prints a summary grouped by trend_category: "ESCALATING: FANG, INTC. PERSISTENT: META. NEW_SPIKE: STX..."

---

## Step 5: Build the Trend Summary Dashboard

**Task:** Write a script `trends/build_dashboard.py` that:

1. Reads `trends/trend_classifications.csv`.
2. Generates a **single-page dashboard** as an HTML file `trends/dashboard.html` using inline CSS (no external dependencies). The dashboard contains:

### Section 1: Priority Alerts (top of page)
- Cards for every ticker classified as ESCALATING or NEW_SPIKE.
- Each card shows: ticker, alert_score, trend_category, consecutive_elevated_quarters, and the new_flags list.
- ESCALATING cards have a red left border. NEW_SPIKE cards have an orange left border.

### Section 2: Watchlist (middle of page)
- Table of all PERSISTENT and IMPROVING tickers.
- Columns: ticker, alert_score, trend_category, slope, consecutive quarters.
- Sortable by alert_score (just render in sorted order — no JS needed).

### Section 3: Universe Overview (bottom of page)
- Embed the heatmap image (reference `charts/universe_heatmap.png`).
- Below it, show summary stats: total tickers, how many are elevated (alert_score >= 4), how many are escalating.

**Styling:**
- Monochrome palette with red/orange accents for alerts.
- System fonts only (no Google Fonts).
- Responsive — should look decent on both desktop and mobile widths.
- No JavaScript. Pure HTML + CSS.

---

## Step 6: Batch Generate All Company Timelines

**Task:** Write a script `trends/batch_timelines.py` that:

1. Reads `trends/trend_data.csv` and gets the list of all unique tickers.
2. For each ticker, calls the same charting logic from Step 2 to generate `trends/charts/{ticker}_timeline.png`.
3. Prints progress: "Generating timeline for AAPL... (1/30)".
4. After all charts are generated, creates an index file `trends/charts/index.html` with:
   - A thumbnail grid linking to each ticker's full timeline PNG.
   - Sorted by latest alert_score descending.

---

## Step 7: Build the Interactive React Artifact (Optional / Demo Mode)

**Task:** Write a single-file React component `trends/redink_trends.jsx` that:

1. Contains hardcoded JSON data for the top 10 companies by alert_score (extracted from `trend_data.csv` — the agent should embed this data directly in the JSX file).
2. Renders:
   - A dropdown to select a ticker.
   - A line chart (using Recharts) showing alert_score over time for the selected ticker.
   - A color-coded bar below the chart showing the trend_category.
   - A small table of the top flagged metrics for the latest quarter.
3. Uses Tailwind for styling. No external data fetches — all data is embedded.
4. This is meant to be a **demo artifact** that can be rendered directly in Claude's artifact viewer.

**Data format to embed:**
```json
{
  "FANG": {
    "quarters": ["Q1 '22", "Q2 '22", ...],
    "alert_scores": [2, 3, 5, 8, 7, ...],
    "trend_category": "ESCALATING",
    "top_flags": [
      {"metric": "sga_pct_revenue", "z": 3.1, "direction": "high"},
      ...
    ]
  },
  ...
}
```

---

## File Structure When Complete

```
trends/
├── build_trend_data.py
├── single_company_timeline.py
├── multi_company_heatmap.py
├── trend_detector.py
├── build_dashboard.py
├── batch_timelines.py
├── redink_trends.jsx
├── schema.json
├── trend_data.csv
├── trend_classifications.csv
├── dashboard.html
└── charts/
    ├── index.html
    ├── universe_heatmap.png
    ├── AAPL_timeline.png
    ├── AAPL_timeline.pdf
    ├── FANG_timeline.png
    ├── FANG_timeline.pdf
    └── ... (one PNG + PDF per ticker)
```

---

## Dependencies

```
pip install google-cloud-bigquery pandas matplotlib seaborn numpy scipy
```

Ensure BigQuery credentials are configured.

---

## Notes for the Agent

- **Schema first.** Step 1 must complete before everything else. Save the schema to JSON so downstream scripts can reference actual column names.
- **The heatmap is the hero visual for the demo.** Spend extra time making it clean and readable. If there are 30 tickers × 20 quarters = 600 cells, the annotations may be too dense — in that case, only annotate cells where alert_score >= 4 and leave the rest blank.
- **The trend detector logic is the analytical core.** The distinction between ESCALATING (getting worse), PERSISTENT (stubbornly elevated), and NEW_SPIKE (sudden appearance) is what makes this more than just a chart. These categories drive the dashboard priority order.
- **Color consistency matters.** Use the same color scheme across all charts: green = clean, yellow = low, orange = medium, red = high. Define these as constants in a shared `trends/config.py` file.
- **The React artifact (Step 7) is optional but high-impact for the Google demo.** If time is short, skip it and rely on the static charts + HTML dashboard. But if built, it's the most impressive demo piece because it's interactive.
- **Print progress throughout every script.** "Processing INTC... (14/30)" style.
- **Each script must be independently runnable** given its input files exist.
