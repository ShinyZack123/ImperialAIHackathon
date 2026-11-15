import assert from 'node:assert/strict';
import test from 'node:test';
import { readFile } from 'node:fs/promises';
import { duration, scenarioOptions } from '../src/twin.js';
import { buildPolicyExperiments, defaultSeeds, serializePolicyExperiments } from '../src/experiment-export.js';

test('policy experiment export has one baseline and one coordinated run per scenario and seed', () => {
  const document = buildPolicyExperiments([0, 73]);
  const scenarios = scenarioOptions().map(scenario => scenario.id);
  assert.equal(document.origin, 'simulated');
  assert.equal(document.runs.length, scenarios.length * 2 * 2);
  assert.equal(document.timeline.length, document.runs.length * duration);
  const keys = new Set(document.runs.map(run => `${run.scenario}/${run.seed}/${run.policy}`));
  assert.equal(keys.size, document.runs.length);
  for (const run of document.runs) {
    assert.ok(scenarios.includes(run.scenario));
    assert.equal(run.minutes, duration);
    assert.equal(run.origin, 'simulated');
    if (run.policy === 'baseline') {
      assert.equal(run.policy_kind, 'heuristic');
      assert.equal(run.plan_json, null);
      assert.equal(run.constraints_passed, null);
    } else {
      assert.equal(run.policy_kind, 'optimizer');
      assert.equal(run.constraints_passed, true);
      assert.ok(JSON.parse(run.plan_json).train.length === 4);
      assert.ok(run.candidates_feasible > 0 && run.candidates_feasible <= run.candidates_considered);
    }
  }
  assert.ok(document.timeline.every(point => Number.isInteger(point.minute) && point.minute >= 0 && point.minute < duration));
});

// Twin values come from Math.sin, so the committed file is compared with a tolerance:
// a different JavaScript engine build must not fail the suite over the last bit of a float.
function assertClose(actual, expected, path = 'export') {
  if (typeof expected === 'number') {
    assert.equal(typeof actual, 'number', path);
    assert.ok(Math.abs(actual - expected) <= 1e-9 * Math.max(1, Math.abs(expected)), `${path}: ${actual} differs from ${expected}`);
  } else if (expected !== null && typeof expected === 'object') {
    assert.equal(Array.isArray(actual), Array.isArray(expected), path);
    assert.deepEqual(Object.keys(actual), Object.keys(expected), path);
    for (const key of Object.keys(expected)) assertClose(actual[key], expected[key], `${path}.${key}`);
  } else {
    assert.equal(actual, expected, path);
  }
}

test('serialized export is deterministic, valid JSON and matches the committed Databricks fixture', async () => {
  const document = buildPolicyExperiments();
  const text = serializePolicyExperiments(document);
  assert.equal(text, serializePolicyExperiments(buildPolicyExperiments()));
  assert.deepEqual(JSON.parse(text), document);
  assert.deepEqual(document.seeds, defaultSeeds);
  const committed = await readFile(new URL('../databricks/demo/twin_policy_experiments.json', import.meta.url), 'utf8');
  assertClose(JSON.parse(committed), document);
});
