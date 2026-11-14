import assert from 'node:assert/strict';
import test, { after } from 'node:test';
import { DatabricksProxyError } from '../scripts/databricks-api.mjs';
import { databricksSourceRequest } from '../scripts/databricks-source.mjs';

// This file exercises the default as a teammate's unconfigured clone sees it.
const previousBackend = process.env.DATABRICKS_API_URL;
delete process.env.DATABRICKS_API_URL;
after(() => {
  if (previousBackend === undefined) delete process.env.DATABRICKS_API_URL;
  else process.env.DATABRICKS_API_URL = previousBackend;
});

const runId = '43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c';
const cursor = '2026-11-14T09:10:45Z';
const replayParams = { run_id: runId, as_of_ts: cursor };
const params = value => new URLSearchParams(value);
const saved = data => ({ schema_version: '1.0', source: 'databricks', synthetic: true, delivery: 'saved_replay', exported_at: '2026-11-14T16:00:00Z', data });
const connected = data => ({ schema_version: '1.0', source: 'databricks', synthetic: true, data });
const jsonResponse = body => new Response(JSON.stringify(body));
const errorStatus = status => error => error instanceof DatabricksProxyError && error.status === status;

test('unconfigured clones use the local replay without making any HTTP request', async () => {
  const body = saved([{ run_id: runId }]);
  let childCalls = 0;
  const actual = await databricksSourceRequest('/api/databricks/runs', params({}), {
    fetchImpl: async () => assert.fail('Default replay must not call an HTTP backend'),
    runReplay: async args => { childCalls += 1; assert.deepEqual(args, ['runs']); return body; },
  });
  assert.equal(childCalls, 1);
  assert.deepEqual(actual, body);
  assert.equal(actual.delivery, 'saved_replay');
});

test('an explicitly configured backend selects connected delivery without starting Python', async () => {
  let networkCalls = 0;
  const result = await databricksSourceRequest('/api/databricks/runs', params({}), {
    backendUrl: 'http://127.0.0.1:8011',
    runReplay: async () => assert.fail('Connected mode must not launch the local replay'),
    fetchImpl: async url => {
      networkCalls += 1;
      assert.equal(url.origin, 'http://127.0.0.1:8011');
      return jsonResponse(connected([{ run_id: runId }]));
    },
  });
  assert.equal(networkCalls, 1);
  assert.equal(result.delivery, 'connected');
  assert.equal(Object.hasOwn(result, 'exported_at'), false);
});

test('connected failures and empty configuration never silently fall back to the export', async () => {
  for (const fetchImpl of [
    async () => { throw new Error('ECONNREFUSED secret-token'); },
    async () => new Response('private upstream failure', { status: 503 }),
    async () => jsonResponse(saved([])),
  ]) {
    await assert.rejects(databricksSourceRequest('/api/databricks/runs', params({}), {
      backendUrl: 'http://127.0.0.1:8011', fetchImpl,
      runReplay: async () => assert.fail('Connected failure must remain a failure'),
    }), error => {
      assert.ok(error instanceof DatabricksProxyError);
      assert.ok([502, 503].includes(error.status));
      assert.doesNotMatch(error.message, /secret-token|private upstream/);
      return true;
    });
  }
  await assert.rejects(databricksSourceRequest('/api/databricks/runs', params({}), {
    backendUrl: '',
    fetchImpl: async () => assert.fail('Invalid configuration must not fetch'),
    runReplay: async () => assert.fail('Empty explicit configuration must not choose replay'),
  }), errorStatus(503));
});

