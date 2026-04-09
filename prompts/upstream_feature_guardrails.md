# Upstream Feature Request: Guardrails for Engineered Anomaly Features

**From:** scoring-pipeline  
**To:** qqq-anomaly-lab-repo  
**Purpose:** Add denominator guardrails to `engineered_anomaly_features` computation so that division-by-zero, near-zero denominators, and insufficient history produce clean `null` values instead of `inf`, `NaN`, or extreme ratios that corrupt downstream scoring.

---

## Why this matters

The scoring pipeline uses `engineered_anomaly_features` directly as inputs to:
- Winsorization (5th–95th percentile clip)
- Robust z-scores (self-history and peer-relative)
- MCD Mahalanobis distance

When a ratio blows up due to a near-zero denominator, it does not just affect that one filing — it shifts the **entire distribution** used for all 1,367+ filings. A single `ocf_to_net_income = -14.39` (observed: DDOG) moves the 95th percentile of the winsorization clip, compresses every other company's z-score, and distorts the Mahalanobis covariance matrix for the whole cohort.

The fix is simple and non-destructive: **write `null` when the denominator is unreliable.** The scoring pipeline already handles `null` correctly via median imputation before z-scoring. Writing `null` is always safer than writing an extreme value.

---

## Current feature formulas

```python
debt_to_assets       = total_debt / total_assets
net_margin           = net_income / revenue
revenue_yoy_growth   = (revenue_t - revenue_{t-1}) / revenue_{t-1}
accrual_ratio        = (net_income - ocf) / avg_total_assets
ocf_to_assets        = ocf / total_assets
ocf_to_net_income    = ocf / net_income
equity_multiplier    = total_assets / total_equity
assets_yoy_growth    = (assets_t - assets_{t-1}) / assets_{t-1}
```

---

## Required guardrails, feature by feature

### 1. `debt_to_assets` and `ocf_to_assets`

**Denominator:** `total_assets`

**Guard:**
```python
if total_assets is None or total_assets <= 0:
    debt_to_assets = None
    ocf_to_assets  = None
```

**Why:** `total_assets <= 0` is always a data extraction error. A company cannot have zero or negative total assets. This contaminates every asset-ratio feature simultaneously.

**Real case:** Any filing where the XBRL `Assets` tag was missing and defaulted to 0.

---

### 2. `net_margin`

**Denominator:** `revenue`

**Guard:**
```python
if revenue is None or revenue <= 0:
    net_margin = None
```

**Why:** Revenue should always be positive for an operating company. Zero or negative revenue means the wrong XBRL tag was used or the period had no revenue (e.g., holding company, first quarter post-spin-off). Dividing by near-zero revenue produces a net margin that is orders of magnitude outside any meaningful range.

---

### 3. `revenue_yoy_growth`

**Denominator:** `revenue_{t-1}` (prior-year same period)

**Guard:**
```python
if revenue_prior is None or revenue_prior <= 0:
    revenue_yoy_growth = None

elif revenue_current / revenue_prior > 10:
    # Growth > 900% — almost always a data artifact (wrong period matched,
    # unit mismatch, or first quarter after a near-zero revenue period).
    # Write the value but also set the flag so it can be reviewed.
    revenue_yoy_growth = (revenue_current - revenue_prior) / revenue_prior
    _flags["revenue_yoy_growth_extreme"] = True

else:
    revenue_yoy_growth = (revenue_current - revenue_prior) / revenue_prior
```

**Why:** Companies in their first 1–2 years post-IPO (e.g., CRWD 2021, AXON 2021) may have had near-zero revenue in the prior-year period. The resulting YoY growth of 2,000–5,000% is not analytically meaningful and becomes the dominant driver in every z-score computation for that company.

The `> 10x` threshold (900% growth) is intentionally conservative — if a company genuinely grew 900% YoY, that IS an anomaly and we want to catch it, but we want it flagged for human review rather than silently distorting the distribution.

---

### 4. `accrual_ratio`

**Denominator:** `avg_total_assets` (average of assets at start and end of period)

**Guard:**
```python
# Require at least one prior period to compute a meaningful average
if assets_prior is None:
    # Fall back to single-period assets rather than writing null,
    # but flag it so the scoring pipeline knows the average is shallow
    avg_assets = assets_current
    _flags["accrual_ratio_single_period_assets"] = True
else:
    avg_assets = (assets_current + assets_prior) / 2

if avg_assets is None or avg_assets <= 0:
    accrual_ratio = None
elif abs(avg_assets) < 0.01 * abs(assets_current if assets_current else 1):
    # avg_assets is implausibly small relative to current assets
    accrual_ratio = None
    _flags["accrual_ratio_unstable_denominator"] = True
else:
    accrual_ratio = (net_income - ocf) / avg_assets
```

**Why:** Early-stage companies have very small average assets in their first few quarters. AXON 2021 showed `accrual_ratio = -5.97` — an extreme value 3–4 standard deviations beyond any other filing in the universe, entirely due to the denominator being small relative to the numerator. This single value pulls the entire cohort's z-score distribution.

---

### 5. `ocf_to_net_income`

**Guard: cap the output, not the input.**

Do not apply a denominator threshold. Instead, compute the ratio and null it if the result exceeds ±10 in absolute value.

```python
if net_income is None or net_income == 0:
    ocf_to_net_income = None

else:
    ratio = ocf / net_income
    if abs(ratio) > 10:
        ocf_to_net_income = None
        _flags["ocf_to_net_income_unstable"] = True
    else:
        ocf_to_net_income = ratio
```

**Why outcome-based, not denominator-based:**

