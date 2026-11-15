-- Paired comparison: every policy against the baseline on the same scenario and seed.
CREATE OR REPLACE VIEW ${evidence}.policy_uplift
COMMENT 'Each policy compared with the heuristic baseline on identical seeded replays (paired by scenario and seed). Simulated results only.'
AS
SELECT p.scenario, p.policy, p.policy_kind, COUNT(*) AS paired_replays,
       ROUND(100.0 * AVG(p.training_samples_per_second / NULLIF(b.training_samples_per_second, 0) - 1), 1) AS mean_training_uplift_pct,
       ROUND(100.0 * MIN(p.training_samples_per_second / NULLIF(b.training_samples_per_second, 0) - 1), 1) AS min_training_uplift_pct,
       ROUND(AVG(p.slo_met_minutes - b.slo_met_minutes), 1) AS mean_slo_minutes_gained,
       ROUND(AVG(p.average_p95_ms - b.average_p95_ms), 1) AS mean_p95_change_ms,
       ROUND(100.0 * AVG(p.energy_kwh / NULLIF(b.energy_kwh, 0) - 1), 1) AS mean_energy_change_pct,
       ROUND(100.0 * AVG(p.samples_per_kwh / NULLIF(b.samples_per_kwh, 0) - 1), 1) AS mean_samples_per_kwh_change_pct
FROM ${evidence}.policy_runs AS p
JOIN ${evidence}.policy_runs AS b
  ON b.scenario = p.scenario AND b.seed = p.seed AND b.policy = 'baseline'
WHERE p.policy <> 'baseline'
GROUP BY p.scenario, p.policy, p.policy_kind
