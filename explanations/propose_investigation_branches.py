"""Step 10 — pre-compute investigation-graph ROOTS for every flagged filing.

This is the BATCH half of the disambiguation graph (ADR-8 / Phase 5 split). The
interactive investigator is request-time (a human steers a branch, ADR-8/ADR-10),
which cannot live in a nightly DAG. But the CHEAP, deterministic-shaped part —
naming the N competing hypotheses for each flag — *is* batch-shaped: one
`propose_branches` call per flagged filing, no human, no investigation budget.

So the line is drawn here (per the batch/interactive checkpoint):
  * BATCH (this step): `propose_branches` → write the proposed root branches to
    `qqq_finance.investigation_branches`. Runs nightly for every ALERT/FLAG filing.
  * REALTIME (redink-ui service, not this repo): a user picks a branch → the service
    runs `run_branch`/`expand` on that branch's `predicate`. Consumes this table.

The batch step and the interactive service call the SAME `propose_branches`
(src/qqq_scoring/investigator/graph.py) — the disambiguation surface is identical
whether pre-computed or (later) regenerated on demand.

Registered as Step 10 in scripts/orchestrate.py (depends on Step 8, analyst_actions,
for key_question). Incremental by default; --full-refresh rebuilds.

Usage:
    python explanations/propose_investigation_branches.py                 # incremental
    python explanations/propose_investigation_branches.py --limit 2       # cheap live test
    python explanations/propose_investigation_branches.py --dry-run       # count only, no LLM
    python explanations/propose_investigation_branches.py --full-refresh  # rebuild all
"""
from __future__ import annotations

import argparse
import io
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from qqq_scoring.investigator.graph import Flag, propose_branches  # noqa: E402

BQ_PROJECT   = "qqq-anomaly-lab"
BQ_DATASET   = "qqq_finance"
SOURCE_VIEW  = f"{BQ_PROJECT}.{BQ_DATASET}.filing_intelligence"
ACTIONS_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.analyst_actions"
OUTPUT_TABLE = f"{BQ_PROJECT}.{BQ_DATASET}.investigation_branches"

# Sonnet for the batch: naming hypotheses is well within it, and it is the
# cost-appropriate tier for one call per flagged filing across the universe. The
# interactive deep-dive (per steered branch, far rarer) can afford Opus.
MODEL        = "claude-sonnet-5"
N_BRANCHES   = 4
# Bump when the proposer prompt/schema changes so stale hypotheses can be invalidated
# (findings #8). Stamped on every row.
PROMPT_VERSION = "v1"
# Flush to BQ every N filings so a crash mid-run doesn't lose everything and a rerun
# resumes via the incremental skip (findings #7).
FLUSH_EVERY  = 25
# Only the filings actually worth investigating — the ambiguous, high-signal flags.
TARGET_TIERS = ("ALERT", "FLAG")


# ── pure helpers (unit-testable, no BQ/LLM) ─────────────────────────────────

def flag_from_row(row) -> Flag:
    """Build the investigator's Flag from a filing_intelligence (+key_question) row."""
    drivers = []
    for i in (1, 2, 3):
        name = row.get(f"top_driver_{i}")
        val = row.get(f"top_driver_{i}_value")
        if name is not None and not pd.isna(name):
            drivers.append(f"{name} ({val})" if val is not None and not pd.isna(val) else str(name))
    kq = row.get("key_question")
    if kq is None or pd.isna(kq):
        kq = "why is this filing anomalous, and will the anomaly persist?"
    summary = (
        f"Anomaly score {row.get('anomaly_score_0_100')}/100 ({row.get('conviction_tier')}). "
        f"Top drivers: {'; '.join(drivers) if drivers else 'n/a'}. "
        f"key_question: {kq}"
    )
    return Flag(ticker=row["ticker"], report_date=row["report_date"], form=row.get("form_type", "10-Q"), summary=summary)


def branch_rows(flag: Flag, branches, row, generated_at: datetime, model: str = MODEL) -> list[dict]:
    """Flatten proposed branches into output rows for the branches table."""
    return [
        {
            "ticker": flag.ticker,
            "report_date": flag.report_date,
            "calendar_quarter": row.get("calendar_quarter"),
            "conviction_tier": row.get("conviction_tier"),
            "anomaly_score_0_100": row.get("anomaly_score_0_100"),
            "branch_id": b.id,
            "hypothesis": b.hypothesis,
            "rationale": b.rationale,
            "predicate": b.predicate,          # the steering wire the UI hands to the realtime service
            "status": b.status.value,          # 'proposed' — never investigated in batch
            "model": model,
            "prompt_version": PROMPT_VERSION,  # invalidation handle (findings #8)
            "generated_at": generated_at,
        }
        for b in branches
    ]


# ── BigQuery I/O (mirrors generate_analyst_actions conventions) ──────────────

def _table_exists(client: bigquery.Client, table_id: str) -> bool:
    from google.api_core.exceptions import NotFound
    try:
        client.get_table(table_id)
        return True
    except NotFound:
        return False


