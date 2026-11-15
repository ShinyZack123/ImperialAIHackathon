const schemaVersion = '1.0.0';
const duration = 24;
const defaultSeed = 73;
const startedAt = Date.UTC(2026, 10, 14, 12);
const minute = 60_000;
const gpuCount = 8;
const trainingGpus = 12;
const inferenceSlo = 160;
const inferenceCapacity = 480;
const rackTemperatureLimit = 82;
const rackPowerLimit = 9;
const plantPower = 7.4;
// Declared scheduler metadata for Atlas. Checkpoints land at minutes 3, 11 and 19 of the replay.
const checkpointIntervalMinutes = 8;
const checkpointOffset = 3;
// Declared simulation assumption, not a market price: the blended cost of one GPU-hour.
const gpuHourCost = 2.4;
const nominalSmClockMhz = 1980;
const floor = [
  { id: 'rack-a01', label: 'A01', zone: 'A', power: 'pdu-a', cooling: 'cdu-a' },
  { id: 'rack-a02', label: 'A02', zone: 'A', power: 'pdu-a', cooling: 'cdu-a' },
  { id: 'rack-b01', label: 'B01', zone: 'B', power: 'pdu-b', cooling: 'cdu-b' },
  { id: 'rack-b02', label: 'B02', zone: 'B', power: 'pdu-b', cooling: 'cdu-b' },
];

const scenarios = {
  network: {
    title: 'Network degradation',
    incident: 'Degraded fabric uplink',
    summary: 'Atlas training ranks are waiting on the A02 uplink.',
    details: 'The training ranks on A02 share an impaired uplink. Their collective communication sets the pace for every other rank.',
    layer: 'fabric', target: 'link-a02', status: 'degraded',
    hypotheses: ['A02 uplink impairment', 'Rack power draw', 'GPU thermal throttling'],
    response: 'Place training ranks on healthy paths and verify the latency and facility limits.',
  },
  demand: {
    title: 'Inference demand surge',
    incident: 'Inference queue building',
    summary: 'Relay request demand has climbed above the serving pool’s latency budget.',
    details: 'More tokens are arriving than the serving pool can handle within its latency target. Training still holds its original allocation.',
    layer: 'fabric', target: 'job-relay', status: 'critical',
    hypotheses: ['Insufficient serving headroom', 'Fabric saturation', 'Thermal throttling'],
    response: 'Reserve more serving slots while keeping all twelve Atlas ranks assigned.',
  },
  power: {
    title: 'Power budget reduction',
    incident: 'Site power cap tightened',
    summary: 'Combined workload demand exceeds the new site power budget.',
    details: 'The lower site budget triggers a broad power derate. The exercise tests targeted training caps while maintaining serving capacity.',
    layer: 'power', target: 'grid-01', status: 'warning',
    hypotheses: ['Site power budget reduction', 'Single-rack power overload', 'Serving demand increase'],
    response: 'Apply a targeted simulated training cap and maintain the inference latency target.',
  },
  thermal: {
    title: 'Thermal constraint',
    incident: 'Rack B02 approaching thermal limit',
    summary: 'Cooling headroom is falling in B02 while Relay is serving traffic there.',
    details: 'Higher coolant inlet temperature forces B02 to derate. The cooling feed for A02 has gone stale, so its headroom is unknown to the policy.',
    layer: 'cooling', target: 'rack-b02', status: 'warning',
    hypotheses: ['B02 coolant inlet rise', 'Fabric congestion', 'Site power cap'],
    response: 'Move load out of B02 and keep allocations away from racks with unverifiable cooling.',
  },
  'gpu-xid': {
    title: 'GPU memory errors',
    incident: 'Row remapping pending on GPU A01-03',
    summary: 'An Atlas rank is logging XID 63 and rising correctable ECC errors.',
    details: 'GPU A01-03 has a pending memory row remap and a climbing correctable error count. In this trace it raises an uncorrectable error (XID 48) at minute 17, which stops the synchronous Atlas job unless the GPU is drained first.',
    layer: 'fabric', target: 'gpu-a01-03', status: 'warning',
    hypotheses: ['Degrading HBM on GPU A01-03', 'Driver or application fault', 'Thermal stress'],
    response: 'Cordon the GPU, drain its rank at the next checkpoint, reset it to apply the remap and open an RMA if errors recur.',
  },
  straggler: {
    title: 'Straggling training rank',
    incident: 'GPU A02-05 is thermally throttled',
    summary: 'One hot Atlas rank is setting the pace for all twelve.',
    details: 'GPU A02-05 reports hardware thermal slowdown and a reduced SM clock. Synchronous training waits for its slowest rank, so the whole job runs at that GPU’s speed.',
    layer: 'cooling', target: 'gpu-a02-05', status: 'warning',
    hypotheses: ['GPU A02-05 hardware thermal slowdown', 'A02 uplink congestion', 'Rack power derate'],
    response: 'Move the slow rank to a spare GPU at the next checkpoint and inspect the GPU’s cold plate.',
  },
};

function scenarioOptions() {
  return Object.entries(scenarios).map(([id, { title, incident, summary, layer }]) => ({ id, title, incident, summary, layer }));
}

const gpuId = (rack, slot) => `gpu-${rack.id.slice(5)}-${String(slot + 1).padStart(2, '0')}`;
const leafId = (rack) => `leaf-${rack.id.slice(5)}`;
const linkId = (rack) => `link-${rack.id.slice(5)}`;
const round = (value, digits = 2) => Number(value.toFixed(digits));
const sum = (values) => values.reduce((total, value) => total + value, 0);
const mean = (values) => sum(values) / values.length;
const capacity = (n) => Array.from({ length: n }, (_, i) => i);
const number = value => new Intl.NumberFormat('en-GB').format(value);

