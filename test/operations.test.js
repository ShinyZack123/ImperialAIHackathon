import assert from 'node:assert/strict';
import test from 'node:test';
import { checkpointAt, getSnapshot, searchPlacement } from '../src/twin.js';
import {
  attributeStragglers, checkpointTiming, createActionGate, decodeXid, downstream, interruptionCost, parseProposal,
  planMaintenance, triageFleet, triageGpu,
} from '../src/operations.js';

const operator = 'test-operator';
const source = { kind: 'operator', name: 'test' };
const model = { kind: 'model', name: 'fine-tuned-planner' };

test('checkpoint schedule and interruption cost follow the declared 8-minute cadence', () => {
  assert.deepEqual(checkpointAt(0), { last: -5, next: 3, intervalMinutes: 8 });
  assert.deepEqual(checkpointAt(11), { last: 11, next: 11, intervalMinutes: 8 });
  assert.deepEqual(checkpointAt(12), { last: 11, next: 19, intervalMinutes: 8 });
  const snapshot = getSnapshot({ scenario: 'gpu-xid', tick: 5 });
  const now = interruptionCost(snapshot, 5);
  assert.equal(now.minutesSinceCheckpoint, 2);
  assert.equal(now.gpuMinutes, 12 * 3);
  assert.equal(now.cost, 1.44);
  const atCheckpoint = interruptionCost(snapshot, 11);
  assert.equal(atCheckpoint.atCheckpoint, true);
  assert.equal(atCheckpoint.cost, .48);
  const timing = checkpointTiming(snapshot);
  assert.equal(timing.recommendedTick, 11);
  assert.equal(timing.savings, .96);
  assert.equal(checkpointTiming(snapshot, 'immediate').recommendedTick, 5, 'immediate faults are never deferred');
});

test('XID triage decodes codes, defers remaps to the checkpoint and escalates uncorrectable errors', () => {
  assert.equal(decodeXid(79).name, 'GPU has fallen off the bus');
  assert.equal(decodeXid(48).urgency, 'immediate');
  assert.equal(decodeXid(13).hardware, false);
  assert.equal(decodeXid(999).name, 'Unlisted XID');

  const early = triageGpu(getSnapshot({ scenario: 'gpu-xid', tick: 5 }), 'gpu-a01-03');
  assert.equal(early.urgency, 'next-checkpoint');
  assert.equal(early.rmaRecommended, false);
  assert.deepEqual(early.proposals, [{ type: 'drain_gpu', gpuId: 'gpu-a01-03', notBeforeTick: 11, rationale: early.proposals[0].rationale }]);
  assert.deepEqual(early.steps.map(step => step.id), ['cordon', 'drain', 'reset']);

  const late = triageGpu(getSnapshot({ scenario: 'gpu-xid', tick: 18 }), 'gpu-a01-03');
  assert.equal(late.urgency, 'immediate');
  assert.equal(late.rmaRecommended, true);
  assert.equal(late.proposals[0].notBeforeTick, 18);
  assert.deepEqual(late.proposals.map(item => item.type), ['drain_gpu', 'open_rma']);

  assert.equal(triageFleet(getSnapshot({ scenario: 'network', tick: 5 })).length, 0);
  assert.equal(triageGpu(getSnapshot({ scenario: 'network' }), 'gpu-b01-01').verdict, 'healthy');
});

test('stale GPU health is unknown and produces no proposal', () => {
  const snapshot = getSnapshot({ scenario: 'thermal', tick: 4 });
  const result = triageGpu(snapshot, 'gpu-a02-08');
  assert.equal(result.verdict, 'unknown');
  assert.deepEqual(result.proposals, []);
});

test('straggler attribution names the slow rank, its layer and its cost', () => {
  const single = attributeStragglers(getSnapshot({ scenario: 'straggler', tick: 2 }));
  assert.equal(single.verdict, 'straggler');
  assert.deepEqual(single.limiting, ['gpu-a02-05']);
  assert.equal(single.cause.layer, 'compute');
  assert.match(single.cause.label, /thermal/);
  assert.ok(single.throughputCost > .2 && single.throughputCost < .3);
  assert.equal(single.mitigation.type, 'drain_gpu');
  assert.equal(single.mitigation.notBeforeTick, 3);

  const fabric = attributeStragglers(getSnapshot({ scenario: 'network', tick: 2 }));
  assert.equal(fabric.limiting.length, 6);
  assert.equal(fabric.cause.entityId, 'link-a02');
  assert.equal(fabric.mitigation, null, 'a shared link is not fixed by swapping one GPU');

  assert.equal(attributeStragglers(getSnapshot({ scenario: 'power', tick: 2 })).verdict, 'uniform');
  assert.equal(attributeStragglers(getSnapshot({ scenario: 'demand', tick: 2 })).verdict, 'balanced');
  assert.equal(attributeStragglers(getSnapshot({ scenario: 'gpu-xid', tick: 18 })).verdict, 'halted');
});

