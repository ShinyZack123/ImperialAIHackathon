# Shared data contract — version 1.0

This contract covers connected and exported Databricks evidence, the official deterministic detector and website. Keep column names and units stable; version any breaking change. The diagnostic-model output later in this document remains a proposal. All readings are synthetic, not a hardware compatibility claim.

## Identity and time

- `gpu_id` identifies a device; `rack_id`, `server_id` and `cooling_zone_id` describe its physical context.
- `run_id` identifies one independent replay. Never join different runs just because their timestamps match.
- `event_ts` is a UTC timestamp. CSV uses ISO 8601 ending in `Z`; Databricks stores TIMESTAMP with the session time zone set to UTC.
- `as_of_ts` is the current replay time. Every investigation read must use `event_ts <= as_of_ts`.
- Samples arrive every five simulated seconds. The starter considers a latest sample stale after 15 seconds. This is a demo setting, not a vendor recommendation.
- The official detector receives only the selected run and at most 15 minutes ending at the exact cursor. A query never borrows future samples or another run's observations.
- Native Databricks IDs are retained by the connector. The 24-GPU/three-rack fixture is separate from the original 32-GPU/four-rack console twin and its scenario/tick clock; no slot mapping is fabricated.

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

## Official snapshot API

The website calls `GET /api/databricks/snapshot?run_id=<uuid>&as_of_ts=<UTC timestamp>` through the same-origin Node server. With `DATABRICKS_API_URL` configured, `OfficialDetector` reads the four evidence streams, validates and translates them through `backend/flagging_adapter.py`, invokes `flagging.flagger.flag_window`, and saves the result before returning success. The evidence tables are not modified. A read, detector or write failure returns an error, never a baseline substitute or an automatic switch to saved replay.

Without that setting, `backend/replay_bridge.py` reads `demo/evidence.json.gz`, checks its size and manifest hashes, and runs the current adapter and detector locally. This mode needs no Databricks login or SDK. Every response has `delivery: "saved_replay"` and `exported_at`; snapshots repeat these fields and use `persistence: {status: "not_saved", reason: "saved_replay"}`. No cloud query or write occurs. The connected proxy adds `delivery: "connected"`; older direct Python responses without this field are also connected. `source: "databricks"` identifies evidence origin, while `delivery` identifies how it was retrieved. Display both accurately and reject delivery changes within an investigation.

The HTTP envelope has `schema_version: "1.0"`, `source: "databricks"`, `synthetic: true`, `run_id`, `as_of_ts` and `data`. The snapshot inside `data` has the following fields:

| Field | Meaning |
|---|---|
| schemaVersion, source, synthetic | Snapshot version `1.0` and explicit provenance |
| run_id, as_of_ts | Selected replay UUID and exact UTC cutoff |
| window.start, window.end | Bounded evidence interval; end equals the cursor |
| state_scope | `analysis_window`: findings may concern earlier events in the interval |
| engine | `module: "flagging.flagger.flag_window"`, deterministic flag, version fingerprint, persistence seconds and analysed record count |
| overview | Total/observed/unknown GPU counts, power-reporting count, observed GPU power in kW, observed mean temperature/utilisation |
| racks | Native rack objects `{id, label, gpus}`; inventory drives their size |
| incidents | Official detector observations, described below |
| suppressed | Detector findings it suppressed, with entity, metric, detector and reason |
| coverage, limitations | Available evidence and explicit limits on interpretation |
| persistence | Connected: confirmed `status: "saved"`, table, detector version, evidence hash and generation time. Saved replay: `status: "not_saved"`, `reason: "saved_replay"`. |

Each `racks[].gpus[]` has source `id`, `vendor`, `model`, `rack_id`, `server_id`, `cooling_zone_id`, `state`, `state_scope`, `telemetry_is_observed`, `telemetry_ts` and `telemetry_age_seconds`. It also contains:

- `metrics`: nullable `power_w`, `temp_c`, `gpu_util_pct`, `clock_mhz`, `memory_used_gb`, `memory_total_gb`, `ecc_uncorrected_total`, `throttled`.
- `limits`: illustrative `power_w` and `temperature_c`.
- `workload`: separate `is_fresh`, `event_ts`, `job_id`, `state`, `progress_units`.
- `cooling`: separate `is_fresh`, `event_ts`, `flow_pct`, `inlet_temp_c`.

`state` is `clear`, `warning`, `critical` or `unknown`. Missing/stale latest GPU telemetry takes precedence as `unknown`, even if historical findings exist. `clear` means no detector finding for that GPU in the selected window, not verified healthy hardware. Workload/cooling freshness is independent; their values are not silently carried forward. A latest observed GPU sample can still contain a NULL metric, so use the power-reporting count when presenting totals.

An official incident contains `severity`, `rootEntity`, `entities`, `workloads`, `detectors`, `message`, `affectedGpuIds`, `scope: "analysis_window"` and `findings`. Each finding includes `detector`, `severity`, `entity`, `metric`, `start`, `end`, `text` and `method`. A root entity identifies the detector's grouping/hypothesis, not a confirmed physical cause: findings are grouped by component, dependency and time, and the root is the earliest evidence, preferring the layer closest to the facility among findings that began together.

