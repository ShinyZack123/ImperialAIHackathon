-- Run log for the scheduled detector job (notebook 04). One row per run.
CREATE TABLE IF NOT EXISTS ${evidence}.detector_job_runs (
  job_run_id STRING COMMENT 'Databricks job run ID; NULL for a manual notebook run',
  started_at TIMESTAMP NOT NULL,
  finished_at TIMESTAMP NOT NULL,
  compute STRING NOT NULL COMMENT 'Where the detector ran, e.g. databricks_serverless',
  detector_version STRING NOT NULL COMMENT 'SHA-256 fingerprint of the flagger, its configuration and the connector adapter',
  step_seconds INT NOT NULL COMMENT 'Spacing of planned replay cursors',
  cursors_planned INT NOT NULL,
  cursors_already_assessed INT NOT NULL COMMENT 'Skipped because this detector version had already saved them',
  cursors_assessed INT NOT NULL COMMENT 'Assessed and saved by this run',
  cursors_deferred INT NOT NULL COMMENT 'Left for a later run by the per-run cursor limit',
  incident_observations BIGINT NOT NULL,
  evidence_rows_read BIGINT NOT NULL
) USING DELTA
COMMENT 'Each run of the scheduled detector job: what it assessed, what it skipped and which detector version it used.'
