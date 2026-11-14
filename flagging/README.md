# Deterministic telemetry flagging

Decides which datacentre telemetry is worth escalating to a low-level agent, and writes each flag as a plain-English string. No ML in the flagging path: same input, same output.

Run from this folder (Python 3, standard library only):

    python3 build_context.py      # builds topology.enriched.json (limits, measured baselines, interventions)
    python3 test_scenarios.py     # one test per labelled scenario (23/23)
    python3 generalise.py         # fresh generated scenarios
    python3 demo.py               # stuck checkpoint vs normal checkpoint, side by side
    python3 generate.py --list    # seeded telemetry generator

See `FLAGGER_METHODS.md` for how the detectors and gates work, and `WORKLOG.md` for the build log.

## In the operator console

`bridge.py` connects the flagger to the Crosslayer console. Run `npm run dev` from the repo root, open http://127.0.0.1:4397 and pick a **Telemetry flagger** scenario from the scenario menu.

- The replay slider steps through 24 cut-offs. At each one the flagger only sees records up to that instant, as `demo.py` does. Flagged GPUs turn red (critical) or amber (warning). GPUs whose workload is affected get a dashed outline (blast radius). Stale or missing telemetry shows as unknown, never as healthy. Power, cooling and fabric paths go red when the root cause is in that layer.
- Click a GPU, server or connection: the console runs `bridge.py entity` for it. It shows that entity's telemetry chart with its catalogue limits and flagged spans, the flagger's findings, and the raw log (filter it to flagged records only).
- The Incident tab shows each escalation, its evidence, the methods that fired, the exact message sent to the low-level agent, and an answer-key check against the scenario label.

Console IDs map to flagging IDs only in `bridge.py` (`TwinMap`): console rack A01, A02, B01, B02 = servers 12-01, 12-02, 13-01, 13-02, and console slot *n* = GPU *n-1*.

Scenarios:
- **S01–S12 (showcase)** are generated from `generate.py` with seed 7. Each covers the whole 32-GPU hall, and S04 runs two unrelated faults at once. They are rebuilt on first use and cached under `generated/console/`, keyed on the code and config that produced them.
- **F01–F12 and B01–B11** are the hand-labelled files. They contain telemetry only for the entities each scenario is about, so the other GPUs correctly show as *no data*.

    python3 bridge.py catalog
    python3 bridge.py analyse S01-straggler-gpu-thermal-throttle
    python3 bridge.py entity  S01-straggler-gpu-thermal-throttle gpu-a02-06
    python3 test_bridge.py    # all 12 showcase scenarios match their labels, plus mapping, validation and stale-data tests

All of the data is synthetic.
