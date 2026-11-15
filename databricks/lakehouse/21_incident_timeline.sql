-- Gold: one row per incident per assessed cursor. This is what an agent would be escalated.
CREATE OR REPLACE VIEW ${evidence}.incident_timeline
COMMENT 'Official detector incidents by replay cursor. incident_id is stable while the root entity and onset are unchanged; status is active or recovered. Findings describe an analysis window, not a confirmed physical diagnosis.'
AS
SELECT s.run_id, s.as_of_ts, i.incidentId AS incident_id, i.status, i.severity, i.rootEntity AS root_entity,
       to_timestamp(i.onset) AS onset_ts, to_timestamp(i.lastSeen) AS last_seen_ts,
       i.detectors, i.affectedGpuIds AS affected_gpu_ids, size(i.affectedGpuIds) AS affected_gpu_count,
       size(i.findings) AS finding_count, i.message, s.detector_version, s.evidence_sha256
FROM ${evidence}.detector_snapshots AS s
LATERAL VIEW explode(s.incidents) exploded AS i
