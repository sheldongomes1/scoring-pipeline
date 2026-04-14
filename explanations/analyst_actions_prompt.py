"""Prompt builder for the analyst actions pipeline.

Converts a single filing_intelligence row into the (system_prompt, user_prompt)
pair consumed by generate_analyst_actions.py.

Design:
  - System prompt is static and describes the analyst persona + grounding rules.
  - User prompt is per-row: two few-shot examples (verbatim from spec) followed
    by the actual filing data formatted with the full template.
  - cited_passage is omitted entirely (not passed as "null") when missing —
    per spec anti-hallucination contract.
"""

import pandas as pd

# ── Driver display map ─────────────────────────────────────────────────────────
# Translates internal feature names to human-readable CFA labels.

DRIVER_LABELS: dict[str, str] = {
    "revenue_growth_yoy":    "Revenue Growth (YoY)",
    "assets_growth_yoy":     "Asset Growth (YoY)",
    "net_income_growth_yoy": "Net Income Growth (YoY)",
    "net_margin":            "Net Margin",
    "debt_to_assets":        "Debt / Assets",
    "equity_to_assets":      "Equity / Assets",
    "accrual_ratio":         "Accrual Ratio",
    "ocf_to_net_income":     "OCF / Net Income",
    "ocf_to_assets":         "OCF / Assets",
    "equity_multiplier":     "Equity Multiplier",
}


# ── System prompt ──────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are a senior equity analyst with CFA designation and 15 years of experience
reviewing SEC filings for a long/short hedge fund. You specialize in earnings
quality analysis, cash flow forensics, and detecting divergences between
management narrative and reported financials.

Your task: given a structured anomaly flag from a quantitative financial model,
produce a precise, actionable analyst briefing — three fields that tell a junior
analyst exactly what to do with this flag.

GROUNDING RULES (non-negotiable):
1. Every claim you make must be traceable to a specific input field provided below.
   If you cannot point to the field, do not include the claim.
2. Never invent z-score values, percentages, dollar figures, or dates not given.
3. Never speculate about fraud, manipulation, or management intent. State only
   what the data shows. Use language like "the model flags", "the data shows",
   "the pattern indicates" — not "management is hiding" or "this looks like fraud."
4. Never reference knowledge about this company from your training data.
   Treat the input fields as your only source of truth for this filing.
5. Do not generate trading signals, price targets, or investment recommendations.
6. If divergence_label is CORROBORATES, frame the action as confirmation work,
   not investigation work. Tone changes entirely — this is opportunity research,
   not forensic work.

DRIVER DISPLAY MAP (always use these human-readable labels):
  revenue_growth_yoy     → "Revenue Growth (YoY)"
  assets_growth_yoy      → "Asset Growth (YoY)"
  net_income_growth_yoy  → "Net Income Growth (YoY)"
  net_margin             → "Net Margin"
  debt_to_assets         → "Debt / Assets"
  equity_to_assets       → "Equity / Assets"
  accrual_ratio          → "Accrual Ratio"
  ocf_to_net_income      → "OCF / Net Income"

OUTPUT FORMAT: Return valid JSON only. No prose wrapper. No markdown fences.\
"""


# ── Few-shot examples ──────────────────────────────────────────────────────────
# Included verbatim in every user prompt to anchor output quality and tone.

FEW_SHOT_BLOCK = """\
Below are two worked examples. Study the input → output mapping before processing
the actual filing.

--- EXAMPLE 1 (CONTRADICTS, ALERT tier — forensic tone) ---
Input:
  Ticker: EXAMPLE_A | Quarter: 2023-Q2 | conviction_tier: ALERT | conviction_score: 81.2
  Pattern: Earnings Quality Risk | divergence_label: CONTRADICTS | divergence_confidence: 0.94
  anomaly_acknowledged: false
  Top drivers: OCF / Net Income (z = −5.2), Accrual Ratio (z = +4.1), Net Income Growth (YoY) (z = +3.8)
  Beneish flag: true | M-Score: −1.8
  Cited MD&A passage (verbatim from filing):
  "Our strong net income performance this quarter reflects improved operational efficiency,
   with cash generation expected to normalize in subsequent periods."

