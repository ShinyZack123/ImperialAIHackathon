-- Gold: the supporting observations behind each incident, one row per finding.
CREATE OR REPLACE VIEW ${evidence}.incident_findings
COMMENT 'Supporting findings for each official incident: which detector fired, on which entity and metric, and over what interval.'
AS
SELECT s.run_id, s.as_of_ts, i.incidentId AS incident_id, i.rootEntity AS root_entity,
       f.detector, f.severity, f.entity AS entity_id, f.metric,
       to_timestamp(f.`start`) AS finding_start_ts, to_timestamp(f.`end`) AS finding_end_ts, f.method, f.text
FROM ${evidence}.detector_snapshots AS s
LATERAL VIEW explode(s.incidents) exploded_incidents AS i
LATERAL VIEW explode(i.findings) exploded_findings AS f
