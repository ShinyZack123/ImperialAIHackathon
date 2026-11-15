-- Scores the official detector against the answer key, entirely from persisted assessments.
CREATE OR REPLACE VIEW ${eval}.detection_scorecard
COMMENT 'Per replay: did the official detector flag when it should, name the expected root, and how long after onset did the first flag appear. Latency includes the detector''s 300-second persistence requirement and the spacing of assessed cursors.'
AS
WITH coverage AS (
  SELECT run_id, COUNT(*) AS cursors_assessed FROM ${evidence}.detector_snapshots GROUP BY run_id
), flags AS (
  SELECT run_id, MIN(as_of_ts) AS first_flag_cursor, COUNT(DISTINCT incident_id) AS distinct_incident_ids,
         collect_set(root_entity) AS flagged_root_entities
  FROM ${evidence}.incident_timeline GROUP BY run_id
)
SELECT g.run_id, g.scenario, g.expected_cause, g.expected_flag, g.expected_root_entity,
       COALESCE(c.cursors_assessed, 0) AS cursors_assessed,
       f.first_flag_cursor,
       unix_timestamp(f.first_flag_cursor) - unix_timestamp(g.event_start_ts) AS seconds_from_onset_to_first_flag,
       COALESCE(f.distinct_incident_ids, 0) AS distinct_incident_ids,
       f.flagged_root_entities,
       CASE WHEN c.cursors_assessed IS NULL THEN 'not_assessed'
            WHEN g.expected_flag AND f.first_flag_cursor IS NULL THEN 'missed'
            WHEN NOT g.expected_flag AND f.first_flag_cursor IS NOT NULL THEN 'false_alarm'
            WHEN NOT g.expected_flag THEN 'correctly_quiet'
            WHEN array_contains(f.flagged_root_entities, g.expected_root_entity) THEN 'detected_root_named'
            ELSE 'detected_other_root' END AS verdict
FROM ${eval}.ground_truth AS g
LEFT JOIN coverage AS c ON c.run_id = g.run_id
LEFT JOIN flags AS f ON f.run_id = g.run_id
