# GPU operations: Databricks starter

This module supplies a Databricks evidence pipeline and a local HTTP API for the hackathon. It contains the notebooks, queries and integration examples developed for the team's data layer.

This starter contains synthetic data for 24 GPUs (8 each labelled NVIDIA, AMD and Intel), four scenarios, a self-contained Databricks notebook and parameterised investigation queries. All hardware models and limits are illustrative. It does not connect to physical GPUs or implement either teammate's model.

**Integration status:** this is an isolated Databricks demonstration fixture. The repository's existing website/digital twin and `flagging/` detector are not wired to it. Their entity IDs and replay formats differ. Use the [integration boundary](CONNECT.md#integrating-with-this-repository) before mapping this module into the canonical model; do not combine the separate datasets or replace the existing detector with these baseline rules.

## Start in Databricks

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

- **Figma / frontend:** `contracts/data-contract.md` and `examples/dashboard.json` provide a sample overview, rack and GPU timeline using the actual query output fields. The JSON is a static fixture generated locally with SQLite emulation of the SQL, not a live Databricks response. The UI should show overview → rack → GPU → incident, with a visible synthetic-data label and separate unknown readings.
- **Deterministic detector:** `telemetry`, `inventory`, `workloads`, `cooling`, plus a run ID and the replay cursor. It owns anomaly rules and emits candidate incidents. The SQL supplied here retrieves evidence; it does not declare a hardware diagnosis.
- **Fine-tuning / diagnostic model:** the same evidence contract and a proposed finding format in the contract. The model receives observations, not the scenario name or correct answer.
- **You:** make the notebook run, keep the schema consistent, and provide the catalog name, schema name and run IDs. When the backend is ready, add a SQL warehouse ID and server-side authentication. Never send an API token to the browser or put one in Git.

Agree on the data contract before either teammate designs a training format. A running baseline agent can use the same contract while fine-tuning is still in progress.

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

The notebook demonstrates `spark.sql(query, args=parameters)`. The included `backend/databricks_data.py` runs the packaged queries through the Databricks SQL Statement Execution API with typed parameters. It polls pending statements, rejects incomplete results and preserves NULL values. Its polling deadline and cancellation are best effort; SDK network requests/retries can take longer than that deadline.

Follow [CONNECT.md](CONNECT.md) to sign in, check a real SQL query, run the local HTTP API and use the website/model evidence examples. No API credentials are included in source files. The notebooks default to `workspace.gpu_ops_hackathon`; change their configuration and `.env` together if your workspace uses another catalog/schema.

## Process health observations on Databricks

`notebooks/02_gpu_health_pipeline.ipynb` extends the data layer into a processing pipeline. It joins the four evidence tables at a selected replay time, calculates per-GPU features with Spark SQL, evaluates six transparent baseline rules and stores `health_features` and `health_alerts` with Delta MERGE. Repeating the same run/time updates those derived snapshots without duplicating rows. Original evidence tables are unchanged.

These rules are integration scaffolding for the deterministic-model teammate. They store active, clear and unknown evaluations rather than pretending that missing readings mean healthy hardware. The diagnostic teammate can retrieve the corresponding histories/context through `examples/model_adapter.py`. Model inference and an incident lifecycle remain to be connected by the team.

The notebook supports Databricks Job parameters `catalog`, `schema`, `run_id`, `as_of_ts`. A repeated fixed cursor is only a repeatable replay; a simulator or collector must advance it to create continuous monitoring. See [CONNECT.md](CONNECT.md) for the demo flow and team handoff.

## What the demo does and does not establish

The four fixtures are a shared cooling problem, a healthy workload increase, missing GPU telemetry and an individual GPU error. Their recovery is **scheduled in prerecorded data**, not caused by an agent action. These fixtures can test diagnosis and recovery detection. To claim autonomous mitigation, a later live simulator must change state in response to the agent's action and report whether it actually helped.

Local tests validate the generator, query logic and API contracts; remote execution must be checked separately. This starter includes the local web API and Databricks processing notebook. MLflow tracing, a continuously running monitor, model training/inference and real vendor adapters remain subsequent integrations.

## Official references

- [Import a Databricks notebook](https://docs.databricks.com/aws/en/notebooks/notebook-export-import)
- [Databricks SQL API](https://docs.databricks.com/aws/en/dev-tools/sql-execution-tutorial)
- [SQL parameter markers](https://docs.databricks.com/gcp/en/sql/language-manual/sql-ref-parameter-marker)
- [Free Edition limitations](https://docs.databricks.com/aws/en/getting-started/free-edition-limitations)