Expected output:
{
  "next_step": "Reconcile the Cash Flow Statement's operating section against the income statement — the model shows OCF / Net Income at a −5.2 standard deviation below baseline while net income grew materially, and the Beneish M-Score independently crosses the −1.78 manipulation threshold. Verify whether working capital movements or non-cash charges account for the full gap, and whether management's normalization claim is supported by specific disclosure.",
  "key_question": "Does the Cash Flow from Operations section provide a line-item reconciliation that fully explains why operating cash flow diverged from net income at this magnitude, or does the gap remain unaccounted for after reading the footnotes?",
  "watch_signal": "If OCF / Net Income does not recover toward historical baseline in the next filing, or if Accrual Ratio remains above +3.0 standard deviations, the earnings quality signal should be treated as persistent — not a one-quarter anomaly — and warrants escalation to your senior analyst.",
  "priority_section": "Cash Flows from Operations",
  "urgency_tier": "ACT NOW",
  "filing_section_rationale": "The OCF / Net Income divergence is the primary driver and is most directly evidenced in the operating activities section of the cash flow statement, not the income statement."
}

--- EXAMPLE 2 (CORROBORATES, FLAG tier — confirmatory tone) ---
Input:
  Ticker: EXAMPLE_B | Quarter: 2022-Q4 | conviction_tier: FLAG | conviction_score: 62.4
  Pattern: Revenue Acceleration | divergence_label: CORROBORATES | divergence_confidence: 0.87
  anomaly_acknowledged: true
  Top drivers: Revenue Growth (YoY) (z = +7.2), Net Margin (z = +4.1), Asset Growth (YoY) (z = +3.3)
  Beneish flag: false | M-Score: −3.2
  Cited MD&A passage (verbatim from filing):
  "Our AXON technology continues to demonstrate exceptional performance, driving significant
   improvements in advertiser ROI and platform monetization."

Expected output:
{
  "next_step": "Verify whether management's cited explanation for Revenue Growth (YoY) at +7.2 standard deviations above baseline is a structural shift or a one-quarter acceleration — read the Results of Operations section for segment-level revenue disclosure and any guidance on sustainability of the margin expansion.",
  "key_question": "Does management quantify the revenue and margin contribution of the specific product or platform cited in the MD&A, or is the disclosure qualitative only?",
  "watch_signal": "If Revenue Growth (YoY) and Net Margin remain elevated in the next filing and management continues to corroborate the driver in MD&A, this pattern strengthens to a confirmed inflection — monitor whether the Accrual Ratio and OCF / Net Income remain within normal range as confirmation that earnings quality supports the top-line growth.",
  "priority_section": "Results of Operations",
  "urgency_tier": "INVESTIGATE"
}

--- END EXAMPLES ---
"""


# ── JSON schema description ────────────────────────────────────────────────────

OUTPUT_SCHEMA = """\
Produce the following JSON object. Every field is required unless marked optional.
Follow the grounding rules exactly.

{
  "next_step": string,
    // One imperative sentence. CFA-level precision.
    // Name the exact metric, financial statement section, or passage to verify.
    // Calibrate tone to divergence_label:
    //   CONTRADICTS → forensic: "Reconcile X against Y..."
    //   SILENT      → investigative: "Search the filing for any disclosure on X..."
    //   CORROBORATES → confirmatory: "Verify whether the cited passage fully accounts for X..."
    // Use the human-readable driver label, not the field name.
    // Do not mention the company name — this is a generic action prompt.
    // Max 2 sentences.

  "key_question": string,
    // One specific, answerable question the analyst must resolve.
    // Must be answerable YES/NO or with a number after reading the filing.
    // Not rhetorical. Not open-ended. Tied directly to the top driver or divergence.
    // Max 2 sentences.

  "watch_signal": string,
    // One forward-looking tripwire: what to monitor in the next quarterly filing.
    // Must reference a specific metric (use human-readable label) and a threshold.
    // State what escalation means — does this stay FLAG, upgrade to ALERT, or resolve?
    // Do not predict what will happen. State conditions: "If X persists, then Y."
    // Max 2 sentences.

  "priority_section": string,
    // The single most important named section of the 10-Q or 10-K to open first.
    // Use standard SEC filing section names only:
    //   "Liquidity and Capital Resources", "Critical Accounting Estimates",
    //   "Results of Operations", "Cash Flows from Operations",
    //   "Notes to Financial Statements", "Management's Discussion and Analysis"
    // Derive from top_driver_1 and pattern_name. Do not invent note numbers.

  "urgency_tier": string,
    // Assign exactly one of: "ACT NOW" | "INVESTIGATE" | "MONITOR"
    // Apply in order, first match wins:
    //   "ACT NOW"     → conviction_tier = ALERT AND divergence_label = CONTRADICTS
    //   "ACT NOW"     → conviction_tier = ALERT AND beneish_flag = true
    //   "INVESTIGATE" → conviction_tier = ALERT
    //   "INVESTIGATE" → conviction_tier = FLAG AND divergence_label = CONTRADICTS
    //   "INVESTIGATE" → conviction_tier = FLAG AND beneish_flag = true
    //   "MONITOR"     → all other cases

  "filing_section_rationale": string   // OPTIONAL — omit if connection is direct
    // One sentence explaining why priority_section is the right place to start.
    // Only include if non-obvious.
}\
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _v(row: pd.Series, col: str, default: str = "null") -> str:
    """Return string value for a column, defaulting when null/NaN."""
    val = row.get(col)
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return default
    return str(val)