const entities = [
  { id: 'site-lon01', type: 'site', label: 'London 01' },
  { id: 'grid-01', type: 'power', label: 'Site feed' },
  { id: 'pdu-a', type: 'power', label: 'PDU A' },
  { id: 'pdu-b', type: 'power', label: 'PDU B' },
  { id: 'cdu-a', type: 'cooling', label: 'Cooling loop A' },
  { id: 'cdu-b', type: 'cooling', label: 'Cooling loop B' },
  { id: 'sw-spine', type: 'switch', label: 'Spine 01' },
  { id: 'job-atlas', type: 'workload', label: 'Atlas', workloadType: 'training' },
  { id: 'job-relay', type: 'workload', label: 'Relay', workloadType: 'inference' },
  ...floor.flatMap(rack => [
    { id: rack.id, type: 'rack', label: `Rack ${rack.label}`, zone: rack.zone },
    { id: leafId(rack), type: 'switch', label: `Leaf ${rack.label}`, rackId: rack.id },
    ...capacity(gpuCount).map(slot => ({
      id: gpuId(rack, slot), type: 'gpu', label: `GPU ${String(slot + 1).padStart(2, '0')}`, rackId: rack.id, slot: slot + 1,
    })),
  ]),
];

const entityById = new Map(entities.map(entity => [entity.id, entity]));
const physicalEdges = [
  { id: 'site-feed', from: 'site-lon01', to: 'grid-01', type: 'contains' },
  ...['pdu-a', 'pdu-b'].map(id => ({ id: `feed-${id}`, from: 'grid-01', to: id, type: 'power' })),
  ...floor.flatMap(rack => [
    { id: `contains-${rack.id}`, from: 'site-lon01', to: rack.id, type: 'contains' },
    { id: linkId(rack), from: 'sw-spine', to: leafId(rack), type: 'fabric', ratedGbps: 200 },
    { id: `trunk-${rack.id}`, from: leafId(rack), to: rack.id, type: 'fabric', ratedGbps: 200 },
    { id: `power-${rack.id}`, from: rack.power, to: rack.id, type: 'power' },
    { id: `cooling-${rack.id}`, from: rack.cooling, to: rack.id, type: 'cooling' },
    ...capacity(gpuCount).flatMap(slot => [
      { id: `slot-${gpuId(rack, slot)}`, from: rack.id, to: gpuId(rack, slot), type: 'contains' },
      { id: `peer-${gpuId(rack, slot)}`, from: gpuId(rack, slot), to: gpuId(rack, (slot + 1) % gpuCount), type: 'gpu-peer' },
    ]),
  ]),
];

function supportedScenario(value) {
  if (!Object.hasOwn(scenarios, value)) throw new RangeError('Unknown scenario');
  return scenarios[value];
}

function validateInput(scenario, tick, seed) {
  supportedScenario(scenario);
  if (!Number.isInteger(tick) || tick < 0 || tick >= duration) throw new RangeError('Minute must be from 0 to 23');
  if (!Number.isInteger(seed) || seed < 0 || seed > 9999) throw new RangeError('Seed must be from 0 to 9999');
}

function environment(scenario, tick, seed) {
  const wave = (period, phase = 0) => Math.sin((tick + phase + (seed % 11) * .17) / period);
  const demand = scenario === 'demand'
    ? 5250 + 510 * Math.max(0, wave(3.7)) + 130 * wave(1.7, 1.2)
    : 3320 + 210 * wave(3.3) + 90 * wave(1.6, 1.2);
  const inlet = [28, 28.8, 27.6, 28.4].map((base, i) => base + 1.3 * wave(3.6, i));
  if (scenario === 'thermal') inlet[3] += 14.4 + 1.2 * Math.max(0, wave(3.5));
  return {
    scenario, tick,
    demand: round(demand, 0),
    price: round((scenario === 'power' ? .31 : .165) + .017 * wave(5, .8), 4),
    powerLimit: scenario === 'power' ? 32.2 : 43,
    inlet,
    // The sandbox plant has an underlying temperature even when its sensor stream is stale.
    staleRack: scenario === 'thermal' ? 'rack-a02' : null,
    links: floor.map((_, i) => ({
      capacity: scenario === 'network' && i === 1 ? round(55 + 7 * wave(2.9), 1) : 200,
      errors: scenario === 'network' && i === 1 ? Math.round(30 + 13 * (1 + wave(2.4))) : 0,
    })),
    gpuFaults: gpuFaults(scenario, tick, wave),
  };
}

// GPU-level health faults. Counters follow NVML/DCGM conventions; XID codes follow NVIDIA's driver catalogue.
function gpuFaults(scenario, tick, wave) {
  if (scenario === 'gpu-xid') {
    const xid = [{ code: 63, minute: -2 }, { code: 63, minute: 6 }, { code: 48, minute: 17 }]
      .filter(event => event.minute <= tick)
      .map(event => ({ code: event.code, at: new Date(startedAt + event.minute * minute).toISOString() }));
    return { 'gpu-a01-03': {
      xid, eccCorrected: 184 + tick * 23, eccUncorrected: tick >= 17 ? 1 : 0, remapPending: true,
      failed: tick >= 17, clockFactor: 1, throttle: [], hotspot: 0,
    } };
  }
  if (scenario === 'straggler') {
    return { 'gpu-a02-05': {
      xid: [], eccCorrected: 0, eccUncorrected: 0, remapPending: false, failed: false,
      clockFactor: round(.75 - .03 * Math.max(0, wave(3.1)), 3), throttle: ['hw_thermal_slowdown'], hotspot: 14,
    } };
  }
  return {};
}

