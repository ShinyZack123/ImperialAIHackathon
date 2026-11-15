CREATE TABLE IF NOT EXISTS ${eval}.ground_truth (
  run_id STRING NOT NULL COMMENT 'Replay UUID',
  scenario STRING NOT NULL COMMENT 'Generator scenario name. An answer key: keep away from agents',
  expected_cause STRING NOT NULL COMMENT 'Label written by the generator',
  expected_flag BOOLEAN NOT NULL COMMENT 'Whether a correct detector should raise anything',
  expected_root_entity STRING COMMENT 'Entity a correct escalation should name; NULL when nothing should be raised',
  affected_gpu_ids ARRAY<STRING> NOT NULL COMMENT 'GPUs the scenario touches',
  event_start_ts TIMESTAMP NOT NULL COMMENT 'When the injected condition begins',
  scheduled_recovery_start_ts TIMESTAMP NOT NULL COMMENT 'Prerecorded recovery begins; not caused by any agent',
  fully_recovered_by_ts TIMESTAMP NOT NULL COMMENT 'Prerecorded recovery complete',
  loaded_at TIMESTAMP NOT NULL
) USING DELTA
COMMENT 'Synthetic scenario answer key for the four replays. Evaluation only.'
