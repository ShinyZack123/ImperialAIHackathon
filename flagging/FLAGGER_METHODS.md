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

Findings are merged by what they are on and when they began. Sharing a workload alone is not enough: two faults at different times on one job are two incidents.

1. Findings on the same component, parent server or redundancy group merge when their time spans touch.
2. Those clusters merge when one depends on the other in the topology graph (GPU → server → NIC → leaf switch → spine, cooled_by → CDU, powered_by → PDU → UPS), their spans touch and the downstream one did not begin first. They also merge when they hit the same workload and began together (one cause, many devices).
3. A workload symptom (slow steps, stalled progress) joins the cluster on that workload that began most recently before it.

"Began together" and "touch" mean within `correlate_onset_gap_s` (15 min), or three sampling periods for sparse data.

The root cause is what went wrong first. Findings that began more than `root_onset_tolerance_s` (5 min) after the first hardware/facility evidence are treated as effects. Among the ones that began together, the layer decides: facility (cooling/power), then network/storage, then device, then workload symptom.

## Incident identity and status

`flag_window` is pure and recomputes from scratch, so each incident carries what a caller needs to avoid re-escalating it:

- `id`: hash of root entity and onset. Same input, same ID.
- `onset`, `last_seen`: earliest and latest supporting evidence.
- `status`: `active` or `recovered`. Recovered needs positive evidence: every flagged condition was observed clear for `recovered_after_s` (5 min). Discrete events (XID, uncorrectable-error increment, job failed) latch, and a series that went silent is unknown, so both stay active.

`IncidentTracker().update(incidents)` keeps the ID stable across successive calls on a growing or sliding window (it matches by root and overlapping entities) and sets `change` to `new`, `escalated`, `rerooted`, `recovered`, `recurred` or `unchanged`. Only incidents with `escalate` true need to go to the agent again. On S01 that is 2 messages over 24 cut-offs instead of 17.

## Optimisation opportunities (healthy capacity doing no work)

`find_opportunities(context, telemetry, now=None, incidents=())` is a separate output from flags. It never changes flag / no-flag: B10 is still "no flag", and it also yields one opportunity.

A GPU is listed when valid utilisation stays under `idle_util_percent` (5%) for `persist_s`, right up to the evaluation time, and it is:

| kind | meaning | proposal |
|---|---|---|
| `reclaimable_gpus` | still allocated to a job that has finished | release the allocation |
| `reserved_idle_gpus` | held for a job that has not started | start it, or lend the GPUs until it does |
| `unallocated_idle_gpus` | allocated to nothing | available for placement |

Each opportunity has the GPUs, the workload, idle GPU-hours, and idle power and energy when every GPU reports power. Idle GPUs of a running job are not listed (that is a possible fault, detector 7). Unknown is never idle: stale or missing samples, entities in maintenance, and anything that depends on an entity in an active incident are left out. The proposal is text for the optimiser or agent, not a command.

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
- The 15-minute correlation gap and 5-minute root tolerance are hand-set too. Two unrelated faults that begin within 15 minutes on the same job are still merged into one incident, and if they begin within 5 minutes the layer order picks the root.
- Opportunities cover idle GPUs only. Placement overhead and power headroom are not detected: the fixtures have no declared targets to compare against.
- Thresholds (z > 6, 5-min persistence, p < 1e-6) are hand-set. They are all in `config.json`. Hardware limits are not there: they come from the metric catalog in `topology.enriched.json`.
- The fresh scenarios share `gen.py`'s noise model, so they show the rules are not over-fitted to the 23 files, not that they hold on real data.
