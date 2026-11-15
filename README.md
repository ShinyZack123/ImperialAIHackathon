# Ithil

| Entered for | |
|---|---|
| **Track** | Autonomous Agents |
| **Bounty** | Databricks Integration |
| **Overall prizes** | Adaption: Yes · Figma: Yes |

Built at Imperial AI Hack 2026. **All data is synthetic.**

## In one minute (no technical background needed)

When an AI model trains across many GPUs (the chips that do the work), **one slow chip or network link can hold up the whole job**. Today, the chips, the network, the power and the cooling are each watched by a different tool, so nobody sees the full picture.

We built a dashboard and a set of AI helpers that:

1. **Spot the problem.** A detector watches the readings and raises *one* clear alert per real problem, instead of hundreds of noisy ones.
2. **Explain the cause.** It shows the likely cause, the evidence and which jobs are affected. For example: "this GPU overheated and slowed itself down to protect the chip".
3. **Test a fix safely.** A simulated copy of the datacentre (a "digital twin") tries different ways of arranging the work and checks that none of them break power, heat or speed limits.
4. **Leave the decision to a human.** The AI only *proposes*. An operator approves, and every change is checked afterwards and undone if it didn't help.

**Headline result (simulated):** on a slow network link, the chosen layout raised training from **407 to 756 examples per second (+86%)** with the same energy use and the same response time.

---

## How we used each track

### Autonomous Agents (track)

The software works like a team of careful assistants. It **fetches readings, checks for problems, explains what likely caused them and tests new layouts in simulation**. The operator starts each analysis and decides what to do.

- **Detect:** a deterministic detector (fixed rules and statistics, not AI) turns raw readings into one alert per root cause.
- **Investigate:** an agent investigates using tools that can only read data *up to the current moment*, so it can't cheat by looking ahead.
- **Propose:** the optimiser and the Operations view turn findings into proposals.
- **Safety:** every proposal goes through an **action gate**. It is simulated, checked against hard limits, approved by a human, verified afterwards and rolled back if it fails. Agents can propose but **can never approve**.
- **MCP server:** any outside AI agent can use the same tools.

### Databricks (bounty)

**In simple terms: Databricks answers "what happened?", and the digital twin answers "what could we change?"**

