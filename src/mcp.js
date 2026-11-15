import { invokeMcpTool, mcpToolCatalog, plainObject } from './tool-catalog.js';

const version = '2025-11-25';
const info = { name: 'Ithil', version: '0.1.0' };
const error = (id, code, message) => ({ jsonrpc: '2.0', id, error: { code, message } });
function createMcpSession() {
  let ready = false;
  let initialized = false;
  return function handle(message) {
    if (!plainObject(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') return error(null, -32600, 'Invalid request');
    const { id, method, params } = message;
    if (id !== undefined && typeof id !== 'string' && !Number.isInteger(id)) return error(null, -32600, 'Invalid request ID');
    if (id === undefined) {
      if (method === 'notifications/initialized' && initialized) ready = true;
      return null;
    }
    const reply = result => ({ jsonrpc: '2.0', id, result });
    if (method === 'initialize') {
      if (initialized) return error(id, -32600, 'Session already initialized');
      if (!plainObject(params) || typeof params.protocolVersion !== 'string' || !plainObject(params.clientInfo) ||
          typeof params.clientInfo.name !== 'string' || typeof params.clientInfo.version !== 'string' || !plainObject(params.capabilities)) {
        return error(id, -32602, 'Invalid initialization parameters');
      }
      initialized = true;
      return reply({ protocolVersion: version, capabilities: { tools: {} }, serverInfo: info });
    }
    if (method === 'ping') return reply({});
    if (!ready) return error(id, -32000, 'Session is not initialized');
    if (method === 'tools/list') return reply({ tools: mcpToolCatalog });
    if (method === 'tools/call') {
      try {
        return reply(invokeMcpTool(params));
      } catch (cause) {
        if (cause instanceof RangeError) return error(id, -32602, cause.message);
        return error(id, -32603, 'Internal error');
      }
    }
    return error(id, -32601, 'Method not found');
  };
}

function mcpLine(line, handle) {
  let request;
  try {
    request = JSON.parse(line);
  } catch {
    return error(null, -32700, 'Parse error');
  }
  return handle(request);
}

export { createMcpSession, mcpLine };
