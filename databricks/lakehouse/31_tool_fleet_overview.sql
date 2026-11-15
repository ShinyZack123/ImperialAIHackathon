-- Agent tool: fleet totals at one cursor. The cursor bound lives here, so a caller cannot read the future.
CREATE OR REPLACE FUNCTION ${evidence}.tool_fleet_overview(
  p_run_id STRING COMMENT 'Replay UUID returned by tool_list_runs',
  p_as_of_ts TIMESTAMP COMMENT 'Replay cursor in UTC. No sample later than this is visible')
RETURNS TABLE
COMMENT 'Agent tool. Observed GPU power, utilisation and temperature at one replay cursor, plus how many GPUs are unknown. Power is the sum of reporting GPUs, not rack or facility power. A stale or missing reading counts as unknown, never zero or healthy.'
RETURN
WITH ranked AS (
  SELECT gpu_id, event_ts, power_w, gpu_util_pct, temp_c, heartbeat_ok,
         ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS sample_rank
  FROM ${evidence}.telemetry
  WHERE run_id = p_run_id AND event_ts <= p_as_of_ts
), snapshot AS (
  SELECT i.gpu_id, t.power_w, t.gpu_util_pct, t.temp_c,
         CASE WHEN t.heartbeat_ok = TRUE AND unix_micros(p_as_of_ts) - unix_micros(t.event_ts) <= 15000000
              THEN TRUE ELSE FALSE END AS is_observed
  FROM ${evidence}.inventory AS i
  LEFT JOIN ranked AS t ON i.gpu_id = t.gpu_id AND t.sample_rank = 1
)
SELECT COUNT(*) AS total_gpu_count,
       COUNT(CASE WHEN is_observed THEN 1 END) AS observed_gpu_count,
       COUNT(CASE WHEN NOT is_observed THEN 1 END) AS unknown_gpu_count,
       COUNT(CASE WHEN is_observed AND power_w IS NOT NULL THEN 1 END) AS power_reporting_gpu_count,
       SUM(CASE WHEN is_observed THEN power_w END) / 1000.0 AS observed_gpu_power_kw,
       AVG(CASE WHEN is_observed THEN gpu_util_pct END) AS observed_avg_gpu_util_pct,
       AVG(CASE WHEN is_observed THEN temp_c END) AS observed_avg_temp_c
FROM snapshot
