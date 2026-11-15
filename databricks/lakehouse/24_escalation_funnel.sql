-- Gold: how much telemetry the deterministic layer absorbs before anything reaches a reasoning agent.
CREATE OR REPLACE VIEW ${evidence}.escalation_funnel
COMMENT 'Per replay: evidence rows stored, metric records the detector analysed, incident observations across cursors, and distinct incident IDs (what an agent is actually sent once incidents keep their identity).'
AS
WITH evidence_rows AS (
  SELECT run_id, COUNT(*) AS row_count FROM ${evidence}.telemetry GROUP BY run_id
  UNION ALL SELECT run_id, COUNT(*) FROM ${evidence}.workloads GROUP BY run_id
  UNION ALL SELECT run_id, COUNT(*) FROM ${evidence}.cooling GROUP BY run_id
), stored AS (
  SELECT run_id, SUM(row_count) AS evidence_rows FROM evidence_rows GROUP BY run_id
), assessed AS (
  SELECT run_id, COUNT(*) AS cursors_assessed, SUM(records_analyzed) AS metric_records_analyzed,
         MAX(records_analyzed) AS largest_window_records
  FROM ${evidence}.detector_snapshots GROUP BY run_id
), escalated AS (
  SELECT run_id, COUNT(*) AS incident_observations, COUNT(DISTINCT incident_id) AS distinct_incident_ids
  FROM ${evidence}.incident_timeline GROUP BY run_id
)
SELECT stored.run_id, stored.evidence_rows,
       COALESCE(assessed.cursors_assessed, 0) AS cursors_assessed,
       COALESCE(assessed.metric_records_analyzed, 0) AS metric_records_analyzed,
       assessed.largest_window_records,
       COALESCE(escalated.incident_observations, 0) AS incident_observations,
       COALESCE(escalated.distinct_incident_ids, 0) AS distinct_incident_ids
FROM stored
LEFT JOIN assessed ON assessed.run_id = stored.run_id
LEFT JOIN escalated ON escalated.run_id = stored.run_id
