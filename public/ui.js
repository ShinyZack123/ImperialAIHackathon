import { scenarioOptions, getSnapshot, inspectEntity, runComparison, traceIncident, duration, defaultSeed, schemaVersion } from './twin.js';
import { invokeMcpTool, mcpToolCatalog } from './tool-catalog.js';

const byId = id => document.getElementById(id);
const scenarios = scenarioOptions();
const statusNames = {
  nominal: 'Nominal', degraded: 'Degraded', derated: 'Derated', unknown: 'Unknown',
  critical: 'Critical', warning: 'Warning',
  training: 'Training', inference: 'Inference', waiting: 'Waiting on fabric', idle: 'Idle',
};
const relationNames = {
  fabric: 'Fabric', cooling: 'Cooling', power: 'Power', contains: 'Contains',
  'assigned-to': 'Assigned to', 'gpu-peer': 'GPU peer',
};
const formatters = [0, 1, 2, 3].map(digits => new Intl.NumberFormat('en-GB', {
  maximumFractionDigits: digits, minimumFractionDigits: digits,
}));
const number = (value, digits = 0) => formatters[digits].format(value);
const qualityName = quality => quality === 'stale' ? 'Stale' : 'Missing';
const metricText = (metric, digits = 0) => metric.quality === 'fresh'
  ? `${number(metric.value, digits)} ${metric.unit}` : qualityName(metric.quality);
const clock = timestamp => timestamp.slice(11, 16);
const bracket = (value, min, max) => Math.min(Math.max(value, min), max);

const state = {
  view: 'overview', scenario: 'network', layer: 'fabric', tick: 0, policy: 'baseline',
  inspector: 'incident', selection: null, chartMetric: 'samplesPerSecond',
  compared: new Set(), moves: new Map(), timer: null,
};
let snapshot, benchmark, incident;
let activeSources = new Map();
let rackLinks = [];
let drag = null;
let suppressClick = false;
let frame = 0;
const racks = new Map();
const map = byId('map');
const wires = byId('wires');
const mapReadout = byId('map-readout');
const gpuTooltip = byId('gpu-tooltip');
const scenarioSelect = byId('scenario');
const range = byId('minute');
const experimentRoot = byId('experiment-results');

const profiles = {
  samplesPerSecond: { name: 'Training throughput', label: 'Samples/s', short: 'Training' },
  p95Ms: { name: 'Inference P95 latency', label: 'Milliseconds', short: 'Latency' },
  powerKw: { name: 'Site power draw', label: 'Kilowatts', short: 'Power' },
};

function refreshModel() {
  snapshot = getSnapshot({ scenario: state.scenario, tick: state.tick, policy: state.policy });
  benchmark = runComparison(state.scenario);
  incident = traceIncident({ scenario: state.scenario, tick: state.tick });
}

function canvasFrame(canvas) {
  if (!canvas?.clientWidth || !canvas.clientHeight) return null;
  const { clientWidth: width, clientHeight: height } = canvas;
  const scale = Math.min(window.devicePixelRatio || 1, 3);
  canvas.width = Math.round(width * scale);
  canvas.height = Math.round(height * scale);
  const context = canvas.getContext('2d');
  if (!context) return null;
  context.scale(scale, scale);
  return { context, width, height };
}

function stroke(context, points, color, lineWidth = 1, dash = []) {
  context.beginPath();
  context.moveTo(...points[0]);
  for (const point of points.slice(1)) context.lineTo(...point);
  context.strokeStyle = color;
  context.lineWidth = lineWidth;
  context.lineJoin = 'round';
  context.setLineDash(dash);
  context.stroke();
  context.setLineDash([]);
}

function scheduleCanvas() {
  cancelAnimationFrame(frame);
  frame = requestAnimationFrame(() => {
    if (state.view === 'overview') {
      drawMap();
      drawSparklines();
    } else drawChart();
  });
}

function renderMetrics() {
  const m = snapshot.metrics;
  const percent = m.draw.value / m.siteBudget.value * 100;
  const metrics = [
    { label: 'Training throughput', metric: m.training, short: 'samples/s', detail: 'Atlas · 12 training ranks', series: 'samplesPerSecond' },
    { label: 'Inference P95 latency', metric: m.p95, short: 'ms', detail: `SLO ≤ ${m.slo.value} ms`, attention: m.p95.value > m.slo.value, series: 'p95Ms' },
    { label: 'Facility power', metric: m.draw, digits: 1, short: 'kW', detail: `${number(percent)}% of ${number(m.siteBudget.value, 1)} kW budget`, attention: percent >= 99, series: 'powerKw' },
    { label: 'Energy productivity', metric: m.efficiency, digits: 1, multiplier: .001, short: 'k samples/kWh', detail: 'Atlas samples per kWh', series: 'efficiency' },
  ];
  byId('metrics').innerHTML = metrics.map(({ label, metric, digits = 0, multiplier = 1, short, detail, attention, series }) => `
    <div class="metric-cell">
      <h3>${label}</h3>
      <div class="metric-value">${metric.quality === 'fresh' ? number(metric.value * multiplier, digits) : '—'}<small>${short}</small></div>
      <canvas class="sparkline" data-series="${series}" data-attention="${Boolean(attention)}" aria-hidden="true"></canvas>
      <p class="metric-detail${attention ? ' attention' : ''}">${detail}</p>
    </div>`).join('');
}

