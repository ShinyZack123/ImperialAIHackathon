export function diagnoseGpu(gpu, finding) {
  if (!gpu?.telemetry_is_observed) {
    return { kind: 'unknown', label: 'Telemetry unknown' };
  }

  const { temp_c: temperature, power_w: power } = gpu.metrics;

  if (Number.isFinite(temperature) && Number.isFinite(gpu.limits.temperature_c) &&
      temperature >= gpu.limits.temperature_c) {
    return {
      kind: 'fault',
      label: 'Overheating',
      detail: Number(temperature.toFixed(1)) + ' °C at a ' + gpu.limits.temperature_c + ' °C temperature limit.',
      metric: 'temperature',
    };
  }
  if (power === 0 && gpu.workload?.is_fresh && gpu.workload.state === 'running') {
    return { kind: 'fault', label: 'Power lost', detail: 'The GPU reports 0 W while its job is running.', metric: 'power' };
  }
  if (finding) {
    return {
      kind: 'fault',
      label: finding.metric === 'gpu.ecc_uncorrected_total' ? 'GPU memory error' : 'GPU finding',
      detail: finding.text,
    };
  }
  if (gpu.state === 'warning' || gpu.state === 'critical') {
    return { kind: 'warning', label: 'Affected by a shared issue' };
  }
  return { kind: 'clear', label: 'No findings' };
}
