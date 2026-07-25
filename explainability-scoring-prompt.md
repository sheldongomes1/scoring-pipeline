# Score Explanation — superseded by shipped implementation (2026-04-19)

> This file was the original design prompt for the `score_explanation` table.
> The implementation has shipped; this document is preserved as a pointer to
> the authoritative source-of-truth files.

## Where the live spec lives now

| What you want to know | Source of truth |
|---|---|
| BigQuery schema for `score_explanation` | `explanations/compute_score_explanation.py` → `build_schema()` |
| Population logic (joins, row builders) | `explanations/compute_score_explanation.py` → `build_row()` |
| Display names, Beneish interpretations, transform notes | `explanations/score_explanation_helpers.py` |
| Pipeline integration | `scripts/orchestrate.py` → STEPS list, entry `num=9` |
| Freshness verification | `scripts/verify_freshness.py` → `PIPELINE_TABLES` |
| Authoritative scoring methodology | `src/qqq_scoring/scorer.py`, `src/qqq_scoring/beneish.py`, `explanations/compute_conviction.py` |

## How the implementation differs from the original prompt

The discovery pass against ground-truth code surfaced four places where the
original spec assumed mechanics that don't exist in production. The shipped
implementation matches reality:

| Original prompt | Shipped reality |
|---|---|
| `pillar_weights STRUCT<statistical, earnings, narrative>` (multiplicative weights) | `pillar_contributions STRUCT<statistical, earnings, narrative>` (additive point contributions; narrative can be negative) |
| `self_mean`, `self_sd`, `peer_mad` | `self_median`, `self_iqr`, `peer_median`, `peer_iqr` (robust statistics, per `scorer._robust_zscore`) |
| `100 * (1 - exp(-d/k))` Mahalanobis transform | **percentile rank** of D² across the universe (per `scorer.to_percentile_scores`) |
| WBD Q2 2022 as golden test (Conviction ~100, Net Margin z≈−6.0) | **PLTR 2024-Q2** as golden test (Conviction 65.79 ALERT, all three pillars positive, exercises growth-sector Beneish threshold and missing-LVGI path) |
| Single fixed Beneish threshold | **Sector-aware**: −1.5 for Information Technology / Communication Services / Health Care; −2.22 for traditional sectors (per `beneish.MANIPULATION_THRESHOLD_GROWTH` + [EM-38] in linkedin.md) |
| `model_version = "brick3_q_v2_robust_mahalanobis_clipped"` | `model_version = "brick3_q_v5_beneish"` (current production after V5 correctness pass) |

## Phase 1 vs Phase 2 (still applies)

**Phase 1 — shipped 2026-04-19:** everything derivable from already-persisted
BigQuery tables. The `self_median`, `self_iqr`, `peer_median`, `peer_iqr`,
and per-feature `winsorization_caps` fields render as null and rows carry
`statistical_pillar.baseline_availability = "baseline_unavailable_v1"`.

**Phase 2 — backlogged:** add hooks to `scorer.py` to persist the medians,
IQRs, and winsorization caps during the scoring run, then bump
`explanation_version` to `"v2"`. Tracked in
`~/.claude/projects/-home-sheldongomes-AIProjects-scoring-pipeline/memory/backlog.md`.

## How the UI should consume this

```sql
SELECT
  ticker, calendar_quarter, conviction_score, conviction_tier,
  final_equation,
  pillar_contributions.statistical AS p1_pts,
  pillar_contributions.earnings    AS p2_pts,
  pillar_contributions.narrative   AS p3_pts,
  statistical_pillar,
  earnings_pillar,
  narrative_pillar
FROM `qqq-anomaly-lab.qqq_finance.score_explanation`
WHERE ticker = ? AND calendar_quarter = ?
```

`final_equation` is a fully-substituted human-readable string ready to render
verbatim in the modal header. The three nested pillar STRUCTs hold the
machine-readable detail; null pillars (`earnings_pillar IS NULL` when Beneish
couldn't compute, `narrative_pillar IS NULL` before the LLM has scored
divergence) should render as "data not available — [reason]" cards rather
than zero values.
