"""Tool 3 of the action space: balance-sheet line items.

The gap the live disambiguation run exposed — the working-capital and
revenue-quality branches couldn't be resolved because the toolset had no
receivables / payables / content-asset figures. This tool returns those raw
balance-sheet stock figures (dollar amounts) at a period.

REUSES `FeatureResult` (ADR-9). A line item is a number, verified the SAME way as
an engineered ratio — re-fetch from the golden source, compare `==`. Same
DETERMINISTIC grounding behaviour, so by ADR-7's rule (behaviour difference → new
type; data difference → reuse) it is NOT a new type. Dollars-vs-ratios is a data
difference; the `feature` field simply holds the line-item key.

This module is the CONTRACT REFERENCE (stubbed body); the REAL backend is
`balance_sheet_edgar.py` (SEC EDGAR companyfacts) — same signature, injected via
the registry, like feature_history / feature_history_bq. The generator-facing
serializer and input parser are shared with the structured-tool pattern.
"""
from __future__ import annotations

from datetime import date

from .contracts import FeatureResult
from .feature_history import to_model_content  # identical generator-facing view (reused, ADR-9)

__all__ = ["balance_sheet_items", "tool_definition", "parse_model_input", "to_model_content", "ITEM_KEYS"]

# Canonical balance-sheet line-item vocabulary (the enum source, per ADR-3 rule 3).
ITEM_KEYS = [
    "accounts_receivable",
    "accounts_payable",
    "inventory",
    "content_assets",
    "total_debt",
    "cash_and_equivalents",
    "deferred_revenue",
]


def balance_sheet_items(
    ticker: str,
    report_date: date,
    period_offset: int,
    items: list[str],
) -> list[FeatureResult]:
    """Return one FeatureResult per requested balance-sheet line item at the period.

    Same shape as `feature_history` — the agent chooses items at run-time (ADR-1),
    the tool owns the calendar math, and each result carries provenance for the
    judge's deterministic re-fetch."""
    raise NotImplementedError(
        "balance_sheet_items contract is stubbed; FMP/BigQuery read not wired yet. "
        "Reuses the structured contract (docs/insights/decisions.md ADR-2, ADR-9)."
    )


def tool_definition(item_keys: list[str]) -> dict:
    """Claude tool-use definition (ADR-3 rules), mirroring feature_history."""
    return {
        "name": "balance_sheet_items",
        "description": (
            "Look up raw balance-sheet line items (dollar amounts) for a company at "
            "a quarter offset from a filing — accounts receivable, payables, "
            "inventory, content assets, debt, cash, deferred revenue. Call this when "
            "a ratio alone can't settle the cause — e.g. to test whether depressed "
            "cash conversion is a working-capital swing (are receivables outrunning "
            "payables?) or a non-cash charge (are content assets being amortized "
            "down?). Returns one result per requested item, each with provenance."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Company ticker, e.g. 'WBD'."},
                "report_date": {
                    "type": "string",
                    "format": "date",
                    "description": "The anchor filing's report date, ISO 8601 (YYYY-MM-DD).",
                },
                "period_offset": {
                    "type": "integer",
                    "description": "Signed quarter offset: +1 = next quarter, -1 = prior, 0 = the filing's own quarter.",
                },
                "items": {
                    "type": "array",
                    "items": {"type": "string", "enum": item_keys},
                    "description": "Which balance-sheet line items to return, e.g. ['accounts_receivable', 'accounts_payable'].",
                },
            },
            "required": ["ticker", "report_date", "period_offset", "items"],
            "additionalProperties": False,
        },
    }


def parse_model_input(model_input: dict) -> dict:
    """Seam (a): model JSON -> kwargs (report_date str -> date; `items` array)."""
    return {
        "ticker": model_input["ticker"],
        "report_date": date.fromisoformat(model_input["report_date"]),
        "period_offset": model_input["period_offset"],
        "items": model_input["items"],
    }
