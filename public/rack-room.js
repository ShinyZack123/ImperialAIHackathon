import { escapeHtml as esc } from './flagging.js';
import { diagnoseGpu } from './rack-scene.js';

const byId = (id) => document.getElementById(id);
const labels = { clear: 'No findings', warning: 'Shared issue', fault: 'GPU fault', unknown: 'Telemetry unknown' };
const priority = ['fault', 'warning', 'unknown', 'clear'];
const number = (n, places = 1) => Number.isFinite(n)
  ? new Intl.NumberFormat('en-GB', { maximumFractionDigits: places }).format(n)
  : 'Unknown';
const measure = (n, unit, places = 1) => Number.isFinite(n) ? number(n, places) + ' ' + unit : 'Unknown';
const clock = (iso) => iso?.slice(11, 19) + ' UTC';
const gpuTitle = (id) => id.toUpperCase();
const focus = byId('focus-rack');
const selector = byId('gpu-select');
const slider = byId('replay-time');
const player = byId('play-replay');
const jump = byId('incident-jump');
const replaySelect = byId('replay-case');
const stageStatus = byId('stage-status');
const tooltip = byId('gpu-hover');
const rackSwitch = byId('rack-switch');
let racks = [];
let frames = [];
let replays = {};
let diagnostics = new Map();
let active = 0;
let inspection = { rack: null, gpu: null };
let scene = null;
let playback = null;
let incidentFrame = 0;
let inventory = new Map();

const frame = () => frames[active];
const rackFor = (id) => racks.find((rack) => rack.id === id);
const selectedRack = () => rackFor(inspection.rack);
const gpuStatus = (id) => diagnostics.get(id);

function rackState(rack) {
  const states = rack.gpus.map(({gpu_id}) => gpuStatus(gpu_id)?.kind);
  return priority.find((value) => states.includes(value)) ?? 'unknown';
}

function faultCount(rack) {
  return rack.gpus.filter(({gpu_id}) => gpuStatus(gpu_id)?.kind === 'fault').length;
}

function getIncident(rack) {
  const ids = new Set(rack.gpus.map((gpu) => gpu.gpu_id));
  return frame().incidents.find((item) => item.root === rack.zone || item.gpus.some((id) => ids.has(id)));
}

function evidenceSummary(incident) {
  if (!incident) return '';
  let reading = incident.evidence;
  const flow = reading.match(/flow_percent fell from baseline ([\d.]+) to ([\d.]+) \(([^)]+)\)/);
  if (flow) reading = 'Coolant flow fell from ' + number(Number(flow[1]), 0) + '% to ' + number(Number(flow[2]), 0) + '% of nominal during the assessment window.';
  return '<div class="evidence"><strong>' + esc(incident.root) + ' · detector finding</strong>' + esc(reading) + '</div>';
}

function metricCell(label, value, detail, state = '') {
  return '<div class="fleet-cell ' + state + '"><span>' + esc(label) + '</span><strong>' + esc(value) +
    '</strong><small>' + esc(detail) + '</small></div>';
}

function field(label, value, flagged = false) {
  return (flagged ? '<div class="is-fault"><dt>' : '<div><dt>') + esc(label) + '</dt><dd>' + esc(value) + '</dd></div>';
}

function statusBadge(state, label = labels[state]) {
  return '<span class="pill ' + state + '">' + esc(label) + '</span>';
}

function gpuInspector(rack, item, meta) {
  const status = gpuStatus(meta.gpu_id);
  const m = item.metrics;
  const timestamp = item.telemetry_ts ? clock(item.telemetry_ts) : 'Unavailable';
  return '<div class="inspect-header"><div><h3>' + esc(gpuTitle(meta.gpu_id)) + '</h3><p class="inspect-meta">' +
    esc(meta.vendor + ' · ' + rack.label + ' · ' + meta.server_id) + '</p></div>' + statusBadge(status.kind, status.label) + '</div>' +
    '<dl class="figures">' + field('Temperature', measure(m.temp_c, '°C'), status.metric === 'temperature') + field('Power', measure(m.power_w, 'W'), status.metric === 'power') +
    field('Utilisation', measure(m.gpu_util_pct, '%')) + field('Memory', measure(m.memory_used_gb, 'GB')) +
    field('Cooling flow', measure(item.cooling?.flow_pct, '%')) + field('Workload', item.workload?.state ?? 'Unknown') +
    '</dl><div class="temp-chart"><h4>Temperature · last 5 min</h4><canvas id="temp-chart" width="550" height="120" role="img"></canvas></div>' +
    '<p class="hint">Last GPU reading ' + esc(timestamp) + ' · ' + esc(meta.cooling_zone_id) + '</p>' +
    (status.detail ? '<div class="evidence fault-note"><strong>' + esc(status.label) + '</strong>' + esc(status.detail) + '</div>' : '') +
    (status.kind === 'fault' && getIncident(rack)?.root === meta.gpu_id ? '' : evidenceSummary(getIncident(rack)));
}

