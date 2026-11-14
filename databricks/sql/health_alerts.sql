-- Bind run_id (STRING), as_of_ts (TIMESTAMP).
-- Exact evaluated snapshot only: an empty result does not mean all GPUs are healthy.
-- Include clear/unknown evaluations so the UI can distinguish them from active alerts.
SELECT alert_id, run_id, as_of_ts, gpu_id, rack_id, vendor,
       rule_id, rule_version, status, severity, summary, evidence_json
FROM health_alerts
WHERE run_id = :run_id
  AND as_of_ts = :as_of_ts
  AND rule_version = 'baseline-v1'
ORDER BY gpu_id, rule_id
LIMIT 1000;
