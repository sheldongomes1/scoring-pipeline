"""In-memory fixture backend for `narrative_sections` (Phase-2 / ADR-7 proof).

Section-addressed lookup over canned filing prose. The passages are written so
SEMANTIC grounding can actually fail: AAPL's MD&A frames its cash-flow dip as a
TEMPORARY timing effect; WBD's MD&A admits SUSTAINED pressure with no near-term
recovery. An investigator that mischaracterises either (e.g. "AAPL management
admitted structural weakness") should be caught by the judge's semantic G check,
because the passage does not support the claim.

Swapping this for the real GCS read touches ZERO loop/judge logic — same signature,
same NarrativeResult return type (the ADR-7 seam).
"""
from __future__ import annotations

from datetime import date, datetime, timezone

from .contracts import NarrativeProvenance, NarrativeResult, NarrativeStatus

_SOURCE_TMPL = "gs://qqq-anomaly-raw-sg/qqq/narrative/{t}/{t}_{y}_{f}_narrative.json"

# (ticker, report_date, form) -> {section: passage}. Mirrors the numeric fixture's
# stories so a single investigation can compare "said" vs "showed".
_DATA: dict[tuple[str, date, str], dict[str, str]] = {
    ("AAPL", date(2025, 6, 30), "10-Q"): {
        "mdna": (
            "Operating cash flow this quarter was affected by a temporary increase in "
            "working capital, primarily a build in receivables tied to the timing of "
            "large enterprise shipments near quarter-end. Management expects this to "
            "normalize as collections occur in the following quarter; underlying demand "
            "and earnings quality remain intact."
        ),
        "risk_factors": (
            "The Company is exposed to foreign-exchange volatility and to concentration "
            "in a limited number of component suppliers, either of which could affect "
            "results in future periods."
        ),
    },
    ("WBD", date(2025, 6, 30), "10-Q"): {
        "mdna": (
            "Operating cash flow remained under pressure during the quarter, reflecting "
            "continued softness in linear advertising and elevated content amortization. "
            "Management does not expect a near-term recovery in cash conversion and is "
            "prioritizing debt reduction over the coming quarters."
        ),
        # risk_factors deliberately ABSENT for WBD to exercise SECTION_ABSENT.
    },
}


def narrative_sections_fake(
    ticker: str,
    report_date: date,
    form: str,
    sections: list[str],
) -> list[NarrativeResult]:
    """Drop-in fake for `narrative_sections` — same signature and return type."""
    retrieved_at = datetime.now(timezone.utc)
    filing = _DATA.get((ticker, report_date, form))
    results: list[NarrativeResult] = []

    for section in sections:
        prov = NarrativeProvenance(
            source=_SOURCE_TMPL.format(t=ticker, y=report_date.year, f=form.replace("-", "")),
            ticker=ticker,
            report_date=report_date,
            form=form,
            section=section,
            retrieved_at=retrieved_at,
        )
        if filing is None:
            results.append(NarrativeResult(section, NarrativeStatus.FILING_NOT_FOUND, None, prov))
        elif section in filing:
            results.append(NarrativeResult(section, NarrativeStatus.FOUND, filing[section], prov))
        else:
            # The filing exists but omits this section — not the same as no filing.
            results.append(NarrativeResult(section, NarrativeStatus.SECTION_ABSENT, None, prov))

    return results
