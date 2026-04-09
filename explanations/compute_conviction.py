#!/usr/bin/env python3
"""Compute the three-pillar conviction score for all scored filings.

Joins quarterly_scores_detailed (anomaly + Beneish) with narrative_divergence
(management transparency) and produces a single conviction_score (0-100) and
conviction_tier (WATCH / FLAG / ALERT) per filing.

The three pillars are built on completely independent data and methodology:

  Pillar 1 — Statistical Anomaly  (0-40 pts)
    Source: MCD Mahalanobis anomaly_score_0_100
    Question: How statistically unusual is this company's financial profile?

  Pillar 2 — Earnings Quality     (0-35 pts)
    Source: Continuous Beneish M-Score (not just the binary flag)
    Question: Is there evidence of earnings engineering?

  Pillar 3 — Management Transparency  (-10 to +25 pts)
    Source: narrative_divergence divergence_label + confidence_score
    Question: Is management acknowledging the anomaly or concealing it?
    CONTRADICTS adds up to +25 (concealment = higher conviction)
    CORROBORATES subtracts up to -10 (acknowledged = lower conviction)
    NEUTRAL / missing = 0

When all three pillars fire simultaneously (ALERT tier), three independent
systems built on different data sources agree — probability of a false positive
is very low.

Usage:
    python explanations/compute_conviction.py
    python explanations/compute_conviction.py --dry-run   # print stats, don't upload
"""

import argparse
import io
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT        = "qqq-anomaly-lab"
BQ_DATASET        = "qqq_finance"
SCORES_TABLE      = f"{BQ_PROJECT}.{BQ_DATASET}.quarterly_scores_detailed"
DIVERGENCE_TABLE  = f"{BQ_PROJECT}.{BQ_DATASET}.narrative_divergence"
OUTPUT_TABLE      = f"{BQ_PROJECT}.{BQ_DATASET}.conviction_scores"

# Pillar 2 — Beneish M-Score normalization range
# M = -6.0 → very clean (0 risk)
# M = -2.22 → manipulation threshold (~47% risk → ~16.5 pts)
# M ≥  2.0 → maximum risk (35 pts)
BENEISH_M_CLEAN  = -6.0
BENEISH_M_DANGER =  2.0

# Conviction tier thresholds
TIER_ALERT = 65   # all three pillars firing, or two firing strongly
TIER_FLAG  = 40   # two pillars contributing meaningfully
TIER_WATCH = 20   # one meaningful signal


# ── Pillar computations ───────────────────────────────────────────────────────

def pillar_anomaly(anomaly_score: float) -> float:
    """Pillar 1: Statistical anomaly (0-40 pts).

    Linear scale of anomaly_score_0_100. A company at the 100th percentile
    of Mahalanobis distance contributes the full 40 points.
    """
    if pd.isna(anomaly_score):
        return 0.0
    return float(anomaly_score) * 0.40


def pillar_earnings(beneish_m_score) -> float:
    """Pillar 2: Earnings quality via continuous Beneish M-Score (0-35 pts).

    Uses the continuous M-Score rather than the binary flag so that a company
    at M = -2.23 (barely flagged) scores very differently from M = +1.5
    (deeply in manipulation territory).

    Normalized between BENEISH_M_CLEAN (-6.0) and BENEISH_M_DANGER (2.0):
      M ≤ -6.0  →  0 pts  (very clean)
      M = -2.22 →  ~16.5 pts (at manipulation threshold)
      M ≥  2.0  →  35 pts (maximum concern)

    Returns 0 when M-Score is unavailable (insufficient raw data).
    """
    if pd.isna(beneish_m_score):
        return 0.0
    risk = (float(beneish_m_score) - BENEISH_M_CLEAN) / (BENEISH_M_DANGER - BENEISH_M_CLEAN)
    risk = max(0.0, min(1.0, risk))
    return risk * 35.0


def pillar_transparency(divergence_label, confidence_score) -> float:
    """Pillar 3: Management transparency (-10 to +25 pts).

    CONTRADICTS: management is upbeat/evasive about deteriorating metrics.
                 Most dangerous case — adds up to +25 pts (scaled by confidence).
    CORROBORATES: management explicitly acknowledged the anomaly.
                  Reduces conviction — subtracts up to 10 pts.
                  Acknowledged risk is less dangerous than concealed risk.
    NEUTRAL / missing: no information either way — 0 pts.
    """
    if pd.isna(divergence_label) or divergence_label is None:
        return 0.0
    conf = float(confidence_score) if pd.notna(confidence_score) else 0.5
    if divergence_label == "CONTRADICTS":
        return conf * 25.0
    elif divergence_label == "CORROBORATES":
        return -conf * 10.0
    else:  # NEUTRAL
        return 0.0


def assign_tier(score: float) -> str | None:
    """Assign a conviction tier label based on total score."""
    if score >= TIER_ALERT:
        return "ALERT"
    elif score >= TIER_FLAG:
        return "FLAG"
    elif score >= TIER_WATCH:
        return "WATCH"
    return None


# ── BigQuery helpers ──────────────────────────────────────────────────────────