function drawSparklines() {
  const timeline = benchmark[state.policy]?.timeline;
  if (!timeline) return;
  for (const canvas of document.querySelectorAll('.sparkline')) {
    const rect = canvasFrame(canvas);
    if (!rect) continue;
    const { context, width, height } = rect;
    const values = timeline.map(row => canvas.dataset.series === 'efficiency'
      ? row.samplesPerSecond * 3600 / row.powerKw : row[canvas.dataset.series]);
    if (values.some(value => value === null)) continue;
    const min = Math.min(...values);
    const spread = Math.max(...values) - min;
    const points = values.map((value, i) => [2 + i / (values.length - 1) * (width - 4),
      spread > .05 ? height - 4 - (value - min) / spread * (height - 8) : height / 2]);
    const color = canvas.dataset.attention === 'true' ? '#a74831' : '#578d73';
    context.beginPath();
    context.moveTo(points[0][0], height);
    for (const point of points) context.lineTo(...point);
    context.lineTo(points.at(-1)[0], height);
    context.fillStyle = canvas.dataset.attention === 'true' ? '#faece3' : '#eaf3eb';
    context.fill();
    stroke(context, points, color, 1.35);
    context.beginPath();
    context.arc(...points[state.tick], 2.4, 0, Math.PI * 2);
    context.fillStyle = color;
    context.fill();
  }
}

function makeRacks() {
  for (const rack of snapshot.racks) {
    const el = document.createElement('div');
    el.className = 'rack';
    el.dataset.rack = rack.id;
    el.innerHTML = `<button type="button" draggable="false" class="rack-header" data-entity="${rack.id}">
        <span><i class="rack-status-dot" aria-hidden="true"></i>Rack ${rack.label}</span><span class="rack-grip" aria-hidden="true">⠿</span>
      </button>
      <div class="slots">${rack.gpus.map(gpu => `<button type="button" class="gpu-slot" data-entity="${gpu.id}">${String(gpu.slot).padStart(2, '0')}</button>`).join('')}</div>
      <div class="rack-meta"></div>`;
    byId('racks').append(el);
    racks.set(rack.id, { el, header: el.firstElementChild, slots: [...el.querySelectorAll('.gpu-slot')], meta: el.lastElementChild });
  }
}

function renderRacks() {
  for (const rack of snapshot.racks) {
    const { el, header, slots, meta } = racks.get(rack.id);
    el.dataset.status = rack.status;
    header.setAttribute('aria-label', `Rack ${rack.label}, ${statusNames[rack.status]}`);
    header.title = `Inspect Rack ${rack.label} or drag to move`;
    meta.textContent = `${metricText(rack.draw, 1)} · ${metricText(rack.temperature)}`;
    rack.gpus.forEach((gpu, i) => {
      const button = slots[i];
      button.className = `gpu-slot ${gpu.status}`;
      button.setAttribute('aria-label', `${gpu.label}, rack ${rack.label}, ${statusNames[gpu.status]}`);
      button.dataset.tooltipStatus = statusNames[gpu.status];
      button.dataset.tooltipMetrics = `${metricText(gpu.utilization)} util · ${metricText(gpu.temperature)}`;
    });
  }
}

function source(id, label) {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'source-node';
  button.dataset.entity = id;
  button.innerHTML = `<span class="port-icon" aria-hidden="true">${'<i></i>'.repeat(6)}</span><span>${label}</span>`;
  byId('sources').append(button);
  activeSources.set(id, button);
}

function buildLayer() {
  activeSources = new Map();
  rackLinks = [];
  byId('sources').replaceChildren();
  byId('links').replaceChildren();

  if (state.layer === 'fabric') {
    source('sw-spine', 'Spine 01');
  } else if (state.layer === 'power') {
    source('grid-01', 'Site feed');
    source('pdu-a', 'PDU A');
    source('pdu-b', 'PDU B');
  } else {
    source('cdu-a', 'Cooling loop A');
    source('cdu-b', 'Cooling loop B');
  }

  snapshot.racks.forEach((rack, i) => {
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'link-node';
    button.dataset.entity = state.layer === 'fabric' ? snapshot.links[i].id : `${state.layer}-${rack.id}`;
    byId('links').append(button);
    rackLinks.push(button);
  });

  for (const button of byId('layers').querySelectorAll('button')) {
    button.setAttribute('aria-pressed', String(button.dataset.layer === state.layer));
  }
  if (!matchMedia('(prefers-reduced-motion: reduce)').matches) {
    byId('sources').animate([{ opacity: .45 }, { opacity: 1 }], { duration: 170 });
    byId('links').animate([{ opacity: .45 }, { opacity: 1 }], { duration: 170 });
  }
}