Each incident also carries `incidentId`, `status`, `onset` and `lastSeen`. `incidentId` is a deterministic hash of the root entity and onset, so the same evidence always yields the same ID. A later cursor keeps it only while the root and onset are unchanged: the ID changes when the onset slides out of the 15-minute window or when newly qualifying evidence moves the onset earlier. It is not a durable lifecycle ID and there is no acknowledgement state. `status` is `active` or `recovered`. `recovered` means every flagged condition was later observed clear for 300 seconds within the window; discrete events (such as an uncorrected-error increment) and telemetry that simply stopped never count as recovery. The flagger's idle-capacity opportunities (`find_opportunities`) are not part of this snapshot.

`GET /api/databricks/runs` returns replay UUIDs and start/end timestamps. GPU `history` and `workload-history` endpoints return flat source rows in the same envelope and accept a 1–15-minute window. They preserve NULLs and the cursor. The website draws gaps for missing readings instead of fabricating zero values.

### Connector units and coverage

The adapter converts `temp_c` to `gpu.temperature_c` (C), `power_w` to `gpu.power_w` (W), utilisation to percent, ECC totals to counts and heartbeat to `exporter.gpu_up`. It retains generic `gpu.clock_mhz`; it does not rename it to a domain-specific SM or memory clock. Cooling `flow_pct` becomes `cdu.flow_percent` (% of nominal), not volumetric flow. `progress_units` becomes `workload.progress_units` in synthetic work units, not training steps or tokens. Shared job counters with conflicting values are rejected rather than aggregated without a contract.

Maintenance, operator interventions, workload phase, calibrated baselines and throttle reasons are not supplied. Detector messages must be read with `coverage` and `limitations`; missing context cannot exclude those explanations. Unsupported metrics/checks remain unavailable. The detector retains its 300-second hard-limit persistence requirement. A short anomaly may not trigger it, and an earlier flag can remain in the window after readings recover.

The adapter declares the fixture's five-second sampling cadence. Consecutive-condition and stalled-progress windows break across gaps over 15 seconds, so distant samples cannot establish an uninterrupted fault across missing evidence.

## Persisted official results

`official_detector_results` is a Delta table keyed by `(run_id, as_of_ts, detector_version, evidence_sha256)`:

| Field | Meaning |
|---|---|
| run_id, as_of_ts | Exact assessed replay snapshot |
| detector_version | SHA-256 fingerprint of the actual flagger, configuration and adapter |
| evidence_sha256 | Hash of the bounded source evidence with run/window provenance |
| incident_count | Number of official incidents in this snapshot |
| snapshot_json | Full official snapshot before the response-only `persistence` field is attached |
| generated_at | UTC assessment generation time, captured before the write |

The parameterised MERGE updates the same key on repeat evaluation; changed code or evidence yields a distinct audit record. A saved status is returned only after Databricks confirms the write. Failed or ambiguous writes produce an error rather than claiming persistence. Source labels/answer keys are excluded, and payload size and provenance are validated before saving. `DatabricksData.official_results(run_id, as_of_ts)` reads matching records for audit/model handoff. This table is separate from comparison `health_alerts`.

## Proposed diagnostic-model handoff

`examples/model_adapter.py` provides cursor-bound evidence tools. `InvestigationEvidence.health_assessments()` invokes the same official service and returns the saved snapshot, including candidate `incidents`. The diagnostic model can retrieve histories and rack context for those native entities. The adapter is connected; diagnostic LLM inference and lifecycle assignment are not yet implemented.

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

## Team integration checkpoint

The website and deterministic model now share one official service, and the diagnostic evidence adapter uses that service at the same run/cursor. Continue to preserve units, NULL values and provenance when adding diagnostic inference. Model output remains a proposal: it does not execute controls or prove a hardware fault. Each current assessment is explicitly requested; a recurring monitor and advancing replay clock are separate work.

## Comparison-only Databricks health snapshots

Notebook 02 computes `health_features` with Spark SQL and persists six baseline rule evaluations per GPU in `health_alerts`. The evidence tables above are unchanged. These rules are testing/comparison scaffolding, not the official detector or a fallback. They do not populate the website's official incidents. Different algorithms and persistence requirements can produce different counts.

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

Read these rows via `GET /api/health-assessments?run_id=...&as_of_ts=...` or the model adapter's explicitly named `.comparison_baseline()`. The HTTP response marks `assessment_kind: "comparison_baseline"` and `official: false`. This legacy endpoint differs from the adapter's `.health_assessments()`, which now calls the official service. The comparison API uses an exact timestamp match, returning `assessment_available: false` if notebook 02 has not processed it. Counts refer to rule evaluations, not official incidents or distinct affected GPUs. All three statuses are preserved. Missing tables or query failures return HTTP errors instead of simulated success.

These snapshots are not an incident lifecycle: they do not track investigation ownership, acknowledge alerts, confirm root causes or execute remediation. The team can build those behaviors on the existing proposed model handoff.