test('blast radius follows the physical graph and flags single points of failure', () => {
  assert.equal(downstream('pdu-a').nodes.filter(id => id.startsWith('gpu-')).length, 16);
  assert.equal(downstream('sw-spine').nodes.filter(id => id.startsWith('gpu-')).length, 32);
  assert.equal(downstream('link-b02').start, 'leaf-b02');
  assert.deepEqual(downstream('gpu-a01-01').nodes, ['gpu-a01-01']);
  assert.throws(() => downstream('site-lon01'), RangeError);
  assert.throws(() => downstream('job-atlas'), RangeError);
  assert.throws(() => downstream('nope'), RangeError);

  const snapshot = getSnapshot({ scenario: 'network', tick: 5 });
  const pdu = planMaintenance({ entityId: 'pdu-a', snapshot });
  assert.equal(pdu.verdict, 'unsafe');
  assert.equal(pdu.affected.atlasRanks, 12);
  assert.ok(pdu.redundancy.singlePointsOfFailure.some(item => item.kind === 'power'));
  assert.ok(pdu.unprepared.newlyFailing.length > 0);
  assert.equal(pdu.proposal, null);

  const gpu = planMaintenance({ entityId: 'gpu-b01-02', snapshot });
  assert.equal(gpu.verdict, 'safe-with-evacuation');
  assert.equal(gpu.evacuation.trainingRanksMoved, 0);
  assert.ok(gpu.evacuation.gpusReassigned <= 2);
  assert.equal(gpu.proposal.type, 'maintenance_hold');

  const idle = planMaintenance({ entityId: 'gpu-a01-08', snapshot });
  assert.equal(idle.verdict, 'safe-now');
  const held = planMaintenance({ entityId: 'gpu-a01-03', snapshot, control: { unavailable: ['gpu-a01-03'] } });
  assert.equal(held.verdict, 'already-held');
  assert.equal(held.proposal, null);

  const stale = planMaintenance({ entityId: 'cdu-a', snapshot: getSnapshot({ scenario: 'thermal', tick: 3 }) });
  assert.equal(stale.verdict, 'unknown', 'stale cooling telemetry means impact cannot be certified');
  assert.equal(stale.proposal, null);
});

test('evacuation search withholds GPUs and keeps every limit', () => {
  const result = searchPlacement({ scenario: 'gpu-xid', tick: 5, control: { unavailable: ['gpu-a01-03'] } });
  assert.equal(result.trainingRanksMoved, 1);
  const check = getSnapshot({ scenario: 'gpu-xid', tick: 5, control: { plan: result.plan, unavailable: ['gpu-a01-03'] } });
  assert.ok(check.constraints.every(item => item.ok));
  assert.equal(check.racks[0].gpus[2].status, 'drained');
  assert.ok(!check.workloads[0].gpus.includes('gpu-a01-03'));
});

test('proposal parser rejects malformed, oversized and unsupported model output', () => {
  const bad = [
    null, [], 'drain', { type: 'drain_gpu' }, { type: 'drain_gpu', gpuId: 'rack-a01' },
    { type: 'placement', plan: { train: [12, 0, 0, 0], infer: [0, 0, 0, 0], cap: 1 } },
    { type: 'placement', plan: { train: [6, 6, 0, 0], infer: [0, 0, 6, 4], cap: .5 } },
    { type: 'power_cap', cap: 2 }, { type: 'power_cap', cap: .84, command: 'rm -rf /' },
    { type: 'drain_gpu', gpuId: 'gpu-a01-01', notBeforeTick: 24 },
    { type: 'drain_gpu', gpuId: 'gpu-a01-01', rationale: 'x'.repeat(601) },
    { type: 'power_cap', cap: .84, claimedEffect: { powerKw: Infinity } },
    { type: 'maintenance_hold', entityId: 'site-lon01' },
    { type: 'set_cdu_flow', value: 1 }, { type: 'power_cycle_pdu' }, { type: 'execute_shell' },
    JSON.parse('{"type":"power_cap","cap":0.84,"__proto__":{"admin":true}}'),
    { type: 'power_cap', cap: .84, rationale: 'y'.repeat(590), claimedEffect: Object.fromEntries(Array.from({ length: 400 }, (_, i) => [`k${i}`, i])) },
  ];
  for (const input of bad) assert.equal(parseProposal(input).ok, false, JSON.stringify(input)?.slice(0, 80));
  assert.match(parseProposal({ type: 'set_cdu_flow' }).message, /Not a supported control/);
  assert.equal(parseProposal({ type: 'power_cap', cap: .84 }).ok, true);
});

