// One explicit source at startup: an exported replay by default, or the configured
// connected backend. A failed connected request never falls back to the export.
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { DatabricksProxyError, databricksRequest, validateDatabricksRequest } from './databricks-api.mjs';

const bridge = fileURLToPath(new URL('../databricks/backend/replay_bridge.py', import.meta.url));
const cwd = fileURLToPath(new URL('../databricks/', import.meta.url));
const runPattern = /^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i;
const timePattern = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

function replayArguments(pathname, params) {
  if (pathname.endsWith('/runs')) return ['runs'];
  const run = params.get('run_id'), time = params.get('as_of_ts');
  if (!runPattern.test(run ?? '') || !timePattern.test(time ?? '') || !Number.isFinite(Date.parse(time))) {
    throw new DatabricksProxyError(400, 'Select a valid replay and UTC timestamp');
  }
  if (pathname.endsWith('/snapshot')) return ['snapshot', run, time];
  const match = pathname.match(/\/gpus\/(gpu-\d{3})\/(history|workload-history)$/);
  const window = params.get('window_minutes') ?? '5';
  if (!match || !/^\d+$/.test(window) || Number(window) < 1 || Number(window) > 15) {
    throw new DatabricksProxyError(400, 'History requires a GPU and a window of 1–15 minutes');
  }
  return [match[2], run, time, match[1], String(Number(window))];
}

function runReplayBridge(args) {
  const python = process.env.CROSSLAYER_PYTHON || (process.platform === 'win32' ? 'python' : 'python3');
  return new Promise((resolve, reject) => {
    execFile(python, ['-I', bridge, ...args], { cwd, timeout: 90_000, maxBuffer: 8 * 1024 * 1024, windowsHide: true }, (problem, stdout) => {
      let body;
      try { body = JSON.parse(stdout); } catch { /* handled below without exposing stderr */ }
      if (body?.error && [400, 502, 503].includes(body.status)) {
        return reject(new DatabricksProxyError(body.status, body.error));
      }
      if (problem || !body) {
        const message = problem?.code === 'ENOENT'
          ? 'Python 3 is required for the local detector. Install Python or set CROSSLAYER_PYTHON to its executable.'
          : problem?.killed ? 'The local deterministic detector timed out'
          : 'The saved replay could not run. Check Python and pull the complete repository, including databricks/demo.';
        return reject(new DatabricksProxyError(502, message));
      }
      resolve(body);
    });
  });
}

export async function databricksSourceRequest(pathname, params, { backendUrl = process.env.DATABRICKS_API_URL, fetchImpl = fetch, runReplay = runReplayBridge } = {}) {
  validateDatabricksRequest(pathname, params);
  if (backendUrl !== undefined) {
    const result = await databricksRequest(pathname, params, { backendUrl, fetchImpl });
    if (result.delivery && result.delivery !== 'connected') throw new DatabricksProxyError(502, 'The configured backend returned a different delivery mode');
    return { ...result, delivery: 'connected' };
  }
  const result = await runReplay(replayArguments(pathname, params));
  if (result?.source !== 'databricks' || result.synthetic !== true || result.delivery !== 'saved_replay' || !Number.isFinite(Date.parse(result.exported_at)) || !Object.hasOwn(result, 'data')) {
    throw new DatabricksProxyError(502, 'The saved replay returned invalid provenance');
  }
  if (pathname.endsWith('/snapshot') && (result.data?.delivery !== 'saved_replay'
      || result.data.exported_at !== result.exported_at
      || result.data.persistence?.status !== 'not_saved'
      || result.data.persistence?.reason !== 'saved_replay')) {
    throw new DatabricksProxyError(502, 'A local replay cannot claim that this assessment was saved in Databricks');
  }
  return result;
}
