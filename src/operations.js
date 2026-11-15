// Operations layer over the shared twin: GPU health triage, checkpoint-aware timing, straggler
// attribution, maintenance blast radius and the action gate that every proposed change must pass.
// Everything here is deterministic code. Model output enters only as a proposal for the gate.
import { checkpointAt, defaultSeed, duration, entities, getSnapshot, gpuHourCost, inferenceSlo, physicalEdges, powerCapSteps, scenarioOptions, searchPlacement, trainingGpus, validatePlan } from './twin.js';

const operationsSchemaVersion = '1.0.0';
const restartMinutes = 1; // Declared simulation assumption: time to relaunch Atlas from a checkpoint.
const verificationDelay = 2; // Minutes after execution before measured outcomes are compared.
const maxReassigned = 16; // Blast-radius ceiling for one action: half of the 32-GPU floor.
const maxProposals = 200;
const maxLedger = 2000;
const entityById = new Map(entities.map(entity => [entity.id, entity]));
const scenarioIds = new Set(scenarioOptions().map(item => item.id));
const round = (value, digits = 2) => Number(value.toFixed(digits));
const gpuFor = (snapshot, id) => snapshot.racks.flatMap(rack => rack.gpus).find(gpu => gpu.id === id) ?? null;
const rackOf = (snapshot, gpuId) => snapshot.racks.find(rack => rack.gpus.some(gpu => gpu.id === gpuId)) ?? null;
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const minuteIso = tick => new Date(Date.UTC(2026, 10, 14, 12) + tick * 60_000).toISOString();

// NVIDIA XID codes most often seen on datacenter GPUs, summarised from NVIDIA's public XID catalogue.
// Confirm against the catalogue for the installed driver branch before acting on real hardware.
const xidCatalogue = {
  13: { name: 'Graphics engine exception', cause: 'Usually an application fault such as an out-of-range access', urgency: 'monitor', hardware: false },
  31: { name: 'GPU memory page fault', cause: 'Usually an application illegal memory access', urgency: 'monitor', hardware: false },
  43: { name: 'GPU stopped processing', cause: 'Application-triggered fault; the GPU remains usable', urgency: 'monitor', hardware: false },
  45: { name: 'Preemptive cleanup', cause: 'Follows another error; read the preceding XID', urgency: 'monitor', hardware: false },
  48: { name: 'Double-bit (uncorrectable) ECC error', cause: 'HBM returned data the ECC could not correct', urgency: 'immediate', hardware: true },
  61: { name: 'Internal micro-controller breakpoint', cause: 'GPU firmware fault', urgency: 'next-checkpoint', hardware: true },
  62: { name: 'Internal micro-controller halt', cause: 'GPU firmware halted', urgency: 'immediate', hardware: true },
  63: { name: 'Row remapping / page retirement event', cause: 'A failing memory row was recorded; a GPU reset applies the remap', urgency: 'next-checkpoint', hardware: true },
  64: { name: 'Row remapping / page retirement failure', cause: 'The GPU could not record the remap; spare rows may be exhausted', urgency: 'immediate', hardware: true },
  74: { name: 'NVLink error', cause: 'Link training or data error on an NVLink connection', urgency: 'immediate', hardware: true },
  79: { name: 'GPU has fallen off the bus', cause: 'The GPU stopped responding on PCIe', urgency: 'immediate', hardware: true },
  92: { name: 'High single-bit ECC error rate', cause: 'Correctable errors are arriving faster than expected', urgency: 'monitor', hardware: true },
  94: { name: 'Contained ECC error', cause: 'An uncorrectable error was contained to one application', urgency: 'next-checkpoint', hardware: true },
  95: { name: 'Uncontained ECC error', cause: 'An uncorrectable error could not be contained; all work on the GPU is affected', urgency: 'immediate', hardware: true },
  119: { name: 'GSP RPC timeout', cause: 'The GPU System Processor stopped responding', urgency: 'next-checkpoint', hardware: false },
  120: { name: 'GSP error', cause: 'GPU System Processor fault', urgency: 'next-checkpoint', hardware: false },
};
const urgencyRank = { none: 0, monitor: 1, 'next-checkpoint': 2, immediate: 3 };

function decodeXid(code) {
  const entry = xidCatalogue[code];
  return entry ? { code, ...entry } : { code, name: 'Unlisted XID', cause: 'Not in the built-in catalogue; consult the driver documentation',
    urgency: 'monitor', hardware: null };
}

// Cost of interrupting Atlas at `tick`: work since the last checkpoint is lost, plus a relaunch.
function interruptionCost(snapshot, tick = snapshot.tick) {
  const atlas = snapshot.workloads.find(item => item.id === 'job-atlas');
  const { last } = checkpointAt(tick);
  const lostMinutes = tick - last;
  const gpuMinutes = atlas.worldSize * (lostMinutes + restartMinutes);
  const rate = snapshot.metrics.training.quality === 'fresh' ? snapshot.metrics.training.value : null;
  return {
    tick, at: minuteIso(tick), lastCheckpoint: last, minutesSinceCheckpoint: lostMinutes, restartMinutes,
    gpuMinutes, cost: round(gpuMinutes / 60 * gpuHourCost), currency: 'GBP',
    lostSamples: rate === null ? null : Math.round(rate * 60 * lostMinutes),
    atCheckpoint: lostMinutes === 0,
    basis: `${atlas.worldSize} GPUs × (${lostMinutes} min since checkpoint + ${restartMinutes} min relaunch) × £${gpuHourCost}/GPU-h (declared)`,
  };
}

// Compare acting now with acting at the next checkpoint. Immediate-urgency faults are never deferred.
function checkpointTiming(snapshot, urgency = 'next-checkpoint') {
  const { next } = checkpointAt(snapshot.tick);
  const now = interruptionCost(snapshot, snapshot.tick);
  const options = [{ label: 'Now', ...now }];
  if (next !== snapshot.tick && next < duration) options.push({ label: 'Next checkpoint', ...interruptionCost(snapshot, next) });
  const defer = urgency !== 'immediate' && options.length > 1;
  const chosen = defer ? options[1] : options[0];
  return {
    urgency, options, recommendedTick: chosen.tick, savings: round(now.cost - chosen.cost),
    reason: urgency === 'immediate' ? 'Fault is immediate: waiting for a checkpoint risks corrupted results or a crash.'
      : options.length === 1 ? (next === snapshot.tick ? 'A checkpoint completes this minute.' : 'No further checkpoint lands inside the replay window.')
        : `Waiting ${next - snapshot.tick} min for the checkpoint avoids discarding ${now.minutesSinceCheckpoint} min of Atlas progress.`,
  };
}

