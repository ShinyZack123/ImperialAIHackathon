import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { FlaggingError, flaggingRequest } from './flagging-api.mjs';
import { DatabricksProxyError } from './databricks-api.mjs';
import { databricksSourceRequest } from './databricks-source.mjs';

export function startPreview(port = 4397) {
  if (!Number.isInteger(port) || port < 1024 || port > 65535) {
    throw new RangeError('The preview port must be an integer from 1024 through 65535');
  }
  const server = createServer({ requestTimeout: 5000 }, serve);
  return new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, host, () => {
      server.off('error', reject);
      resolve(server);
    });
  });
}
const host = '127.0.0.1';
const paths = new Map([
  ['/', ['index.html', 'text/html']],
  ['/index.html', ['index.html', 'text/html']],
  ['/styles.css', ['styles.css', 'text/css']],
  ['/ui.js', ['ui.js', 'text/javascript']],
  ['/flagging.js', ['flagging.js', 'text/javascript']],
  ['/databricks.js', ['databricks.js', 'text/javascript']],
  ['/twin.js', ['twin.js', 'text/javascript']],
  ['/operations.js', ['operations.js', 'text/javascript']],
  ['/operations-view.js', ['operations-view.js', 'text/javascript']],
  ['/tool-catalog.js', ['tool-catalog.js', 'text/javascript']],
  ['/ai-infrastructure-optimisation-platform.html', ['ai-infrastructure-optimisation-platform.html', 'text/html']],
  ['/Crosslayer.html', ['Crosslayer.html', 'text/html']],
]);
async function serve(request, response) {
  response.setHeader('X-Content-Type-Options', 'nosniff');
  response.setHeader('Cache-Control', 'no-store');
  if (!['GET', 'HEAD'].includes(request.method)) {
    response.writeHead(405, { Allow: 'GET, HEAD' }).end();
    return;
  }
  const url = new URL(request.url, `http://${host}`);
  const { pathname } = url;
  if (pathname.startsWith('/api/databricks/')) {
    await serveDatabricks(request, response, url);
    return;
  }
  if (pathname.startsWith('/api/flagging/')) {
    await serveFlagging(request, response, url);
    return;
  }
  if (pathname === '/favicon.ico') {
    response.writeHead(204).end();
    return;
  }
  const route = paths.get(pathname);
  if (!route) {
    response.writeHead(404, { 'Content-Type': 'text/plain; charset=utf-8' }).end('Not found');
    return;
  }
  const [file, mime] = route;
  try {
    const content = await readFile(new URL(`../dist/${file}`, import.meta.url));
    response.writeHead(200, { 'Content-Type': `${mime}; charset=utf-8`, 'Content-Length': content.length })
      .end(request.method === 'HEAD' ? undefined : content);
  } catch {
    response.writeHead(503, { 'Content-Type': 'text/plain; charset=utf-8' }).end('Run npm run build before starting the preview.');
  }
}

async function serveFlagging(request, response, url) {
  let status = 200;
  let body;
  try {
    body = await flaggingRequest(url.pathname, url.searchParams);
  } catch (problem) {
    status = problem instanceof FlaggingError ? problem.status : 500;
    body = { error: problem instanceof FlaggingError ? problem.message : 'Internal error' };
    if (!(problem instanceof FlaggingError)) console.error('flagging bridge', problem);
  }
  const content = Buffer.from(JSON.stringify(body));
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': content.length })
    .end(request.method === 'HEAD' ? undefined : content);
}

async function serveDatabricks(request, response, url) {
  let status = 200;
  let body;
  try {
    body = await databricksSourceRequest(url.pathname, url.searchParams);
  } catch (problem) {
    status = problem instanceof DatabricksProxyError ? problem.status : 500;
    body = { error: problem instanceof DatabricksProxyError ? problem.message : 'Databricks request failed' };
  }
  const content = Buffer.from(JSON.stringify(body));
  response.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': content.length })
    .end(request.method === 'HEAD' ? undefined : content);
}

// import.meta.main needs Node 22.18+; fall back to comparing the entry script on older runtimes.
const isEntry = import.meta.main ?? process.argv[1] === fileURLToPath(import.meta.url);
if (isEntry) {
  const port = Number(process.env.CROSSLAYER_PORT ?? 4397);
  try {
    await startPreview(port);
    console.log(`AI infrastructure optimisation platform  http://${host}:${port}`);
    console.log(process.env.DATABRICKS_API_URL === undefined
      ? 'Databricks: saved synthetic export; detector runs locally; no Databricks login needed.'
      : 'Databricks: connected backend; authentication stays on that backend.');
  } catch (problem) {
    console.error(`Local preview failed: ${problem.code ?? problem.message}`);
    process.exitCode = 1;
  }
}