function renderLayer() {
  const { layer } = state;
  let readout;
  if (layer === 'fabric') {
    const impaired = snapshot.links.find(link => link.errors.value > 0);
    readout = impaired
      ? `A02 uplink · ${metricText(impaired.capacity, 1)} available of ${number(impaired.ratedGbps)} Gbps · ${metricText(impaired.errors)}`
      : 'Spine fabric · Four 200 Gbps rack uplinks';
  } else if (layer === 'power') {
    readout = `Site feed · ${metricText(snapshot.metrics.draw, 1)} used of ${metricText(snapshot.metrics.siteBudget, 1)}`;
  } else {
    const stale = snapshot.racks.find(rack => rack.inlet.quality !== 'fresh');
    readout = stale ? `Cooling loop A · ${stale.label} sensor ${stale.inlet.quality} · thermal headroom unknown`
      : 'Two cooling loops · 82°C rack temperature limit';
  }
  mapReadout.textContent = readout;

  snapshot.racks.forEach((rack, i) => {
    const link = snapshot.links[i];
    const button = rackLinks[i];
    button.classList.toggle('fault', layer === 'fabric' ? link.errors.value > 0 : layer === 'cooling' && rack.temperature.value >= 78);
    button.classList.toggle('unknown', layer === 'cooling' && rack.inlet.quality !== 'fresh');
    button.textContent = layer === 'fabric' ? metricText(link.capacity)
      : layer === 'power' ? metricText(rack.draw, 1) : metricText(rack.inlet);
    button.setAttribute('aria-label', `${rack.label} ${layer === 'fabric' ? 'uplink' : layer + ' connection'}, ${button.textContent}`);
  });
}

function placement(rack, i, width, height) {
  const compact = width < 650;
  const x = width * (compact ? (i % 2 ? .72 : .28) : .135 + i * .2433);
  const y = compact ? (i < 2 ? 149 : 299) : 144;
  const move = state.moves.get(rack.id) ?? { x: 0, y: 0 };
  const widthOfRack = compact ? Math.min(150, Math.floor(width * .405)) : Math.min(168, Math.floor(width * .207));
  const minimum = widthOfRack / 2 + 11;
  return {
    x: bracket(x + move.x, minimum, width - minimum),
    y: bracket(y + move.y, state.layer === 'power' ? 121 : 100, height - 120),
    widthOfRack, compact,
  };
}

function sizeMap() {
  const compact = map.clientWidth < 650;
  const size = compact ? '452px' : '288px';
  if (map.style.height !== size) map.style.height = size;
  scheduleCanvas();
}

function drawMap() {
  const rect = canvasFrame(wires);
  if (!rect) return;
  const { context, width, height } = rect;

  const setSource = (id, x, y) => {
    const el = activeSources.get(id);
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
    return { x, y: y + el.offsetHeight };
  };
  const sourceForRack = [];
  if (state.layer === 'fabric') {
    const spine = setSource('sw-spine', width / 2, compactTop(width));
    sourceForRack.push(spine, spine, spine, spine);
  } else {
    const power = state.layer === 'power';
    const left = setSource(power ? 'pdu-a' : 'cdu-a', width * .27, power ? 60 : compactTop(width));
    const right = setSource(power ? 'pdu-b' : 'cdu-b', width * .73, power ? 60 : compactTop(width));
    if (power) {
      const feed = setSource('grid-01', width / 2, 12);
      for (const source of [left, right]) {
        stroke(context, [[feed.x, feed.y], [feed.x, 54], [source.x, 54], [source.x, 62]], '#91a79a', 1.5);
      }
    }
    sourceForRack.push(left, left, right, right);
  }

  snapshot.racks.forEach((rack, i) => {
    const { x, y, widthOfRack, compact } = placement(rack, i, width, height);
    const { el } = racks.get(rack.id);
    el.style.setProperty('--rack-width', `${widthOfRack}px`);
    el.style.left = `${x}px`;
    el.style.top = `${y}px`;
    const link = rackLinks[i];
    const source = sourceForRack[i];
    const stem = Math.min(y - 13, source.y + 14);
    const badgeY = Math.max(stem + 4, y - 22);
    const selected = state.inspector === 'selection' && [rack.id, link.dataset.entity, ...rack.gpus.map(gpu => gpu.id)].includes(state.selection);
    const fault = link.classList.contains('fault');
    const stale = link.classList.contains('unknown');
    const color = fault ? '#bd694e' : stale ? '#86968d' : selected ? '#417c62' : '#9eb4a6';
    const bypassRow = compact && i >= 2;
    const lane = width * (i % 2 ? .975 : .025);
    const laneY = y - 45;
    const points = bypassRow ? [[source.x, source.y], [source.x, stem], [lane, stem], [lane, laneY], [x, laneY], [x, y]]
      : [[source.x, source.y], [source.x, stem], [x, stem], [x, y]];
    stroke(context, points, color, selected ? 2 : 1.35, fault || stale ? [4, 3] : []);
    link.style.left = `${x}px`;
    link.style.top = `${badgeY}px`;
  });
}

const compactTop = width => width < 650 ? 39 : 28;

function renderSelectionState() {
  for (const button of map.querySelectorAll('[data-entity]')) {
    const selected = state.inspector === 'selection' && state.selection === button.dataset.entity;
    button.classList.toggle('is-selected', selected);
    button.setAttribute('aria-pressed', String(selected));
  }
  for (const [id, { el }] of racks) el.classList.toggle('is-selected', state.inspector === 'selection' &&
    (state.selection === id || state.selection?.startsWith(`gpu-${id.slice(5)}-`)));
  for (const button of document.querySelectorAll('[data-inspector]')) {
    button.setAttribute('aria-pressed', String(button.dataset.inspector === state.inspector));
  }
}