function rackInspector(rack) {
  const readings = rack.gpus.map(({gpu_id}) => frame().devices[gpu_id]);
  const valid = (key) => readings.map((row) => row.metrics[key]).filter(Number.isFinite);
  const mean = (list) => list.length ? list.reduce((a, b) => a + b, 0) / list.length : null;
  const power = valid('power_w');
  const flow = readings.find((item) => Number.isFinite(item.cooling?.flow_pct))?.cooling.flow_pct;
  const flagged = readings.filter((row) => gpuStatus(row.id)?.kind === 'warning').length;
  return '<div class="inspect-header"><div><h3>' + esc(rack.label) + '</h3><p class="inspect-meta">8 GPU trays · ' +
    esc(rack.zone) + '</p></div>' + statusBadge(rackState(rack)) + '</div>' +
    '<dl class="figures">' + field('Mean temperature', measure(mean(valid('temp_c')), '°C')) +
    field('GPU draw', measure(power.length ? power.reduce((a, b) => a + b, 0) / 1000 : null, 'kW', 2)) +
    field('Observed GPUs', readings.filter((item) => item.telemetry_is_observed).length + ' / 8') +
    field('GPU faults', String(faultCount(rack)), faultCount(rack) > 0) + field('Latest coolant flow', measure(flow, '%')) +
    field('Shared findings', String(flagged)) + '</dl>' + evidenceSummary(getIncident(rack)) +
    '<p class="hint">Click a tray in the hall to slide it out and check that GPU’s readings.</p>';
}

function drawTemperature() {
  const chart = byId('temp-chart');
  if (!chart) return;
  const end = frame().offset;
  const points = frames.slice(0, active + 1).filter((f) => f.offset >= end - 300)
    .map((f) => [f.offset, f.devices[inspection.gpu]?.metrics.temp_c]);
  const values = points.map((point) => point[1]).filter(Number.isFinite);
  const low = Math.min(...values) - 1;
  const high = Math.max(...values) + 1;
  const ctx = chart.getContext('2d');
  ctx.font = '17px monospace';
  ctx.fillStyle = '#52665d';
  if (!values.length) {
    chart.setAttribute('aria-label', 'No temperature readings in this window');
    ctx.fillText('No readings in this window', 18, 62);
    return;
  }
  chart.setAttribute('aria-label', 'Recorded temperatures ranged from ' + number(Math.min(...values)) + ' to ' +
    number(Math.max(...values)) + ' degrees Celsius in the last five minutes.');
  ctx.fillText(number(Math.max(...values)) + ' °C', 15, 22);
  ctx.fillText(number(Math.min(...values)) + ' °C', 15, 105);
  ctx.strokeStyle = '#c8d9cd';
  ctx.lineWidth = 2;
  for (const y of [20, 61, 102]) {
    ctx.beginPath(); ctx.moveTo(102, y); ctx.lineTo(535, y); ctx.stroke();
  }
  let drawing = false;
  ctx.strokeStyle = ctx.fillStyle = '#347c5b';
  ctx.lineWidth = 3;
  ctx.beginPath();
  const xAt = (t) => 105 + (t - (end - 300)) / 300 * 429;
  const yAt = (n) => 103 - (n - low) / (high - low) * 84;
  for (const [t, n] of points) {
    if (!Number.isFinite(n)) { drawing = false; continue; }
    if (drawing) ctx.lineTo(xAt(t), yAt(n));
    else ctx.moveTo(xAt(t), yAt(n));
    drawing = true;
  }
  ctx.stroke();
  const last = points.filter((p) => Number.isFinite(p[1])).at(-1);
  ctx.beginPath(); ctx.arc(xAt(last[0]), yAt(last[1]), 3.5, 0, Math.PI * 2); ctx.fill();
}

function renderInspector() {
  const rack = selectedRack();
  const id = inspection.gpu;
  byId('inspect-kind').textContent = id ? 'GPU' : 'RACK';
  byId('inspection').innerHTML = id
    ? gpuInspector(rack, frame().devices[id], inventory.get(id))
    : rackInspector(rack);
  selector.value = id ?? '';
  focus.disabled = !scene || !rack;
  drawTemperature();
}

