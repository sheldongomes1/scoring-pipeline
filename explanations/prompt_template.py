"""Prompt builder for anomaly explanation generation.

Constructs the analyst-grade prompt sent to Claude for each anomalous filing.
Do not modify the prompt structure without explicit instruction — the format
is intentional and validated against analyst communication preferences.
"""

import pandas as pd

# Human-readable labels for each scoring feature
FEATURE_LABELS = {
    "debt_to_assets":        "Debt-to-Assets",
    "equity_to_assets":      "Equity-to-Assets",
    "net_margin":            "Net Profit Margin",
    "ocf_to_net_income":     "OCF-to-Net Income",
    "ocf_to_assets":         "OCF-to-Assets",
    "accrual_ratio":         "Accrual Ratio",
    "equity_multiplier":     "Equity Multiplier",
    "revenue_growth_yoy":    "Revenue Growth YoY",
    "assets_growth_yoy":     "Asset Growth YoY",
    "net_income_growth_yoy": "Net Income Growth YoY",
}

# Beneish component interpretations — hardcoded, not LLM-generated
BENEISH_INTERPRETATIONS = {
    "dsri":  ("DSRI",  lambda v: v > 1.0,  "Days sales receivable growing faster than revenue — possible aggressive revenue recognition"),
    "gmi":   ("GMI",   lambda v: v > 1.0,  "Gross margin deteriorating — cost pressures or pricing erosion"),
    "aqi":   ("AQI",   lambda v: v > 1.0,  "Asset quality declining — potential capitalisation of operating expenses"),
    "sgi":   ("SGI",   lambda v: v > 1.0,  "Sales growth elevated — high-growth companies have more incentive and opportunity to manipulate"),
    "depi":  ("DEPI",  lambda v: v > 1.0,  "Depreciation rate slowing — extending asset lives to inflate reported earnings"),
    "sgai":  ("SGAI",  lambda v: v < 1.0,  "SG&A declining relative to revenue — potential underinvestment in operations"),
    "tata":  ("TATA",  lambda v: v > 0.0,  "Total accruals to total assets positive — reported earnings exceed cash generation"),
    "lvgi":  ("LVGI",  lambda v: v > 1.0,  "Leverage increasing — rising debt burden heightens manipulation incentive"),
}

# Named patterns the model must choose from
PATTERN_CHOICES = [
    "Earnings Quality Risk",
    "Growth Bubble",
    "Financial Distress",
    "Aggressive Asset Expansion",
    "Recovery",
    "M&A Distortion",
    "Idiosyncratic",
]


def _top_features(row: pd.Series, n: int = 5) -> list[dict]:
    """Extract top N features by |combined_z| from a scored row."""
    features = []
    for feat, label in FEATURE_LABELS.items():
        cz = row.get(f"combined_z__{feat}")
        sz = row.get(f"self_z__{feat}")
        pz = row.get(f"peer_z__{feat}")
        if cz is None or (isinstance(cz, float) and pd.isna(cz)):
            continue
        features.append({
            "label":      label,
            "feature":    feat,
            "combined_z": round(float(cz), 2),
            "self_z":     round(float(sz), 2) if pd.notna(sz) else None,
            "peer_z":     round(float(pz), 2) if pd.notna(pz) else None,
            "direction":  "above baseline" if cz > 0 else "below baseline",
        })
    features.sort(key=lambda x: abs(x["combined_z"]), reverse=True)
    return features[:n]


def _beneish_drivers(row: pd.Series) -> list[str]:
    """Return human-readable lines for Beneish components that are flagged."""
    lines = []
    for col_key, (name, is_flagged_fn, interpretation) in BENEISH_INTERPRETATIONS.items():
        val = row.get(f"beneish_{col_key}")
        if val is None or (isinstance(val, float) and pd.isna(val)):
            continue
        if is_flagged_fn(float(val)):
            lines.append(f"  {name} = {float(val):.3f}: {interpretation}")
    return lines


def build_prompt(row: pd.Series) -> str:
    """Build the full Claude prompt for one anomalous filing row."""

    ticker         = row.get("ticker", "")
    sector         = row.get("gics_sector", "Unknown")
    quarter        = row.get("calendar_quarter", "")
    anomaly_pct    = row.get("anomaly_score_0_100", 0)
    m_score        = row.get("beneish_m_score")
    m_flagged      = row.get("beneish_manipulation_flag", False)
    alert_score    = row.get("alert_score", 0)
    top_feats      = _top_features(row)
    beneish_lines  = _beneish_drivers(row)

    # Build feature block
    feat_block = ""
    for f in top_feats:
        self_str = f"{f['self_z']:+.2f}" if f["self_z"] is not None else "n/a"
        peer_str = f"{f['peer_z']:+.2f}" if f["peer_z"] is not None else "n/a"
        feat_block += (
            f"- {f['label']}: combined z = {f['combined_z']:+.2f} ({f['direction']}). "
            f"Self-history z = {self_str}. Sector-peer z = {peer_str}.\n"
        )

    # Build Beneish block
    if m_score is not None and not (isinstance(m_score, float) and pd.isna(m_score)):
        beneish_flag_str = "YES — FLAGGED (M > -2.22)" if m_flagged else "No"
        beneish_block = f"M-Score: {float(m_score):.4f}. Manipulation flag: {beneish_flag_str}.\n"
        if beneish_lines:
            beneish_block += "Contributing components:\n" + "\n".join(beneish_lines)
        else:
            beneish_block += "No individual components above threshold."
    else:
        beneish_block = "Not available (insufficient raw data for this filing)."

    pattern_choices_str = " | ".join(PATTERN_CHOICES)

    prompt = f"""You are a senior equity research analyst writing an internal anomaly brief for your portfolio manager. You are direct, specific, and avoid filler language. Every sentence must either state a fact or a testable hypothesis.

## Company
{ticker} — {sector} sector — {quarter}
Anomaly score: {anomaly_pct:.1f}/100 (alert score: {alert_score})

## Flagged Metrics (sorted by severity)
{feat_block.strip()}

## Beneish M-Score (Earnings Manipulation Risk)
{beneish_block.strip()}

## Your Task

First, classify this anomaly profile. Choose exactly one pattern from this list:
{pattern_choices_str}

Then respond with a JSON object in this exact structure:
{{
  "pattern_name": "<one of the pattern choices above>",
  "pattern_confidence": "<high|medium|low>",
  "pattern_summary": "<single sentence — what is anomalous and why it matters, written for a portfolio manager scanning a list>",
  "explanation_brief": "<three paragraphs separated by double newline>\\n\\nParagraph 1 — What is happening: State the 2-3 most significant anomalies in plain English. Be specific about which metrics are anomalous, by how much, and in which direction. Reference whether this is unusual vs the company's own history (self-history z) or vs sector peers (peer z), or both.\\n\\nParagraph 2 — Why it matters: Generate 1-2 testable hypotheses about what could be driving these anomalies. Connect the quantitative flags to plausible business explanations. If Beneish is flagged, state which component is driving it and what that implies about earnings quality. Be direct about the risk.\\n\\nParagraph 3 — What to do next: Recommend 2-3 specific actions — e.g. compare receivables growth to revenue growth in the 10-Q footnotes, check the earnings call transcript for management commentary on a specific line item, or monitor whether this metric normalises next quarter or accelerates. Do not use bullet points. Do not use phrases like it is worth noting or it may be worth exploring."
}}

Return only the JSON object. No preamble, no explanation outside the JSON."""

    return prompt
