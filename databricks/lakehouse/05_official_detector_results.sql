-- Same definition the result store creates on first save; kept here so the views apply on a fresh workspace.
CREATE TABLE IF NOT EXISTS ${evidence}.official_detector_results (
  run_id STRING NOT NULL, as_of_ts TIMESTAMP NOT NULL,
  detector_version STRING NOT NULL, evidence_sha256 STRING NOT NULL,
  incident_count BIGINT NOT NULL, snapshot_json STRING NOT NULL,
  generated_at TIMESTAMP NOT NULL
) USING DELTA