// Atlas checkpoint schedule. `last` is the latest completed checkpoint at or before the minute;
// `next` is the earliest checkpoint at or after it and may fall beyond the replay.
function checkpointAt(tick) {
  const phase = ((tick - checkpointOffset) % checkpointIntervalMinutes + checkpointIntervalMinutes) % checkpointIntervalMinutes;
  const last = tick - phase;
  return {
    last, // May be negative: Atlas was already running and checkpointing before the replay began.
    next: phase === 0 ? tick : last + checkpointIntervalMinutes,
    intervalMinutes: checkpointIntervalMinutes,
  };
}

function observed(value, unit, tick, quality = 'fresh', source = 'simulated', digits = 2) {
  return {
    value: quality === 'fresh' ? round(value, digits) : null,
    unit, source, quality,
    sampledAt: quality === 'missing' ? null : new Date(startedAt + (tick - (quality === 'stale' ? 5 : 0)) * minute).toISOString(),
  };
}

function fillSlots(train, count, order) {
  const infer = [0, 0, 0, 0];
  for (const i of order) {
    if (count <= 0) break;
    infer[i] = Math.min(gpuCount - train[i], count);
    count -= infer[i];
  }
  return infer;
}

function baseline(state) {
  const train = [6, 6, 0, 0];
  // Keep existing ranks fixed. Provision additional service slots only after demand crosses 90% of capacity.
  const infer = state.scenario === 'thermal' ? [0, 0, 2, 8] : [0, 0, 6, 4];
  let extra = Math.max(0, Math.ceil(state.demand / (inferenceCapacity * .90)) - sum(infer));
  for (const i of [2, 3, 0, 1]) {
    const amount = Math.min(extra, gpuCount - train[i] - infer[i]);
    infer[i] += amount;
    extra -= amount;
  }
  return { train, infer, cap: 1 };
}

const powerCapSteps = [1, .92, .84, .76];
const gpuIds = new Set(entities.filter(entity => entity.type === 'gpu').map(entity => entity.id));
const noOperations = { unavailable: new Set(), cordoned: new Set(), restarting: false };

// Fill each rack's usable slots in order: training ranks first, then serving replicas.
function slotJobs(plan, rackIndex, unavailable = noOperations.unavailable) {
  const jobs = Array(gpuCount).fill(null);
  const free = capacity(gpuCount).filter(slot => !unavailable.has(gpuId(floor[rackIndex], slot)));
  free.forEach((slot, k) => {
    jobs[slot] = k < plan.train[rackIndex] ? 'job-atlas' : k < plan.train[rackIndex] + plan.infer[rackIndex] ? 'job-relay' : null;
  });
  return jobs;
}

// The placement that can actually run once drained or held GPUs are removed from the pool.
function effectivePlan(plan, unavailable = noOperations.unavailable) {
  const train = [], infer = [];
  floor.forEach((rack, i) => {
    const free = capacity(gpuCount).filter(slot => !unavailable.has(gpuId(rack, slot))).length;
    train.push(Math.min(plan.train[i], free));
    infer.push(Math.min(plan.infer[i], free - train[i]));
  });
  return { train, infer, cap: plan.cap };
}

// Validate an operator control overlay at the twin boundary. Only the action gate should construct one.
function normalizeControl(control) {
  if (control === undefined || control === null) return null;
  if (typeof control !== 'object' || Array.isArray(control)) throw new RangeError('Control must be an object');
  const allowed = ['plan', 'cap', 'unavailable', 'cordoned', 'restarting'];
  if (Object.keys(control).some(key => !allowed.includes(key))) throw new RangeError('Unexpected control field');
  const plan = control.plan ?? null;
  if (plan !== null) validatePlan(plan);
  const cap = control.cap ?? null;
  if (cap !== null && !powerCapSteps.includes(cap)) throw new RangeError('Unsupported power setting');
  const ids = (list, name) => {
    if (list === undefined) return new Set();
    if (!Array.isArray(list) || list.length > gpuIds.size || list.some(id => !gpuIds.has(id))) throw new RangeError(`Invalid ${name} GPU list`);
    return new Set(list);
  };
  if (control.restarting !== undefined && typeof control.restarting !== 'boolean') throw new RangeError('Invalid restart flag');
  return { plan, cap, unavailable: ids(control.unavailable, 'unavailable'), cordoned: ids(control.cordoned, 'cordoned'),
    restarting: control.restarting ?? false };
}

function validatePlan(plan) {
  if (!plan || typeof plan !== 'object' || Array.isArray(plan)) throw new RangeError('Plan must be an object');
  if (Object.keys(plan).some(key => !['train', 'infer', 'cap'].includes(key))) throw new RangeError('Unexpected plan field');
  const counts = list => Array.isArray(list) && list.length === floor.length && list.every(n => Number.isInteger(n) && n >= 0 && n <= gpuCount);
  if (!counts(plan.train) || !counts(plan.infer)) throw new RangeError('Plan needs four rack slot counts from 0 to 8');
  if (plan.train.some((n, i) => n + plan.infer[i] > gpuCount)) throw new RangeError('Plan exceeds eight slots in a rack');
  if (!powerCapSteps.includes(plan.cap)) throw new RangeError('Unsupported power setting');
  return plan;
}

