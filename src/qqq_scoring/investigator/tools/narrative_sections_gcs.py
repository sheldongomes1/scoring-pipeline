"""REAL GCS backend for `narrative_sections` — makes the second tool production-real.

Drop-in for `narrative_sections_fake`: same signature, same `NarrativeResult`.
Reads the pre-extracted narrative JSON for a filing from
`gs://qqq-anomaly-raw-sg/qqq/narrative/{TICKER}/...` and returns the requested
named sections (SECTION-ADDRESSED, ADR-7 — the whole named section, no embeddings).

Grounding for narrative is SEMANTIC (the judge's answer-support head reads the
passages), so this backend needs no `reverify` entry — it just returns real prose.

The eval showed the fake returned `filing_not_found` for every real ticker (17/17),
starving investigations; this backend gives the agent actual management prose.
"""
from __future__ import annotations

import json
import threading
from datetime import date, datetime, timezone

from .contracts import NarrativeProvenance, NarrativeResult, NarrativeStatus

_BUCKET = "qqq-anomaly-raw-sg"
_PREFIX = "qqq/narrative"
SOURCE = "gs://qqq-anomaly-raw-sg/qqq/narrative"

# Module-level shared client (latency #3): a fresh storage.Client() per call throws
# away auth/discovery each time. Built lazily+once, thread-safe, and only when no
# client is injected (tests still pass their own fake untouched).
_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _shared_client():
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                from google.cloud import storage  # lazy import so the module loads without creds

                _CLIENT = storage.Client()
    return _CLIENT


def _find_blob(bucket, ticker: str, report_date: date, form: str):
    """Locate the filing's narrative JSON. 10-Q filenames embed the report_date;
    10-K filenames don't — so match by form + (report_date for 10-Q)."""
    formkey = form.replace("-", "")          # "10-Q" -> "10Q"
    rd = report_date.isoformat()
    blobs = list(bucket.list_blobs(prefix=f"{_PREFIX}/{ticker}/"))
    for b in blobs:
        name = b.name
        if formkey not in name:
            continue
        if formkey == "10K" or rd in name:    # 10-K has no report_date in the name
            return b
    return None


def narrative_sections_gcs(
    ticker: str,
    report_date: date,
    form: str,
    sections: list[str],
    *,
    client=None,
) -> list[NarrativeResult]:
    """Drop-in for `narrative_sections_fake` — reads real GCS narrative JSON."""
    client = client or _shared_client()
    bucket = client.bucket(_BUCKET)
    retrieved_at = datetime.now(timezone.utc)

    blob = _find_blob(bucket, ticker, report_date, form)
    filing_sections = None
    source_path = f"{SOURCE}/{ticker}/"
    if blob is not None:
        source_path = f"gs://{_BUCKET}/{blob.name}"
        try:
            filing_sections = json.loads(blob.download_as_text()).get("sections", {})
        except Exception:
            filing_sections = None

    results: list[NarrativeResult] = []
    for section in sections:
        prov = NarrativeProvenance(
            source=source_path, ticker=ticker, report_date=report_date,
            form=form, section=section, retrieved_at=retrieved_at,
        )
        if filing_sections is None:
            results.append(NarrativeResult(section, NarrativeStatus.FILING_NOT_FOUND, None, prov))
            continue
        val = filing_sections.get(section)
        if val:
            passage = val if isinstance(val, str) else json.dumps(val)
            results.append(NarrativeResult(section, NarrativeStatus.FOUND, passage, prov))
        else:
            results.append(NarrativeResult(section, NarrativeStatus.SECTION_ABSENT, None, prov))
    return results
