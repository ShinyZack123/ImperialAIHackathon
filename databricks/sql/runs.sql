-- Replay metadata only: no scenario names or ground-truth failure labels.
-- Choose a returned run_id and use its time range as the replay cursor bounds.
SELECT run_id,
       MIN(event_ts) AS start_ts,
       MAX(event_ts) AS end_ts,
       COUNT(*) AS sample_count
FROM telemetry
GROUP BY run_id
ORDER BY run_id
LIMIT 100;
