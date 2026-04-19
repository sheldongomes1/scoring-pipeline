"""Helpers for compute_score_explanation.py — display lookups + transform builders.

Phase 1 (v1): operates entirely on data already in BigQuery
(`quarterly_scores_detailed`, `conviction_scores`, `narrative_divergence`).
Self/peer medians + IQRs are NOT in BQ today — those fields render as null
and the row is tagged `baseline_availability = "baseline_unavailable_v1"`.

Constants here MUST stay in lockstep with:
    src/qqq_scoring/scorer.py     — the 10 feature keys + ±8.0 clip
    src/qqq_scoring/beneish.py    — the 8 Beneish coefficients + sector thresholds
    explanations/compute_conviction.py  — the pillar arithmetic

If any of those change, update this module too. The helpers prefer to fail
loudly (KeyError on unknown feature) over silently emitting "Unknown" labels.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd


# ── Feature keys (the 10 statistical features used in z-scoring) ───────────────
# Must exactly match the order in src/qqq_scoring/scorer.py and the column
# suffixes self_z__*, peer_z__*, combined_z__* in quarterly_scores_detailed.

FEATURE_KEYS: list[str] = [
    "debt_to_assets",
    "equity_to_assets",
    "net_margin",
    "ocf_to_net_income",
    "ocf_to_assets",
    "accrual_ratio",
    "equity_multiplier",
    "revenue_growth_yoy",
    "assets_growth_yoy",
    "net_income_growth_yoy",
]

FEATURE_DISPLAY_NAMES: dict[str, str] = {
    "debt_to_assets":        "Debt / Assets",
    "equity_to_assets":      "Equity / Assets",
    "net_margin":            "Net Margin",
    "ocf_to_net_income":     "OCF / Net Income",
    "ocf_to_assets":         "OCF / Assets",
    "accrual_ratio":         "Accrual Ratio",
    "equity_multiplier":     "Equity Multiplier",
    "revenue_growth_yoy":    "Revenue Growth (YoY)",
    "assets_growth_yoy":     "Asset Growth (YoY)",
    "net_income_growth_yoy": "Net Income Growth (YoY)",
}

# Z-score clip ceiling — must match scorer.CLIP
Z_CLIP = 8.0


# ── Beneish components ────────────────────────────────────────────────────────
# Values must match _COEFF in src/qqq_scoring/beneish.py exactly.

BENEISH_COEFFICIENTS: dict[str, float] = {
    "dsri":  0.920,
    "gmi":   0.528,
    "aqi":   0.404,
    "sgi":   0.892,
    "depi":  0.115,
    "sgai": -0.172,
    "tata":  4.679,
    "lvgi": -0.327,
}
BENEISH_INTERCEPT = -4.84

BENEISH_DISPLAY_NAMES: dict[str, str] = {
    "dsri": "Days Sales in Receivables Index",
    "gmi":  "Gross Margin Index",
    "aqi":  "Asset Quality Index",
    "sgi":  "Sales Growth Index",
    "depi": "Depreciation Index",
    "sgai": "SG&A Expense Index",
    "tata": "Total Accruals to Total Assets",
    "lvgi": "Leverage Index",
}

# One-line plain-English interpretation per component. Keep these grounded —
# describe what the metric means and what direction is concerning. Do not
# editorialise about manipulation likelihood.
BENEISH_INTERPRETATIONS: dict[str, str] = {
    "dsri": (
        "Receivables growth relative to revenue growth. Values above 1.0 mean "
        "receivables are outpacing sales — classic signal of premature revenue "
        "recognition or channel stuffing."
    ),
    "gmi": (
        "Last year's gross margin divided by this year's. Values above 1.0 mean "
        "gross margin has deteriorated year-over-year — pressure that can "
        "incentivise earnings management."
    ),
    "aqi": (
        "Share of non-current, non-tangible assets (excluding goodwill and "
        "acquired intangibles per V5). Values above 1.0 mean management may be "
        "capitalising costs that should be expensed."
    ),
    "sgi": (
        "This year's revenue divided by last year's. Capped at 1.5 (50% YoY) "
        "to prevent hypergrowth (NVDA, AVGO) from dominating the M-Score via "
        "out-of-distribution extrapolation of the 1999 coefficient."
    ),
    "depi": (
        "Last year's depreciation rate divided by this year's. Values above 1.0 "
        "mean depreciation has slowed — potentially extending asset useful "
        "lives to inflate earnings."
    ),
    "sgai": (
        "SG&A as % of revenue, current divided by prior year. Values above 1.0 "
        "mean operating leverage is deteriorating. Coefficient is negative — "
        "rising SG&A intensity slightly REDUCES the M-Score."
    ),
    "tata": (
        "Total accruals (Net Income + SBC − OCF) divided by Total Assets. "
        "High positive values mean reported earnings are not backed by cash. "
        "SBC adjustment removes the structural false positive on high-SBC tech."
    ),
    "lvgi": (
        "Total liabilities / total assets, current divided by prior. Values "
        "above 1.0 mean leverage rose — debt covenant pressure can incentivise "
        "manipulation. Coefficient is negative — small de-emphasis effect."
    ),
}


# ── Sector-aware Beneish thresholds (must match beneish.py) ───────────────────

GROWTH_SECTORS = frozenset({
    "Information Technology",
    "Communication Services",
    "Health Care",
})
THRESHOLD_GROWTH = -1.5
THRESHOLD_TRADITIONAL = -2.22


# ── Conviction-score constants (must match compute_conviction.py) ─────────────

PILLAR_ANOMALY_WEIGHT      = 0.40
PILLAR_EARNINGS_MAX        = 35.0
BENEISH_M_CLEAN            = -6.0   # M ≤ this → 0 pts
BENEISH_M_DANGER           =  2.0   # M ≥ this → 35 pts
PILLAR_TRANSPARENCY_MAX_POS = 25.0  # CONTRADICTS @ confidence=1.0
PILLAR_TRANSPARENCY_MAX_NEG = -10.0 # CORROBORATES @ confidence=1.0


# ── Helpers: scalar coercion ──────────────────────────────────────────────────

def _f(v: Any) -> float | None:
    """Coerce to float, returning None for NaN/None/missing."""
    if v is None:
        return None
    try:
        if isinstance(v, float) and math.isnan(v):
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _i(v: Any) -> int | None:
    """Coerce to int, returning None for NaN/None/missing."""
    f = _f(v)
    return int(f) if f is not None else None


def _b(v: Any) -> bool | None:
    """Coerce to bool, preserving None."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    return bool(v)


