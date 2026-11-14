import assert from 'node:assert/strict';
import test from 'node:test';
import { duration, getSnapshot, inspectEntity, runComparison, scenarioOptions, traceIncident } from '../src/twin.js';

test('canonical graph has four racks, 32 GPUs and valid relationship endpoints', () => {
  const snapshot = getSnapshot();
  const ids = new Set(snapshot.entities.map(entity => entity.id));
  assert.equal(ids.size, snapshot.entities.length);
  assert.equal(snapshot.racks.length, 4);
  assert.equal(snapshot.racks.flatMap(rack => rack.gpus).length, 32);
  assert.equal(new Set(snapshot.edges.map(edge => edge.id)).size, snapshot.edges.length);
  for (const { from, to } of snapshot.edges) assert.ok(ids.has(from) && ids.has(to));
  const assignmentEdges = snapshot.edges.filter(edge => edge.type === 'assigned-to');
  assert.equal(assignmentEdges.length, 22);
  assert.equal(inspectEntity({ entityId: 'gpu-a02-01' }).reading.status, 'waiting');
  const uplink = inspectEntity({ entityId: 'link-a02' });
  assert.equal(uplink.reading.ratedGbps, 200);
  assert.ok(uplink.related.some(edge => edge.from === 'sw-spine' && edge.to === 'leaf-a02'));
  assert.equal(inspectEntity({ entityId: 'cooling-rack-a02' }).entity.relationship, 'cooling');
});

test('stale and missing readings carry distinct quality without a numeric value', () => {
  const snapshot = getSnapshot({ scenario: 'thermal', tick: 8, policy: 'coordinated' });
  const rack = snapshot.racks.find(item => item.id === 'rack-a02');
  assert.equal(rack.inlet.value, null);
  assert.equal(rack.inlet.quality, 'stale');
  assert.ok(rack.inlet.sampledAt < snapshot.timestamp);
  assert.equal(rack.gpus[7].temperature.value, null);
  assert.equal(rack.gpus[7].temperature.sampledAt, null);
  assert.equal(rack.gpus[7].temperature.quality, 'missing');
  assert.ok(rack.gpus.every(gpu => gpu.job === null));
});

test('every coordinated plan passes each constraint for every sampled minute and seed', () => {
  for (const { id } of scenarioOptions()) {
    for (const seed of [0, 73, 9989]) {
      const result = runComparison(id, seed);
      assert.ok(result.search.feasible > 0 && result.search.feasible <= result.search.considered);
      assert.ok(result.constraints.every(check => check.ok));
      assert.equal(result.coordinated.sloMetMinutes, duration);
      assert.equal(result.baseline.timeline.length, duration);
      assert.equal(result.coordinated.timeline.length, duration);
      for (const sample of result.coordinated.timeline) {
        const snapshot = getSnapshot({ scenario: id, seed, tick: sample.minute, policy: 'coordinated' });
        assert.equal(snapshot.metrics.p95.value, sample.p95Ms);
        assert.equal(snapshot.metrics.training.value, sample.samplesPerSecond);
        assert.ok(snapshot.metrics.requestedDraw.value <= snapshot.metrics.siteBudget.value);
        assert.ok(snapshot.metrics.p95.value <= 160);
        assert.equal(snapshot.plan.train.reduce((a, b) => a + b), 12);
        for (const [i, rack] of snapshot.racks.entries()) {
          const active = snapshot.plan.train[i] + snapshot.plan.infer[i];
          assert.ok(active <= 8 && rack.draw.value <= 9);
          assert.ok(!active || (rack.temperature.quality === 'fresh' && rack.temperature.value <= 82));
        }
      }
    }
  }
});

test('scenario outputs report genuine trade-offs on a shared trace', () => {
  const network = runComparison('network');
  assert.ok(network.coordinated.trainingSamples > network.baseline.trainingSamples);
  assert.equal(network.coordinated.energyKwh, network.baseline.energyKwh);
  const demand = runComparison('demand');
  assert.equal(demand.baseline.sloMetMinutes, 0);
  assert.ok(demand.coordinated.energyKwh > demand.baseline.energyKwh);
  const power = runComparison('power');
  assert.ok(power.coordinated.energyKwh < power.baseline.energyKwh);
  assert.ok(power.coordinated.cost < power.baseline.cost);
  const thermal = runComparison('thermal');
  assert.ok(thermal.baseline.sloMetMinutes < thermal.coordinated.sloMetMinutes);
  assert.ok(traceIncident({ scenario: 'thermal' }).signals.some(signal => signal.id === 'stale-cooling'));
  for (const { id } of scenarioOptions()) {
    assert.equal(traceIncident({ scenario: id }).detected, true);
    const result = runComparison(id);
    assert.deepEqual(result.baseline.timeline.map(item => item.pricePerKwh), result.coordinated.timeline.map(item => item.pricePerKwh));
  }
});

test('parameter boundaries reject invalid replay and entity lookups', () => {
  assert.throws(() => getSnapshot({ scenario: 'unknown' }), RangeError);
  assert.throws(() => getSnapshot({ tick: duration }), RangeError);
  assert.throws(() => getSnapshot({ policy: 'invalid' }), RangeError);
  assert.throws(() => runComparison('network', -1), RangeError);
  assert.throws(() => inspectEntity({ entityId: 'unknown' }), RangeError);
});
