# How the flagger decides what to escalate

`flagger.py` takes the topology and a window of telemetry. It returns incidents, each with a plain-text `message` for the low-level agent. It is deterministic: the same input always gives the same output, with no ML model and no randomness.

**Result:** all 23 labelled scenarios are correct (12/12 faults flagged, 0/11 benign flagged), and each names the right root-cause entity. On 34 fresh generated scenarios (new seeds, entities, timings and magnitudes) it is 340/340 over 10 noise seeds, after three rule fixes found by that check (see `datacentre-scenarios/WORKLOG.md`).

Run from `datacentre-scenarios/`:
- `python3 build_context.py` builds `topology.enriched.json` (limits, measured baselines, interventions). The flagger reads this file.
- `python3 test_scenarios.py` runs one test per labelled scenario and writes the agent messages to `flagger_results.json`.
- `python3 generalise.py` runs the fresh-scenario check. `python3 demo.py` replays a fault and its benign twin side by side.

## The pipeline (the line for the demo)

> Validate → de-spike → 8 statistical detectors → context gates → graph correlation → one message per root cause.

## The 8 detectors, in plain English

| # | Method | Plain English | Catches |
|---|---|---|---|
| 1 | **Robust z-score (median/MAD)** | How unusual is this value vs normal? It uses median and "median absolute deviation" instead of mean and standard deviation, so outliers in the baseline don't distort it. Flags at z > 6. | F01, F03, F04, F08, F10 (step time, coolant flow, latency, storage latency) |
| 2 | **Spec / SLA thresholds** | Hard limits taken from the entity itself: the GPU's own `thermalLimitC`, the workload's `latencySlaMs`, the PDU at 80%, BER (bit error rate) > 1e-12, and PCIe negotiated below rated. | F01, F05, F08, F09 |
| 3 | **Peer comparison (cross-sectional robust z)** | GPUs in the same job do identical work, so they should look identical. Each GPU is compared to the median of its siblings at the same instant. This removes everything common-mode (job start, checkpoint, time of day). | F01 (one hot GPU), F07 (one hung GPU at 0%) |
| 4 | **CUSUM** (cumulative sum control chart) | Adds up small deviations over time, so a slow creep raises an alarm before any threshold is crossed. Applied to "this GPU minus its peers", so room temperature cycles cancel out. | F12 (3-day drift that never crosses a limit) |
| 5 | **Poisson rate test** | Error counters have a normal background rate. "Is getting N errors in 5 minutes plausible if the normal rate is λ?" Flags when p < 1e-6. | F02 (ECC burst), F03 (CRC errors), F09 (PCIe replays). Correctly ignores B06 (4 errors/day) |
| 6 | **Never-normal events** | Some signals are always bad: a double-bit ECC error, XID 48/79, a breaker trip, a pump fault, a job failing, hardware throttling. | F02, F04, F05, F08 |
| 7 | **Progress heartbeat** | The job's step counter must advance. If it's flat for more than 10 step-times while the scheduler says "running", the job is stalled. A checkpoint is allowed, but only up to 3x its expected length. | F07 (silent hang), F10 (stuck checkpoint) |
| 8 | **Telemetry health** | Exporter up/down, stale-marked samples, and frozen values (a gauge that used to move and now repeats exactly). "No news" isn't treated as "good news". | F06 |

**Hampel filter** (pre-processing): any single sample far from its neighbours' median is replaced before detection. Combined with a **persistence window** (5 min and 3+ samples), one-off glitches never fire. This is why B03 and B05 pass.

## Context gates (why the benign twins don't fire)

| Gate | Plain English | Saves |
|---|---|---|
| Maintenance window | Suppress if the entity, its parent, or the device at the other end of its cable is in an active maintenance record (+15 min grace) for the finding's whole duration | B04, B09 (and still flags F11, which is outside its window) |
| Own intervention | Suppress a slowdown or software throttle only while a logged `Intervention` record is active for that entity, and only up to the effect size it predicted | B11 |
| Workload phase | A checkpoint within 3x its expected duration counts as a pause, not a stall | B01 (and still flags F10) |
| Workload status | Idle after `completed` is expected | B10 (and still flags F07) |
| Software vs hardware throttle | `SW_POWER_CAP` is requested; `HW_*` is a fault | B11 vs F01 |

Benign cases that pass with no gate at all, because the statistics alone handle them: B02 (peer comparison cancels the common rise), B03 (under the limit, too short), B05 (invalid sample, Hampel, persistence), B06 (Poisson says normal), B07 (inside the ASHRAE band, the industry's recommended air-temperature range), B08 (all GPUs move together).

## Correlation (one message per root cause, not 30 alerts)

Findings are merged when they share an entity, an affected workload, a redundancy group or a parent server. Workloads are found by walking the topology graph (GPU → server → NIC → leaf switch → spine, cooled_by → CDU, powered_by → PDU → UPS). The root cause is chosen by layer: facility (cooling/power), then network/storage, then device, then workload symptom.

Example output (F03):
```
[WARNING] leaf-12-p07 (port7): port.crc_errors_total increased by 4960 in 5 min vs ~5 expected (Poisson p<1e-300).
Corroborating evidence:
  - leaf-12-p07 (port7): port.symbol_errors_total increased by 208000 in 5 min ... [Poisson rate test on error counter]
  - training-job-104: workload.step_time_ms rose from baseline 2402 to 2764 (+15%, robust z=20) for 27 min [robust z-score ...]
  - training-job-104: workload.comm_overhead_percent rose from baseline 13.85 to 24.1 (+74%, robust z=17) ...
Why flagged: Poisson rate test on error counter.
Context: Link leaf-12-p07 <-> spine-01-p01 via cable-leaf12-p07; if the far end is clean, suspect the cable/optic.
Affected workloads: training-job-104 (training, priority 70, status running).
```

## Honest caveats

- **The test set is easier than production.** I wrote both the scenarios and the detectors, so 23/23 shows the logic works, not that it generalises. Test it on real DCGM/UFM data before claiming accuracy.
- **Baselines are stored, but synthetic.** They are measured from fault-free runs made by `generate.py` (one synthetic day), conditioned on workload state, hour of day and traffic. The first 25% of the window is now only a fallback when no stored baseline exists. Production needs baselines measured from real telemetry.
- Thresholds (z > 6, 5-min persistence, p < 1e-6) are hand-set. They are all in `config.json`. Hardware limits are not there: they come from the metric catalog in `topology.enriched.json`.
- The fresh scenarios share `gen.py`'s noise model, so they show the rules are not over-fitted to the 23 files, not that they hold on real data.