// Slot for the fine-tuned GPU health classifier: telemetry in, a per-GPU label out. Its verdict is
// evidence for triage, never a command. Labels are validated here; anything else is ignored.
const healthLabels = ['stable', 'warning', 'critical'];
function parseHealthVerdict(value) {
  if (!plain(value) || !healthLabels.includes(value.label)) return null;
  const confidence = typeof value.confidence === 'number' && value.confidence >= 0 && value.confidence <= 1 ? value.confidence : null;
  const model = nameOk(value.model) ? value.model : 'health-model';
  return { label: value.label, confidence, model };
}

function triageGpu(snapshot, gpuId, { verdict } = {}) {
  const gpu = gpuFor(snapshot, gpuId);
  if (!gpu) throw new RangeError('Unknown GPU');
  const { health } = gpu;
  const base = { schemaVersion: operationsSchemaVersion, gpuId, label: gpu.label, rackId: gpu.rackId, tick: snapshot.tick,
    job: gpu.job, status: gpu.status, origin: snapshot.origin };
  if (health.xid === null || health.eccUncorrected.quality !== 'fresh') {
    return { ...base, verdict: 'unknown', urgency: 'unknown', events: [], steps: [], rmaRecommended: null, proposals: [],
      reasons: ['GPU health telemetry is stale or missing. Health is unknown, not healthy; no action is proposed.'] };
  }
  const events = health.xid.map(event => ({ ...event, ...decodeXid(event.code) }));
  const counts = events.reduce((map, event) => map.set(event.code, (map.get(event.code) ?? 0) + 1), new Map());
  const reasons = [];
  let urgency = events.reduce((worst, event) => urgencyRank[event.urgency] > urgencyRank[worst] ? event.urgency : worst, 'none');
  const model = parseHealthVerdict(verdict);
  if (model?.label === 'critical' && urgencyRank[urgency] < urgencyRank['next-checkpoint']) urgency = 'next-checkpoint';
  if (model?.label === 'warning' && urgencyRank[urgency] < urgencyRank.monitor) urgency = 'monitor';
  if (model && model.label !== 'stable') reasons.push(`Health model (${model.model}) classifies the GPU as ${model.label}.`);
  if (health.eccUncorrected.value > 0) {
    urgency = 'immediate';
    reasons.push(`${health.eccUncorrected.value} uncorrectable ECC error recorded: results on this GPU can no longer be trusted.`);
  }
  if (health.remapPending) {
    if (urgencyRank[urgency] < urgencyRank['next-checkpoint']) urgency = 'next-checkpoint';
    reasons.push('A memory row remap is pending and only takes effect after a GPU reset, which needs the GPU idle.');
  }
  if (health.eccCorrected.value > 500) reasons.push(`${health.eccCorrected.value} correctable ECC errors: memory is degrading.`);
  for (const event of events.filter((item, i, all) => all.findIndex(other => other.code === item.code) === i)) {
    reasons.push(`XID ${event.code} × ${counts.get(event.code)} · ${event.name}: ${event.cause}.`);
  }
  const memoryHistory = (counts.get(63) ?? 0) + (counts.get(92) ?? 0) > 0;
  const rmaReasons = [
    counts.get(64) && 'Row remapping failed (XID 64)',
    (counts.get(48) || counts.get(95)) && memoryHistory && 'Uncorrectable error after earlier memory degradation',
    (counts.get(79) ?? 0) >= 2 && 'Repeatedly fell off the bus',
    (counts.get(74) ?? 0) >= 3 && 'Persistent NVLink errors',
  ].filter(Boolean);
  const timing = gpu.job === 'job-atlas' ? checkpointTiming(snapshot, urgency === 'immediate' ? 'immediate' : 'next-checkpoint') : null;
  const drainTick = timing ? timing.recommendedTick : snapshot.tick;
  const drained = gpu.status === 'drained';
  const steps = urgency === 'none' || urgency === 'monitor' ? [
    { id: 'monitor', label: 'Keep in service and watch the error trend', owner: 'platform' },
  ] : [
    { id: 'cordon', label: 'Cordon: stop scheduling new work on the GPU', owner: 'scheduler', done: gpu.cordoned || drained },
    { id: 'drain', label: drainTick === snapshot.tick ? 'Drain now and move its work to a spare GPU' : `Drain at the minute ${drainTick} checkpoint`, owner: 'scheduler', done: drained },
    { id: 'reset', label: health.remapPending ? 'Reset the GPU to apply the pending row remap' : 'Reset the GPU and rerun vendor diagnostics', owner: 'operator', done: false },
    ...(rmaReasons.length ? [{ id: 'rma', label: 'Open an RMA after diagnostics confirm the fault', owner: 'operator', done: false }] : []),
  ];
  const proposals = [];
  if (!drained && urgencyRank[urgency] >= urgencyRank['next-checkpoint']) {
    proposals.push({ type: 'drain_gpu', gpuId, notBeforeTick: drainTick,
      rationale: `Triage: ${urgency} · ${reasons[0] ?? 'hardware fault'}`.slice(0, 600) });
  }
  if (rmaReasons.length) proposals.push({ type: 'open_rma', gpuId, rationale: `Triage: ${rmaReasons.join('; ')}`.slice(0, 600) });
  return { ...base, verdict: urgency === 'none' ? 'healthy' : urgency === 'monitor' ? 'watch' : 'remove-from-service',
    urgency, events, reasons, steps, timing, rmaRecommended: rmaReasons.length > 0, rmaReasons, proposals, model,
    counters: { eccCorrected: health.eccCorrected, eccUncorrected: health.eccUncorrected, remapPending: health.remapPending,
      smClock: health.smClock, throttleReasons: health.throttleReasons } };
}

// `verdicts` maps GPU id to the health model's output, when a model is connected.
function triageFleet(snapshot, { verdicts = new Map() } = {}) {
  return snapshot.racks.flatMap(rack => rack.gpus)
    .filter(gpu => gpu.health.xid?.length || gpu.health.eccUncorrected.value > 0 || gpu.health.remapPending ||
      ['warning', 'critical'].includes(parseHealthVerdict(verdicts.get(gpu.id))?.label))
    .map(gpu => triageGpu(snapshot, gpu.id, { verdict: verdicts.get(gpu.id) }))
    .sort((a, b) => (urgencyRank[b.urgency] ?? 0) - (urgencyRank[a.urgency] ?? 0));
}

// Draft a vendor-neutral RMA ticket from recorded evidence. It is never sent from this platform.
function draftRmaTicket(snapshot, gpuId, history = []) {
  const triage = triageGpu(snapshot, gpuId);
  const rack = rackOf(snapshot, gpuId);
  return {
    schemaVersion: operationsSchemaVersion, status: 'draft-not-sent', origin: snapshot.origin,
    title: `RMA candidate · ${gpuId} · ${[...new Set(triage.events.map(event => `XID ${event.code}`))].join(', ') || 'ECC'}`,
    asset: { gpuId, rack: rack?.id ?? null, slot: entityById.get(gpuId)?.slot ?? null,
      serialNumber: null, note: 'Serial and part numbers are not in the twin; add them from the asset system.' },
    evidence: { events: triage.events.map(({ code, name, at }) => ({ code, name, at })), counters: triage.counters, rmaReasons: triage.rmaReasons },
    actionsTaken: history.map(entry => ({ at: entry.at, event: entry.event, detail: entry.detail })),
    requiredBeforeSubmission: ['Run the vendor field diagnostic suite and attach its log', 'Confirm the GPU is drained and powered down'],
  };
}

