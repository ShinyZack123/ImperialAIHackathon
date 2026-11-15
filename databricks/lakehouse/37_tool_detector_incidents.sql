-- Agent tool: what the deterministic detector escalated, as of the cursor and never later.
CREATE OR REPLACE FUNCTION ${evidence}.tool_detector_incidents(
  p_run_id STRING COMMENT 'Replay UUID returned by tool_list_runs',
  p_as_of_ts TIMESTAMP COMMENT 'Replay cursor in UTC. Only assessments made at or before this cursor are visible')
RETURNS TABLE
COMMENT 'Agent tool. Incidents from the newest official detector assessment at or before the replay cursor. root_entity is the detector''s grouping hypothesis, not a confirmed physical cause; investigate it with the history and context tools before proposing anything.'
RETURN
WITH newest AS (
  SELECT MAX(as_of_ts) AS as_of_ts
  FROM ${evidence}.detector_snapshots
  WHERE run_id = p_run_id AND as_of_ts <= p_as_of_ts
)
SELECT t.run_id, t.as_of_ts AS assessed_at, t.incident_id, t.status, t.severity, t.root_entity,
       t.onset_ts, t.last_seen_ts, t.detectors, t.affected_gpu_ids, t.finding_count, t.message
FROM ${evidence}.incident_timeline AS t
JOIN newest ON t.as_of_ts = newest.as_of_ts
WHERE t.run_id = p_run_id
ORDER BY t.severity, t.incident_id
LIMIT 50
