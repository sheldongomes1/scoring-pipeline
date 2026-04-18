"""Prompt builder for the analyst actions pipeline.

Converts a single filing_intelligence row into the (system_prompt, user_prompt)
pair consumed by generate_analyst_actions.py.

Philosophy:
  RedInk analyses historical filings. The output describes what a competent
  analyst would investigate and why — it does NOT instruct the user to take
  action. Voice is descriptive/analytical (third person), never imperative.

Design:
  - System prompt is static and enforces the analytical-voice contract.
  - User prompt is per-row: two few-shot examples (written in analytical voice)
    followed by the actual filing data.
  - cited_passage is omitted entirely (not passed as "null") when missing.
"""

import pandas as pd

# ── Driver display map ─────────────────────────────────────────────────────────

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
reviewing SEC filings for a long/short hedge fund. You specialise in earnings
quality analysis, cash flow forensics, and detecting divergences between
management narrative and reported financials.

Your task: given a structured anomaly flag from a quantitative financial model,
produce a precise analyst briefing that describes what a competent analyst
would investigate in this filing, what specific question the filing must
answer, and what conditions would escalate or resolve the concern.

VOICE CONTRACT (non-negotiable):
You DESCRIBE what a competent analyst would investigate and why. You do NOT
instruct the reader. The output is analytical reasoning about historical
filings — it demonstrates what matters after detection, not a command to act.

  ✗ IMPERATIVE (never use):
      "Reconcile X against Y..."
      "Verify whether..."
      "Monitor the next filing..."
      "Read the Results of Operations section..."
      "Escalate to your senior analyst..."

  ✓ ANALYTICAL (always use):
      "Investigation focuses on reconciling X against Y..."
      "The pattern warrants examination of..."
      "An analyst reviewing this filing would focus on..."
      "The key analytical question is whether..."
      "This pattern would escalate if X persists in a subsequent filing."

GROUNDING RULES (non-negotiable):
1. Every claim must be traceable to a specific input field provided below.
   If you cannot point to the field, do not include the claim.
2. Never invent z-score values, percentages, dollar figures, or dates not given.
3. Never speculate about fraud, manipulation, or management intent. State only
   what the data shows. Use language like "the model flags", "the data shows",
   "the pattern indicates" — not "management is hiding" or "this looks like fraud."
4. Never reference knowledge about this company from your training data.
   Treat the input fields as your only source of truth for this filing.
