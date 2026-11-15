import { duration, entities, runComparison, checkpointAt } from './twin.js';
import { attributeStragglers, decodeXid, getActionGate, planMaintenance, resetActionGate, triageFleet } from './operations.js';
import { escapeHtml as esc } from './flagging.js';

// Operator console for the action gate. Plain-English labels; detail lives in the exported ledger.
const statusText = {
  'awaiting-approval': 'Needs approval', rejected: 'Blocked', declined: 'Declined', scheduled: 'Approved · waiting', executed: 'Running',
  verified: 'Worked · kept', inconclusive: 'Result unclear', 'rolled-back': 'Undone', cancelled: 'Cancelled', recorded: 'Drafted',
  'needs-operator': 'Needs a decision',
};
const stageOrder = ['Proposed', 'Checked', 'Approved', 'Applied', 'Confirmed'];
const label = id => entities.find(item => item.id === id)?.label ?? id;
// "gpu-a01-03" → "GPU 3 in rack A01"
const gpuName = id => id?.startsWith('gpu-') ? `GPU ${Number(id.slice(-2))} in rack ${id.slice(4, 7).toUpperCase()}` : label(id);
const money = value => `£${Number(value).toFixed(2)}`;
const fmt = (value, digits = 0) => value === null || value === undefined ? '—'
  : new Intl.NumberFormat('en-GB', { maximumFractionDigits: digits, minimumFractionDigits: digits }).format(value);
const bad = ['rejected', 'declined', 'rolled-back', 'cancelled', 'needs-operator'];
const minuteText = tick => `minute ${tick}`;
const listText = items => items.length < 2 ? items.join('') : `${items.slice(0, -1).join(', ')} and ${items.at(-1)}`;

function stages(proposal) {
  const reached = { rejected: 1, declined: 2, 'awaiting-approval': 2, scheduled: 3, cancelled: 3, recorded: 3,
    executed: 4, inconclusive: 4, 'rolled-back': 4, 'needs-operator': 4, verified: 5 }[proposal.status] ?? 0;
  const failedAt = bad.includes(proposal.status) ? reached : -1;
  return stageOrder.map((name, i) => `<li class="${failedAt === i ? 'failed' : i < reached ? 'done' : i === reached ? 'current' : ''}">${name}</li>`).join('');
}

// What the action does, in one sentence.
function describe(proposal) {
  if (proposal.type === 'drain_gpu') return proposal.source.kind === 'straggler'
    ? `Replace the slow ${gpuName(proposal.gpuId)} with a spare` : `Take the faulty ${gpuName(proposal.gpuId)} out of service`;
  if (proposal.type === 'open_rma') return `Request a replacement for ${gpuName(proposal.gpuId)}`;
  if (proposal.type === 'power_cap') return `Limit training GPUs to ${Math.round(proposal.cap * 100)}% power`;
  if (proposal.type === 'maintenance_hold') return `Clear ${label(proposal.entityId)} for maintenance`;
  const racks = proposal.prediction?.racks ?? [];
  const serve = racks.reduce((sum, rack) => sum + rack.serveAfter - rack.serveBefore, 0);
  const trainTo = racks.filter(rack => rack.trainAfter > rack.trainBefore).map(rack => rack.rack);
  const parts = [
    serve > 0 ? `give the chat service ${serve} more GPU${serve > 1 ? 's' : ''}` : serve < 0 ? `free ${-serve} GPU${serve < -1 ? 's' : ''} from the chat service` : null,
    trainTo.length ? `move training onto rack ${listText(trainTo)}` : null,
    proposal.plan?.cap < 1 ? `limit training power to ${Math.round(proposal.plan.cap * 100)}%` : null,
  ].filter(Boolean);
  const text = parts.length ? listText(parts) : 'rearrange which GPUs run which job';
  return text[0].toUpperCase() + text.slice(1);
}

