"""Review pack generation."""
from __future__ import annotations

import pandas as pd

_CLIP = 8.0  # Must match scorer.CLIP


def _driver_label(z: float, clip: float = _CLIP) -> str:
    """Return a human-readable label for a combined z-score value."""
    if abs(z) >= clip:
        direction = "far above" if z > 0 else "far below"
    elif z > 0:
        direction = "above"
    else:
        direction = "below"
    return f"{direction} baseline ({round(z, 4)})"


def build_driver_summary(scores_df: pd.DataFrame, n: int = 3) -> pd.Series:
    """Build a pipe-separated driver summary string for each row."""
    summaries = []
    for _, row in scores_df.iterrows():
        parts = []
        for i in range(1, n + 1):
            name = row.get(f"top_driver_{i}")
            value = row.get(f"top_driver_{i}_value")
            if pd.notna(name) and pd.notna(value):
                parts.append(f"{name}: {_driver_label(float(value))}")
        summaries.append(" | ".join(parts))
    return pd.Series(summaries, index=scores_df.index)


def build_review_pack(
    scores_df: pd.DataFrame,
    raw_df: pd.DataFrame,
    feature_keys: list[str],
    top_n: int = 20,
) -> pd.DataFrame:
    """Return the top N anomalies enriched with driver summaries and raw feature values.

    Args:
        scores_df:    quarterly_scores_detailed DataFrame (rich format with anomaly_score_0_100)
        raw_df:       period_features DataFrame (raw/winsorized feature values per filing)
        feature_keys: ordered list of feature column names
        top_n:        number of top anomalies to include
    """
    top = scores_df.nlargest(top_n, "anomaly_score_0_100").reset_index(drop=True)
    top["driver_summary"] = build_driver_summary(top)

    # Merge raw feature values by (ticker, report_date)
    raw_cols = raw_df.drop_duplicates(subset=["ticker", "report_date"])[
        ["ticker", "report_date"] + feature_keys
    ]
    top = top.merge(raw_cols, on=["ticker", "report_date"], how="left")

    # Review pack schema: no cik, form_type, model_type, z-score columns, scoring_version, scored_at
    base_cols = [
        "ticker", "company_name", "report_date", "filing_date", "filing_url",
        "anomaly_score_0_100", "mahalanobis_distance",
        "self_history_score", "peer_relative_score", "combined_signal_strength", "num_features_used",
        "top_driver_1", "top_driver_1_value",
        "top_driver_2", "top_driver_2_value",
        "top_driver_3", "top_driver_3_value",
        "driver_summary",
    ]
    cols = [c for c in base_cols + feature_keys if c in top.columns]
    return top[cols]
