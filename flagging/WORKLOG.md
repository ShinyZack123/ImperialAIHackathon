# Work log

## Step 1: read and summarise (done)
- `datacentre-scenarios/` existed only inside `datacentre-scenarios.zip`, so I unzipped it in place. The top-level `index.json`, `gen.py` and `topology.json` are byte-identical to the copies in the zip.
- Contents: 23 scenarios (12 flag, 11 no-flag) and 5 near-miss pairs. Each scenario file has `expected`, `window`, `primaryEntityIds`, `context` and `telemetry[]` (TelemetryRecord).
- Already present in the data: `maintenance[]` (maint-0042 firmware upgrade, maint-0043 reserved ports), the checkpoint policy in workload attributes, and `workload.phase` as telemetry.
- Missing: baselines, limits, interventions, port admin state, redundancy semantics.

## Step 2: fill the schema gaps (done)
- Added `build_context.py`. It reads `topology.json` (left unchanged) and writes `topology.enriched.json`. Two runs gave identical output (sha1 4528006449172de98addc6c514c96622c66fcf4b).
- `metricCatalog`: 59 MetricDefinitions, covering every metric seen in the 23 scenarios (the script checks this). Each has a unit, a kind (gauge, counter, event or state), hard limits (read from entity attributes where available, e.g. `thermal.thermalLimitC`, PDU `electrical.capacityW`, workload `latencySlaMs`), a plausible range with max change per second (to catch sensor glitches), counter rate limits, a table of XID event codes, and throttle-reason classes (software vs hardware).
- `baselines`: 160 MetricBaselines, each with mean, median, stddev, MAD, p05 and p95. They are conditioned on workload type and state (loaded, checkpoint, idle), on hour of day for room and facility metrics (B07), and on demand (a linear model on requests/s) for inference GPUs (B08). The values come from `gen.py`'s normal-operation settings.
- Workloads gained `phase`, `phaseSince`, `checkpointPolicy`, `progress` and `expectedUtilizationPercent`. The inference workload also gained `desiredReplicas` and its replica-to-GPU map. There is now a phase enum with plain-English meanings.
- `interventions`: added int-0091 (B11 power cap 700 → 500 W on rack 12, active 16:40–18:00), with the expected throttle reason SW_POWER_CAP and expected effects. I also added the agent it names, `agent-datacenter-energy`.
- `maintenance`: the two existing records are checked against the MaintenanceRecord shape.
- Ports: added `adminState`. It is `up` when the port is connected, `reserved` for leaf-13-p05/p06 (from maint-0043), and `down` otherwise.
- `redundancyGroups`: 4 groups (feeds, UPS, PDU-12, PDU-13), each 2N with `requiredHealthy` = 2.
- Decision: physical facts live in the schema. Detector tunables (z cut-offs, windows, time budgets) will go in `config.json` in step 4.

## Next
Step 3 (seeded generator `generate.py`), then steps 4–8 from the brief.

## Session 2: reuse gen.py + flagger.py, measured baselines, steps 6–8

### Inputs picked up
- The updated zip contained `flagger.py`, `evaluate.py` and `flagger_results.json`. Extracted next to the step-2 files. On the plain `topology.json` it scored 23/23 before any change.
- `gen.py` is unchanged. `generate.py` executes its topology and helper section (`Scn`, `ramp`, `SOURCES`, `ALL_IDS`) from source, up to the first scenario.

### generate.py (new, replaces the "Next: step 3" note above)
- `normal(world)`: fault-free telemetry for the whole topology (16 training GPUs, 8 inference, 8 idle, CDU, PDUs, uplinks, storage, room, jobs). Periodic checkpoints, the daily temperature cycle and the day/night traffic curve are part of normal.
- 23 injectors that wrap the existing signals, so they compose. 27 presets. CLI: `--scenario --seed --minutes --step --start --out --jsonl --list`.
- Checked: same seed gives byte-identical output for all 27 presets; different seeds differ. `render()` asserts counters never decrease and timestamps are inside the window.
- Generated scenarios carry their own `contextRecords` (maintenance, interventions) and a label written by the injector's rule.

