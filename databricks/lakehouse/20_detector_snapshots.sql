-- Silver: the newest official assessment per replay cursor, with the audit JSON parsed into typed columns.
CREATE OR REPLACE VIEW ${evidence}.detector_snapshots
COMMENT 'Newest official detector assessment per (run_id, as_of_ts), parsed from official_detector_results.snapshot_json. Source rows keep their detector fingerprint and evidence hash.'
AS
WITH latest AS (
  SELECT run_id, as_of_ts, detector_version, evidence_sha256, incident_count, snapshot_json, generated_at,
         ROW_NUMBER() OVER (PARTITION BY run_id, as_of_ts ORDER BY generated_at DESC) AS recency
  FROM ${evidence}.official_detector_results
)
SELECT run_id, as_of_ts, detector_version, evidence_sha256, incident_count, generated_at,
       CAST(get_json_object(snapshot_json, '$.engine.recordsAnalyzed') AS BIGINT) AS records_analyzed,
       CAST(get_json_object(snapshot_json, '$.overview.total_gpu_count') AS INT) AS total_gpu_count,
       CAST(get_json_object(snapshot_json, '$.overview.observed_gpu_count') AS INT) AS observed_gpu_count,
       CAST(get_json_object(snapshot_json, '$.overview.unknown_gpu_count') AS INT) AS unknown_gpu_count,
       CAST(get_json_object(snapshot_json, '$.overview.observed_gpu_power_kw') AS DOUBLE) AS observed_gpu_power_kw,
       from_json(get_json_object(snapshot_json, '$.incidents'),
         'ARRAY<STRUCT<incidentId: STRING, status: STRING, onset: STRING, lastSeen: STRING, severity: STRING, rootEntity: STRING, entities: ARRAY<STRING>, workloads: ARRAY<STRING>, detectors: ARRAY<STRING>, message: STRING, affectedGpuIds: ARRAY<STRING>, findings: ARRAY<STRUCT<detector: STRING, severity: STRING, entity: STRING, metric: STRING, `start`: STRING, `end`: STRING, text: STRING, method: STRING>>>>') AS incidents,
       from_json(get_json_object(snapshot_json, '$.racks'),
         'ARRAY<STRUCT<id: STRING, gpus: ARRAY<STRUCT<id: STRING, vendor: STRING, rack_id: STRING, server_id: STRING, cooling_zone_id: STRING, state: STRING, telemetry_is_observed: BOOLEAN, metrics: STRUCT<power_w: DOUBLE, temp_c: DOUBLE, gpu_util_pct: DOUBLE, throttled: BOOLEAN>>>>>') AS racks
FROM latest
WHERE recency = 1
