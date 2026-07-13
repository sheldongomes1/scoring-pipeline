# qqq-investigator service (5b — live deep-dive)

The request-time half of the Agentic Investigator (ADR-8). The nightly batch
(Step 10) proposes hypothesis branches into `qqq_finance.investigation_branches`;
redink-ui renders them; when a user clicks **Investigate this branch**, this
service runs the full audited loop (`run_branch`: generator + tools + grounding
judge) on that single branch and returns the terminal state, verdict, answer,
and generator-facing evidence.

One source of truth: the loop lives in `src/qqq_scoring/investigator/` — this
service only wires it (same wiring as `scripts/investigate_fanout.py`, with the
REAL BigQuery `feature_history` backend).

## Endpoints

- `GET /health` → `{status, anthropic_key_present}`
- `POST /investigate` — body:

```json
{
  "ticker": "WBD",
  "report_date": "2025-06-30",
  "form": "10-Q",
  "calendar_quarter": "2025Q2",
  "branch_id": "h1",
  "hypothesis": "Cash conversion collapsed due to elevated content amortization",
  "rationale": "…",
  "predicate": "Did non-cash content amortization, rather than working capital, drive the OCF/NI collapse?",
  "flag_summary": null
}
```

`flag_summary` is optional — when absent the service reconstructs it from
`filing_intelligence` + `analyst_actions` (falling back to a generic summary if
BQ is unreachable; the `predicate` is the real steering input either way).

Response (the UI-facing DTO — provenance receipts are never included, ADR-5):

```json
{
  "ticker": "WBD",
  "report_date": "2025-06-30",
  "branch_id": "h1",
  "hypothesis": "…",
  "status": "resolved",             // resolved | inconclusive | abandoned | capped
  "final_text": "…the investigator's closing answer…",
  "verdict": {
    "grounded": true,
    "confirm": "confirmed",          // confirmed | refuted | indeterminate | null
    "open_questions": false,
    "reasoning": "…"
  },
  "tool_calls": 4,
  "iterations": 3,
  "evidence": [
    {"kind": "metric", "feature": "ocf_to_net_income", "status": "found", "value": 0.5, "resolved_report_date": "2025-06-30"},
    {"kind": "narrative", "section": "mdna", "status": "found", "passage": "…"}
  ],
  "flag_summary": "…",
  "elapsed_seconds": 24.3
}
```

## Cost

Every `/investigate` call spends real Opus (generator) + Sonnet (judge) tokens:
**~10–60 s and roughly $0.05–$0.20 per call.** Guards:

- `INVESTIGATOR_MAX_CONCURRENT` (default 2) — extra concurrent requests get 429.
- `INVESTIGATOR_API_TOKEN` (optional) — when set, requests must send the same
  value in an `X-Api-Token` header. Set it on any deployed instance.

## Run locally

```bash
cd scoring-pipeline
pip install -r service/requirements.txt          # or use the repo venv
ANTHROPIC_API_KEY=sk-ant-... uvicorn investigator_api:app --app-dir service --port 8080

curl localhost:8080/health
curl -X POST localhost:8080/investigate \
  -H 'Content-Type: application/json' \
  -d '{"ticker":"WBD","report_date":"2025-06-30","branch_id":"h1",
       "hypothesis":"Content amortization is the driver",
       "predicate":"Did non-cash content amortization drive the OCF/NI collapse?"}'
```

BigQuery access uses Application Default Credentials locally
(`gcloud auth application-default login`); on Cloud Run the attached service
account is picked up automatically (needs BigQuery read on `qqq_finance`).

## Deploy (Cloud Run sketch — user runs these)

```bash
cd scoring-pipeline
gcloud builds submit --project qqq-anomaly-lab \
  --tag us-central1-docker.pkg.dev/qqq-anomaly-lab/services/investigator-api .
# (build context = repo root; Dockerfile at service/Dockerfile — with
#  `gcloud builds submit` use a cloudbuild.yaml or:
#  docker build -f service/Dockerfile -t <tag> . && docker push <tag>)

gcloud run deploy investigator-api \
  --project qqq-anomaly-lab --region us-central1 \
  --image us-central1-docker.pkg.dev/qqq-anomaly-lab/services/investigator-api \
  --set-env-vars ANTHROPIC_API_KEY=sk-ant-...,INVESTIGATOR_API_TOKEN=<shared-secret> \
  --timeout 300 --concurrency 4 --memory 512Mi \
  --no-allow-unauthenticated   # or gate with INVESTIGATOR_API_TOKEN if public
```

Then point redink-ui at it: set `INVESTIGATOR_SERVICE_URL=https://investigator-api-….run.app`
(and `INVESTIGATOR_API_TOKEN` if configured) in redink-ui's environment.
