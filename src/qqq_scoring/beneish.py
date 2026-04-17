"""Beneish M-Score computation — earnings manipulation detection.

The Beneish M-Score (Messod Beneish, 1999) uses 8 financial ratios comparing
consecutive periods to estimate the probability a company is manipulating its
reported earnings.

    M > -2.22  →  likely manipulator
    M ≤ -2.22  →  not likely manipulating

Each component is an index (ratio of ratios) comparing current period to prior
year same period — except TATA which is current-period only.

References:
    Beneish, M.D. (1999). The Detection of Earnings Manipulation.
    Financial Analysts Journal, 55(5), 24–36.
"""

import pandas as pd

# Beneish M-Score intercept and coefficients
_INTERCEPT = -4.84
_COEFF = {
    "dsri":  0.920,   # Days Sales Receivable Index
    "gmi":   0.528,   # Gross Margin Index
    "aqi":   0.404,   # Asset Quality Index
    "sgi":   0.892,   # Sales Growth Index
    "depi":  0.115,   # Depreciation Index
    "sgai": -0.172,   # SG&A Expense Index
    "tata":  4.679,   # Total Accruals to Total Assets  ← highest weight
    "lvgi": -0.327,   # Leverage Index
}

# Minimum components required to produce a valid M-Score
MIN_COMPONENTS = 5

# Manipulation probability threshold (Beneish 1999)
MANIPULATION_THRESHOLD = -2.22

# Cap on index-type components (ratios of ratios) — beyond ±10 is unstable noise
_INDEX_CAP = 10.0


def _v(row: pd.Series, col: str):
    """Return float value or None if missing/NaN."""
    val = row.get(col)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return None
    return float(val)


def _ratio(num, denom):
    """Divide num/denom safely. Returns None if denom is None/zero."""
    if num is None or denom is None or denom == 0:
        return None
    return num / denom


def _index(curr_ratio, prior_ratio):
    """Compute a Beneish index component: curr_ratio / prior_ratio.

    Returns None if either input is None. Caps result at ±_INDEX_CAP to
    prevent unstable denominators from dominating the M-Score.
    """
    val = _ratio(curr_ratio, prior_ratio)
    if val is None:
        return None
    if abs(val) > _INDEX_CAP:
        return None  # treat as unstable, not informative
    return val


