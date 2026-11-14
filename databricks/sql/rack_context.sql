-- Bind run_id/rack_id (STRING), as_of_ts (TIMESTAMP).
-- Returns raw last-known values with explicit freshness flags, not diagnoses.
-- Consumers must check each stream's flag before interpreting a reading.
WITH telemetry_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS rn
  FROM telemetry
  WHERE run_id = :run_id AND event_ts <= :as_of_ts
), workload_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS rn
  FROM workloads
  WHERE run_id = :run_id AND event_ts <= :as_of_ts
), cooling_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cooling_zone_id ORDER BY event_ts DESC) AS rn
  FROM cooling
  WHERE run_id = :run_id AND event_ts <= :as_of_ts
)
SELECT i.gpu_id, i.vendor, i.model, i.rack_id, i.server_id,
       i.cooling_zone_id, i.power_limit_w, i.temperature_limit_c,
       t.event_ts AS telemetry_ts,
       unix_timestamp(:as_of_ts) - unix_timestamp(t.event_ts) AS telemetry_age_seconds,
       CASE WHEN t.event_ts IS NOT NULL
                   AND unix_timestamp(:as_of_ts) - unix_timestamp(t.event_ts) <= 15
              THEN TRUE ELSE FALSE END AS telemetry_is_fresh,
       CASE WHEN t.heartbeat_ok = TRUE
                   AND unix_timestamp(:as_of_ts) - unix_timestamp(t.event_ts) <= 15
              THEN TRUE ELSE FALSE END AS telemetry_is_observed,
       t.heartbeat_ok, t.power_w, t.gpu_util_pct, t.memory_used_gb,
       t.memory_total_gb, t.temp_c, t.clock_mhz, t.throttled, t.ecc_uncorrected_total,
       w.event_ts AS workload_ts,
       CASE WHEN w.event_ts IS NOT NULL
                   AND unix_timestamp(:as_of_ts) - unix_timestamp(w.event_ts) <= 15
              THEN TRUE ELSE FALSE END AS workload_is_fresh,
       w.job_id, w.job_state, w.progress_units, w.priority,
       c.event_ts AS cooling_ts,
       CASE WHEN c.event_ts IS NOT NULL
                   AND unix_timestamp(:as_of_ts) - unix_timestamp(c.event_ts) <= 15
              THEN TRUE ELSE FALSE END AS cooling_is_fresh,
       c.inlet_temp_c, c.flow_pct
FROM inventory AS i
LEFT JOIN telemetry_ranked AS t ON i.gpu_id = t.gpu_id AND t.rn = 1
LEFT JOIN workload_ranked AS w ON i.gpu_id = w.gpu_id AND w.rn = 1
LEFT JOIN cooling_ranked AS c ON i.cooling_zone_id = c.cooling_zone_id AND c.rn = 1
WHERE i.rack_id = :rack_id
ORDER BY i.gpu_id
LIMIT 24;
