# Upstream Feature Request: Beneish M-Score Raw Inputs

**From:** scoring-pipeline  
**To:** qqq-anomaly-lab-repo  
**Purpose:** Add raw financial statement values to `analysis_ready.json` so the scoring pipeline can compute Beneish M-Score — an academically validated, 8-variable earnings manipulation detection model used widely by CFA analysts and hedge fund quants.

---

## What is the Beneish M-Score and why does it matter

The Beneish M-Score (Messod Beneish, 1999) is a quantitative model that predicts the probability a company is **manipulating its reported earnings**. It uses 8 financial ratios computed from two consecutive periods. The final score:

- **M > -2.22** → likely manipulator (the higher, the more suspicious)
- **M ≤ -2.22** → not likely manipulating

This is not a theoretical exercise — Beneish's model flagged Enron before its collapse. It is standard in forensic accounting, the CFA curriculum, and used by systematic equity funds as a quality screen.

The 8 components and what they detect:

| Variable | Formula | What it detects |
|----------|---------|----------------|
| **DSRI** — Days Sales Receivable Index | `(AR_t / Rev_t) / (AR_{t-1} / Rev_{t-1})` | Receivables growing faster than revenue → inflated revenue recognition |
| **GMI** — Gross Margin Index | `GrossMargin_{t-1} / GrossMargin_t` | Deteriorating gross margin → margin pressure or manipulation |
| **AQI** — Asset Quality Index | `(1 - (CA_t + PPE_t) / Assets_t) / (1 - (CA_{t-1} + PPE_{t-1}) / Assets_{t-1})` | Rising non-current, non-tangible assets → capitalising expenses |
| **SGI** — Sales Growth Index | `Rev_t / Rev_{t-1}` | High growth companies have more incentive and opportunity to manipulate |
| **DEPI** — Depreciation Index | `DepRate_{t-1} / DepRate_t` | Slowing depreciation → extending asset lives to inflate earnings |
| **SGAI** — SG&A Index | `(SGA_t / Rev_t) / (SGA_{t-1} / Rev_{t-1})` | Rising SG&A as % of revenue → operating leverage deterioration |
| **LVGI** — Leverage Index | `Leverage_t / Leverage_{t-1}` | Rising leverage → debt covenant pressure increases manipulation incentive |
| **TATA** — Total Accruals to Total Assets | `(NetIncome_t - OCF_t) / Assets_t` | The core earnings quality signal: high accruals = earnings not backed by cash |

Final formula:
```
M = -4.84 + 0.920*DSRI + 0.528*GMI + 0.404*AQI + 0.892*SGI
        + 0.115*DEPI - 0.172*SGAI + 4.679*TATA - 0.327*LVGI
```

---

## What to add to `analysis_ready.json`

Add a new top-level key: **`beneish_raw_features`**

Do not compute the ratios upstream — extract the raw values and let the scoring pipeline compute the components. This gives maximum flexibility if the formula needs to be adjusted.

Structure:

```json
{
  "beneish_raw_features": {
    "current": {
      "accounts_receivable":        null,
      "revenue":                    null,
      "cost_of_revenue":            null,
      "current_assets":             null,
      "ppe_net":                    null,
      "total_assets":               null,
      "depreciation_amortization":  null,
      "sga_expense":                null,
      "long_term_debt":             null,
      "current_liabilities":        null,
      "net_income":                 null,
      "operating_cash_flow":        null
    },
    "prior_year_same_period": {
      "accounts_receivable":        null,
      "revenue":                    null,
      "cost_of_revenue":            null,
      "current_assets":             null,
      "ppe_net":                    null,
      "total_assets":               null,
      "depreciation_amortization":  null,
      "sga_expense":                null,
      "long_term_debt":             null,
      "current_liabilities":        null,
      "net_income":                 null,
      "operating_cash_flow":        null
    },
    "_sources": {}
  }
}
```

`_sources` is a diagnostic dict — for each field, record which XBRL concept tag was actually used (or `"missing"` if none found). Example: `{ "revenue": "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax", "sga_expense": "computed:G&A+S&M" }`. This is critical for debugging unexpected scores.

---

## Period alignment rules

**For 10-Q filings:**

- **Balance sheet items** (`accounts_receivable`, `current_assets`, `ppe_net`, `total_assets`, `long_term_debt`, `current_liabilities`):
  - `current` = the balance sheet date of the selected filing (e.g. 2026-01-31)
  - `prior_year_same_period` = the balance sheet date exactly one year prior (e.g. 2025-01-31)
  - Use the period-end value only, not averages

- **Flow items** (`revenue`, `cost_of_revenue`, `depreciation_amortization`, `sga_expense`, `net_income`, `operating_cash_flow`):
  - `current` = the single quarter covered by the selected 10-Q (e.g. Q2 FY2026: Nov 1 – Jan 31, 2026)
  - `prior_year_same_period` = the same calendar quarter one year prior (e.g. Nov 1 – Jan 31, 2025)
  - **Do NOT use YTD cumulative values** — use single-quarter values only for consistency
  - If the filing only reports YTD (cumulative), subtract the prior-period YTD from the companyfacts history to derive the single-quarter value

