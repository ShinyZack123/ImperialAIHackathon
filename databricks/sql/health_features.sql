-- Databricks processing step, not a diagnosis. All reads stop at the replay cursor.
-- Bind run_id STRING, as_of_ts TIMESTAMP, window_start_ts TIMESTAMP, rule_version STRING.
WITH t_latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS rn
  FROM telemetry WHERE run_id = :run_id AND event_ts <= :as_of_ts
), t_window AS (
  SELECT *,
    LAG(ecc_uncorrected_total) OVER (PARTITION BY gpu_id ORDER BY event_ts) AS prior_ecc,
    LAG(event_ts) OVER (PARTITION BY gpu_id ORDER BY event_ts) AS prior_ts
  FROM telemetry
  WHERE run_id = :run_id AND event_ts >= :window_start_ts AND event_ts <= :as_of_ts
), t_stats AS (
  SELECT gpu_id,
    unix_timestamp(MAX(event_ts)) - unix_timestamp(MIN(event_ts)) AS window_seconds,
    MAX(ecc_uncorrected_total) - MIN(ecc_uncorrected_total) AS counter_delta,
    COUNT(*) AS samples, COUNT(ecc_uncorrected_total) AS counter_samples,
    SUM(CASE WHEN heartbeat_ok = TRUE THEN 1 ELSE 0 END) AS observed_samples,
    MAX(CASE WHEN ecc_uncorrected_total < prior_ecc THEN 1 ELSE 0 END) AS counter_reset,
    MAX(unix_timestamp(event_ts) - unix_timestamp(prior_ts)) AS max_gap
  FROM t_window GROUP BY gpu_id
), w_latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY gpu_id ORDER BY event_ts DESC) AS rn
  FROM workloads WHERE run_id = :run_id AND event_ts <= :as_of_ts
), w_window AS (
  SELECT *,
    LAG(progress_units) OVER (PARTITION BY gpu_id, job_id ORDER BY event_ts) AS prior_progress,
    LAG(event_ts) OVER (PARTITION BY gpu_id, job_id ORDER BY event_ts) AS prior_ts
  FROM workloads
  WHERE run_id = :run_id AND event_ts >= :window_start_ts AND event_ts <= :as_of_ts
), w_stats AS (
  SELECT gpu_id, job_id,
    unix_timestamp(MAX(event_ts)) - unix_timestamp(MIN(event_ts)) AS window_seconds,
    MAX(progress_units) - MIN(progress_units) AS progress_delta,
    COUNT(*) AS samples, COUNT(progress_units) AS progress_samples,
    SUM(CASE WHEN job_state = 'running' THEN 1 ELSE 0 END) AS running_samples,
    MAX(CASE WHEN progress_units < prior_progress THEN 1 ELSE 0 END) AS counter_reset,
    MAX(unix_timestamp(event_ts) - unix_timestamp(prior_ts)) AS max_gap
  FROM w_window GROUP BY gpu_id, job_id
), c_latest AS (
  SELECT *, ROW_NUMBER() OVER (PARTITION BY cooling_zone_id ORDER BY event_ts DESC) AS rn
  FROM cooling WHERE run_id = :run_id AND event_ts <= :as_of_ts
), joined AS (
  SELECT i.*, t.event_ts AS telemetry_ts, t.heartbeat_ok,
    t.power_w AS raw_power_w, t.gpu_util_pct AS raw_util, t.temp_c AS raw_temp_c,
    t.throttled AS raw_throttled,
    CASE WHEN t.heartbeat_ok = TRUE
      AND unix_timestamp(:as_of_ts) - unix_timestamp(t.event_ts) <= 15
      THEN TRUE ELSE FALSE END AS telemetry_is_observed,
    ts.window_seconds AS ecc_window_seconds, ts.counter_delta,
    CASE WHEN ts.samples >= 2 AND ts.samples = ts.counter_samples
      AND ts.samples = ts.observed_samples AND ts.counter_reset = 0
      AND ts.window_seconds >= 55 AND ts.max_gap <= 15
      THEN TRUE ELSE FALSE END AS ecc_window_complete,
    w.event_ts AS workload_ts, w.job_id, w.job_state,
    CASE WHEN w.event_ts IS NOT NULL
      AND unix_timestamp(:as_of_ts) - unix_timestamp(w.event_ts) <= 15
      THEN TRUE ELSE FALSE END AS workload_is_fresh,
    ws.window_seconds AS progress_window_seconds, ws.progress_delta,
    CASE WHEN ws.samples >= 2 AND ws.samples = ws.progress_samples
      AND ws.samples = ws.running_samples AND ws.counter_reset = 0
      AND ws.window_seconds >= 55 AND ws.max_gap <= 15
      THEN TRUE ELSE FALSE END AS progress_window_complete,
    c.event_ts AS cooling_ts, c.inlet_temp_c AS raw_inlet_temp_c, c.flow_pct AS raw_flow_pct,
    CASE WHEN c.event_ts IS NOT NULL
      AND unix_timestamp(:as_of_ts) - unix_timestamp(c.event_ts) <= 15
      THEN TRUE ELSE FALSE END AS cooling_is_fresh
  FROM inventory i
  LEFT JOIN t_latest t ON i.gpu_id = t.gpu_id AND t.rn = 1
  LEFT JOIN t_stats ts ON i.gpu_id = ts.gpu_id
  LEFT JOIN w_latest w ON i.gpu_id = w.gpu_id AND w.rn = 1
  LEFT JOIN w_stats ws ON i.gpu_id = ws.gpu_id AND w.job_id = ws.job_id
  LEFT JOIN c_latest c ON i.cooling_zone_id = c.cooling_zone_id AND c.rn = 1
)
SELECT :run_id AS run_id, :as_of_ts AS as_of_ts, :rule_version AS rule_version,
  gpu_id, rack_id, vendor, cooling_zone_id,
  telemetry_ts, unix_timestamp(:as_of_ts) - unix_timestamp(telemetry_ts) AS telemetry_age_seconds,
  telemetry_is_observed,
  CASE WHEN telemetry_is_observed THEN raw_power_w END AS power_w,
  CASE WHEN telemetry_is_observed THEN raw_util END AS gpu_util_pct,
  CASE WHEN telemetry_is_observed THEN raw_temp_c END AS temp_c,
  CASE WHEN telemetry_is_observed THEN raw_throttled END AS throttled,
  power_limit_w, temperature_limit_c,
  CASE WHEN telemetry_is_observed AND power_limit_w > 0 THEN raw_power_w / power_limit_w END AS power_ratio,
  CASE WHEN telemetry_is_observed THEN temperature_limit_c - raw_temp_c END AS temperature_headroom_c,
  CASE WHEN telemetry_is_observed AND ecc_window_complete THEN counter_delta END AS ecc_delta_60s,
  ecc_window_seconds, workload_ts, workload_is_fresh, job_id, job_state,
  CASE WHEN workload_is_fresh AND progress_window_complete THEN progress_delta END AS progress_delta_60s,
  progress_window_seconds, cooling_ts, cooling_is_fresh,
  CASE WHEN cooling_is_fresh THEN raw_inlet_temp_c END AS inlet_temp_c,
  CASE WHEN cooling_is_fresh THEN raw_flow_pct END AS flow_pct
FROM joined
ORDER BY gpu_id;
