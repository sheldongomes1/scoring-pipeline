"""Phase 6 harness — run a SAMPLE of live investigations and capture transcripts.

Produces the "outputs file" the qqq-eval-suite grades (evals/investigator_evals.py):
one record per investigation, with the inputs (key_question, predicate), the system's
own verdict, the generator-facing evidence, and cost/latency. This is the Analyze
step's raw material — you cannot eval an agent you haven't run.

Real feature_history (BigQuery); balance_sheet + narrative are still fakes, so for
tickers outside the WBD/AAPL fixtures those tools return absent/not-filed and the
investigation leans on the real numeric path (which is the honest thing to eval today).

Run:  ANTHROPIC_API_KEY=... python3 scripts/eval_capture_investigations.py --n 6 --out <path>
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from google.cloud import bigquery  # noqa: E402

from qqq_scoring.investigator.graph import Branch, Flag, run_branch  # noqa: E402
from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools import balance_sheet as bs  # noqa: E402
from qqq_scoring.investigator.tools import feature_history as fh  # noqa: E402
from qqq_scoring.investigator.tools import narrative_sections as ns  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_fake import SOURCE as BS_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_fake import balance_sheet_items as bs_fake  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_bq import SOURCE as FH_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_bq import feature_history_bq  # noqa: E402
from qqq_scoring.investigator.tools.narrative_sections_gcs import narrative_sections_gcs  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())
BQ_PROJECT = "qqq-anomaly-lab"

SYSTEM = (
    "You are an equity-analyst investigator. Resolve the specific hypothesis you are "
    "given using ONLY the tools provided (numbers via feature_history, line items via "
    "balance_sheet_items, management's words via narrative_sections). Never state a "
    "figure you have not fetched. Give a clear verdict."
)

# One investigation per distinct ticker (diverse), highest-signal quarter, branch h1.
SAMPLE_QUERY = """
  SELECT ib.ticker, ib.report_date, ib.calendar_quarter, ib.conviction_tier,
         ib.branch_id, ib.hypothesis, ib.rationale, ib.predicate,
         fi.form_type, fi.anomaly_score_0_100,
         fi.top_driver_1, fi.top_driver_1_value,
         fi.top_driver_2, fi.top_driver_2_value,
         fi.top_driver_3, fi.top_driver_3_value,
         aa.key_question
  FROM `qqq-anomaly-lab.qqq_finance.investigation_branches` ib
  JOIN `qqq-anomaly-lab.qqq_finance.filing_intelligence` fi
    USING (ticker, calendar_quarter)
  LEFT JOIN `qqq-anomaly-lab.qqq_finance.analyst_actions` aa
    USING (ticker, calendar_quarter)
  WHERE ib.branch_id = 'h1'
  QUALIFY ROW_NUMBER() OVER (PARTITION BY ib.ticker ORDER BY fi.anomaly_score_0_100 DESC) = 1
  ORDER BY fi.anomaly_score_0_100 DESC
  LIMIT @n
"""


def _registry() -> ToolRegistry:
    return ToolRegistry([
        ToolBinding("feature_history", feature_history_bq, fh.tool_definition(FEATURE_KEYS),
                    fh.parse_model_input, fh.to_model_content),
        ToolBinding("balance_sheet_items", bs_fake, bs.tool_definition(bs.ITEM_KEYS),
                    bs.parse_model_input, bs.to_model_content),
        ToolBinding("narrative_sections", narrative_sections_gcs, ns.tool_definition(ns.SECTION_KEYS),
                    ns.parse_model_input, ns.to_model_content),
    ])


def _flag_summary(row) -> str:
    drivers = []
    for i in (1, 2, 3):
        n, v = row.get(f"top_driver_{i}"), row.get(f"top_driver_{i}_value")
        if n:
            drivers.append(f"{n} ({v})" if v is not None else str(n))
    kq = row.get("key_question") or "why is this filing anomalous?"
    return (f"Anomaly score {row.get('anomaly_score_0_100')}/100 ({row.get('conviction_tier')}). "
            f"Top drivers: {'; '.join(drivers) or 'n/a'}. key_question: {kq}")


def _evidence_view(ev) -> list[dict]:
    out = []
    for e in ev:
        mode = e.grounding_mode.value
        if mode == "semantic":
            out.append({"kind": "narrative", "section": e.section, "status": e.status.value,
                        "passage": e.passage})
        else:
            out.append({"kind": "metric", "feature": e.feature, "status": e.status.value,
                        "value": e.value,
                        "resolved_report_date": e.provenance.resolved_report_date.isoformat()})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6)
    ap.add_argument("--out", default=str(REPO / "output" / "investigator_eval_outputs.json"))
    args = ap.parse_args()

    import anthropic
    bq = bigquery.Client(project=BQ_PROJECT)
    rows = list(bq.query(SAMPLE_QUERY, job_config=bigquery.QueryJobConfig(
        query_parameters=[bigquery.ScalarQueryParameter("n", "INT64", args.n)])))
    print(f"Running {len(rows)} investigations…")

    generator = anthropic.Anthropic()
    judge = Judge(client=anthropic.Anthropic(),
                  reverify={FH_SOURCE: feature_history_bq, BS_SOURCE: bs_fake})
    records = []
    for i, row in enumerate(rows, 1):
        rd = row["report_date"]  # a date
        flag = Flag(ticker=row["ticker"], report_date=rd,
                    form=row["form_type"] or "10-Q", summary=_flag_summary(row))
        branch = Branch(id=row["branch_id"], hypothesis=row["hypothesis"],
                        rationale=row["rationale"], predicate=row["predicate"])
        t0 = time.time()
        try:
            run_branch(branch, flag, generator, _registry(), judge, system=SYSTEM, max_seconds=240)
            r = branch.result
            rec = {
                "trace_id": f"{flag.ticker}_{row['calendar_quarter']}_{branch.id}",
                "ticker": flag.ticker, "calendar_quarter": row["calendar_quarter"],
                "report_date": rd.isoformat(), "branch_id": branch.id,
                "hypothesis": branch.hypothesis, "predicate": branch.predicate,
                "key_question": row.get("key_question"),
                "terminal_state": branch.status.value,
                "trusted": r.trusted,   # did the answer pass grounding cleanly? (Fable health check)
                "grounded": r.verdict.grounded if r.verdict else None,
                "confirm": (r.verdict.confirm.value if r.verdict and r.verdict.confirm else None),
                "open_questions": r.verdict.open_questions if r.verdict else None,
                "final_text": r.final_text,
                "evidence": _evidence_view(r.evidence),
                "tool_calls": r.tool_calls, "iterations": r.iterations,
                "elapsed_seconds": round(time.time() - t0, 1),
            }
            print(f"  [{i}/{len(rows)}] {flag.ticker} {branch.id} → {branch.status.value} "
                  f"({rec['elapsed_seconds']}s, {r.tool_calls} tools)")
        except Exception as e:
            rec = {"trace_id": f"{flag.ticker}_{row['calendar_quarter']}_{branch.id}",
                   "ticker": flag.ticker, "error": f"{type(e).__name__}: {e}",
                   "elapsed_seconds": round(time.time() - t0, 1)}
            print(f"  [{i}/{len(rows)}] {flag.ticker} {branch.id} ERROR: {e}")
        records.append(rec)

    Path(args.out).write_text(json.dumps(records, indent=2))
    print(f"\nWrote {len(records)} records → {args.out}  (captured_at {datetime.now(timezone.utc).isoformat()})")


if __name__ == "__main__":
    main()