test('invalid route, query keys, identifiers and windows are rejected before starting Python', async () => {
  const options = {
    runReplay: async () => assert.fail('Invalid arguments must not reach the Python bridge'),
    fetchImpl: async () => assert.fail('Invalid arguments must not reach HTTP'),
  };
  const badRequests = [
    ['/api/databricks/gpus/../../runs', params({}), 404],
    ['/api/databricks/runs', params({ host: 'remote.example' }), 400],
    ['/api/databricks/snapshot', new URLSearchParams([['run_id', runId], ['run_id', runId], ['as_of_ts', cursor]]), 400],
    ['/api/databricks/snapshot', params({ run_id: '--bad', as_of_ts: cursor }), 400],
    ['/api/databricks/snapshot', params({ run_id: runId }), 400],
    ['/api/databricks/snapshot', params({ ...replayParams, as_of_ts: '2026-11-14 09:10:45' }), 400],
    ['/api/databricks/snapshot', params({ ...replayParams, as_of_ts: '2026-99-14T09:10:45Z' }), 400],
    ...['0', '16', '-1', '1.5', '--help'].map(window_minutes => ['/api/databricks/gpus/gpu-001/history', params({ ...replayParams, window_minutes }), 400]),
  ];
  for (const [path, query, status] of badRequests) {
    await assert.rejects(databricksSourceRequest(path, query, options), errorStatus(status));
  }
});

test('saved replay calls the fixed bridge with separate validated arguments', async () => {
  const calls = [];
  const options = {
    fetchImpl: async () => assert.fail('Saved replay must stay local'),
    runReplay: async args => {
      calls.push(args);
      return saved(args[0] === 'snapshot' ? {
        delivery: 'saved_replay', exported_at: '2026-11-14T16:00:00Z',
        persistence: { status: 'not_saved', reason: 'saved_replay' },
      } : []);
    },
  };
  await databricksSourceRequest('/api/databricks/snapshot', params(replayParams), options);
  await databricksSourceRequest('/api/databricks/gpus/gpu-010/history', params(replayParams), options);
  await databricksSourceRequest('/api/databricks/gpus/gpu-010/workload-history', params({ ...replayParams, window_minutes: '015' }), options);
  assert.deepEqual(calls, [
    ['snapshot', runId, cursor],
    ['history', runId, cursor, 'gpu-010', '5'],
    ['workload-history', runId, cursor, 'gpu-010', '15'],
  ]);
});

test('saved delivery preserves export provenance, unknown readings and an explicit no-write status', async () => {
  const snapshot = { ...saved(undefined), run_id: runId, as_of_ts: cursor, persistence: { status: 'not_saved', reason: 'saved_replay' }, racks: [{ gpus: [{ metrics: { temp_c: null, power_w: 0 }, state: 'unknown' }] }] };
  delete snapshot.data;
  const body = saved(snapshot);
  const result = await databricksSourceRequest('/api/databricks/snapshot', params(replayParams), {
    runReplay: async () => body,
    fetchImpl: async () => assert.fail('Saved replay must not contact Databricks'),
  });
  assert.deepEqual(result, body);
  assert.deepEqual(result.data.persistence, { status: 'not_saved', reason: 'saved_replay' });
  assert.equal(result.data.racks[0].gpus[0].metrics.temp_c, null);
  assert.equal(result.data.racks[0].gpus[0].metrics.power_w, 0);
  assert.equal(result.exported_at, '2026-11-14T16:00:00Z');
});

test('malformed or misleading local provenance produces a controlled gateway error', async () => {
  const missingData = saved([]);
  delete missingData.data;
  for (const body of [null, [], { ...saved([]), source: 'generated_fixture' }, { ...saved([]), synthetic: false }, { ...saved([]), delivery: 'connected' }, { ...saved([]), exported_at: 'unknown' }, missingData]) {
    await assert.rejects(databricksSourceRequest('/api/databricks/runs', params({}), {
      runReplay: async () => body,
      fetchImpl: async () => assert.fail('Invalid replay must not fall back to HTTP'),
    }), errorStatus(502));
  }
});

test('saved replay snapshots cannot claim a fresh Databricks write', async () => {
  for (const persistence of [undefined, { status: 'saved' }, { status: 'not_saved', reason: 'unknown' }]) {
    await assert.rejects(databricksSourceRequest('/api/databricks/snapshot', params(replayParams), {
      runReplay: async () => saved({ source: 'databricks', synthetic: true, delivery: 'saved_replay', exported_at: '2026-11-14T16:00:00Z', persistence }),
      fetchImpl: async () => assert.fail('Misleading replay must not fall back to HTTP'),
    }), errorStatus(502));
  }
});