// Which Atlas rank sets the pace, and why. Speeds are relative to a healthy rank (1.0).
function attributeStragglers(snapshot) {
  const atlas = snapshot.workloads.find(item => item.id === 'job-atlas');
  const result = { schemaVersion: operationsSchemaVersion, tick: snapshot.tick, workloadId: 'job-atlas', origin: snapshot.origin };
  if (!atlas.running) {
    return { ...result, verdict: 'halted', haltReason: atlas.haltReason, ranks: [], limiting: [], throughputCost: null,
      summary: atlas.haltReason === 'rank-failed' ? 'Atlas is stopped: one of its ranks has failed.'
        : atlas.haltReason === 'world-size' ? `Atlas cannot run: fewer than ${trainingGpus} GPUs are available for its ranks.`
          : 'Atlas is relaunching from its last checkpoint.' };
  }
  const spread = new Set(atlas.gpus.map(id => rackOf(snapshot, id).id)).size;
  const ranks = atlas.gpus.map(id => {
    const gpu = gpuFor(snapshot, id);
    const rack = rackOf(snapshot, id);
    const link = snapshot.links.find(item => item.rackId === rack.id);
    const clock = gpu.health.smClock.quality === 'fresh' ? gpu.health.smClock.value / gpu.health.nominalSmClockMhz : null;
    const fabric = spread > 1 && link.capacity.quality === 'fresh' && link.traffic.quality === 'fresh'
      ? Math.min(1, link.capacity.value / Math.max(1, link.traffic.value)) * (link.errors.value > 0 ? .86 : 1) : spread > 1 ? null : 1;
    const derate = rack.derate.quality === 'fresh' ? rack.derate.value : null;
    const components = [
      { id: 'clock', layer: 'compute', factor: clock, entityId: id,
        cause: gpu.health.throttleReasons?.includes('hw_thermal_slowdown') ? 'GPU thermal slowdown' : 'Reduced SM clock' },
      { id: 'fabric', layer: 'fabric', factor: fabric, entityId: link.id,
        cause: link.errors.value > 0 ? `${link.label} errors and reduced bandwidth` : `${link.label} congestion` },
      { id: 'power', layer: 'power', factor: derate, entityId: rack.id, cause: `${rack.label} power or thermal derate` },
    ];
    const known = components.filter(item => item.factor !== null);
    const speed = known.reduce((product, item) => product * item.factor, 1);
    const worst = known.reduce((low, item) => item.factor < low.factor ? item : low, { factor: 1 });
    return { gpuId: id, rackId: rack.id, speed: round(speed, 3), unknown: components.filter(item => item.factor === null).map(item => item.id),
      cause: worst.factor < .99 ? { id: worst.id, layer: worst.layer, label: worst.cause, entityId: worst.entityId, factor: round(worst.factor, 3) } : null };
  });
  const pace = Math.min(...ranks.map(rank => rank.speed));
  const limiting = ranks.filter(rank => rank.speed <= pace * 1.01);
  const others = ranks.filter(rank => rank.speed > pace * 1.01);
  const unknown = ranks.filter(rank => rank.unknown.length);
  if (pace >= .99) {
    return { ...result, verdict: unknown.length ? 'uncertain' : 'balanced', ranks, limiting: [], pace: round(pace, 3), throughputCost: 0,
      summary: unknown.length ? `${unknown.length} ranks have stale telemetry, so a straggler cannot be ruled out.` : 'All Atlas ranks are running at full speed.' };
  }
  if (!others.length) {
    return { ...result, verdict: 'uniform', ranks, limiting, pace: round(pace, 3), throughputCost: null,
      summary: `Every rank is equally slowed (${Math.round((1 - pace) * 100)}%) · ${limiting[0].cause?.label ?? 'shared limit'}. No single straggler.` };
  }
  const potential = Math.min(...others.map(rank => rank.speed));
  const cost = 1 - pace / potential;
  const lead = limiting[0];
  const rate = snapshot.metrics.training.value;
  const group = limiting.length > 1 && limiting.every(rank => rank.cause?.entityId === lead.cause?.entityId);
  const subject = limiting.length === 1 ? entityById.get(lead.gpuId)?.label + ` (${lead.rackId.slice(5).toUpperCase()})`
    : `${limiting.length} ranks on ${[...new Set(limiting.map(rank => rank.rackId.slice(5).toUpperCase()))].join(', ')}`;
  const mitigation = lead.cause?.id === 'clock' && limiting.length === 1
    ? { type: 'drain_gpu', gpuId: lead.gpuId, notBeforeTick: checkpointTiming(snapshot).recommendedTick,
      rationale: `Straggler: ${lead.gpuId} runs at ${Math.round(lead.speed * 100)}% (${lead.cause.label}); swap its rank to a spare GPU.` }
    : null;
  return {
    ...result, verdict: 'straggler', ranks, limiting: limiting.map(rank => rank.gpuId), pace: round(pace, 3),
    throughputCost: round(cost, 3), lostSamplesPerSecond: round(rate / (1 - cost) - rate, 1),
    cause: lead.cause, grouped: group,
    summary: `Atlas is limited by ${subject} · ${lead.cause?.label ?? 'unknown cause'}, costing ~${Math.round(cost * 100)}% throughput.`,
    mitigation, timing: mitigation ? checkpointTiming(snapshot) : null,
  };
}

// Everything physically downstream of an entity, following power, cooling, fabric and containment edges.
function downstream(entityId) {
  const edge = physicalEdges.find(item => item.id === entityId && ['power', 'cooling', 'fabric'].includes(item.type));
  const start = edge ? edge.to : entityId;
  if (!edge && !entityById.has(entityId)) throw new RangeError('Unknown entity');
  const type = edge ? edge.type : entityById.get(entityId).type;
  if (['site', 'workload'].includes(type)) throw new RangeError('Choose a physical component, not a site or workload');
  const seen = new Set([start]);
  const queue = [start];
  while (queue.length) {
    const node = queue.shift();
    for (const next of physicalEdges.filter(item => item.from === node && ['power', 'cooling', 'fabric', 'contains'].includes(item.type))) {
      if (!seen.has(next.to)) {
        seen.add(next.to);
        queue.push(next.to);
      }
    }
  }
  return { start, kind: type, nodes: [...seen] };
}

