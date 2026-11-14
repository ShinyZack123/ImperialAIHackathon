// The same-origin service explicitly identifies connected or saved Databricks
// evidence. Errors never change its mode; device IDs stay as supplied by the source.
import { escapeHtml } from './flagging.js';

const databricksTimeoutMs = 245_000; // Warehouse startup, detector and persisted result.
const dbMetricOptions = {
  temp_c: { label: 'GPU temperature', unit: '°C' },
  power_w: { label: 'GPU power', unit: 'W' },
  gpu_util_pct: { label: 'GPU utilisation', unit: '%' },
  clock_mhz: { label: 'GPU clock', unit: 'MHz' },
};
const dbStateLabels = { critical: 'Critical finding', warning: 'Warning finding', clear: 'No findings', unknown: 'Telemetry unknown' };

function dbNumber(value, digits = 1) {
  return typeof value === 'number' && Number.isFinite(value)
    ? value.toLocaleString('en-GB', { maximumFractionDigits: digits }) : 'Unknown';
}

function databricksPresentation(body) {
  const mode = body?.delivery ?? 'connected';
  if (!['connected', 'saved_replay'].includes(mode)) throw new Error('The data service returned an unsupported delivery mode.');
  if (mode === 'saved_replay') {
    if (typeof body.exported_at !== 'string' || !Number.isFinite(Date.parse(body.exported_at))) throw new Error('The saved replay is missing its export timestamp.');
    const exportedAt = new Date(body.exported_at).toISOString();
    return {
      mode, exportedAt, label: 'Saved Databricks replay',
      description: `Synthetic evidence exported ${exportedAt}. The team’s deterministic detector runs locally. Assessments are not written to Databricks.`,
      loading: 'Reading the saved Databricks export and running the team’s deterministic detector locally…',
      footer: 'Saved Databricks replay · local detector · no Databricks writes',
    };
  }
  return {
    mode, exportedAt: null, label: 'Connected Databricks',
    description: 'Synthetic evidence queried from Databricks. The team’s deterministic detector runs in the backend, and assessments are saved to Databricks.',
    loading: 'Reading Databricks evidence, running the team’s deterministic detector and saving the assessment…',
    footer: 'Connected Databricks · deterministic detector · observation only',
  };
}

function assertDatabricksDelivery(body, expected) {
  const actual = databricksPresentation(body);
  if (expected && (actual.mode !== expected.mode || actual.exportedAt !== expected.exportedAt)) {
    throw new Error('The data service changed delivery mode or export. Refresh replays before continuing.');
  }
  return actual;
}

function databricksPersistenceText(snapshot) {
  if (databricksPresentation(snapshot).mode === 'saved_replay') {
    if (snapshot.persistence?.status !== 'not_saved' || snapshot.persistence?.reason !== 'saved_replay') {
      throw new Error('A saved replay cannot claim a new Databricks write.');
    }
    return 'Assessment computed locally · not written to Databricks';
  }
  return snapshot.persistence?.status === 'saved' ? 'Assessment saved in Databricks' : 'Databricks save not confirmed';
}

function parseReplayRuns(body) {
  if (body?.source !== 'databricks' || body?.synthetic !== true || !Array.isArray(body.data)) {
    throw new Error('The data service returned an unsupported replay catalogue.');
  }
  databricksPresentation(body);
  return body.data.map(run => {
    if (!/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(run.run_id) || !Number.isFinite(Date.parse(run.start_ts)) || !Number.isFinite(Date.parse(run.end_ts)) || Date.parse(run.start_ts) > Date.parse(run.end_ts)) {
      throw new Error('The data service returned an invalid replay interval.');
    }
    return run;
  });
}

