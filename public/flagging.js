// Client for the deterministic telemetry flagger (flagging/bridge.py via /api/flagging/*).
// Flagger output, device names and log lines are connector data: always escape before rendering.

const flaggingTimeoutMs = 95_000;
const severityRank = { impacted: 1, stale: 2, warning: 3, critical: 4 };
const flagStateNames = {
  critical: 'Flagged · critical', warning: 'Flagged · warning', stale: 'Telemetry stale',
  impacted: 'In blast radius', nodata: 'No telemetry', nominal: 'No flags',
};
// Metrics shown first in the inspector chart, by how much they say about a device's health.
const metricPriority = [
  'gpu.temperature_c', 'gpu.sm_clock_mhz', 'gpu.utilization_percent', 'gpu.power_w', 'gpu.memory_temperature_c',
  'gpu.ecc_sbe_total', 'gpu.pcie_replay_total', 'gpu.pcie_rx_throughput_gbps',
  'port.crc_errors_total', 'port.effective_ber', 'port.symbol_errors_total',
  'pdu.load_percent_of_capacity', 'pdu.active_power_w', 'pdu.current_a',
  'cdu.flow_lpm', 'cdu.return_temp_c', 'cdu.supply_temp_c', 'cdu.differential_pressure_kpa',
  'room.inlet_temp_c', 'switch.temperature_c', 'nic.temperature_c',
];

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]);
}

async function requestFlagging(path, params = {}, signal) {
  const query = new URLSearchParams(params).toString();
  const timeout = AbortSignal.timeout(flaggingTimeoutMs);
  const response = await fetch(`./api/flagging/${path}${query ? `?${query}` : ''}`, {
    signal: signal ? AbortSignal.any([signal, timeout]) : timeout,
    headers: { Accept: 'application/json' },
  });
  let body = null;
  try { body = await response.json(); } catch { /* handled below */ }
  if (!response.ok || !body || body.error) throw new Error(body?.error ?? `Flagger unavailable (${response.status})`);
  if (body.schemaVersion !== '1.0.0') throw new Error('Unsupported flagger schema');
  return body;
}

const fetchFlagCatalog = signal => requestFlagging('catalog', {}, signal);
const fetchFlagAnalysis = (scenario, signal) => requestFlagging('analysis', { scenario }, signal);
const fetchFlagEntity = (scenario, entity, signal) => requestFlagging('entity', { scenario, entity }, signal);

function worstState(states) {
  return states.reduce((worst, state) => (severityRank[state] ?? 0) > (severityRank[worst] ?? 0) ? state : worst, null);
}

function seriesKey(series) {
  return `${series.entity}|${series.metric}`;
}

function sortSeries(series) {
  const rank = metric => {
    const index = metricPriority.indexOf(metric);
    return index === -1 ? metricPriority.length : index;
  };
  return [...series].filter(item => item.points.length > 1)
    .sort((a, b) => rank(a.metric) - rank(b.metric) || a.entity.localeCompare(b.entity));
}

const isoSeconds = iso => Date.parse(iso) / 1000;
const shortMetric = metric => metric.replace(/^(gpu|port|pdu|cdu|room|switch|nic|crac|chiller|node|bmc|exporter)\./, '');

function compactValue(value) {
  if (typeof value !== 'number') return String(value);
  if (value !== 0 && (Math.abs(value) < 1e-3 || Math.abs(value) >= 1e7)) return value.toExponential(2);
  return Number.isInteger(value) ? value.toLocaleString('en-GB') : value.toLocaleString('en-GB', { maximumFractionDigits: 2 });
}

