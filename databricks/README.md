# GPU operations: Databricks integration

This module connects the hackathon's Databricks evidence tables to the operator console and the team's deterministic detector. It contains the evidence notebook, bounded SQL queries, connector adapter, local HTTP API and persisted official results.

The fixture contains synthetic data for 24 GPUs (8 each labelled NVIDIA, AMD and Intel), three racks and four independent replays. All hardware models and limits are illustrative. It does not connect to physical GPUs or establish cross-vendor hardware compatibility.

**Connected path:** the website's **Databricks** tab requests a replay UUID and UTC cursor. `OfficialDetector` reads at most 15 minutes of evidence, translates source fields through `backend/flagging_adapter.py`, invokes `flagging.flagger.flag_window`, and saves the result in `official_detector_results` before returning it. Native IDs such as `gpu-001` and `rack-a` are preserved. The original 32-GPU twin and its scenario mode remain separate.

**Saved replay path (default after cloning):** `npm run dev` from the repository root reads the included Databricks export and runs the current detector locally. No Databricks account, credentials, Python virtual environment or pip install is needed. The website shows **Saved Databricks replay**, the export timestamp, and **Assessment computed locally · not written to Databricks**. Node.js 22.18+ and Python 3.9+ on PATH are required.

`flagging/flagger.py` is the sole official detector for both paths. Notebook 02's six baseline rules are for comparison only and never replace a failed detector or populate official website alerts. Diagnostic LLM inference, continuous monitoring and hardware actions remain unconnected. [CONNECT.md](CONNECT.md) explains the default teammate quickstart and the optional authenticated backend. A configured connected backend does not fall back to saved evidence on errors.

## Start in Databricks

This setup is only required to query or update the workspace directly. Teammates using the default saved replay can skip it.

Use a Databricks workspace and SQL warehouse that your account can access. Configure the host, catalog, schema and warehouse ID in a local `.env` copied from `.env.example`.

1. Open **Workspace**, open your personal folder, then choose **Import** from its menu.
2. Upload `notebooks/01_gpu_data_starter.ipynb`. The notebook includes its generator and SQL; do not upload the CSVs separately.
3. Open the imported notebook and select **Serverless** compute if prompted.
4. Read the first cells. Keep `SCHEMA = "gpu_ops_hackathon"` if that name is unused; select a catalog you can write to if the current catalog is not writable.
5. Choose **Run all**. It creates four Delta tables, checks their counts and displays query results. Existing identical tables are reused; different data is not overwritten.
6. Open **Catalog**, expand your selected catalog and `gpu_ops_hackathon`, and check `inventory`, `telemetry`, `workloads` and `cooling`.

Expected rows for the default four-scenario dataset:

| Table | Rows | Purpose |
|---|---:|---|
| inventory | 24 | Vendor, rack, server, cooling zone and demo limits |
| telemetry | 17,280 | Time-stamped GPU readings |
| workloads | 17,280 | Whether a job is running and whether it is progressing |
| cooling | 2,160 | Cooling-zone inlet temperature and flow |

If creation fails with a permissions error, select a catalog/schema you can write to. Do not change account permissions or create paid compute just to run this notebook. Partial runs can be resumed: matching tables are checked and reused.

If an older imported version reports `[UNSUPPORTED_OPERATION] errorifexists is not supported`, replace the entire failed persistence cell with `notebooks/replace_failed_cell.py`, then run that cell and the cells below it. The updated notebook uses SQL `CREATE TABLE ... USING DELTA AS SELECT` instead of that DataFrame write mode. It still refuses to overwrite different existing data. Do not switch to `overwrite` or delete the tables to fix this error.

## What to give your teammates

- **Figma / frontend:** the implemented Databricks tab in `../public/databricks.js` uses the official snapshot and GPU history endpoints. `contracts/data-contract.md` describes those responses. `examples/dashboard.json` is an older static query fixture, not an official detector result or live response.
- **Deterministic detector:** `backend/flagging_adapter.py` supplies the existing `flagging.flagger.flag_window` with validated inventory, metrics and workload context. Changes to the detector must preserve units, evidence cutoffs and missing-data semantics.
- **Fine-tuning / diagnostic model:** the eight `tool_*` Unity Catalog functions in [lakehouse/](lakehouse/README.md) are the agent's tools: each takes the replay time and cannot return later samples. In Python, `examples/model_adapter.py` exposes the same cursor-bound evidence. To compare a model's scheduling decisions with the heuristic and the optimiser, add its rows to `policy_runs` with `policy_kind` `llm_base` or `llm_finetuned`; `policy_uplift` pairs them by scenario and seed. `.comparison_baseline()` is for testing only.
- **You:** keep the shared tables, warehouse access and connected backend working. Each connected backend operator needs their own server-side login plus permission to read evidence and create/modify the official result table. Teammates using the saved replay need none of those credentials. Never send an API token to the browser or put one in Git.

The investigating model should receive retrieved observations, never the scenario name or correct answer. Ground-truth labels stay in the evaluation workflow.

## Run locally

Python 3.10+ is sufficient for data generation alone. The backend and full test suite require Python 3.11+ and the packages in `requirements-backend.txt`.

Run these commands from this repository's `databricks/` directory:

```sh
python3 generate.py --output data
python3 build_notebook.py
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-backend.txt
.venv/bin/python -m unittest discover -s tests -v
```

Generation refuses to replace an existing directory unless `--overwrite` is supplied. Use a new output directory and a different `--seed` for separate development/evaluation runs. Random noise changes do not make these templates a realistic hardware benchmark.