**For 10-K filings:**

- `current` = full fiscal year of the selected filing
- `prior_year_same_period` = full fiscal year one year prior
- Straightforward — no quarterly adjustment needed

---

## XBRL concept tags, by field

Use the primary tag first. If not found, try fallbacks in order. If all fail, write `null`.

### `revenue`
Primary: `us-gaap/Revenues`  
Fallbacks (in order):
1. `us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax`
2. `us-gaap/RevenueFromContractWithCustomerIncludingAssessedTax`
3. `us-gaap/SalesRevenueNet`
4. `us-gaap/SalesRevenueGoodsNet`

Note: This should match or be very close to what is already used to compute `revenue_yoy_growth` in `engineered_anomaly_features`. Use the same tag for consistency.

### `cost_of_revenue`
Primary: `us-gaap/CostOfRevenue`  
Fallbacks:
1. `us-gaap/CostOfGoodsSoldAndServicesSold`
2. `us-gaap/CostOfGoodsSold`
3. `us-gaap/CostOfServices`

Note: Many tech/SaaS companies report "Cost of Revenue" not "COGS" — prefer `CostOfRevenue`.

### `accounts_receivable`
Primary: `us-gaap/AccountsReceivableNetCurrent`  
Fallbacks:
1. `us-gaap/ReceivablesNetCurrent`
2. `us-gaap/AccountsReceivableNet`
3. `us-gaap/TradeAndOtherReceivablesNetCurrent`

Note: Use **net** receivables (after allowance for doubtful accounts). If only gross receivables are available, use gross and record in `_sources`.

### `current_assets`
Primary: `us-gaap/AssetsCurrent`  
No common fallback — if missing, write `null`.

### `ppe_net`
Primary: `us-gaap/PropertyPlantAndEquipmentNet`  
Fallbacks:
1. `us-gaap/PropertyPlantAndEquipmentAndFinanceLeaseRightOfUseAssetAfterAccumulatedDepreciationAndAmortization`

Note: Use **net** PP&E (after accumulated depreciation). Gross PP&E would overstate the AQI component.

### `total_assets`
Primary: `us-gaap/Assets`  
This is already extracted for existing features — reuse the same value.

### `depreciation_amortization`
Primary: `us-gaap/DepreciationAndAmortization`  
Fallbacks:
1. `us-gaap/DepreciationDepletionAndAmortization`
2. `us-gaap/Depreciation`

**Important edge case:** Many tech companies do not report D&A as a separate income statement line — they embed it in COGS and SG&A. In that case, look in the **cash flow statement** (operating activities section) where D&A is always shown as a reconciling item:  
- `us-gaap/DepreciationDepletionAndAmortization` in the operating cash flow context

If found in cash flow statement only, record `_sources["depreciation_amortization"] = "cash_flow_statement"`.

### `sga_expense`
Primary: `us-gaap/SellingGeneralAndAdministrativeExpense`  
Fallbacks:
1. Sum of `us-gaap/GeneralAndAdministrativeExpense` + `us-gaap/SellingAndMarketingExpense`
2. Sum of `us-gaap/GeneralAndAdministrativeExpense` + `us-gaap/SellingExpense`

**Important edge case:** Many tech companies (especially SaaS) separate G&A and Sales & Marketing instead of reporting a combined SG&A. If the combined tag is missing, sum the components and record `_sources["sga_expense"] = "computed:G&A+S&M"`.

If neither combined nor components are found, write `null`.

### `long_term_debt`
Primary: `us-gaap/LongTermDebt`  
Fallbacks:
1. `us-gaap/LongTermDebtNoncurrent`
2. `us-gaap/LongTermDebtAndCapitalLeaseObligations`

Note: Use the **noncurrent** portion only. Current portion of long-term debt is captured in current liabilities.

### `current_liabilities`
Primary: `us-gaap/LiabilitiesCurrent`  
No common fallback — if missing, write `null`.

### `net_income`
Primary: `us-gaap/NetIncomeLoss`  
This is already extracted for existing features — reuse the same tag.

### `operating_cash_flow`
Primary: `us-gaap/NetCashProvidedByUsedInOperatingActivities`  
This is already used for `ocf_to_net_income` and `ocf_to_assets` — reuse the same tag.

---

## Edge case handling rules

1. **Always write `null`, never `0`, for missing values.** A zero receivables balance is a legitimate financial value. A missing tag should be `null`. Downstream the scoring pipeline will detect nulls and skip Beneish computation for that filing.

2. **Negative values are valid.** Net income can be negative. OCF can be negative. Do not clip or zero-out negative values.