- **Where the data lives.** Databricks stores a separate set of synthetic readings in Delta tables: GPU inventory, timestamped readings, job progress and cooling flow (24 GPUs, 3 racks, four 15-minute replays).
- **How we read it.** You pick a replay and a time. The app asks Databricks only for the 15 minutes before that time. An adapter checks and translates the records for Python and keeps IDs, units and missing values intact.
- **How we write back.** In connected mode, the detector's result is **saved back to Databricks**, tagged with the evidence used and the detector version. The website only shows the result once Databricks confirms it was saved.
- **Running inside Databricks.** The official detector also runs as a **scheduled serverless Databricks job**. Unity Catalog functions give the agent safe, time-limited tools, and a scorecard and a policy ledger record how well it did.
- **Example from the demo:** in Replay 1, coolant flow falls to about 20% of normal. Eight GPUs depend on that cooling loop, and their temperature readings have stopped updating. The dashboard points the operator to the cooling loop and explains why those temperature readings can't be trusted.
- **Result:** the detector assessed every 15-second point of the four replays (240 assessments, about 4.9 million records) and got **all 4 replays right**. See [Results](#results-so-far).

### Adaption (overall prize)

We used Adaption to **fine-tune a small language model** that reads a GPU's telemetry and labels its health as `stable`, `warning` or `critical`.

- **Result:** our fine-tuned model performed **75% better than a non-fine-tuned Qwen 2-billion-parameter model** on the same GPU health task.
- **Where it fits:** the model's labels feed into **GPU health triage** in the Operations view. A `critical` label moves a GPU up to "drain at the next checkpoint"; `warning` moves it to "watch".
- **Kept safe:** the model can't act on its own. Invalid labels are ignored, a label can never override stale data, and any drain still needs operator approval.
- **Status:** the slot in the app is built and tested. The trained model is not yet connected to the running demo. See [Fine-tuned GPU health model](#fine-tuned-gpu-health-model-slot-ready-not-connected-yet).

### Figma (overall prize)

The operator console (the website: infrastructure map, incident panel, experiments and Databricks workspace) was **designed in Figma** before it was built.

---

## Project overview

An autonomous operations layer for AI datacenters. It treats compute, network, power and cooling as **one connected system** instead of four separate ones.

In short:

1. A **deterministic detector** (fixed rules and statistics, no AI) turns raw telemetry into **one alert per root cause**, not hundreds of alerts per symptom.
2. An **investigating agent** digs into that alert using governed, time-limited tools in Databricks.
3. A **coordinated optimiser** proposes new job placements. Each plan is simulated and checked against hard limits, then an operator reviews it.

---

## The problem

GPU clusters are managed by separate tools for the scheduler, the GPUs, the network fabric, power and cooling. A slow training job could be caused by any of these:

- a degraded network uplink
- a failing cooling loop
- a power cap
- one straggling GPU

Each tool sees only its own layer. As a result, operators either drown in alerts or fix one layer while breaking another.

---

## What we built

| Part | What it does | Code |
|---|---|---|
| **Digital twin** | A simulated datacenter: 32 GPUs in four racks, with fabric, power and cooling dependencies, training and inference jobs, and four incident scenarios. | [`src/twin.js`](src/twin.js) |
| **Deterministic detector** | Checks the data is valid, removes one-off spikes, runs 8 statistical detectors, filters out alerts that make sense in context (e.g. expected load), then groups related alerts using the topology graph. Sends one plain-English message per root cause. | [`flagging/`](flagging/README.md) |
| **Databricks lakehouse** | Stores the evidence in Delta tables and runs the detector as a scheduled serverless job. Also holds the detector's audit trail, incident timelines, a topology graph, agent tools (Unity Catalog functions that can only read data up to a given time), an evaluation scorecard and a ledger of scheduling decisions. | [`databricks/`](databricks/README.md) |
| **Coordinated optimiser** | Searches rack placement, serving capacity and power caps together. Every candidate plan must pass six hard constraints at every simulated minute. | [`src/twin.js`](src/twin.js) |
| **Operations: action gate** | Every proposed change must be simulated, validated, approved by an operator and verified, and it is rolled back if it fails. Also includes GPU health triage, straggler attribution and maintenance planning. See [Operations](#operations-action-gate-triage-and-maintenance). | [`src/operations.js`](src/operations.js) |
| **Operator console** | The website: interactive 3D rack explorer, infrastructure map, incident inspector, policy experiments, operations view and the Databricks evidence workspace. | [`public/`](public/) |
| **MCP server** | Exposes the twin as tools any AI agent can call: four read-only tools, plus the operations tools listed below. | [`src/mcp.js`](src/mcp.js) |

---

## How it works

```text
raw telemetry ─► validate ─► de-spike ─► 8 detectors ─► context gates ─► graph correlation ─► 1 escalation per root cause
                                                                                                        │
     Unity Catalog tools: history, rack context, blast radius, incidents (cannot read the future) ◄─── agent investigates
                                                                                                        │
     twin simulates candidate plans ─► 6 hard constraints ─► operator reviews ─► simulated outcome ─► policy ledger in Delta
```

Step by step:

1. **Detect.** Telemetry goes through the detector. Only real escalations (one per root cause) reach the AI agent.
2. **Investigate.** The agent can only use Unity Catalog functions. Each function takes the replay time as an input, so the agent **cannot see data from later in the scenario**. The scenario answer key is in a separate schema that agents never get access to.
3. **Propose, then decide.** What the AI produces is only a **proposal**. A deterministic validator and a human operator make the actual decision.

---

## Results so far

> All figures come from synthetic data and simulation, not real hardware.

**Detector**
- All 23 hand-labelled scenarios correct: 12 faults flagged with the right root cause, and 11 harmless near-misses correctly left alone.
- 340 out of 340 runs correct on newly generated scenarios across 10 noise seeds.
- Details: [FLAGGER_METHODS.md](flagging/FLAGGER_METHODS.md)

**Optimiser** (average of 11 seeded replays per scenario)

| Scenario | Result |
|---|---|
| Network degradation | +86% training throughput at the same energy |
| Inference demand surge | Went from 0 to 24 of 24 minutes within the 160 ms P95 latency target |
| Power-budget cut | +3% throughput with 2.5% less energy |
| Thermal constraint | 13 more minutes within the latency target |

Default network experiment (seed 73): **407 → 756** average training samples/s (+86%), **116.5 ms** average minute-level P95 latency in both, and **13.26 kWh** modelled energy in both. **1,440 of 1,512** candidate layouts met every limit at every minute. (P95 is how quickly 95% of requests get a response.)

*Caveat:* the coordinated plan sees the whole 24-minute trace in advance and is applied at minute 0, with no cost for migrating jobs. It picks and scores the best layout on the same test run, so gains on a real fleet still need testing.

**Fine-tuned GPU health model (Adaption)**
- 75% better than a non-fine-tuned Qwen 2B model on the GPU health task. See [Adaption](#adaption-overall-prize).

**Databricks**
- The official detector runs as a Databricks job on serverless compute.
- It has assessed every 15-second point of the four Databricks replays: **240 assessments** over about **4.9 million metric records**, saved in Delta, raising **6 distinct incidents**.
- Graded against the answer key, **all 4 replays are correct**:
  - Isolated GPU error: named at onset.
  - Telemetry gap: named after 15 seconds.
  - Shared cooling zone: named after 315 seconds.
  - Healthy load increase: correctly raised nothing.
- *Caveat:* the same team wrote these four scenarios and the detector.
- Details: [the lakehouse guide](databricks/lakehouse/README.md)

---

## APIs, models and datasets

- **Databricks Free Edition:** Unity Catalog, Delta tables, a SQL warehouse via the Statement Execution API, SQL table functions, serverless notebooks, a scheduled serverless job, the Databricks SDK for Python, and the Databricks CLI (OAuth sign-in).
- **Adaption:** used to fine-tune the GPU health model (labels `stable` / `warning` / `critical`). Baseline for comparison: Qwen 2B without fine-tuning.
- **Figma:** the operator console was designed in Figma.
- **Datasets (all synthetic):**
  - [`databricks/generate.py`](databricks/generate.py): creates 24 GPUs and four 15-minute replays.
  - [`flagging/generate.py`](flagging/generate.py) and [`flagging/gen.py`](flagging/gen.py): create the 32-GPU hall, its 23 labelled scenarios and 12 showcase scenarios.
  - [`src/twin.js`](src/twin.js): a seeded simulator.
- **Libraries:**
  - Databricks backend: FastAPI, Uvicorn, python-dotenv, httpx.
  - Detector: Python standard library only.
  - Console and MCP server: Node.js, no dependencies.

---

## Quick start (no Databricks account needed)

**You need:** Node.js 22.18+ and Python 3.9+ on your PATH. Set `ITHIL_PORT` to use a different local port or `ITHIL_PYTHON` to select a Python executable.

From the repository root:

```sh
npm run dev
```

Then open **http://127.0.0.1:4397**:

- **Rack explorer**: the 3D landing screen for three racks and 24 GPUs. Orbit and zoom the hall, click a tray to slide it out, or select a GPU from the inspector. Choose **Rack overheating** or **Single GPU fault** to see direct GPU faults outlined in red. Amber indicates shared findings and grey indicates unavailable telemetry. This view uses two saved synthetic Databricks replays.
- **Overview**: the digital twin and its incident scenarios. The scenario menu can also replay the detector's full-hall, labelled-fault and benign scenarios.
- **Experiments**: compares the baseline policy with the coordinated policy.
- **Operations** (http://127.0.0.1:4397/#operations): the action gate, triage and maintenance tools. See [Operations](#operations-action-gate-triage-and-maintenance).

To regenerate the rack explorer assets during development, run `npm ci`, `npm run rebuild:3d` and `python3 -B scripts/export-rack-replay.py`. The checked-in browser bundle and replay assets let `npm run dev` start directly after cloning.

### Viewing the Databricks evidence offline

Open **http://127.0.0.1:4397/#databricks**.

You **don't** need a Databricks account, credentials, a Python virtual environment or `pip install` for this. The repo already contains four synthetic replays exported from Databricks. The local server reads those saved files and runs the current detector (`flagging.flagger.flag_window`) on your machine.

The website labels this mode **Saved Databricks replay**. It shows when the evidence was exported and makes clear that nothing is written back to Databricks.

To use it:

1. Choose a **Databricks replay**.
2. Set **Replay time · UTC**.
3. Click **Load snapshot**.
4. Click any GPU to see its history, and check **Deterministic detector findings**.

> ⚠️ If `DATABRICKS_API_URL` is still set in your terminal from an earlier session, run `unset DATABRICKS_API_URL` first. Otherwise the site will try to use the connected mode instead.

> ⚠️ This only works with the local Node server running. Opening the HTML file directly (`file://`), a static preview or GitHub Pages won't work, because they don't include the replay API.

---

## Connected demo (live Databricks)

This mode reads from and writes to a real Databricks workspace instead of the saved files.

### One-time setup

Follow the [connection guide](databricks/CONNECT.md) to:

1. Import the evidence notebook into Databricks.
2. Set up your local credentials.
3. Get access to the shared SQL warehouse and tables.

### Running it (two terminals)

**Terminal 1: start the Python backend** (talks to Databricks)

```sh
# From the repository root, after Python setup and Databricks login
cd databricks
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8011
```

**Terminal 2: start the website** and point it at that backend

```sh
# From the repository root; needs Node.js 22.18+
DATABRICKS_API_URL=http://127.0.0.1:8011 npm run dev
```

Then open the site as usual. It now shows **Connected Databricks** instead of "Saved Databricks replay".

### What's different in connected mode

- **Your credentials stay on the server.** The browser only talks to the Node website, which passes requests on to the Python backend. Only the backend talks to Databricks.
- **Results are confirmed before you see them.** The official snapshot appears only after Databricks confirms the detector result was saved.
- **No silent fallback.** If a query, the detector or a save fails, you get an error. The site will **not** quietly switch back to the saved offline data.

### The two modes side by side

```text
Connected: Delta evidence → bounded query → detector → saved Databricks result → website
Default:   exported Delta evidence → local detector → website (no Databricks writes)
```

### Building the lakehouse from scratch

To create the lakehouse objects, backfill the detector's assessments, and load the scorecard and policy ledger, follow [databricks/lakehouse/README.md](databricks/lakehouse/README.md). **Notebook 03** in the workspace walks through all of it.

---

## Operations: action gate, triage and maintenance

Open **http://127.0.0.1:4397/#operations**. This view runs on the 32-GPU simulated twin (not the Databricks data). It adds two scenarios: **GPU memory errors** (`gpu-xid`) and **Straggling training rank** (`straggler`). All the logic is deterministic and lives in [`src/operations.js`](src/operations.js).

### The action gate

No change happens without passing through the gate. Every change follows the same steps:

```text
propose → simulate → validate → approve → execute → verify → keep or roll back
```

**1. Propose.** A proposal can come from the optimiser, triage, straggler attribution, the maintenance planner or an MCP agent. Each proposal is checked against a strict schema.

**2. Simulate.** The twin runs the change, assuming current conditions stay the same.

**3. Validate.** The gate rejects or warns on:
- the kill switch being on, unsupported controls, missing dependencies, or stale telemetry;
- a blast radius over 16 reassigned GPUs, or the change causing *new* constraint failures;
- a change to the world size (number of GPUs) of Atlas, the training job; it also warns if the change would interrupt Atlas between checkpoints.

**4. Approve.** Only a human operator can approve. There is no approval tool for agents.

**5. Verify.** Two minutes after the change, the gate compares measured results with the prediction:
- **Failed** → rolls back automatically. If rolling back would itself be unsafe, it escalates to a human instead.
- **Inconclusive** (not "success") → if telemetry is stale, or something unrelated failed at the same time.

**Other rules**
- Rollbacks happen in reverse order: the most recent change is undone first.
- The **kill switch** cancels scheduled actions and blocks new approvals.
- Every step is written to an **append-only ledger**, which you can export as JSON.

### Supported controls

| Control | What it does |
|---|---|
| `placement` | Move work between GPUs |
| `power_cap` | Change a power limit |
| `drain_gpu` | Take a GPU out of service |
| `open_rma` | Draft a hardware return ticket. **Never actually sent.** |
| `maintenance_hold` | Hold a component for maintenance |

Facility controls such as CDU flow, fans and PDU outlets are **rejected**: the platform can only observe these, not control them.

### GPU health triage

Reads NVIDIA XID error codes and ECC memory counters and recommends the next step: **cordon → drain → reset → RMA** (hardware return). The built-in list of codes is a summary, so check it against your driver version.

### Fine-tuned GPU health model (slot ready, not connected yet)

A space in GPU health triage is ready for a fine-tuned model that reads GPU telemetry and labels each GPU `stable`, `warning` or `critical`.

To feed in labels, use either:

```js
operationsView.setHealthVerdicts({
  'gpu-a01-03': { label: 'critical', confidence: 0.93, model: 'gpu-health-ft' }
});
// or
triageFleet(snapshot, { verdicts });
```

How the labels are used:
- `critical` → triage raises the GPU to "drain at the next checkpoint".
- `warning` → triage raises it to "watch".
- Invalid labels are ignored, and a label can never override stale telemetry.
- Any drain still goes through the action gate and needs operator approval.

### Checkpoint-aware timing

Atlas saves a checkpoint every 8 minutes (a declared setting). The cost of interrupting it is estimated as:

```text
12 GPUs × (minutes since last checkpoint + 1 min to relaunch) × £2.40 per GPU-hour
```

The £2.40 GPU-hour price is an assumption, not a market price. Non-urgent drains wait for the next checkpoint. Urgent faults never wait.

### Straggler attribution

When a training job is slow, this splits each rank's speed into three factors:
- **compute** (SM clock)
- **fabric** (network uplink)
- **power** (rack power derate)

It then names the rank or group that's slowing the whole job down, and how much throughput that costs.

### Maintenance blast radius

Pick a component and it follows the power, cooling, fabric and containment connections downstream to show:
- which GPUs and jobs would be affected;
- single points of failure (the twin declares no redundant feeds);
- the least disruptive way to move work off it, and a suggested time.

### MCP tools for operations

The MCP server adds `triage_gpu_health`, `attribute_straggler`, `plan_maintenance`, `submit_proposal` and `get_action_ledger`.

- Anything submitted through MCP is always tagged as coming from a `model`.
- There is **no approval tool**, so agents can propose but never approve.
- All tools read the same twin that the gate operates on.
- Each MCP server process and each browser tab has its own in-memory gate session.

### Operations limitations

- The twin fills rack slots in order. Draining a GPU therefore shifts the remaining work in that rack, and cordoning a GPU doesn't move work already running on it.
- Predictions assume the current minute's conditions continue.
- All results are simulated.

---

## Limits

- All telemetry, scenarios and hardware limits are synthetic. Nothing here has run against real GPUs.
- Recovery in the Databricks replays is prerecorded; an agent doesn't cause it.
- The 24-GPU Databricks dataset and the 32-GPU twin are separate datasets. We don't invent a mapping between their devices.
- The detector only runs when asked about a specific replay time. There is no continuous monitoring and no hardware control.

---

## Reference notes

- [`flagging/flagger.py`](flagging/flagger.py) is the **only official detector**.
- Notebook 02's six `baseline-v1` rules and its `health_alerts` table are kept only for comparison.
- [Data contract](databricks/contracts/data-contract.md): units, how unknown values are handled, what the analysis window means, and what provenance is saved.
- [Flagging guide](flagging/README.md): both ways to run the detector.
