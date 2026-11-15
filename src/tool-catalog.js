import { defaultSeed, duration, getSnapshot, inspectEntity, runComparison, scenarioOptions, traceIncident } from './twin.js';
import { attributeStragglers, getActionGate, planMaintenance, triageFleet, triageGpu } from './operations.js';

const scenarioIds = scenarioOptions().map(scenario => scenario.id);
const query = {
  scenario: { type: 'string', enum: scenarioIds, default: 'network' },
  tick: { type: 'integer', minimum: 0, maximum: duration - 1, default: 0 },
  seed: { type: 'integer', minimum: 0, maximum: 9999, default: defaultSeed },
};
const policy = { type: 'string', enum: ['baseline', 'coordinated'], default: 'baseline' };
const entityId = { type: 'string', maxLength: 60, pattern: '^[a-z0-9-]+$' };
const agent = { type: 'string', maxLength: 60, pattern: '^[A-Za-z0-9 ._:/-]+$', description: 'Name of the submitting model or agent' };
const proposal = {
  type: 'object',
  description: 'One action: placement {plan}, power_cap {cap}, drain_gpu {gpuId, notBeforeTick?}, open_rma {gpuId} or maintenance_hold {entityId, plan?}. Optional rationale and claimedEffect {trainingSamplesPerSecond, p95Ms, powerKw}.',
};

// Every tool reads the same shared, gate-operated view of the twin. Once an operator has approved an
// action, agents see its effect; before that, the view is identical to the plain twin snapshot.
const gateFor = ({ scenario = 'network', seed = defaultSeed }) => getActionGate(scenario, seed);
const sharedControl = args => {
  const gate = gateFor(args);
  return gate.hasControl() ? gate.controlAt(args.tick ?? 0) : undefined;
};
const sharedSnapshot = args => getActionGate(args.scenario ?? 'network', args.seed ?? defaultSeed).snapshotAt(args.tick ?? 0);
const readOnly = { readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false };

const toolDefinitions = [
  ['get_snapshot', 'Inspect the infrastructure graph and simulated telemetry for a given minute.', { ...query, policy }, [],
    args => getSnapshot({ ...args, control: sharedControl(args) })],
  ['inspect_entity', 'Inspect a GPU, rack, workload, or infrastructure connection in the shared twin.', { ...query, policy, entityId }, ['entityId'],
    args => inspectEntity({ ...args, control: sharedControl(args) })],
  ['trace_incident', 'Read the deterministic incident signals, supporting telemetry, and proposed response.', query, [],
    args => traceIncident({ ...args, control: sharedControl(args) })],
  ['compare_policies', 'Compare two schedules against the same reproducible 24-minute simulated trace.', { scenario: query.scenario, seed: query.seed }, [],
    ({ scenario, seed }) => runComparison(scenario, seed)],
  ['triage_gpu_health', 'Decode XID events and ECC counters into cordon, drain, reset and RMA steps with checkpoint-aware timing. Omit gpuId to triage every GPU with health events.',
    { ...query, gpuId: entityId }, [], args => args.gpuId ? triageGpu(sharedSnapshot(args), args.gpuId) : triageFleet(sharedSnapshot(args))],
  ['attribute_straggler', 'Find which training rank sets the pace for the synchronous Atlas job, why, and what it costs.', query, [],
    args => attributeStragglers(sharedSnapshot(args))],
  ['plan_maintenance', 'Blast radius for taking a component out of service: affected GPUs and jobs, single points of failure, an evacuation placement and its timing.',
    { ...query, entityId }, ['entityId'],
    args => planMaintenance({ entityId: args.entityId, snapshot: sharedSnapshot(args), control: sharedControl(args) })],
  ['submit_proposal', 'Submit one proposed action to the action gate. The gate simulates and validates it; only a human operator can approve it. Nothing executes from this call.',
    { ...query, agent, proposal }, ['agent', 'proposal'],
    args => gateFor(args).submit(args.proposal, { tick: args.tick ?? gateFor(args).now, source: { kind: 'model', name: args.agent } }),
    { readOnlyHint: false, destructiveHint: false, idempotentHint: false, openWorldHint: false }],
  ['get_action_ledger', 'Read the action gate: proposals, checks, predictions, measured outcomes and the append-only audit ledger.',
    { scenario: query.scenario, seed: query.seed }, [], args => gateFor(args).state()],
].map(([name, description, properties, required, run, annotations = readOnly]) => ({
  spec: {
    name, description,
    inputSchema: { type: 'object', properties, additionalProperties: false, ...(required.length ? { required } : {}) },
    annotations,
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
    // Objects are checked shallowly here and strictly by the action gate's own proposal parser.
    const valid = schema.type === 'object' ? plainObject(value)
      : schema.type === 'integer'
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
