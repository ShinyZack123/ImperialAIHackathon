# Deterministic telemetry flagging

Decides which datacentre telemetry is worth escalating to a low-level agent, and writes each flag as a plain-English string. No ML in the flagging path: same input, same output.

Run from this folder (Python 3, standard library only):

    python3 build_context.py      # builds topology.enriched.json (limits, measured baselines, interventions)
    python3 test_scenarios.py     # one test per labelled scenario (23/23)
    python3 test_incidents.py     # grouping by time, root choice, incident IDs/status, idle-capacity opportunities
    python3 generalise.py         # fresh generated scenarios
    python3 demo.py               # stuck checkpoint vs normal checkpoint, side by side
    python3 generate.py --list    # seeded telemetry generator

See `FLAGGER_METHODS.md` for how the detectors and gates work, and `WORKLOG.md` for the build log.

## In the operator console: simulated scenarios

`bridge.py` connects the flagger to the Crosslayer console. Run `npm run dev` from the repo root, open http://127.0.0.1:4397 and pick a **Telemetry flagger** scenario from the scenario menu.

- The replay slider steps through 24 cut-offs. At each one the flagger only sees records up to that instant, as `demo.py` does. Flagged GPUs turn red (critical) or amber (warning). GPUs whose workload is affected get a dashed outline (blast radius). Stale or missing telemetry shows as unknown, never as healthy. Power, cooling and fabric paths go red when the root cause is in that layer.
- Click a GPU, server or connection: the console runs `bridge.py entity` for it. It shows that entity's telemetry chart with its catalogue limits and flagged spans, the flagger's findings, and the raw log (filter it to flagged records only).
- The Incident tab shows each escalation, its evidence, the methods that fired, the exact message sent to the low-level agent, and an answer-key check against the scenario label. Each incident keeps one ID through the replay and shows `active`/`recovered` and what changed; the message is marked as not re-sent when nothing changed since the previous cut-off.
- Under the incidents, **Optimisation opportunities** lists healthy GPUs doing no work (finished job still holding them, queued job not started, unallocated). These are not faults and never colour the map.

Console IDs map to flagging IDs only in `bridge.py` (`TwinMap`): console rack A01, A02, B01, B02 = servers 12-01, 12-02, 13-01, 13-02, and console slot *n* = GPU *n-1*.

Scenarios:
- **S01–S12 (showcase)** are generated from `generate.py` with seed 7. Each covers the whole 32-GPU hall, and S04 runs two unrelated faults at once. They are rebuilt on first use and cached under `generated/console/`, keyed on the code and config that produced them.
- **F01–F12 and B01–B11** are the hand-labelled files. They contain telemetry only for the entities each scenario is about, so the other GPUs correctly show as *no data*.

    python3 bridge.py catalog
    python3 bridge.py analyse S01-straggler-gpu-thermal-throttle
    python3 bridge.py entity  S01-straggler-gpu-thermal-throttle gpu-a02-06
    python3 test_bridge.py    # all 12 showcase scenarios match their labels, plus mapping, validation and stale-data tests

All of the data is synthetic.

## In the operator console: Databricks evidence

The separate **Databricks** tab now invokes this same `flagger.flag_window` function through `../databricks/backend/flagging_adapter.py`. It preserves the Databricks fixture's native 24 GPUs and three racks, such as `gpu-001`, `rack-a` and `zone-a`; it does not use `TwinMap` or project this fleet onto the 32-GPU console. Databricks replay UUIDs and UTC timestamps are independent of the scenario names and ticks above.

The official flow is:

```text
four Delta evidence tables → at most 15 minutes before the selected cursor
  → validated source-to-detector adapter → flag_window
  → confirmed official_detector_results write in Databricks → website/model tools
```

`OfficialDetector` in `../databricks/backend/official_detection.py` is shared by the website's snapshot API and the model adapter's `.health_assessments()`. It uses this module as the sole official detector. The six `baseline-v1` rules in Databricks notebook 02 and its `health_alerts` table are comparison-only; `.comparison_baseline()` exposes them for tests. A failure in the official detector or result persistence returns an error and never falls back to those six rules.

The connector retains source units: job progress remains synthetic work units, cooling flow remains percent of nominal, and generic GPU clock readings are not relabelled as SM/memory clocks. It supplies only available evidence and does not load scenario labels or answer keys. Maintenance, operator interventions, workload phases, calibrated baselines and throttle reasons are absent, so detector messages cannot exclude those explanations.

Flags describe observations within the selected analysis window, not confirmed current hardware failures. A recovered latest reading can coexist with an earlier finding. The existing 300-second persistence rule is retained; short windows may have no flags despite unusual readings. Missing/stale latest telemetry remains unknown, and no finding does not certify healthy hardware.

Consecutive-condition and stalled-progress windows break across gaps longer than three sampling periods. The connector declares this fixture's five-second cadence; other sources should supply `samplingIntervalSeconds` in their metric definitions/catalog. Without that metadata the detector estimates cadence from the observed median interval, which cannot distinguish uniformly missing samples from a genuinely slower source.

Databricks incidents also carry `incidentId`, `status`, `onset` and `lastSeen`. Because each review is a single explicit cursor, the stateful `IncidentTracker` is not used there, and opportunities are not part of the saved snapshot.

Each website review uses an explicit cursor; it does not start continuous monitoring, diagnostic LLM inference or hardware control. Official results include a detector/configuration/adapter fingerprint and evidence hash in `official_detector_results`. Repeating the same input/version updates the same record. See the [connection guide](../databricks/CONNECT.md) for Python API + same-origin Node proxy startup, shared warehouse permissions and live validation, and the [data contract](../databricks/contracts/data-contract.md) for API details.