function gpuCondition(state, id) {
  return state.gpuFaults[id] ?? null;
}

function plant(state, requested, operations = noOperations) {
  const plan = effectivePlan(requested, operations.unavailable);
  const baseRackPower = 3.15;
  const trainingPower = .68 * (.27 + .73 * plan.cap ** 2);
  const request = floor.map((_, i) => plan.train[i] * trainingPower + plan.infer[i] * .5);
  const totalRequest = sum(request);
  const available = state.powerLimit - plantPower - baseRackPower * floor.length;
  const siteScale = totalRequest ? Math.min(1, available / totalRequest) : 1;
  const spread = plan.train.filter(Boolean).length;
  const racks = floor.map((rack, i) => {
    const requested = request[i];
    const scale = requested ? Math.min(siteScale, (rackPowerLimit - baseRackPower) / requested,
      Math.max(0, (rackTemperatureLimit - state.inlet[i] - 17) / (6.5 * requested))) : 1;
    const draw = baseRackPower + requested * scale;
    const requestedTemperature = state.inlet[i] + 17 + 6.5 * requested;
    const traffic = (spread > 1 ? plan.train[i] * 15 : 0) + plan.infer[i] * 1.8;
    return {
      id: rack.id, draw, requestedDraw: baseRackPower + requested,
      temperature: state.inlet[i] + 17 + 6.5 * requested * scale, requestedTemperature,
      traffic, scale, capacity: state.links[i].capacity, errors: state.links[i].errors,
    };
  });
  // A straggling rank limits the whole synchronous job after the network transit penalty.
  const network = Math.min(...racks.map((rack, i) => {
    if (!plan.train[i] || spread === 1) return 1;
    const delivery = rack.errors ? .86 : 1;
    return Math.min(1, rack.capacity / Math.max(1, rack.traffic)) * delivery;
  }));
  const trainScale = Math.min(...racks.map((rack, i) => plan.train[i] ? rack.scale : 1));
  const jobs = floor.map((_, i) => slotJobs(plan, i, operations.unavailable));
  const assigned = job => jobs.flatMap((slots, i) => slots.flatMap((value, slot) => value === job ? [gpuId(floor[i], slot)] : []));
  const atlasGpus = assigned('job-atlas');
  // Synchronous training waits for its slowest rank and stops if any rank fails or the world size cannot be met.
  const rankFactors = atlasGpus.map(id => gpuCondition(state, id)?.clockFactor ?? 1);
  const stragglerFactor = rankFactors.length ? Math.min(...rankFactors) : 1;
  const halt = sum(plan.train) !== trainingGpus ? 'world-size'
    : atlasGpus.some(id => gpuCondition(state, id)?.failed) ? 'rank-failed'
      : operations.restarting ? 'restarting' : null;
  const trainingRate = halt ? 0
    : sum(plan.train) * 63 * (.72 + .28 * plan.cap) * trainScale * network * stragglerFactor * (spread > 2 ? .97 : 1);
  const servingCapacity = sum(racks.map((rack, i) => jobs[i].filter((job, slot) => job === 'job-relay' &&
    !gpuCondition(state, gpuId(floor[i], slot))?.failed).length * inferenceCapacity * rack.scale));
  const served = Math.min(state.demand, servingCapacity);
  const pressure = servingCapacity ? state.demand / servingCapacity : Infinity;
  const latency = servingCapacity ? Math.min(610, 33 + 25 / Math.max(.045, 1 - pressure)) : null;
  const draw = plantPower + sum(racks.map(rack => rack.draw));
  const requestedDraw = plantPower + sum(racks.map(rack => rack.requestedDraw));
  return { racks, network, draw, requestedDraw, trainingRate, served, servingCapacity, latency,
    plan, jobs, atlasGpus, stragglerFactor, halt };
}

function constraintChecks(state, plan, output) {
  return [
    { id: 'training', label: '12 training ranks preserved', ok: sum(output.plan.train) === trainingGpus },
    { id: 'inference', label: 'Inference P95 within 160 ms', ok: output.latency !== null && output.latency <= inferenceSlo },
    { id: 'site-power', label: 'Requested site power within budget', ok: output.requestedDraw <= state.powerLimit + 1e-8 },
    { id: 'rack-power', label: 'Requested rack power within limit', ok: output.racks.every(r => r.requestedDraw <= rackPowerLimit + 1e-8) },
    { id: 'temperature', label: 'Loaded racks within 82°C limit', ok: output.racks.every(r => r.requestedTemperature <= rackTemperatureLimit + 1e-8) },
    { id: 'freshness', label: 'Cooling evidence fresh on loaded racks', ok: floor.every((r, i) => r.id !== state.staleRack || !(output.plan.train[i] + output.plan.infer[i])) },
  ];
}

const orders = floor.flatMap((_, a) => floor.flatMap((_, b) => floor.flatMap((_, c) =>
  a !== b && a !== c && b !== c ? [[a, b, c, 6 - a - b - c]] : [])));
const trainingLayouts = [...new Map([
  [6, 6, 0, 0], [8, 4, 0, 0], [4, 4, 4, 0], [3, 3, 3, 3],
].flatMap(shape => orders.map(order => order.map(i => shape[i]))).map(layout => [layout.join(), layout])).values()];

