-- Bounded evidence for the canonical deterministic flagger adapter.
-- Fetch one extra row per stream so the backend rejects overflow, never partial evidence.
-- JSON preserves SQL nulls; timestamps are serialized in UTC. No evaluation labels are read.
WITH inventory_bounded AS (
  SELECT gpu_id, vendor, model, rack_id, server_id, cooling_zone_id, power_limit_w, temperature_limit_c FROM inventory
  LIMIT 25
),
telemetry_bounded AS (
  SELECT run_id, event_ts, gpu_id, power_w, gpu_util_pct, memory_used_gb, memory_total_gb, temp_c, clock_mhz, throttled, ecc_uncorrected_total, heartbeat_ok FROM telemetry
  WHERE run_id = :run_id AND event_ts >= :window_start_ts AND event_ts <= :as_of_ts
  LIMIT 4345
),
workloads_bounded AS (
  SELECT run_id, event_ts, gpu_id, job_id, job_state, progress_units, priority FROM workloads
  WHERE run_id = :run_id AND event_ts >= :window_start_ts AND event_ts <= :as_of_ts
  LIMIT 4345
),
cooling_bounded AS (
  SELECT run_id, event_ts, cooling_zone_id, inlet_temp_c, flow_pct FROM cooling
  WHERE run_id = :run_id AND event_ts >= :window_start_ts AND event_ts <= :as_of_ts
  LIMIT 4345
)
SELECT 'inventory' AS dataset, COUNT(*) AS row_count,
  to_json(collect_list(named_struct('gpu_id', gpu_id, 'vendor', vendor, 'model', model, 'rack_id', rack_id, 'server_id', server_id, 'cooling_zone_id', cooling_zone_id, 'power_limit_w', power_limit_w, 'temperature_limit_c', temperature_limit_c)), map('ignoreNullFields', 'false', 'timeZone', 'UTC')) AS payload
FROM inventory_bounded
UNION ALL
SELECT 'telemetry' AS dataset, COUNT(*) AS row_count,
  to_json(collect_list(named_struct('run_id', run_id, 'event_ts', event_ts, 'gpu_id', gpu_id, 'power_w', power_w, 'gpu_util_pct', gpu_util_pct, 'memory_used_gb', memory_used_gb, 'memory_total_gb', memory_total_gb, 'temp_c', temp_c, 'clock_mhz', clock_mhz, 'throttled', throttled, 'ecc_uncorrected_total', ecc_uncorrected_total, 'heartbeat_ok', heartbeat_ok)), map('ignoreNullFields', 'false', 'timeZone', 'UTC')) AS payload
FROM telemetry_bounded
UNION ALL
SELECT 'workloads' AS dataset, COUNT(*) AS row_count,
  to_json(collect_list(named_struct('run_id', run_id, 'event_ts', event_ts, 'gpu_id', gpu_id, 'job_id', job_id, 'job_state', job_state, 'progress_units', progress_units, 'priority', priority)), map('ignoreNullFields', 'false', 'timeZone', 'UTC')) AS payload
FROM workloads_bounded
UNION ALL
SELECT 'cooling' AS dataset, COUNT(*) AS row_count,
  to_json(collect_list(named_struct('run_id', run_id, 'event_ts', event_ts, 'cooling_zone_id', cooling_zone_id, 'inlet_temp_c', inlet_temp_c, 'flow_pct', flow_pct)), map('ignoreNullFields', 'false', 'timeZone', 'UTC')) AS payload
FROM cooling_bounded;
