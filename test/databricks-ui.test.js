import assert from 'node:assert/strict';
import test from 'node:test';
import { parseReplayRuns, parseDatabricksSnapshot, historySegments, dbNumber, requestDatabricks, databricksPresentation, assertDatabricksDelivery, databricksPersistenceText } from '../public/databricks.js';

const runId = '43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c';
const cursor = '2026-11-14T09:10:45Z';
const sample = () => ({
  source: 'databricks', synthetic: true, run_id: runId, as_of_ts: cursor,
  overview: { total_gpu_count: 2, observed_gpu_count: 1, unknown_gpu_count: 1 },
  racks: [{ id: 'rack-a', gpus: [
    { id: 'gpu-001', state: 'clear', metrics: { temp_c: 50 } },
    { id: 'gpu-002', state: 'unknown', metrics: { temp_c: null } },
  ] }], incidents: [],
});

test('Databricks snapshot retains native inventory IDs and unknown readings', () => {
  const result = parseDatabricksSnapshot({ data: sample() }, runId, cursor);
  assert.equal(result.racks.length, 1);
  assert.equal(result.racks[0].gpus[1].id, 'gpu-002');
  assert.equal(result.racks[0].gpus[1].metrics.temp_c, null);
  assert.equal(dbNumber(null), 'Unknown');
  assert.equal(dbNumber(undefined), 'Unknown');
  assert.equal(dbNumber(0), '0');
});

test('Databricks snapshot rejects stale request results, foreign sources and malformed inventories', () => {
  for (const changes of [{ run_id: 'different' }, { as_of_ts: '2026-11-14T09:10:50Z' }, { source: 'fixture' }, { synthetic: false }, { overview: { total_gpu_count: 32 } }]) {
    assert.throws(() => parseDatabricksSnapshot({ data: { ...sample(), ...changes } }, runId, cursor));
  }
  const duplicate = sample();
  duplicate.racks[0].gpus[1].id = 'gpu-001';
  assert.throws(() => parseDatabricksSnapshot(duplicate, runId, cursor));
});

test('saved exports have explicit local delivery and cannot claim a new Databricks write', () => {
  const provenance = { delivery: 'saved_replay', exported_at: '2026-11-14T12:00:00Z' };
  const snapshot = { ...sample(), ...provenance, persistence: { status: 'not_saved', reason: 'saved_replay' } };
  const body = { ...provenance, data: snapshot };
  const mode = databricksPresentation(body);
  assert.equal(mode.label, 'Saved Databricks replay');
  assert.match(mode.description, /exported 2026-11-14T12:00:00.000Z/);
  assert.match(mode.loading, /detector locally/);
  assert.match(mode.footer, /no Databricks writes/);
  assert.equal(parseDatabricksSnapshot(body, runId, cursor, mode).racks[0].gpus[1].metrics.temp_c, null);
  assert.equal(databricksPersistenceText(snapshot), 'Assessment computed locally · not written to Databricks');
  for (const persistence of [undefined, { status: 'saved' }, { status: 'not_saved', reason: 'unknown' }]) {
    assert.throws(() => parseDatabricksSnapshot({ ...body, data: { ...snapshot, persistence } }, runId, cursor, mode), /cannot claim/);
  }
});

test('connected responses remain supported and only confirmed writes show the saved notice', () => {
  const mode = databricksPresentation({});
  assert.equal(mode.label, 'Connected Databricks');
  assert.equal(mode.exportedAt, null);
  assert.match(mode.loading, /saving the assessment/);
  assert.equal(databricksPersistenceText(sample()), 'Databricks save not confirmed');
  assert.equal(databricksPersistenceText({ ...sample(), persistence: { status: 'saved' } }), 'Assessment saved in Databricks');
});

test('delivery must stay consistent across catalogue, snapshot and history requests', () => {
  const saved = databricksPresentation({ delivery: 'saved_replay', exported_at: '2026-11-14T12:00:00Z' });
  assert.throws(() => parseDatabricksSnapshot({ data: sample() }, runId, cursor, saved), /changed delivery/);
  assert.throws(() => assertDatabricksDelivery({ delivery: 'saved_replay', exported_at: '2026-11-14T12:01:00Z' }, saved), /changed delivery/);
  assert.throws(() => databricksPresentation({ delivery: 'saved_replay' }), /export timestamp/);
  assert.throws(() => databricksPresentation({ delivery: 'saved_replay', exported_at: 'invalid' }), /export timestamp/);
  assert.throws(() => databricksPresentation({ delivery: 'unexpected' }), /unsupported delivery/);
});

test('replay catalogue rejects malformed intervals and identifiers', () => {
  const replay = { run_id: runId, start_ts: '2026-11-14T09:00:00Z', end_ts: '2026-11-14T09:14:55Z' };
  assert.equal(parseReplayRuns({ source: 'databricks', synthetic: true, data: [replay] }).length, 1);
  for (const values of [{ run_id: '-'.repeat(36) }, { start_ts: 'not-time' }, { end_ts: '2026-11-14T08:00:00Z' }]) {
    assert.throws(() => parseReplayRuns({ source: 'databricks', synthetic: true, data: [{ ...replay, ...values }] }));
  }
});

test('history chart breaks at nulls, heartbeat gaps and absent intervals; future records are excluded', () => {
  const row = (time, temp, observed = true) => ({ event_ts: `2026-11-14T${time}Z`, temp_c: temp, heartbeat_ok: observed });
  const segments = historySegments([
    row('09:05:00', 1), // outside the requested five-minute chart
    row('09:10:00', 60), row('09:10:05', 61), row('09:10:10', null, false),
    row('09:10:15', 63), row('09:10:40', 65), row('09:10:45', 66),
    row('09:10:50', 1000), // future sample
  ], 'temp_c', cursor);
  assert.deepEqual(segments.map(segment => segment.map(point => point[1])), [[60, 61], [63], [65, 66]]);
  assert.deepEqual(historySegments([row('09:10:40', null, false)], 'temp_c', cursor), []);
});

test('source failures propagate instead of supplying local fixture data', async t => {
  t.mock.method(globalThis, 'fetch', async () => ({ ok: false, status: 502, json: async () => ({ detail: 'Warehouse unavailable' }) }));
  await assert.rejects(requestDatabricks('runs'), /Warehouse unavailable/);
});

test('requests use the same-origin proxy and encode fixed run/time parameters', async t => {
  let called;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    called = { url, options };
    return { ok: true, status: 200, json: async () => ({ source: 'databricks', data: [] }) };
  });
  await requestDatabricks('snapshot', { run_id: runId, as_of_ts: cursor });
  assert.ok(called.url.startsWith('./api/databricks/snapshot?'));
  assert.equal(new URL(called.url, 'http://localhost').searchParams.get('as_of_ts'), cursor);
  assert.equal(called.options.headers.Accept, 'application/json');
  assert.equal(called.options.headers.Authorization, undefined);
});
