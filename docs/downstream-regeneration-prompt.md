# Downstream Regeneration Prompt

> Paste this verbatim into a fresh Claude session inside `linkedin-coach/` (and again inside `redink-presentation/` — the rules are identical, only the artifacts being audited differ). Phased so Claude stops and waits for review before doing the expensive regeneration.

---

# CONSTITUTION UPDATE + REGENERATION — read fully before doing anything

You are Claude operating inside this project (`linkedin-coach/` or `redink-presentation/`). A serious data-integrity failure has been identified that affects every artifact in this folder that cites a number, a ticker, a z-score, an anomaly score, a divergence judgment, or any specific case study. We are going to (1) fix the root cause in `CLAUDE.md`, (2) audit what is already on disk so we know how bad it is, and (3) regenerate from the single source of truth.

**Stop and confirm with the user before moving from one phase to the next.** Do not attempt to do all four phases in one go. The audit phase exists so the user can scope the regeneration before it runs.

---

## 0 · What broke — the concrete failure that triggered this

A LinkedIn post (post-01-v3, WBD Q2 2022 case study) made the following quantitative and qualitative claims:

- Anomaly score 100/100
- Self-history z 4.29, peer-relative z 3.85
- Revenue and asset growth "+8.0 cap"
- Net margin z −5.99
- "8 of 8 features contributing"
- "WBD's management discussed integration milestones and subscriber targets in the MD&A. The −34.8% net margin received no coverage in the narrative."

The actual scoring-pipeline output for `(WBD, 2022-06-30)` says:

- Anomaly score **75.75**, not 100
- Self-history score **1.25**, peer-relative **2.04** (not 4.29 / 3.85)
- Top drivers: assets_growth_yoy **+6.19**, revenue_growth_yoy **+3.88**, net_income_growth_yoy **−1.49** — net_margin is **not** in the top three drivers, and neither growth feature is at the +8.0 cap
- 10 features used, 9 peers
- BigQuery `narrative_divergence`: divergence_label = **CORROBORATES**, confidence 0.82, **anomaly_acknowledged = TRUE**, rationale: *"Management explicitly and repeatedly acknowledges the WarnerMedia merger as the driver of massive asset inflation… the sharp deterioration in net income (GAAP net loss of $3.4B in Q2)…"*
- BigQuery `anomaly_explanations`: pattern = **M&A Distortion**, brief attributes margin compression to "transaction fees, restructuring charges, purchase accounting amortization of intangibles"

**Every quantitative claim in the post was fabricated. The qualitative claim is the exact opposite of what the pipeline says.** The fabrication did not originate in the scoring-pipeline — its outputs are correct. It originated in this project, where evidence files (e.g. `evidence-bank.md` entry EB-51) generated plausible-looking numbers from prose context instead of reading the actual scoring CSV / BigQuery tables. Posts then propagated those fabricated numbers and contradicted the pipeline's own divergence judgment.

This is not a one-off. Any artifact in this folder that names a ticker + numeric scoring fact + qualitative narrative claim must be assumed wrong until verified.

---

## 1 · The new constitution — add this verbatim to `CLAUDE.md`

Add a top-level section titled **"Source-of-Truth Discipline"** to this project's `CLAUDE.md` (above any phase or content sections so it cannot be skipped). Insert these rules verbatim:

```markdown
## Source-of-Truth Discipline (MANDATORY — overrides all other content rules)

The QQQ scoring-pipeline is the single source of truth for every quantitative
fact and every narrative judgment about a filing. This project is a CONSUMER
of that pipeline, never a parallel producer.

### Hard rules

1. **No local copies of pipeline data.** Do not write CSVs, JSON, or markdown
   tables that duplicate fields from `quarterly_scores_detailed.csv`,
   `top_anomaly_review_pack.csv`, or any BigQuery table in `qqq_finance`.
   If a fact is in the pipeline, reference it by `(ticker, report_date)` and
   pull at use-time. Cached snapshots rot and lie.

2. **Every quantitative claim must trace to a source row.** Anomaly score,
   z-score, peer count, driver feature, conviction tier, divergence label,
   Beneish M-Score, etc. — each must point to a specific row in
   `quarterly_scores_detailed.csv` or a `qqq_finance.*` table, identified by
   `ticker + report_date`. If you cannot point to the row, you cannot make
   the claim.

3. **Qualitative framing must match the pipeline's structured judgment.**
   - If `divergence_label = CORROBORATES`, do not write that "management hid"
     or "did not address" the anomaly. The pipeline says they did.
   - If `anomaly_acknowledged = TRUE`, the post angle "narrative ignores the
     anomaly" is invalid for that filing. Pick a different filing.
   - If `pattern_name = M&A Distortion`, do not frame it as fraud or earnings
     manipulation; the pipeline has already classified the mechanism.
   - The pipeline's `rationale` and `explanation_brief` fields are the
     authoritative narrative summary. Paraphrase them; do not contradict them.

4. **No invented numbers, ever.** If the actual number is not impressive
   enough to support the post angle, either change the case study or change
   the angle. Do not round up, do not extrapolate, do not "approximate."

5. **Pull at write-time, not at plan-time.** Each post / slide / outreach
   message that cites a filing must run a fresh lookup against the pipeline
   output the moment it is drafted. Plans and outlines may name the
   `(ticker, report_date)` but must not pre-fill numbers; numbers fill in
   only at draft time, from the source.

### Source-of-truth catalog

Local CSVs (refreshed by the scoring pipeline):
- `~/AIProjects/redink/scoring-pipeline/output/quarterly_scores_detailed.csv`
  — one row per (ticker, report_date) with full z-scores and drivers
- `~/AIProjects/redink/scoring-pipeline/output/top_anomaly_review_pack.csv`
  — top anomalies with metadata

GCS mirrors of the above:
- `gs://qqq-anomaly-raw-sg/qqq/scoring_output/quarterly_scores_detailed.csv`
- `gs://qqq-anomaly-raw-sg/qqq/scoring_output/top_anomaly_review_pack.csv`

BigQuery (project `qqq-anomaly-lab`, dataset `qqq_finance`):
- `narrative_divergence` — divergence_label, confidence_score, cited_passage,
  rationale, mda_tone, anomaly_acknowledged
- `anomaly_explanations` — pattern_name, pattern_confidence, pattern_summary,
  explanation_brief
- `conviction_scores` — conviction_score, conviction_tier, pillar_anomaly,
  pillar_earnings, pillar_transparency
- `analyst_actions` — investigation_path, key_question, persistence_test,
  priority_section, urgency_tier

### Lookup recipes (use these, not your memory)

```bash
# All scoring fields for one filing (numbers)
awk -F, 'NR==1 || ($1=="WBD" && $5=="2022-06-30")' \
  ~/AIProjects/redink/scoring-pipeline/output/quarterly_scores_detailed.csv

# Narrative divergence + acknowledgment for one filing
bq query --project_id=qqq-anomaly-lab --use_legacy_sql=false --format=prettyjson "
  SELECT divergence_label, confidence_score, mda_tone, anomaly_acknowledged,
         cited_passage, rationale
  FROM qqq_finance.narrative_divergence
  WHERE ticker='WBD' AND report_date='2022-06-30'"

# Analyst brief + pattern for one filing
bq query --project_id=qqq-anomaly-lab --use_legacy_sql=false --format=prettyjson "
  SELECT pattern_name, pattern_summary, explanation_brief
  FROM qqq_finance.anomaly_explanations
  WHERE ticker='WBD' AND report_date='2022-06-30'"

# Conviction tier + pillars for one filing
bq query --project_id=qqq-anomaly-lab --use_legacy_sql=false --format=prettyjson "
  SELECT conviction_score, conviction_tier, pillar_anomaly,
         pillar_earnings, pillar_transparency
  FROM qqq_finance.conviction_scores
  WHERE ticker='WBD' AND report_date='2022-06-30'"
```