function renderRackButtons() {
  for (const button of rackSwitch.querySelectorAll('button')) {
    const rack = rackFor(button.dataset.rack);
    const state = rackState(rack);
    button.dataset.state = state;
    button.setAttribute('aria-pressed', String(inspection.rack === rack.id));
    const count = faultCount(rack);
    button.querySelector('small').textContent = count ? count + (count === 1 ? ' GPU fault' : ' GPU faults') : labels[state];
  }
}

function renderFrame() {
  const f = frame();
  diagnostics = new Map(Object.entries(f.devices).map(([id, gpu]) => [id, diagnoseGpu(gpu, f.gpuFindings?.[id])]));
  const count = [...diagnostics.values()].filter((item) => item.kind === 'fault').length;
  byId('stage-count').textContent = '3 racks · 24 GPUs' + (count ? ' · ' + count + (count === 1 ? ' GPU fault' : ' GPU faults') : '');
  tooltip.hidden = true;
  const o = f.overview;
  byId('fleet-metrics').innerHTML =
    metricCell('Observed GPUs', o.observed_gpu_count + ' / ' + o.total_gpu_count, 'Across 3 racks') +
    metricCell('GPU power', measure(o.observed_gpu_power_kw, 'kW', 2), 'Reporting GPUs') +
    metricCell('Mean GPU temp', measure(o.observed_avg_temp_c, '°C'), 'Reporting GPUs') +
    metricCell('Detector findings', String(f.incidents.length), f.incidents[0] ? f.incidents[0].root : 'Selected window',
      f.incidents.length ? 'warning' : '');
  slider.value = String(active);
  slider.setAttribute('aria-valuetext', clock(f.time));
  byId('replay-clock').textContent = clock(f.time);
  renderRackButtons();
  renderInspector();
  scene?.update(diagnostics, rackState);
}

function selectRack(id, zoom = false) {
  inspection = { rack: id, gpu: null };
  renderRackButtons();
  renderInspector();
  scene?.select(null, id);
  if (zoom) scene?.focus(id);
}

function selectGpu(id, toggle = false) {
  const gpu = inventory.get(id);
  if (!gpu) return;
  if (toggle && id === inspection.gpu) {
    selectRack(gpu.rack_id);
    return;
  }
  inspection = { rack: gpu.rack_id, gpu: id };
  renderRackButtons();
  renderInspector();
  scene?.select(id, gpu.rack_id);
}

function showHover(id, x, y) {
  if (!id) { tooltip.hidden = true; return; }
  const read = frame().devices[id];
  const status = gpuStatus(id);
  tooltip.dataset.state = status.kind;
  tooltip.textContent = gpuTitle(id) + ' · ' + measure(read.metrics.temp_c, '°C') + ' · ' + status.label;
  tooltip.hidden = false;
  tooltip.style.left = Math.min(x + 10, Math.max(6, byId('scene').clientWidth - tooltip.offsetWidth - 6)) + 'px';
  tooltip.style.top = Math.max(5, y - 28) + 'px';
}

function addRecorder() {
  const row = document.createElement('p');
  row.className = 'recording';
  const button = document.createElement('button');
  button.type = 'button';
  button.textContent = 'Record walkthrough';
  row.append(button);
  byId('workspace').append(row);
  button.addEventListener('click', async () => {
    pause();
    button.disabled = true;
    button.textContent = 'Recording walkthrough…';
    try {
      const { recordWalkthrough } = await import('./rack-scene.js');
      const download = await recordWalkthrough({
        scene,
        setStep(step) {
          const coolingRack = racks.find((item) => item.zone === 'zone-b');
          if (step === 0 || step === 4) loadReplay('cooling');
          else if (step === 1) selectRack(coolingRack.id, true);
          else if (step === 2) selectGpu('gpu-016');
          else if (step === 3) {
            loadReplay('single');
            selectGpu('gpu-001');
            scene.focus(inspection.rack);
          }
        },
        getView() {
          const overview = frame().overview;
          return {
            time: clock(frame().time),
            observed: overview.observed_gpu_count + ' / ' + overview.total_gpu_count,
            power: measure(overview.observed_gpu_power_kw, 'kW', 2),
            temp: measure(overview.observed_avg_temp_c, '°C'),
            findings: frame().incidents.length,
            title: byId('inspection').querySelector('h3').textContent,
            meta: byId('inspection').querySelector('.inspect-meta').textContent,
            metrics: Array.from(byId('inspection').querySelectorAll('.figures div')).map((el) =>
              [el.querySelector('dt').textContent, el.querySelector('dd').textContent]),
            chart: byId('temp-chart'),
            evidence: inspection.gpu ? gpuStatus(inspection.gpu)?.label : rackState(selectedRack()) === 'fault' ? 'GPU faults on this rack' : '',
            replayLabel: replaySelect.selectedOptions[0].textContent,
          };
        },
      });
      row.replaceChildren(download);
    } catch (error) {
      button.textContent = error.message;
      button.disabled = false;
    }
  });
}

