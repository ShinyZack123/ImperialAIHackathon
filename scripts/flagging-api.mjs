// Local bridge from the console to the deterministic Python flagger (flagging/bridge.py).
// The browser never sends code or paths: only a scenario ID and a console entity ID, both checked
// against a strict pattern before python3 is started with an argument vector (no shell).
import { execFile } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const bridge = fileURLToPath(new URL('../flagging/bridge.py', import.meta.url));
const cwd = fileURLToPath(new URL('../flagging/', import.meta.url));
const python = process.env.CROSSLAYER_PYTHON || 'python3';
const idPattern = /^[A-Za-z0-9][A-Za-z0-9-]{0,80}$/;
const timeoutMs = 90_000;          // a cold analysis of the 3-day drift scenario takes ~15 s
const cache = new Map();           // key -> Promise<result>, deterministic so safe to reuse
const cacheLimit = 96;

export class FlaggingError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

function runBridge(args) {
  return new Promise((resolve, reject) => {
    const started = performance.now();
    execFile(python, ['-I', bridge, ...args], { cwd, timeout: timeoutMs, maxBuffer: 64 * 1024 * 1024, windowsHide: true },
      (problem, stdout) => {
        let body = null;
        try { body = JSON.parse(stdout); } catch { /* handled below */ }
        if (body?.error) return reject(new FlaggingError(400, body.error));
        if (problem || !body) {
          const reason = problem?.killed ? 'Flagger timed out' : problem?.code === 'ENOENT' ? 'python3 not found' : 'Flagger failed';
          return reject(new FlaggingError(502, reason));
        }
        body.bridgeMs = Math.round(performance.now() - started);
        resolve(body);
      });
  });
}

function cached(key, args) {
  if (cache.has(key)) return cache.get(key);
  const pending = runBridge(args).catch(problem => {
    cache.delete(key);              // never cache failures
    throw problem;
  });
  if (cache.size >= cacheLimit) cache.delete(cache.keys().next().value);
  cache.set(key, pending);
  return pending;
}

const checkId = (value, name) => {
  if (typeof value !== 'string' || !idPattern.test(value)) throw new FlaggingError(400, `Invalid ${name}`);
  return value;
};

export async function flaggingRequest(pathname, searchParams) {
  if (pathname === '/api/flagging/catalog') return cached('catalog', ['catalog']);
  if (pathname === '/api/flagging/analysis') {
    const scenario = checkId(searchParams.get('scenario'), 'scenario');
    return cached(`analysis/${scenario}`, ['analyse', scenario]);
  }
  if (pathname === '/api/flagging/entity') {
    const scenario = checkId(searchParams.get('scenario'), 'scenario');
    const entity = checkId(searchParams.get('entity'), 'entity');
    // Each click runs the Python bridge for that entity; the analysis itself is cached.
    return runBridge(['entity', scenario, entity]);
  }
  throw new FlaggingError(404, 'Not found');
}