def load_scores(client: bigquery.Client) -> pd.DataFrame:
    print(f"Loading scores from {SCORES_TABLE}...")
    df = client.query(f"""
        SELECT
            ticker, report_date, calendar_quarter, gics_sector,
            anomaly_score_0_100, beneish_m_score, beneish_manipulation_flag,
            scoring_version, scored_at
        FROM `{SCORES_TABLE}`
    """).to_dataframe()
    print(f"  {len(df)} rows, {df['ticker'].nunique()} tickers")
    return df


def load_divergence(client: bigquery.Client) -> pd.DataFrame:
    print(f"Loading narrative divergence from {DIVERGENCE_TABLE}...")
    try:
        df = client.query(f"""
            SELECT
                ticker, report_date,
                divergence_label, confidence_score, mda_tone, anomaly_acknowledged
            FROM `{DIVERGENCE_TABLE}`
        """).to_dataframe()
        print(f"  {len(df)} rows")
        return df
    except Exception as e:
        print(f"  narrative_divergence table not found or empty: {e}")
        return pd.DataFrame(columns=["ticker", "report_date", "divergence_label",
                                     "confidence_score", "mda_tone", "anomaly_acknowledged"])


def upload_conviction(client: bigquery.Client, df: pd.DataFrame) -> None:
    upload_df = df.copy()
    upload_df["report_date"] = pd.to_datetime(upload_df["report_date"], utc=True).dt.date

    buf = io.BytesIO()
    upload_df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field="report_date",
        ),
        clustering_fields=["ticker", "conviction_tier"],
    )

    job = client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config)
    job.result()
    print(f"Uploaded {len(upload_df)} rows → {OUTPUT_TABLE}")


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Compute conviction scores")
    parser.add_argument("--dry-run", action="store_true", help="Print stats without uploading to BQ")
    args = parser.parse_args()

    client = bigquery.Client(project=BQ_PROJECT)

    # Load both tables
    scores = load_scores(client)
    divergence = load_divergence(client)

    # Normalise report_date for joining (both to date string YYYY-MM-DD)
    scores["report_date_key"] = pd.to_datetime(scores["report_date"]).dt.strftime("%Y-%m-%d")
    divergence["report_date_key"] = pd.to_datetime(divergence["report_date"]).dt.strftime("%Y-%m-%d")

    # Left join — filings without narrative divergence get pillar_3 = 0
    merged = scores.merge(
        divergence[["ticker", "report_date_key", "divergence_label",
                    "confidence_score", "mda_tone", "anomaly_acknowledged"]],
        on=["ticker", "report_date_key"],
        how="left",
    )
    narrative_coverage = merged["divergence_label"].notna().sum()
    print(f"\nNarrative coverage: {narrative_coverage}/{len(merged)} filings "
          f"({100*narrative_coverage/len(merged):.1f}%)")

    # Compute pillars
    merged["pillar_anomaly"] = merged["anomaly_score_0_100"].apply(pillar_anomaly)
    merged["pillar_earnings"] = merged["beneish_m_score"].apply(pillar_earnings)
    merged["pillar_transparency"] = merged.apply(
        lambda r: pillar_transparency(r["divergence_label"], r["confidence_score"]), axis=1
    )

    # Conviction score — sum of pillars, clamped to [0, 100]
    merged["conviction_score"] = (
        merged["pillar_anomaly"] +
        merged["pillar_earnings"] +
        merged["pillar_transparency"]
    ).clip(0, 100).round(2)

    # Conviction tier
    merged["conviction_tier"] = merged["conviction_score"].apply(assign_tier)

    # Assemble output
    out = merged[[
        "ticker", "report_date", "calendar_quarter", "gics_sector",
        "anomaly_score_0_100", "beneish_m_score", "beneish_manipulation_flag",
        "divergence_label", "confidence_score", "mda_tone", "anomaly_acknowledged",
        "pillar_anomaly", "pillar_earnings", "pillar_transparency",
        "conviction_score", "conviction_tier",
        "scoring_version",
    ]].copy()
    out["computed_at"] = datetime.now(timezone.utc).isoformat()
    out = out.sort_values("conviction_score", ascending=False).reset_index(drop=True)

    # Print summary
    print("\n── Conviction tier distribution ──")
    tier_counts = out["conviction_tier"].value_counts(dropna=False)
    for tier in ["ALERT", "FLAG", "WATCH", None]:
        label = tier if tier else "(none)"
        count = tier_counts.get(tier, 0)
        print(f"  {label:8s}: {count:4d} filings")

    print("\n── Top 20 by conviction score ──")
    cols = ["ticker", "calendar_quarter", "conviction_score", "conviction_tier",
            "pillar_anomaly", "pillar_earnings", "pillar_transparency", "divergence_label"]
    print(out[cols].head(20).to_string(index=False))

    print("\n── ALERT filings ──")
    alerts = out[out["conviction_tier"] == "ALERT"][cols]
    if len(alerts):
        print(alerts.to_string(index=False))
    else:
        print("  None yet — run narrative divergence batch first to unlock Pillar 3.")

    if args.dry_run:
        print("\nDry run — not uploading.")
        return

    print(f"\nUploading conviction scores to BigQuery...")
    upload_conviction(client, out)
    print("Done.")


if __name__ == "__main__":
    main()