function pause() {
  window.clearInterval(playback);
  playback = null;
  player.textContent = '▶';
  player.setAttribute('aria-label', 'Play replay');
}

function focusReplayIssue() {
  const faulty = racks.find((rack) => faultCount(rack));
  const affected = racks.find((rack) => getIncident(rack));
  selectRack((faulty ?? affected ?? racks[0]).id);
}

function loadReplay(key) {
  pause();
  replaySelect.value = key;
  const replay = replays[key];
  frames = replay.frames;
  active = Math.max(0, frames.findIndex((f) => f.offset === replay.defaultOffset));
  incidentFrame = Math.max(0, frames.findIndex((f) => f.offset === replay.eventOffset));
  slider.max = String(frames.length - 1);
  jump.textContent = key === 'cooling' ? 'Cooling finding' : 'GPU fault';
  inspection = { rack: racks[0].id, gpu: null };
  renderFrame();
  focusReplayIssue();
  scene?.focus(null);
}

async function initialise() {
  const response = await fetch('./assets/rack-replay.json');
  if (!response.ok) throw new Error('Cannot open the saved replay.');
  const data = await response.json();
  if (data.source !== 'saved_databricks_replay' || data.synthetic !== true ||
      data.inventory?.length !== 24 || !data.replays?.cooling?.frames?.length || !data.replays?.single?.frames?.length) throw new Error('Invalid rack replay.');
  inventory = new Map(data.inventory.map((gpu) => [gpu.gpu_id, gpu]));
  racks = [...new Set(data.inventory.map((gpu) => gpu.rack_id))].map((id) => {
    const gpus = data.inventory.filter((gpu) => gpu.rack_id === id);
    return { id, label: 'Rack ' + id.split('-').at(-1).toUpperCase(), zone: gpus[0].cooling_zone_id, gpus };
  });
  if (racks.length !== 3 || racks.some((r) => r.gpus.length !== 8)) throw new Error('Unexpected rack layout.');
  replays = data.replays;
  slider.disabled = player.disabled = jump.disabled = selector.disabled = replaySelect.disabled = false;
  replaySelect.addEventListener('change', () => loadReplay(replaySelect.value));
  for (const rack of racks) {
    const button = document.createElement('button');
    button.type = 'button';
    button.dataset.rack = rack.id;
    button.innerHTML = esc(rack.label) + '<small></small>';
    button.addEventListener('click', () => selectRack(rack.id, true));
    rackSwitch.append(button);
    const group = document.createElement('optgroup');
    group.label = rack.label;
    for (const gpu of rack.gpus) group.append(new Option(gpuTitle(gpu.gpu_id) + ' · ' + gpu.vendor, gpu.gpu_id));
    selector.append(group);
  }
  slider.addEventListener('input', () => {
    pause();
    active = +slider.value;
    renderFrame();
  });
  player.addEventListener('click', () => {
    if (playback !== null) { pause(); return; }
    if (active >= frames.length - 1) { active = 0; renderFrame(); }
    player.textContent = 'Ⅱ';
    player.setAttribute('aria-label', 'Pause replay');
    playback = window.setInterval(() => {
      active += 1;
      renderFrame();
      if (active >= frames.length - 1) pause();
    }, 720);
  });
  jump.addEventListener('click', () => {
    pause();
    active = incidentFrame;
    renderFrame();
    focusReplayIssue();
  });
  selector.addEventListener('change', () => {
    if (selector.value) selectGpu(selector.value);
    else selectRack(inspection.rack);
  });
  focus.addEventListener('click', () => scene?.focus(inspection.rack));
  byId('fit').addEventListener('click', () => scene?.focus(null));
  window.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && inspection.gpu) selectRack(inspection.rack);
  });
  loadReplay('cooling');
  try {
    const { mountRackScene } = await import('./rack-scene.js');
    scene = await mountRackScene(byId('scene'), racks, (id) => selectGpu(id, true), showHover);
    scene.select(inspection.gpu, inspection.rack);
    renderFrame();
    stageStatus.hidden = true;
    if (new URLSearchParams(location.search).has('record')) addRecorder();
  } catch (error) {
    console.warn('3D scene unavailable:', error.message);
    stageStatus.textContent = 'The 3D view is unavailable. Select a GPU to see its recorded readings.';
  }
}

initialise().catch((error) => {
  stageStatus.textContent = 'The saved replay could not be loaded.';
  byId('inspection').textContent = error.message;
});
