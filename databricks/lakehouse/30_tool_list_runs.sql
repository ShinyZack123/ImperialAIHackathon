-- Agent tool: discover replays. Returns no scenario names or labels.
CREATE OR REPLACE FUNCTION ${evidence}.tool_list_runs()
RETURNS TABLE
COMMENT 'Agent tool. Lists replay runs with their recorded interval. Use one run_id and one as_of_ts for a whole investigation; runs are independent and must never be pooled.'
RETURN
SELECT run_id, MIN(event_ts) AS start_ts, MAX(event_ts) AS end_ts, COUNT(*) AS sample_count
FROM ${evidence}.telemetry
GROUP BY run_id
ORDER BY run_id
LIMIT 100