// Before → after for each result that changes; the measured value appears once it is checked.
function impact(proposal) {
  const p = proposal.prediction;
  if (!p) return '';
  const rows = [
    ['Chat response time', 'p95Ms', ' ms', 0, 'limit 160 ms'],
    ['Training speed', 'trainingSamplesPerSecond', ' samples/s', 0, ''],
    ['Power use', 'powerKw', ' kW', 1, ''],
  ].flatMap(([name, key, unit, digits, note]) => {
    const [a, b] = [p.before[key], p.after[key]];
    const measured = proposal.measured?.[key];
    if (a === b && measured === undefined) return [];
    return [`<li><span>${name}${note ? ` <small>(${note})</small>` : ''}</span><span class="mono">${fmt(a, digits)} → ${fmt(b, digits)}${unit}${measured !== undefined
      ? ` <b>· actual ${fmt(measured, digits)}</b>` : ''}</span></li>`];
  });
  if (p.atlasMoves) rows.push(`<li><span>Training restart cost</span><span class="mono">${money(p.interruption.cost)}</span></li>`);
  if (p.reassigned) rows.push(`<li><span>GPUs switching jobs</span><span class="mono">${p.reassigned}</span></li>`);
  return rows.length ? `<ul class="ops-impact">${rows.join('')}</ul>` : '<p class="ops-empty">No change to speed, response time or power</p>';
}

// What happens next, so the operator knows to move the clock forward.
function nextStep(proposal, gate) {
  if (proposal.status === 'awaiting-approval') return `If approved, starts at ${minuteText(Math.max(proposal.effectiveTick, gate.now))}.`;
  if (proposal.status === 'scheduled') return `Starts at ${minuteText(proposal.effectiveTick)}. Step forward to see it.`;
  if (proposal.status === 'executed') return `Result is checked at ${minuteText(proposal.verifyAt)}. Step forward to see it.`;
  if (proposal.status === 'verified') return 'Measured result matched the prediction, so the change was kept.';
  if (proposal.status === 'rolled-back') return proposal.rolledBackAt !== undefined && proposal.history ? 'Undone. The previous setup is back.' : 'Undone.';
  if (proposal.status === 'needs-operator') return 'It did not work, but undoing it would be unsafe. You decide.';
  if (proposal.status === 'inconclusive') return proposal.confoundedBy ? `A separate fault (${gpuName(proposal.confoundedBy[0])}) muddied the result. Not undone.` : 'Data is stale, so success is not claimed. Rechecked each minute.';
  return '';
}

const warnText = check => check.id === 'checkpoint' ? 'Training restarts between checkpoints, so some work is lost'
  : `${check.label} was already failing before this change`;

function proposalCard(proposal, gate, viewTick) {
  const live = viewTick === gate.now;
  const blocks = (proposal.checks ?? []).filter(check => check.severity === 'block');
  const warns = (proposal.checks ?? []).filter(check => check.severity === 'warn' && !check.ok);
  return `<article class="ops-card${bad.includes(proposal.status) ? ' muted' : ''}">
    <header class="ops-card-head">
      <h3>${esc(describe(proposal))}</h3>
      <span class="badge ${bad.includes(proposal.status) ? 'warning' : proposal.status === 'inconclusive' ? 'unknown' : ''}">${esc(statusText[proposal.status] ?? proposal.status)}</span>
    </header>
    <ol class="ops-stages" aria-label="Progress">${stages(proposal)}</ol>
    ${impact(proposal)}
    ${blocks.length ? `<p class="ops-why bad">Blocked: ${blocks.map(check => esc(check.label)).join(' · ')}</p>` : ''}
    ${warns.length ? `<p class="ops-why">Note: ${warns.map(check => esc(warnText(check))).join(' · ')}</p>` : ''}
    <p class="ops-next">${esc(nextStep(proposal, gate))}</p>
    ${proposal.ticket ? `<details class="ops-ticket"><summary>View replacement request</summary><pre>${esc(JSON.stringify(proposal.ticket, null, 2))}</pre></details>` : ''}
    <div class="ops-card-actions">
      ${proposal.status === 'awaiting-approval' ? `<button type="button" class="primary-button" data-approve="${esc(proposal.id)}" ${!live || gate.killSwitch ? 'disabled' : ''}>Approve</button>
        <button type="button" class="secondary-button" data-decline="${esc(proposal.id)}" ${live ? '' : 'disabled'}>Decline</button>` : ''}
      ${['scheduled', 'executed', 'verified', 'inconclusive', 'needs-operator'].includes(proposal.status) && proposal.type !== 'open_rma'
        ? `<button type="button" class="secondary-button" data-rollback="${esc(proposal.id)}" ${live ? '' : 'disabled'}>Undo</button>` : ''}
      <span class="ops-id mono">${esc(proposal.id)} · from ${esc(proposal.source.name)}</span>
    </div>
  </article>`;
}

