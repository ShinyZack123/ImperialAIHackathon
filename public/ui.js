import { scenarioOptions, getSnapshot, inspectEntity, runComparison, traceIncident, duration, defaultSeed, schemaVersion } from './twin.js';
import { invokeMcpTool, mcpToolCatalog } from './tool-catalog.js';
import { escapeHtml, fetchFlagCatalog, fetchFlagAnalysis, fetchFlagEntity, worstState, flagStateNames, seriesKey, sortSeries, isoSeconds, shortMetric, compactValue, drawFlagChart } from './flagging.js';

const byId = id => document.getElementById(id);
const esc = escapeHtml;
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
  flagging: null, flagMetric: null, logFilter: 'all',
};
// Flagger replay state. The analysis is computed by flagging/bridge.py; the console only renders it.
let analysis = null;
let flagError = null;
let flagCatalog = [];
let analysisController = null;
let flagGpus = new Map();
let flagRacks = new Map();
const entityData = new Map();
const entityErrors = new Map();
const flagMode = () => Boolean(state.flagging);
const flagStep = () => analysis?.steps[state.tick] ?? null;
const flagStateOf = id => flagStep()?.states[id] ?? null;
const flagged = value => value === 'critical' || value === 'warning';
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
      drawFlagEntityChart();
    } else drawChart();
  });
}

function flagMinutes(minutes) {
  return minutes >= 120 ? `${number(minutes / 60, minutes % 60 ? 1 : 0)} h` : `${number(minutes)} min`;
}

const flagSeries = {
  'flag-records': step => step.records,
  'flag-incidents': step => step.incidents.length,
  'flag-gates': step => step.gateCount,
  'flag-detectors': step => step.detectorsFired.length,
};

function renderFlagMetrics() {
  const step = flagStep();
  const pending = flagError ? 'Flagger unavailable' : 'Running flagger…';
  const top = step?.incidents[0];
  const cells = step ? [
    { label: 'Telemetry records analysed', value: number(step.records), short: 'records', series: 'flag-records',
      detail: `${number(analysis.records)} in the ${flagMinutes(analysis.window.end ? (isoSeconds(analysis.window.end) - isoSeconds(analysis.window.start)) / 60 : 0)} window` },
    { label: 'Escalated to agent', value: number(step.incidents.length), short: step.incidents.length === 1 ? 'incident' : 'incidents',
      series: 'flag-incidents', attention: Boolean(top),
      detail: top ? `${top.severity} · root ${top.rootEntity}` : 'Nothing worth escalating' },
    { label: 'Explained by context gates', value: number(step.gateCount), short: 'deviations', series: 'flag-gates',
      detail: step.gates[0] ?? 'No gate needed' },
    { label: 'Detectors firing', value: number(step.detectorsFired.length), short: 'methods', series: 'flag-detectors',
      attention: step.detectorsFired.length > 0, detail: step.detectorsFired.join(' · ') || 'All statistical detectors quiet' },
  ] : ['Telemetry records analysed', 'Escalated to agent', 'Explained by context gates', 'Detectors firing']
    .map(label => ({ label, value: '—', short: '', detail: flagError ?? pending, attention: Boolean(flagError) }));
  byId('metrics').innerHTML = cells.map(({ label, value, short, detail, attention, series }) => `
    <div class="metric-cell">
      <h3>${label}</h3>
      <div class="metric-value">${value}<small>${short}</small></div>
      ${series ? `<canvas class="sparkline" data-series="${series}" data-attention="${Boolean(attention)}" aria-hidden="true"></canvas>` : '<span></span>'}
      <p class="metric-detail clamp${attention ? ' attention' : ''}" title="${esc(detail)}">${esc(detail)}</p>
    </div>`).join('');
}