### build_context.py: baselines are now measured
- Removed the typed-in baseline numbers. Two fault-free runs are generated (24 h at 120 s; 2 h at 30 s with the fine-tune job running), about 278k records, then summarised.
- Output: 231 baselines + 7 counter-rate baselines. Conditions (idle / loaded / checkpoint, workload type, hour of day) are read from the workload status and phase telemetry in the run, not from generator internals.
- Demand models (straight-line fit against requests/s) are kept only where the fit is good (r² ≥ 0.5): inference GPU util, power, temperature, p99 latency, queue depth.
- New: peer-residual baseline (a GPU's temperature minus the median of its job peers) for CUSUM.
- Catalog changes: typed-in `normalPerHour` rates removed (now measured); `gpu.sm_clock_mhz` limit removed (max clock is a ceiling, and a hung GPU sitting at it tripped the limit); `gpu.power_w` limit is a warning, not critical.
- Still deterministic: two builds give the same sha1.

### flagger.py: reads topology.enriched.json
- Tunables moved to `config.json` (one file, each with a plain-English note).
- Hard limits, XID code table, bad state values, throttle classes, any-increase counters: all from `metricCatalog`. Limits resolve to the entity's own attribute where the catalog points at one.
- Robust z-score, CUSUM and the Poisson background rate use stored baselines. The old "first 25% of the window" is only a fallback when no stored baseline exists.
- Samples outside the catalog's plausible range are dropped as sensor glitches.
- Gates now use the enriched records: port `adminState`, `interventions` (active window, target, predicted effect size, expected throttle reason), `checkpointPolicy`.
- Gates log what they explained (checkpoint within budget, job finished, invalid sample), so the test table can name the gate.
- Added `flag(telemetry, context) -> list[str]` and `merge_context(topology, scenario)`.
- Flag string: added "Why flagged" with every method that fired, "Not explained by" (gates checked), gate notes when a gate expired, and "Blast radius" with GPU counts and servers.
- F09's step-time impact (+5%), listed as a known miss in FLAGGER_METHODS.md, is now caught because the stored baseline is tighter than the in-window one.

### Step 6: tests (`python3 test_scenarios.py`)
- One unittest per scenario from `index.json`: flag/no-flag, root-cause entity, determinism (two runs give identical strings), input not modified.
- Result: 23/23 pass, 5/5 near-miss pairs. Exit code 1 on any failure. Writes `flagger_results.json`.

### Step 7: generalisation (`python3 generalise.py [--seeds N]`)
- 20 fresh scenarios + 14 extras (more twins, two faults at once, four fault-free runs), each on the full normal world, with new seeds, entities, timings and magnitudes.
- First run: 18/20, two false positives, no misses. Extras 14/14. A 5-seed run found a third false positive.
- Rule fixes (no label was changed):
  1. Intervention gate stopped at the first intervention matching the entity, so an old expired one (int-0091) shadowed the active one. Now every intervention is checked; an expired one only matters if the finding started under it.
  2. Stall check read the job status only at the end of a flat period, so queued time counted as stalled. Now only time in state "running" counts.
  3. Frozen-value detector fired on a healthy gauge rounded to 0.1 A (repeats by chance). Now a repeat run must also be improbable given how often that signal normally repeats (`frozen_p`).
- Also fixed while there: the maintenance gate only looked at when a finding started; a server still down long after its window closed was suppressed. Now the whole span must be inside the window plus grace.
- After fixes: 340/340 runs correct over 10 noise seeds (200 fresh + 140 extra).
- Magnitude sweep (3 seeds each), where the decision flips:
  - checkpoint: no flag up to 260 s, flag from 300 s (budget 270 s)
  - one hot GPU (peers 66 °C): no flag at 69 °C, flag from 72 °C
  - slow drift over 2 days: no flag up to +5 °C, flag from +6 °C
  - CRC errors: no flag up to 3/min, flag from 5/min
  - logged power cap (predicted ≤ 15%): no flag at 14.5%, flag at 15.5%
  - corrected-memory-error burst: no flag up to 4, 1 of 3 at 6, flag from 10
  - down after maintenance end: no flag up to 16 min, flag from 20 min (grace 15 min)
  - CDU pump fault and replica crash flag at any magnitude (the fault state itself is the signal)

### Step 8: demo (`python3 demo.py`)
- Replays F10 (stuck checkpoint) and B01 (normal checkpoint) side by side in time order; at each tick the flagger sees only the records so far. Prints when the flag is first raised, the full flag strings, and why the benign twin was not flagged.
- `--pair 1..5`, `--list`, `--fault/--benign`, `--delay`.

### Caveats
- The fresh scenarios come from a generator I wrote with the same noise model as `gen.py`, so 340/340 shows the rules are not over-fitted to the 23 files, not that they hold on real DCGM/UFM data.
- Baselines are measured from one synthetic day. They pool all GPUs of a model, so a real per-GPU offset would need per-entity baselines.
- Grey-zone magnitudes the answer key does not define (e.g. a checkpoint between 1.5x and 3x expected) were not used as test cases.
- `datacentre-scenarios.zip` and the loose copies at the top level were not updated.
