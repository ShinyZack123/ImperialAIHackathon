import assert from 'node:assert/strict';
import test from 'node:test';
import { DatabricksProxyError, databricksRequest } from '../scripts/databricks-api.mjs';

const replay = { run_id: '43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c', as_of_ts: '2026-11-14T09:07:30Z' };
const params = values => new URLSearchParams(values);
const envelope = data => ({ schema_version: '1.0', source: 'databricks', synthetic: true, data });
const jsonResponse = body => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
const isProxyError = status => problem => problem instanceof DatabricksProxyError && problem.status === status;

test('Databricks proxy preserves nulls and forwards one fixed read-only route', async () => {
  const body = envelope({ temperature_c: null, observed: false, power_kw: 0 });
  let calls = 0;
  const actual = await databricksRequest('/api/databricks/snapshot', params(replay), {
    backendUrl: 'http://127.0.0.1:8010',
    fetchImpl: async (url, options) => {
      calls += 1;
      assert.equal(url.origin, 'http://127.0.0.1:8010');
      assert.equal(url.pathname, '/api/databricks/snapshot');
      assert.deepEqual(Object.fromEntries(url.searchParams), replay);
      assert.equal(options.method ?? 'GET', 'GET');
      assert.equal(options.redirect, 'error');
      assert.equal(options.headers.Accept, 'application/json');
      assert.ok(options.signal instanceof AbortSignal);
      assert.equal(options.signal.aborted, false);
      return jsonResponse(body);
    },
  });
  assert.equal(calls, 1);
  assert.deepEqual(actual, body);
});

test('Databricks proxy allows only packaged API paths', async () => {
  let calls = 0;
  const fetchImpl = async () => { calls += 1; return jsonResponse(envelope([])); };
  for (const pathname of ['/api/databricks/anything', '/api/databricks/gpus/../../runs', '/api/databricks/gpus/gpu-1/history', 'https://attacker.example/api/databricks/runs', '/api/databricks/runs/']) {
    await assert.rejects(databricksRequest(pathname, params({}), { fetchImpl }), isProxyError(404));
  }
  assert.equal(calls, 0);
  for (const pathname of ['/api/databricks/runs', '/api/databricks/snapshot', '/api/databricks/gpus/gpu-001/history', '/api/databricks/gpus/gpu-024/workload-history']) {
    await databricksRequest(pathname, params({}), { fetchImpl });
  }
  assert.equal(calls, 4);
});

test('Databricks proxy rejects unknown or duplicate query keys before any network call', async () => {
  let calls = 0;
  const fetchImpl = async () => { calls += 1; throw new Error('must not fetch'); };
  const invalid = [
    ['/api/databricks/runs', params({ run_id: replay.run_id })],
    ['/api/databricks/snapshot', params({ ...replay, backendUrl: 'https://attacker.example' })],
    ['/api/databricks/snapshot', params({ ...replay, host: 'attacker.example' })],
    ['/api/databricks/snapshot', params({ ...replay, window_minutes: '5' })],
    ['/api/databricks/snapshot', new URLSearchParams([['run_id', replay.run_id], ['run_id', 'second-run']])],
    ['/api/databricks/gpus/gpu-001/history', new URLSearchParams([['window_minutes', '5'], ['window_minutes', '15']])],
  ];
  for (const [pathname, query] of invalid) {
    await assert.rejects(databricksRequest(pathname, query, { fetchImpl }), isProxyError(400));
  }
  assert.equal(calls, 0);
});

test('Databricks proxy validates server-configured origin without exposing it', async () => {
  let calls = 0;
  const fetchImpl = async () => { calls += 1; throw new Error('must not fetch'); };
  for (const backendUrl of ['file:///etc/passwd', 'http://user:secret@localhost:8010', 'http://localhost:8010/path', 'http://localhost:8010?token=secret', 'http://localhost:8010#secret', 'invalid']) {
    await assert.rejects(databricksRequest('/api/databricks/runs', params({}), { backendUrl, fetchImpl }), problem => {
      assert.ok(isProxyError(503)(problem));
      assert.doesNotMatch(problem.message, /secret|passwd|user:/);
      return true;
    });
  }
  assert.equal(calls, 0);
});