function relatedLabel(id) {
  return snapshot.entities.find(item => item.id === id)?.label ?? id;
}

function inspectorRow(label, value, note, entityId = null) {
  const tag = entityId ? 'button' : 'div';
  return `<li><${tag} class="evidence-item"${entityId ? ` type="button" data-entity="${entityId}"` : ''}>
    <strong>${label}</strong><span class="mono">${value}</span><small>${note}</small>
  </${tag}></li>`;
}

function evidenceRows(rows) {
  return rows.map(({ label, metric, reference, entityId }) => {
    const digits = metric.unit === '£/kWh' ? 3 : ['kW', 'Gbps'].includes(metric.unit) ? 1 : 0;
    return inspectorRow(label, metricText(metric, digits), reference, entityId);
  }).join('');
}

function incidentInspector() {
  return `<div class="inspector-content">
    <span class="badge ${incident.detected ? incident.status : 'unknown'}">${incident.detected ? incident.status : 'Unconfirmed'}</span>
    <h2 class="inspector-title" id="inspector-title">${incident.incident}</h2>
    <p class="inspector-copy">${incident.summary}</p>
    <h3 class="evidence-heading">Observed in the simulation</h3>
    <ul class="evidence-list">${evidenceRows(incident.evidence)}</ul>
    <div class="response-box"><strong>Proposed response</strong><span>${incident.response}</span></div>
    <button type="button" class="primary-button" data-compare>Compare policies <span aria-hidden="true">→</span></button>
    ${state.policy === 'coordinated' ? '<p class="placement-note">Incident evidence from the baseline layout.</p>' : ''}
  </div>`;
}

function selectionRows(info) {
  const { entity, reading } = info;
  if (entity.type === 'gpu') return [
    ['Workload', reading.job ? relatedLabel(reading.job) : 'Unassigned', `Slot ${entity.slot} · ${relatedLabel(entity.rackId)}`],
    ['GPU utilization', metricText(reading.utilization), 'Simulated device counter'],
    ['Temperature', metricText(reading.temperature), reading.temperature.sampledAt ? `Last sample ${clock(reading.temperature.sampledAt)} UTC` : 'No temperature sample'],
  ];
  if (entity.type === 'rack') return [
    ['Rack power', metricText(reading.draw, 1), 'Includes cooling-independent rack base draw'],
    ['Rack temperature', metricText(reading.temperature), '82 °C limit'],
    ['Coolant inlet', metricText(reading.inlet, 1), reading.inlet.sampledAt ? `Last sample ${clock(reading.inlet.sampledAt)} UTC` : 'No coolant sample'],
  ];
  if (entity.type === 'link') return [
    ['Available bandwidth', metricText(reading.capacity, 1), `${reading.ratedGbps} Gbps nominal`],
    ['Requested traffic', metricText(reading.traffic, 1), 'Training collectives and serving traffic'],
    ['Link errors', metricText(reading.errors), 'Simulated receive errors'],
  ];
  if (entity.type === 'workload') {
    const rate = entity.id === 'job-atlas' ? snapshot.metrics.training : snapshot.metrics.served;
    return [['Useful output', metricText(rate), entity.workloadType],
      ['Allocated GPUs', number(info.related.length), 'Assigned slots across the facility'],
      ...(entity.id === 'job-relay' ? [['P95 latency', metricText(snapshot.metrics.p95), '160 ms SLO']] : [])];
  }
  if (entity.id === 'grid-01') return [
    ['Site power draw', metricText(snapshot.metrics.draw, 1), 'Includes facility overhead'],
    ['Site budget', metricText(snapshot.metrics.siteBudget, 1), 'Simulated hard limit'],
    ['Power requested', metricText(snapshot.metrics.requestedDraw, 1), 'Before simulated protection'],
  ];

  const directlyLinkedRack = reading?.id && snapshot.racks.find(rack => rack.id === reading.id);
  if (directlyLinkedRack && ['power', 'cooling'].includes(entity.relationship)) {
    const metric = entity.relationship === 'power' ? directlyLinkedRack.draw : directlyLinkedRack.inlet;
    return [[entity.relationship === 'power' ? 'Rack power' : 'Coolant inlet', metricText(metric, 1), `Supplies ${directlyLinkedRack.label}`]];
  }
  const rackIds = new Set(info.related.flatMap(edge => [edge.from, edge.to]).filter(id => id.startsWith('rack-')));
  if (entity.type === 'power') {
    const total = snapshot.racks.filter(rack => rackIds.has(rack.id)).reduce((kw, rack) => kw + rack.draw.value, 0);
    return [['Downstream rack draw', `${number(total, 1)} kW`, 'Rack loads on this distribution unit']];
  }
  if (entity.type === 'cooling') return snapshot.racks.filter(rack => rackIds.has(rack.id))
    .map(rack => [`${rack.label} inlet`, metricText(rack.inlet, 1), 'Simulated coolant temperature']);
  if (entity.type === 'switch') return [['Fabric connections', number(info.related.length), entity.id === 'sw-spine' ? 'Rack uplinks' : 'Rack and spine']];
  return [['Relationships', number(info.related.length), 'Physical infrastructure']];
}

