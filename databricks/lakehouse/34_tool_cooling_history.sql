-- Agent tool: the facility layer. Lets an agent test "is this a shared cooling problem?".
CREATE OR REPLACE FUNCTION ${evidence}.tool_cooling_history(
  p_run_id STRING COMMENT 'Replay UUID returned by tool_list_runs',
  p_cooling_zone_id STRING COMMENT 'Cooling zone ID such as zone-b',
  p_as_of_ts TIMESTAMP COMMENT 'Replay cursor in UTC. No sample later than this is visible',
  p_window_minutes INT DEFAULT 5 COMMENT 'Minutes of history ending at the cursor; clamped to 1-15')
RETURNS TABLE
COMMENT 'Agent tool. Coolant inlet temperature and flow for one cooling zone, newest first. flow_pct is percent of nominal flow, not litres per minute. Compare with GPU temperatures in the same zone to separate a shared facility cause from a single-device fault.'
RETURN
SELECT run_id, event_ts, cooling_zone_id, inlet_temp_c, flow_pct
FROM ${evidence}.cooling
WHERE run_id = p_run_id
  AND cooling_zone_id = p_cooling_zone_id
  AND event_ts <= p_as_of_ts
  AND event_ts >= timestampadd(MINUTE, -LEAST(GREATEST(COALESCE(p_window_minutes, 5), 1), 15), p_as_of_ts)
ORDER BY event_ts DESC
LIMIT 180
