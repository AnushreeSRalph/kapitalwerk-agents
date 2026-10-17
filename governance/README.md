# KapitalWerk NXT

KapitalWerk NXT is a governed multi-agent financial intelligence platform for SME banking. It gives relationship managers one explainable view of payment inflows, risk signals, AML review, customer engagement opportunities, and AI governance telemetry.

Prepared for the Google Cloud AI Builder Cup by **Pixel Perfect**.

## What the platform does

KapitalWerk NXT processes customer transaction snapshots through a LangGraph workflow:

1. Ingestion normalizes transaction files and writes a dated snapshot.
2. Inflow analysis detects missing payments, deviations, and high-value transactions.
3. Risk analysis assigns a portfolio risk tier and applies the PII guardrail.
4. AML analysis creates regulatory narratives for escalated cases.
5. Reporting creates dashboard, reminder, and case artifacts.
6. Marketing creates a data-backed postal-code opportunity heatmap.
7. Governance records LLM calls, token usage, cache hits, latency, and fail-closed decisions.

The dashboard reads the newest completed snapshot for each customer and does not wait for a new daily pipeline run to finish.

## Production architecture

~~~text
Dashboard UI
    |
Cloud Run API and pipeline service
    |
LangGraph orchestrator
    |
Ingestion -> Inflow -> Risk -> AML when required
                     |       |
                     +-> Reporting -> Marketing -> Reminders
    |
GovernanceMiddleware
    |
Vertex AI Gemini 2.5 Flash
    |
Cloud Storage snapshots + Firestore cache and audit log
~~~

- **Cloud Run** hosts the API/UI service and pipeline agent service.
- **Cloud Storage** stores source data and dated pipeline artifacts.
- **Firestore** stores the shared semantic cache and immutable governance audit records.
- **Vertex AI Gemini 2.5 Flash** provides production LLM reasoning.
- **Workload identity and service accounts** provide keyless cloud authentication.

## Local development

Local development uses the same API and UI contracts:

- Transaction artifacts are read from the local bucket_output directory.
- Ollama can provide the local model, for example llama3.
- Local governance can use JSONL audit storage and a local cache.
- Production uses Vertex Gemini, GCS, and Firestore.

Install:

~~~bash
pip install -r requirements.txt
~~~

Run locally with Ollama:

~~~bash
export LLM_PROVIDER=ollama
export OLLAMA_MODEL=llama3
export BUCKET_BACKEND=local
export BUCKET_ROOT=./bucket_output

ollama run llama3
uvicorn api.main:app --reload --port 8080
~~~

Open http://localhost:8080/.

Run the pipeline manually:

~~~bash
cd orchestrator
RUN_ALL_PERSONAS=1 python3 run_orchestrator.py
~~~

Useful flags:

- RUN_ALL_PERSONAS=1 processes all customers.
- FORCE_RUN=1 reruns a snapshot even when that date already exists.
- RUN_DATE=YYYY-MM-DD runs or backfills a specific date.
- SKIP_STARTUP_PIPELINE_RUN=1 prevents the API startup background run.

## Data and snapshot selection

Each customer snapshot is stored under:

~~~text
customer_id/run_date/
  normalized_transactions.json
  historical_stats.json
  inflow_flags.json
  top_risk_report.json
  marketing_heatmap.json
  reminders_*.json
~~~

The API selects the latest complete snapshot and ignores in-progress folders. The UI keeps showing the previous valid data while a new daily run is processing.

Configured customers:

- glucometer-vendor
- gym-chain
- boho-house

## Production configuration

Cloud Run must receive these environment variables:

~~~text
BUCKET_BACKEND=gcs
GCS_BUCKET_NAME=kapitalwerk-nxt-gcs
LLM_PROVIDER=vertex
GEMINI_MODEL=gemini-2.5-flash
GOVERNANCE_STORE=firestore
FIRESTORE_PROJECT=hack-team-chasing-alpha
PUBLIC_API_BASE_URL=https://YOUR_API_CLOUD_RUN_HOST/api
~~~