function propose(states) {
  const stress = states.reduce((worst, state) => state.demand > worst.demand ? state : worst, states[0]);
  const servingMinimum = Math.ceil(stress.demand / (inferenceCapacity * (1 - 25 / (inferenceSlo - 33))));
  const reference = baseline(states[0]);
  let considered = 0;
  let feasible = 0;
  let winner = null;
  const alternatives = new Set();
  for (const train of trainingLayouts) {
    for (const order of orders) {
      for (const requested of [servingMinimum, servingMinimum + 1]) {
        const infer = fillSlots(train, requested, order);
        for (const cap of [1, .92, .84, .76]) {
          const key = [train.join(), infer.join(), cap].join('/');
          if (alternatives.has(key)) continue;
          alternatives.add(key);
          considered++;
          const plan = { train, infer, cap };
          const stressed = plant(stress, plan);
          if (!constraintChecks(stress, plan, stressed).every(check => check.ok)) continue;
          let throughput = 0;
          let cost = 0;
          let qualifies = true;
          for (const state of states) {
            const result = state === stress ? stressed : plant(state, plan);
            if (!constraintChecks(state, plan, result).every(check => check.ok)) {
              qualifies = false;
              break;
            }
            throughput += result.trainingRate;
            cost += result.draw * state.price / 60;
          }
          if (!qualifies) continue;
          feasible++;
          const relocation = sum(train.map((count, i) => Math.max(0, reference.train[i] - count)));
          const servingRelocation = sum(infer.map((count, i) => Math.max(0, reference.infer[i] - count)));
          if (!winner || throughput > winner.throughput + 1e-6 ||
              (Math.abs(throughput - winner.throughput) < 1e-6 && (cost < winner.cost - 1e-6 ||
                (Math.abs(cost - winner.cost) < 1e-6 && (relocation < winner.relocation ||
                  (relocation === winner.relocation && servingRelocation < winner.servingRelocation)))))) {
            winner = { plan, throughput, cost, relocation, servingRelocation };
          }
        }
      }
    }
  }
  return { plan: winner?.plan ?? null, considered, feasible, relocation: winner?.relocation ?? null,
    servingRelocation: winner?.servingRelocation ?? null };
}

// Search placements for one minute's observed state with some GPUs withheld. This is a persistence
// forecast: it assumes the current minute's demand and limits hold, unlike `propose`, which sees the trace.
function searchPlacement({ scenario = 'network', seed = defaultSeed, tick = 0, control, reference } = {}) {
  validateInput(scenario, tick, seed);
  const operations = normalizeControl(control) ?? { ...noOperations, plan: null, cap: null };
  const state = environment(scenario, tick, seed);
  const current = reference ? validatePlan(reference) : baseline(state);
  const currentJobs = floor.map((_, i) => slotJobs(current, i));
  const servingMinimum = Math.ceil(state.demand / (inferenceCapacity * (1 - 25 / (inferenceSlo - 33))));
  let considered = 0;
  let feasible = 0;
  let winner = null;
  const seen = new Set();
  const free = floor.map(rack => capacity(gpuCount).filter(slot => !operations.unavailable.has(gpuId(rack, slot))).length);
  // Local repairs keep the current placement and move only the overflow, in each rack order.
  const repair = (train, order, wanted) => {
    const infer = current.infer.map((n, i) => Math.max(0, Math.min(n, free[i] - train[i])));
    let deficit = Math.max(wanted, sum(current.infer)) - sum(infer);
    for (const i of order) {
      const room = Math.max(0, free[i] - train[i] - infer[i]);
      const amount = Math.min(room, deficit);
      infer[i] += amount;
      deficit -= amount;
    }
    return infer;
  };
  const candidates = [];
  for (const train of [current.train, ...trainingLayouts]) {
    for (const order of orders) {
      for (const requested of [servingMinimum, servingMinimum + 1]) {
        candidates.push([train, fillSlots(train, requested, order)], [train, repair(train, order, requested)]);
      }
    }
  }
  for (const [train, infer] of candidates) {
    for (const cap of operations.cap ? [operations.cap] : powerCapSteps) {
      const plan = { train, infer, cap };
      const key = [train.join(), infer.join(), cap].join('/');
      if (seen.has(key)) continue;
      seen.add(key);
      considered++;
      const fitted = effectivePlan(plan, operations.unavailable);
      if (fitted.train.some((n, i) => n !== train[i]) || fitted.infer.some((n, i) => n !== infer[i])) continue;
      const output = plant(state, plan, operations);
      if (!constraintChecks(state, plan, output).every(check => check.ok) || output.halt) continue;
      feasible++;
      const moved = sum(output.jobs.map((slots, i) => slots.filter((job, slot) => job !== currentJobs[i][slot]).length));
      const atlasMoved = output.atlasGpus.filter(id => {
        const [rackIndex, slot] = [floor.findIndex(rack => id.startsWith(`gpu-${rack.id.slice(5)}-`)), Number(id.slice(-2)) - 1];
        return currentJobs[rackIndex][slot] !== 'job-atlas';
      }).length;
      const candidate = { plan, trainingRate: output.trainingRate, draw: output.draw, moved, atlasMoved };
      // Least disruption first: keep training ranks in place, then move as few GPUs as possible.
      const better = !winner || candidate.atlasMoved < winner.atlasMoved ||
        (candidate.atlasMoved === winner.atlasMoved && (candidate.moved < winner.moved ||
          (candidate.moved === winner.moved && (candidate.trainingRate > winner.trainingRate + 1e-6 ||
            (Math.abs(candidate.trainingRate - winner.trainingRate) <= 1e-6 && candidate.draw < winner.draw - 1e-6)))));
      if (better) winner = candidate;
    }
  }
  return { plan: winner?.plan ?? null, considered, feasible, gpusReassigned: winner?.moved ?? null,
    trainingRanksMoved: winner?.atlasMoved ?? null, forecast: 'persistence of the current minute' };
}

