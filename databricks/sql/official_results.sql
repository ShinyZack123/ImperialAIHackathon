-- Official detector audit records for one exact replay cursor; never baseline rules.
SELECT run_id, as_of_ts, detector_version, evidence_sha256, incident_count,
       snapshot_json, generated_at
FROM official_detector_results
WHERE run_id = :run_id AND as_of_ts = :as_of_ts
ORDER BY generated_at DESC
LIMIT 50;
