import test from 'node:test';
import assert from 'node:assert/strict';
import { diagnoseGpu } from '../src/rack-health.js';

const healthy = {
  telemetry_is_observed: true,
  state: 'clear',
  metrics: { temp_c: 62, power_w: 205 },
  limits: { temperature_c: 88 },
  workload: { is_fresh: true, state: 'running' },
};

test('red flags apply to measured overheating, zero draw during work and direct detector findings', () => {
  assert.equal(diagnoseGpu({ ...healthy, metrics: { ...healthy.metrics, temp_c: 88 } }).label, 'Overheating');
  assert.equal(diagnoseGpu({ ...healthy, metrics: { ...healthy.metrics, power_w: 0 } }).label, 'Power lost');
  assert.equal(diagnoseGpu(healthy, { metric: 'gpu.ecc_uncorrected_total', text: '1 uncorrected error' }).label, 'GPU memory error');
});

test('shared warnings, missing values and idle zero draw avoid a false red flag', () => {
  assert.equal(diagnoseGpu({ ...healthy, state: 'warning' }).kind, 'warning');
  assert.equal(diagnoseGpu({ ...healthy, telemetry_is_observed: false }).kind, 'unknown');
  assert.equal(diagnoseGpu({ ...healthy, metrics: { temp_c: null, power_w: null } }).kind, 'clear');
  assert.equal(diagnoseGpu({ ...healthy, metrics: { ...healthy.metrics, power_w: 0 }, workload: { is_fresh: true, state: 'idle' } }).kind, 'clear');
});