function renderMetrics() {
  if (flagMode()) return renderFlagMetrics();
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
  const timeline = flagMode() ? analysis?.steps : benchmark[state.policy]?.timeline;
  if (!timeline) return;
  for (const canvas of document.querySelectorAll('.sparkline')) {
    const rect = canvasFrame(canvas);
    if (!rect) continue;
    const { context, width, height } = rect;
    const values = timeline.map(row => flagMode() ? flagSeries[canvas.dataset.series]?.(row) ?? 0
      : canvas.dataset.series === 'efficiency' ? row.samplesPerSecond * 3600 / row.powerKw : row[canvas.dataset.series]);
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
        <span><i class="rack-status-dot" aria-hidden="true"></i><span class="rack-name">Rack ${rack.label}</span></span><span class="rack-grip" aria-hidden="true">⠿</span>
      </button>
      <div class="slots">${rack.gpus.map(gpu => `<button type="button" class="gpu-slot" data-entity="${gpu.id}">${String(gpu.slot).padStart(2, '0')}</button>`).join('')}</div>
      <div class="rack-meta"></div>`;
    byId('racks').append(el);
    racks.set(rack.id, { el, header: el.firstElementChild, name: el.querySelector('.rack-name'),
      slots: [...el.querySelectorAll('.gpu-slot')], meta: el.lastElementChild });
  }
}

function flagGpuState(gpu) {
  if (!analysis) return 'loading';
  return flagStateOf(gpu.twinId) ?? (gpu.hasTelemetry ? 'nominal' : 'nodata');
}

function renderFlagRacks() {
  for (const rack of snapshot.racks) {
    const { el, header, name, slots, meta } = racks.get(rack.id);
    const layout = flagRacks.get(rack.id);
    const gpus = layout?.gpus ?? [];
    const states = gpus.map(flagGpuState);
    const rackState = worstState([flagStateOf(rack.id), ...states.filter(value => flagged(value) || value === 'stale')]);
    el.dataset.status = flagged(rackState) ? 'flagged' : rackState === 'stale' ? 'unknown' : 'nominal';
    name.textContent = layout?.label ?? `Rack ${rack.label}`;
    header.setAttribute('aria-label', `${name.textContent}, ${flagStateNames[rackState] ?? 'no flags'}`);
    header.title = `Inspect ${name.textContent} or drag to move`;
    const hot = states.filter(flagged).length;
    const impacted = states.filter(value => value === 'impacted').length;
    const missing = states.filter(value => value === 'nodata' || value === 'stale').length;
    meta.textContent = !analysis ? (flagError ? 'Flagger unavailable' : 'Running flagger…')
      : [hot && `${hot} flagged`, impacted && `${impacted} in blast radius`, missing && `${missing} unknown`]
        .filter(Boolean).join(' · ') || 'No flags';
    gpus.forEach((gpu, i) => {
      const button = slots[i];
      const value = states[i];
      const base = value === 'nodata' || value === 'stale' || value === 'loading' ? 'unknown' : gpu.role;
      button.className = `gpu-slot ${base}${['critical', 'warning', 'impacted', 'stale'].includes(value) ? ` flag-${value}` : ''}`;
      button.textContent = gpu.label;
      const status = value === 'loading' ? 'Analysing' : flagStateNames[value] ?? value;
      button.setAttribute('aria-label', `GPU ${gpu.label}, ${layout.label}, ${status}`);
      button.dataset.tooltipStatus = status;
      button.dataset.tooltipMetrics = `${gpu.sourceId} · ${gpu.workloadId ?? 'unallocated'}`;
    });
  }
}

function renderRacks() {
  if (flagMode()) return renderFlagRacks();
  for (const rack of snapshot.racks) {
    const { el, header, name, slots, meta } = racks.get(rack.id);
    name.textContent = `Rack ${rack.label}`;
    el.dataset.status = rack.status;
    header.setAttribute('aria-label', `Rack ${rack.label}, ${statusNames[rack.status]}`);
    header.title = `Inspect Rack ${rack.label} or drag to move`;
    meta.textContent = `${metricText(rack.draw, 1)} · ${metricText(rack.temperature)}`;
    rack.gpus.forEach((gpu, i) => {
      const button = slots[i];
      button.className = `gpu-slot ${gpu.status}`;
      button.textContent = String(gpu.slot).padStart(2, '0');
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

  // In flagger mode the same map shows the flagging topology's names for each source.
  const names = flagMode()
    ? { 'pdu-a': 'PDUs 12A/B', 'pdu-b': 'PDUs 13A/B', 'cdu-a': 'CDU 02 · rack 12', 'cdu-b': 'CDU 02 · rack 13', 'grid-01': 'Feeds A/B · UPS' }
    : { 'pdu-a': 'PDU A', 'pdu-b': 'PDU B', 'cdu-a': 'Cooling loop A', 'cdu-b': 'Cooling loop B', 'grid-01': 'Site feed' };
  if (state.layer === 'fabric') {
    source('sw-spine', 'Spine 01');
  } else if (state.layer === 'power') {
    source('grid-01', names['grid-01']);
    source('pdu-a', names['pdu-a']);
    source('pdu-b', names['pdu-b']);
  } else {
    source('cdu-a', names['cdu-a']);
    source('cdu-b', names['cdu-b']);
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

function renderFlagLayer() {
  const step = flagStep();
  const top = step?.incidents[0];
  mapReadout.textContent = !analysis
    ? flagError ? `Flagger unavailable · ${flagError}` : 'Running the Python flagger over the telemetry window…'
    : top ? top.headline
      : `No escalation · ${number(step.records)} records checked${step.gateCount ? ` · ${number(step.gateCount)} deviations explained by context gates` : ''}`;
  snapshot.racks.forEach((rack, i) => {
    const button = rackLinks[i];
    const value = flagStateOf(button.dataset.entity);
    button.classList.toggle('fault', flagged(value));
    button.classList.toggle('unknown', value === 'stale');
    button.textContent = !analysis ? '…' : value === 'critical' ? 'Critical' : value === 'warning' ? 'Warning' : value === 'stale' ? 'Stale' : 'No flags';
    button.setAttribute('aria-label', `${flagRacks.get(rack.id)?.label ?? rack.label} ${state.layer} path, ${button.textContent}`);
  });
}

function renderLayer() {
  if (flagMode()) return renderFlagLayer();
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

function flagLabel(id) {
  const gpu = flagGpus.get(id);
  if (gpu) return `GPU ${gpu.label} · ${gpu.server}`;
  const rack = flagRacks.get(id);
  if (rack) return rack.label;
  const path = id.match(/^(link|power|cooling)-(?:rack-)?([ab]0[12])$/);
  if (path) return `${flagRacks.get(`rack-${path[2]}`)?.label ?? path[2]} · ${{ link: 'network uplinks', power: 'power path', cooling: 'cooling path' }[path[1]]}`;
  return { 'sw-spine': 'Spine 01', 'grid-01': 'Feeds A/B and UPS', 'pdu-a': 'PDUs 12A/12B', 'pdu-b': 'PDUs 13A/13B',
    'cdu-a': 'CDU 02 and coolant loop', 'cdu-b': 'CDU 02 and coolant loop' }[id] ?? id;
}

// The first console entity a finding lands on directly, so its evidence row can open that entity.
const findingTarget = finding => Object.entries(finding.twin ?? {}).find(([, how]) => how === 'direct')?.[0] ?? null;

function flagPending(title) {
  return `<div class="inspector-content">
    <span class="badge ${flagError ? 'unknown' : 'nominal'}">${flagError ? 'Unavailable' : 'Analysing'}</span>
    <h2 class="inspector-title" id="inspector-title">${esc(title)}</h2>
    <p class="inspector-copy">${flagError ? `${esc(flagError)}. Start the console with <code class="mono">npm run dev</code> so the Python flagger can run; until then these devices are unknown, not healthy.`
      : 'flagging/bridge.py is replaying the telemetry window through flag_window at 24 cut-offs.'}</p>
  </div>`;
}

function flagFindingRows(findings, limit = 6) {
  return findings.slice(0, limit).map(finding => {
    const target = findingTarget(finding);
    const tag = target ? 'button' : 'div';
    return `<li><${tag} class="evidence-item flag-evidence ${finding.severity}"${target ? ` type="button" data-entity="${esc(target)}"` : ''}>
      <strong>${esc(finding.entity)} · ${esc(shortMetric(finding.metric))}</strong><span class="mono">${esc(finding.detector)}</span>
      <small>${esc(finding.text)}</small>
    </${tag}></li>`;
  }).join('') + (findings.length > limit ? `<li class="placement-note">${number(findings.length - limit)} more findings in the agent message.</li>` : '');
}

function flagVerdict() {
  const v = analysis.verdict;
  const mark = ok => `<span class="${ok ? '' : 'fail'}" aria-label="${ok ? 'Matches' : 'Does not match'}">${ok ? '✓' : '×'}</span>`;
  return `<h3 class="evidence-heading">Answer key · end of window</h3>
    <ul class="constraint-list flag-verdict">
      <li>${mark(v.match)}Expected ${v.expectedFlag ? `a ${esc(v.expectedSeverity ?? '')} flag` : 'no flag'} · flagger ${v.flagged ? 'flagged' : 'stayed quiet'}</li>
      ${v.expectedFlag ? `<li>${mark(v.rootMatch)}Root cause ${esc(v.expectedRoot)} named in the escalation</li>` : ''}
      <li class="flag-reason">${esc(v.reason ?? '')}</li>
    </ul>`;
}

function flagIncidentInspector() {
  if (!analysis) return flagPending('Telemetry flagger');
  const step = flagStep();
  const { incidents } = step;
  const top = incidents[0];
  const firstFlag = analysis.firstFlagStep === null ? null : analysis.steps[analysis.firstFlagStep];
  const cards = incidents.slice(0, 3).map(incident => `<section class="flag-incident ${incident.severity}">
      <div class="flag-incident-head"><span class="badge ${incident.severity}">${esc(incident.severity)}</span>
        <button type="button" class="mono flag-root"${incident.rootTwin ? ` data-entity="${esc(incident.rootTwin)}"` : ''}>${esc(incident.rootEntity)}</button></div>
      <p class="flag-headline">${esc(incident.headline.replace(/^\[[A-Z]+\]\s*/, ''))}</p>
      <ul class="evidence-list">${flagFindingRows(incident.findings)}</ul>
      <div class="method-chips">${incident.methods.map(method => `<span>${esc(method)}</span>`).join('')}</div>
      <details class="agent-message"><summary>Message sent to the low-level agent</summary><pre>${esc(incident.message)}</pre></details>
    </section>`).join('');
  const quiet = step.gates.length
    ? `<h3 class="evidence-heading">Deviations a context gate explained</h3><ul class="evidence-list">${step.gates.map(gate => inspectorRow('Suppressed', 'gate', esc(gate))).join('')}</ul>`
    : '<div class="response-box"><strong>No detector fired</strong><span>The statistics alone show normal behaviour up to this cut-off.</span></div>';
  return `<div class="inspector-content">
    <span class="badge ${top ? top.severity : 'nominal'}">${top ? `${incidents.length} escalation${incidents.length === 1 ? '' : 's'}` : 'No escalation'}</span>
    <h2 class="inspector-title" id="inspector-title">${top ? esc(flagGpus.has(top.rootTwin) ? flagLabel(top.rootTwin) : top.rootDisplay) : 'Nothing to escalate'}</h2>
    <p class="inspector-copy">${esc(analysis.title)}</p>
    ${incidents.length ? cards : quiet}
    ${incidents.length > 3 ? `<p class="placement-note">${number(incidents.length - 3)} more incidents at this cut-off.</p>` : ''}
    ${flagVerdict()}
    <p class="placement-note mono">flag_window × ${number(analysis.engine.runs)} cut-offs · ${number(analysis.records)} records · ${number(analysis.engine.elapsedMs)} ms in Python${firstFlag ? ` · first escalated T+${flagMinutes(firstFlag.minute)}` : ''} · deterministic</p>
  </div>`;
}

function ensureFlagEntity(id) {
  const key = `${state.flagging}/${id}`;
  if (entityData.has(key) || entityErrors.get(key) === 'pending') return;
  entityErrors.set(key, 'pending');
  const scenario = state.flagging;
  fetchFlagEntity(scenario, id).then(body => {
    entityData.set(key, body);
    entityErrors.delete(key);
  }).catch(problem => {
    entityErrors.set(key, problem.message);
  }).finally(() => {
    if (state.flagging === scenario && state.selection === id && state.inspector === 'selection') {
      renderInspector(false);
      scheduleCanvas();
    }
  });
}

function entityFindings(id) {
  return (flagStep()?.incidents ?? []).flatMap(incident => incident.findings
    .filter(finding => finding.twin?.[id]).map(finding => ({ ...finding, how: finding.twin[id] })));
}

function selectedSeries(data) {
  const series = sortSeries(data.series);
  return { series, selected: series.find(item => seriesKey(item) === state.flagMetric) ?? series[0] ?? null };
}

function flagSelectionInspector() {
  if (!state.selection) return `<div class="inspector-content"><h2 class="inspector-title" id="inspector-title">Inspect the map</h2>
    <p class="inspector-copy">Select a GPU, server or connection. The console asks the Python bridge for its telemetry log and shows what the flagger concluded about it.</p></div>`;
  const id = state.selection;
  if (!analysis) return flagPending(flagLabel(id));
  const key = `${state.flagging}/${id}`;
  const data = entityData.get(key);
  const problem = entityErrors.get(key);
  const gpu = flagGpus.get(id);
  const findings = entityFindings(id);
  const value = flagStateOf(id) ?? (gpu && !gpu.hasTelemetry ? 'nodata' : 'nominal');
  const badge = flagged(value) ? value : value === 'stale' || value === 'nodata' ? 'unknown' : value === 'impacted' ? 'warning' : 'nominal';
  const header = `<span class="badge ${badge}">${esc(flagStateNames[value] ?? value)}</span>
    <h2 class="inspector-title" id="inspector-title">${esc(flagLabel(id))}</h2>
    <p class="inspector-copy mono flag-sources">${esc(data ? data.sources.map(item => item.id).join(' · ') : gpu ? `${gpu.sourceId} · ${gpu.workloadId ?? 'unallocated'}` : id)}</p>`;
  if (!data) {
    return `<div class="inspector-content">${header}
      <p class="inspector-copy">${problem && problem !== 'pending' ? `Bridge request failed: ${esc(problem)}. Readings are unknown.`
        : `Running <code class="mono">bridge.py entity ${esc(state.flagging)} ${esc(id)}</code>…`}</p></div>`;
  }
  const cursor = isoSeconds(flagStep().cutoff);
  const { series, selected } = selectedSeries(data);
  const latest = selected?.points.filter(([t]) => isoSeconds(t) <= cursor).at(-1);
  const spans = findings.filter(finding => finding.how === 'direct');
  const covered = row => spans.some(finding => finding.entity === row.entity && finding.metric === row.metric &&
    isoSeconds(row.t) >= isoSeconds(finding.start) - 1 && isoSeconds(row.t) <= isoSeconds(finding.end) + 1);
  const rows = data.log.filter(row => isoSeconds(row.t) <= cursor);
  const visible = (state.logFilter === 'flagged' ? rows.filter(covered) : rows).slice(-80).reverse();
  const options = series.map(item => `<option value="${esc(seriesKey(item))}"${item === selected ? ' selected' : ''}>${esc(shortMetric(item.metric))}${data.sources.length > 1 ? ` · ${esc(item.entity)}` : ''}</option>`).join('');
  return `<div class="inspector-content">${header}
    ${selected ? `<div class="flag-chart-head">
        <span class="select-wrap flag-metric"><select id="flag-metric" aria-label="Telemetry series">${options}</select><i aria-hidden="true"></i></span>
        <span class="mono flag-latest">${latest ? `${esc(compactValue(latest[1]))} ${esc(selected.unit ?? '')}${latest[2] !== 'ok' ? ` · ${esc(latest[2])}` : ''}` : 'No sample yet'}</span>
      </div>
      <canvas class="flag-chart" id="flag-chart" role="img" aria-label="${esc(shortMetric(selected.metric))} for ${esc(selected.entity)} up to the replay cursor"></canvas>`
      : '<p class="placement-note">No numeric series for this entity; see the log below.</p>'}
    <h3 class="evidence-heading">Flagger findings here</h3>
    ${findings.length ? `<ul class="evidence-list">${flagFindingRows(findings, 5)}</ul>`
      : `<p class="placement-note">${value === 'nodata' ? 'No telemetry in this window, so the state is unknown.' : 'No detector has flagged this entity up to this cut-off.'}</p>`}
    <div class="flag-log-head"><h3 class="evidence-heading">Telemetry log</h3>
      <div class="segment-control" role="group" aria-label="Log filter">
        <button type="button" data-log-filter="all" aria-pressed="${state.logFilter === 'all'}">All</button>
        <button type="button" data-log-filter="flagged" aria-pressed="${state.logFilter === 'flagged'}">Flagged</button>
      </div></div>
    <ol class="flag-log mono" aria-label="Telemetry records, newest first">${visible.map(row => `<li class="${covered(row) ? 'hit' : ''} ${row.quality !== 'ok' ? 'bad' : ''}">
        <time>${esc(row.t.slice(11, 19))}</time><span>${esc(data.sources.length > 1 ? `${row.entity} ${shortMetric(row.metric)}` : shortMetric(row.metric))}</span>
        <b>${esc(compactValue(row.value))}${row.unit && row.unit !== 'bool' ? ` ${esc(row.unit)}` : ''}</b>${row.quality !== 'ok' ? `<em>${esc(row.quality)}</em>` : ''}</li>`).join('')
      || '<li class="empty">No records up to this cut-off.</li>'}</ol>
    <p class="placement-note mono">bridge.py entity · ${number(data.elapsedMs)} ms in Python · ${number(data.bridgeMs ?? 0)} ms round trip · ${number(rows.length)} of ${number(data.log.length)} records so far${data.truncated ? ' · truncated' : ''}</p>
    <button type="button" class="secondary-button" data-show-incident>Back to incident <span aria-hidden="true">↗</span></button>
  </div>`;
}

function drawFlagEntityChart() {
  const canvas = byId('flag-chart');
  if (!canvas || !flagMode() || !analysis) return;
  const data = entityData.get(`${state.flagging}/${state.selection}`);
  if (!data) return;
  const { selected } = selectedSeries(data);
  const rect = selected && canvasFrame(canvas);
  if (!rect) return;
  const spans = entityFindings(state.selection)
    .filter(finding => finding.entity === selected.entity && finding.metric === selected.metric);
  drawFlagChart(rect.context, rect.width, rect.height, {
    series: selected, limits: data.limits[seriesKey(selected)] ?? [], spans,
    window: analysis.window, cursor: isoSeconds(flagStep().cutoff), stroke,
  });
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
  root.innerHTML = state.inspector === 'incident'
    ? flagMode() ? flagIncidentInspector() : incidentInspector()
    : flagMode() ? flagSelectionInspector() : selectionInspector();
  if (selector) root.querySelector(selector)?.focus({ preventScroll: true });
  renderSelectionState();
}

function flagClock(iso) {
  const long = analysis && isoSeconds(analysis.window.end) - isoSeconds(analysis.window.start) > 86_400;
  return `${long ? `${iso.slice(8, 10)} Nov ` : ''}${clock(iso)} UTC`;
}

function renderReplay() {
  if (flagMode()) {
    const step = flagStep();
    range.value = state.tick;
    const text = step ? `${flagClock(step.cutoff)} · T+${flagMinutes(step.minute)}` : '—';
    range.setAttribute('aria-valuetext', `${text}, cut-off ${state.tick + 1} of ${duration}`);
    byId('replay-timestamp').textContent = `${text} · ${state.tick + 1}/${duration}`;
    byId('policy').hidden = true;
    byId('topology-summary').textContent = '4 servers · 32 GPUs · Python flagger replay';
    byId('play').setAttribute('aria-label', state.timer ? 'Pause telemetry replay' : 'Play telemetry replay');
    byId('play').firstElementChild.classList.toggle('playing', Boolean(state.timer));
    return;
  }
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

const legends = {
  twin: [['training', 'Training'], ['inference', 'Inference'], ['waiting', 'Waiting on fabric'], ['idle', 'Idle'], ['unknown', 'Unknown']],
  flag: [['training', 'Training'], ['inference', 'Inference'], ['idle', 'Idle'], ['flag-critical', 'Flagged critical'],
    ['flag-warning', 'Flagged warning'], ['flag-impacted', 'Blast radius'], ['unknown', 'Unknown / no data']],
};

function renderLegend() {
  const legend = byId('legend');
  const mode = flagMode() ? 'flag' : 'twin';
  if (legend.dataset.mode === mode) return;
  legend.dataset.mode = mode;
  legend.innerHTML = legends[mode].map(([css, label]) => `<span><i class="${css}"></i> ${label}</span>`).join('');
}

function renderOverview() {
  renderLegend();
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
    ? flagMode() ? 'Replay GPU and facility telemetry through the deterministic flagger. Click any GPU to read its log.'
      : 'Trace workload performance across the compute, network and facility layers.'
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
  if (flagMode() && analysis) ensureFlagEntity(id);
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
  if (button.dataset.logFilter) {
    state.logFilter = button.dataset.logFilter;
    renderInspector(false);
    scheduleCanvas();
  }
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
  title.textContent = `GPU ${button.textContent.trim()} · ${button.dataset.tooltipStatus}`;
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

function optionGroup(label, options) {
  const group = document.createElement('optgroup');
  group.label = label;
  group.append(...options);
  return group;
}

function renderScenarioOptions() {
  const twin = optionGroup('Twin simulation · policy experiments', scenarios.map(scenario => new Option(scenario.title, scenario.id)));
  const groups = [twin];
  const named = { showcase: 'Telemetry flagger · full-hall showcase', fault: 'Telemetry flagger · labelled faults', benign: 'Telemetry flagger · benign near-misses' };
  for (const [group, label] of Object.entries(named)) {
    const items = flagCatalog.filter(item => item.group === group)
      .map(item => new Option(`${item.id.split('-')[0]} · ${item.title}`, `flag:${item.id}`));
    if (items.length) groups.push(optionGroup(label, items));
  }
  if (!flagCatalog.length) {
    const offline = new Option('Flagger offline · run npm run dev', '', false, false);
    offline.disabled = true;
    groups.push(optionGroup('Telemetry flagger', [offline]));
  }
  scenarioSelect.replaceChildren(...groups);
  scenarioSelect.value = state.flagging ? `flag:${state.flagging}` : state.scenario;
}

function indexAnalysis(body) {
  flagRacks = new Map(body.layout.racks.map(rack => [rack.twinId, rack]));
  flagGpus = new Map(body.layout.racks.flatMap(rack => rack.gpus.map(gpu => [gpu.twinId, { ...gpu, server: rack.label }])));
}

function enterFlagging(id) {
  stopReplay();
  analysisController?.abort();
  analysisController = new AbortController();
  state.flagging = id;
  state.tick = 0;
  state.inspector = 'incident';
  state.selection = null;
  state.flagMetric = null;
  state.logFilter = 'all';
  analysis = null;
  flagError = null;
  refreshModel();
  buildLayer();
  if (location.hash === '#experiments') location.hash = 'overview';
  else changeView();
  const { signal } = analysisController;
  fetchFlagAnalysis(id, signal).then(body => {
    if (state.flagging !== id) return;
    if (!Array.isArray(body.steps) || body.steps.length !== duration) throw new Error('Unexpected flagger response');
    analysis = body;
    indexAnalysis(body);
    // Open the layer where the final root cause lives, so the red path is visible.
    const layer = body.steps.at(-1).incidents.map(incident => incident.rootLayer).find(value => ['fabric', 'power', 'cooling'].includes(value));
    state.layer = layer ?? 'fabric';
    buildLayer();
    renderOverview();
  }).catch(problem => {
    if (signal.aborted || state.flagging !== id) return;
    flagError = problem.message;
    renderOverview();
  });
}

function leaveFlagging() {
  analysisController?.abort();
  state.flagging = null;
  analysis = null;
  flagError = null;
}

renderScenarioOptions();
fetchFlagCatalog().then(body => {
  flagCatalog = body.scenarios.filter(item => typeof item.id === 'string' && typeof item.title === 'string');
  renderScenarioOptions();
}).catch(() => renderScenarioOptions());
range.max = String(duration - 1);
byId('footer-meta').textContent = `Twin schema ${schemaVersion} · Seed ${defaultSeed}`;
refreshModel();
makeRacks();
buildLayer();
changeView();

scenarioSelect.addEventListener('change', () => {
  if (scenarioSelect.value.startsWith('flag:')) {
    const id = scenarioSelect.value.slice(5);
    if (flagCatalog.some(item => item.id === id)) enterFlagging(id);
    return;
  }
  const selected = scenarios.find(scenario => scenario.id === scenarioSelect.value);
  if (!selected) return;
  stopReplay();
  leaveFlagging();
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
byId('inspector').addEventListener('change', event => {
  if (event.target.id !== 'flag-metric') return;
  state.flagMetric = event.target.value;
  renderInspector(false);
  byId('flag-metric')?.focus({ preventScroll: true });
  scheduleCanvas();
});
document.querySelector('.inspector-tabs').addEventListener('click', event => {
  const button = event.target.closest('[data-inspector]');
  if (button?.dataset.inspector === 'incident') selectIncident();
  if (button?.dataset.inspector === 'selection') {
    state.inspector = 'selection';
    if (flagMode() && analysis && state.selection) ensureFlagEntity(state.selection);
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
