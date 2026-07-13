"""Tool 1 of the action space: BigQuery feature history.

Answers "what was feature F for ticker T, N quarters from the flagged filing?" —
the move that resolves a `persistence_test` (ADR-1). This is a STUB: the contract
(typed I/O + provenance envelope, ADR-2) is real, importable, and tested; the
BigQuery body is not wired yet.
"""
from __future__ import annotations

import json
from datetime import date

from .contracts import FeatureResult


def feature_history(
    ticker: str,
    report_date: date,
    period_offset: int,      # signed: +1 = next quarter, -1 = prior, 0 = the filing's own quarter
    features: list[str],
) -> list[FeatureResult]:
    """Return one FeatureResult per requested feature at report_date + period_offset.

    The agent chooses these arguments at run-time; that run-time choice is what
    makes the loop agentic (ADR-1: the model owns the next edge). The tool — not
    the agent — owns the calendar math: it resolves `report_date + period_offset`
    to an actual quarter, so the model never does date arithmetic (a reliability
    leak). The resolved quarter is echoed back in `Provenance.resolved_report_date`
    so the judge can verify the value belongs to the period the agent claims.

    Input cardinality matches output cardinality: `features` is a list, so the
    return is one FeatureResult per requested feature (never a bare scalar).
    """
    raise NotImplementedError(
        "feature_history contract is stubbed; BigQuery lookup not wired yet. "
        "Contract standard: docs/agentic-investigation/decisions.md (ADR-2)."
    )


def tool_definition(feature_keys: list[str]) -> dict:
    """Build the Claude tool-use definition for feature_history (the "menu entry").

    This is the *model-facing* half of the contract (ADR-2): only the inputs the
    agent chooses appear here. The output envelope (status, value, provenance)
    is NOT described to the model — it flows back separately as a `tool_result`.

    `strict=True` extends ADR-2 principle 2 ("make illegal states unrepresentable")
    from the tool's output to its *input*: with `additionalProperties: false` and
    every field required, the API guarantees `tool_use.input` validates exactly —
    a malformed/hallucinated argument can't reach the BigQuery query.

    `feature_keys` is passed in (not hardcoded) so the enum stays in sync with the
    canonical list in output/feature_keys.json — one source of truth, not two.
    """
    return {
        "name": "feature_history",
        "description": (
            "Look up the value of one or more engineered anomaly features for a "
            "company at a quarter offset from a given filing. Call this when you "
            "need to check whether a feature persisted, recovered, or worsened in "
            "a later period — e.g. to resolve a persistence_test such as 'did "
            "OCF/NI recover next quarter'. Returns one result per requested "
            "feature, each carrying provenance for grounding."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {
                    "type": "string",
                    "description": "Company ticker, e.g. 'AAPL'.",
                },
                "report_date": {
                    "type": "string",
                    "format": "date",
                    "description": "The flagged filing's report date (the anchor), ISO 8601 (YYYY-MM-DD).",
                },
                "period_offset": {
                    "type": "integer",
                    "description": "Signed quarter offset from report_date: +1 = next quarter, -1 = prior, 0 = the filing's own quarter.",
                },
                "features": {
                    "type": "array",
                    "items": {"type": "string", "enum": feature_keys},
                    "description": "Which feature columns to return, e.g. ['ocf_to_net_income'].",
                },
            },
            "required": ["ticker", "report_date", "period_offset", "features"],
            "additionalProperties": False,
        },
    }


# ---------------------------------------------------------------------------
# Phase-2 adapters — the two seams ADR-3 deferred.
#
# The model speaks JSON (strings, numbers, arrays); the Python callable speaks
# typed args (a `date`) and returns dataclasses. These two functions are the
# translation layer at the boundary. They are tool-SPECIFIC (they know
# feature_history's shape), so they live with the tool — the registry that
# routes calls stays generic and knows nothing about dates or FeatureResult.
# ---------------------------------------------------------------------------


def parse_model_input(model_input: dict) -> dict:
    """Seam (a): the model's JSON input -> kwargs for the `feature_history` callable.

    The model sends `report_date` as an ISO string (that's all JSON has); the
    callable's signature wants a real `date`. Parsing lives HERE, once, at the
    edge — so no downstream code, and never the model, does date arithmetic
    (a reliability leak). `strict:true` on the tool definition already guarantees
    the four keys are present and well-typed, so this parse is safe, not defensive.
    """
    return {
        "ticker": model_input["ticker"],
        "report_date": date.fromisoformat(model_input["report_date"]),
        "period_offset": model_input["period_offset"],
        "features": model_input["features"],
    }


def to_model_content(results: list[FeatureResult]) -> str:
    """Serialize results into the tool_result content the GENERATOR sees (ADR-5).

    Generator-facing subset only: `feature`, `status`, `value`, and
    `resolved_report_date` (the model must know WHICH quarter it landed on to
    pick its next edge). The receipts — `query`, `retrieved_at`,
    `accession_number`, `source` — are withheld here and retained separately for
    the judge. Withholding the query is what stops the model from parroting a
    receipt it never used and *looking* grounded: the only way its answer can
    match the paper trail is if it actually used the real value.
    """
    out = []
    for r in results:
        item = {
            "feature": r.feature,
            "status": r.status.value,
            "value": r.value,
            "resolved_report_date": r.provenance.resolved_report_date.isoformat(),
        }
        # ADR-14: warn the model the jump crossed a fiscal year-end (a 10-K sits
        # between), so it doesn't read a ~6-month gap as two adjacent quarters.
        if getattr(r, "periods_skipped", 0):
            item["fiscal_periods_skipped"] = r.periods_skipped
        out.append(item)
    return json.dumps(out)