function selectionInspector() {
  if (!state.selection) return `<div class="inspector-content"><h2 class="inspector-title" id="inspector-title">Inspect the map</h2>
    <p class="inspector-copy">Select a rack, GPU, or connection to see its telemetry and dependencies.</p></div>`;
  const info = inspectEntity({ entityId: state.selection, scenario: state.scenario, tick: state.tick, policy: state.policy });
  const { entity, reading, related } = info;
  const status = entity.type === 'link' ? reading.errors.value > 0 ? 'degraded' : 'nominal'
    : reading?.status ?? 'nominal';
  const rows = selectionRows(info).map(([label, value, note]) => inspectorRow(label, value, note)).join('');
  const endpoints = related.flatMap(edge => {
    const ids = ['connection', 'link'].includes(entity.type) ? [edge.from, edge.to]
      : [edge.from === entity.id ? edge.to : edge.from];
    return ids.map(id => ({ id, type: edge.type }));
  });
  const connectedIds = [...new Map(endpoints.map(item => [item.id + '/' + item.type, item])).values()];

  return `<div class="inspector-content">
    <span class="badge ${status}">${statusNames[status] ?? entity.type}</span>
    <h2 class="inspector-title" id="inspector-title">${entity.label}</h2>
    <p class="inspector-copy">${entity.rackId ? `${relatedLabel(entity.rackId)} · ` : ''}${reading ? 'Simulated telemetry' : 'Shared infrastructure graph'}</p>
    <h3 class="evidence-heading">Current readings</h3><ul class="evidence-list">${rows}</ul>
    <h3 class="evidence-heading">Relationships</h3>
    <ul class="relations">${connectedIds.slice(0, 8).map(({ id, type }) => `<li><button type="button" data-entity="${id}"><span>${relatedLabel(id)}</span><span class="mono">${relationNames[type] ?? type}</span></button></li>`).join('')}</ul>
    ${connectedIds.length > 8 ? `<p class="placement-note">${number(connectedIds.length - 8)} more relationships available via MCP.</p>` : ''}
    <button type="button" class="secondary-button" data-show-incident>Back to incident <span aria-hidden="true">↗</span></button>
  </div>`;
}

function renderInspector(restoreFocus = true) {
  const root = byId('inspector');
  const active = document.activeElement;
  let selector = null;
  if (restoreFocus && root.contains(active)) {
    if (active.dataset.entity) selector = `[data-entity="${CSS.escape(active.dataset.entity)}"]`;
    if (active.hasAttribute('data-compare')) selector = '[data-compare]';
    if (active.hasAttribute('data-show-incident')) selector = '[data-show-incident]';
  }
  root.innerHTML = state.inspector === 'incident' ? incidentInspector() : selectionInspector();
  if (selector) root.querySelector(selector)?.focus({ preventScroll: true });
  renderSelectionState();
}

function renderReplay() {
  range.value = state.tick;
  range.setAttribute('aria-valuetext', `${clock(snapshot.timestamp)} UTC, minute ${state.tick + 1} of ${duration}`);
  byId('replay-timestamp').textContent = `${clock(snapshot.timestamp)} UTC · ${state.tick + 1}/${duration}`;
  const policy = byId('policy');
  policy.hidden = !state.compared.has(state.scenario) || !benchmark.plan;
  policy.setAttribute('aria-pressed', String(state.policy === 'coordinated'));
  policy.textContent = state.policy === 'coordinated' ? 'View baseline layout' : 'View coordinated layout';
  byId('topology-summary').textContent = `4 racks · 32 GPUs · ${state.policy === 'coordinated' ? 'Coordinated' : 'Baseline'}`;
  byId('play').setAttribute('aria-label', state.timer ? 'Pause telemetry replay' : 'Play telemetry replay');
  byId('play').firstElementChild.classList.toggle('playing', Boolean(state.timer));
}

function renderOverview() {
  renderMetrics();
  renderRacks();
  renderLayer();
  renderReplay();
  renderInspector();
  sizeMap();
}

function formatDelta(previous, current, direction, digits, unit) {
  const difference = current - previous;
  const display = Number(difference.toFixed(digits));
  const sign = display > 0 ? '+' : display < 0 ? '−' : '';
  return { text: display ? `${sign}${number(Math.abs(display), digits)}${unit}` : '—',
    loss: (direction === 'up' && display < 0) || (direction === 'down' && display > 0) };
}

function renderResultRows(baseline, coordinated) {
  const rows = [
    ['Training throughput', 'trainingSamplesPerSecond', 1, ' /s', 'up'],
    ['Training samples completed', 'trainingSamples', 0, '', 'up'],
    ['Inference throughput', 'inferenceTokensPerSecond', 1, ' tok/s', 'up'],
    ['Mean minute P95', 'averageP95Ms', 1, ' ms', 'down'],
    ['Minutes within latency SLO', 'sloMetMinutes', 0, '', 'up'],
    ['Energy used', 'energyKwh', 2, ' kWh', 'down'],
    ['Electricity cost', 'cost', 2, '', 'down'],
  ];
  return rows.map(([label, field, digits, unit, direction]) => {
    const a = baseline[field], b = coordinated?.[field] ?? null;
    const delta = a === null || b === null ? { text: '—' } : formatDelta(a, b, direction, digits, field === 'cost' ? ' GBP' : unit);
    const show = value => value === null ? '—' : field === 'cost' ? `£${number(value, digits)}` : `${number(value, digits)}${unit}`;
    return `<tr><th scope="row">${label}</th><td>${show(a)}</td><td>${show(b)}</td><td class="${delta.loss ? 'loss' : ''}">${delta.text}</td></tr>`;
  }).join('');
}