function planMaintenance({ entityId, snapshot, control }) {
  const graph = downstream(entityId);
  const gpus = graph.nodes.filter(id => entityById.get(id)?.type === 'gpu');
  const rackIds = graph.nodes.filter(id => entityById.get(id)?.type === 'rack');
  const racks = snapshot.racks.filter(rack => rackIds.includes(rack.id) || rack.gpus.some(gpu => gpus.includes(gpu.id)));
  const label = entityById.get(entityId)?.label ?? snapshot.links.find(link => link.id === entityId)?.label ?? entityId;
  const affected = snapshot.racks.flatMap(rack => rack.gpus).filter(gpu => gpus.includes(gpu.id));
  const atlasRanks = affected.filter(gpu => gpu.job === 'job-atlas').length;
  const relayReplicas = affected.filter(gpu => gpu.job === 'job-relay').length;
  const singleFeeds = racks.flatMap(rack => ['power', 'cooling', 'fabric'].map(kind => {
    const feeds = physicalEdges.filter(item => item.to === rack.id && item.type === kind)
      .concat(kind === 'fabric' ? physicalEdges.filter(item => item.type === 'fabric' && item.to === `leaf-${rack.id.slice(5)}`) : []);
    return feeds.length <= (kind === 'fabric' ? 2 : 1) ? { rackId: rack.id, kind, via: feeds.map(item => item.from) } : null;
  }).filter(Boolean));
  const stale = racks.filter(rack => rack.temperature.quality !== 'fresh' || rack.inlet.quality !== 'fresh').map(rack => rack.id);
  const held = control?.unavailable ?? [];
  const withheld = [...new Set([...held, ...gpus])];
  const asIs = getSnapshot({ scenario: snapshot.scenario, seed: snapshot.seed, tick: snapshot.tick,
    control: { ...(control ?? {}), restarting: false, unavailable: withheld } });
  const newlyFailing = asIs.constraints.filter(check => !check.ok && snapshot.constraints.find(item => item.id === check.id)?.ok);
  const search = affected.some(gpu => gpu.job) ? searchPlacement({ scenario: snapshot.scenario, seed: snapshot.seed, tick: snapshot.tick,
    control: { unavailable: withheld, ...(control?.cap ? { cap: control.cap } : {}) }, reference: snapshot.effectivePlan }) : null;
  const timing = search?.trainingRanksMoved || atlasRanks ? checkpointTiming(snapshot) : null;
  const alreadyHeld = gpus.every(id => held.includes(id));
  const verdict = alreadyHeld ? 'already-held' : stale.length ? 'unknown' : !affected.some(gpu => gpu.job) ? 'safe-now'
    : search?.plan ? 'safe-with-evacuation' : 'unsafe';
  const notBeforeTick = timing?.recommendedTick ?? snapshot.tick;
  return {
    schemaVersion: operationsSchemaVersion, entityId, label, kind: graph.kind, tick: snapshot.tick, origin: snapshot.origin,
    verdict,
    summary: {
      'already-held': `All ${affected.length} downstream GPUs are already out of service. Roll back the hold in the gate to return them.`,
      'unknown': `Cooling or temperature telemetry is stale on ${stale.join(', ')}. Impact cannot be certified.`,
      'safe-now': `${affected.length} GPUs downstream and none are running work. Safe to take down now.`,
      'safe-with-evacuation': `${atlasRanks} Atlas ranks and ${relayReplicas} Relay replicas must move first. A placement that keeps every limit exists.`,
      'unsafe': `No placement keeps Atlas and Relay within limits without these ${affected.length} GPUs. Defer, or add capacity elsewhere.`,
    }[verdict],
    affected: { gpus: affected.map(gpu => ({ id: gpu.id, job: gpu.job, status: gpu.status })), racks: racks.map(rack => rack.id),
      atlasRanks, relayReplicas, atlasStops: atlasRanks > 0 },
    redundancy: { singlePointsOfFailure: singleFeeds,
      note: singleFeeds.length ? 'No alternate power, cooling or fabric path is declared for these racks, so they are treated as single-fed.' : 'Alternate paths are declared.' },
    unprepared: { constraints: asIs.constraints, newlyFailing: newlyFailing.map(check => check.label),
      trainingRate: asIs.metrics.training, p95: asIs.metrics.p95, haltReason: asIs.workloads[0].haltReason },
    evacuation: search ? { plan: search.plan, gpusReassigned: search.gpusReassigned, trainingRanksMoved: search.trainingRanksMoved,
      considered: search.considered, feasible: search.feasible, forecast: search.forecast } : null,
    timing,
    proposal: verdict === 'safe-with-evacuation' || verdict === 'safe-now'
      ? { type: 'maintenance_hold', entityId, ...(search?.plan ? { plan: search.plan } : {}), notBeforeTick,
        rationale: `Maintenance on ${label}: evacuate ${affected.filter(gpu => gpu.job).length} busy GPUs and hold ${affected.length} out of service.` }
      : null,
  };
}

// ---------------------------------------------------------------------------------------------
// Action gate: observe → propose → simulate → validate → authorize → execute → verify → keep/roll back.

const controls = {
  placement: { interface: 'scheduler', label: 'Workload placement', risk: 'medium' },
  power_cap: { interface: 'gpu-driver power limit', label: 'Training GPU power setting', risk: 'medium' },
  drain_gpu: { interface: 'scheduler', label: 'Cordon and drain a GPU', risk: 'medium' },
  open_rma: { interface: 'ticketing (draft only)', label: 'Draft an RMA ticket', risk: 'low' },
  maintenance_hold: { interface: 'scheduler', label: 'Evacuate and hold for maintenance', risk: 'high' },
};
// Visible in telemetry, but no supported, certified control interface is connected.
const observedOnly = {
  set_cdu_flow: 'Coolant flow is owned by the CDU controller.',
  set_fan_speed: 'Fan curves are owned by the BMC and facility controls.',
  power_cycle_pdu: 'PDU outlets are owned by electrical operations.',
  set_gpu_clock: 'Clock locking is not exposed as a supported control here.',
  reroute_fabric: 'Fabric routing is owned by the network controller.',
  reset_gpu: 'GPU resets are performed by an operator on the host after draining.',
};
const sourceKinds = ['operator', 'optimizer', 'triage', 'planner', 'straggler', 'model'];
const nameOk = value => typeof value === 'string' && value.length >= 1 && value.length <= 60 && /^[A-Za-z0-9 ._:/-]+$/.test(value);

