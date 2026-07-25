"""Tool 2 of the action space: GCS narrative sections (the UNSTRUCTURED tool).

Answers "what did management SAY about X?" by returning the written sections of a
filing (MD&A, risk factors). This is the first tool whose evidence is prose, so it
exercises ADR-2's unstructured half for the first time: no numeric value, a passage
locator instead of a re-runnable query, and SEMANTIC grounding (a model checks the
passage supports the claim — you cannot `==` a paragraph).

Retrieval is SECTION-ADDRESSED, not RAG (ADR-7): one filing's narrative fits in
context, so the tool hands over the named section rather than embedding-searching
for a needle. The contract is identical whether retrieval is a dict lookup today or
a vector search later — RAG can slot behind it unchanged.

STUB: the contract + Claude schema are real and tested; the GCS read is not wired.
"""
from __future__ import annotations

import json
from datetime import date

from .contracts import NarrativeResult

# Canonical section vocabulary (the narrative-tool analogue of feature_keys). The
# real list should be sourced from the narrative schema; hardcoded here until the
# GCS body is wired.
# The real section names in the GCS narrative JSONs (gs://.../narrative/): the
# `sections` dict has exactly these keys. (The fake fixture predates this and uses
# its own names; the canonical list matches production.)
SECTION_KEYS = ["mda", "quantitative_disclosures", "risk_factors"]


def narrative_sections(
    ticker: str,
    report_date: date,
    form: str,
    sections: list[str],
) -> list[NarrativeResult]:
    """Return one NarrativeResult per requested section of the identified filing.

    The agent chooses ticker/report_date/form/sections at run-time (ADR-1). Section
    names come from the filing's own structure — the retrieval is a lookup by label,
    not a search, so provenance is a crisp address (this section of this filing),
    the cleanest possible grounding handle.
    """
    raise NotImplementedError(
        "narrative_sections contract is stubbed; GCS narrative read not wired yet. "
        "Contract standard: docs/insights/decisions.md (ADR-2, ADR-7)."
    )


def tool_definition(section_keys: list[str]) -> dict:
    """Build the Claude tool-use definition for narrative_sections (ADR-3 rules).

    Same four rules as feature_history: inputs-only (provenance never exposed),
    `strict:true`, enum sourced from the canonical `section_keys`, prescriptive
    description. Only the input SHAPE differs — this is what ADR-2 predicted."""
    return {
        "name": "narrative_sections",
        "description": (
            "Retrieve the written sections of a company's filing (management's own "
            "words) — e.g. the MD&A or risk factors. Call this when you need to "
            "check what management SAID about a topic, to compare their narrative "
            "against the numbers — e.g. to see whether they acknowledged a cash-flow "
            "problem the ratios reveal. Returns the passage for each requested "
            "section, each carrying provenance for grounding."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "ticker": {"type": "string", "description": "Company ticker, e.g. 'AAPL'."},
                "report_date": {
                    "type": "string",
                    "format": "date",
                    "description": "The filing's report date, ISO 8601 (YYYY-MM-DD).",
                },
                "form": {"type": "string", "description": "Filing form, e.g. '10-Q' or '10-K'."},
                "sections": {
                    "type": "array",
                    "items": {"type": "string", "enum": section_keys},
                    "description": "Which named sections to return, e.g. ['mdna'].",
                },
            },
            "required": ["ticker", "report_date", "form", "sections"],
            "additionalProperties": False,
        },
    }


# --- Phase-2 adapters (same seams as feature_history) -----------------------


def parse_model_input(model_input: dict) -> dict:
    """Seam (a): model JSON -> kwargs for the callable (report_date str -> date)."""
    return {
        "ticker": model_input["ticker"],
        "report_date": date.fromisoformat(model_input["report_date"]),
        "form": model_input["form"],
        "sections": model_input["sections"],
    }


def to_model_content(results: list[NarrativeResult]) -> str:
    """Generator-facing serialization (ADR-5). The passage IS the payload the model
    reasons on (the prose analogue of `value`), so it is shown; the receipts
    (source path, retrieved_at) are withheld for the judge."""
    return json.dumps(
        [
            {
                "section": r.section,
                "status": r.status.value,
                "passage": r.passage,
            }
            for r in results
        ]
    )
