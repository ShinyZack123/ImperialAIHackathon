CREATE OR REPLACE VIEW ${evidence}.policy_leaderboard
COMMENT 'Mean simulated outcome of each policy per scenario across seeded replays. Simulated results only.'
AS
SELECT scenario, policy, policy_kind, COUNT(*) AS replays,
       ROUND(AVG(training_samples_per_second), 1) AS mean_training_samples_per_second,
       ROUND(AVG(average_p95_ms), 1) AS mean_p95_ms,
       ROUND(100.0 * SUM(slo_met_minutes) / SUM(minutes), 1) AS slo_minutes_pct,
       ROUND(AVG(energy_kwh), 2) AS mean_energy_kwh,
       ROUND(AVG(cost_gbp), 2) AS mean_cost_gbp,
       ROUND(AVG(samples_per_kwh), 0) AS mean_samples_per_kwh,
       ROUND(100.0 * AVG(CASE WHEN constraints_passed THEN 1.0 WHEN constraints_passed = FALSE THEN 0.0 END), 1) AS validated_plans_pct
FROM ${evidence}.policy_runs
GROUP BY scenario, policy, policy_kind
