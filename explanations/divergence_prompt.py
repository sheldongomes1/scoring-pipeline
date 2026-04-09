"""Prompt builder for narrative-quantitative divergence analysis.

Constructs the prompt sent to Claude for each anomalous filing.
Claude receives the full MD&A text + the quantitative anomaly profile
and returns a structured assessment of whether the narrative contradicts,
corroborates, or is neutral toward the numbers.

Do not modify the prompt structure without explicit instruction.
"""

import pandas as pd

from prompt_template import FEATURE_LABELS, _top_features

DIVERGENCE_LABELS = ["CONTRADICTS", "CORROBORATES", "NEUTRAL"]
MDA_TONES = ["BULLISH", "CAUTIOUS", "NEUTRAL", "MIXED"]


def build_divergence_prompt(row: pd.Series, mda_text: str) -> str:
    """Build the full Claude prompt for one filing's divergence analysis.

    Args:
        row: A scored filing row from quarterly_scores_detailed (includes
             z-scores, Beneish flags, anomaly score etc.)
        mda_text: The raw MD&A section text from the narrative JSON.
    """
    ticker      = row.get("ticker", "")
    sector      = row.get("gics_sector", "Unknown")
    quarter     = row.get("calendar_quarter", "")
    anomaly_pct = float(row.get("anomaly_score_0_100", 0))
    m_flagged   = bool(row.get("beneish_manipulation_flag", False))
    alert_score = row.get("alert_score", 0)
    top_feats   = _top_features(row, n=5)

    # Build the quantitative anomaly block
    feat_lines = []
    for f in top_feats:
        label     = f["label"]
        cz        = f["combined_z"]
        direction = f["direction"]
        self_str  = f"{f['self_z']:+.2f}" if f["self_z"] is not None else "n/a"
        peer_str  = f"{f['peer_z']:+.2f}" if f["peer_z"] is not None else "n/a"
        feat_lines.append(
            f"- {label}: combined z = {cz:+.2f} ({direction}). "
            f"Self-history z = {self_str}. Sector-peer z = {peer_str}."
        )
    feat_block = "\n".join(feat_lines) if feat_lines else "No flagged features."

    beneish_line = (
        "Beneish M-Score: FLAGGED — model indicates elevated earnings manipulation risk."
        if m_flagged else
        "Beneish M-Score: Not flagged."
    )

    label_choices = " | ".join(DIVERGENCE_LABELS)
    tone_choices  = " | ".join(MDA_TONES)

    prompt = f"""You are a senior equity research analyst performing a narrative-quantitative divergence analysis. Your job is to determine whether management's language in the MD&A is consistent with, or diverges from, what the financial data shows.

## Company
{ticker} — {sector} sector — {quarter}
Anomaly score: {anomaly_pct:.1f}/100 (alert score: {alert_score})

## Quantitative Anomaly Profile (what the numbers show)
{feat_block}

{beneish_line}

## MD&A Text (what management says)
{mda_text}

---

## Your Task

Assess whether the MD&A narrative CONTRADICTS, CORROBORATES, or is NEUTRAL toward the quantitative anomalies above.

Definitions:
- CONTRADICTS: Management's language is positive, reassuring, or evasive about areas where the numbers are deteriorating. The narrative creates an impression inconsistent with what the data shows. This is the high-signal case — an analyst red flag.
- CORROBORATES: Management explicitly acknowledges and discusses the anomalous metrics. The narrative is consistent with or confirms what the data shows.
- NEUTRAL: The MD&A does not meaningfully address the anomalous areas. No meaningful signal either way.

Rules for cited_passage:
- Must be a verbatim quote from the MD&A text above (copy-paste exact wording).
- Should be the sentence or phrase that most directly demonstrates the divergence_label you chose.
- If the label is NEUTRAL, cite the most relevant passage even if it is indirect.
- Maximum 2 sentences.

Respond with a JSON object in this exact structure:
{{
  "divergence_label": "<{label_choices}>",
  "confidence_score": <float between 0.0 and 1.0>,
  "cited_passage": "<verbatim quote from the MD&A above>",
  "rationale": "<2-3 sentences: what specific metric diverges, what management says vs what the numbers show, and why this matters to an analyst>",
  "mda_tone": "<{tone_choices}>",
  "anomaly_acknowledged": <true if management explicitly discussed the anomalous metric(s), false if they were silent or only mentioned them obliquely>
}}

Return only the JSON object. No preamble, no explanation outside the JSON."""

    return prompt
