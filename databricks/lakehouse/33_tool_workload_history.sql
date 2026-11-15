-- Agent tool: job progress for one GPU, to separate "busy" from "making progress".
CREATE OR REPLACE FUNCTION ${evidence}.tool_workload_history(
  p_run_id STRING COMMENT 'Replay UUID returned by tool_list_runs',
  p_gpu_id STRING COMMENT 'Native GPU ID such as gpu-001',
  p_as_of_ts TIMESTAMP COMMENT 'Replay cursor in UTC. No sample later than this is visible',
  p_window_minutes INT DEFAULT 5 COMMENT 'Minutes of history ending at the cursor; clamped to 1-15')
RETURNS TABLE
COMMENT 'Agent tool. Scheduler-side job state and cumulative progress for one GPU, newest first. progress_units is a synthetic work counter: a flat counter while job_state is running suggests a stall. An idle GPU with no job is not a fault.'
RETURN
SELECT run_id, event_ts, gpu_id, job_id, job_state, progress_units, priority
FROM ${evidence}.workloads
WHERE run_id = p_run_id
  AND gpu_id = p_gpu_id
  AND event_ts <= p_as_of_ts
  AND event_ts >= timestampadd(MINUTE, -LEAST(GREATEST(COALESCE(p_window_minutes, 5), 1), 15), p_as_of_ts)
ORDER BY event_ts DESC
LIMIT 180