Generated `data/evidence/*.csv` can be used by the app. `data/evaluation/` contains ground truth and generation metadata for the evaluator only. The notebook never writes those labels to the four Delta tables.

## Querying the data

Use one `run_id` and a fixed `as_of_ts` throughout an investigation. All queries bound their reads to that cursor so replay cannot expose future samples. The scenarios have overlapping timestamps and must never be pooled into one fleet.

- `sql/overview.sql`: observed GPU power and utilisation, plus the number of unknown devices.
- `sql/gpu_history.sql`: a bounded timeline for one GPU.
- `sql/rack_context.sql`: GPU, job and cooling observations from one rack.
- `sql/fleet_evidence.sql`: the four evidence streams for one run and a bounded analysis window, used by the official detector.
- `sql/official_results.sql`: saved official results at one exact replay cursor, available through `DatabricksData.official_results()` for audit/readback.

The notebook demonstrates `spark.sql(query, args=parameters)`. The included `backend/databricks_data.py` runs the packaged queries through the Databricks SQL Statement Execution API with typed parameters. It polls pending statements, rejects incomplete results and preserves NULL values. Its polling deadline and cancellation are best effort; SDK network requests/retries can take longer than that deadline.

Follow [CONNECT.md](CONNECT.md) to sign in, check a real SQL query, run the connected HTTP API and use the model evidence examples. Set `DATABRICKS_API_URL` explicitly when starting the website to select that backend; without it, the website uses the saved replay. No API credentials are included in source files. The notebooks default to `workspace.gpu_ops_hackathon`; change their configuration and `.env` together if your workspace uses another catalog/schema.

## Official assessments and comparison baselines

In connected mode, `backend/official_detection.py` is shared by the website and model evidence adapter. For each requested cursor it reads up to 15 minutes of source evidence, calls the actual teammate detector and persists the snapshot using a parameterised Delta MERGE. `official_detector_results` stores the run, cursor, detector/configuration/adapter fingerprint, evidence hash, incident count, result JSON and generation time. Repeating the same evidence and detector version updates the same record. Source evidence tables are unchanged.

The default saved replay uses the same connector and detector on exported evidence. It recomputes findings for the requested cursor rather than loading prewritten alert answers, but it does not query the workspace or persist a new official result. Responses carry `delivery: "saved_replay"`, `exported_at`, and snapshot `persistence: {status: "not_saved", reason: "saved_replay"}`. These labels stay visible in the website.

The UI reports findings within that analysis window. A flag can refer to an earlier event even when the latest reading has recovered. Missing or stale latest GPU telemetry remains unknown; no finding means only that this detector found none in the window. Maintenance, operator interventions, workload phases and calibrated baselines are not supplied by the fixture and cannot be ruled out as explanations.

`notebooks/02_gpu_health_pipeline.ipynb` extends the data layer into a processing pipeline. It joins the four evidence tables at a selected replay time, calculates per-GPU features with Spark SQL, evaluates six transparent baseline rules and stores `health_features` and `health_alerts` with Delta MERGE. Repeating the same run/time updates those derived snapshots without duplicating rows. Original evidence tables are unchanged.

These six rules are comparison fixtures, not official escalations. They store active, clear and unknown evaluations and are returned by `GET /api/health-assessments` with `official: false`. They are not required for the connected website's official assessments and may disagree with the actual detector, which has different persistence/context requirements. A failure in the official path returns an error; it never falls back to these rules.

The notebook supports Databricks Job parameters `catalog`, `schema`, `run_id`, `as_of_ts`. A repeated fixed cursor is only a repeatable replay; a simulator or collector must advance it to create continuous monitoring. See [CONNECT.md](CONNECT.md) for the demo flow and team handoff.

## Lakehouse control plane

`python -m backend.detector_job deploy` runs the official detector as a scheduled Databricks job on serverless compute (notebook 04), saving to the same `official_detector_results` table under the same detector version. `python -m backend.lakehouse` builds a governed workbench on top of the evidence tables: incident and GPU-state timelines parsed from the official detector's saved assessments, a topology graph, eight cursor-bound agent tools as Unity Catalog SQL functions, an evaluation schema with the scenario answer key and a detection scorecard, and a ledger of the twin's scheduling decisions and outcomes. Notebook 03 reads all of it. See [lakehouse/README.md](lakehouse/README.md).

## What the demo does and does not establish

The four fixtures are a shared cooling problem, a healthy workload increase, missing GPU telemetry and an individual GPU error. Their recovery is **scheduled in prerecorded data**, not caused by an agent action. These fixtures can test diagnosis and recovery detection. To claim autonomous mitigation, a later live simulator must change state in response to the agent's action and report whether it actually helped.

The connected path was validated against Databricks on 2026-11-14: the selected 24-GPU replay produced an official `zone-b` incident, and its saved result matched the API response on readback. See [validation details](CONNECT.md#validation-boundaries) for the exact run/cursor and test coverage. This validates a synthetic replay, not real hardware fault detection.

MLflow tracing, live telemetry ingestion, diagnostic model inference inside this backend and real vendor adapters remain subsequent integrations. The detector job is scheduled but paused, and the replays are a fixed recording. The local server has no application-user authorization and is not a public deployment.

## Official references

- [Import a Databricks notebook](https://docs.databricks.com/aws/en/notebooks/notebook-export-import)
- [Databricks SQL API](https://docs.databricks.com/aws/en/dev-tools/sql-execution-tutorial)
- [SQL parameter markers](https://docs.databricks.com/gcp/en/sql/language-manual/sql-ref-parameter-marker)
- [Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations)
