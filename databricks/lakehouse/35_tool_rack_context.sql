-- Agent tool: every GPU in one rack with its job and cooling context at the cursor.
CREATE OR REPLACE FUNCTION ${evidence}.tool_rack_context(
  p_run_id STRING COMMENT 'Replay UUID returned by tool_list_runs',
  p_rack_id STRING COMMENT 'Rack ID such as rack-b',
  p_as_of_ts TIMESTAMP COMMENT 'Replay cursor in UTC. No sample later than this is visible')
RETURNS TABLE
COMMENT 'Agent tool. Latest GPU, workload and cooling observations for every GPU in one rack at the replay cursor, each with its own freshness flag. Check the flag before interpreting a value: a stale stream is unknown.'
RETURN
WITH telemetry_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS rn
  FROM ${evidence}.telemetry
  WHERE run_id = p_run_id AND event_ts <= p_as_of_ts
), workload_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS rn
  FROM ${evidence}.workloads
  WHERE run_id = p_run_id AND event_ts <= p_as_of_ts
), cooling_ranked AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cooling_zone_id ORDER BY event_ts DESC) AS rn
  FROM ${evidence}.cooling
  WHERE run_id = p_run_id AND event_ts <= p_as_of_ts
)
SELECT i.gpu_id, i.vendor, i.model, i.rack_id, i.server_id, i.cooling_zone_id,
       i.power_limit_w, i.temperature_limit_c,
       t.event_ts AS telemetry_ts,
       CASE WHEN t.heartbeat_ok = TRUE AND unix_micros(p_as_of_ts) - unix_micros(t.event_ts) <= 15000000
            THEN TRUE ELSE FALSE END AS telemetry_is_observed,
       t.power_w, t.gpu_util_pct, t.temp_c, t.clock_mhz, t.throttled, t.ecc_uncorrected_total,
       w.event_ts AS workload_ts,
       CASE WHEN w.event_ts IS NOT NULL AND unix_micros(p_as_of_ts) - unix_micros(w.event_ts) <= 15000000
            THEN TRUE ELSE FALSE END AS workload_is_fresh,
       w.job_id, w.job_state, w.progress_units, w.priority,
       c.event_ts AS cooling_ts,
       CASE WHEN c.event_ts IS NOT NULL AND unix_micros(p_as_of_ts) - unix_micros(c.event_ts) <= 15000000
            THEN TRUE ELSE FALSE END AS cooling_is_fresh,
       c.inlet_temp_c, c.flow_pct
FROM ${evidence}.inventory AS i
LEFT JOIN telemetry_ranked AS t ON i.gpu_id = t.gpu_id AND t.rn = 1
LEFT JOIN workload_ranked AS w ON i.gpu_id = w.gpu_id AND w.rn = 1
LEFT JOIN cooling_ranked AS c ON i.cooling_zone_id = c.cooling_zone_id AND c.rn = 1
WHERE i.rack_id = p_rack_id
ORDER BY i.gpu_id
LIMIT 24