function parseDatabricksSnapshot(body, runId, cursor, expectedDelivery) {
  const snapshot = body?.data ?? body;
  if (snapshot?.source !== 'databricks' || snapshot?.synthetic !== true || snapshot?.run_id !== runId || Date.parse(snapshot.as_of_ts) !== Date.parse(cursor)
      || !snapshot.overview || !Array.isArray(snapshot.racks) || !Array.isArray(snapshot.incidents)) {
    throw new Error('The data service returned a snapshot for an unexpected source or replay time.');
  }
  const delivery = assertDatabricksDelivery(body, expectedDelivery);
  assertDatabricksDelivery(snapshot, delivery);
  databricksPersistenceText(snapshot);
  const seen = new Set();
  for (const rack of snapshot.racks) {
    if (typeof rack.id !== 'string' || !Array.isArray(rack.gpus)) throw new Error('The data service returned an invalid rack.');
    for (const gpu of rack.gpus) {
      if (typeof gpu.id !== 'string' || seen.has(gpu.id) || !dbStateLabels[gpu.state] || !gpu.metrics) {
        throw new Error('The data service returned an invalid GPU.');
      }
      seen.add(gpu.id);
    }
  }
  if (seen.size !== snapshot.overview.total_gpu_count) throw new Error('The fleet count does not match its inventory.');
  return snapshot;
}

async function requestDatabricks(path, params = {}, signal) {
  if (typeof location !== 'undefined' && location.protocol === 'file:') {
    throw new Error('Open the website with npm run dev to use Databricks evidence. This downloaded HTML copy has no data service.');
  }
  const query = new URLSearchParams(params).toString();
  const timeout = AbortSignal.timeout(databricksTimeoutMs);
  const response = await fetch(`./api/databricks/${path}${query ? `?${query}` : ''}`, {
    headers: { Accept: 'application/json' },
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
  });
  let body;
  try { body = await response.json(); } catch { throw new Error('The data service did not return a readable response.'); }
  if (!response.ok || body?.error) {
    throw new Error(typeof body?.detail === 'string' ? body.detail : typeof body?.error === 'string' ? body.error : `The replay data service is unavailable (${response.status}).`);
  }
  return body;
}

// Missing readings and gaps break the chart, rather than becoming zero or being
// joined across an outage. All points are checked against the selected cutoff.
function historySegments(rows, metric, cursor) {
  const end = Date.parse(cursor);
  if (!Number.isFinite(end)) throw new Error('Invalid replay time.');
  const sorted = [...rows].sort((a, b) => Date.parse(a.event_ts) - Date.parse(b.event_ts));
  const segments = [];
  let current = [];
  let previous = null;
  for (const row of sorted) {
    const time = Date.parse(row.event_ts);
    if (!Number.isFinite(time) || time > end || time < end - 5 * 60_000) continue;
    const value = row[metric];
    const good = row.heartbeat_ok === true && typeof value === 'number' && Number.isFinite(value);
    if (!good || (previous !== null && time - previous > 15_000)) {
      if (current.length) segments.push(current);
      current = [];
    }
    if (good) current.push([time, value]);
    previous = time;
  }
  if (current.length) segments.push(current);
  return segments;
}

function dbChart(rows, metric, cursor) {
  const segments = historySegments(rows, metric, cursor);
  const values = segments.flat().map(point => point[1]);
  if (!values.length) return '<div class="db-empty">No observed readings for this metric in the selected window.</div>';
  const end = Date.parse(cursor);
  const start = end - 5 * 60_000;
  let low = Math.min(...values), high = Math.max(...values);
  const pad = Math.max((high - low) * .15, 1);
  low -= pad; high += pad;
  const x = time => 44 + (time - start) / (end - start) * 466;
  const y = value => 150 - (value - low) / (high - low) * 126;
  const lines = [0, 1, 2].map(index => {
    const value = low + (high - low) * index / 2;
    return `<line x1="44" x2="510" y1="${y(value)}" y2="${y(value)}" class="db-chart-grid"/><text x="37" y="${y(value) + 3}" text-anchor="end">${escapeHtml(dbNumber(value))}</text>`;
  }).join('');
  const paths = segments.map(points => points.length === 1
    ? `<circle cx="${x(points[0][0])}" cy="${y(points[0][1])}" r="2.5" class="db-chart-point"/>`
    : `<polyline points="${points.map(([time, value]) => `${x(time)},${y(value)}`).join(' ')}" class="db-chart-line"/>`).join('');
  return `<svg class="db-history-chart" viewBox="0 0 530 182" role="img" aria-label="${escapeHtml(dbMetricOptions[metric].label)} over the five minutes ending at ${escapeHtml(cursor)}; gaps indicate unknown readings">
    ${lines}${paths}<text x="44" y="174">${new Date(start).toISOString().slice(11, 19)}</text><text x="510" y="174" text-anchor="end">${new Date(end).toISOString().slice(11, 19)} UTC</text></svg>`;
}