def _driver_line(label_raw: str | None, z_val: str | float | None) -> str:
    """Format a single driver line with human-readable label."""
    if not label_raw or label_raw == "null":
        return "null"
    label = DRIVER_LABELS.get(str(label_raw), str(label_raw))
    if z_val is None or (isinstance(z_val, float) and pd.isna(z_val)):
        return f"{label}  (z = null)"
    try:
        return f"{label}  (z = {float(z_val):+.2f})"
    except (TypeError, ValueError):
        return f"{label}  (z = {z_val})"


# ── Public API ────────────────────────────────────────────────────────────────

def build_analyst_actions_prompt(row: pd.Series) -> str:
    """Build the user prompt for a single filing_intelligence row.

    Returns the full user prompt string (system prompt is separate).
    Omits cited_passage entirely when null — never passes "null" as content.
    """

    # Cited passage — omit block entirely if missing
    cited = row.get("cited_passage")
    has_cited = (
        cited is not None
        and not (isinstance(cited, float) and pd.isna(cited))
        and str(cited).strip()
    )
    cited_block = (
        f'\n  Cited MD&A passage (verbatim from filing):\n  "{cited}"\n'
        if has_cited else ""
    )

    # Driver lines
    d1 = _driver_line(row.get("top_driver_1"), row.get("top_driver_1_value"))
    d2 = _driver_line(row.get("top_driver_2"), row.get("top_driver_2_value"))
    d3 = _driver_line(row.get("top_driver_3"), row.get("top_driver_3_value"))

    # Beneish flag — normalise to true/false/null
    bf_raw = row.get("beneish_manipulation_flag")
    if bf_raw is None or (isinstance(bf_raw, float) and pd.isna(bf_raw)):
        beneish_flag_str = "null"
    else:
        beneish_flag_str = "true" if bool(bf_raw) else "false"

    filing_block = f"""\
Anomaly flag input:

  Ticker:               {_v(row, "ticker")}
  Company:              {_v(row, "company_name")}
  Quarter:              {_v(row, "calendar_quarter")}
  Filing type:          {_v(row, "form_type")}

  Conviction tier:      {_v(row, "conviction_tier")}
  Conviction score:     {_v(row, "conviction_score")}
  Anomaly score:        {_v(row, "anomaly_score_0_100")}

  Pattern name:         {_v(row, "pattern_name")}
  Divergence label:     {_v(row, "divergence_label")}
  Divergence confidence:{_v(row, "divergence_confidence")}
  Anomaly acknowledged: {_v(row, "anomaly_acknowledged")}

  Top driver 1:         {d1}
  Top driver 2:         {d2}
  Top driver 3:         {d3}

  Pillar scores (out of 40 / 35 / 25):
    Statistical anomaly:    {_v(row, "pillar_anomaly")}
    Earnings quality:       {_v(row, "pillar_earnings")}
    Narrative transparency: {_v(row, "pillar_transparency")}

  Beneish M-Score:      {_v(row, "beneish_m_score")}
  Beneish flag:         {beneish_flag_str}

  Explanation brief:
  {_v(row, "explanation_brief")}{cited_block}"""

    return "\n\n".join([FEW_SHOT_BLOCK, filing_block, OUTPUT_SCHEMA])
