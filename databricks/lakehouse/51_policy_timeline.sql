CREATE TABLE IF NOT EXISTS ${evidence}.policy_timeline (
  scenario STRING NOT NULL, seed INT NOT NULL, policy STRING NOT NULL,
  minute INT NOT NULL COMMENT 'Simulated minute within the replay',
  samples_per_second DOUBLE COMMENT 'Training throughput',
  p95_ms DOUBLE COMMENT 'Inference P95 latency; NULL when no serving capacity',
  tokens_per_second DOUBLE COMMENT 'Inference tokens served',
  power_kw DOUBLE COMMENT 'Simulated facility draw',
  price_per_kwh DOUBLE, energy_kwh DOUBLE
) USING DELTA
COMMENT 'Minute-by-minute simulated outcomes behind policy_runs.'