FIRESTORE_DATABASE may be omitted. The application uses Firestore's default database automatically.

No Gemini API key belongs in the public repository or frontend JavaScript. Vertex AI uses the Cloud Run service account and ADC.

The Cloud Run service account needs Vertex AI User, GCS object read/write access for the configured bucket, and Firestore read/write access. The API and agent services must use the same project, bucket, Firestore database, and governance collection settings.

PUBLIC_API_BASE_URL is a runtime-only deployment variable. Do not put the deployed URL in the public repository or in the bundled HTML. If the UI is served by the API service, it can be omitted and same-origin routing is used.

## Governance and safety

Every model interaction passes through GovernanceMiddleware.

- PII detection fails closed.
- AML and marketing calls are blocked when upstream PII is detected.
- Blocked decisions are still recorded.
- Semantic cache keys use normalized prompt hashes.
- Call and token budgets are enforced per agent.
- Local audit storage uses immutable JSONL.
- Production audit and cache storage use Firestore.

The UI exposes completed LLM calls, PII blocks, cache hit rate, per-agent token counts, latency, stop reasons, and audit trace IDs.

## API endpoints

- GET / — dashboard UI
- GET /api/personas — customer metadata and latest snapshot information
- GET /api/{persona}/dashboard — complete dashboard payload
- GET /api/{persona}/counterparties — full counterparty ledger
- GET /api/{persona}/cases — cases awaiting review
- GET /api/{persona}/marketing-heatmap — persisted heatmap
- GET /api/{persona}/reminder-preview — reminder preview
- POST /api/{persona}/cases/{case_id}/acknowledge — acknowledge a case
- POST /api/{persona}/cases/{case_id}/escalate — governed Risk and AML review
- GET /api/governance/audit-summary — governance totals
- GET /api/governance/audit-log — detailed audit events
- POST /api/run — invoke the pipeline agent service

## Deployment verification

After deploying to Cloud Run:

~~~bash
curl https://YOUR_CLOUD_RUN_URL/api/personas
curl https://YOUR_CLOUD_RUN_URL/api/glucometer-vendor/dashboard
curl https://YOUR_CLOUD_RUN_URL/api/governance/audit-summary
~~~

The dashboard should show the latest completed GCS snapshot immediately. The daily pipeline can run independently and the UI will pick up the new snapshot after completion.

## Prototype benchmark

The submitted prototype deck reports:

- End-to-end pipeline run: **45–90 seconds**
- Tokens per run: **approximately 12k–18k**
- Repeat-run cache hit rate: **approximately 85%–95%**
- Escalate-to-AML narrative latency: **approximately 1.2–2.5 seconds**
- Fallback: local CSV and local cache when Cloud Storage or Firestore is unavailable

These are benchmark claims from the submission deck. The live audit trail reports observed values for the current deployment and should be used for a new measured benchmark.

## Tests

~~~bash
for d in ingestion_agent inflow_agent risk_agent aml_agent governance reporting_agent marketing_agent; do
  (cd "$d" && python3 -m pytest -q)
done
~~~

## Repository map

- api/main.py — FastAPI service and dashboard hosting
- standalone-ui.html — bundled dashboard UI
- orchestrator/graph.py — LangGraph workflow
- orchestrator/nodes.py — pipeline nodes and telemetry
- orchestrator/run_orchestrator.py — local and daily batch runner
- governance/ — provider, guardrails, cache, token metering, and audit stores
- ingestion_agent/ — input normalization and snapshot creation
- inflow_agent/ — rolling baseline and anomaly detection
- risk_agent/ — risk and PII fail-closed analysis
- aml_agent/ — regulatory narrative generation
- marketing_agent/ — postal-code opportunity analysis
- reporting_agent/ — exports, reminders, and dashboard data
- config.json — non-secret project and deployment metadata

## Submission links

Add the final public GitHub repository, Cloud Run URL, and three-minute demo video link before submission.