def _s(v: Any) -> str | None:
    """Coerce to string, returning None for NaN/None/missing."""
    if v is None:
        return None
    if isinstance(v, float) and math.isnan(v):
        return None
    s = str(v)
    return s if s and s.lower() != "nan" else None


# ── Builders ──────────────────────────────────────────────────────────────────

def beneish_threshold_for_sector(sector: str | None) -> tuple[float, str]:
    """Return (threshold, human-readable rationale) for a given GICS sector."""
    if sector and sector in GROWTH_SECTORS:
        return (
            THRESHOLD_GROWTH,
            f"growth sector ({sector}) — uses tightened threshold {THRESHOLD_GROWTH} "
            f"because the 1999 Beneish formula structurally misfires on high-SBC, "
            f"hypergrowth, M&A-heavy businesses (per [EM-38])"
        )
    return (
        THRESHOLD_TRADITIONAL,
        f"traditional sector ({sector or 'Unknown'}) — uses Beneish 1999 paper "
        f"threshold {THRESHOLD_TRADITIONAL}"
    )


def build_features_array(row: pd.Series) -> list[dict]:
    """Build the per-feature breakdown for the statistical pillar.

    contribution_pct = (combined_z^2 / sum(combined_z^2)) * 100 — feature's
    share of the Mahalanobis distance signal. Sums to ~100 across all features
    with non-null combined z-scores.
    """
    # Collect combined z-scores once for contribution_pct denominator
    combined_zs: dict[str, float] = {}
    for feat in FEATURE_KEYS:
        cz = _f(row.get(f"combined_z__{feat}"))
        if cz is not None:
            combined_zs[feat] = cz
    z_sq_sum = sum(z * z for z in combined_zs.values()) or 1.0  # guard /0

    out: list[dict] = []
    for feat in FEATURE_KEYS:
        self_z     = _f(row.get(f"self_z__{feat}"))
        peer_z     = _f(row.get(f"peer_z__{feat}"))
        combined_z = combined_zs.get(feat)
        contribution_pct = (
            round((combined_z * combined_z) / z_sq_sum * 100, 2)
            if combined_z is not None else None
        )
        out.append({
            "name":             feat,
            "display_name":     FEATURE_DISPLAY_NAMES[feat],
            "self_z":           self_z,
            "self_median":      None,   # baseline_unavailable_v1
            "self_iqr":         None,   # baseline_unavailable_v1
            "peer_z":           peer_z,
            "peer_median":      None,   # baseline_unavailable_v1
            "peer_iqr":         None,   # baseline_unavailable_v1
            "combined_z":       combined_z,
            "z_clip_applied":   abs(combined_z) >= Z_CLIP if combined_z is not None else None,
            "contribution_pct": contribution_pct,
        })
    return out


