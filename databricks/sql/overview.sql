-- Bind run_id (STRING) and as_of_ts (TIMESTAMP). Set catalog/schema first.
-- Synthetic replay: no sample later than the replay cursor is visible.
-- Power is the sum of reporting GPUs, not rack/facility consumption.
WITH ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS sample_rank
  FROM telemetry
  WHERE run_id = :run_id AND event_ts <= :as_of_ts
), snapshot AS (
  SELECT i.gpu_id, t.power_w, t.gpu_util_pct, t.temp_c,
         CASE WHEN t.heartbeat_ok = TRUE
                   AND unix_micros(:as_of_ts) - unix_micros(t.event_ts) <= 15000000
              THEN TRUE ELSE FALSE END AS is_observed
  FROM inventory AS i
  LEFT JOIN ranked AS t ON i.gpu_id = t.gpu_id AND t.sample_rank = 1
)
SELECT COUNT(*) AS total_gpu_count,
       COUNT(CASE WHEN is_observed THEN 1 END) AS observed_gpu_count,
       COUNT(CASE WHEN NOT is_observed THEN 1 END) AS unknown_gpu_count,
       COUNT(CASE WHEN is_observed AND power_w IS NOT NULL THEN 1 END)
         AS power_reporting_gpu_count,
       SUM(CASE WHEN is_observed THEN power_w END) / 1000.0
         AS observed_gpu_power_kw,
       AVG(CASE WHEN is_observed THEN gpu_util_pct END)
         AS observed_avg_gpu_util_pct,
       AVG(CASE WHEN is_observed THEN temp_c END)
         AS observed_avg_temp_c
FROM snapshot;