function renderExperiment() {
  const { baseline, coordinated, constraints, plan, search, trainingRanksMoved, servingSlotsMoved } = benchmark;
  byId('experiment-meta').textContent = `${duration}-minute replay · Seed ${defaultSeed} · Simulated results`;
  byId('show-placement').disabled = !plan;
  experimentRoot.innerHTML = `<div class="experiment-grid">
    <section class="panel comparison-panel" aria-labelledby="comparison-title">
      <div class="panel-head chart-head"><div><h2 id="comparison-title">Policy comparison</h2><p>Same demand, hardware and fault traces</p></div>
        <div class="segment-control" role="group" aria-label="Chart metric">
          ${Object.entries(profiles).map(([id, metric]) => `<button type="button" data-chart="${id}" aria-pressed="${id === state.chartMetric}">${metric.short}</button>`).join('')}
        </div>
      </div>
      <figure class="chart-block" aria-label="Line chart of policy outcomes">
        <figcaption class="chart-key"><span><i></i> Baseline</span>${plan ? '<span><i class="coordinated"></i> Coordinated</span>' : ''}<span id="chart-limit" hidden><i class="limit"></i> <span id="chart-limit-text"></span></span></figcaption>
        <canvas class="chart" id="chart" role="img" aria-label="">
          Timelines are available in the exported JSON. The table below shows aggregate outcomes.
        </canvas>
      </figure>
      <table class="results-table"><caption>24-minute outcomes</caption>
        <thead><tr><th scope="col">Metric</th><th scope="col">Baseline</th><th scope="col">Coordinated</th><th scope="col">Change</th></tr></thead>
        <tbody>${renderResultRows(baseline, coordinated)}</tbody>
      </table>
    </section>
    <aside class="experiment-aside" aria-label="Candidate plan and validation">
      <section class="panel" aria-labelledby="plan-title">
        <h2 id="plan-title">Candidate placement</h2>
        <p class="inspector-copy">${plan ? `${number(trainingRanksMoved)} training ranks and ${number(servingSlotsMoved)} serving slots relocated at the start of this replay.`
          : 'No candidate satisfied every limit across the replay window.'}</p>
        ${plan ? `<div class="plan-grid" aria-label="Slots by rack">
            <span class="plan-head">Rack</span><span class="plan-head">Training</span><span class="plan-head">Serving</span>
            ${snapshot.racks.map((rack, i) => `<strong>${rack.label}</strong><span class="mono">${plan.train[i]}</span><span class="mono">${plan.infer[i]}</span>`).join('')}
          </div><p class="placement-note">Training power setting · ${number(plan.cap * 100)}%</p>` : ''}
      </section>
      <section class="panel" aria-labelledby="constraints-title">
        <h2 id="constraints-title">Constraint checks</h2>
        <p class="inspector-copy">${number(search.feasible)} of ${number(search.considered)} candidates passed every simulated minute.</p>
        ${plan ? `<ul class="constraint-list">${constraints.map(check => `<li><span class="${check.ok ? '' : 'fail'}" aria-label="${check.ok ? 'Passed' : 'Failed'}">${check.ok ? '✓' : '×'}</span>${check.label}</li>`).join('')}</ul>` : ''}
      </section>
      <section class="panel" aria-labelledby="method-title">
        <h2 id="method-title">Comparison method</h2>
        <p class="inspector-copy">The baseline keeps training ranks fixed and adds serving slots after demand crosses its target. The coordinated policy evaluates rack placement, serving capacity and training power together.</p>
      </section>
    </aside>
  </div>`;
  scheduleCanvas();
}