test('gate runs a drain through approval, checkpoint execution and measured verification', () => {
  const gate = createActionGate({ scenario: 'gpu-xid' });
  const triage = triageGpu(gate.snapshotAt(5), 'gpu-a01-03');
  const proposal = gate.submit(triage.proposals[0], { tick: 5, source: { kind: 'triage', name: 'xid-triage' } });
  assert.equal(proposal.status, 'awaiting-approval');
  assert.equal(proposal.effectiveTick, 11);
  assert.equal(gate.hasControl(), false, 'nothing changes before approval');

  const approved = gate.approve(proposal.id, { tick: 6, operator });
  assert.equal(approved.status, 'scheduled');
  assert.deepEqual(gate.controlAt(6).cordoned, ['gpu-a01-03']);
  assert.deepEqual(gate.controlAt(10).unavailable, []);
  assert.deepEqual(gate.controlAt(11).unavailable, ['gpu-a01-03']);
  assert.equal(gate.controlAt(11).restarting, true);
  assert.equal(gate.snapshotAt(11).metrics.training.value, 0, 'relaunch minute');

  gate.advance(13);
  const final = gate.state();
  assert.equal(final.proposals[0].status, 'verified');
  assert.deepEqual(final.ledger.map(entry => entry.event), ['proposed', 'validated', 'approved', 'scheduled', 'executed', 'verified']);
  assert.ok(gate.snapshotAt(18).metrics.training.value > 0, 'Atlas survives the minute-17 uncorrectable error');
  assert.equal(getSnapshot({ scenario: 'gpu-xid', tick: 18 }).metrics.training.value, 0, 'the unoperated twin does not');

  assert.throws(() => gate.approve(proposal.id, { tick: 13, operator }), RangeError);
  assert.throws(() => gate.submit({ type: 'power_cap', cap: .84 }, { tick: 12, source }), /past/);
});

test('gate enforces dependencies, blast radius and capability limits', () => {
  const gate = createActionGate({ scenario: 'gpu-xid' });
  const early = gate.submit({ type: 'open_rma', gpuId: 'gpu-a01-03' }, { tick: 2, source });
  assert.equal(early.status, 'rejected');
  assert.ok(early.checks.some(check => check.id === 'dependency' && !check.ok));
  const wide = gate.submit({ type: 'placement', plan: { train: [0, 0, 6, 6], infer: [6, 4, 0, 0], cap: 1 } }, { tick: 2, source: model });
  assert.ok(wide.checks.some(check => check.id === 'blast-radius' && !check.ok));
  assert.equal(wide.status, 'rejected');
  const fewer = gate.submit({ type: 'placement', plan: { train: [6, 5, 0, 0], infer: [0, 0, 6, 4], cap: 1 } }, { tick: 2, source: model });
  assert.equal(fewer.status, 'rejected');
  assert.ok(fewer.checks.some(check => check.id === 'atlas' && !check.ok));
  const unsupported = gate.submit({ type: 'set_fan_speed', percent: 100 }, { tick: 2, source: model });
  assert.equal(unsupported.status, 'rejected');
  assert.throws(() => gate.submit({ type: 'power_cap', cap: .84 }, { tick: 2, source: { kind: 'root', name: 'x' } }), RangeError);
  assert.throws(() => gate.approve(wide.id, { tick: 2, operator }), /rejected/);
  assert.throws(() => gate.approve('act-999', { tick: 2, operator }), RangeError);
  assert.equal(gate.hasControl(), false);
});