// Draws one telemetry series up to the replay cursor, with declared limits and flagged spans.
function drawFlagChart(context, width, height, { series, limits = [], spans = [], window, cursor, stroke }) {
  const plot = { left: 46, right: width - 10, top: 14, bottom: height - 22 };
  const start = isoSeconds(window.start);
  const end = isoSeconds(window.end);
  const x = t => plot.left + (t - start) / Math.max(1, end - start) * (plot.right - plot.left);
  const points = series.points.map(([t, value, quality]) => [isoSeconds(t), value, quality]);
  const shown = points.filter(([t]) => t <= cursor);
  const usable = points.filter(([, , quality]) => quality === 'ok');
  const values = [...usable.map(([, value]) => value), ...limits.map(limit => limit.value)];
  const logScale = series.metric === 'port.effective_ber' && values.every(value => value > 0);
  const scaled = value => logScale ? Math.log10(value) : value;
  const scaledValues = values.map(scaled);
  let low = Math.min(...scaledValues);
  let high = Math.max(...scaledValues);
  if (!Number.isFinite(low)) { low = 0; high = 1; }
  const pad = Math.max((high - low) * .12, Math.abs(high) * .01, logScale ? .5 : .5);
  low -= pad;
  high += pad;
  const y = value => plot.bottom - (scaled(value) - low) / (high - low) * (plot.bottom - plot.top);

  context.font = '9px monospace';
  context.fillStyle = '#647470';
  for (let i = 0; i <= 3; i++) {
    const level = low + i * (high - low) / 3;
    const pos = plot.bottom - i * (plot.bottom - plot.top) / 3;
    stroke(context, [[plot.left, pos], [plot.right, pos]], '#e4ebe3');
    context.textAlign = 'right';
    context.fillText(compactValue(logScale ? 10 ** level : level), plot.left - 5, pos + 3);
  }
  context.textAlign = 'left';
  context.fillText(new Date(start * 1000).toISOString().slice(11, 16), plot.left, plot.bottom + 15);
  context.textAlign = 'right';
  context.fillText(new Date(end * 1000).toISOString().slice(11, 16), plot.right, plot.bottom + 15);

  for (const span of spans) {
    const from = Math.max(start, isoSeconds(span.start));
    const to = Math.min(cursor, isoSeconds(span.end) + (series.points.length > 1 ? 0 : 60));
    if (to < from) continue;
    context.fillStyle = span.severity === 'critical' ? '#a63f2824' : '#c9733f22';
    context.fillRect(x(from), plot.top, Math.max(2, x(to) - x(from)), plot.bottom - plot.top);
  }
  for (const limit of limits) {
    const critical = limit.kind.startsWith('critical');
    const pos = y(limit.value);
    if (pos < plot.top - 1 || pos > plot.bottom + 1) continue;
    stroke(context, [[plot.left, pos], [plot.right, pos]], critical ? '#a63f28' : '#c9733f', 1, [4, 3]);
    context.fillStyle = critical ? '#a63f28' : '#9b5a2f';
    context.textAlign = 'left';
    context.fillText(`${critical ? 'critical' : 'warn'} ${compactValue(limit.value)}`, plot.left + 3, pos - 3);
  }
  // Unknown is not zero: stale or invalid samples break the line and are drawn as hollow marks.
  let run = [];
  const flush = () => {
    if (run.length > 1) stroke(context, run, '#28735a', 1.6);
    else if (run.length === 1) {
      context.beginPath();
      context.arc(...run[0], 1.8, 0, Math.PI * 2);
      context.fillStyle = '#28735a';
      context.fill();
    }
    run = [];
  };
  for (const [t, value, quality] of shown) {
    if (quality !== 'ok') {
      flush();
      context.beginPath();
      context.arc(x(t), Math.min(plot.bottom, Math.max(plot.top, y(value))), 2.4, 0, Math.PI * 2);
      context.strokeStyle = '#86968d';
      context.lineWidth = 1;
      context.stroke();
      continue;
    }
    run.push([x(t), y(value)]);
  }
  flush();
  if (cursor < end) {
    stroke(context, [[x(cursor), plot.top], [x(cursor), plot.bottom]], '#417c62', 1, [2, 2]);
  }
  return { logScale, shown: shown.length };
}

// Kept on one line: scripts/build.mjs inlines modules by matching a single-line export list.
export { escapeHtml, fetchFlagCatalog, fetchFlagAnalysis, fetchFlagEntity, worstState, flagStateNames, severityRank, seriesKey, sortSeries, isoSeconds, shortMetric, compactValue, drawFlagChart };
