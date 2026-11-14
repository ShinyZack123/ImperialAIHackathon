// Same-origin evidence/assessment bridge. Browser input cannot select an upstream host/path.
const allowedPath = /^\/api\/databricks\/(runs|snapshot|gpus\/gpu-[0-9]{3}\/(history|workload-history))$/;
const timeoutMs = 240_000; // SQL warehouse startup + official detector result persistence.
const maxResponseBytes = 8 * 1024 * 1024;

export class DatabricksProxyError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

export async function databricksRequest(pathname, searchParams, { fetchImpl = fetch, backendUrl = process.env.DATABRICKS_API_URL || 'http://127.0.0.1:8010' } = {}) {
  if (!allowedPath.test(pathname)) throw new DatabricksProxyError(404, 'Unknown Databricks API route');
  const allowed = pathname.endsWith('/runs') ? [] : pathname.endsWith('/snapshot') ? ['run_id', 'as_of_ts'] : ['run_id', 'as_of_ts', 'window_minutes'];
  for (const key of searchParams.keys()) {
    if (!allowed.includes(key) || searchParams.getAll(key).length !== 1) throw new DatabricksProxyError(400, 'Unexpected or repeated query parameter');
  }
  let upstream;
  try {
    upstream = new URL(backendUrl);
    if (!['http:', 'https:'].includes(upstream.protocol) || upstream.username || upstream.password || upstream.search || upstream.hash || !['', '/'].includes(upstream.pathname)) throw new Error('Invalid backend origin');
    upstream.pathname = pathname;
    upstream.search = searchParams.toString();
  } catch {
    throw new DatabricksProxyError(503, 'The server Databricks API origin is not configured correctly');
  }
  try {
    const response = await fetchImpl(upstream, { headers: { Accept: 'application/json' }, signal: AbortSignal.timeout(timeoutMs), redirect: 'error' });
    if (!response.ok) {
      // Do not forward arbitrary upstream HTML, credentials or tracebacks.
      const status = [400, 404, 422, 503, 504].includes(response.status) ? response.status : 502;
      const message = status === 422 || status === 400 ? 'Invalid replay time, run ID or GPU selection' : status === 504 ? 'Databricks query timed out; the SQL warehouse may be starting' : 'Databricks request failed. Check the backend login, configuration and warehouse access';
      throw new DatabricksProxyError(status, message);
    }
    if (!response.body) throw new Error('No upstream body');
    let bytes = 0;
    const chunks = [];
    for await (const chunk of response.body) {
      bytes += chunk.byteLength;
      if (bytes > maxResponseBytes) throw new Error('Oversized response');
      chunks.push(chunk);
    }
    const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
    if (body.source !== 'databricks' || body.synthetic !== true || !Object.hasOwn(body, 'data')) throw new Error('Unexpected data source');
    return body;
  } catch (problem) {
    if (problem instanceof DatabricksProxyError) throw problem;
    const timeout = problem?.name === 'TimeoutError' || problem?.name === 'AbortError';
    throw new DatabricksProxyError(timeout ? 504 : 502, timeout ? 'Databricks request timed out' : 'Databricks backend unavailable. Start the Python API and check its connection');
  }
}
