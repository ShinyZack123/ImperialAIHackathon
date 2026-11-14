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

test('MCP subprocess discovers, validates and runs the four shared tools over stdio', () => {
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
    '{',
  );
  const call = spawnSync(process.execPath, [server], { input, encoding: 'utf8', timeout: 5000 });
  assert.equal(call.status, 0, call.stderr);
  assert.equal(call.stderr, '');
  const results = new Map(call.stdout.trim().split('\n').map(JSON.parse).map(item => [item.id, item]));
  assert.equal(results.size, 11);
  assert.equal(results.get(0).error.code, -32000);
  assert.equal(results.get(1).result.protocolVersion, '2025-11-25');
  assert.equal(results.get(1).result.serverInfo.name, 'ai-infrastructure-optimisation-platform');
  const catalog = results.get(2).result.tools;
  assert.deepEqual(catalog.map(item => item.name), ['get_snapshot', 'inspect_entity', 'trace_incident', 'compare_policies']);
  assert.ok(catalog.every(item => item.annotations.readOnlyHint));
  assert.deepEqual(results.get(3).result.structuredContent, getSnapshot({ scenario: 'thermal', tick: 8 }));
  assert.ok(results.get(4).result.structuredContent.signals.some(signal => signal.id === 'site-power'));
  assert.equal(results.get(5).result.structuredContent.reading.status, 'waiting');
  assert.deepEqual(results.get(6).result.structuredContent, runComparison('demand'));
  assert.equal(results.get(7).error.code, -32602);
  assert.equal(results.get(8).result.isError, true);
  assert.equal(results.get(9).error.code, -32602);
  assert.equal(results.get(null).error.code, -32700);
});