3. **Unit consistency.** All values must be in the same unit as the rest of the filing (typically thousands or millions of USD as reported in XBRL). Do not convert units — the scoring pipeline handles normalisation via ratios, so the unit cancels out as long as current and prior-year values use the same unit.

4. **Prior-year period not found.** If the companyfacts history does not contain data for the prior-year same period (e.g. company IPO'd less than 12 months ago), write `null` for all `prior_year_same_period` fields. Do not attempt to extrapolate.

5. **Multiple values for the same concept in the same period.** Sometimes XBRL filings report the same concept multiple times (restated, amended). Use the most recently filed value for the target period. If in doubt, prefer the value from the selected filing itself over earlier filings.

6. **Fiscal quarter vs calendar quarter.** The period alignment described above uses the company's own fiscal quarter dates, not calendar dates. Do not convert to calendar quarters — just match the filing period dates directly.

---

## Validation checks (run before writing to GCS)

After extracting values, run these sanity checks and write warnings to a `_validation` key:

```python
warnings = []

# Revenue must be positive
if current["revenue"] is not None and current["revenue"] <= 0:
    warnings.append("current.revenue <= 0")

# Receivables must be less than total assets
if current["accounts_receivable"] and current["total_assets"]:
    if current["accounts_receivable"] > current["total_assets"]:
        warnings.append("accounts_receivable > total_assets")

# Current assets must be less than total assets
if current["current_assets"] and current["total_assets"]:
    if current["current_assets"] > current["total_assets"]:
        warnings.append("current_assets > total_assets")

# PPE must be less than total assets
if current["ppe_net"] and current["total_assets"]:
    if current["ppe_net"] > current["total_assets"]:
        warnings.append("ppe_net > total_assets")

# Revenue current vs existing revenue_yoy feature (sanity cross-check)
# If revenue from beneish extraction differs by >5% from what was used
# for revenue_yoy_growth, flag it
```

Write `_validation: { "warnings": [...] }` to `beneish_raw_features`. Empty list = clean.

---

## Output example (PANW, 10-Q quarter ending 2026-01-31)

```json
{
  "beneish_raw_features": {
    "current": {
      "accounts_receivable":        2341000000,
      "revenue":                    2255000000,
      "cost_of_revenue":            612000000,
      "current_assets":             7823000000,
      "ppe_net":                    890000000,
      "total_assets":               18432000000,
      "depreciation_amortization":  143000000,
      "sga_expense":                891000000,
      "long_term_debt":             3990000000,
      "current_liabilities":        6201000000,
      "net_income":                 -48000000,
      "operating_cash_flow":        872000000
    },
    "prior_year_same_period": {
      "accounts_receivable":        1987000000,
      "revenue":                    1983000000,
      "cost_of_revenue":            551000000,
      "current_assets":             6901000000,
      "ppe_net":                    812000000,
      "total_assets":               16201000000,
      "depreciation_amortization":  128000000,
      "sga_expense":                801000000,
      "long_term_debt":             3990000000,
      "current_liabilities":        5811000000,
      "net_income":                 -84000000,
      "operating_cash_flow":        701000000
    },
    "_sources": {
      "revenue": "us-gaap/RevenueFromContractWithCustomerExcludingAssessedTax",
      "cost_of_revenue": "us-gaap/CostOfRevenue",
      "accounts_receivable": "us-gaap/AccountsReceivableNetCurrent",
      "sga_expense": "computed:G&A+S&M",
      "depreciation_amortization": "cash_flow_statement"
    },
    "_validation": {
      "warnings": []
    }
  }
}
```

---

## What the scoring pipeline will do with this

Once `beneish_raw_features` is present, the scoring pipeline (`src/qqq_scoring/scorer.py`) will:

1. Compute the 8 Beneish ratios (DSRI, GMI, AQI, SGI, DEPI, SGAI, LVGI, TATA)
2. Apply the Beneish formula: `M = -4.84 + 0.920*DSRI + ...`
3. Add `beneish_m_score` and `beneish_manipulation_flag` (True if M > -2.22) to `quarterly_scores_detailed.csv`
4. Surface it as a named signal alongside the anomaly score in the review pack

A filing can have a low anomaly score but a high Beneish score — those are the most dangerous companies because the numbers look "normal" but the accounting patterns suggest manipulation. Surfacing that combination is the core value.

---

## Priority

The most impactful fields if a full implementation is not immediately feasible (in order):

1. `accounts_receivable` — needed for DSRI, the strongest single signal
2. `cost_of_revenue` — needed for GMI
3. `sga_expense` — needed for SGAI
4. `depreciation_amortization` — needed for DEPI
5. `current_assets` + `ppe_net` — needed for AQI

`total_assets`, `net_income`, `operating_cash_flow`, `revenue`, `long_term_debt`, and `current_liabilities` are already partially available from existing feature extraction — confirm they are accessible for both current and prior-year periods.
