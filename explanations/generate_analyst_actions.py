#!/usr/bin/env python3
"""Generate analyst action briefs for flagged filings.

Reads from the `qqq_finance.filing_intelligence` view (output of Step 6),
calls Claude once per row to produce structured analyst guidance, and writes
results to `qqq_finance.analyst_actions`.

Rows that fail validation are written to `qqq_finance.analyst_actions_rejected`
with the raw LLM response for inspection.

Usage:
    # Batch mode — all ALERT + FLAG + WATCH rows (default)
    python explanations/generate_analyst_actions.py

    # Single filing
    python explanations/generate_analyst_actions.py --ticker PLTR --quarter 2024-Q3

    # Dry run — print prompts without calling Claude
    python explanations/generate_analyst_actions.py --dry-run
"""

import argparse
import io
import json
import os
import sys
import time
import re
from datetime import datetime, timezone

import pandas as pd
from google.cloud import bigquery

sys.path.insert(0, os.path.dirname(__file__))
from analyst_actions_prompt import SYSTEM_PROMPT, build_analyst_actions_prompt

# ── Config ────────────────────────────────────────────────────────────────────

BQ_PROJECT        = "qqq-anomaly-lab"
BQ_DATASET        = "qqq_finance"
SOURCE_VIEW       = f"{BQ_PROJECT}.{BQ_DATASET}.filing_intelligence"
OUTPUT_TABLE      = f"{BQ_PROJECT}.{BQ_DATASET}.analyst_actions"
REJECTED_TABLE    = f"{BQ_PROJECT}.{BQ_DATASET}.analyst_actions_rejected"

MODEL             = "claude-sonnet-4-6"
MAX_TOKENS        = 1024
TEMPERATURE       = 0.0
API_DELAY_SEC     = 0.5

# Tiers to process (WATCH included — produces MONITOR urgency, still useful)
TARGET_TIERS      = ("ALERT", "FLAG", "WATCH")

# Valid urgency tier values — concern levels, not commands
VALID_URGENCY     = {"CRITICAL", "INVESTIGATE", "CONTEXTUAL"}

# Required non-empty string fields in the response
REQUIRED_FIELDS   = ["investigation_path", "key_question", "persistence_test",
                     "priority_section", "urgency_tier"]

# Banned phrases that indicate hallucination or policy violation
BANNED_PATTERNS   = [
    r"\bfraud\b",
    r"\bbuying?\b",
    r"\bselling?\b",
    r"\boverweight\b",
    r"\bunderweight\b",
]

# Valid priority_section values per spec
VALID_PRIORITY_SECTIONS = {
    "Liquidity and Capital Resources",
    "Critical Accounting Estimates",
    "Results of Operations",
    "Cash Flows from Operations",
    "Notes to Financial Statements",
    "Management's Discussion and Analysis",
}


# ── BigQuery helpers ──────────────────────────────────────────────────────────

def load_filings(client: bigquery.Client,
                 ticker: str | None,
                 quarter: str | None) -> pd.DataFrame:
    """Query filing_intelligence for rows to process."""
    tiers_str = ", ".join(f"'{t}'" for t in TARGET_TIERS)
    conditions = [f"conviction_tier IN ({tiers_str})"]

    if ticker:
        conditions.append(f"ticker = '{ticker}'")
    if quarter:
        conditions.append(f"calendar_quarter = '{quarter}'")

    where = " AND ".join(conditions)
    query = f"""
        SELECT *
        FROM `{SOURCE_VIEW}`
        WHERE {where}
        ORDER BY conviction_score DESC, anomaly_score_0_100 DESC
    """
    print(f"Loading from {SOURCE_VIEW}...")
    df = client.query(query).to_dataframe()
    print(f"  {len(df)} rows ({df['conviction_tier'].value_counts().to_dict() if len(df) else 'none'})")
    return df


def _upload(client: bigquery.Client,
            rows: list[dict],
            table_id: str,
            date_col: str = "report_date") -> None:
    """Write rows to a BQ table (WRITE_TRUNCATE — full refresh each run)."""
    if not rows:
        print(f"  No rows to upload to {table_id}.")
        return

    df = pd.DataFrame(rows)
    if date_col in df.columns:
        df[date_col] = pd.to_datetime(df[date_col], utc=True).dt.date

    buf = io.BytesIO()
    df.to_parquet(buf, index=False, engine="pyarrow")
    buf.seek(0)

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.PARQUET,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        time_partitioning=bigquery.TimePartitioning(
            type_=bigquery.TimePartitioningType.DAY,
            field=date_col,
        ),
        clustering_fields=["ticker"],
    )
    job = client.load_table_from_file(buf, table_id, job_config=job_config)
    job.result()
    print(f"  Uploaded {len(df)} rows → {table_id}")


# ── Claude API ────────────────────────────────────────────────────────────────

def call_claude(user_prompt: str) -> dict | None:
    """Call Claude with separate system + user message. Returns parsed JSON or None."""
    try:
        import anthropic
        client = anthropic.Anthropic()
        message = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            temperature=TEMPERATURE,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = message.content[0].text.strip()

        # Strip markdown fences if present
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        return json.loads(text)

    except json.JSONDecodeError as e:
        print(f"  JSON parse error: {e}")
        return None
    except Exception as e:
        print(f"  API error: {e}")
        return None