def build_components_array(row: pd.Series) -> list[dict]:
    """Build the per-component breakdown for the Beneish pillar.

    contribution = coefficient × value (the additive term that flows into
    the M-Score sum). For missing components value/contribution are null
    AND the missing component name still appears (so the UI can render
    "DSRI: not available — insufficient prior-year data").
    """
    out: list[dict] = []
    for comp, coef in BENEISH_COEFFICIENTS.items():
        val = _f(row.get(f"beneish_{comp}"))
        contribution = round(coef * val, 4) if val is not None else None
        out.append({
            "name":           comp.upper(),
            "display_name":   BENEISH_DISPLAY_NAMES[comp],
            "value":          val,
            "coefficient":    coef,
            "contribution":   contribution,
            "interpretation": BENEISH_INTERPRETATIONS[comp],
            "available":      val is not None,
        })
    return out


def build_final_equation(row: pd.Series) -> str:
    """Render the conviction equation with substituted numeric values.

    Format: "Conviction X = clip(0,100) of [Pa (P1: ...) + Pe (P2: ...) + Pt (P3: ...)]"
    Verbose by design — leaves no arithmetic for the reader to verify.
    """
    cs = _f(row.get("conviction_score"))
    pa = _f(row.get("pillar_anomaly"))
    pe = _f(row.get("pillar_earnings"))
    pt = _f(row.get("pillar_transparency"))
    anomaly = _f(row.get("anomaly_score_0_100"))
    m_score = _f(row.get("beneish_m_score"))
    div_label = _s(row.get("divergence_label"))
    confidence = _f(row.get("confidence_score"))

    if cs is None:
        return "Conviction unavailable — missing inputs."

    p1_part = (
        f"{pa:+.1f} (P1: anomaly {anomaly:.1f}/100 × {PILLAR_ANOMALY_WEIGHT} = {pa:.1f}/40 max)"
        if pa is not None and anomaly is not None
        else f"{pa:+.1f} (P1: anomaly_score unavailable)"
    )

    if pe is not None and m_score is not None:
        p2_part = (
            f"{pe:+.1f} (P2: M={m_score:+.2f} mapped through "
            f"[{BENEISH_M_CLEAN:+.1f}, {BENEISH_M_DANGER:+.1f}] linear scale → "
            f"{pe:.1f}/{PILLAR_EARNINGS_MAX:.0f} max)"
        )
    else:
        p2_part = f"{pe or 0:+.1f} (P2: Beneish M-Score unavailable — insufficient components)"

    if pt is not None and div_label and confidence is not None:
        if div_label == "CONTRADICTS":
            p3_part = (
                f"{pt:+.1f} (P3: CONTRADICTS @ confidence {confidence:.2f} → "
                f"+{pt:.1f}/+{PILLAR_TRANSPARENCY_MAX_POS:.0f} max)"
            )
        elif div_label == "CORROBORATES":
            p3_part = (
                f"{pt:+.1f} (P3: CORROBORATES @ confidence {confidence:.2f} → "
                f"{pt:.1f}/{PILLAR_TRANSPARENCY_MAX_NEG:.0f} max negative)"
            )
        else:
            p3_part = f"{pt:+.1f} (P3: {div_label} @ confidence {confidence:.2f} → 0)"
    else:
        p3_part = "+0.0 (P3: narrative divergence not available)"

    return (
        f"Conviction {cs:.1f} = clip(0,100) of [{p1_part} + {p2_part} + {p3_part}]"
    )