const experimentCache = new Map();

function runComparison(scenario = 'network', seed = defaultSeed) {
  validateInput(scenario, 0, seed);
  const key = `${scenario}/${seed}`;
  if (experimentCache.has(key)) return experimentCache.get(key);
  const states = capacity(duration).map(tick => environment(scenario, tick, seed));
  const proposal = propose(states);
  const base = summarize(states, baseline);
  const coordinated = proposal.plan ? summarize(states, () => proposal.plan) : null;
  const result = {
    schemaVersion, scenario, seed, minutes: duration,
    origin: 'simulated', search: { considered: proposal.considered, feasible: proposal.feasible },
    plan: proposal.plan,
    trainingRanksMoved: proposal.relocation,
    servingSlotsMoved: proposal.servingRelocation,
    constraints: proposal.plan ? constraintChecks(states[0], proposal.plan, plant(states[0], proposal.plan)) : [],
    baseline: base, coordinated,
  };
  if (experimentCache.size >= 16) experimentCache.delete(experimentCache.keys().next().value);
  experimentCache.set(key, result);
  return result;
}

function summarize(states, choosePlan) {
  const timeline = states.map(state => {
    const output = plant(state, choosePlan(state));
    return {
      minute: state.tick, samplesPerSecond: round(output.trainingRate), p95Ms: output.latency === null ? null : round(output.latency),
      tokensPerSecond: round(output.served), powerKw: round(output.draw), pricePerKwh: state.price,
      energyKwh: output.draw / 60,
    };
  });
  const energy = sum(timeline.map(step => step.energyKwh));
  const validLatency = timeline.filter(step => step.p95Ms !== null);
  const trainingSamples = sum(timeline.map(step => step.samplesPerSecond * 60));
  return {
    trainingSamplesPerSecond: round(mean(timeline.map(step => step.samplesPerSecond)), 1),
    trainingSamples: Math.round(trainingSamples),
    inferenceTokensPerSecond: round(mean(timeline.map(step => step.tokensPerSecond)), 1),
    averageP95Ms: validLatency.length ? round(mean(validLatency.map(step => step.p95Ms)), 1) : null,
    sloMetMinutes: timeline.filter(step => step.p95Ms !== null && step.p95Ms <= inferenceSlo).length,
    energyKwh: round(energy),
    cost: round(sum(timeline.map(step => step.energyKwh * step.pricePerKwh))),
    samplesPerKwh: Math.round(trainingSamples / energy),
    timeline,
  };
}

function activePlan(state, scenario, seed, policy, control) {
  const chosen = control?.plan ?? (policy === 'baseline' ? baseline(state) : runComparison(scenario, seed).plan);
  if (!chosen) throw new RangeError('No feasible placement is available');
  return control?.cap ? { ...chosen, cap: control.cap } : chosen;
}