5. Do not generate trading signals, price targets, or investment recommendations.
6. If divergence_label is CORROBORATES, frame the investigation as confirmation
   work (testing whether management's explanation fully accounts for the data),
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


# ── Few-shot examples (written in analytical voice) ────────────────────────────

FEW_SHOT_BLOCK = """\
Below are two worked examples. Study the input → output mapping before
processing the actual filing. Note the voice: the analyst describes what
matters; the analyst does not give the reader commands.

--- EXAMPLE 1 (CONTRADICTS, ALERT tier — forensic framing) ---
Input:
  Ticker: EXAMPLE_A | Quarter: 2023-Q2 | conviction_tier: ALERT | conviction_score: 81.2
  Pattern: Earnings Quality Risk | divergence_label: CONTRADICTS | divergence_confidence: 0.94
  anomaly_acknowledged: false
  Top drivers: OCF / Net Income (z = −5.2), Accrual Ratio (z = +4.1), Net Income Growth (YoY) (z = +3.8)
  Beneish flag: true | M-Score: −1.8
  Cited MD&A passage (verbatim from filing):
  "Our strong net income performance this quarter reflects improved operational
   efficiency, with cash generation expected to normalize in subsequent periods."

Expected output:
{
  "urgency_tier": "CRITICAL",
  "investigation_path": "Analysis focuses on reconciling the Cash Flow Statement's operating section against the income statement — the data shows OCF / Net Income at −5.2 standard deviations below baseline while net income grew materially, and the Beneish M-Score independently crosses the −1.78 manipulation threshold. The core analytical question is whether working capital movements or non-cash charges account for the full gap, and whether management's normalization claim is supported by specific disclosure.",
  "key_question": "Does the Cash Flow from Operations section provide a line-item reconciliation that fully explains why operating cash flow diverged from net income at this magnitude, or does the gap remain unaccounted for after reading the footnotes?",
  "persistence_test": "This pattern would escalate from ALERT to a confirmed earnings-quality concern if OCF / Net Income does not recover toward historical baseline in a subsequent filing, or if Accrual Ratio remains above +3.0 standard deviations — indicating the divergence is structural rather than a one-quarter anomaly.",
  "priority_section": "Cash Flows from Operations",
  "filing_section_rationale": "The OCF / Net Income divergence is the primary driver and is most directly evidenced in the operating activities section of the cash flow statement, not the income statement."
}

--- EXAMPLE 2 (CORROBORATES, FLAG tier — confirmatory framing) ---
Input:
  Ticker: EXAMPLE_B | Quarter: 2022-Q4 | conviction_tier: FLAG | conviction_score: 62.4
  Pattern: Revenue Acceleration | divergence_label: CORROBORATES | divergence_confidence: 0.87
  anomaly_acknowledged: true
  Top drivers: Revenue Growth (YoY) (z = +7.2), Net Margin (z = +4.1), Asset Growth (YoY) (z = +3.3)
  Beneish flag: false | M-Score: −3.2
  Cited MD&A passage (verbatim from filing):
  "Our AXON technology continues to demonstrate exceptional performance, driving
   significant improvements in advertiser ROI and platform monetization."

Expected output:
{
  "urgency_tier": "INVESTIGATE",
  "investigation_path": "The pattern warrants examination of whether management's cited explanation for Revenue Growth (YoY) at +7.2 standard deviations above baseline reflects a structural shift or a single-quarter acceleration. An analyst reviewing this filing would focus on segment-level revenue disclosure in the Results of Operations section and any guidance on the sustainability of margin expansion.",
  "key_question": "Does management quantify the revenue and margin contribution of the specific product or platform cited in the MD&A, or is the disclosure qualitative only?",
  "persistence_test": "If Revenue Growth (YoY) and Net Margin remain elevated in a subsequent filing and management continues to corroborate the driver in MD&A, the pattern strengthens to a confirmed inflection; Accrual Ratio and OCF / Net Income staying within normal range would provide additional confirmation that earnings quality supports the top-line growth.",
  "priority_section": "Results of Operations"
}

--- END EXAMPLES ---
"""


# ── JSON schema description ────────────────────────────────────────────────────

OUTPUT_SCHEMA = """\
Produce the following JSON object. Every field is required unless marked optional.
Emit fields in the exact order listed below. Follow the voice contract and grounding
rules exactly.

{
  "urgency_tier": string,
    // Emit this field FIRST. Describes concern level, not a command to act.
    // Assign exactly one of: "CRITICAL" | "INVESTIGATE" | "CONTEXTUAL"
    // Apply in order, first match wins:
    //   "CRITICAL"    → conviction_tier = ALERT AND divergence_label = CONTRADICTS
    //   "CRITICAL"    → conviction_tier = ALERT AND beneish_flag = true
    //   "INVESTIGATE" → conviction_tier = ALERT
    //   "INVESTIGATE" → conviction_tier = FLAG AND divergence_label = CONTRADICTS
    //   "INVESTIGATE" → conviction_tier = FLAG AND beneish_flag = true
    //   "CONTEXTUAL"  → all other cases

  "investigation_path": string,
    // What an analyst reviewing this filing would investigate and why.
    // Analytical voice — describe the investigation, do not issue commands.
    // Name the exact metric, financial statement section, or passage to examine.
    // Calibrate framing to divergence_label:
    //   CONTRADICTS  → forensic ("Analysis focuses on reconciling X against Y...")
    //   SILENT       → investigative ("Investigation centres on whether the filing discloses X...")
    //   CORROBORATES → confirmatory ("Examination tests whether the cited passage fully accounts for X...")
    // Use the human-readable driver label, not the field name.
    // Do not mention the company name — this is a generic analytical prompt.
    // 2–3 sentences max.

  "key_question": string,
    // The single specific, answerable question this filing must resolve.
    // Must be answerable YES/NO or with a number after reading the filing.
    // Not rhetorical. Not open-ended. Tied directly to the top driver or divergence.
    // Analytical voice — "Does the filing disclose..." / "Does management quantify..."
    // Max 2 sentences.

  "persistence_test": string,
    // The conditional test that would escalate, confirm, or resolve this pattern.
    // Describes a condition, not an action: "If X remains above Y in a subsequent
    // filing, the pattern would escalate from FLAG to ALERT."
    // Reference a specific metric (use human-readable label) and a threshold.
    // Do not instruct the reader to monitor — describe what the data would need
    // to show for the signal to strengthen or resolve.
    // Max 2 sentences.

  "priority_section": string,
    // The single most important named section of the 10-Q or 10-K that an
    // analyst would open first. Use standard SEC filing section names only:
    //   "Liquidity and Capital Resources", "Critical Accounting Estimates",
    //   "Results of Operations", "Cash Flows from Operations",
    //   "Notes to Financial Statements", "Management's Discussion and Analysis"
    // Derive from top_driver_1 and pattern_name. Do not invent note numbers.

  "filing_section_rationale": string   // OPTIONAL — omit if connection is direct
    // One sentence explaining why priority_section is the right place to start.
    // Only include if non-obvious.
}\
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _is_missing(val) -> bool:
    """True for None, NaN, pd.NA, NaT — safe on scalars of any type."""
    if val is None:
        return True
    try:
        return bool(pd.isna(val))
    except (TypeError, ValueError):
        return False


def _v(row: pd.Series, col: str, default: str = "null") -> str:
    """Return string value for a column, defaulting when null/NaN."""
    val = row.get(col)
    if _is_missing(val):
        return default
    return str(val)


def _driver_line(label_raw, z_val) -> str:
    """Format a single driver line with human-readable label."""
    if _is_missing(label_raw) or not str(label_raw).strip():
        return "null"
    label = DRIVER_LABELS.get(str(label_raw), str(label_raw))
    if _is_missing(z_val):
        return f"{label}  (z = null)"
    try:
        return f"{label}  (z = {float(z_val):+.2f})"
    except (TypeError, ValueError):
        return f"{label}  (z = {z_val})"


# ── Public API ────────────────────────────────────────────────────────────────

def build_analyst_actions_prompt(row: pd.Series) -> str:
    """Build the user prompt for a single filing_intelligence row."""

    cited = row.get("cited_passage")
    has_cited = not _is_missing(cited) and str(cited).strip()
    cited_block = (
        f'\n  Cited MD&A passage (verbatim from filing):\n  "{cited}"\n'
        if has_cited else ""
    )

    d1 = _driver_line(row.get("top_driver_1"), row.get("top_driver_1_value"))
    d2 = _driver_line(row.get("top_driver_2"), row.get("top_driver_2_value"))
    d3 = _driver_line(row.get("top_driver_3"), row.get("top_driver_3_value"))

    bf_raw = row.get("beneish_manipulation_flag")
    if _is_missing(bf_raw):
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