# ── Validation ────────────────────────────────────────────────────────────────

def validate_response(response: dict) -> tuple[bool, str]:
    """Validate a Claude response against spec rules.

    Returns (is_valid, rejection_reason). rejection_reason is empty if valid.
    """
    # Required fields must be non-empty strings
    for field in REQUIRED_FIELDS:
        val = response.get(field)
        if not val or not isinstance(val, str) or not val.strip():
            return False, f"Missing or empty required field: {field}"

    # urgency_tier must be exact match
    if response.get("urgency_tier") not in VALID_URGENCY:
        return False, f"Invalid urgency_tier: {response.get('urgency_tier')!r} — must be one of {VALID_URGENCY}"

    # priority_section should be a recognised SEC section name
    ps = response.get("priority_section", "")
    if ps not in VALID_PRIORITY_SECTIONS:
        # Warn but don't reject — LLM may use a valid variant
        pass

    # Scan all string fields for banned patterns
    full_text = " ".join(
        str(v) for v in response.values() if isinstance(v, str)
    ).lower()
    for pattern in BANNED_PATTERNS:
        if re.search(pattern, full_text, re.IGNORECASE):
            return False, f"Banned pattern found in response: {pattern!r}"

    return True, ""


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Generate analyst action briefs")
    parser.add_argument("--ticker",   default=None, help="Single ticker (e.g. PLTR)")
    parser.add_argument("--quarter",  default=None, help="Single quarter (e.g. 2024-Q3)")
    parser.add_argument("--dry-run",  action="store_true",
                        help="Print prompts without calling Claude")
    args = parser.parse_args()

    bq_client = bigquery.Client(project=BQ_PROJECT)

    df = load_filings(bq_client, args.ticker, args.quarter)
    if df.empty:
        print("No qualifying filings found.")
        return

    results:  list[dict] = []
    rejected: list[dict] = []
    generated_at = datetime.now(timezone.utc).isoformat()

    for i, (_, row) in enumerate(df.iterrows(), 1):
        ticker  = row.get("ticker", "?")
        quarter = row.get("calendar_quarter", "?")
        tier    = row.get("conviction_tier", "?")
        score   = row.get("conviction_score", 0)
        print(f"\n[{i}/{len(df)}] {ticker} {quarter}  ({tier}, score={score:.1f})")

        user_prompt = build_analyst_actions_prompt(row)

        if args.dry_run:
            print("--- PROMPT (first 600 chars) ---")
            print(user_prompt[:600] + "...")
            continue

        response = call_claude(user_prompt)

        if response is None:
            print(f"  Skipping — API call failed")
            rejected.append({
                "ticker":           ticker,
                "report_date":      str(row.get("report_date", "")),
                "calendar_quarter": quarter,
                "rejection_reason": "API call failed or JSON parse error",
                "raw_response":     "",
                "generated_at":     generated_at,
            })
            time.sleep(API_DELAY_SEC)
            continue

        is_valid, reason = validate_response(response)

        if not is_valid:
            print(f"  REJECTED — {reason}")
            rejected.append({
                "ticker":           ticker,
                "report_date":      str(row.get("report_date", "")),
                "calendar_quarter": quarter,
                "rejection_reason": reason,
                "raw_response":     json.dumps(response),
                "generated_at":     generated_at,
            })
            time.sleep(API_DELAY_SEC)
            continue

        # Accepted
        results.append({
            "ticker":                   ticker,
            "report_date":              str(row.get("report_date", "")),
            "calendar_quarter":         quarter,
            "conviction_tier":          str(row.get("conviction_tier", "")),
            "conviction_score":         float(row.get("conviction_score", 0) or 0),
            "investigation_path":       response["investigation_path"],
            "key_question":             response["key_question"],
            "persistence_test":         response["persistence_test"],
            "priority_section":         response["priority_section"],
            "urgency_tier":             response["urgency_tier"],
            "filing_section_rationale": response.get("filing_section_rationale") or None,
            "model_version":            MODEL,
            "generated_at":             generated_at,
        })
        print(f"  urgency={response['urgency_tier']} | section={response['priority_section']}")
        print(f"  investigation: {response['investigation_path'][:100]}...")

        time.sleep(API_DELAY_SEC)

    # ── Upload ────────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\nDry run complete — {len(df)} prompts previewed.")
        return

    print(f"\n── Results ──────────────────────────────────────────────────────")
    print(f"  Accepted: {len(results)} | Rejected: {len(rejected)}")

    _upload(bq_client, results,  OUTPUT_TABLE)
    if rejected:
        _upload(bq_client, rejected, REJECTED_TABLE)

    # Print urgency distribution
    if results:
        urgency_counts: dict[str, int] = {}
        for r in results:
            u = r["urgency_tier"]
            urgency_counts[u] = urgency_counts.get(u, 0) + 1
        print("\n── Urgency tier distribution ──")
        for tier in ["CRITICAL", "INVESTIGATE", "CONTEXTUAL"]:
            print(f"  {tier:12s}: {urgency_counts.get(tier, 0)}")

    print("\nDone.")


if __name__ == "__main__":
    main()
