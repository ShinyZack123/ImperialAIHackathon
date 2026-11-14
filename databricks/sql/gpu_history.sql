-- Bind run_id/gpu_id (STRING), window_start_ts/as_of_ts (TIMESTAMP).
-- Newest first; reverse rows for a chronological chart. At most 180 samples.
-- Null readings remain null. A missing heartbeat is not a zero-power GPU.
SELECT run_id, event_ts, gpu_id, power_w, gpu_util_pct,
       memory_used_gb, memory_total_gb, temp_c, clock_mhz,
       throttled, ecc_uncorrected_total, heartbeat_ok
FROM telemetry
WHERE run_id = :run_id
  AND gpu_id = :gpu_id
  AND event_ts >= :window_start_ts
  AND event_ts <= :as_of_ts
ORDER BY event_ts DESC
LIMIT 180;
