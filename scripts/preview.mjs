import { createServer } from 'node:http';
import { readFile } from 'node:fs/promises';

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
  ['/twin.js', ['twin.js', 'text/javascript']],
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
  const pathname = request.url.split('?', 1)[0];
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

if (import.meta.main) {
  const port = Number(process.env.AI_INFRASTRUCTURE_PORT ?? process.env.CROSSLAYER_PORT ?? 4397);
  try {
    await startPreview(port);
    console.log(`AI infrastructure optimisation platform  http://${host}:${port}`);
  } catch (problem) {
    console.error(`Local preview failed: ${problem.code ?? problem.message}`);
    process.exitCode = 1;
  }
}