// Restart cost now vs at the next checkpoint, recommended option highlighted.
function timingLine(timing) {
  if (!timing) return '';
  return `<p class="ops-timing"><span class="ops-timing-label">Training restart cost</span>${timing.options.map((option, i) =>
    `<span class="${option.tick === timing.recommendedTick ? 'chosen' : ''}">${i === 0 ? 'now' : `at ${minuteText(option.tick)}`}: ${money(option.cost)}</span>`).join('')}</p>`;
}

function triagePanel(snapshot, live, verdicts) {
  const results = triageFleet(snapshot, { verdicts });
  const body = results.map(result => {
    const codes = [...result.events.reduce((map, event) => map.set(event.code, (map.get(event.code) ?? 0) + 1), new Map())]
      .map(([code, count]) => `${decodeXid(code).name} (XID ${code}${count > 1 ? `, ${count} times` : ''})`);
    const facts = [...codes, result.counters?.eccUncorrected.value ? `${result.counters.eccUncorrected.value} uncorrectable memory error` : null,
      result.counters?.remapPending ? 'memory repair waiting for a reset' : null, result.model ? `health model says ${result.model.label}` : null].filter(Boolean);
    const steps = result.steps.map(step => `<span class="${step.done ? 'done' : ''}">${{ cordon: 'Stop new work', drain: 'Move its work off', reset: 'Reset GPU', rma: 'Request replacement', monitor: 'Keep watching' }[step.id]}</span>`).join('');
    return `<article class="ops-finding">
      <header><button type="button" class="ops-link" data-select="${esc(result.gpuId)}">${esc(gpuName(result.gpuId))}</button>
        <span class="badge ${result.urgency === 'immediate' ? 'critical' : result.verdict === 'unknown' ? 'unknown' : 'warning'}">${esc({ immediate: 'act now', 'next-checkpoint': 'act at next checkpoint', monitor: 'watch', unknown: 'unknown · no data', none: 'healthy' }[result.urgency])}</span></header>
      <p class="ops-facts-line mono" title="${esc(result.reasons.join(' '))}">${esc(facts.join(' · '))}</p>
      <p class="ops-steps">${steps}</p>
      ${timingLine(result.timing)}
      <div class="ops-card-actions">${result.proposals.map((proposal, i) => `<button type="button" class="secondary-button" data-submit="triage" data-index="${i}" data-gpu="${esc(result.gpuId)}" ${live ? '' : 'disabled'}>
        ${proposal.type === 'drain_gpu' ? `Propose: take it out at ${minuteText(proposal.notBeforeTick)}` : 'Propose: request a replacement'}</button>`).join('')}</div>
    </article>`;
  }).join('') || '<p class="ops-empty">All GPUs healthy</p>';
  return `<section class="panel ops-panel" aria-labelledby="ops-triage-title"><div class="panel-head"><h2 id="ops-triage-title">GPU health</h2>
      <span class="ops-model-chip mono" title="Fine-tuned GPU health model: telemetry in, stable / warning / critical out">${verdicts.size ? 'Health model on' : 'Health model not connected'}</span></div>
    <div class="ops-panel-body">${body}
      <form class="ops-decoder" data-decoder><input id="ops-xid" inputmode="numeric" pattern="[0-9]{1,3}" maxlength="3" placeholder="XID" aria-label="NVIDIA XID error code"><button type="submit" class="secondary-button">Explain error code</button>
        <span id="ops-xid-result" aria-live="polite"></span></form>
    </div></section>`;
}