function drawChart() {
  const canvas = byId('chart');
  const rect = canvasFrame(canvas);
  if (!rect) return;
  const { context, width, height } = rect;
  const metric = profiles[state.chartMetric];
  const timeline = benchmark.baseline.timeline;
  const limit = state.chartMetric === 'p95Ms' ? snapshot.metrics.slo.value
    : state.chartMetric === 'powerKw' ? snapshot.metrics.siteBudget.value : null;
  byId('chart-limit').hidden = limit === null;
  byId('chart-limit-text').textContent = state.chartMetric === 'powerKw' ? 'Site budget' : '160 ms SLO';
  const plot = { left: 41, right: width - 12, top: 27, bottom: height - 28 };
  const data = [
    { timeline, color: '#a65c47', label: 'baseline' },
    { timeline: benchmark.coordinated?.timeline ?? [], color: '#28735a', label: 'coordinated' },
  ].filter(series => series.timeline.length);
  const values = data.flatMap(series => series.timeline.map(step => step[state.chartMetric])).filter(value => value !== null);
  if (limit !== null) values.push(limit);
  const minimum = Math.min(...values);
  const maximum = Math.max(...values);
  const floor = Math.max(0, minimum - Math.max((maximum - minimum) * .15, maximum * .02));
  const ceiling = maximum + Math.max((maximum - minimum) * .15, maximum * .02, 1);
  const x = i => plot.left + i / (duration - 1) * (plot.right - plot.left);
  const y = value => plot.bottom - (value - floor) / (ceiling - floor) * (plot.bottom - plot.top);
  const digits = state.chartMetric === 'powerKw' ? 1 : 0;

  context.font = '10px monospace';
  context.fillStyle = '#53635e';
  context.fillText(metric.label, plot.left, 16);
  for (let i = 0; i <= 4; i++) {
    const value = floor + i * (ceiling - floor) / 4;
    const pos = y(value);
    stroke(context, [[plot.left, pos], [plot.right, pos]], '#e1e8df');
    context.fillText(number(value, digits), 1, pos + 3);
  }
  context.textAlign = 'center';
  for (const minute of [0, 6, 12, 18, 23]) context.fillText(`+${minute}m`, x(minute), plot.bottom + 18);
  if (limit !== null) stroke(context, [[plot.left, y(limit)], [plot.right, y(limit)]], '#65746d', 1, [5, 3]);
  for (const series of data) {
    let points = [];
    for (const step of series.timeline) {
      const value = step[state.chartMetric];
      if (value === null) {
        if (points.length) stroke(context, points, series.color, 2.1);
        points = [];
      } else points.push([x(step.minute), y(value)]);
    }
    if (points.length) stroke(context, points, series.color, 2.1, series.label === 'baseline' ? [5, 3] : []);
  }
  const label = data.map(series => {
    const entries = series.timeline.map(step => step[state.chartMetric]).filter(value => value !== null);
    return entries.length ? `${series.label} ${number(Math.min(...entries), digits)} to ${number(Math.max(...entries), digits)}` : `${series.label} readings unavailable`;
  }).join(', ');
  canvas.setAttribute('aria-label', `${metric.name} over 24 minutes in ${metric.label}, ${label}`);
}

function stopReplay() {
  if (state.timer) clearInterval(state.timer);
  state.timer = null;
  if (snapshot) renderReplay();
}

function updateMinute(minute) {
  state.tick = minute;
  refreshModel();
  renderOverview();
}

function changeView() {
  const view = location.hash === '#experiments' ? 'experiments' : 'overview';
  state.view = view;
  stopReplay();
  byId('overview').hidden = view !== 'overview';
  byId('experiments').hidden = view !== 'experiments';
  const title = byId('page-title');
  title.textContent = view === 'overview' ? 'Infrastructure overview' : 'Policy experiments';
  byId('page-summary').textContent = view === 'overview'
    ? 'Trace workload performance across the compute, network and facility layers.'
    : 'Replay an incident under two scheduling policies and compare useful work, latency and energy.';
  for (const nav of document.querySelectorAll('[data-view]')) {
    if (nav.dataset.view === view) nav.setAttribute('aria-current', 'page');
    else nav.removeAttribute('aria-current');
  }
  if (view === 'experiments') {
    state.compared.add(state.scenario);
    renderExperiment();
  } else renderOverview();
  window.scrollTo(0, 0);
}

function openView(view) {
  if (location.hash === `#${view}`) changeView();
  else location.hash = view;
  byId('page-title').focus({ preventScroll: true });
}

function selectEntity(id) {
  state.inspector = 'selection';
  state.selection = id;
  renderInspector(false);
  scheduleCanvas();
}

function selectIncident() {
  state.inspector = 'incident';
  renderInspector(false);
  scheduleCanvas();
}

function navigateInspector(event) {
  const button = event.target.closest('button');
  if (!button) return;
  if (button.dataset.entity) {
    selectEntity(button.dataset.entity);
    byId('inspector-title').setAttribute('tabindex', '-1');
    byId('inspector-title').focus({ preventScroll: true });
  }
  if (button.hasAttribute('data-show-incident')) {
    selectIncident();
    document.querySelector('[data-inspector="incident"]').focus({ preventScroll: true });
  }
  if (button.hasAttribute('data-compare')) openView('experiments');
}

function startDrag(event) {
  const header = event.target.closest('.rack-header');
  if (!header || event.button !== 0) return;
  const previous = state.moves.get(header.dataset.entity) ?? { x: 0, y: 0 };
  drag = { id: header.dataset.entity, pointer: event.pointerId, previous, x: event.clientX, y: event.clientY, moved: false };
  header.setPointerCapture(event.pointerId);
}

function moveDrag(event) {
  if (drag?.pointer !== event.pointerId) return;
  const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
  if (!drag.moved && Math.hypot(dx, dy) < 4) return;
  drag.moved = true;
  state.moves.set(drag.id, { x: drag.previous.x + dx, y: drag.previous.y + dy });
  scheduleCanvas();
}

function endDrag(event) {
  if (drag?.pointer !== event.pointerId) return;
  suppressClick = drag.moved;
  drag = null;
  if (suppressClick) setTimeout(() => { suppressClick = false; }, 0);
}

function showGpuTooltip(event) {
  const button = event.target.closest('.gpu-slot');
  if (!button || drag || event.pointerType === 'touch') return;
  const bounds = map.getBoundingClientRect();
  const position = button.getBoundingClientRect();
  gpuTooltip.replaceChildren();
  const title = document.createElement('strong');
  title.textContent = `GPU ${button.textContent} · ${button.dataset.tooltipStatus}`;
  const readings = document.createElement('small');
  readings.textContent = button.dataset.tooltipMetrics;
  gpuTooltip.append(title, readings);
  gpuTooltip.style.left = `${bracket(position.x - bounds.x, 9, bounds.width - gpuTooltip.offsetWidth - 9)}px`;
  gpuTooltip.style.top = `${position.y - bounds.y - gpuTooltip.offsetHeight - 6}px`;
  gpuTooltip.classList.add('is-visible');
}

