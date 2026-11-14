import { createInterface } from 'node:readline';
import { createMcpSession, mcpLine } from './mcp.js';

const handle = createMcpSession();
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  const response = mcpLine(line, handle);
  if (response) process.stdout.write(JSON.stringify(response) + '\n');
}