test('stale telemetry blocks actions on the affected rack', () => {
  const gate = createActionGate({ scenario: 'thermal' });
  const blocked = gate.submit({ type: 'drain_gpu', gpuId: 'gpu-a02-01' }, { tick: 3, source });
  assert.equal(blocked.status, 'rejected');
  assert.ok(blocked.checks.some(check => check.id === 'freshness' && !check.ok));
});

test('kill switch cancels scheduled actions and blocks approvals', () => {
  const gate = createActionGate({ scenario: 'gpu-xid' });
  const first = gate.submit({ type: 'drain_gpu', gpuId: 'gpu-a01-03', notBeforeTick: 11 }, { tick: 4, source });
  const second = gate.submit({ type: 'power_cap', cap: .92 }, { tick: 4, source });
  gate.approve(first.id, { tick: 4, operator });
  gate.setKillSwitch(true, { tick: 5, operator });
  assert.throws(() => gate.approve(second.id, { tick: 5, operator }), /Kill switch/);
  const during = gate.submit({ type: 'power_cap', cap: .84 }, { tick: 5, source });
  assert.equal(during.status, 'rejected', 'submissions are still recorded for audit, but cannot pass');
  assert.ok(during.checks.some(check => check.id === 'kill-switch' && !check.ok));
  const state = gate.state();
  assert.equal(state.proposals[0].status, 'cancelled');
  assert.deepEqual(gate.controlAt(11).unavailable, [], 'cancelled drain never executes');
  assert.ok(state.ledger.some(entry => entry.event === 'kill-switch-engaged'));
});

test('failed verification rolls back automatically; confounded outcomes are not blamed', () => {
  const thermal = createActionGate({ scenario: 'thermal' });
  const cap = thermal.submit({ type: 'power_cap', cap: .84 }, { tick: 18, source });
  thermal.approve(cap.id, { tick: 18, operator });
  thermal.advance(20);
  const rolled = thermal.state();
  assert.equal(rolled.proposals[0].status, 'rolled-back');
  assert.ok(rolled.ledger.some(entry => entry.event === 'verification-failed'));
  assert.equal(thermal.controlAt(21).cap, null);

  const xid = createActionGate({ scenario: 'gpu-xid' });
  const innocent = xid.submit({ type: 'power_cap', cap: .92 }, { tick: 15, source });
  xid.approve(innocent.id, { tick: 15, operator });
  xid.advance(19);
  const confounded = xid.state().proposals[0];
  assert.equal(confounded.status, 'inconclusive');
  assert.deepEqual(confounded.confoundedBy, ['gpu-a01-03']);
  assert.equal(xid.state().ledger.filter(entry => entry.event === 'inconclusive').length, 1);
});

test('rollback is last-in first-out and refuses to restore an unsafe state', () => {
  const gate = createActionGate({ scenario: 'gpu-xid' });
  const drain = gate.submit({ type: 'drain_gpu', gpuId: 'gpu-a01-03' }, { tick: 18, source });
  gate.approve(drain.id, { tick: 18, operator });
  const cap = gate.submit({ type: 'power_cap', cap: .92 }, { tick: 18, source });
  gate.approve(cap.id, { tick: 18, operator });
  assert.throws(() => gate.rollback(drain.id, { tick: 18, operator }), /first/);
  assert.equal(gate.rollback(cap.id, { tick: 18, operator }).status, 'rolled-back');
  assert.throws(() => gate.rollback(drain.id, { tick: 19, operator }), /stop Atlas/);
});

test('health-model verdicts are evidence for triage, validated and never commands', () => {
  const snapshot = getSnapshot({ scenario: 'network', tick: 5 });
  const verdicts = new Map([
    ['gpu-b01-02', { label: 'critical', confidence: .93, model: 'gpu-health-ft' }],
    ['gpu-b01-03', { label: 'stable' }],
    ['gpu-b01-04', { label: 'rm -rf', confidence: 7 }],
  ]);
  const results = triageFleet(snapshot, { verdicts });
  assert.deepEqual(results.map(item => item.gpuId), ['gpu-b01-02']);
  assert.equal(results[0].urgency, 'next-checkpoint');
  assert.equal(results[0].model.label, 'critical');
  assert.equal(results[0].proposals[0].type, 'drain_gpu', 'a drain is proposed, still subject to the gate');
  const stale = triageGpu(getSnapshot({ scenario: 'thermal', tick: 4 }), 'gpu-a02-08', { verdict: { label: 'critical' } });
  assert.equal(stale.verdict, 'unknown', 'a model verdict cannot override stale telemetry');
});