function parseProposal(input) {
  const fail = message => ({ ok: false, message });
  if (!plain(input)) return fail('Proposal must be an object');
  let size;
  try {
    size = JSON.stringify(input).length;
  } catch {
    return fail('Proposal is not serialisable');
  }
  if (size > 4000) return fail('Proposal is larger than 4 000 characters');
  const { type } = input;
  if (typeof type !== 'string' || type.length > 40) return fail('Proposal needs a type');
  if (Object.hasOwn(observedOnly, type)) return fail(`Not a supported control: ${observedOnly[type]}`);
  if (!Object.hasOwn(controls, type)) return fail(`Unknown action type “${type.replace(/[^\w-]/g, '')}”`);
  const fields = { placement: ['plan'], power_cap: ['cap'], drain_gpu: ['gpuId'], open_rma: ['gpuId'], maintenance_hold: ['entityId', 'plan'] }[type];
  const allowed = new Set(['type', 'notBeforeTick', 'rationale', 'claimedEffect', ...fields]);
  const extra = Object.keys(input).find(key => !allowed.has(key));
  if (extra) return fail(`Unexpected field “${extra.replace(/[^\w-]/g, '').slice(0, 30)}”`);
  const out = { type };
  if (input.notBeforeTick !== undefined) {
    if (!Number.isInteger(input.notBeforeTick) || input.notBeforeTick < 0 || input.notBeforeTick >= duration) return fail('notBeforeTick must be a replay minute');
    out.notBeforeTick = input.notBeforeTick;
  }
  if (input.rationale !== undefined) {
    if (typeof input.rationale !== 'string' || input.rationale.length > 600) return fail('Rationale must be text up to 600 characters');
    out.rationale = input.rationale;
  }
  if (input.claimedEffect !== undefined) {
    if (!plain(input.claimedEffect)) return fail('claimedEffect must be an object');
    const keys = ['trainingSamplesPerSecond', 'p95Ms', 'powerKw'];
    const claim = {};
    for (const [key, value] of Object.entries(input.claimedEffect)) {
      if (!keys.includes(key) || typeof value !== 'number' || !Number.isFinite(value) || Math.abs(value) > 1e6) return fail('claimedEffect accepts finite trainingSamplesPerSecond, p95Ms and powerKw');
      claim[key] = value;
    }
    out.claimedEffect = claim;
  }
  try {
    if (fields.includes('plan')) {
      if (input.plan === undefined && type === 'maintenance_hold') out.plan = null;
      else out.plan = structuredClone(validatePlan(input.plan));
    }
  } catch (cause) {
    return fail(cause instanceof RangeError ? cause.message : 'Invalid plan');
  }
  if (fields.includes('cap')) {
    if (!powerCapSteps.includes(input.cap)) return fail(`cap must be one of ${powerCapSteps.join(', ')}`);
    out.cap = input.cap;
  }
  if (fields.includes('gpuId')) {
    if (typeof input.gpuId !== 'string' || entityById.get(input.gpuId)?.type !== 'gpu') return fail('gpuId must name a GPU in the twin');
    out.gpuId = input.gpuId;
  }
  if (fields.includes('entityId')) {
    if (typeof input.entityId !== 'string' || input.entityId.length > 60) return fail('entityId must name a component');
    try {
      downstream(input.entityId);
    } catch (cause) {
      return fail(cause instanceof RangeError ? cause.message : 'Invalid entity');
    }
    out.entityId = input.entityId;
  }
  return { ok: true, proposal: out };
}

function parseSource(source) {
  if (!plain(source) || !sourceKinds.includes(source.kind) || !nameOk(source.name)) throw new RangeError('Source needs a known kind and a short name');
  return { kind: source.kind, name: source.name };
}