Run these lookups every time you draft an artifact that cites the filing.
Do not paste the results into a permanent file; the artifact references
the row, not a copy of it.

### Verification gate before any artifact ships

Before any post / slide / outreach is marked ready:

1. Extract every (ticker, report_date) it mentions.
2. Re-run the lookup recipes above for each.
3. Check every number in the artifact against the row.
4. Check every narrative claim against `divergence_label`,
   `anomaly_acknowledged`, and the `rationale` text.
5. If any mismatch: fix the artifact, not the source. Re-run the gate.

If you cannot run the gate (no BQ access, CSV not refreshed), the artifact
does not ship. Period.
```

After inserting this section, also do the following inside `CLAUDE.md`:

- Find every existing instruction that tells you to "build an evidence bank," "create a deduplicated inventory of moments," "write atomic facts," or anything that produces a local file containing pipeline numbers. Either delete those instructions or rewrite them to produce **pointer files** (entries containing only `ticker + report_date + one-sentence post-angle`, with no numbers).
- Add a line at the top of any phase that drafts content (Phase 4 / Phase 6 / Phase 7 etc.): *"Before drafting, run the lookup recipes for every (ticker, report_date) you plan to cite. Numbers from the source only."*

When the CLAUDE.md edits are ready, **show them to the user as a diff and stop. Do not move to Phase 2 until the user approves the new CLAUDE.md.**

---

## 2 · Audit phase — do this before any regeneration

The user does not want a regeneration first; they want to know how much is broken. Produce a single audit report, `audit-report-YYYY-MM-DD.md`, with one row per artifact in this project that references pipeline data.

**In scope:**
- Every file under `posts/`, `outreach/`, `slides/`, `decks/`, or any folder containing drafted content
- `evidence-bank.md`, `evidence-bank-v2-addendum.md`, `narrative-threads*.md`, `phase4-content-engine.md`, `phase6-drafting-guide.md`, `comments-guide.md`, `campaign-export.md`, and any analogue files in `redink-presentation/`
- Any presentation file: `*.md`, `*.pptx`, slide JSON, exported HTML, image captions

**For each artifact, record:**

| Column | What goes in it |
|---|---|
| `path` | relative path from project root |
| `tickers_cited` | comma-separated `(ticker, report_date)` tuples |
| `quantitative_claims` | each numeric claim, one per line, e.g. "anomaly score 100", "self-z 4.29", "+8.0 cap" |
| `qualitative_claims` | each narrative judgment, e.g. "management did not address the loss", "narrative contradicts the anomaly" |
| `source_row_check` | for each tuple, the actual values from `quarterly_scores_detailed.csv` and `narrative_divergence` |
| `verdict` | `MATCH` / `MISMATCH` / `UNVERIFIABLE` (no row exists in source) / `NO_CLAIMS` (artifact doesn't cite specific filings) |
| `failure_mode` | for MISMATCH only: `fabricated_number` / `wrong_divergence_framing` / `wrong_top_driver` / `right_quarter_wrong_year` / `multiple` |
| `regeneration_strategy` | `rewrite_with_correct_numbers` / `swap_case_study` / `delete` / `no_change_needed` |

Run the lookup recipes for every tuple. Do not skip any. If the CSV doesn't have a row for a `(ticker, report_date)` cited by an artifact, mark it `UNVERIFIABLE` — that's a different and possibly worse failure (the writer invented a filing that doesn't exist in our universe).

After producing the report, **stop and surface to the user**:
- Total artifacts scanned
- How many MATCH / MISMATCH / UNVERIFIABLE / NO_CLAIMS
- Top 5 worst offenders by number of mismatched claims
- A recommendation: regenerate-all vs. fix-in-place vs. swap-case-studies for each cluster of failures

**Do not regenerate until the user reviews the audit and approves the regeneration scope.** They may want to drop some posts entirely rather than rewrite them.

---

## 3 · Regeneration phase — only after the user approves the audit

For each artifact the user approves for regeneration:

1. **Re-derive the case study from the source, not from the prior draft.**
   The prior draft is contaminated; do not use it as a starting point for
   numbers or framing. You may keep the structural skeleton (hook / tension
   / insight / etc.) but every fact gets pulled fresh.
2. **If the prior case study no longer makes sense given the real data**
   (e.g. the post's angle was "narrative hides the anomaly" but the actual
   filing is CORROBORATES), pick a different filing. Concretely: query
   BigQuery for filings that genuinely fit the angle, e.g.
   `WHERE divergence_label = 'CONTRADICTS' AND confidence_score >= 0.7
    AND anomaly_acknowledged = FALSE AND anomaly_score_0_100 >= 75`
   for a "narrative hides the anomaly" angle.
3. **Inline source citations.** At the end of every regenerated artifact,
   include a hidden block (HTML comment, frontmatter, or appendix —
   appropriate to the format) listing every `(ticker, report_date)` cited
   and the timestamp the lookup ran. This is the audit trail.
4. **No new evidence bank.** Replace `evidence-bank.md` (and any analogue)
   with a thin `case-studies-pointers.md` that lists, per case study, only:
   `(ticker, report_date)`, the post angle in one sentence, the
   `divergence_label`, and the `pattern_name`. No numbers. Numbers come from
   the source at draft time.

After regeneration, run **the verification gate from Section 1 on every
regenerated artifact** and produce a `regeneration-verification-report.md`
showing each artifact's gate result. Any artifact that doesn't pass the gate
gets pulled, not patched.

---

## 4 · Final verification gate — `verify_artifact.py`

Write a small script in this project that takes a path to a draft artifact
and:

1. Extracts every `(ticker, report_date)` it mentions (regex over uppercase
   ticker symbols and ISO dates, plus any explicit `Q[1-4] YYYY` patterns
   resolved to quarter-end dates).
2. For each tuple, queries the scoring CSV + the four BigQuery tables.
3. Prints a side-by-side: the artifact's claims (numbers extracted by regex)
   vs. the actual row.
4. Flags any number in the artifact that does not appear in the source row
   for that filing, and any qualitative phrase from a known-bad list
   ("management did not address," "narrative ignores," "hidden in the MD&A,"
   "no coverage of") when `anomaly_acknowledged = TRUE` for the cited filing.
5. Exits non-zero on any flag.

Wire this into the project's content workflow as a pre-publish gate. Make
it impossible to ship an artifact without passing.

---

## 5 · Things to NOT do

- Do not edit anything in `~/AIProjects/redink/scoring-pipeline/`. The pipeline is correct.
- Do not generate new "evidence" by reading the SEC filings directly. The pipeline already did that and produced `narrative_divergence.cited_passage` and `anomaly_explanations.explanation_brief`. Use those.
- Do not "approximate" or "stylize" numbers for impact. If 75.75 isn't impressive enough to anchor a post, the post needs a different filing, not a different number.
- Do not assume earlier audits are still valid. Phase4 / Phase6 / "Five Gates" audits all passed v3 of the WBD post — they are insufficient. The new gate replaces them.
- Do not run a full regeneration before the audit phase finishes and the user approves scope.

---

## 6 · Order of operations — confirm at each gate

1. Update `CLAUDE.md` with Section 1 verbatim. Show the diff. **STOP — wait for approval.**
2. Run the audit (Section 2). Produce `audit-report-*.md`. **STOP — surface findings, wait for approval.**
3. Regenerate approved artifacts (Section 3). Produce `regeneration-verification-report.md`. **STOP — wait for approval.**
4. Wire `verify_artifact.py` into the workflow. Confirm it blocks an
   intentionally-broken test artifact. **DONE.**

Do not skip a gate. Do not batch the approvals. The user wants to see how
bad it is before deciding how much to rebuild.
