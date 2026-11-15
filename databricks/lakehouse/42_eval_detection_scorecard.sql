-- Scores the official detector against the answer key, entirely from persisted assessments.
CREATE OR REPLACE VIEW ${eval}.detection_scorecard
COMMENT 'Per replay: did the official detector flag when it should, did an incident name the expected root, and how long after onset each happened. Latency includes the detector''s 300-second persistence requirement and the spacing of assessed cursors.'
AS
WITH coverage AS (
  SELECT run_id, COUNT(*) AS cursors_assessed FROM ${evidence}.detector_snapshots GROUP BY run_id
), flags AS (
  SELECT run_id, MIN(as_of_ts) AS first_flag_cursor, COUNT(DISTINCT incident_id) AS distinct_incident_ids,
         collect_set(root_entity) AS flagged_root_entities
  FROM ${evidence}.incident_timeline GROUP BY run_id
), named AS (
  SELECT t.run_id, MIN(t.as_of_ts) AS first_root_named_cursor
  FROM ${evidence}.incident_timeline AS t
  JOIN ${eval}.ground_truth AS g ON g.run_id = t.run_id AND t.root_entity = g.expected_root_entity
  GROUP BY t.run_id
)
SELECT g.run_id, g.scenario, g.expected_cause, g.expected_flag, g.expected_root_entity,
       COALESCE(c.cursors_assessed, 0) AS cursors_assessed,
       f.first_flag_cursor,
       unix_timestamp(f.first_flag_cursor) - unix_timestamp(g.event_start_ts) AS seconds_from_onset_to_first_flag,
       n.first_root_named_cursor,
       unix_timestamp(n.first_root_named_cursor) - unix_timestamp(g.event_start_ts) AS seconds_from_onset_to_root_named,
       COALESCE(f.distinct_incident_ids, 0) AS distinct_incident_ids,
       f.flagged_root_entities,
       CASE WHEN c.cursors_assessed IS NULL THEN 'not_assessed'
            WHEN g.expected_flag AND f.first_flag_cursor IS NULL THEN 'missed'
            WHEN NOT g.expected_flag AND f.first_flag_cursor IS NOT NULL THEN 'false_alarm'
            WHEN NOT g.expected_flag THEN 'correctly_quiet'
            WHEN n.first_root_named_cursor IS NOT NULL THEN 'detected_root_named'
            ELSE 'detected_other_root' END AS verdict
FROM ${eval}.ground_truth AS g
LEFT JOIN coverage AS c ON c.run_id = g.run_id
LEFT JOIN flags AS f ON f.run_id = g.run_id
LEFT JOIN named AS n ON n.run_id = g.run_id