function getSnapshot({ scenario = 'network', tick = 0, seed = defaultSeed, policy = 'baseline', control } = {}) {
  validateInput(scenario, tick, seed);
  if (!['baseline', 'coordinated'].includes(policy)) throw new RangeError('Unknown policy');
  const operations = normalizeControl(control);
  const state = environment(scenario, tick, seed);
  const plan = activePlan(state, scenario, seed, policy, operations);
  const output = plant(state, plan, operations ?? noOperations);
  const unavailable = operations?.unavailable ?? noOperations.unavailable;
  const cordoned = operations?.cordoned ?? noOperations.cordoned;
  const read = (value, unit, quality, source) => observed(value, unit, tick, quality, source);
  const racks = floor.map((rack, i) => {
    const reading = output.racks[i];
    const quality = rack.id === state.staleRack ? 'stale' : 'fresh';
    const gpus = capacity(gpuCount).map(slot => {
      const id = gpuId(rack, slot);
      const job = output.jobs[i][slot];
      const condition = gpuCondition(state, id);
      const atlasStopped = job === 'job-atlas' && output.halt;
      const waiting = job === 'job-atlas' && reading.errors > 0 && output.network < .98;
      const throttled = Boolean(job && condition?.clockFactor < .95);
      const busy = job && !condition?.failed && !atlasStopped;
      const activeUtil = (job === 'job-atlas' ? 93 : 79) * reading.scale * (waiting ? output.network : 1) *
        (job === 'job-atlas' ? output.stragglerFactor / (condition?.clockFactor ?? 1) : 1);
      const gpuQuality = job === null && rack.id === state.staleRack && slot === 7 ? 'missing' : quality;
      const status = gpuQuality !== 'fresh' ? 'unknown' : unavailable.has(id) ? 'drained' : condition?.failed ? 'failed'
        : atlasStopped ? 'stalled' : throttled ? 'throttled' : waiting ? 'waiting'
          : job === 'job-atlas' ? 'training' : job ? 'inference' : 'idle';
      return {
        ...entityById.get(id), job, status, cordoned: cordoned.has(id),
        utilization: read(busy ? activeUtil + (slot % 3 - 1) * 2 : 0, '%'),
        temperature: read(reading.temperature - (job ? 2 + slot % 3 : 7) + (condition?.hotspot ?? 0), '°C', gpuQuality),
        health: {
          smClock: read(busy ? nominalSmClockMhz * (condition?.clockFactor ?? 1) : 345, 'MHz', gpuQuality),
          nominalSmClockMhz,
          throttleReasons: gpuQuality === 'fresh' ? [...(condition?.throttle ?? [])] : null,
          eccCorrected: read(condition?.eccCorrected ?? 0, 'count', gpuQuality),
          eccUncorrected: read(condition?.eccUncorrected ?? 0, 'count', gpuQuality),
          remapPending: gpuQuality === 'fresh' ? Boolean(condition?.remapPending) : null,
          xid: gpuQuality === 'fresh' ? (condition?.xid ?? []).map(event => ({ ...event })) : null,
        },
      };
    });
    return {
      ...rack,
      status: quality !== 'fresh' ? 'unknown' : reading.errors ? 'degraded' : reading.scale < .98 ? 'derated' : 'nominal',
      draw: read(reading.draw, 'kW'), temperature: read(reading.temperature, '°C', quality),
      inlet: read(state.inlet[i], '°C', quality),
      derate: read(reading.scale, 'ratio', 'fresh', 'derived'),
      gpus,
    };
  });
  const links = floor.map((rack, i) => ({
    id: linkId(rack), label: `Aisle ${rack.label} uplink`, rackId: rack.id, from: 'sw-spine', to: leafId(rack),
    capacity: read(state.links[i].capacity, 'Gbps'), traffic: read(output.racks[i].traffic, 'Gbps'),
    errors: read(state.links[i].errors, 'errors/min'), ratedGbps: 200,
  }));
  const allocationEdges = racks.flatMap(rack => rack.gpus.filter(gpu => gpu.job).map(gpu => ({
    id: `assignment-${gpu.id}`, from: gpu.job, to: gpu.id, type: 'assigned-to',
  })));
  const checkpoint = checkpointAt(tick);
  const workloads = [
    { id: 'job-atlas', workloadType: 'training', worldSize: trainingGpus, gpus: output.atlasGpus,
      running: !output.halt, haltReason: output.halt, stragglerFactor: round(output.stragglerFactor, 3),
      checkpoint: { ...checkpoint, source: 'declared' } },
    { id: 'job-relay', workloadType: 'inference', gpus: racks.flatMap(rack => rack.gpus.filter(gpu => gpu.job === 'job-relay').map(gpu => gpu.id)),
      running: output.servingCapacity > 0, haltReason: null, checkpoint: null },
  ];
  return {
    schemaVersion, scenario, policy, seed, tick, timestamp: new Date(startedAt + minute * tick).toISOString(),
    origin: 'simulated', plan, effectivePlan: output.plan,
    control: operations ? { plan: operations.plan, cap: operations.cap, unavailable: [...unavailable], cordoned: [...cordoned],
      restarting: operations.restarting } : null,
    racks, links, workloads, entities, edges: [...physicalEdges, ...allocationEdges],
    constraints: constraintChecks(state, plan, output),
    metrics: {
      training: read(output.trainingRate, 'samples/s', 'fresh', 'derived'),
      p95: read(output.latency, 'ms', output.latency === null ? 'missing' : 'fresh', 'derived'),
      draw: read(output.draw, 'kW', 'fresh', 'derived'),
      requestedDraw: read(output.requestedDraw, 'kW', 'fresh', 'derived'),
      efficiency: read(output.trainingRate * 3600 / output.draw, 'samples/kWh', 'fresh', 'derived'),
      demand: read(state.demand, 'tokens/s'), served: read(output.served, 'tokens/s', 'fresh', 'derived'),
      servingCapacity: read(output.servingCapacity, 'tokens/s', 'fresh', 'derived'),
      siteBudget: read(state.powerLimit, 'kW', 'fresh', 'declared'),
      price: observed(state.price, '£/kWh', tick, 'fresh', 'simulated', 4), slo: read(inferenceSlo, 'ms', 'fresh', 'declared'),
      gpuHourCost: observed(gpuHourCost, '£/GPU-h', tick, 'fresh', 'declared', 2),
    },
  };
}

