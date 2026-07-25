"""Feature selection and winsorizing — Steps 1 & 2."""

import json

import pandas as pd

_META_COLS = {
    "ticker", "company_name", "cik",
    "form_type", "year", "feature_count",
    "report_date", "filing_date", "accession_number", "filing_url",
    "feature_flags_json", "gics_sector", "gics_sub_industry",
    "calendar_quarter",
}

# Column prefixes that are raw inputs for derived models (Beneish), not z-scored directly
_NON_SCORE_PREFIXES = ("b_curr_", "b_prior_")


def discover_feature_keys(df: pd.DataFrame) -> list[str]:
    """Return numeric feature column names eligible for z-scoring.

    Excludes metadata columns, non-numeric columns, and raw Beneish input
    columns (b_curr_* / b_prior_*) which are used to compute Beneish ratios
    downstream — not fed directly into the z-score pipeline.
    """
    return [
        c for c in df.columns
        if c not in _META_COLS
        and not any(c.startswith(p) for p in _NON_SCORE_PREFIXES)
        and pd.api.types.is_numeric_dtype(df[c])
    ]


def load_feature_keys(path: str) -> list[str]:
    with open(path) as f:
        return json.load(f)


def winsorize(df: pd.DataFrame, feature_keys: list[str], lower: float = 0.05, upper: float = 0.95) -> pd.DataFrame:
    """Clip each feature at the given percentiles across the full dataset."""
    df = df.copy()
    for col in feature_keys:
        lo = df[col].quantile(lower)
        hi = df[col].quantile(upper)
        df[col] = df[col].clip(lo, hi)
    return df