function hideGpuTooltip(event) {
  if (event.target.closest('.gpu-slot')) gpuTooltip.classList.remove('is-visible');
}

scenarioSelect.replaceChildren(...scenarios.map(scenario => new Option(scenario.title, scenario.id)));
range.max = String(duration - 1);
byId('footer-meta').textContent = `Twin schema ${schemaVersion} · Seed ${defaultSeed}`;
refreshModel();
makeRacks();
buildLayer();
changeView();

scenarioSelect.addEventListener('change', () => {
  const selected = scenarios.find(scenario => scenario.id === scenarioSelect.value);
  if (!selected) return;
  stopReplay();
  state.scenario = selected.id;
  state.layer = selected.layer;
  state.policy = 'baseline';
  state.chartMetric = { network: 'samplesPerSecond', demand: 'p95Ms', power: 'powerKw', thermal: 'p95Ms' }[state.scenario];
  state.inspector = 'incident';
  state.selection = null;
  state.tick = 0;
  refreshModel();
  buildLayer();
  changeView();
});
window.addEventListener('hashchange', changeView);
byId('layers').addEventListener('click', event => {
  const button = event.target.closest('[data-layer]');
  if (!button || state.layer === button.dataset.layer) return;
  state.layer = button.dataset.layer;
  buildLayer();
  renderLayer();
  renderSelectionState();
  scheduleCanvas();
});
byId('reset-map').addEventListener('click', () => {
  state.moves.clear();
  scheduleCanvas();
});
map.addEventListener('click', event => {
  const control = event.target.closest('[data-entity]');
  if (suppressClick) return;
  if (control) selectEntity(control.dataset.entity);
});
map.addEventListener('pointerdown', startDrag);
map.addEventListener('pointermove', moveDrag);
map.addEventListener('pointerup', endDrag);
map.addEventListener('pointercancel', endDrag);
map.addEventListener('pointerover', showGpuTooltip);
map.addEventListener('pointerout', hideGpuTooltip);
map.addEventListener('focusin', showGpuTooltip);
map.addEventListener('focusout', hideGpuTooltip);
byId('inspector').addEventListener('click', navigateInspector);
document.querySelector('.inspector-tabs').addEventListener('click', event => {
  const button = event.target.closest('[data-inspector]');
  if (button?.dataset.inspector === 'incident') selectIncident();
  if (button?.dataset.inspector === 'selection') {
    state.inspector = 'selection';
    renderInspector(false);
    scheduleCanvas();
  }
});
byId('play').addEventListener('click', () => {
  if (state.timer) {
    stopReplay();
    return;
  }
  if (state.tick === duration - 1) updateMinute(0);
  state.timer = setInterval(() => {
    updateMinute(state.tick + 1);
    if (state.tick === duration - 1) stopReplay();
  }, 650);
  renderReplay();
});
range.addEventListener('input', () => {
  const minute = range.valueAsNumber;
  stopReplay();
  updateMinute(minute);
});
byId('policy').addEventListener('click', () => {
  stopReplay();
  state.policy = state.policy === 'baseline' ? 'coordinated' : 'baseline';
  refreshModel();
  renderOverview();
});
experimentRoot.addEventListener('click', event => {
  const button = event.target.closest('[data-chart]');
  if (!button) return;
  state.chartMetric = button.dataset.chart;
  for (const chartButton of experimentRoot.querySelectorAll('[data-chart]')) {
    chartButton.setAttribute('aria-pressed', String(chartButton === button));
  }
  scheduleCanvas();
});
byId('show-placement').addEventListener('click', () => {
  if (!benchmark.plan) return;
  state.policy = 'coordinated';
  refreshModel();
  openView('overview');
});
byId('export-results').addEventListener('click', () => {
  const URLForExport = URL.createObjectURL(new Blob([JSON.stringify(benchmark, null, 2)], { type: 'application/json' }));
  const link = document.createElement('a');
  link.href = URLForExport;
  link.download = `crosslayer-${state.scenario}-seed-${defaultSeed}.json`;
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(URLForExport), 1000);
});
byId('open-tools').addEventListener('click', () => byId('tools-dialog').showModal());
new ResizeObserver(() => {
  if (state.view === 'overview') sizeMap();
  else scheduleCanvas();
}).observe(byId('main'));

const registry = document.modelContext ?? navigator.modelContext;
if (typeof registry?.registerTool === 'function') {
  for (const spec of mcpToolCatalog) {
    const report = () => console.warn(`Crosslayer could not register ${spec.name}`);
    try {
      void Promise.resolve(registry.registerTool({
        ...spec,
        execute(args) {
          try {
            return invokeMcpTool({ name: spec.name, arguments: args ?? {} });
          } catch (cause) {
            return { isError: true, content: [{ type: 'text', text: cause instanceof RangeError ? cause.message : 'Internal error' }] };
          }
        },
      })).catch(report);
    } catch {
      report();
    }
  }
}