function createActionGate({ scenario = 'network', seed = defaultSeed } = {}) {
  if (!scenarioIds.has(scenario)) throw new RangeError('Unknown scenario');
  if (!Number.isInteger(seed) || seed < 0 || seed > 9999) throw new RangeError('Seed must be from 0 to 9999');
  let now = 0;
  let killSwitch = false;
  let sequence = 0;
  const deltas = []; // { seq, actionId, fromTick, untilTick, op, value }
  const proposals = [];
  const ledger = [];
  const restartCache = new Map();

  const record = (tick, actor, event, proposalId, detail) => {
    if (ledger.length >= maxLedger) throw new RangeError('Ledger is full; export and start a new session');
    const entry = { seq: ledger.length + 1, tick, at: minuteIso(tick), actor, event, proposalId, detail };
    ledger.push(entry);
    return entry;
  };

  function fold(tick, { exclude = null, extra = [] } = {}) {
    const control = { plan: null, cap: null, unavailable: new Set(), cordoned: new Set() };
    for (const delta of [...deltas, ...extra].sort((a, b) => a.seq - b.seq)) {
      if (delta.actionId === exclude || delta.fromTick > tick || (delta.untilTick !== null && delta.untilTick <= tick)) continue;
      if (delta.op === 'plan') control.plan = delta.value;
      if (delta.op === 'cap') control.cap = delta.value;
      if (delta.op === 'hold') for (const id of delta.value) control.unavailable.add(id);
      if (delta.op === 'cordon') for (const id of delta.value) control.cordoned.add(id);
      if (delta.op === 'uncordon') for (const id of delta.value) control.cordoned.delete(id);
    }
    for (const id of control.unavailable) control.cordoned.delete(id);
    return { plan: control.plan, cap: control.cap, unavailable: [...control.unavailable], cordoned: [...control.cordoned] };
  }

  const atlasSet = (tick, control) => getSnapshot({ scenario, seed, tick, control: { ...control, restarting: false } })
    .workloads[0].gpus.join();

  function controlAt(tick, options = {}) {
    const control = fold(tick, options);
    let restarting = false;
    if (tick > 0 && (deltas.length || options.extra?.length)) {
      const key = `${tick}/${JSON.stringify(control)}/${JSON.stringify(fold(tick - 1, options))}`;
      if (!restartCache.has(key)) {
        if (restartCache.size > 512) restartCache.clear();
        restartCache.set(key, atlasSet(tick, control) !== atlasSet(tick - 1, fold(tick - 1, options)));
      }
      restarting = restartCache.get(key);
    }
    return { ...control, restarting };
  }

  const snapshotAt = (tick, options) => getSnapshot({ scenario, seed, tick, control: controlAt(tick, options) });

  // Translate a validated proposal into control deltas, resolving any planner-provided placement.
  function deltasFor(proposal, effectiveTick, approvalTick, seq) {
    const base = { seq, actionId: proposal.id, untilTick: null };
    if (proposal.type === 'placement') return [{ ...base, fromTick: effectiveTick, op: 'plan', value: proposal.plan }];
    if (proposal.type === 'power_cap') return [{ ...base, fromTick: effectiveTick, op: 'cap', value: proposal.cap }];
    if (proposal.type === 'open_rma') return [];
    const gpus = proposal.type === 'drain_gpu' ? [proposal.gpuId]
      : downstream(proposal.entityId).nodes.filter(id => entityById.get(id)?.type === 'gpu');
    const list = [];
    if (effectiveTick > approvalTick) list.push({ ...base, fromTick: approvalTick, op: 'cordon', value: gpus });
    if (proposal.plan) list.push({ ...base, fromTick: effectiveTick, op: 'plan', value: proposal.plan });
    list.push({ ...base, fromTick: effectiveTick, op: 'hold', value: gpus });
    return list;
  }

  const touchedGpus = proposal => proposal.type === 'drain_gpu' || proposal.type === 'open_rma' ? [proposal.gpuId]
    : proposal.type === 'maintenance_hold' ? downstream(proposal.entityId).nodes.filter(id => entityById.get(id)?.type === 'gpu') : [];

  // Simulate the proposal in the twin and run every deterministic check. Nothing is applied here.
  function evaluate(proposal, tick) {
    const effectiveTick = Math.max(tick, proposal.notBeforeTick ?? tick);
    const before = snapshotAt(tick);
    const probe = deltasFor(proposal, tick, tick, Number.MAX_SAFE_INTEGER).map(delta => ({ ...delta, fromTick: tick }));
    const forecastControl = { ...fold(tick, { extra: probe }), restarting: false };
    const after = getSnapshot({ scenario, seed, tick, control: forecastControl });
    const checks = [];
    const add = (id, label, ok, detail, severity = 'block') => checks.push({ id, label, ok, detail, severity: ok ? 'pass' : severity });
    add('kill-switch', 'Kill switch released', !killSwitch, killSwitch ? 'Automation is halted by an operator.' : 'Automation enabled.');
    const control = controls[proposal.type];
    add('capability', 'Supported control interface', true, `${control.label} via ${control.interface}.`);
    add('timing', 'Scheduled inside the replay window', effectiveTick < duration, `Effective at minute ${effectiveTick}.`);
    const gpus = touchedGpus(proposal);
    const jobsBefore = before.racks.flatMap(rack => rack.gpus.map(gpu => gpu.job));
    const jobsAfter = after.racks.flatMap(rack => rack.gpus.map(gpu => gpu.job));
    const reassigned = jobsBefore.filter((job, i) => job !== jobsAfter[i]).length;
    const changedRacks = before.racks.filter((rack, i) => rack.gpus.some((gpu, slot) => gpu.job !== after.racks[i].gpus[slot].job)).map(rack => rack.id);
    const rackIds = new Set([...gpus.map(id => entityById.get(id).rackId), ...changedRacks]);
    const stale = before.racks.filter(rack => rackIds.has(rack.id) && (rack.temperature.quality !== 'fresh' || rack.inlet.quality !== 'fresh'));
    const staleGpus = gpus.filter(id => gpuFor(before, id)?.health.eccUncorrected.quality !== 'fresh');
    add('freshness', 'Critical telemetry fresh', !stale.length && !staleGpus.length,
      stale.length || staleGpus.length ? `Stale or missing: ${[...stale.map(rack => rack.id), ...staleGpus].join(', ')}.` : 'All affected racks and GPUs report fresh data.');
    const held = new Set(fold(effectiveTick).unavailable);
    if (proposal.type === 'drain_gpu') add('dependency', 'GPU not already out of service', !held.has(proposal.gpuId), held.has(proposal.gpuId) ? 'Already drained.' : 'In service.');
    if (proposal.type === 'open_rma') add('dependency', 'GPU drained before RMA', held.has(proposal.gpuId) || new Set(fold(tick).unavailable).has(proposal.gpuId),
      'An RMA may only be drafted for a GPU that has been drained.');
    if (proposal.type === 'maintenance_hold') add('dependency', 'Component not already held', !gpus.every(id => held.has(id)), 'Hold applies to downstream GPUs.');
    add('blast-radius', `Blast radius ≤ ${maxReassigned} GPUs`, reassigned <= maxReassigned, `${reassigned} GPU assignments change.`);
    for (const check of after.constraints) {
      const was = before.constraints.find(item => item.id === check.id)?.ok;
      add(`twin-${check.id}`, check.label, check.ok || !was, check.ok ? 'Holds in simulation.' : was ? 'Would newly fail in simulation.' : 'Already failing; not made worse.',
        check.ok || !was ? 'warn' : 'block');
    }
    const atlasHalts = !after.workloads[0].running && before.workloads[0].running;
    add('atlas', 'Atlas keeps running', !atlasHalts, atlasHalts ? `Simulation stops Atlas (${after.workloads[0].haltReason}).` : 'Atlas world size preserved.');
    const atlasMoves = before.workloads[0].gpus.join() !== after.workloads[0].gpus.join();
    const interruption = atlasMoves ? interruptionCost(before, effectiveTick) : null;
    if (interruption) add('checkpoint', 'Atlas interrupted at a checkpoint', interruption.atCheckpoint,
      `Relaunch costs £${interruption.cost.toFixed(2)} (${interruption.minutesSinceCheckpoint} min since checkpoint).`, 'warn');
    const metric = snap => ({ trainingSamplesPerSecond: snap.metrics.training.value, p95Ms: snap.metrics.p95.value, powerKw: snap.metrics.draw.value });
    // Per-rack job counts before and after, so the change can be described in plain words.
    const count = (rack, job) => rack.gpus.filter(gpu => gpu.job === job).length;
    const racks = before.racks.map((rack, i) => ({ rack: rack.label, trainBefore: count(rack, 'job-atlas'), trainAfter: count(after.racks[i], 'job-atlas'),
      serveBefore: count(rack, 'job-relay'), serveAfter: count(after.racks[i], 'job-relay') }));
    const prediction = { before: metric(before), after: metric(after), forecast: 'persistence of the current minute', reassigned, atlasMoves, interruption, racks,
      constraints: Object.fromEntries(after.constraints.map(check => [check.id, check.ok])) };
    if (proposal.claimedEffect) {
      prediction.claimVsSimulation = Object.fromEntries(Object.entries(proposal.claimedEffect).map(([key, value]) => [key,
        { claimed: value, simulated: prediction.after[key], difference: prediction.after[key] === null ? null : round(value - prediction.after[key]) }]));
    }
    const blocking = checks.filter(check => check.severity === 'block');
    const risk = reassigned > 8 || proposal.type === 'maintenance_hold' ? 'high' : controls[proposal.type].risk;
    return { checks, blocking, prediction, effectiveTick, risk, touchedGpus: gpus, changedRacks: [...rackIds] };
  }

  const find = id => {
    const proposal = proposals.find(item => item.id === id);
    if (!proposal) throw new RangeError('Unknown proposal');
    return proposal;
  };
  const atTick = tick => {
    if (!Number.isInteger(tick) || tick < 0 || tick >= duration) throw new RangeError('Minute must be from 0 to 23');
    if (tick < now) throw new RangeError(`The gate clock is at minute ${now}; actions cannot be recorded in the past`);
    advance(tick);
    return tick;
  };
  const view = proposal => structuredClone(proposal);

  function submit(input, { tick = now, source } = {}) {
    const actor = parseSource(source);
    atTick(tick);
    if (proposals.length >= maxProposals) throw new RangeError('Proposal limit reached for this session');
    const id = `act-${String(proposals.length + 1).padStart(3, '0')}`;
    const parsed = parseProposal(input);
    const proposal = { id, source: actor, submittedAt: tick, status: 'rejected', history: [] };
    if (!parsed.ok) {
      Object.assign(proposal, { type: typeof input?.type === 'string' ? input.type.slice(0, 40).replace(/[^\w-]/g, '') : 'invalid',
        checks: [{ id: 'schema', label: 'Proposal schema valid', ok: false, detail: parsed.message, severity: 'block' }] });
      proposals.push(proposal);
      record(tick, actor, 'rejected', id, parsed.message);
      return view(proposal);
    }
    Object.assign(proposal, parsed.proposal);
    const evaluation = evaluate(parsed.proposal, tick);
    Object.assign(proposal, {
      checks: [{ id: 'schema', label: 'Proposal schema valid', ok: true, detail: 'Strict schema and limits passed.', severity: 'pass' }, ...evaluation.checks],
      prediction: evaluation.prediction, risk: evaluation.risk, effectiveTick: evaluation.effectiveTick, touchedGpus: evaluation.touchedGpus,
      changedRacks: evaluation.changedRacks,
      status: evaluation.blocking.length ? 'rejected' : 'awaiting-approval',
      approval: 'operator',
    });
    proposals.push(proposal);
    record(tick, actor, 'proposed', id, `${controls[proposal.type].label}${proposal.rationale ? ` · ${proposal.rationale.slice(0, 140)}` : ''}`);
    record(tick, { kind: 'system', name: 'gate' }, evaluation.blocking.length ? 'rejected' : 'validated', id,
      evaluation.blocking.length ? evaluation.blocking.map(check => `${check.label}: ${check.detail}`).join(' ') : `Simulated and validated · risk ${evaluation.risk} · awaiting operator approval`);
    return view(proposal);
  }

  function approve(id, { tick = now, operator } = {}) {
    if (!nameOk(operator)) throw new RangeError('Approval needs an operator name');
    atTick(tick);
    const proposal = find(id);
    if (proposal.status !== 'awaiting-approval') throw new RangeError(`Proposal is ${proposal.status}`);
    if (killSwitch) throw new RangeError('Kill switch engaged: approvals are blocked');
    const actor = { kind: 'operator', name: operator };
    const evaluation = evaluate(proposal, tick);
    if (evaluation.blocking.length) {
      proposal.status = 'rejected';
      proposal.checks = [proposal.checks[0], ...evaluation.checks];
      record(tick, { kind: 'system', name: 'gate' }, 'rejected', id, `Revalidation at approval failed: ${evaluation.blocking.map(check => check.label).join(', ')}`);
      return view(proposal);
    }
    proposal.prediction = evaluation.prediction;
    proposal.effectiveTick = evaluation.effectiveTick;
    proposal.changedRacks = evaluation.changedRacks;
    proposal.approvedBy = operator;
    proposal.approvedAt = tick;
    sequence += 1;
    deltas.push(...deltasFor(proposal, proposal.effectiveTick, tick, sequence));
    record(tick, actor, 'approved', id, `Effective at minute ${proposal.effectiveTick}`);
    if (proposal.type === 'open_rma') {
      proposal.status = 'recorded';
      proposal.ticket = draftRmaTicket(snapshotAt(tick), proposal.gpuId, ledger.filter(entry => proposals.find(item => item.id === entry.proposalId)?.gpuId === proposal.gpuId));
      record(tick, { kind: 'system', name: 'ticketing' }, 'recorded', id, 'RMA ticket drafted · not sent; attach diagnostics before submission');
      return view(proposal);
    }
    proposal.status = 'scheduled';
    proposal.verifyAt = Math.min(proposal.effectiveTick + verificationDelay, duration - 1);
    record(tick, { kind: 'system', name: 'gate' }, 'scheduled', id, proposal.effectiveTick > tick
      ? `Cordoned now; executes at minute ${proposal.effectiveTick}` : 'Executes this minute');
    advance(tick);
    return view(proposal);
  }

  function decline(id, { tick = now, operator, reason = 'Declined by operator' } = {}) {
    if (!nameOk(operator)) throw new RangeError('Declining needs an operator name');
    if (typeof reason !== 'string' || reason.length > 200) throw new RangeError('Reason must be short text');
    atTick(tick);
    const proposal = find(id);
    if (proposal.status !== 'awaiting-approval') throw new RangeError(`Proposal is ${proposal.status}`);
    proposal.status = 'declined';
    record(tick, { kind: 'operator', name: operator }, 'declined', id, reason);
    return view(proposal);
  }

  const activeActions = () => proposals.filter(item => ['scheduled', 'executed', 'verified', 'inconclusive'].includes(item.status))
    .sort((a, b) => b.approvedAt - a.approvedAt || b.id.localeCompare(a.id));

  function undo(proposal, tick, actor, reason) {
    const latest = activeActions().find(item => item.type !== 'open_rma');
    if (latest?.id !== proposal.id) return { ok: false, detail: `Roll back ${latest?.id ?? 'later actions'} first: rollbacks are last-in, first-out.` };
    const own = deltas.filter(delta => delta.actionId === proposal.id && (delta.untilTick === null || delta.untilTick > tick));
    const beforeUndo = snapshotAt(tick);
    const afterUndo = getSnapshot({ scenario, seed, tick, control: { ...fold(tick, { exclude: proposal.id }), restarting: false } });
    const regressions = afterUndo.constraints.filter(check => !check.ok && beforeUndo.constraints.find(item => item.id === check.id)?.ok);
    const halts = !afterUndo.workloads[0].running && beforeUndo.workloads[0].running;
    if (regressions.length || halts) {
      return { ok: false, detail: `Rollback would ${halts ? 'stop Atlas' : `break ${regressions.map(check => check.label).join(', ')}`}; operator decision required.` };
    }
    for (const delta of own) delta.untilTick = Math.max(tick, delta.fromTick);
    proposal.status = 'rolled-back';
    proposal.rolledBackAt = tick;
    record(tick, actor, 'rolled-back', proposal.id, reason);
    return { ok: true };
  }

  function rollback(id, { tick = now, operator } = {}) {
    if (!nameOk(operator)) throw new RangeError('Rollback needs an operator name');
    atTick(tick);
    const proposal = find(id);
    if (!['scheduled', 'executed', 'verified', 'inconclusive', 'needs-operator'].includes(proposal.status)) throw new RangeError(`Proposal is ${proposal.status}`);
    const result = undo(proposal, tick, { kind: 'operator', name: operator }, 'Rolled back by operator');
    if (!result.ok) throw new RangeError(result.detail);
    return view(proposal);
  }

  function verify(proposal, tick) {
    const measured = snapshotAt(tick);
    const gpus = proposal.touchedGpus ?? [];
    const findings = [];
    const rackIds = new Set(proposal.changedRacks ?? []);
    const stale = measured.racks.filter(rack => rackIds.has(rack.id) && rack.temperature.quality !== 'fresh').map(rack => rack.id);
    const predicted = proposal.prediction.after;
    const actual = { trainingSamplesPerSecond: measured.metrics.training.value, p95Ms: measured.metrics.p95.value, powerKw: measured.metrics.draw.value };
    proposal.measured = { tick, ...actual };
    if (stale.length || controlAt(tick).restarting) {
      // Retry next minute; log only when the reason changes so a long outage does not flood the ledger.
      const detail = stale.length ? `Telemetry stale on ${stale.join(', ')}; success is not claimed.` : 'Atlas is relaunching this minute; success is not claimed.';
      if (proposal.status !== 'inconclusive' || proposal.inconclusiveReason !== detail) {
        record(tick, { kind: 'system', name: 'verifier' }, 'inconclusive', proposal.id, detail);
      }
      proposal.status = 'inconclusive';
      proposal.inconclusiveReason = detail;
      proposal.verifyAt = tick + 1;
      return;
    }
    // Only limits the simulation predicted would hold count against the action; pre-existing failures do not.
    for (const check of measured.constraints) {
      if (!check.ok && proposal.prediction.constraints[check.id]) findings.push(`${check.label} failed`);
    }
    if (predicted.trainingSamplesPerSecond > 0 && actual.trainingSamplesPerSecond < predicted.trainingSamplesPerSecond * .9) {
      findings.push(`training ${actual.trainingSamplesPerSecond} samples/s vs ${predicted.trainingSamplesPerSecond} predicted`);
    }
    if (predicted.p95Ms !== null && predicted.p95Ms <= inferenceSlo && (actual.p95Ms === null || actual.p95Ms > inferenceSlo)) findings.push('Relay P95 above SLO');
    const busy = gpus.filter(id => gpuFor(measured, id)?.job);
    if (['drain_gpu', 'maintenance_hold'].includes(proposal.type) && busy.length) findings.push(`${busy.join(', ')} still running work`);
    // A concurrent hardware failure the action did not touch confounds the comparison: neither credit
    // nor blame the action, and leave the decision with the operator.
    const failed = measured.racks.flatMap(rack => rack.gpus).filter(gpu => gpu.status === 'failed' && !gpus.includes(gpu.id)).map(gpu => gpu.id);
    if (findings.length && failed.length && measured.workloads[0].haltReason === 'rank-failed') {
      proposal.status = 'inconclusive';
      proposal.confoundedBy = failed;
      proposal.verifyAt = Infinity; // Terminal until an operator acts: retrying cannot separate the two causes.
      record(tick, { kind: 'system', name: 'verifier' }, 'inconclusive', proposal.id,
        `Confounded: ${failed.join(', ')} failed independently and stopped Atlas. Not rolled back; success is not claimed.`);
      return;
    }
    if (!findings.length) {
      proposal.status = 'verified';
      record(tick, { kind: 'system', name: 'verifier' }, 'verified', proposal.id,
        `Measured ${actual.trainingSamplesPerSecond} samples/s (predicted ${predicted.trainingSamplesPerSecond}) · P95 ${actual.p95Ms ?? '—'} ms · kept`);
      return;
    }
    record(tick, { kind: 'system', name: 'verifier' }, 'verification-failed', proposal.id, findings.join('; '));
    const result = undo(proposal, tick, { kind: 'system', name: 'verifier' }, `Automatic rollback: ${findings.join('; ')}`);
    if (!result.ok) {
      proposal.status = 'needs-operator';
      record(tick, { kind: 'system', name: 'verifier' }, 'escalated', proposal.id, result.detail);
    }
  }

  function advance(tick) {
    if (!Number.isInteger(tick) || tick < 0 || tick >= duration) throw new RangeError('Minute must be from 0 to 23');
    if (tick < now) return [];
    const start = ledger.length;
    for (let minute = now; minute <= tick; minute++) {
      for (const proposal of proposals.filter(item => item.status === 'scheduled' && item.effectiveTick <= minute)) {
        proposal.status = 'executed';
        record(minute, { kind: 'system', name: proposal.type === 'power_cap' ? 'gpu-driver' : 'scheduler' }, 'executed', proposal.id,
          `${controls[proposal.type].label} applied in simulation`);
      }
      for (const proposal of proposals.filter(item => ['executed', 'inconclusive'].includes(item.status) && item.verifyAt <= minute)) {
        verify(proposal, minute);
      }
    }
    now = tick;
    return ledger.slice(start);
  }

  function setKillSwitch(engaged, { tick = now, operator } = {}) {
    if (typeof engaged !== 'boolean') throw new RangeError('Kill switch state must be true or false');
    if (!nameOk(operator)) throw new RangeError('Kill switch needs an operator name');
    atTick(tick);
    if (engaged === killSwitch) return state();
    killSwitch = engaged;
    record(tick, { kind: 'operator', name: operator }, engaged ? 'kill-switch-engaged' : 'kill-switch-released', null,
      engaged ? 'Approvals and scheduled actions halted' : 'Approvals re-enabled');
    if (engaged) {
      for (const proposal of proposals.filter(item => item.status === 'scheduled')) {
        for (const delta of deltas.filter(item => item.actionId === proposal.id && item.fromTick > tick)) delta.untilTick = delta.fromTick;
        proposal.status = 'cancelled';
        record(tick, { kind: 'system', name: 'gate' }, 'cancelled', proposal.id, 'Cancelled by kill switch before execution');
      }
    }
    return state();
  }

  function state() {
    return { schemaVersion: operationsSchemaVersion, scenario, seed, now, killSwitch, origin: 'simulated',
      control: controlAt(now), proposals: proposals.map(view), ledger: ledger.map(entry => ({ ...entry, actor: { ...entry.actor } })) };
  }

  const hasControl = () => deltas.some(delta => delta.untilTick === null || delta.untilTick > delta.fromTick);

  return { submit, approve, decline, rollback, advance, setKillSwitch, state, controlAt, snapshotAt, hasControl,
    get now() { return now; }, get killSwitch() { return killSwitch; } };
}

const gates = new Map();
function getActionGate(scenario = 'network', seed = defaultSeed) {
  const key = `${scenario}/${seed}`;
  if (!gates.has(key)) {
    if (gates.size >= 32) gates.delete(gates.keys().next().value);
    gates.set(key, createActionGate({ scenario, seed }));
  }
  return gates.get(key);
}
function resetActionGate(scenario = 'network', seed = defaultSeed) {
  gates.delete(`${scenario}/${seed}`);
  return getActionGate(scenario, seed);
}

export { parseHealthVerdict, operationsSchemaVersion, xidCatalogue, decodeXid, interruptionCost, checkpointTiming, triageGpu, triageFleet, draftRmaTicket, attributeStragglers, downstream, planMaintenance, parseProposal, createActionGate, getActionGate, resetActionGate, observedOnly };
