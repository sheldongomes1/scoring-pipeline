"""Z-score computation, MCD Mahalanobis distance, and anomaly scoring — Steps 3–6."""

import numpy as np
import pandas as pd
from sklearn.covariance import MinCovDet

# Z-score clip boundary: values reaching ±CLIP are labelled "far above/below baseline"
CLIP = 8.0


def to_calendar_quarter(dates: pd.Series) -> pd.Series:
    """Map report_date values to calendar quarter labels, e.g. '2026-Q1'.

    Groups by the calendar quarter the period-end falls in:
        Jan–Mar → Q1,  Apr–Jun → Q2,  Jul–Sep → Q3,  Oct–Dec → Q4

    This ensures companies with different fiscal year-ends but overlapping
    economic periods are compared as peers — e.g. a company with a Jan 31
    quarter-end and one with a Mar 31 quarter-end both land in Q1 and are
    scored against each other, as any equity analyst would expect.
    """
    dt = pd.to_datetime(dates)
    quarter = ((dt.dt.month - 1) // 3 + 1)
    return dt.dt.year.astype(str) + "-Q" + quarter.astype(str)


def _robust_zscore(series: pd.Series) -> pd.Series:
    """Robust z-score: (x - median) / (IQR / 1.35).

    Returns a NaN series when fewer than 2 valid observations exist.
    Returns zeros when IQR is zero (constant feature within this group).
    """
    valid = series.dropna()
    if len(valid) < 2:
        return pd.Series(np.nan, index=series.index, dtype=float)
    med = valid.median()
    iqr = float(valid.quantile(0.75) - valid.quantile(0.25))
    if iqr == 0:
        return pd.Series(np.zeros(len(series)), index=series.index)
    return (series - med) / (iqr / 1.35)


def self_history_zscores(df: pd.DataFrame, feature_keys: list[str]) -> pd.DataFrame:
    """Step 3: For each ticker, z-score each feature relative to its own history."""
    result = df[["ticker", "report_date"]].copy()
    for col in feature_keys:
        zname = f"zh_{col}"
        result[zname] = np.nan
        for _, grp in df.groupby("ticker"):
            z = _robust_zscore(grp[col])
            result.loc[z.index, zname] = z.values
    return result


def peer_zscores(
    df: pd.DataFrame,
    feature_keys: list[str],
    sector_col: str | None = "gics_sector",
    min_sector_peers: int = 5,
) -> pd.DataFrame:
    """Step 4: Peer-relative z-scores grouped by calendar quarter and GICS sector.

    Primary grouping: (calendar_quarter, gics_sector) — compares companies
    against peers in the same economic period AND same industry. A SaaS company
    with negative margins is normal; a retailer with negative margins is a crisis.
    Sector-adjusted scoring catches the difference.

    Fallback: when a sector group has fewer than min_sector_peers companies,
    falls back to calendar_quarter-only (universe-wide) grouping. This preserves
    coverage for small sectors (Utilities, Energy, Materials) that would otherwise
    never get peer scores.

    Groups by calendar quarter (Q1=Jan–Mar, Q2=Apr–Jun, Q3=Jul–Sep, Q4=Oct–Dec)
    rather than exact report_date — companies with fiscal quarters ending Jan 31,
    Feb 28, or Mar 31 all fall in Q1 and are compared as peers.
    """
    result = df[["ticker", "report_date"]].copy()
    cal_quarter = to_calendar_quarter(df["report_date"])
    result["calendar_quarter"] = cal_quarter

    use_sector = (
        sector_col is not None
        and sector_col in df.columns
        and df[sector_col].notna().any()
    )

    # Compute peer_count per row (size of the group actually used for z-scoring)
    result["peer_count"] = 0
    if use_sector:
        _sector_key = cal_quarter.astype(str) + "|" + df[sector_col].fillna("Unknown").astype(str)
        for _group_label, _grp in df.groupby(_sector_key):
            if len(_grp) >= min_sector_peers:
                result.loc[_grp.index, "peer_count"] = len(_grp)
            else:
                _quarter_label = _group_label.split("|")[0]
                _universe_grp = df[cal_quarter == _quarter_label]
                result.loc[_grp.index, "peer_count"] = len(_universe_grp)
    else:
        for _, _grp in df.groupby(cal_quarter):
            result.loc[_grp.index, "peer_count"] = len(_grp)

    for col in feature_keys:
        zname = f"zp_{col}"
        result[zname] = np.nan

        if use_sector:
            # Build sector-level peer group key
            sector_key = cal_quarter.astype(str) + "|" + df[sector_col].fillna("Unknown").astype(str)
            universe_key = cal_quarter

            for group_label, grp in df.groupby(sector_key):
                if len(grp) >= min_sector_peers:
                    # Enough sector peers — use sector-level z-scores
                    z = _robust_zscore(grp[col])
                else:
                    # Too few sector peers — fall back to universe (calendar quarter only)
                    quarter_label = group_label.split("|")[0]
                    universe_grp = df[cal_quarter == quarter_label]
                    z = _robust_zscore(universe_grp[col])
                    z = z.loc[z.index.isin(grp.index)]
                result.loc[z.index, zname] = z.values
        else:
            # No sector data — group by calendar quarter only
            for _, grp in df.groupby(cal_quarter):
                z = _robust_zscore(grp[col])
                result.loc[z.index, zname] = z.values

    return result


def combine_zscores(
    zh: pd.DataFrame,
    zp: pd.DataFrame,
    feature_keys: list[str],
    clip: float = CLIP,
) -> pd.DataFrame:
    """Step 5: Blend self-history and peer z-scores; use single source when the other is NaN.

    When both sources are available: combined = (self + peer) / 2, clipped to ±clip.
    When only one source is available: use it directly, clipped to ±clip.
    When neither is available: NaN.
    """
    result = zh[["ticker", "report_date"]].copy()
    for col in feature_keys:
        h = zh[f"zh_{col}"]
        p = zp[f"zp_{col}"]
        both = h.notna() & p.notna()
        only_h = h.notna() & p.isna()
        only_p = h.isna() & p.notna()

        combined = pd.Series(np.nan, index=h.index, dtype=float)
        combined[both] = ((h[both] + p[both]) / 2).clip(-clip, clip)
        combined[only_h] = h[only_h].clip(-clip, clip)
        combined[only_p] = p[only_p].clip(-clip, clip)
        result[f"z_{col}"] = combined
    return result


def anomaly_score(
    df: pd.DataFrame,
    feature_keys: list[str],
    support_fraction: float = 0.75,
    random_state: int = 42,
) -> pd.Series:
    """Step 6: Squared Mahalanobis distance (D²) via MinCovDet robust covariance estimator.

    Fits a MinCovDet estimator on the winsorized raw feature matrix and returns D² for
    each filing. NaN feature values are imputed with column medians before fitting.
    Falls back to squared L2 norm if the dataset is too small for MCD (n < p+1).

    Returns:
        pd.Series of D² values — stored as ``mahalanobis_distance`` in output schema.
    """
    X = df[feature_keys].copy()
    col_medians = X.median()
    X_filled = X.fillna(col_medians).to_numpy(dtype=float)
    n, p = X_filled.shape

    if n < p + 1:
        # Too few samples for MCD — fall back to squared L2 norm
        return pd.Series(np.sum(X_filled ** 2, axis=1), index=df.index)

    mcd = MinCovDet(support_fraction=support_fraction, random_state=random_state)
    mcd.fit(X_filled)
    sq_dist = mcd.mahalanobis(X_filled)  # sklearn returns D² (squared distances)
    return pd.Series(sq_dist, index=df.index)


def to_percentile_scores(distances: pd.Series) -> pd.Series:
    """Convert D² distances to percentile-based 0–100 anomaly scores.

    Score = fraction of all records outscored × 100.
    The highest distance gets 100.0; tied values share the same percentile.
    """
    return (distances.rank(pct=True) * 100).round(4)


def summary_scores(
    zh: pd.DataFrame,
    zp: pd.DataFrame,
    zdf: pd.DataFrame,
    feature_keys: list[str],
) -> pd.DataFrame:
    """Compute per-filing summary statistics from the three z-score DataFrames.

    Returns a DataFrame with:
        self_history_score       — mean |self-z| across non-NaN features (NaN if all NaN)
        peer_relative_score      — mean |peer-z| across non-NaN features (NaN if all NaN)
        combined_signal_strength — mean |combined-z| across non-NaN features (NaN if all NaN)
        num_features_used        — count of non-NaN combined z-scores
    """
    zh_cols = [f"zh_{c}" for c in feature_keys]
    zp_cols = [f"zp_{c}" for c in feature_keys]
    z_cols = [f"z_{c}" for c in feature_keys]

    result = pd.DataFrame(index=zdf.index)
    result["self_history_score"] = zh[zh_cols].abs().mean(axis=1, skipna=True)
    result["peer_relative_score"] = zp[zp_cols].abs().mean(axis=1, skipna=True)
    result["combined_signal_strength"] = zdf[z_cols].abs().mean(axis=1, skipna=True)
    result["num_features_used"] = zdf[z_cols].notna().sum(axis=1).astype(int)
    return result


def top_drivers(zdf: pd.DataFrame, feature_keys: list[str], n: int = 3) -> pd.DataFrame:
    """Top N features by absolute combined z-score for each filing.

    NaN z-scores are excluded from ranking. Unfilled driver slots are None/NaN.
    """
    z_cols = [f"z_{col}" for col in feature_keys]
    rows = []
    for _, row in zdf.iterrows():
        valid = row[z_cols].dropna()
        ranked = valid.abs().sort_values(ascending=False)
        d: dict = {}
        for i in range(1, n + 1):
            if i - 1 < len(ranked):
                col = ranked.index[i - 1]
                d[f"top_driver_{i}"] = col[2:]  # strip "z_" prefix
                d[f"top_driver_{i}_value"] = round(float(row[col]), 4)
            else:
                d[f"top_driver_{i}"] = None
                d[f"top_driver_{i}_value"] = None
        rows.append(d)
    return pd.DataFrame(rows, index=zdf.index)