def load_flags(client: bigquery.Client, ticker: str | None, limit: int | None,
               full_refresh: bool) -> pd.DataFrame:
    """Flagged filings (ALERT/FLAG) + their key_question, incremental by default."""
    tiers = ", ".join(f"'{t}'" for t in TARGET_TIERS)
    conds = [f"fi.conviction_tier IN ({tiers})"]
    if ticker:
        conds.append(f"fi.ticker = '{ticker}'")
    # Incremental only when the output table already exists (first run has nothing
    # to diff against — the NOT EXISTS would query a non-existent table).
    if not full_refresh and not ticker and _table_exists(client, OUTPUT_TABLE):
        conds.append(f"""NOT EXISTS (
            SELECT 1 FROM `{OUTPUT_TABLE}` ib
            WHERE ib.ticker = fi.ticker AND ib.calendar_quarter = fi.calendar_quarter
        )""")
    where = " AND ".join(conds)
    lim = f"LIMIT {int(limit)}" if limit else ""
    query = f"""
        SELECT fi.ticker, fi.company_name, fi.report_date, fi.calendar_quarter,
               fi.form_type, fi.anomaly_score_0_100, fi.conviction_tier,
               fi.top_driver_1, fi.top_driver_1_value,
               fi.top_driver_2, fi.top_driver_2_value,
               fi.top_driver_3, fi.top_driver_3_value,
               aa.key_question
        FROM `{SOURCE_VIEW}` fi
        LEFT JOIN `{ACTIONS_TABLE}` aa
          ON aa.ticker = fi.ticker AND aa.calendar_quarter = fi.calendar_quarter
        WHERE {where}
        ORDER BY fi.anomaly_score_0_100 DESC
        {lim}
    """
    return client.query(query).to_dataframe()


def _upload(client: bigquery.Client, rows: list[dict], truncate: bool) -> None:
    if not rows:
        print("  No branches to upload.")
        return
    df = pd.DataFrame(rows)
    df["report_date"] = pd.to_datetime(df["report_date"], utc=True).dt.date
    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)
    disposition = (bigquery.WriteDisposition.WRITE_TRUNCATE if truncate
                   else bigquery.WriteDisposition.WRITE_APPEND)
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=disposition,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY, field="report_date"),
        clustering_fields=["ticker"],
    )
    client.load_table_from_file(buf, OUTPUT_TABLE, job_config=job_config).result()
    print(f"  Uploaded {len(df)} branch rows → {OUTPUT_TABLE}  [{'TRUNCATE' if truncate else 'APPEND'}]")


def _delete_ticker(client: bigquery.Client, ticker: str) -> None:
    """Make a targeted --ticker rerun idempotent: drop that ticker's existing branch
    rows before re-appending (findings #8 — otherwise WRITE_APPEND duplicates them)."""
    if not _table_exists(client, OUTPUT_TABLE):
        return
    client.query(
        f"DELETE FROM `{OUTPUT_TABLE}` WHERE ticker=@t",
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("t", "STRING", ticker)]),
    ).result()


def main() -> None:
    ap = argparse.ArgumentParser(description="Pre-compute investigation-graph roots for flagged filings")
    ap.add_argument("--ticker", help="Only this ticker (idempotent: replaces its rows)")
    ap.add_argument("--limit", type=int, help="Cap filings processed (cheap live test)")
    ap.add_argument("--model", default=MODEL, help=f"Proposer model (default {MODEL}); use to A/B tiers")
    ap.add_argument("--full-refresh", action="store_true", help="Rebuild the whole table")
    ap.add_argument("--dry-run", action="store_true", help="Count filings, no LLM/write")
    args = ap.parse_args()

    bq = bigquery.Client(project=BQ_PROJECT)
    flags = load_flags(bq, args.ticker, args.limit, args.full_refresh)
    print(f"Flagged filings to propose branches for: {len(flags)}  (model={args.model})")
    if args.dry_run:
        for _, r in flags.head(5).iterrows():
            print(f"  {r['ticker']} {r.get('form_type')} @ {r['report_date']}  [{r['conviction_tier']}]")
        return
    if flags.empty:
        print("Nothing to do (all flagged filings already have branches).")
        return

    if args.ticker and not args.full_refresh:
        _delete_ticker(bq, args.ticker)   # idempotent targeted rerun

    import anthropic
    llm = anthropic.Anthropic()
    generated_at = datetime.now(timezone.utc)
    out: list[dict] = []
    ok = fail = 0
    uploaded_any = False

    def flush() -> None:
        # First flush honours --full-refresh (TRUNCATE); later flushes must APPEND,
        # or they would wipe earlier flushes. Incremental skip lets a rerun resume.
        nonlocal uploaded_any
        if not out:
            return
        _upload(bq, out, truncate=(args.full_refresh and not uploaded_any))
        uploaded_any = True
        out.clear()

    for i, (_, row) in enumerate(flags.iterrows(), 1):
        flag = flag_from_row(row)
        try:
            graph = propose_branches(llm, flag, n=N_BRANCHES, model=args.model)
            if not graph.branches:
                print(f"  WARN {flag.ticker} @ {flag.report_date}: 0 branches (will retry next run)")
                continue
            out.extend(branch_rows(flag, graph.branches, row, generated_at, model=args.model))
            ok += 1
        except Exception as e:  # one bad filing must not lose the batch (findings #7)
            fail += 1
            print(f"  ERROR {flag.ticker} @ {flag.report_date}: {type(e).__name__}: {e}")
        if i % FLUSH_EVERY == 0:
            flush()
            print(f"  … checkpoint at {i}/{len(flags)}  (ok={ok} fail={fail})")

    flush()
    print(f"Done. filings ok={ok} fail={fail}. Rerun resumes any failures via incremental skip.")


if __name__ == "__main__":
    main()
