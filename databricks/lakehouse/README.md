# Lakehouse control plane

This folder turns the four evidence tables into a governed workbench in Unity Catalog. The detector's audit trail, the investigating agent's tools, the evaluation and the scheduler's decisions all live in one place, under the same catalog and permissions. Every row is synthetic.

```text
bronze   inventory · telemetry · workloads · cooling                    notebook 01
job      GPU Ops official detector (notebook 04, serverless)             detector_job_runs log
silver   official_detector_results → detector_snapshots                 detector audit, parsed
gold     incident_timeline · incident_findings · gpu_state_timeline · escalation_funnel
graph    topology_entities · topology_connections                       nodes and edges from inventory
tools    tool_* SQL table functions                                     cursor-bound, for agents and Genie
eval     <schema>_eval: ground_truth · detection_scorecard              answer key, never granted to agents
ledger   policy_runs · policy_timeline · policy_leaderboard · policy_uplift   twin decisions and outcomes
```

## Objects

| Object | Kind | Purpose |
|---|---|---|
| `detector_snapshots` | view | Newest official assessment per replay cursor, with the saved JSON parsed into typed columns |
| `incident_timeline` | view | One row per incident per cursor: what would be escalated to an agent |
| `incident_findings` | view | The detector findings behind each incident |
| `gpu_state_timeline` | view | Each GPU's state at each cursor. `unknown` is its own state, never healthy |
| `escalation_funnel` | view | Evidence rows stored, records analysed, and distinct incidents raised per replay |
| `detector_job_runs` | table | One row per run of the detector job: cursors planned, skipped, assessed and deferred, and the detector version |
| `topology_entities`, `topology_connections` | views | Knowledge graph of GPUs, servers, racks and cooling zones, derived from inventory |
| `tool_*` | SQL table functions | Agent tools, listed below |
| `<schema>_eval.ground_truth` | table | Scenario answer key, regenerated locally from the deterministic generator |
| `<schema>_eval.detection_scorecard` | view | Per replay: flagged or not, root named or not, and seconds from onset to first flag |
| `policy_runs`, `policy_timeline` | tables | Baseline and coordinated twin outcomes on identical seeded traces |
| `policy_leaderboard`, `policy_uplift` | views | Mean outcome per policy, and paired uplift over the baseline |

## Detector job

`GPU Ops official detector` is a Databricks job that runs `flagging.flagger.flag_window` on serverless compute. Notebook 04 carries the repository's detector, configuration and connector adapter, checks each file's SHA-256, and so saves results under the same detector version as the backend: a cursor assessed by either one has the same audit key, and repeating it updates the same row.

Each run plans a cursor every 15 seconds of every replay, skips cursors the current detector version has already saved, assesses the rest from a 15-minute window, saves them with one MERGE per batch and appends a row to `detector_job_runs`. When the detector changes, its version fingerprint changes and the next run re-assesses everything, so the scorecard always describes the current code.

```sh
.venv/bin/python -m backend.detector_job deploy             # upload notebook 04, create or update the job (schedule paused)
.venv/bin/python -m backend.detector_job deploy --run-now   # ... and run it once
.venv/bin/python -m backend.detector_job deploy --unpause   # ... and let the 30-minute schedule run
```

The schedule is created paused because each run uses Free Edition serverless quota; unpause it only while you need it. Rebuild notebook 04 with `python3 build_detector_job_notebook.py` after changing the detector; a test fails until you do.

## Agent tools

| Function | Returns |
|---|---|
| `tool_list_runs()` | Replay IDs and their recorded intervals |
| `tool_fleet_overview(run, cursor)` | Observed power, utilisation and temperature, plus the unknown-GPU count |
| `tool_gpu_history(run, gpu, cursor[, minutes])` | One GPU's readings, at most 15 minutes and 180 rows |
| `tool_workload_history(run, gpu, cursor[, minutes])` | Job state and cumulative progress |
| `tool_cooling_history(run, zone, cursor[, minutes])` | Coolant inlet temperature and flow |
| `tool_rack_context(run, rack, cursor)` | Every GPU in a rack with its job and cooling context |
| `tool_blast_radius(entity)` | GPUs that depend on a GPU, server, rack or cooling zone |
| `tool_detector_incidents(run, cursor)` | Newest official incidents assessed at or before the cursor |

The cursor bound and the window clamp are inside each function, so a model calling them cannot read later samples or pull unbounded data. Function and parameter comments are written for a model to read. Grant an agent principal `EXECUTE` on these functions and nothing on the `_eval` schema.

```sql
SELECT * FROM tool_detector_incidents('43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c', TIMESTAMP'2026-11-14T09:10:45Z');
SELECT * FROM tool_blast_radius('zone-b');
SELECT * FROM tool_cooling_history('43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c', 'zone-b', TIMESTAMP'2026-11-14T09:10:45Z', 10);
```

## Commands

Run from `databricks/` after the sign-in in [CONNECT.md](../CONNECT.md):

```sh
.venv/bin/python -m backend.lakehouse plan          # list the statements; no cloud calls
.venv/bin/python -m backend.lakehouse apply         # create or refresh every object above
.venv/bin/python -m backend.lakehouse backfill      # official detector at every 30 s cursor, saved by MERGE
.venv/bin/python -m backend.lakehouse ground-truth  # answer key into the _eval schema
.venv/bin/python -m backend.lakehouse experiments   # twin results into policy_runs and policy_timeline
.venv/bin/python -m backend.lakehouse governance    # table and column comments and tags; metadata only
.venv/bin/python -m backend.lakehouse status        # funnel, scorecard and uplift as JSON
```

Each command can be repeated. `apply` runs only the packaged files in this folder, and rejects anything other than `CREATE OR REPLACE VIEW|FUNCTION`, `CREATE TABLE IF NOT EXISTS` and `CREATE SCHEMA IF NOT EXISTS`. `backfill` skips cursors the current detector version has already saved and re-assesses cursors saved by an older version. Source evidence is never modified.

Regenerate the twin export with `npm run export:experiments` after changing `src/twin.js`, then run `experiments` again. Rebuild notebook 03 with `python3 build_lakehouse_notebook.py`.

## In the workspace

- **Notebook 03** (`notebooks/03_lakehouse_workbench.ipynb`) walks through the funnel, the detector job's runs, the incident timeline, an investigation using the tool functions, the scorecard and the policy ledger. It only reads.
- **Jobs & Pipelines → GPU Ops official detector** shows the job, its paused schedule and each run.
- **Catalog Explorer → Lineage** on `incident_timeline` shows `official_detector_results → detector_snapshots → incident_timeline`.
- **Genie**: add the gold views, the topology views and the policy views to a Genie space, then ask questions such as "which cooling zone had the most incidents" or "how much training throughput did the coordinated policy gain in the network scenario".
- **AI/BI dashboard**: `escalation_funnel`, `policy_uplift` and `<schema>_eval.detection_scorecard` each make a single-query tile.

## Limits

- The replays, answer key and hardware limits are synthetic. Recovery in the replays is prerecorded, not caused by an agent.
- The scorecard grades four scenarios written by the same team that wrote the detector. It shows the pipeline works end to end, not that the detector generalises.
- The policy ledger is simulated. The coordinated plan sees the whole 24-minute trace in advance and is applied at minute 0 without a migration cost.
- The replays are a fixed recording, so a scheduled run only finds new work when the detector changes or the spacing is reduced. A live collector appending telemetry would give every run new cursors.
- `backfill` runs the detector on the machine that runs the command; the detector job runs the same code on Databricks.
- The topology graph covers GPU, server, rack and cooling zone. The fixture has no network or power-distribution entities.
