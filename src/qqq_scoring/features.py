"""Feature selection and winsorizing — Steps 1 & 2."""

import json

import pandas as pd

_META_COLS = {"ticker", "form_type", "report_date", "filing_date", "accession_number", "filing_url"}


def discover_feature_keys(df: pd.DataFrame) -> list[str]:
    """Return numeric feature column names, excluding metadata columns."""
    return [
        c for c in df.columns
        if c not in _META_COLS and pd.api.types.is_numeric_dtype(df[c])
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