test('Databricks proxy sanitizes HTTP failures and never returns upstream error bodies as data', async () => {
  for (const [upstreamStatus, expected] of [[400, 400], [404, 404], [422, 422], [503, 503], [504, 504], [401, 502], [500, 502], [302, 502]]) {
    await assert.rejects(databricksRequest('/api/databricks/runs', params({}), {
      fetchImpl: async () => new Response('TOKEN secret upstream traceback', { status: upstreamStatus }),
    }), problem => {
      assert.ok(isProxyError(expected)(problem));
      assert.doesNotMatch(problem.message, /TOKEN|secret|traceback/);
      return true;
    });
  }
});

test('Databricks proxy reports unavailable backend instead of returning synthetic fixture success', async () => {
  await assert.rejects(databricksRequest('/api/databricks/runs', params({}), {
    fetchImpl: async () => { throw new Error('ECONNREFUSED TOKEN secret'); },
  }), problem => {
    assert.ok(isProxyError(502)(problem));
    assert.match(problem.message, /backend unavailable/);
    assert.doesNotMatch(problem.message, /TOKEN|secret/);
    return true;
  });
});

test('Databricks proxy maps timeout and abort failures to 504', async () => {
  for (const name of ['TimeoutError', 'AbortError']) {
    await assert.rejects(databricksRequest('/api/databricks/runs', params({}), {
      fetchImpl: async () => { throw new DOMException('secret internal details', name); },
    }), problem => {
      assert.ok(isProxyError(504)(problem));
      assert.match(problem.message, /timed out/);
      assert.doesNotMatch(problem.message, /secret|internal/);
      return true;
    });
  }
});

test('Databricks proxy rejects malformed JSON, missing bodies and wrong provenance', async () => {
  const responses = [
    () => new Response('<html>TOKEN secret</html>'),
    () => new Response(null, { status: 200 }),
    () => jsonResponse(null),
    () => jsonResponse({ source: 'local_fixture', synthetic: true, data: [] }),
    () => jsonResponse({ source: 'databricks', synthetic: false, data: [] }),
    () => jsonResponse({ source: 'databricks', synthetic: true }),
  ];
  for (const response of responses) {
    await assert.rejects(databricksRequest('/api/databricks/runs', params({}), { fetchImpl: async () => response() }), problem => {
      assert.ok(isProxyError(502)(problem));
      assert.doesNotMatch(problem.message, /TOKEN|secret/);
      return true;
    });
  }
});

test('Databricks proxy rejects a stream over eight MiB and stops reading it', async () => {
  let chunksRead = 0;
  let streamClosed = false;
  async function* body() {
    try {
      for (let i = 0; i < 12; i += 1) {
        chunksRead += 1;
        yield Buffer.alloc(1024 * 1024, 32);
      }
    } finally {
      streamClosed = true;
    }
  }
  await assert.rejects(databricksRequest('/api/databricks/runs', params({}), {
    fetchImpl: async () => ({ ok: true, body: body() }),
  }), isProxyError(502));
  assert.equal(chunksRead, 9);
  assert.equal(streamClosed, true);
});

test('Databricks proxy accepts valid JSON at the exact eight MiB limit', async () => {
  const empty = JSON.stringify(envelope(''));
  const payload = JSON.stringify(envelope('x'.repeat(8 * 1024 * 1024 - Buffer.byteLength(empty))));
  assert.equal(Buffer.byteLength(payload), 8 * 1024 * 1024);
  const result = await databricksRequest('/api/databricks/runs', params({}), { fetchImpl: async () => new Response(payload) });
  assert.equal(result.source, 'databricks');
  assert.equal(result.data.length, 8 * 1024 * 1024 - Buffer.byteLength(empty));
});