def _compute_row(row: pd.Series) -> dict:
    """Compute all Beneish components and M-Score for a single filing row."""

    # ── Current period ──────────────────────────────────────────────────────
    ar_t     = _v(row, "b_curr_accounts_receivable")
    rev_t    = _v(row, "b_curr_revenue")
    cogs_t   = _v(row, "b_curr_cost_of_revenue")
    ca_t     = _v(row, "b_curr_current_assets")
    ppe_t    = _v(row, "b_curr_ppe_net")
    assets_t = _v(row, "b_curr_total_assets")
    dep_t    = _v(row, "b_curr_depreciation_amortization")
    sga_t    = _v(row, "b_curr_sga_expense")
    ltd_t    = _v(row, "b_curr_long_term_debt")
    cl_t     = _v(row, "b_curr_current_liabilities")
    ni_t     = _v(row, "b_curr_net_income")
    ocf_t    = _v(row, "b_curr_operating_cash_flow")
    sbc_t    = _v(row, "b_curr_stock_based_compensation")

    # ── Prior year same period ───────────────────────────────────────────────
    ar_p     = _v(row, "b_prior_accounts_receivable")
    rev_p    = _v(row, "b_prior_revenue")
    cogs_p   = _v(row, "b_prior_cost_of_revenue")
    ca_p     = _v(row, "b_prior_current_assets")
    ppe_p    = _v(row, "b_prior_ppe_net")
    assets_p = _v(row, "b_prior_total_assets")
    dep_p    = _v(row, "b_prior_depreciation_amortization")
    sga_p    = _v(row, "b_prior_sga_expense")
    ltd_p    = _v(row, "b_prior_long_term_debt")
    cl_p     = _v(row, "b_prior_current_liabilities")

    # ── Component 1: DSRI — Days Sales Receivable Index ─────────────────────
    # Rising receivables relative to revenue → revenue recognition inflation
    # (AR_t/Rev_t) / (AR_{t-1}/Rev_{t-1})
    dsri = _index(
        _ratio(ar_t, rev_t),
        _ratio(ar_p, rev_p),
    )

    # ── Component 2: GMI — Gross Margin Index ───────────────────────────────
    # Deteriorating gross margin → margin pressure or manipulation
    # [(Rev_{t-1} - COGS_{t-1}) / Rev_{t-1}] / [(Rev_t - COGS_t) / Rev_t]
    gm_t = _ratio((rev_t - cogs_t), rev_t) if (rev_t and cogs_t is not None) else None
    gm_p = _ratio((rev_p - cogs_p), rev_p) if (rev_p and cogs_p is not None) else None
    gmi = _index(gm_p, gm_t)  # note: prior over current (higher = worse margin)

    # ── Component 3: AQI — Asset Quality Index ───────────────────────────────
    # Fraction of non-current, non-tangible assets — rising ratio suggests
    # management is capitalising operating costs rather than expensing them.
    # Original: [1-(CA+PPE)/Assets] current / prior.
    #
    # Goodwill + intangibles strip: Beneish was designed to catch capitalised
    # operating expenses (deferred costs, capitalised R&D). Goodwill and
    # acquired intangibles from M&A are a different beast — purchase-accounting
    # artifacts, not management-discretion accruals. Serial acquirers (CSCO 49%,
    # ADBE 45%, AMD 35%, KHC 32%, MSFT 21% of assets in goodwill) systematically
    # tripped AQI under the original formula. Stripping goodwill + intangibles
    # from the numerator isolates the capitalisation signal Beneish actually
    # intended to catch.
    #
    # Graceful degradation: null goodwill/intangibles treated as 0. Non-acquirers
    # (NVDA, COST) see no change vs the original formula.
    gw_t  = _v(row, "b_curr_goodwill")            or 0.0
    int_t = _v(row, "b_curr_intangible_assets_net") or 0.0
    gw_p  = _v(row, "b_prior_goodwill")           or 0.0
    int_p = _v(row, "b_prior_intangible_assets_net") or 0.0
    aqi_t = (1 - (ca_t + ppe_t + gw_t + int_t) / assets_t) if (ca_t is not None and ppe_t is not None and assets_t) else None
    aqi_p = (1 - (ca_p + ppe_p + gw_p + int_p) / assets_p) if (ca_p is not None and ppe_p is not None and assets_p) else None
    aqi = _index(aqi_t, aqi_p)

    # ── Component 4: SGI — Sales Growth Index ───────────────────────────────
    # High growth → more incentive and opportunity to manipulate
    # Rev_t / Rev_{t-1}
    sgi = _index(rev_t, rev_p)

    # ── Component 5: DEPI — Depreciation Index ──────────────────────────────
    # Slowing depreciation rate → extending asset lives to inflate earnings
    # [Dep_{t-1}/(PPE_{t-1}+Dep_{t-1})] / [Dep_t/(PPE_t+Dep_t)]
    depi_t = _ratio(dep_t, (ppe_t + dep_t)) if (dep_t is not None and ppe_t is not None and (ppe_t + dep_t) != 0) else None
    depi_p = _ratio(dep_p, (ppe_p + dep_p)) if (dep_p is not None and ppe_p is not None and (ppe_p + dep_p) != 0) else None
    depi = _index(depi_p, depi_t)  # prior over current

    # ── Component 6: SGAI — SG&A Expense Index ──────────────────────────────
    # SG&A growing faster than revenue → operating leverage deterioration
    # (SGA_t/Rev_t) / (SGA_{t-1}/Rev_{t-1})
    sgai = _index(
        _ratio(sga_t, rev_t),
        _ratio(sga_p, rev_p),
    )

    # ── Component 7: TATA — Total Accruals to Total Assets ──────────────────
    # Core earnings quality signal: high accruals = earnings not backed by cash
    # SBC-adjusted formula: (NetIncome_t + SBC_t - OCF_t) / Assets_t
    #
    # Why the adjustment: SBC is a non-cash expense that reduces Net Income but
    # is added back in Operating Cash Flow. Without adjustment, high-SBC growth
    # companies (PLTR, CRWD, APP) produce deeply negative TATA — not because
    # earnings are accrual-inflated, but because NI and OCF treat SBC differently.
    # Adding SBC back to NI restores parity and removes this structural false positive.
    #
    # Graceful degradation: if SBC is unavailable (upstream not yet supplying it),
    # sbc_adj defaults to 0 and behaviour is identical to the original formula.
    if ni_t is not None and ocf_t is not None and assets_t:
        sbc_adj = sbc_t if sbc_t is not None else 0.0
        tata = (ni_t + sbc_adj - ocf_t) / assets_t
        # TATA is naturally bounded; values beyond ±1 indicate bad upstream data
        tata = tata if abs(tata) <= 1.0 else None
    else:
        tata = None

    # ── Component 8: LVGI — Leverage Index ──────────────────────────────────
    # Rising leverage → debt covenant pressure increases manipulation incentive
    # [(LTD_t + CL_t)/Assets_t] / [(LTD_{t-1} + CL_{t-1})/Assets_{t-1}]
    lvgi_t = _ratio((ltd_t + cl_t), assets_t) if (ltd_t is not None and cl_t is not None and assets_t) else None
    lvgi_p = _ratio((ltd_p + cl_p), assets_p) if (ltd_p is not None and cl_p is not None and assets_p) else None
    lvgi = _index(lvgi_t, lvgi_p)

    # ── M-Score ──────────────────────────────────────────────────────────────
    components = {
        "beneish_dsri": dsri,
        "beneish_gmi":  gmi,
        "beneish_aqi":  aqi,
        "beneish_sgi":  sgi,
        "beneish_depi": depi,
        "beneish_sgai": sgai,
        "beneish_tata": tata,
        "beneish_lvgi": lvgi,
    }

    available = [(k.replace("beneish_", ""), v) for k, v in components.items() if v is not None]
    n_available = len(available)

    if n_available >= MIN_COMPONENTS:
        m_score = _INTERCEPT + sum(_COEFF[k] * v for k, v in available)
        m_score = round(m_score, 4)
        manipulation_flag = bool(m_score > MANIPULATION_THRESHOLD)
    else:
        m_score = None
        manipulation_flag = None

    return {
        **components,
        "beneish_m_score":              m_score,
        "beneish_manipulation_flag":    manipulation_flag,
        "beneish_components_available": n_available,
    }


def compute_beneish(df: pd.DataFrame) -> pd.DataFrame:
    """Compute Beneish M-Score for every row in df.

    Expects b_curr_* and b_prior_* columns produced by the flatten step.
    Returns a DataFrame of beneish_* columns with the same index as df.
    """
    rows = [_compute_row(row) for _, row in df.iterrows()]
    return pd.DataFrame(rows, index=df.index)
