# Connect Databricks to the website and models

## What Databricks does in this project

Notebook 01 creates a controlled synthetic dataset in four Delta tables: `inventory`, `telemetry`, `workloads` and `cooling`. These tables persist after the notebook closes and give the team a shared source of GPU inventory, readings, job progress and cooling evidence.

The SQL warehouse is the compute service that queries those tables; it is not a folder or another copy of the data. The connected website now asks the warehouse for evidence and sends it through the team's actual deterministic detector. The result is saved back to Databricks before it appears in the website.

```text
Databricks: inventory + telemetry + workloads + cooling
          ↓ one run UUID, explicit UTC cursor, at most 15 minutes
Local Python backend → validated connector adapter
          ↓
flagging.flagger.flag_window (the sole official detector)
          ↓
Databricks: official_detector_results (confirmed Delta MERGE)
          ↓
Same-origin Node proxy → website Databricks tab
          ↘ cursor-bound evidence tools for a future diagnostic model
```

Each review evaluates the selected replay time. No continuous monitor, diagnostic LLM inference or GPU-control action is started by this flow. All displayed data is synthetic, including the NVIDIA, AMD and Intel labels.

## 1. Configure access and sign in

Use Python 3.11+, Node.js 22.18+ and the official Databricks CLI. Import and run `notebooks/01_gpu_data_starter.ipynb` if the shared evidence tables do not exist yet; see [README.md](README.md#start-in-databricks).

From the repository root:

```sh
cd databricks
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-backend.txt
cp .env.example .env
# Edit .env with your workspace host, catalog/schema and Warehouse ID.
databricks auth login --host https://your-workspace.cloud.databricks.com --profile gpu-ops
```

Find the Warehouse ID under **SQL Warehouses → your warehouse → Connection details**. It is the final component of `/sql/1.0/warehouses/<warehouse-id>`. Keep the host and warehouse configuration in your local `.env`; do not commit credentials or send an API token to the browser.

Complete the browser sign-in. The backend needs this separate local CLI session even if you are already signed into the Databricks website. Each teammate needs their own login and access to the same workspace, SQL warehouse, catalog and schema. The backend needs permission to use the warehouse, `SELECT` on the evidence, `CREATE TABLE` in the selected schema for first setup, and `MODIFY` on `official_detector_results`; audit readback also needs `SELECT` on that table. Use the team's shared grants rather than passing around one person's credentials.

`DATABRICKS_CLI_PATH=databricks` uses the executable on PATH; set its absolute path locally if necessary. The CLI binary, virtual environment and credentials are excluded from Git. `requirements-backend.lock` records the versions used during validation. On Windows, use `.venv\Scripts\python.exe` instead of `.venv/bin/python`.

## 2. Check a real evidence query

From `databricks/`:

```sh
.venv/bin/python -m backend.check_connection
```

For the default fixture, expect `"status": "connected"`, four runs and 24 GPUs. This reads Databricks rather than `examples/dashboard.json`. A warehouse may need time to start. A failure produces an error, with no sample-data fallback. This check verifies evidence reads; official-result write permissions are exercised when loading a snapshot below.

## 3. Start the Python API and website

Keep the Python API running in the first terminal, from `databricks/`:

```sh
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8010
```

In a second terminal, from the repository root:

```sh
DATABRICKS_API_URL=http://127.0.0.1:8010 npm run dev
```

Open http://127.0.0.1:4397/#databricks. `DATABRICKS_API_URL` is a server-side setting used by the Node preview; it defaults to `http://127.0.0.1:8010`. If Python uses a different port, change that value before starting the preview. The browser calls `/api/databricks/...` on the website's own origin, and Node forwards only the allowed routes to Python. No Databricks token is exposed to frontend code.

1. Select a replay UUID from the **Databricks replay** menu. Runs are independent and must not be pooled.
2. Select a UTC replay time and choose **Load snapshot**. The default is 10 minutes 45 seconds after the run starts, capped at its final sample.
3. Inspect the observed totals and three native racks. Click a GPU to inspect power, temperature, utilisation or clock history, plus its latest job and cooling context.
4. Review **Deterministic detector findings** and their evidence window. **Assessment saved in Databricks** appears only after the write succeeds.
5. Change the cursor and load again to evaluate another window. Returning to **Overview** restores the existing simulated twin and its separate scenario controls.

The Databricks tab has 24 GPUs across three racks. The original twin has 32 GPUs across four racks. No invented ID mapping joins them. Simulation controls are unavailable in Databricks mode; the original twin's tools continue to operate on simulated data.

Use the recorded clock rather than today's clock: readings become stale/unknown if a cursor is too far beyond them. Missing/stale latest telemetry is unknown, not zero or healthy. The detector's states summarise findings anywhere in the selected analysis window, so a historical flag can remain visible after a latest reading recovers. Its 300-second persistence requirement can also produce no flags during a short anomaly. No finding is not a hardware-health certificate.

If loading fails, the website shows an explicit error rather than another data source. Check the backend terminal, CLI login, warehouse access and table permissions. A failed or ambiguous write is not reported as saved. The query and write pollers have bounded deadlines and best-effort cancellation; underlying SDK network calls/retries can extend wall time.

## 4. Use the API and model adapter

The local API documentation is at http://127.0.0.1:8010/docs. The addresses below refer to the machine running the backend, not a shared hosted service.

| Interface | Purpose |
|---|---|
| `GET /api/databricks/runs` | Available replay UUIDs and start/end times |
| `GET /api/databricks/snapshot?run_id=...&as_of_ts=...` | Read evidence, run the official detector, save and return the snapshot |
| `GET /api/databricks/gpus/{gpu_id}/history` | GPU history using the same `run_id` and `as_of_ts`; `window_minutes` defaults to 5, maximum 15 |
| `GET /api/databricks/gpus/{gpu_id}/workload-history` | Job progress history under the same replay cutoff |
| `GET /api/overview`, `/api/racks/{rack_id}` | Additional bounded evidence queries for model tools |
| `GET /api/health-assessments` | Saved six-rule **comparison baseline**, explicitly `official: false`; not the official snapshot endpoint |

Although the official snapshot uses GET for the local demo, it computes an assessment and writes its audit result to Databricks. The source tables are unchanged. Do not describe the whole backend as read-only.

`examples/model_adapter.py` gives the diagnostic-model teammate an `InvestigationEvidence` object bound to one run/cursor. `.health_assessments()` calls the same `OfficialDetector` service as the website, including persistence, and returns the full snapshot dictionary. Read candidate observations from `result["incidents"]`. `.gpu_history()`, `.workload_history()` and `.rack_context()` retrieve supporting evidence. `.comparison_baseline()` is for tests/comparison only and must not generate official escalations.

The orchestrator chooses the cursor, so the model cannot query future replay data through these bound methods. Evaluation labels are excluded. The evidence adapter is connected, but the team still needs to invoke its diagnostic/fine-tuned model and validate any proposed explanation. No inference runs automatically when a user loads the website.

Maintenance, interventions, workload phase, calibrated baselines and throttle reasons are absent from the source tables. A detector message must be interpreted with these coverage limits; those explanations have not been excluded. Generic GPU clocks do not establish domain-specific SM/memory clock behavior. Cooling flow stays percent of nominal, and job progress stays synthetic work units. See the [contract](contracts/data-contract.md) for the complete semantics.

## 5. Inspect saved official results

`official_detector_results` contains one result per `(run_id, as_of_ts, detector_version, evidence_sha256)`. It stores the full snapshot JSON, incident count and generation time. The version fingerprints the flagger, its configuration and the connector adapter. The evidence hash binds the result to the exact bounded source records. Repeating the same inputs updates that record; a changed detector or evidence creates a distinct audit record.

Use `DatabricksData.official_results(run_id, as_of_ts)` or `sql/official_results.sql` to read saved results at an exact cursor. The write is confirmed before API success. It does not overwrite source readings or claim that a physical repair happened.

For the validated seed-42 fixture, run `43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c` at `2026-11-14T09:10:45Z` produces one incident rooted at `zone-b`. Use the run IDs actually returned by your workspace; a differently generated dataset can differ.

## Optional: compare the six baseline rules

Notebook 02 (`notebooks/02_gpu_health_pipeline.ipynb`) computes `health_features` with Spark SQL and saves six `baseline-v1` evaluations per GPU to `health_alerts` using Delta MERGE: 24 feature rows and 144 evaluation rows for one cursor. These cover stale data, temperature, power, ECC changes, job stalls and cooling-flow observations. They are explicitly comparison scaffolding, not official alerts.

Import the notebook or run a one-off serverless task from `databricks/`:

```sh
.venv/bin/python -m backend.run_health_pipeline \
  --run-id 43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c \
  --as-of-ts 2026-11-14T09:07:30Z
```

This uploads a versioned notebook without replacing an existing one, starts one task, updates only the two comparison tables and reports the result. It does not create a recurring schedule. Check the run before retrying an ambiguous network failure. The official website path does not require this notebook, and no official-service error triggers a baseline fallback.

The baseline can disagree with the official detector because the algorithms and time/context requirements differ. Do not present its count of rule evaluations as a count of official incidents or affected GPUs.

## Integrating with this repository

`../public/databricks.js` consumes the API envelope and renders native source inventory. `backend/flagging_adapter.py` translates metric names and units at the connector boundary, preserving `gpu_id`, `rack_id`, `server_id`, `cooling_zone_id`, replay UUID and UTC cutoff. It does not load the detector's separate scenario fixtures or answer keys. `backend/official_detection.py` owns the read → detect → persist sequence, and `backend/result_store.py` owns the validated result write.

The existing simulated twin in `../src/twin.js` and `../flagging/bridge.py` still uses its own `scenario`, `tick`, `seed`, `policy` and console-entity mapping. Neither that scenario data nor the baseline six-rule table is substituted for Databricks evidence. The diagnostic evidence adapter shares the official path; diagnostic inference and an incident ownership/acknowledgement lifecycle remain future work.

This local development server has no application-user authentication. Before shared cloud hosting, add server-side authorization, deployment credentials, rate limits/caching and deployment configuration, and grant the deployed principal access to the shared warehouse and evidence/result tables. Local personal OAuth sessions do not authorize other teammates or a hosted service. CORS origins for direct development clients are configured through `FRONTEND_ORIGINS`; the existing website uses the same-origin proxy.

## Demonstrating the Databricks work

Show the full data path: persistent mixed-vendor evidence, a bounded warehouse query, the actual deterministic detector, a saved official assessment with provenance, and the website consuming it. Notebook 02 adds a separate Spark SQL comparison pipeline. Demonstrate uncertainty and missing readings alongside the anomaly, rather than claiming every unusual reading is a confirmed hardware failure.

The intended benefit is faster, traceable investigation of GPU and shared-resource observations. Real telemetry collection, production throughput, continuous monitoring, diagnostic LLM inference, measured operational savings and autonomous remediation have not been demonstrated. Recovery in these fixtures is prerecorded and must not be attributed to an agent. Check the full bounty rules with the organisers; this implementation is not a guarantee of eligibility or score.

## Validation boundaries

On 2026-11-14 the authenticated integration read all four runs from Databricks. For run `43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c` at `2026-11-14T09:10:45Z`, the official path read 24 inventory rows, 3,120 telemetry rows, 3,120 workload rows and 390 cooling rows within its bounded window. It returned one `zone-b` incident for the 24-GPU fleet, saved the official result, and a readback matched the API snapshot. This verifies that synthetic replay and connection, not physical fault detection.

The other three cloud replays also saved official results: the healthy fixture had zero incidents, the telemetry-gap fixture had one incident and one unknown GPU, and the isolated-error fixture had one incident. Headless browser checks confirmed the four-replay selector, 24 GPUs/three racks, saved `zone-b` finding, GPU selection and power chart, and return to Overview. There were no JavaScript errors or horizontal overflow at desktop (1440px) and mobile (390px) widths.

The integration validation passed the Python suite in `databricks/tests` and 24 Node tests. Run them from their respective directories:

```sh
# From databricks/
.venv/bin/python -m unittest discover -s tests -v
```

```sh
# From the repository root
npm test
npm run build
```

Tests cover bounded evidence, units/identity/freshness validation, the actual detector adapter, saved-result provenance and failure handling, HTTP/proxy contracts, and frontend data handling. Local SQL emulation and mocks do not replace live Databricks checks. Verification records containing local account/configuration details are intentionally excluded from Git.
