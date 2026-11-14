import { defaultSeed, duration, getSnapshot, inspectEntity, runComparison, scenarioOptions, traceIncident } from './twin.js';

const scenarioIds = scenarioOptions().map(scenario => scenario.id);
const query = {
  scenario: { type: 'string', enum: scenarioIds, default: 'network' },
  tick: { type: 'integer', minimum: 0, maximum: duration - 1, default: 0 },
  seed: { type: 'integer', minimum: 0, maximum: 9999, default: defaultSeed },
};
const policy = { type: 'string', enum: ['baseline', 'coordinated'], default: 'baseline' };
const entityId = { type: 'string', maxLength: 60, pattern: '^[a-z0-9-]+$' };
const toolDefinitions = [
  ['get_snapshot', 'Inspect the infrastructure graph and simulated telemetry for a given minute.', { ...query, policy }, [], getSnapshot],
  ['inspect_entity', 'Inspect a GPU, rack, workload, or infrastructure connection in the shared twin.', { ...query, policy, entityId }, ['entityId'], inspectEntity],
  ['trace_incident', 'Read the deterministic incident signals, supporting telemetry, and proposed response.', query, [], traceIncident],
  ['compare_policies', 'Compare two schedules against the same reproducible 24-minute simulated trace.', { scenario: query.scenario, seed: query.seed }, [],
    ({ scenario, seed }) => runComparison(scenario, seed)],
].map(([name, description, properties, required, run]) => ({
  spec: {
    name, description,
    inputSchema: { type: 'object', properties, additionalProperties: false, ...(required.length ? { required } : {}) },
    annotations: { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false },
  },
  run,
}));

const plainObject = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const toolResult = (value, isError = false) => ({
  content: [{ type: 'text', text: isError ? value : JSON.stringify(value) }],
  ...(isError ? { isError } : { structuredContent: value }),
});

function invokeMcpTool(params) {
  if (!plainObject(params) || typeof params.name !== 'string') throw new RangeError('Expected a tool name');
  const tool = toolDefinitions.find(item => item.spec.name === params.name);
  if (!tool) throw new RangeError('Unknown tool');
  const args = Object.hasOwn(params, 'arguments') ? params.arguments : {};
  if (!plainObject(args)) throw new RangeError('Arguments must be an object');
  const { properties, required = [] } = tool.spec.inputSchema;
  if (Object.keys(args).some(key => !Object.hasOwn(properties, key))) throw new RangeError('Unexpected tool argument');
  if (required.some(key => !Object.hasOwn(args, key))) throw new RangeError('A required argument is missing');
  for (const [key, value] of Object.entries(args)) {
    const schema = properties[key];
    const valid = schema.type === 'integer'
      ? Number.isInteger(value) && value >= schema.minimum && value <= schema.maximum
      : typeof value === 'string' && (schema.enum ? schema.enum.includes(value) : value.length <= schema.maxLength && new RegExp(schema.pattern).test(value));
    if (!valid) throw new RangeError(`Invalid ${key}`);
  }
  try {
    return toolResult(tool.run(args));
  } catch (cause) {
    if (cause instanceof RangeError) return toolResult(cause.message, true);
    throw cause;
  }
}

const mcpToolCatalog = toolDefinitions.map(tool => tool.spec);

export { invokeMcpTool, mcpToolCatalog, plainObject };
