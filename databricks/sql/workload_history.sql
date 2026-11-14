-- Bind run_id/gpu_id (STRING), window_start_ts/as_of_ts (TIMESTAMP).
-- Historical evidence for progress/stall detection; newest first.
SELECT run_id, event_ts, gpu_id, job_id, job_state, progress_units, priority
FROM workloads
WHERE run_id = :run_id
  AND gpu_id = :gpu_id
  AND event_ts >= :window_start_ts
  AND event_ts <= :as_of_ts
ORDER BY event_ts DESC
LIMIT 180;
