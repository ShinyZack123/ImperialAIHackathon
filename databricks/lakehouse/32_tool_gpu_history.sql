-- Agent tool: one GPU's recent readings, bounded to 15 minutes and 180 samples.
CREATE OR REPLACE FUNCTION ${evidence}.tool_gpu_history(
  p_run_id STRING COMMENT 'Replay UUID returned by tool_list_runs',
  p_gpu_id STRING COMMENT 'Native GPU ID such as gpu-009',
  p_as_of_ts TIMESTAMP COMMENT 'Replay cursor in UTC. No sample later than this is visible',
  p_window_minutes INT DEFAULT 5 COMMENT 'Minutes of history ending at the cursor; clamped to 1-15')
RETURNS TABLE
COMMENT 'Agent tool. Time-ordered readings for one GPU ending at the replay cursor, newest first. NULL metrics with heartbeat_ok = false mean the collector had no sample: unknown, not zero. ecc_uncorrected_total is cumulative, so compare changes, not levels.'
RETURN
SELECT run_id, event_ts, gpu_id, power_w, gpu_util_pct, memory_used_gb, memory_total_gb,
       temp_c, clock_mhz, throttled, ecc_uncorrected_total, heartbeat_ok
FROM ${evidence}.telemetry
WHERE run_id = p_run_id
  AND gpu_id = p_gpu_id
  AND event_ts <= p_as_of_ts
  AND event_ts >= timestampadd(MINUTE, -LEAST(GREATEST(COALESCE(p_window_minutes, 5), 1), 15), p_as_of_ts)
ORDER BY event_ts DESC
LIMIT 180