A denominator threshold (e.g. "net_income must be at least X% of assets") forces you to guess upfront whether net income is "too small." That guess is wrong in both directions — it either nulls legitimate near-breakeven readings or still allows extreme ratios through.

Capping the output at ±10 is directly observable and analytically defensible: no equity analyst interprets `ocf_to_net_income = 14` as a real signal. The ratio is widely meaningful in the range [-5, +5]; beyond ±10 it is universally treated as a data artifact caused by near-zero net income.

**Why ±10 specifically:** A ratio of 10 means OCF is 10x net income — already an extreme earnings quality signal. Beyond that, the magnitude conveys no additional information and only distorts z-score distributions for the rest of the universe.

**Signal is not lost when nulled:** When `ocf_to_net_income` is null because net income is near zero, `accrual_ratio = (net_income - ocf) / avg_assets` captures the same underlying earnings quality signal in a more stable, asset-scaled form. This is precisely why Beneish used the TATA formulation rather than `ocf/net_income` in his manipulation model — he solved this instability problem the same way.

**Confirmed case:** DDOG Q3 2022 showed `ocf_to_net_income = -14.39`. This single value shifted the 95th percentile winsorization boundary for the entire 1,367-filing universe, compressing every other company's z-scores. Writing `null` here removes noise, not signal.

---

### 6. `equity_multiplier`

**Denominator:** `total_equity`

**Guard:**
```python
# total_equity can legitimately be negative (companies with aggressive buybacks,
# e.g. STX). Negative equity is a valid financial state and the ratio still
# has meaning. However, near-zero equity (positive or negative) makes the
# ratio blow up.
min_equity_threshold = 0.02 * total_assets if total_assets else None

if total_equity is None:
    equity_multiplier = None

elif min_equity_threshold is not None and abs(total_equity) < min_equity_threshold:
    # Equity is less than 2% of assets in absolute terms — ratio is unstable
    equity_multiplier = None
    _flags["equity_multiplier_near_zero_equity"] = True

else:
    equity_multiplier = total_assets / total_equity
```

**Why:** Companies with negative equity due to buybacks (STX, many mature tech companies) are valid cases and the ratio should be computed. But when equity is within ~2% of zero (approaching a sign change), the ratio swings wildly between +∞ and -∞ and has no analytical meaning.

---

### 7. `assets_yoy_growth`

**Denominator:** `assets_{t-1}` (prior-year same period)

**Guard:**
```python
if assets_prior is None or assets_prior <= 0:
    assets_yoy_growth = None

elif assets_current / assets_prior > 5:
    # Growth > 400% — flag for review, likely acquisition or data issue
    assets_yoy_growth = (assets_current - assets_prior) / assets_prior
    _flags["assets_yoy_growth_extreme"] = True

else:
    assets_yoy_growth = (assets_current - assets_prior) / assets_prior
```

**Why:** Similar to revenue growth — early-stage companies or post-acquisition periods can show asset growth that is technically correct but analytically misleading as an anomaly signal.

---

## `_flags` output structure

Add a top-level `_feature_flags` key to `engineered_anomaly_features` (or as a sibling key):

```json
{
  "engineered_anomaly_features": {
    "debt_to_assets": 0.71,
    "net_margin": -0.19,
    "revenue_yoy_growth": null,
    "accrual_ratio": null,
    "ocf_to_assets": 0.04,
    "ocf_to_net_income": null,
    "equity_multiplier": 3.48,
    "assets_yoy_growth": 0.89
  },
  "_feature_flags": {
    "revenue_yoy_growth_extreme": true,
    "ocf_to_net_income_unstable_denominator": true,
    "accrual_ratio_single_period_assets": true
  }
}
```

The scoring pipeline will read `_feature_flags` and:
1. Log flagged filings during the flatten step
2. Optionally surface flags as columns in the output CSV for analyst review
3. Use flags to weight down confidence scores for affected features

---

## Summary: guard conditions at a glance

| Feature | Denominator | Write `null` when |
|---------|-------------|-------------------|
| `debt_to_assets` | `total_assets` | `assets <= 0` |
| `net_margin` | `revenue` | `revenue <= 0` |
| `revenue_yoy_growth` | `revenue_prior` | `revenue_prior <= 0` |
| `accrual_ratio` | `avg_assets` | `avg_assets <= 0` |
| `ocf_to_assets` | `total_assets` | `assets <= 0` |
| `ocf_to_net_income` | `net_income` | `net_income == 0` or `abs(ocf/net_income) > 10` |
| `equity_multiplier` | `total_equity` | `abs(equity) < 2% of assets` |
| `assets_yoy_growth` | `assets_prior` | `assets_prior <= 0` |

**Universal rule:** never write `inf`, `-inf`, or `NaN` to the JSON. Always write `null` when a computation fails or a guard triggers. The scoring pipeline treats `null` as "not scoreable for this feature" and handles it gracefully.

---

## Known affected filings (confirmed from scoring output)

These are real cases already in production output that motivated this request:

| Ticker | Period | Feature | Observed value | Likely cause |
|--------|--------|---------|----------------|--------------|
| DDOG | 2022-09-30 | `ocf_to_net_income` | -14.39 | near-zero net income |
| AXON | 2021-06-30 | `accrual_ratio` | -5.97 | small avg_assets post-IPO |
| AXON | 2021-03-31 | `accrual_ratio` | -5.32 | small avg_assets post-IPO |
| CRWD | 2021 filings | `revenue_yoy_growth` | 2.6–4.1 | small prior-year revenue post-IPO |
| STX | 2024-03-29 | `debt_to_assets` | 1.27 | verify: negative equity from buybacks — may be valid |