function stragglerPanel(snapshot, live) {
  const result = attributeStragglers(snapshot);
  const causeText = { compute: 'is overheating and running slowly', fabric: 'have a slow network link', power: 'are power-limited' };
  const headline = {
    straggler: () => `${result.limiting.length === 1 ? gpuName(result.limiting[0]) : `${result.limiting.length} GPUs`} ${causeText[result.cause?.layer] ?? 'are slow'}, so the whole training job is ${Math.round(result.throughputCost * 100)}% slower`,
    halted: () => `Training has stopped: ${result.haltReason === 'rank-failed' ? 'one of its GPUs failed' : result.haltReason === 'world-size' ? 'not enough GPUs' : 'restarting'}`,
    uniform: () => `No single slow GPU. All training GPUs are ${Math.round((1 - result.pace) * 100)}% slower together`,
    balanced: () => 'All training GPUs are running at full speed',
    uncertain: () => 'Unknown: some readings are out of date',
  }[result.verdict]();
  const slow = [...result.ranks].sort((a, b) => a.speed - b.speed).filter(rank => rank.speed < .99).slice(0, 3);
  return `<section class="panel ops-panel" aria-labelledby="ops-straggler-title"><div class="panel-head"><h2 id="ops-straggler-title">Slowest training GPU</h2></div>
    <div class="ops-panel-body">
      <p class="ops-headline${result.verdict === 'straggler' || result.verdict === 'halted' ? ' attention' : ''}">${esc(headline)}</p>
      ${result.verdict === 'straggler' ? `<ul class="ops-ranks">${slow.map(rank => `<li><button type="button" class="ops-link" data-select="${esc(rank.gpuId)}">${esc(gpuName(rank.gpuId))}</button>
        <span class="ops-bar"><i style="width:${Math.max(4, Math.round(rank.speed * 100))}%" class="slow"></i></span><span class="mono">${Math.round(rank.speed * 100)}% speed</span></li>`).join('')}</ul>` : ''}
      ${timingLine(result.timing)}
      ${result.mitigation ? `<div class="ops-card-actions"><button type="button" class="secondary-button" data-submit="straggler" ${live ? '' : 'disabled'}>Propose: swap it for a spare at ${minuteText(result.mitigation.notBeforeTick)}</button></div>` : ''}
    </div></section>`;
}

const maintenanceTargets = [
  ['Power', ['grid-01', 'pdu-a', 'pdu-b']], ['Cooling', ['cdu-a', 'cdu-b']],
  ['Fabric', ['sw-spine', 'leaf-a01', 'leaf-a02', 'leaf-b01', 'leaf-b02']],
  ['Racks', ['rack-a01', 'rack-a02', 'rack-b01', 'rack-b02']],
  ['GPUs', entities.filter(item => item.type === 'gpu').map(item => item.id)],
];
const verdictText = { 'safe-now': 'Safe to switch off now', 'safe-with-evacuation': 'Safe once its work is moved', unsafe: 'Not safe: not enough spare capacity',
  unknown: 'Unknown: sensor data is out of date', 'already-held': 'Already switched off' };

function maintenancePanel(snapshot, control, target, live) {
  let plan;
  try {
    plan = planMaintenance({ entityId: target, snapshot, control });
  } catch (cause) {
    plan = { error: cause.message };
  }
  const options = maintenanceTargets.map(([group, ids]) => `<optgroup label="${group}">${ids.map(id =>
    `<option value="${id}" ${id === target ? 'selected' : ''}>${esc(gpuName(id))}</option>`).join('')}</optgroup>`).join('');
  const tone = { unsafe: 'critical', 'safe-with-evacuation': 'warning', unknown: 'unknown' }[plan.verdict] ?? '';
  const spof = [...new Set(plan.redundancy?.singlePointsOfFailure.map(item => item.kind) ?? [])];
  return `<section class="panel ops-panel" aria-labelledby="ops-maint-title"><div class="panel-head"><h2 id="ops-maint-title">What if I switch this off?</h2></div>
    <div class="ops-panel-body">
      <span class="select-wrap"><select id="ops-maint-target" aria-label="Equipment to switch off">${options}</select><i aria-hidden="true"></i></span>
      ${plan.error ? `<p class="ops-why bad">${esc(plan.error)}</p>` : `
      <p class="ops-headline"><span class="badge ${tone}">${esc(verdictText[plan.verdict])}</span></p>
      <dl class="ops-facts">
        <div><dt>GPUs lost</dt><dd>${fmt(plan.affected.gpus.length)}</dd></div>
        <div><dt>Training GPUs hit</dt><dd>${fmt(plan.affected.atlasRanks)}</dd></div>
        <div><dt>Chat GPUs hit</dt><dd>${fmt(plan.affected.relayReplicas)}</dd></div>
        <div><dt>GPUs to move first</dt><dd>${fmt(plan.evacuation?.gpusReassigned ?? 0)}</dd></div>
      </dl>
      ${plan.verdict === 'unsafe' && plan.unprepared.newlyFailing.length ? `<p class="ops-why bad">Would break: ${esc(plan.unprepared.newlyFailing.join(' · '))}</p>` : ''}
      ${spof.length && plan.verdict !== 'already-held' ? `<p class="ops-why">No backup ${esc(listText(spof))} supply for these racks</p>` : ''}
      ${timingLine(plan.timing)}
      ${plan.proposal ? `<div class="ops-card-actions"><button type="button" class="secondary-button" data-submit="maintenance" ${live ? '' : 'disabled'}>Propose: move work off and switch off at ${minuteText(plan.proposal.notBeforeTick)}</button></div>` : ''}`}
    </div></section>`;
}

