# AI infrastructure optimisation platform

An autonomous operations layer for AI datacenters. It treats compute, network, power and cooling as one connected system. A deterministic detector turns raw telemetry into one escalation per root cause. An investigating agent gets governed, time-bounded tools in Databricks. A coordinated optimiser proposes placements that are simulated and checked against hard limits before an operator reviews them.

Built at Imperial AI Hack 2026, Autonomous Agents track. All data is synthetic.

## The problem

GPU clusters are run by separate tools for the scheduler, the GPUs, the network fabric, power and cooling. A slow training job can come from a degraded uplink, a failing cooling loop, a power cap or one straggling GPU, and each tool sees only its own layer. Operators either drown in alerts or fix one layer at the expense of another, and 100% GPU utilisation is not the same as useful work.

## What we built

| Part | What it does | Code |
|---|---|---|
| Digital twin | 32 GPUs in four racks, with fabric, power and cooling dependencies, training and inference jobs, and four incident scenarios | [`src/twin.js`](src/twin.js) |
| Deterministic detector | Validates, de-spikes and runs 8 statistical detectors, then context gates and graph correlation. Sends one plain-English message per root cause | [`flagging/`](flagging/README.md) |
| Databricks lakehouse | Evidence in Delta, the detector running as a scheduled serverless job, its audit trail, incident timelines, a topology graph, cursor-bound agent tools as Unity Catalog functions, an evaluation scorecard and a ledger of scheduling decisions | [`databricks/`](databricks/README.md) |
| Coordinated optimiser | Searches rack placement, serving capacity and power caps together. Every candidate must pass six hard constraints at every simulated minute | [`src/twin.js`](src/twin.js) |
| Operator console | Infrastructure map, incident inspector, policy experiments and the Databricks evidence workspace | [`public/`](public/) |
| MCP server | The twin as four read-only tools any agent can call | [`src/mcp.js`](src/mcp.js) |

## How it works

```text
raw telemetry ─► validate ─► de-spike ─► 8 detectors ─► context gates ─► graph correlation ─► 1 escalation per root cause
                                                                                                        │
     Unity Catalog tools: history, rack context, blast radius, incidents (cannot read the future) ◄─── agent investigates
                                                                                                        │
     twin simulates candidate plans ─► 6 hard constraints ─► operator reviews ─► simulated outcome ─► policy ledger in Delta
```

Only escalations reach a reasoning agent. Everything the agent may read is a Unity Catalog function that takes the replay time as a parameter, so it cannot see later samples, and the scenario answer key sits in a separate schema that agents are never granted. Model output is a proposal; the deterministic validator and an operator decide.

## Results so far

All figures come from synthetic data and simulation, not real hardware.

- **Detector:** all 23 hand-labelled scenarios correct (12 faults flagged with the right root cause, 11 benign near-misses left quiet), and 340 of 340 runs correct on freshly generated scenarios over 10 noise seeds. See [FLAGGER_METHODS.md](flagging/FLAGGER_METHODS.md).
- **Optimiser, mean of 11 seeded replays per scenario:** network degradation +86% training throughput at the same energy; inference demand surge from 0 to 24 of 24 minutes inside the 160 ms P95 target; power-budget cut +3% throughput with 2.5% less energy; thermal constraint 13 more minutes inside the latency target. The coordinated plan sees the whole 24-minute trace and is applied at minute 0 without a migration cost.
- **Databricks:** the official detector runs as a Databricks job on serverless compute and has assessed every 15-second point of the four Databricks replays: 240 assessments over about 4.9 million metric records, saved in Delta, raising 6 distinct incidents. Graded against the answer key, all 4 replays are correct. The isolated GPU error was named at onset, the telemetry gap after 15 seconds and the shared cooling zone after 315 seconds; the healthy load increase raised nothing. These are four scenarios written by the same team as the detector. See [the lakehouse guide](databricks/lakehouse/README.md).

## APIs, models and datasets

- **Databricks Free Edition:** Unity Catalog, Delta tables, a SQL warehouse through the Statement Execution API, SQL table functions, serverless notebooks, a scheduled serverless job, the Databricks SDK for Python and the Databricks CLI (OAuth sign-in).
- **Figma:** the operator console was designed in Figma.
- **Datasets:** synthetic only. [`databricks/generate.py`](databricks/generate.py) creates 24 GPUs and four 15-minute replays; [`flagging/generate.py`](flagging/generate.py) and [`flagging/gen.py`](flagging/gen.py) create the 32-GPU hall and its 23 labelled and 12 showcase scenarios; [`src/twin.js`](src/twin.js) is a seeded simulator.
- **Libraries:** FastAPI, Uvicorn, python-dotenv and httpx in the Databricks backend. The detector uses the Python standard library; the console and MCP server use Node.js with no dependencies.

## Run after cloning

With Node.js 22.18+ and Python 3.9+ on PATH, run from the repository root:

```sh
npm run dev
```

Open http://127.0.0.1:4397. **Overview** shows the twin and its incident scenarios; the scenario menu also replays the detector's full-hall, labelled-fault and benign scenarios. **Experiments** compares the baseline and coordinated policies.

Open http://127.0.0.1:4397/#databricks for the Databricks evidence. No Databricks account, credentials, Python virtual environment or pip install is needed for this default mode. The repository includes four synthetic replays exported from Databricks; the server reads that saved evidence and runs the current `flagging.flagger.flag_window` detector locally. The website labels this **Saved Databricks replay**, shows when the evidence was exported, and states that assessments are not written to Databricks.

Choose a **Databricks replay**, set **Replay time · UTC**, then click **Load snapshot**. Click a GPU for its history and inspect **Deterministic detector findings**. If an old terminal has `DATABRICKS_API_URL` set, unset it before running the default saved replay.

This requires the running local Node server. Opening the HTML with `file://`, a static preview, or GitHub Pages does not provide the replay API.

## Run the connected demo

Follow the [connection guide](databricks/CONNECT.md) to import the evidence notebook, configure local credentials and grant access to the shared SQL warehouse and tables. Then use two terminals:

```sh
# Terminal 1, from the repository root; after Python setup and Databricks login
cd databricks
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8011
```

```sh
# Terminal 2, from the repository root; Node.js 22.18+
DATABRICKS_API_URL=http://127.0.0.1:8011 npm run dev
```

The website labels this mode **Connected Databricks**. The Node preview proxies same-origin requests to the configured Python backend; credentials stay on the server. The official snapshot is returned only after Databricks confirms its result was saved. An explicitly configured backend never falls back to saved replay when a query, detector or write fails.

```text
Connected: Delta evidence → bounded query → detector → saved Databricks result → website
Default:   exported Delta evidence → local detector → website (no Databricks writes)
```

To build the lakehouse objects, backfill the detector's assessments and load the scorecard and policy ledger, follow [databricks/lakehouse/README.md](databricks/lakehouse/README.md). Notebook 03 in the workspace walks through all of it.

## Limits

- All telemetry, scenarios and hardware limits are synthetic. Nothing here has run against real GPUs.
- Recovery in the Databricks replays is prerecorded; it is not caused by an agent.
- The 24-GPU Databricks fixture and the 32-GPU twin are separate datasets; no device mapping between them is invented.
- Each detector assessment is requested for an explicit replay time. There is no continuous monitor or hardware control.

[`flagging/flagger.py`](flagging/flagger.py) is the sole official detector. Notebook 02's six `baseline-v1` rules and `health_alerts` table remain comparison fixtures only. See the [data contract](databricks/contracts/data-contract.md) for units, unknown values, analysis-window semantics and persisted provenance, and the [flagging guide](flagging/README.md) for both detector entry points.