function traceIncident({ scenario = 'network', tick = 0, seed = defaultSeed, control } = {}) {
  const config = supportedScenario(scenario);
  const snapshot = getSnapshot({ scenario, tick, seed, control });
  const { metrics, racks, links } = snapshot;
  const link = links[1];
  const cooled = racks[3];
  const uncertain = racks[1];
  const allGpus = racks.flatMap(rack => rack.gpus);
  const xidGpu = allGpus.find(gpu => gpu.health.xid?.length);
  const slowGpu = allGpus.find(gpu => gpu.status === 'throttled');
  const memoryGpu = xidGpu ?? allGpus.find(gpu => gpu.id === 'gpu-a01-03');
  const thermalGpu = slowGpu ?? allGpus.find(gpu => gpu.id === 'gpu-a02-05');
  const readings = {
    'gpu-xid': [
      { label: 'A01-03 XID events', metric: observed(memoryGpu.health.xid?.length ?? 0, 'events', tick, memoryGpu.health.xid ? 'fresh' : 'stale', 'simulated', 0),
        reference: (memoryGpu.health.xid ?? []).map(event => `XID ${event.code}`).join(' · ') || 'None recorded', entityId: memoryGpu.id },
      { label: 'A01-03 correctable ECC', metric: memoryGpu.health.eccCorrected, reference: 'Lifetime count · rising', entityId: memoryGpu.id },
      { label: 'A01-03 uncorrectable ECC', metric: memoryGpu.health.eccUncorrected, reference: 'Any increment stops the job', entityId: memoryGpu.id },
    ],
    straggler: [
      { label: 'A02-05 SM clock', metric: thermalGpu.health.smClock, reference: `${number(nominalSmClockMhz)} MHz nominal`, entityId: thermalGpu.id },
      { label: 'A02-05 temperature', metric: thermalGpu.temperature, reference: 'Hardware thermal slowdown', entityId: thermalGpu.id },
      { label: 'Atlas training rate', metric: metrics.training, reference: 'Paced by the slowest rank', entityId: 'job-atlas' },
    ],
    network: [
      { label: 'A02 link capacity', metric: link.capacity, reference: '200 Gbps rated', entityId: link.id },
      { label: 'A02 link errors', metric: link.errors, reference: '0 errors/min on healthy links', entityId: link.id },
      { label: 'Atlas training rate', metric: metrics.training, reference: 'Synchronous training', entityId: 'job-atlas' },
    ],
    demand: [
      { label: 'Relay request demand', metric: metrics.demand, reference: 'Incoming token rate', entityId: 'job-relay' },
      { label: 'Relay latency', metric: metrics.p95, reference: '160 ms limit', entityId: 'job-relay' },
      { label: 'Serving capacity', metric: metrics.servingCapacity, reference: 'Theoretical pool capacity', entityId: 'job-relay' },
    ],
    power: [
      { label: 'Requested site power', metric: metrics.requestedDraw, reference: 'Before protection', entityId: 'grid-01' },
      { label: 'Site power budget', metric: metrics.siteBudget, reference: 'Hard facility limit', entityId: 'grid-01' },
      { label: 'Electricity price', metric: metrics.price, reference: 'Scenario input', entityId: 'grid-01' },
    ],
    thermal: [
      { label: 'B02 rack temperature', metric: cooled.temperature, reference: '82 °C limit', entityId: cooled.id },
      { label: 'B02 coolant inlet', metric: cooled.inlet, reference: 'Above nominal range', entityId: cooled.id },
      { label: 'A02 cooling telemetry', metric: uncertain.inlet, reference: 'Stale · excluded from placement', entityId: uncertain.id },
    ],
  };
  const exceeds = (metric, threshold) => metric.quality === 'fresh' && metric.value > threshold;
  const below = (metric, threshold) => metric.quality === 'fresh' && metric.value < threshold;
  const signals = [
    { id: 'fabric-fault', scope: 'Rack', entityId: link.id, active: below(link.capacity, 160) && exceeds(link.errors, 0) },
    { id: 'serving-slo', scope: 'Cluster', entityId: 'job-relay', active: exceeds(metrics.p95, inferenceSlo) },
    { id: 'site-power', scope: 'Site', entityId: 'grid-01', active: exceeds(metrics.requestedDraw, metrics.siteBudget.value) },
    { id: 'hot-rack', scope: 'Rack', entityId: cooled.id, active: exceeds(cooled.inlet, 39) && exceeds(cooled.temperature, 78) },
    { id: 'stale-cooling', scope: 'Rack', entityId: uncertain.id, active: uncertain.inlet.quality === 'stale' },
    { id: 'gpu-xid', scope: 'GPU', entityId: xidGpu?.id ?? null, active: Boolean(xidGpu) },
    { id: 'straggler', scope: 'Job', entityId: slowGpu?.id ?? null, active: Boolean(slowGpu?.job === 'job-atlas') },
  ];
  const primarySignals = { network: 'fabric-fault', demand: 'serving-slo', power: 'site-power', thermal: 'hot-rack',
    'gpu-xid': 'gpu-xid', straggler: 'straggler' };
  const detected = signals.some(signal => signal.id === primarySignals[scenario] && signal.active);
  return { schemaVersion, scenario, tick, origin: 'simulated', ...config, detected,
    signals: signals.filter(signal => signal.active), evidence: readings[scenario] };
}

function inspectEntity({ entityId, scenario = 'network', tick = 0, seed = defaultSeed, policy = 'baseline', control }) {
  const snapshot = getSnapshot({ scenario, tick, seed, policy, control });
  const entity = entityById.get(entityId);
  const link = snapshot.links.find(item => item.id === entityId);
  const connection = !entity && !link && snapshot.edges.find(edge => edge.id === entityId && ['power', 'cooling', 'fabric'].includes(edge.type));
  if (!entity && !link && !connection) throw new RangeError('Unknown entity');
  const rack = snapshot.racks.find(item => item.id === (entity?.rackId ?? connection?.to ?? entityId));
  const reading = entity?.type === 'gpu' ? rack.gpus.find(gpu => gpu.id === entityId)
    : entity?.type === 'rack' || connection ? rack ?? null : link ?? null;
  if (connection) {
    return {
      schemaVersion, scenario, tick, policy, origin: 'simulated', reading, related: [connection],
      entity: { id: connection.id, label: `${entityById.get(connection.from).label} → ${entityById.get(connection.to).label}`,
        type: 'connection', relationship: connection.type, rackId: rack?.id },
    };
  }
  const related = snapshot.edges.filter(edge => edge.from === entityId || edge.to === entityId);
  if (link) {
    related.push(...snapshot.edges.filter(edge => edge.from === link.to || edge.to === link.to));
  }
  return { schemaVersion, scenario, tick, policy, origin: 'simulated', entity: entity ?? { id: link.id, label: link.label, type: 'link' }, reading, related };
}

export { scenarioOptions, getSnapshot, inspectEntity, runComparison, traceIncident, schemaVersion, duration, defaultSeed, inferenceSlo, physicalEdges, entities, powerCapSteps, gpuHourCost, checkpointAt, trainingGpus, validatePlan, searchPlacement };
