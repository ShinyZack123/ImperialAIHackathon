import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { getSnapshot, runComparison } from '../src/twin.js';

const server = fileURLToPath(new URL('../src/mcp-stdio.js', import.meta.url));
const rpc = (id, method, params) => ({ jsonrpc: '2.0', id, method, params });
const init = rpc(1, 'initialize', {
  protocolVersion: '2025-11-25',
  capabilities: {},
  clientInfo: { name: 'mcp-test', version: '1.0.0' },
});
const message = (...requests) => requests.map(request =>
  typeof request === 'string' ? request : JSON.stringify(request)).join('\n') + '\n';

test('MCP subprocess discovers, validates and runs the shared tools over stdio', () => {
  const input = message(
    rpc(0, 'tools/list'),
    init,
    { jsonrpc: '2.0', method: 'notifications/initialized' },
    rpc(2, 'tools/list'),
    rpc(3, 'tools/call', { name: 'get_snapshot', arguments: { scenario: 'thermal', tick: 8 } }),
    rpc(4, 'tools/call', { name: 'trace_incident', arguments: { scenario: 'power' } }),
    rpc(5, 'tools/call', { name: 'inspect_entity', arguments: { entityId: 'gpu-a02-01' } }),
    rpc(6, 'tools/call', { name: 'compare_policies', arguments: { scenario: 'demand' } }),
    rpc(7, 'tools/call', { name: 'get_snapshot', arguments: { tick: -1 } }),
    rpc(8, 'tools/call', { name: 'inspect_entity', arguments: { entityId: 'gpu-unknown' } }),
    rpc(9, 'tools/call', { name: 'trace_incident', arguments: { apply: true } }),
    rpc(10, 'tools/call', { name: 'submit_proposal', arguments: { scenario: 'gpu-xid', tick: 5, agent: 'fine-tuned-planner',
      proposal: { type: 'drain_gpu', gpuId: 'gpu-a01-03', notBeforeTick: 11, claimedEffect: { trainingSamplesPerSecond: 760 } } } }),
    rpc(11, 'tools/call', { name: 'submit_proposal', arguments: { scenario: 'gpu-xid', tick: 5, agent: 'fine-tuned-planner',
      proposal: { type: 'set_cdu_flow', value: 9 } } }),
    rpc(12, 'tools/call', { name: 'get_action_ledger', arguments: { scenario: 'gpu-xid' } }),
    rpc(13, 'tools/call', { name: 'triage_gpu_health', arguments: { scenario: 'gpu-xid', tick: 5 } }),
    rpc(14, 'tools/call', { name: 'submit_proposal', arguments: { agent: 'x', proposal: 'drain everything' } }),
    '{',
  );
  const call = spawnSync(process.execPath, [server], { input, encoding: 'utf8', timeout: 5000 });
  assert.equal(call.status, 0, call.stderr);
  assert.equal(call.stderr, '');
  const results = new Map(call.stdout.trim().split('\n').map(JSON.parse).map(item => [item.id, item]));
  assert.equal(results.size, 16);
  assert.equal(results.get(0).error.code, -32000);
  assert.equal(results.get(1).result.protocolVersion, '2025-11-25');
  assert.equal(results.get(1).result.serverInfo.name, 'Ithil');
  const catalog = results.get(2).result.tools;
  assert.deepEqual(catalog.map(item => item.name), ['get_snapshot', 'inspect_entity', 'trace_incident', 'compare_policies',
    'triage_gpu_health', 'attribute_straggler', 'plan_maintenance', 'submit_proposal', 'get_action_ledger']);
  // Only proposal submission writes, and it can only queue work for an operator. There is no approval tool.
  assert.deepEqual(catalog.filter(item => !item.annotations.readOnlyHint).map(item => item.name), ['submit_proposal']);
  assert.ok(catalog.every(item => !item.annotations.destructiveHint && !/approve|execute/.test(item.name)));
  assert.deepEqual(results.get(3).result.structuredContent, getSnapshot({ scenario: 'thermal', tick: 8 }));
  assert.ok(results.get(4).result.structuredContent.signals.some(signal => signal.id === 'site-power'));
  assert.equal(results.get(5).result.structuredContent.reading.status, 'waiting');
  assert.deepEqual(results.get(6).result.structuredContent, runComparison('demand'));
  assert.equal(results.get(7).error.code, -32602);
  assert.equal(results.get(8).result.isError, true);
  assert.equal(results.get(9).error.code, -32602);
  assert.equal(results.get(null).error.code, -32700);
  const submitted = results.get(10).result.structuredContent;
  assert.equal(submitted.status, 'awaiting-approval');
  assert.deepEqual(submitted.source, { kind: 'model', name: 'fine-tuned-planner' });
  assert.equal(submitted.prediction.claimVsSimulation.trainingSamplesPerSecond.claimed, 760);
  assert.equal(results.get(11).result.structuredContent.status, 'rejected');
  const ledger = results.get(12).result.structuredContent;
  assert.deepEqual(ledger.proposals.map(item => item.status), ['awaiting-approval', 'rejected']);
  assert.ok(ledger.ledger.every(entry => entry.event !== 'approved' && entry.event !== 'executed'));
  assert.equal(results.get(13).result.structuredContent[0].gpuId, 'gpu-a01-03');
  assert.equal(results.get(14).error.code, -32602);
});
