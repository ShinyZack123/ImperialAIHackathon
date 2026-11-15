-- Evaluation labels live in their own schema so access can be granted separately from evidence.
CREATE SCHEMA IF NOT EXISTS ${eval}
COMMENT 'Evaluation only: scenario labels and scorecards. Never grant this schema to an agent or inference-time principal, and never expose it through an agent tool.'