function ledgerRows(ledger) {
  return [...ledger].reverse().slice(0, 60).map(entry => `<tr title="${esc(entry.detail)}"><td class="mono">m${entry.tick}</td>
    <td><span class="ops-event ${esc(entry.event)}">${esc(entry.event)}</span></td><td class="mono">${esc(entry.proposalId ?? '—')}</td><td>${esc(entry.actor.name)}</td></tr>`).join('');
}

function mountOperationsView(root, { context, setTick, changed }) {
  const ui = { target: null, message: '', operator: 'on-call', ledgerOpen: false };
  // Health-model verdicts by GPU id. Empty until the fine-tuned model is connected.
  const verdicts = new Map();
  const gate = () => getActionGate(context().scenario);
  const proposalSource = {
    triage: button => {
      const result = triageFleet(context().snapshot, { verdicts }).find(item => item.gpuId === button.dataset.gpu);
      return [result?.proposals[Number(button.dataset.index)], { kind: 'triage', name: 'xid-triage' }];
    },
    straggler: () => [attributeStragglers(context().snapshot).mitigation, { kind: 'straggler', name: 'straggler' }],
    maintenance: () => [planMaintenance({ entityId: ui.target, snapshot: context().snapshot, control: context().control }).proposal, { kind: 'planner', name: 'maintenance' }],
    optimizer: () => {
      const { plan } = runComparison(context().scenario);
      return [plan && { type: 'placement', plan, rationale: 'Coordinated optimizer placement.' }, { kind: 'optimizer', name: 'optimizer' }];
    },
  };

  // Only errors are shown; successful actions speak through the card and ledger.
  function act(fn) {
    ui.message = '';
    try {
      fn();
    } catch (cause) {
      if (!(cause instanceof RangeError)) throw cause;
      ui.message = cause.message;
    }
    changed();
  }

  function render() {
    const { scenario, tick, snapshot, control } = context();
    const g = gate();
    const live = tick === g.now;
    const state = g.state();
    if (!ui.target) ui.target = { 'gpu-xid': 'gpu-a01-03', straggler: 'gpu-a02-05', network: 'leaf-b02', thermal: 'cdu-b' }[scenario] ?? 'leaf-b01';
    const next = checkpointAt(tick).next;
    const open = state.proposals.filter(item => item.status === 'awaiting-approval').length;
    root.innerHTML = `
      <div class="panel ops-toolbar">
        <div class="ops-clock"><strong>Minute ${tick}</strong><span>${next === tick ? 'Training saves a checkpoint now' : `Next training checkpoint at minute ${next}`}${live ? '' : ' · viewing the past (read-only)'}</span></div>
        <div class="action-group">
          <button type="button" class="secondary-button" data-step="-1" aria-label="Back one minute" ${tick === 0 ? 'disabled' : ''}>← 1 min</button>
          <button type="button" class="secondary-button" data-step="1" aria-label="Forward one minute" ${tick === duration - 1 ? 'disabled' : ''}>1 min →</button>
          <button type="button" class="secondary-button" data-jump ${next >= duration || next === tick ? 'disabled' : ''}>Jump to checkpoint</button>
        </div>
        <input id="ops-operator" class="ops-operator" maxlength="40" value="${esc(ui.operator)}" aria-label="Operator name" pattern="[A-Za-z0-9 ._:/-]+">
        <button type="button" class="kill-switch${g.killSwitch ? ' engaged' : ''}" data-kill aria-pressed="${g.killSwitch}" ${live ? '' : 'disabled'}>■ ${g.killSwitch ? 'Resume changes' : 'Stop all changes'}</button>
      </div>
      ${ui.message ? `<p class="ops-why bad ops-banner" role="alert">${esc(ui.message)}</p>` : ''}
      <div class="ops-grid">
        <div class="ops-main">
          <section class="panel ops-panel" aria-labelledby="ops-gate-title">
            <div class="panel-head"><h2 id="ops-gate-title">Proposed changes${open ? ` <span class="ops-count" title="Waiting for approval">${open}</span>` : ''}</h2>
              <button type="button" class="secondary-button" data-submit="optimizer" ${live ? '' : 'disabled'}>Ask the optimizer for a better layout</button></div>
            <div class="ops-panel-body ops-cards">${state.proposals.length ? [...state.proposals].reverse().map(item => proposalCard(item, g, tick)).join('')
              : '<p class="ops-empty">Nothing proposed yet. Use a Propose button on the right.</p>'}</div>
          </section>
          <details class="panel ops-panel ops-ledger-panel" ${ui.ledgerOpen ? 'open' : ''}>
            <summary class="panel-head"><h2>History log <span class="ops-count">${state.ledger.length}</span></h2>
              <span class="action-group"><button type="button" class="secondary-button" data-export>Export</button><button type="button" class="secondary-button" data-reset>Reset</button></span></summary>
            <div class="ops-ledger-wrap"><table class="ops-table ops-ledger"><tbody>${ledgerRows(state.ledger) || '<tr><td>Empty</td></tr>'}</tbody></table></div>
          </details>
        </div>
        <aside class="ops-aside">
          ${triagePanel(snapshot, live, verdicts)}
          ${stragglerPanel(snapshot, live)}
          ${maintenancePanel(snapshot, control, ui.target, live)}
        </aside>
      </div>`;
  }

  root.addEventListener('click', event => {
    const button = event.target.closest('button');
    if (!button || !root.contains(button)) return;
    if (button.closest('summary')) event.preventDefault();
    const g = gate();
    const { tick } = context();
    const operator = ui.operator;
    if (button.dataset.step) setTick(Math.min(duration - 1, Math.max(0, tick + Number(button.dataset.step))));
    else if (button.hasAttribute('data-jump')) setTick(checkpointAt(tick).next);
    else if (button.dataset.select) changed({ select: button.dataset.select });
    else if (button.dataset.submit) {
      const [proposal, source] = proposalSource[button.dataset.submit](button);
      act(() => { if (proposal) g.submit(proposal, { tick, source }); else throw new RangeError('Nothing to propose'); });
    } else if (button.dataset.approve) act(() => g.approve(button.dataset.approve, { tick, operator }));
    else if (button.dataset.decline) act(() => g.decline(button.dataset.decline, { tick, operator }));
    else if (button.dataset.rollback) act(() => g.rollback(button.dataset.rollback, { tick, operator }));
    else if (button.hasAttribute('data-kill')) act(() => g.setKillSwitch(!g.killSwitch, { tick, operator }));
    else if (button.hasAttribute('data-reset')) act(() => resetActionGate(context().scenario));
    else if (button.hasAttribute('data-export')) {
      const url = URL.createObjectURL(new Blob([JSON.stringify(g.state(), null, 2)], { type: 'application/json' }));
      const link = Object.assign(document.createElement('a'), { href: url, download: `action-ledger-${context().scenario}.json` });
      document.body.append(link);
      link.click();
      link.remove();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    }
  });
  root.addEventListener('submit', event => {
    event.preventDefault();
    if (!event.target.matches('[data-decoder]')) return;
    const code = Number(root.querySelector('#ops-xid').value);
    const out = root.querySelector('#ops-xid-result');
    if (!Number.isInteger(code) || code < 1 || code > 999) { out.textContent = ''; return; }
    const entry = decodeXid(code);
    out.textContent = `${entry.name}: ${entry.cause}.`;
    out.title = entry.cause;
  });
  root.addEventListener('toggle', event => {
    if (event.target.matches?.('.ops-ledger-panel')) ui.ledgerOpen = event.target.open;
  }, true);
  root.addEventListener('change', event => {
    if (event.target.id === 'ops-maint-target') {
      ui.target = event.target.value;
      changed();
    }
    if (event.target.id === 'ops-operator') ui.operator = event.target.value.trim() || 'on-call';
  });

  return {
    render,
    resetTarget() { ui.target = null; ui.message = ''; },
    // Hook for the fine-tuned health model: pass { [gpuId]: { label, confidence, model } }. Invalid entries are ignored.
    setHealthVerdicts(map) { verdicts.clear(); for (const [id, value] of Object.entries(map ?? {})) verdicts.set(id, value); },
  };
}

export { mountOperationsView };
