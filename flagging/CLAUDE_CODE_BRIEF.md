# Brief: synthetic datacentre telemetry + deterministic flagging

## Goal
Build two things for a hackathon demo:
1. A generator of synthetic AI-datacentre telemetry (normal operation, real faults, benign deviations).
2. A deterministic flagging function that decides what to send to the low-level agent, and writes each flag as a plain-English string saying what is wrong and why.

No ML in the flagging path. Same input must always give the same output.

## What already exists
`datacentre-scenarios/` holds 23 labelled scenarios on one shared topology
(2 racks, 32 H100 GPUs, InfiniBand network, A/B power feeds, one liquid-cooling CDU, 3 workloads).
- `index.json`: list of scenarios with expected flag / no-flag and reason. Read this first.
- `SCHEMA_SUGGESTIONS.md`: schema gaps, ranked by priority.
- 12 scenarios must flag, 11 must not. 5 are near-miss pairs
  (e.g. a 90-second checkpoint dip vs a 24-minute stuck checkpoint).

Treat `index.json` as the answer key. Do not edit labels to make tests pass.

## Steps (do in order, stop and show me the result after each)

### 1. Read and summarise
Read `index.json`, `SCHEMA_SUGGESTIONS.md` and two or three scenario files.
Summarise the file layout and data shapes in under 15 lines.

### 2. Fill the schema gaps
Apply the top-priority items from `SCHEMA_SUGGESTIONS.md`. At minimum:
- per-metric baselines (normal value and spread)
- thresholds (hard limits, e.g. thermal limit, PDU capacity)
- workload phase (e.g. running, checkpointing, starting, finishing)
- `MaintenanceRecord` (entity, start, end)
- `Intervention` (our own deliberate actions, e.g. a power cap)

### 3. Seeded generator
Write `generate.py` (or .ts):
- input: topology, scenario name, random seed, duration
- output: replayable JSON/JSONL telemetry files in the `TelemetryRecord` shape, plus context (workloads, maintenance, interventions)
- one "injector" per fault or benign deviation, composable on top of normal noise
- fixed seed gives byte-identical output
- error counters only ever increase; timestamps stay inside the window

### 4. Flagging function
Write a pure function: `flag(telemetry, context) -> list[str]`.

Detection methods (use the simplest one that catches each scenario):
- Robust z-score using median and MAD (median absolute deviation) against a rolling baseline. One bad sample cannot skew it.
- EWMA (exponentially weighted moving average) to smooth noise and ignore single spikes.
- CUSUM (cumulative sum of small deviations) for slow drift, e.g. a multi-day temperature creep.
- Peer comparison: compare each GPU with others on the same workload. Flag the outlier.
- Counter rate-of-change: flag error counters that climb fast, not ones that are merely non-zero.
- Hard thresholds for absolute limits (thermal limit, PDU load, latency SLA).
- Staleness check: no new samples from a source means a hung exporter.
- Persistence window: a deviation must last N seconds before it flags.

Context gates (suppress a statistical hit when it is expected):
- inside a maintenance window for that entity
- workload phase explains it (checkpoint, start, finish), within a time budget
- caused by our own intervention
- sample marked invalid or low-confidence
- entity has no workload or link by design (e.g. unused switch port)

A gate must expire. A checkpoint dip is fine for ~2 minutes, not 24. Maintenance is fine until the window ends, not after.

### 5. Flag string format
One string per flag, readable by an LLM agent with no other context. Include:
- severity and entity ID / name
- what is wrong: metric, observed vs expected, how long
- why it was flagged: which method fired, and which gates were checked and did not apply
- blast radius: affected workloads and connected hardware (from the Connection graph)
- likely cause, if the graph suggests one (e.g. shared CDU, shared cable)

Example:
`[CRITICAL] gpu-12-04-03 (Rack 12 / Server 4 / GPU 3): temperature 91C vs baseline 72C for 14 min, thermal throttling active. Fired: robust z-score 6.2 + hard threshold. Not explained by: maintenance, workload phase, own intervention. Affects: training-job-104 (step time +38%). 7 other GPUs on cooling-loop-02 also rising: likely upstream CDU fault.`

Merge correlated hits into one flag where they share a root cause.

### 6. Tests
- One test per scenario: output matches `index.json` (flag or no flag, right entity).
- Print a table: scenario, expected, actual, method that fired or gate that suppressed.
- All 23 must pass, including the 5 near-miss pairs.

### 7. Generalisation check
Generate 20 fresh scenarios with new seeds and varied magnitudes.
Report false positives and misses. Fix rules, not labels.

### 8. Demo script
One command that replays a fault scenario and a benign near-miss side by side and prints the flag strings.

## Constraints
- Deterministic. No randomness or ML in `flag()`.
- All tunables (window sizes, z cut-offs, time budgets) in one config file.
- Keep it small: standard library plus numpy at most.
- Explain any jargon in plain English in comments and output.
