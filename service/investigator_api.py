"""5b — the request-time LIVE DEEP-DIVE service (the interactive half of ADR-8).

The batch half (Step 10, `explanations/propose_investigation_branches.py`) writes
the PROPOSED hypothesis roots to `qqq_finance.investigation_branches`; redink-ui
renders them as the steering surface. THIS service is what happens when the human
steers: one POST /investigate = one `run_branch` deep-dive on the chosen branch's
`predicate`, with the full Phase-2 loop — real tools, real grounding judge.

Wiring is IDENTICAL to scripts/investigate_fanout.py (the audited reference),
with two production upgrades: `feature_history` uses the REAL BigQuery backend
(feature_history_bq, ADR-13/14) and `balance_sheet_items` uses the REAL SEC EDGAR
companyfacts backend (balance_sheet_edgar) — the judge's reverify map routes each
source to its own backend, so grounding re-fetches against the golden sources.
narrative reads GCS; no fixtures remain in the served tool set.

The service returns a GENERATOR-FACING view of the evidence only (feature /
status / value / resolved_report_date for numbers; section / passage for prose).
Provenance receipts — query, retrieved_at, accession_number, source — are the
judge's paper trail and are NEVER serialized into the response (ADR-5).

Cost guard: every /investigate call spends real Opus (generator) + Sonnet (judge)
tokens (~10-60s, ~$0.05-0.20). A small semaphore rejects pile-ups with 429
instead of queueing unbounded spend, and an optional shared secret
(INVESTIGATOR_API_TOKEN) gates the endpoint when deployed.

Run locally:
    ANTHROPIC_API_KEY=... uvicorn investigator_api:app --port 8080  (from service/)
See service/README.md for the Cloud Run deploy sketch.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import date
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from qqq_scoring.investigator.graph import Branch, Flag, run_branch  # noqa: E402
from qqq_scoring.investigator.judge import Judge  # noqa: E402
from qqq_scoring.investigator.registry import ToolBinding, ToolRegistry  # noqa: E402
from qqq_scoring.investigator.tools import balance_sheet as bs  # noqa: E402
from qqq_scoring.investigator.tools import feature_history as fh  # noqa: E402
from qqq_scoring.investigator.tools import narrative_sections as ns  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_bq import SOURCE as BS_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.balance_sheet_bq import balance_sheet_items as balance_sheet_backend  # noqa: E402
from qqq_scoring.investigator.tools.contracts import GroundingMode  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_bq import SOURCE as FH_SOURCE  # noqa: E402
from qqq_scoring.investigator.tools.feature_history_bq import feature_history_bq  # noqa: E402
from qqq_scoring.investigator.tools.narrative_sections_gcs import narrative_sections_gcs  # noqa: E402

FEATURE_KEYS = json.loads((REPO / "output" / "feature_keys.json").read_text())

BQ_PROJECT = "qqq-anomaly-lab"
FH_TABLE = f"{BQ_PROJECT}.qqq_finance.period_features"
INTEL_VIEW = f"{BQ_PROJECT}.qqq_finance.filing_intelligence"
ACTIONS_TABLE = f"{BQ_PROJECT}.qqq_finance.analyst_actions"

# Same investigator persona as the audited fan-out reference.
SYSTEM = (
    "You are an equity-analyst investigator. Resolve the specific hypothesis you are "
    "given using ONLY the tools provided (numbers via feature_history, management's "
    "words via narrative_sections). Never state a figure you have not fetched. Give "
    "a verdict; a reviewer verifies your evidence before the branch is closed."
)

# Cost guard: at most this many deep-dives in flight; extra requests get 429
# (a click retried later is cheaper than an unbounded Opus queue).
MAX_CONCURRENT = int(os.environ.get("INVESTIGATOR_MAX_CONCURRENT", "2"))
_slots = threading.Semaphore(MAX_CONCURRENT)

app = FastAPI(title="qqq-investigator", version="0.1.0")


# ── request / response contracts ─────────────────────────────────────────────


class InvestigateRequest(BaseModel):
    """One branch to steer into — mirrors an investigation_branches row."""

    ticker: str
    report_date: str = Field(description="Flagged filing's report date, YYYY-MM-DD")
    form: str = "10-Q"
    calendar_quarter: str | None = None
    branch_id: str
    hypothesis: str
    rationale: str = ""
    predicate: str
    flag_summary: str | None = Field(
        default=None,
        description="Optional pre-built flag summary; when absent it is reconstructed "
        "from filing_intelligence + analyst_actions.",
    )


# ── flag reconstruction (mirrors propose_investigation_branches.flag_from_row) ─


def _reconstruct_flag_summary(ticker: str, report_date: date) -> str | None:
    """Pull drivers + key_question from BQ to rebuild the Flag summary the batch
    proposer saw. Returns None on any failure — the caller falls back gracefully
    (a degraded summary beats a 500; the predicate is the real steering input)."""
    try:
        from google.cloud import bigquery

        client = bigquery.Client(project=BQ_PROJECT)
        sql = f"""
            SELECT fi.anomaly_score_0_100, fi.conviction_tier,
                   fi.top_driver_1, fi.top_driver_1_value,
                   fi.top_driver_2, fi.top_driver_2_value,
                   fi.top_driver_3, fi.top_driver_3_value,
                   aa.key_question
            FROM `{INTEL_VIEW}` fi
            LEFT JOIN `{ACTIONS_TABLE}` aa
              ON aa.ticker = fi.ticker AND aa.calendar_quarter = fi.calendar_quarter
            WHERE fi.ticker = @ticker AND fi.report_date = @report_date
            LIMIT 1
        """
        job = client.query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("ticker", "STRING", ticker),
                    bigquery.ScalarQueryParameter("report_date", "DATE", report_date.isoformat()),
                ]
            ),
        )
        rows = list(job)
        if not rows:
            return None
        row = rows[0]
        drivers = []
        for i in (1, 2, 3):
            name = row.get(f"top_driver_{i}")
            val = row.get(f"top_driver_{i}_value")
            if name:
                drivers.append(f"{name} ({val})" if val is not None else str(name))
        kq = row.get("key_question") or "why is this filing anomalous, and will the anomaly persist?"
        return (
            f"Anomaly score {row.get('anomaly_score_0_100')}/100 ({row.get('conviction_tier')}). "
            f"Top drivers: {'; '.join(drivers) if drivers else 'n/a'}. "
            f"key_question: {kq}"
        )
    except Exception as exc:  # noqa: BLE001 — degrade, don't fail the deep-dive
        print(f"[investigator_api] flag reconstruction failed for {ticker} @ {report_date}: {exc}")
        return None


# ── investigator wiring (identical to scripts/investigate_fanout.py) ─────────

# The canonical vocabulary (feature_keys.json) can drift ahead of the REAL table:
# ocf_to_assets / equity_multiplier are in the canonical list but not (yet)
# columns of period_features — if the enum offers them, the model's first probe
# 400s the whole deep-dive. So the served enum is canonical ∩ live schema,
# resolved lazily (first /investigate) and cached; on any BQ failure we fall
# back to the canonical list rather than refuse to serve.
_feature_keys_cache: list[str] | None = None
_feature_keys_lock = threading.Lock()


def _live_feature_keys() -> list[str]:
    global _feature_keys_cache
    if _feature_keys_cache is not None:
        return _feature_keys_cache
    with _feature_keys_lock:
        if _feature_keys_cache is not None:
            return _feature_keys_cache
        keys = FEATURE_KEYS
        try:
            from google.cloud import bigquery

            table = bigquery.Client(project=BQ_PROJECT).get_table(FH_TABLE)
            cols = {f.name for f in table.schema}
            live = [k for k in FEATURE_KEYS if k in cols]
            if live:
                dropped = sorted(set(FEATURE_KEYS) - set(live))
                if dropped:
                    print(f"[investigator_api] feature keys not in {FH_TABLE}, dropped from enum: {dropped}")
                keys = live
        except Exception as exc:  # noqa: BLE001 — degrade to the canonical list
            print(f"[investigator_api] could not read {FH_TABLE} schema ({exc}); serving canonical keys")
        _feature_keys_cache = keys
        return keys


def _registry() -> ToolRegistry:
    return ToolRegistry([
        # REAL BigQuery backend for the numbers (ADR-13/14) — the production upgrade.
        ToolBinding("feature_history", feature_history_bq, fh.tool_definition(_live_feature_keys()),
                    fh.parse_model_input, fh.to_model_content),
        # REAL SEC EDGAR companyfacts backend (ADR-9 fake→real swap; zero loop/judge code changes).
        ToolBinding("balance_sheet_items", balance_sheet_backend, bs.tool_definition(bs.ITEM_KEYS),
                    bs.parse_model_input, bs.to_model_content),
        ToolBinding("narrative_sections", narrative_sections_gcs, ns.tool_definition(ns.SECTION_KEYS),
                    ns.parse_model_input, ns.to_model_content),
    ])


def _judge():
    from anthropic import Anthropic

    # Source-routed reverify map (ADR-9): each structured backend keyed by the
    # source its evidence carries. feature_history re-fetches against the REAL
    # table, balance_sheet against REAL EDGAR — grounding replays the request (ADR-13).
    return Judge(client=Anthropic(), reverify={FH_SOURCE: feature_history_bq, BS_SOURCE: balance_sheet_backend})


def _evidence_view(evidence: list) -> list[dict]:
    """GENERATOR-FACING view only (ADR-5): what the model saw, never the receipts.

    query / retrieved_at / accession_number / source stay server-side with the
    judge. `resolved_report_date` IS generator-facing (the model needs to know
    which quarter it landed on), so it is safe to show."""
    view: list[dict] = []
    for e in evidence:
        if e.grounding_mode is GroundingMode.SEMANTIC:
            view.append({
                "kind": "narrative",
                "section": e.section,
                "status": e.status.value,
                "passage": e.passage,
            })
        else:
            item = {
                "kind": "metric",
                "feature": e.feature,
                "status": e.status.value,
                "value": e.value,
                "resolved_report_date": e.provenance.resolved_report_date.isoformat(),
            }
            if getattr(e, "periods_skipped", 0):
                item["fiscal_periods_skipped"] = e.periods_skipped
            view.append(item)
    return view


def build_response_dto(branch: Branch, flag: Flag, elapsed_seconds: float) -> dict:
    """The clean DTO the UI renders. Terminal state + verdict + answer + evidence."""
    result = branch.result
    verdict = None
    if result is not None and result.verdict is not None:
        v = result.verdict
        verdict = {
            "grounded": v.grounded,
            "confirm": v.confirm.value if v.confirm is not None else None,
            "open_questions": v.open_questions,
            "reasoning": v.reasoning,
        }
    return {
        "ticker": flag.ticker,
        "report_date": flag.report_date.isoformat(),
        "branch_id": branch.id,
        "hypothesis": branch.hypothesis,
        "status": branch.status.value,          # resolved | inconclusive | abandoned | capped
        # `trusted` (Fable health check): did final_text pass the grounding gate on a
        # clean terminal? False for capped/abandoned — the UI must NOT present an
        # untrusted answer as a verified conclusion (it may be a rejected/partial one).
        "trusted": bool(result and result.trusted),
        "final_text": result.final_text if result else "",
        "verdict": verdict,
        "tool_calls": result.tool_calls if result else 0,
        "iterations": result.iterations if result else 0,
        "evidence": _evidence_view(result.evidence) if result else [],
        "flag_summary": flag.summary,
        "elapsed_seconds": round(elapsed_seconds, 1),
    }


# ── endpoints ────────────────────────────────────────────────────────────────


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "service": "qqq-investigator",
        "anthropic_key_present": bool(os.environ.get("ANTHROPIC_API_KEY")),
    }


@app.post("/investigate")
def investigate(req: InvestigateRequest, x_api_token: str | None = Header(default=None)) -> dict:
    # Optional shared-secret gate for deployed environments (real $ per call).
    expected = os.environ.get("INVESTIGATOR_API_TOKEN")
    if expected and x_api_token != expected:
        raise HTTPException(status_code=401, detail="invalid or missing X-Api-Token")

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise HTTPException(status_code=503, detail="ANTHROPIC_API_KEY is not configured on the service")

    try:
        report_date = date.fromisoformat(req.report_date)
    except ValueError:
        raise HTTPException(status_code=422, detail=f"report_date must be YYYY-MM-DD, got {req.report_date!r}")

    # Cost guard: reject pile-ups instead of queueing unbounded Opus spend.
    if not _slots.acquire(blocking=False):
        raise HTTPException(
            status_code=429,
            detail=f"investigator busy ({MAX_CONCURRENT} deep-dives already running); retry shortly",
        )
    try:
        summary = req.flag_summary or _reconstruct_flag_summary(req.ticker, report_date) or (
            f"Filing flagged by the anomaly screen ({req.calendar_quarter or report_date.isoformat()}). "
            f"key_question: why is this filing anomalous, and will the anomaly persist?"
        )
        flag = Flag(ticker=req.ticker, report_date=report_date, form=req.form, summary=summary)
        branch = Branch(
            id=req.branch_id,
            hypothesis=req.hypothesis,
            rationale=req.rationale,
            predicate=req.predicate,
        )

        from anthropic import Anthropic

        started = time.monotonic()
        # Single-branch deep-dive only (MVP): one click = one run_branch. No
        # auto-expand — recursive tree growth stays a deliberate, human-steered step.
        # max_seconds=240 keeps the investigation under Cloud Run's 300s request
        # timeout (eval #2 wall-clock guard) — it returns CAP_REACHED rather than a 504.
        run_branch(branch, flag, Anthropic(), _registry(), _judge(), system=SYSTEM, max_seconds=240)
        return build_response_dto(branch, flag, time.monotonic() - started)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — surface a clean 500, not a stack trace
        print(f"[investigator_api] investigation failed for {req.ticker}/{req.branch_id}: {exc}")
        raise HTTPException(status_code=500, detail=f"investigation failed: {type(exc).__name__}")
    finally:
        _slots.release()
