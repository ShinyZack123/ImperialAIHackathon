# Connect Databricks to the website and models

## What you already did

Notebook 01 generated a controlled synthetic dataset and stored it in four Delta tables in `workspace.gpu_ops_hackathon`. Those tables persist after the notebook closes. You now have shared GPU inventory, time-stamped GPU readings, job progress and cooling readings for the whole team. This step did not train a model, connect physical GPUs or start a continuous monitor.

The SQL warehouse is the compute service that answers queries against those tables. It is not a folder or an additional copy of the data. Find its Warehouse ID under SQL Warehouses → your warehouse → Connection details: it is the final component of the HTTP path.

The integration is:

```text
Synthetic readings saved in Databricks
          ↓
Databricks processing notebook → saved health features and rule evaluations
          ↓
Local Python backend ↔ investigating model's evidence tools
          ↓
Coded website based on your partner's Figma design
```

## 1. Sign in from your computer

Install Python 3.11+ and the [official Databricks CLI](https://docs.databricks.com/aws/en/dev-tools/cli/install). From a clone of this repository, open Terminal and run the following, replacing the example host with yours:

```sh
cd databricks
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-backend.txt
cp .env.example .env
# Edit .env with your workspace host, catalog/schema and Warehouse ID.
databricks auth login --host https://your-workspace.cloud.databricks.com --profile gpu-ops
```

Complete the browser sign-in. This is separate from signing into a browser tab: the backend needs its own authenticated session. The official CLI caches credentials locally; don't send tokens to teammates or put them in frontend code. See [Databricks OAuth documentation](https://docs.databricks.com/aws/en/dev-tools/auth/oauth-u2m).

The CLI binary, virtual environment and credentials are not included in Git. `DATABRICKS_CLI_PATH=databricks` uses the official executable on PATH; set an absolute path if you installed it elsewhere. Each teammate needs their own login and access to the same workspace/tables. `requirements-backend.lock` records the versions used during validation; the requirements file gives the supported dependency ranges. On Windows, use `.venv\Scripts\python.exe` in place of `.venv/bin/python`.

## 2. Check a real query

```sh
.venv/bin/python -m backend.check_connection
```

Expected: `"status": "connected"`, four runs, 24 GPUs and an overview at replay time `2026-11-14T09:07:30Z`. It uses data from Databricks, not `examples/dashboard.json`. The warehouse may take time to start. If the check fails, it exits with an error rather than substituting sample data.

## 3. Process the health snapshot

Import `notebooks/02_gpu_health_pipeline.ipynb` using the same Workspace folder menu as notebook 01, open it on Serverless, and select **Run all**. Its default configuration reads your existing schema and the first replay at `2026-11-14T09:07:30Z`.

It computes one row per GPU in `health_features` and six rule evaluations per GPU in `health_alerts`: **24 features and 144 evaluations** for a snapshot. The six rules cover missing/stale GPU telemetry, temperature, power limits, recent new ECC errors, workload stalls and cooling-flow observations. Thresholds are explicitly illustrative baseline rules, ready for your deterministic teammate to improve.

Each evaluation is `active`, `clear` or `unknown`. The GPU/rack, replay time, rule version and supporting observations are saved with it. These are evidence-based flags, not confirmed physical diagnoses. Delta MERGE makes rerunning the same snapshot update those rows rather than duplicate them. The original four tables are not modified.

Keep the run ID and time printed at the end. A different time has no assessment until the pipeline processes it. A Databricks Job can run this notebook with the same four parameters; running it repeatedly with the same time does not simulate new incoming data.

You can also upload and run a versioned copy from Terminal using the authenticated CLI profile:

```sh
.venv/bin/python -m backend.run_health_pipeline \
  --run-id 43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c \
  --as-of-ts 2026-11-14T09:07:30Z
```

This explicitly starts one serverless notebook task and updates the two derived tables. It prints the Databricks run link and observed result, uploads without overwriting an existing notebook, disables automatic task retries, and requests cancellation if its five-minute polling deadline expires. Check that run before resubmitting after an ambiguous network failure. It does not create a recurring schedule. The example run ID belongs to the validated seed-42 demo; use the ID returned by `/api/runs` for your imported data.

## 4. Start the local API

From the same project directory:

```sh
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8010
```

Keep Terminal open. Visit [the API's interactive documentation](http://127.0.0.1:8010/docs). This address refers to the computer running the backend; it is not a shared hosted endpoint.

1. Expand `GET /api/runs`, click **Try it out**, then **Execute**. Copy a returned `run_id`.
2. Expand `GET /api/overview`, enter that run ID and `2026-11-14T09:07:30Z` as `as_of_ts`, then **Execute**.
3. Try `GET /api/racks/{rack_id}` with `rack-b` and the same run/time.
4. Try GPU history with `gpu-001` and the same run/time.
5. Try `GET /api/health-assessments` with the exact run/time processed in notebook 02. `assessment_available: false` means unprocessed, not healthy. Its counts count rule evaluations, not distinct GPUs.

Always use the recorded replay clock, not today's real clock. Queries with a time long after the recorded readings correctly report them as stale/unknown. CORS permits localhost frontends on ports 4397 (the repository preview), 3000 and 5173; update `FRONTEND_ORIGINS` and restart if your local frontend uses a different port.

This is a local development server with no app-user authentication. Before public hosting, add server-side authorization, deployment credentials, rate limits/caching and a deployment configuration. The user's OAuth login is suitable for this local development step.

## 5. Give each teammate the interface

| Teammate | Files / interface | Next action |
|---|---|---|
| Figma / frontend | `examples/frontend-client.ts`, API `/docs`, `contracts/data-contract.md` | Implement the Figma screen as a coded app, then call the API to fill overview, rack and GPU views. Figma design alone does not make these data requests. |
| Deterministic model | Four evidence tables and `health_features`; `health_rules.py` as a baseline | Replace/improve the detector while keeping run, time, units and missing-data semantics consistent. |
| Diagnostic / fine-tuned model | `examples/model_adapter.py`, active evaluations and evidence JSON | Investigate a candidate using bounded GPU/workload histories and rack cooling context; return the agreed incident format. |
| You | Databricks tables, processing notebook/Job, backend configuration | Keep the shared data/API working and validate each consumer at the same replay time. |

The adapter binds the replay cursor in the orchestrator so the model cannot choose a future timestamp while diagnosing a past event. Training/evaluation labels in `data/evaluation/` are not available through these evidence tools.

## A stronger Databricks bounty demonstration

Based on the criteria supplied by your team, merely generating synthetic rows is a thin submission. Show Databricks doing useful work: process mixed-vendor evidence, persist derived health snapshots, rerun without duplicate alerts, and serve those results to the website and investigating model.

Demonstrate a healthy workload increase, a cooling anomaly, missing telemetry and an isolated GPU error. Show what changed, which observations support the flag and what remains unknown. Explain the intended operational benefit—faster investigation and fewer unsupported hardware-failure alarms—without presenting unmeasured savings as results.

The quoted bounty criteria do not specify whether synthetic data is permitted; the team must check the full rules. Clearly label the synthetic demo, and do not treat these criteria alone as a guarantee of eligibility or a score. Real telemetry adapters, production-scale throughput, continuous monitoring and autonomous remediation have not been demonstrated by these fixtures. Their recovery is prerecorded and must not be attributed to the agent.

## Integrating with this repository

The existing website reads the simulated twin in `../src/twin.js`, whose tools use `scenario`, `tick`, `seed`, `policy` and `entityId`. The detector in `../flagging/` consumes its own enriched topology and telemetry scenarios. This module's separate fixture uses `run_id`, `as_of_ts`, `gpu_id` and three illustrative racks. **They are not interchangeable data sources.**

Do not overwrite the team's topology, pool independent runs, fabricate identifier mappings or imply that adding these files has connected the existing website. Agree an adapter into the shared canonical model first:

1. Choose the team's canonical inventory and entity IDs; ingest those IDs unchanged or supply an explicit validated mapping at the connector boundary.
2. Define how the canonical replay clock maps to `run_id` and UTC timestamps, preserving the same cutoff for every query and model tool.
3. Map metrics and units, unknown/freshness semantics and provenance explicitly. Observed GPU power is not facility power or energy.
4. Invoke the detector in `flagging/` on its documented inputs; keep `baseline-v1` assessments labelled as demo scaffolding until that adapter exists.
5. Adapt API responses into the website's view model and feed evidence tools to the diagnostic model. The TypeScript and Python examples illustrate the interface; importing this directory does not perform that wiring.

The API is local and read-only. It does not implement tenant/user authorization, agent inference or hardware control. Existing platform policy and approval checks remain the responsibility of the canonical application.

## Validation boundaries

On 2026-11-14, the authenticated backend read all four replay runs and a 24-GPU overview from the actual workspace. A Databricks serverless processing run completed successfully for run `43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c` at `2026-11-14T09:07:30Z`.

The live HTTP API subsequently returned all 144 saved evaluations: 16 active observations across 8 GPUs in rack-b, 128 clear and 0 unknown. Active conditions were temperature limits and low cooling flow. GPU and workload history endpoints each returned 61 samples for the requested five-minute window. These results validate this synthetic snapshot and connection, not real hardware fault detection. Local integration records are intentionally excluded from Git. Run the connection check and processing task in your own workspace to verify your setup.

```sh
.venv/bin/python -m unittest discover -s tests -v
```

Tests cover generated scenarios, actual query logic with a local SQL emulator, rule outcomes, persistence guards, SDK response handling and HTTP contracts. They do not replace executing the notebooks and reading their results in Databricks. The SDK polling deadline is best effort, not a hard limit on network calls/retries.
