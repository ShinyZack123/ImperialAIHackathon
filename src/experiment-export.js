// Flattens the twin's policy comparison into rows for the Databricks policy_runs and
// policy_timeline tables. Pure and deterministic: no clock, no filesystem, no randomness.
import { runComparison, schemaVersion } from './twin.js';

// The twin's trace varies with seed % 11, so seeds 0-10 cover every distinct replay.
const defaultSeeds = Array.from({ length: 11 }, (_, seed) => seed);
// The Databricks policy ledger covers the four optimiser scenarios. The operations scenarios
// (gpu-xid, straggler) exercise the action gate and are deliberately left out so the committed
// fixture and the loaded Delta tables stay in step.
const policyScenarios = ['network', 'demand', 'power', 'thermal'];
const notEvaluated = {
  plan_json: null, constraints_passed: null, constraint_checks_json: null,
  candidates_considered: null, candidates_feasible: null, training_ranks_moved: null, serving_slots_moved: null,
};

function buildPolicyExperiments(seeds = defaultSeeds) {
  const runs = [];
  const timeline = [];
  for (const scenario of policyScenarios) {
    for (const seed of seeds) {
      const result = runComparison(scenario, seed);
      const add = (policy, policyKind, summary, details) => {
        runs.push({
          scenario, seed, policy, policy_kind: policyKind, origin: 'simulated',
          twin_schema_version: schemaVersion, minutes: result.minutes, ...details,
          training_samples_per_second: summary.trainingSamplesPerSecond,
          training_samples: summary.trainingSamples,
          inference_tokens_per_second: summary.inferenceTokensPerSecond,
          average_p95_ms: summary.averageP95Ms,
          slo_met_minutes: summary.sloMetMinutes,
          energy_kwh: summary.energyKwh, cost_gbp: summary.cost, samples_per_kwh: summary.samplesPerKwh,
        });
        for (const step of summary.timeline) {
          timeline.push({
            scenario, seed, policy, minute: step.minute,
            samples_per_second: step.samplesPerSecond, p95_ms: step.p95Ms, tokens_per_second: step.tokensPerSecond,
            power_kw: step.powerKw, price_per_kwh: step.pricePerKwh, energy_kwh: step.energyKwh,
          });
        }
      };
      // The baseline re-plans every minute, so it has no single placement or validator verdict to record.
      add('baseline', 'heuristic', result.baseline, notEvaluated);
      if (result.coordinated) {
        add('coordinated', 'optimizer', result.coordinated, {
          plan_json: JSON.stringify(result.plan),
          constraints_passed: result.constraints.every(check => check.ok),
          constraint_checks_json: JSON.stringify(result.constraints),
          candidates_considered: result.search.considered,
          candidates_feasible: result.search.feasible,
          training_ranks_moved: result.trainingRanksMoved,
          serving_slots_moved: result.servingSlotsMoved,
        });
      }
    }
  }
  return { schemaVersion: '1.0', origin: 'simulated', generator: 'src/experiment-export.js', twinSchemaVersion: schemaVersion, seeds: [...seeds], runs, timeline };
}

// One row per line keeps the committed file reviewable and its diffs small.
function serializePolicyExperiments(document) {
  const { runs, timeline, ...header } = document;
  const rows = items => items.map(item => `  ${JSON.stringify(item)}`).join(',\n');
  const head = Object.entries(header).map(([key, value]) => `${JSON.stringify(key)}: ${JSON.stringify(value)}`).join(',\n');
  return `{\n${head},\n"runs": [\n${rows(runs)}\n],\n"timeline": [\n${rows(timeline)}\n]\n}\n`;
}

export { buildPolicyExperiments, serializePolicyExperiments, defaultSeeds, policyScenarios };
