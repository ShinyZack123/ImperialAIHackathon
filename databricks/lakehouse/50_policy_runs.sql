-- The decision ledger: one row per policy (heuristic, optimiser, or a model) per simulated replay.
CREATE TABLE IF NOT EXISTS ${evidence}.policy_runs (
  scenario STRING NOT NULL COMMENT 'Twin incident scenario: network, demand, power or thermal',
  seed INT NOT NULL COMMENT 'Twin seed; the same seed gives every policy the same demand, hardware and fault trace',
  policy STRING NOT NULL COMMENT 'Policy or model identifier, e.g. baseline, coordinated, or a fine-tuned model name',
  policy_kind STRING NOT NULL COMMENT 'heuristic, optimizer, llm_base, llm_finetuned or human',
  origin STRING NOT NULL COMMENT 'Always simulated for twin results',
  twin_schema_version STRING NOT NULL,
  minutes INT NOT NULL COMMENT 'Length of the replay in simulated minutes',
  plan_json STRING COMMENT 'Placement the policy chose: training ranks and serving slots per rack, and the training power cap',
  constraints_passed BOOLEAN COMMENT 'Whether the deterministic validator accepted the plan; NULL when not evaluated',
  constraint_checks_json STRING COMMENT 'Individual validator checks',
  candidates_considered INT, candidates_feasible INT,
  training_ranks_moved INT, serving_slots_moved INT,
  training_samples_per_second DOUBLE, training_samples BIGINT, inference_tokens_per_second DOUBLE,
  average_p95_ms DOUBLE, slo_met_minutes INT, energy_kwh DOUBLE, cost_gbp DOUBLE, samples_per_kwh DOUBLE,
  loaded_at TIMESTAMP NOT NULL
) USING DELTA
COMMENT 'Simulated outcomes of each scheduling policy on identical seeded twin replays. Append a model''s decisions here to compare it with the heuristic baseline and the exhaustive-search optimiser.'