function mountDatabricksView(root, onDeliveryChange = () => {}) {
  const find = id => root.querySelector(`#${id}`);
  const escDb = escapeHtml;
  const runSelect = find('db-run');
  const timeInput = find('db-time');
  const status = find('db-status');
  const details = find('db-selection');
  const content = find('db-content');
  const form = find('db-form');
  let runs = [], snapshot = null, selection = null, metric = 'temp_c', history = null;
  let active = false, busy = false, runRequest = null, snapshotRequest = null, historyRequest = null;
  let delivery = null;

  function setDelivery(next) {
    delivery = next;
    find('db-delivery').hidden = !next;
    find('db-delivery-title').textContent = next?.label ?? '';
    find('db-delivery-description').textContent = next?.description ?? '';
    find('db-fleet-source').textContent = `${next?.label ?? 'Databricks evidence'} · synthetic`;
    onDeliveryChange(next);
  }

  function message(text, error = false) {
    status.textContent = text;
    status.classList.toggle('db-error', error);
    status.setAttribute('role', error ? 'alert' : 'status');
  }

  function setBusy(value) {
    busy = value;
    find('db-load').disabled = value || !runs.length;
    find('db-retry').disabled = value;
    runSelect.disabled = value || !runs.length;
    timeInput.disabled = value || !runs.length;
    root.setAttribute('aria-busy', String(value));
  }

  function chooseRun() {
    const run = runs.find(item => item.run_id === runSelect.value);
    if (!run) return;
    timeInput.min = new Date(run.start_ts).toISOString().slice(0, 19);
    timeInput.max = new Date(run.end_ts).toISOString().slice(0, 19);
    timeInput.value = new Date(Math.min(Date.parse(run.start_ts) + 10 * 60_000 + 45_000, Date.parse(run.end_ts))).toISOString().slice(0, 19);
    find('db-run-window').textContent = `Recorded ${new Date(run.start_ts).toISOString().slice(11, 19)}–${new Date(run.end_ts).toISOString().slice(11, 19)} UTC · ${dbNumber(run.sample_count, 0)} GPU samples`;
    snapshot = null; history = null; selection = null;
    content.hidden = true;
    message('Choose a replay time, then load the snapshot.');
  }

  async function loadRuns() {
    runRequest?.abort();
    snapshotRequest?.abort(); historyRequest?.abort();
    const controller = runRequest = new AbortController();
    snapshot = null; history = null; runs = [];
    content.hidden = true;
    setDelivery(null);
    setBusy(true);
    message('Loading the available Databricks replays…');
    try {
      const body = await requestDatabricks('runs', {}, controller.signal);
      if (controller.signal.aborted || !active) return;
      runs = parseReplayRuns(body);
      setDelivery(databricksPresentation(body));
      runSelect.replaceChildren(...runs.map((run, index) => new Option(`Replay ${index + 1} · ${run.run_id.slice(0, 8)}`, run.run_id)));
      if (!runs.length) { message('No replay data is available from this source.', true); return; }
      chooseRun();
      setBusy(false);
      await loadSnapshot();
    } catch (problem) {
      if (!controller.signal.aborted && active) message(`Could not load Databricks replays. ${problem.message}`, true);
    } finally {
      if (runRequest === controller && !snapshotRequest) setBusy(false);
    }
  }

  async function loadSnapshot() {
    if (!form.reportValidity()) return;
    const run = runs.find(item => item.run_id === runSelect.value);
    if (!run) return;
    const timestamp = Date.parse(`${timeInput.value}Z`);
    if (!Number.isFinite(timestamp) || timestamp < Date.parse(run.start_ts) || timestamp > Date.parse(run.end_ts)) {
      message('Choose a UTC time inside the recorded replay interval.', true); return;
    }
    const cursor = new Date(timestamp).toISOString();
    snapshotRequest?.abort(); historyRequest?.abort();
    const controller = snapshotRequest = new AbortController();
    snapshot = null; history = null;
    content.hidden = true;
    setBusy(true);
    message(delivery.loading);
    try {
      const body = await requestDatabricks('snapshot', { run_id: run.run_id, as_of_ts: cursor }, controller.signal);
      if (controller.signal.aborted || !active || snapshotRequest !== controller) return;
      snapshot = parseDatabricksSnapshot(body, run.run_id, cursor, delivery);
      content.hidden = false;
      renderSnapshot();
      message(`Loaded ${snapshot.as_of_ts} · ${snapshot.overview.observed_gpu_count} of ${snapshot.overview.total_gpu_count} GPUs observed. Findings cover the selected analysis window.`);
    } catch (problem) {
      if (!controller.signal.aborted && active && snapshotRequest === controller) {
        snapshot = null;
        content.hidden = true;
        message(`Could not load this snapshot. ${problem.message}`, true);
      }
    } finally {
      if (snapshotRequest === controller) { snapshotRequest = null; setBusy(false); }
    }
  }

  function renderSnapshot() {
    const overview = snapshot.overview;
    find('db-metrics').innerHTML = [
      ['Observed GPUs', `${dbNumber(overview.observed_gpu_count, 0)} / ${dbNumber(overview.total_gpu_count, 0)}`, '', `${dbNumber(overview.unknown_gpu_count, 0)} with unknown telemetry`],
      ['Observed GPU power', dbNumber(overview.observed_gpu_power_kw, 2), 'kW', 'Reporting GPUs only · not facility power'],
      ['Observed mean temperature', dbNumber(overview.observed_avg_temp_c), '°C', `${dbNumber(overview.observed_avg_gpu_util_pct)}% mean utilisation`],
      ['Detector escalations', dbNumber(snapshot.incidents.length, 0), '', 'Findings in the selected analysis window'],
    ].map(([label, value, unit, detail]) => `<div class="metric-cell"><h3>${escDb(label)}</h3><div class="metric-value">${escDb(value)}<small>${escDb(unit)}</small></div><p class="metric-detail">${escDb(detail)}</p></div>`).join('');
    find('db-fleet-summary').textContent = `${snapshot.racks.length} racks · ${overview.total_gpu_count} GPUs · native inventory IDs`;
    find('db-racks').innerHTML = snapshot.racks.map(rack => {
      const reporting = rack.gpus.filter(gpu => typeof gpu.metrics.power_w === 'number' && Number.isFinite(gpu.metrics.power_w));
      const power = reporting.length ? reporting.reduce((sum, gpu) => sum + gpu.metrics.power_w, 0) / 1000 : null;
      return `<article class="db-rack">
      <div class="db-rack-head"><h3>${escDb(rack.label ?? rack.id)}</h3><span class="mono">${escDb(rack.id)}</span></div>
      <p class="db-rack-summary">${escDb(dbNumber(power, 2))} kW observed GPU power · ${reporting.length}/${rack.gpus.length} reporting</p>
      <div class="db-gpu-grid">${rack.gpus.map(gpu => `<button type="button" class="db-gpu" data-db-gpu="${escDb(gpu.id)}" data-state="${escDb(gpu.state)}" aria-pressed="${gpu.id === selection}">
        <strong>${escDb(gpu.id)}</strong><span>${escDb(gpu.vendor)}</span><span>${escDb(dbStateLabels[gpu.state])}</span><small>${escDb(dbNumber(gpu.metrics.temp_c))} °C · ${escDb(dbNumber(gpu.metrics.power_w))} W</small>
      </button>`).join('')}</div></article>`;
    }).join('');
    find('db-incidents').innerHTML = snapshot.incidents.length ? snapshot.incidents.map(incident => `<article class="db-incident" data-severity="${escDb(incident.severity)}">
      <div class="db-incident-heading"><span class="db-badge">${escDb(incident.severity)}</span><h3>${escDb(incident.rootEntity)}</h3></div>
      <p>${escDb(incident.message)}</p><p class="mono db-quiet">Methods: ${escDb((incident.detectors ?? []).join(' · '))}</p>
      <details><summary>Supporting observations (${incident.findings?.length ?? 0})</summary><ul>${(incident.findings ?? []).map(finding => `<li><strong>${escDb(finding.entity)} · ${escDb(finding.metric)}</strong><p>${escDb(finding.text)}</p><small>${escDb(finding.method)} · ${escDb(finding.start)}–${escDb(finding.end)}</small></li>`).join('')}</ul></details>
    </article>`).join('') : `<p class="db-empty">No findings met the detector’s escalation rules in this analysis window. This does not establish that every device is healthy.</p>`;
    find('db-evidence-window').textContent = `${snapshot.window?.start ?? 'Unknown'} → ${snapshot.window?.end ?? snapshot.as_of_ts}`;
    find('db-persistence').hidden = false;
    find('db-persistence').textContent = databricksPersistenceText(snapshot);
    find('db-persistence').dataset.delivery = delivery.mode;
    find('db-limitations').replaceChildren(...(snapshot.limitations ?? []).map(text => { const item = document.createElement('li'); item.textContent = text; return item; }));
    find('db-coverage-note').textContent = 'These are detector observations, not confirmed causes. Maintenance and intervention records are not supplied by this dataset, so those explanations cannot be excluded.';
    const gpus = snapshot.racks.flatMap(rack => rack.gpus);
    if (!gpus.some(gpu => gpu.id === selection)) selection = gpus.find(gpu => ['critical', 'warning', 'unknown'].includes(gpu.state))?.id ?? gpus[0]?.id;
    renderSelection();
    if (selection) void loadHistory();
  }

  function selectedGpu() { return snapshot?.racks.flatMap(rack => rack.gpus).find(gpu => gpu.id === selection); }

  function renderSelection() {
    const gpu = selectedGpu();
    if (!gpu) { details.innerHTML = '<p class="db-empty">Select a GPU to inspect its readings.</p>'; return; }
    for (const button of find('db-racks').querySelectorAll('[data-db-gpu]')) button.setAttribute('aria-pressed', String(button.dataset.dbGpu === selection));
    details.innerHTML = `<div class="db-selection-head"><h3>${escDb(gpu.id)}</h3><span class="db-badge" data-state="${escDb(gpu.state)}">${escDb(dbStateLabels[gpu.state])}</span></div>
      <p class="db-quiet">${escDb(gpu.vendor)} · ${escDb(gpu.model ?? 'Synthetic GPU')} · ${escDb(gpu.server_id ?? '')}</p>
      <dl class="db-gpu-facts"><div><dt>GPU power</dt><dd>${escDb(dbNumber(gpu.metrics.power_w))} W</dd></div><div><dt>Temperature</dt><dd>${escDb(dbNumber(gpu.metrics.temp_c))} °C</dd></div><div><dt>Utilisation</dt><dd>${escDb(dbNumber(gpu.metrics.gpu_util_pct))}%</dd></div><div><dt>Cooling zone</dt><dd>${escDb(gpu.cooling_zone_id ?? 'Unknown')}</dd></div></dl>
      <p class="mono db-quiet">Last GPU reading: ${escDb(gpu.telemetry_ts ?? 'Unavailable')}</p>
      <dl class="db-gpu-facts db-context-facts"><div><dt>Workload</dt><dd>${escDb(gpu.workload?.state ?? 'unknown')}</dd></div><div><dt>Job progress</dt><dd>${escDb(dbNumber(gpu.workload?.progress_units, 0))} units</dd></div><div><dt>Cooling flow</dt><dd>${escDb(dbNumber(gpu.cooling?.flow_pct))}%</dd></div><div><dt>Inlet temperature</dt><dd>${escDb(dbNumber(gpu.cooling?.inlet_temp_c))} °C</dd></div></dl>
      <p class="db-quiet">Illustrative limits: ${escDb(dbNumber(gpu.limits?.temperature_c))} °C · ${escDb(dbNumber(gpu.limits?.power_w))} W. Workload and cooling observations have their own freshness checks.</p>
      <label class="db-metric-control" for="db-metric">Five-minute history<select id="db-metric">${Object.entries(dbMetricOptions).map(([key, value]) => `<option value="${key}"${metric === key ? ' selected' : ''}>${escDb(value.label)} (${escDb(value.unit)})</option>`).join('')}</select></label>
      <div id="db-history">${history ? dbChart(history, metric, snapshot.as_of_ts) : '<p class="db-empty" role="status">Loading GPU history…</p>'}</div>
      <p class="db-quiet">Gaps indicate unknown readings. GPU colours summarise findings in the analysis window, not a confirmed diagnosis.</p>`;
  }

  async function loadHistory() {
    const gpu = selectedGpu();
    if (!gpu || !snapshot) return;
    historyRequest?.abort();
    const controller = historyRequest = new AbortController();
    const id = gpu.id, cursor = snapshot.as_of_ts, runId = snapshot.run_id;
    history = null;
    renderSelection();
    try {
      const body = await requestDatabricks(`gpus/${encodeURIComponent(id)}/history`, { run_id: runId, as_of_ts: cursor, window_minutes: '5' }, controller.signal);
      if (controller.signal.aborted || !active || historyRequest !== controller || selection !== id || snapshot?.as_of_ts !== cursor) return;
      if (body?.source !== 'databricks' || !Array.isArray(body.data) || body.run_id !== runId || Date.parse(body.as_of_ts) !== Date.parse(cursor)
          || body.data.some(row => row.gpu_id !== id || Date.parse(row.event_ts) > Date.parse(cursor))) throw new Error('The data service returned history for an unexpected selection.');
      assertDatabricksDelivery(body, delivery);
      history = body.data;
      renderSelection();
    } catch (problem) {
      if (!controller.signal.aborted && active && historyRequest === controller && selection === id) {
        const holder = find('db-history');
        if (holder) { holder.textContent = `History unavailable. ${problem.message}`; holder.className = 'db-empty db-error'; }
      }
    }
  }

  form.addEventListener('submit', event => { event.preventDefault(); if (!busy) void loadSnapshot(); });
  runSelect.addEventListener('change', chooseRun);
  timeInput.addEventListener('change', () => { snapshot = null; history = null; content.hidden = true; message('Replay time changed. Load the snapshot to update the readings.'); });
  find('db-retry').addEventListener('click', () => { if (!busy) void loadRuns(); });
  find('db-racks').addEventListener('click', event => {
    const button = event.target.closest('[data-db-gpu]');
    if (!button || !snapshot) return;
    selection = button.dataset.dbGpu;
    void loadHistory();
  });
  details.addEventListener('change', event => {
    if (event.target.id !== 'db-metric' || !dbMetricOptions[event.target.value]) return;
    metric = event.target.value;
    renderSelection();
    find('db-metric')?.focus({ preventScroll: true });
  });

  return {
    presentation() { return delivery; },
    enter() { active = true; if (!runs.length) void loadRuns(); else if (!snapshot) void loadSnapshot(); else if (selection && !history) void loadHistory(); },
    leave() { active = false; runRequest?.abort(); snapshotRequest?.abort(); historyRequest?.abort(); snapshotRequest = null; setBusy(false); },
  };
}

// Kept on one line for the standalone build's module inliner.
export { mountDatabricksView, parseReplayRuns, parseDatabricksSnapshot, historySegments, dbNumber, requestDatabricks, databricksPresentation, assertDatabricksDelivery, databricksPersistenceText };