def build_statistical_transform_note(row: pd.Series) -> str:
    """Explain the Mahalanobis → percentile → pillar mapping for this row."""
    d = _f(row.get("mahalanobis_distance"))
    pct = _f(row.get("anomaly_score_0_100"))
    pa = _f(row.get("pillar_anomaly"))
    if d is None or pct is None or pa is None:
        return "Statistical pillar inputs incomplete."
    return (
        f"Robust MCD Mahalanobis D² = {d:.2f} across {_i(row.get('num_features_used'))} features. "
        f"anomaly_score_0_100 = percentile rank of D² across the full universe of scored "
        f"filings (NOT an absolute manipulation probability) = {pct:.1f}. "
        f"Pillar contribution = anomaly_score × {PILLAR_ANOMALY_WEIGHT} = {pa:.1f}/40 max."
    )


def build_earnings_transform_note(row: pd.Series, threshold: float) -> str:
    """Explain the M-Score → 0–35 mapping + threshold decision for this row."""
    m = _f(row.get("beneish_m_score"))
    pe = _f(row.get("pillar_earnings"))
    flag = _b(row.get("beneish_manipulation_flag"))
    available = _i(row.get("beneish_components_available"))
    if m is None:
        return f"M-Score not computed ({available or 0} of 8 components available — minimum 5 required)."
    flag_str = "FLAGGED" if flag else "not flagged"
    rel = "above" if m > threshold else "at or below"
    return (
        f"M-Score = {m:+.3f} (using {available} of 8 components). "
        f"{rel} the {threshold} threshold → {flag_str}. "
        f"Pillar contribution = linear map of M from "
        f"[{BENEISH_M_CLEAN:+.1f}, {BENEISH_M_DANGER:+.1f}] onto [0, {PILLAR_EARNINGS_MAX:.0f}] = "
        f"{pe:.1f}/35 max."
    )


def build_narrative_transform_note(row: pd.Series) -> str:
    """Explain the divergence_label + confidence → −10..+25 mapping."""
    label = _s(row.get("divergence_label"))
    conf = _f(row.get("confidence_score"))
    pt = _f(row.get("pillar_transparency"))
    if not label:
        return "Narrative divergence not yet scored for this filing."
    if conf is None:
        conf = 0.5
    if label == "CONTRADICTS":
        return (
            f"Management's MD&A CONTRADICTS the anomaly signal at confidence {conf:.2f}. "
            f"Pillar contribution = confidence × +{PILLAR_TRANSPARENCY_MAX_POS:.0f} = +{pt:.1f}. "
            f"Concealment is the most-conviction-raising signal in the model."
        )
    if label == "CORROBORATES":
        return (
            f"Management's MD&A CORROBORATES the anomaly signal at confidence {conf:.2f}. "
            f"Pillar contribution = confidence × {PILLAR_TRANSPARENCY_MAX_NEG:.0f} = {pt:.1f}. "
            f"Acknowledged risk reduces conviction — known risk is less dangerous than concealed risk."
        )
    return f"Divergence label {label!r} carries no conviction adjustment (0 pts)."


def build_missing_components(row: pd.Series) -> list[str]:
    """List Beneish component names (uppercase) that were not computed for this row."""
    return [
        comp.upper()
        for comp in BENEISH_COEFFICIENTS
        if _f(row.get(f"beneish_{comp}")) is None
    ]
