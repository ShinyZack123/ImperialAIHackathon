# Shared data contract — version 1.0

This is a proposed interface for the four teammates. Keep column names and units stable; version any breaking change. These are synthetic readings, not a hardware compatibility claim.

## Identity and time

- `gpu_id` identifies a device; `rack_id`, `server_id` and `cooling_zone_id` describe its physical context.
- `run_id` identifies one independent replay. Never join different runs just because their timestamps match.
- `event_ts` is a UTC timestamp. CSV uses ISO 8601 ending in `Z`; Databricks stores TIMESTAMP with the session time zone set to UTC.
- `as_of_ts` is the current replay time. Every investigation read must use `event_ts <= as_of_ts`.
- Samples arrive every five simulated seconds. The starter considers a latest sample stale after 15 seconds. This is a demo setting, not a vendor recommendation.

## Tables

### inventory

One row per `gpu_id`.

| Column | Type | Meaning |
|---|---|---|
| gpu_id | STRING | Stable device ID |
| vendor | STRING | NVIDIA, AMD or Intel |
| model | STRING | Explicitly synthetic model name |
| rack_id, server_id, cooling_zone_id | STRING | Topology |
| power_limit_w | DOUBLE | Configured illustrative GPU power limit, watts |
| temperature_limit_c | DOUBLE | Configured illustrative temperature limit, °C |

Limits belong to each device. Do not assume one threshold applies to every vendor/model. A production adapter should also supply supported sensors and permitted actions; this fixture has a common sensor set for simplicity.

### telemetry

Unique key: `(run_id, event_ts, gpu_id)`.

| Column | Type | Meaning |
|---|---|---|
| run_id, gpu_id | STRING | Replay and device IDs |
| event_ts | TIMESTAMP | Sample time |
| power_w | DOUBLE, nullable | Instantaneous GPU power, watts |
| gpu_util_pct | DOUBLE, nullable | Utilisation from 0 to 100 |
| memory_used_gb, memory_total_gb | DOUBLE, nullable | Memory in decimal gigabytes |
| temp_c | DOUBLE, nullable | GPU temperature, °C |
| clock_mhz | DOUBLE, nullable | GPU clock, MHz |
| throttled | BOOLEAN, nullable | Simulated hardware throttling signal |
| ecc_uncorrected_total | BIGINT, nullable | Cumulative uncorrected error counter within this run |
| heartbeat_ok | BOOLEAN | Whether the collector obtained a GPU sample |

An absent or stale sample means **unknown**, not zero, healthy or definitely broken. When `heartbeat_ok` is false, metric fields are NULL. Do not forward-fill these values into a health verdict. For cumulative errors, examine counter changes over an interval; old errors remain after recovery. Real collectors also need reset/boot identifiers to interpret counter resets.

### workloads

Unique key: `(run_id, event_ts, gpu_id)`. Columns: `run_id STRING`, `event_ts TIMESTAMP`, `gpu_id STRING`, `job_id STRING nullable`, `job_state STRING`, `progress_units BIGINT`, `priority STRING`.

`job_state` is `idle` or `running`. An idle device has no job. `progress_units` is a cumulative synthetic work counter per job, not tokens/sec or real training steps. A rate requires at least two samples for the same job. Priority is `normal` or `high`. Low GPU utilisation without scheduled work is not a fault.

### cooling

Unique key: `(run_id, event_ts, cooling_zone_id)`. Columns: `run_id STRING`, `event_ts TIMESTAMP`, `cooling_zone_id STRING`, `inlet_temp_c DOUBLE`, `flow_pct DOUBLE`.

`flow_pct` is percentage of configured nominal flow, not litres per minute. Use the same replay cursor for cooling and GPU observations and expose their separate ages.

## Dashboard conventions

- A sum of GPU readings is **observed GPU power**, not rack mains power or total data-centre consumption.
- When readings are missing, show the number of observed devices alongside power totals. Never label a partial total as complete.
- Power is W/kW. Energy is Wh/kWh and requires integrating readings over time. Do not calculate an energy total by simply summing watts.
- No PUE or cooling-power estimate is supplied by these fixtures.
- Keep device identity visible when telemetry is unknown. Distinguish stale data from a detected incident.

## Proposed model handoff

The deterministic detector emits a candidate incident with `incident_id`, `run_id`, `as_of_ts`, affected GPU IDs, its rule name, and timestamped observations. The investigating model uses those IDs and the parameterised queries to collect additional evidence.

Suggested model output (agree with the model teammate before freezing it):

```json
{
  "incident_id": "example-incident-001",
  "run_id": "opaque-run-id",
  "as_of_ts": "2026-11-14T09:07:30Z",
  "status": "investigating",
  "suspected_cause": "shared_cooling_issue",
  "severity": "warning",
  "evidence": [
    {
      "gpu_id": "gpu-001",
      "event_ts": "2026-11-14T09:07:30Z",
      "field": "temp_c",
      "observation": "Replace with an actual retrieved reading"
    }
  ],
  "uncertainties": ["Physical cause is not confirmed by telemetry alone"],
  "recommended_action": "Inspect the cooling zone",
  "verification": "Not performed"
}
```

This is an interface example, not an actual model result. Do not claim measured confidence from an uncalibrated model score. Store tools, evidence and concise explanations; do not depend on exposing private model reasoning.

Ground-truth labels belong only to training/evaluation workflows, never to inference-time tools. Keep held-out evaluation runs out of fine-tuning. Different seeds on the same simple scenario templates provide only a limited demonstration of generalisation.

## First team integration checkpoint

All three consumers should successfully read the same `run_id` at the same `as_of_ts`, agree on units, and preserve NULL values. Then connect detector → investigator → incident panel. A Figma prototype, a trained model and a populated database are separate pieces until this path works end to end.

## Derived Databricks health snapshots

Notebook 02 computes `health_features` with Spark SQL and persists six baseline rule evaluations per GPU in `health_alerts`. The evidence tables above are unchanged. The baseline rules are replaceable scaffolding for the deterministic-model teammate, not a trained model or final diagnosis.

`health_features` is keyed by `(run_id, as_of_ts, gpu_id, rule_version)`. It includes identity, per-stream timestamps/freshness, observed power/temperature/utilisation/throttling, per-device power ratio and temperature headroom, recent ECC counter delta, recent job progress delta and cooling flow/temperature. See `sql/health_features.sql` for exact columns. The 60-second deltas require sufficient coverage and reject gaps, resets and unavailable counters; unknown stays NULL.

`health_alerts` is keyed by `(run_id, as_of_ts, gpu_id, rule_version, rule_id)`:

| Field | Meaning |
|---|---|
| alert_id | Stable hash of the snapshot and rule identity |
| run_id, as_of_ts | Exact evaluated replay snapshot |
| gpu_id, rack_id, vendor | Device context |
| rule_id, rule_version | Baseline condition and version (`baseline-v1`) |
| status | `active`, `clear` or `unknown`; always display alongside the condition |
| severity | Potential severity of this rule when active, not a current incident severity for clear/unknown rows |
| summary | Description of the rule condition; it is only asserted when status is active |
| evidence_json | JSON text containing the actual observations supporting the evaluation |

Read these rows via `GET /api/health-assessments?run_id=...&as_of_ts=...`. The API uses an exact timestamp match, returning `assessment_available: false` if that snapshot has not been processed. Its counts count rule evaluations, not distinct affected GPUs. All three statuses are preserved. Missing tables or query failures return HTTP errors instead of simulated success.

These snapshots are not an incident lifecycle: they do not track investigation ownership, acknowledge alerts, confirm root causes or execute remediation. The team can build those behaviors on the existing proposed model handoff.
