#!/usr/bin/env python3
"""Step 2: fill the schema gaps listed in SCHEMA_SUGGESTIONS.md.

Reads  topology.json            (original, never modified)
Writes topology.enriched.json   (topology + the missing context the flagger needs)

What gets added (plain English):
  metricCatalog     one MetricDefinition per metric name: unit, kind (gauge = a reading,
                    counter = a running total that only goes up, event = something that
                    happened once, state = an on/off or text status), hard limits,
                    plausible range (for spotting impossible sensor readings) and
                    whether counters reset on reboot.
  baselines         MetricBaseline list: what "normal" looks like (mean, spread) for a
                    metric, conditioned on what the entity is doing (idle vs loaded,
                    training vs serving, hour of day). MEASURED from fault-free runs made
                    by generate.py, not typed in. Includes median and MAD (median absolute
                    deviation, a spread measure that one bad sample can't skew).
  counterRateBaselines  how fast each error counter climbs in normal operation.
  workloads[]       + phase, checkpointPolicy, progress, expectedUtilizationPercent,
                    replica layout for inference.
  interventions[]   our own deliberate actions with an active window and predicted effects.
  maintenance[]     already present; validated against the MaintenanceRecord shape.
  ports[]           + adminState ("up" | "down" | "reserved"): a port that is meant to be
                    unplugged should not alarm when its link is down.
  redundancyGroups  what "healthy" means for A/B power: how many members must be up.

Limits are facts about hardware, not detector settings. Detector settings (z-score
cut-offs, persistence windows, time budgets) live in config.json (step 4).

Deterministic: no randomness, fixed key order, so reruns give byte-identical output.
"""
import copy, json, math, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "topology.json")
OUT = os.path.join(HERE, "topology.enriched.json")
COMPUTED_AT = "2026-11-10T00:00:00Z"   # baselines computed before every scenario window
SCHEMA_VERSION = "0.2.0-enriched"

topo = json.load(open(SRC))
t = copy.deepcopy(topo)


def all_entities(d):
    keys = ["buildings", "rooms", "racks", "devices", "components", "powerSystems",
            "coolingSystems", "storageSystems", "clusters"]
    for k in keys:
        for e in d[k]:
            yield e


ENT = {e["id"]: e for e in all_entities(t)}
PORTS = {p["id"]: p for e in ENT.values() for p in e.get("ports", [])}
WL = {w["id"]: w for w in t["workloads"]}

# ---------------------------------------------------------------- metric catalog
# limits:   hard bounds. A value can be a number or {"fromEntity": "<dotted path>", "default": n}
#           so the rule reads the real limit off the entity (e.g. thermal.thermalLimitC = 87).
#           "warn"/"critical" are upper bounds; "warnBelow"/"criticalBelow" are lower bounds.
# plausibleRange: anything outside is physically impossible -> sensor glitch, not a fault.
#           maxDeltaPerSecond: faster change than this is also treated as a glitch (B05).
# rate:     for counters, how fast they normally climb (per hour) and what climb rate
#           is a hard limit. A counter that is merely non-zero is fine (B06).

def lim_from(path, default):
    return {"fromEntity": path, "default": default}


G = "gauge"; C = "counter"; E = "event"; S = "state"
catalog = {
    # --- GPU (NVIDIA DCGM) ---
    "gpu.temperature_c": dict(unit="C", kind=G, samplingIntervalSeconds=30,
        limits={"warn": 83, "critical": lim_from("thermal.thermalLimitC", 87)},
        plausibleRange={"min": 5, "max": 110, "maxDeltaPerSecond": 1.0},
        minConsecutiveSamples=3, baselineRef="gpu.temperature_c"),
    "gpu.memory_temperature_c": dict(unit="C", kind=G, samplingIntervalSeconds=30,
        limits={"warn": 85, "critical": 95},  # HBM3 slowdown ~95C
        plausibleRange={"min": 5, "max": 120, "maxDeltaPerSecond": 1.0}, minConsecutiveSamples=3,
        baselineRef="gpu.memory_temperature_c"),
    "gpu.power_w": dict(unit="W", kind=G, samplingIntervalSeconds=30,
        limits={"warn": lim_from("power.limitWatts", 700)},  # board limit; GPU self-caps at it
        plausibleRange={"min": 0, "max": 1000}, minConsecutiveSamples=6,
        note="Short excursions near the limit during big matrix ops are normal (B03). "
             "The live limit may be lowered by an intervention: see gpu.power_limit_w.",
        baselineRef="gpu.power_w"),
    "gpu.power_limit_w": dict(unit="W", kind=S, samplingIntervalSeconds=30,
        note="Configured power cap. A change here should match an Intervention record."),
    "gpu.sm_clock_mhz": dict(unit="MHz", kind=G, samplingIntervalSeconds=30,
        plausibleRange={"min": 0, "max": 2100}, minConsecutiveSamples=3, baselineRef="gpu.sm_clock_mhz"),
    "gpu.utilization_percent": dict(unit="%", kind=G, samplingIntervalSeconds=30,
        plausibleRange={"min": 0, "max": 100}, minConsecutiveSamples=3,
        note="100% utilisation does NOT prove progress: NCCL spin-wait shows 100% while stalled (F07).",
        baselineRef="gpu.utilization_percent"),
    "gpu.throttle_reasons": dict(unit=None, kind=S, samplingIntervalSeconds=30,
        valueClasses={
            "none": "ok",
            "SW_POWER_CAP": "software",            # we asked for it (power cap) -> check Intervention
            "SW_THERMAL_SLOWDOWN": "hardware",     # driver slowing down because it's hot
            "HW_THERMAL_SLOWDOWN": "hardware",     # GPU itself slowing down because it's hot
            "HW_POWER_BRAKE": "hardware",          # external power-brake signal asserted
            "HW_SLOWDOWN": "hardware",
            "SYNC_BOOST": "ok", "APPLICATION_CLOCKS": "ok"},
        note="software = intentional/explainable; hardware = a fault until proven otherwise."),
    "gpu.ecc_sbe_total": dict(unit="count", kind=C, samplingIntervalSeconds=30, resetsOnReboot=False,
        rate={"warnPerHour": 30},
        note="Single-bit (corrected) memory errors. A few per day is normal (B06); a burst is a precursor (F02)."),
    "gpu.ecc_dbe_total": dict(unit="count", kind=C, samplingIntervalSeconds=30, resetsOnReboot=False,
        rate={"criticalOnAnyIncrease": True},
        note="Double-bit (uncorrectable) errors corrupt data. Any increase is critical."),
    "gpu.retired_pages_total": dict(unit="count", kind=C, samplingIntervalSeconds=3600, resetsOnReboot=False,
        rate={"warnOnAnyIncrease": True}),
    "gpu.remapped_rows_pending": dict(unit="bool", kind=S, samplingIntervalSeconds=3600,
        badValues=[True], note="True = memory row remap needs a GPU reset to take effect."),
    "gpu.xid": dict(unit="code", kind=E, samplingIntervalSeconds=0,
        eventCodes={
            "13": {"severity": "warning", "meaning": "Graphics engine exception (often an application bug)"},
            "31": {"severity": "warning", "meaning": "GPU memory page fault"},
            "43": {"severity": "warning", "meaning": "GPU stopped processing"},
            "48": {"severity": "critical", "meaning": "Double-bit ECC error (uncorrectable memory error)"},
            "63": {"severity": "info", "meaning": "ECC page retirement / row remap recorded"},
            "64": {"severity": "warning", "meaning": "ECC page retirement / row remap failed"},
            "74": {"severity": "critical", "meaning": "NVLink error"},
            "79": {"severity": "critical", "meaning": "GPU has fallen off the bus (no longer reachable)"},
            "92": {"severity": "warning", "meaning": "High single-bit ECC error rate"},
            "94": {"severity": "warning", "meaning": "Contained ECC error"},
            "95": {"severity": "critical", "meaning": "Uncontained ECC error"}},
        defaultEventSeverity="warning"),
    "gpu.pcie_link_gen": dict(unit="gen", kind=S, samplingIntervalSeconds=30,
        expectedFromEntity="connectivity.pcieGeneration",
        note="Lower than expected = link has down-trained (runs slower)."),
    "gpu.pcie_link_width": dict(unit="lanes", kind=S, samplingIntervalSeconds=30,
        expectedFromEntity="connectivity.pcieLanes"),
    "gpu.pcie_replay_total": dict(unit="count", kind=C, samplingIntervalSeconds=30, resetsOnReboot=True,
        rate={"warnPerHour": 600},
        note="PCIe replay = packet re-sent because it failed its checksum on the CPU<->GPU link."),
    "gpu.pcie_rx_throughput_gbps": dict(unit="GB/s", kind=G, samplingIntervalSeconds=30,
        plausibleRange={"min": 0, "max": 64}, baselineRef="gpu.pcie_rx_throughput_gbps"),
    # --- exporter / host / BMC ---
    "exporter.dcgm_up": dict(unit="bool", kind=S, samplingIntervalSeconds=30, badValues=[False],
        coversConnector="dcgm-prod-01", coversScope="children",
        note="Collector health. False = GPU readings for this server's children are not fresh, "
             "whatever their values say (F06)."),
    "node.up": dict(unit="bool", kind=S, samplingIntervalSeconds=60, badValues=[False]),
    "bmc.power_state": dict(unit=None, kind=S, samplingIntervalSeconds=60),
    "bmc.post_code": dict(unit=None, kind=E, samplingIntervalSeconds=0,
        note="Power-on self-test code. 'stuck' = server never finished booting."),
    # --- network ---
    "port.link_up": dict(unit="bool", kind=S, samplingIntervalSeconds=60, badValues=[False],
        note="Only a fault if the port's adminState is 'up' (B09)."),
    "port.rx_bytes_total": dict(unit="B", kind=C, samplingIntervalSeconds=60, resetsOnReboot=True),
    "port.crc_errors_total": dict(unit="count", kind=C, samplingIntervalSeconds=60, resetsOnReboot=True,
        rate={"warnPerHour": 600},
        note="CRC error = packet corrupted in transit and re-sent. Large static totals are history, not a fault."),
    "port.symbol_errors_total": dict(unit="count", kind=C, samplingIntervalSeconds=60, resetsOnReboot=True,
        rate={"warnPerHour": 60000}),
    "port.effective_ber": dict(unit="ratio", kind=G, samplingIntervalSeconds=60,
        limits={"warn": 1e-12, "critical": 1e-9},
        note="Bit error rate: fraction of bits received wrong. Healthy NDR links are ~1e-15."),
    "port.link_downed_total": dict(unit="count", kind=C, samplingIntervalSeconds=60, resetsOnReboot=True,
        rate={"warnOnAnyIncrease": True},
        note="Link flap counter: how many times the link went down and came back."),
    "switch.temperature_c": dict(unit="C", kind=G, samplingIntervalSeconds=1800,
        limits={"warn": 70, "critical": 80}, plausibleRange={"min": 5, "max": 110},
        baselineRef="switch.temperature_c"),
    # --- power ---
    "pdu.active_power_w": dict(unit="W", kind=G, samplingIntervalSeconds=60,
        limits={"warn": {"fromEntity": "electrical.capacityW", "default": 22000, "fraction": 0.8},
                "critical": {"fromEntity": "electrical.capacityW", "default": 22000, "fraction": 0.9}},
        plausibleRange={"min": 0, "max": 30000}, minConsecutiveSamples=2, baselineRef="pdu.active_power_w"),
    "pdu.current_a": dict(unit="A", kind=G, samplingIntervalSeconds=60,
        limits={"warn": 25.6, "critical": 28.8},   # 80% / 90% of a 32 A breaker
        plausibleRange={"min": 0, "max": 40}, baselineRef="pdu.current_a"),
    "pdu.load_percent_of_capacity": dict(unit="%", kind=G, samplingIntervalSeconds=60,
        limits={"warn": 80, "critical": 90}, plausibleRange={"min": 0, "max": 150}, minConsecutiveSamples=2,
        baselineRef="pdu.load_percent_of_capacity"),
    "pdu.input_status": dict(unit=None, kind=S, samplingIntervalSeconds=60,
        badValues=["breaker_tripped", "input_lost", "fault"]),
    "control.power_cap_change": dict(unit=None, kind=E, samplingIntervalSeconds=0,
        note="Audit event for a power-cap change. Carries labels.interventionId when we did it."),
    # --- cooling / facility ---
    "cdu.flow_lpm": dict(unit="L/min", kind=G, samplingIntervalSeconds=60,
        limits={"warnBelow": {"fromEntity": "thermal.flowRateLitersPerMinute", "default": 115, "fraction": 0.8},
                "criticalBelow": {"fromEntity": "thermal.flowRateLitersPerMinute", "default": 115, "fraction": 0.6}},
        plausibleRange={"min": 0, "max": 300}, minConsecutiveSamples=2, baselineRef="cdu.flow_lpm"),
    "cdu.supply_temp_c": dict(unit="C", kind=G, samplingIntervalSeconds=60,
        limits={"warn": 35, "critical": 40}, plausibleRange={"min": 5, "max": 70}, baselineRef="cdu.supply_temp_c"),
    "cdu.return_temp_c": dict(unit="C", kind=G, samplingIntervalSeconds=60,
        limits={"warn": 45, "critical": 50}, plausibleRange={"min": 5, "max": 80}, baselineRef="cdu.return_temp_c"),
    "cdu.differential_pressure_kpa": dict(unit="kPa", kind=G, samplingIntervalSeconds=60,
        limits={"warnBelow": 170, "criticalBelow": 130}, plausibleRange={"min": 0, "max": 500},
        baselineRef="cdu.differential_pressure_kpa"),
    "cdu.pump_a_status": dict(unit=None, kind=S, samplingIntervalSeconds=60, badValues=["fault", "stopped"]),
    "cdu.pump_b_status": dict(unit=None, kind=S, samplingIntervalSeconds=60, badValues=["fault", "stopped"]),
    "crac.supply_air_temp_c": dict(unit="C", kind=G, samplingIntervalSeconds=1800,
        limits={"warn": 24, "critical": 27}, plausibleRange={"min": 5, "max": 50}, baselineRef="crac.supply_air_temp_c"),
    "room.inlet_temp_c": dict(unit="C", kind=G, samplingIntervalSeconds=1800,
        limits={"warn": 27, "critical": 32, "warnBelow": 18},   # ASHRAE recommended 18-27C, allowable to 32C
        plausibleRange={"min": 0, "max": 50}, baselineRef="room.inlet_temp_c",
        note="ASHRAE = industry body whose guidelines set recommended hall air temperatures (18-27C)."),
    "room.humidity_percent": dict(unit="%", kind=G, samplingIntervalSeconds=1800,
        limits={"warn": 70, "warnBelow": 20}, plausibleRange={"min": 0, "max": 100}, baselineRef="room.humidity_percent"),
    "chiller.outdoor_temp_c": dict(unit="C", kind=G, samplingIntervalSeconds=1800,
        plausibleRange={"min": -30, "max": 50}, baselineRef="chiller.outdoor_temp_c",
        note="Outdoor weather: an explanatory driver, never a fault by itself."),
    # --- storage ---
    "storage.write_bytes_per_second": dict(unit="B/s", kind=G, samplingIntervalSeconds=30,
        limits={"critical": lim_from("attributes.maxWriteBytesPerSecond", 4e11)},
        plausibleRange={"min": 0, "max": 1e12}, baselineRef="storage.write_bytes_per_second"),
    "storage.metadata_latency_ms": dict(unit="ms", kind=G, samplingIntervalSeconds=30,
        limits={"warn": 100, "critical": 1000}, plausibleRange={"min": 0, "max": 600000},
        minConsecutiveSamples=3, baselineRef="storage.metadata_latency_ms"),
    # --- workloads ---
    "workload.phase": dict(unit=None, kind=S, samplingIntervalSeconds=30,
        note="See Workload.phase enum. Phase explains a dip only within its time budget."),
    "workload.status": dict(unit=None, kind=S, samplingIntervalSeconds=60,
        note="Scheduler status: queued | running | completed | failed | cancelled."),
    "workload.steps_completed": dict(unit="count", kind=C, samplingIntervalSeconds=60, resetsOnReboot=False,
        note="Progress heartbeat. Flat while status=running and phase=train = stalled (F07)."),
    "workload.step_time_ms": dict(unit="ms", kind=G, samplingIntervalSeconds=60,
        minConsecutiveSamples=3, baselineRef="workload.step_time_ms"),
    "workload.tokens_per_second": dict(unit="tokens/s", kind=G, samplingIntervalSeconds=60,
        minConsecutiveSamples=3, baselineRef="workload.tokens_per_second"),
    "workload.comm_overhead_percent": dict(unit="%", kind=G, samplingIntervalSeconds=60,
        plausibleRange={"min": 0, "max": 100}, minConsecutiveSamples=3, baselineRef="workload.comm_overhead_percent"),
    "workload.last_log_line_age_s": dict(unit="s", kind=G, samplingIntervalSeconds=60,
        limits={"warn": 300, "critical": 600}, baselineRef="workload.last_log_line_age_s",
        note="Seconds since the job last wrote a log line."),
    "workload.queue_wait_s": dict(unit="s", kind=G, samplingIntervalSeconds=300,
        note="How long a queued job has waited for its GPUs."),
    # --- inference serving ---
    "inference.requests_per_second": dict(unit="req/s", kind=G, samplingIntervalSeconds=30,
        plausibleRange={"min": 0, "max": 100000}, baselineRef="inference.requests_per_second",
        note="User demand: an explanatory driver (B08), not a fault by itself."),
    "inference.p99_latency_ms": dict(unit="ms", kind=G, samplingIntervalSeconds=30,
        limits={"critical": {"fromWorkload": "requirements.latencySlaMs", "default": 800}},
        minConsecutiveSamples=4, baselineRef="inference.p99_latency_ms",
        note="p99 = 99th percentile: 1 in 100 requests is slower than this."),
    "inference.ttft_ms": dict(unit="ms", kind=G, samplingIntervalSeconds=30,
        baselineRef="inference.ttft_ms", note="Time to first token."),
    "inference.queue_depth": dict(unit="requests", kind=G, samplingIntervalSeconds=30,
        baselineRef="inference.queue_depth"),
    "inference.error_rate": dict(unit="ratio", kind=G, samplingIntervalSeconds=30,
        limits={"warn": 0.01, "critical": 0.05}, minConsecutiveSamples=3, baselineRef="inference.error_rate"),
    "inference.healthy_replicas": dict(unit="count", kind=G, samplingIntervalSeconds=30,
        limits={"warnBelow": {"fromWorkload": "inference.desiredReplicas", "default": 2}},
        minConsecutiveSamples=2),
    "inference.replica_event": dict(unit=None, kind=E, samplingIntervalSeconds=0,
        defaultEventSeverity="warning"),
}
# stable order + explicit metric name inside each entry
t["metricCatalog"] = {m: dict({"metric": m}, **catalog[m]) for m in sorted(catalog)}

# ---------------------------------------------------------------- baselines
# Baselines are MEASURED, not typed in: we generate fault-free telemetry with generate.py
# (seeded, so repeatable), then summarise it. Nothing below knows the generator's set points.
#   - gauges: mean, median, stddev, MAD (median absolute deviation: the typical distance from
#     the median; one bad sample can't skew it), 5th/95th percentile, sample count
#   - conditioned on what the entity was doing at that moment (idle / loaded / checkpoint,
#     workload type, hour of day), read from the workload status/phase telemetry in the run
#   - demand model: where a metric follows user traffic, a straight-line fit against requests/s
#   - peer residual: a GPU's temperature minus the median of the other GPUs on the same job
#   - counters: how fast error counters climb in normal operation (per hour)
import bisect, statistics
from datetime import datetime, timezone
import generate

BASELINE_RUNS = [   # (preset, kwargs): all fault-free, dated before COMPUTED_AT
    ("normal",      dict(seed=9001, start="2026-11-09T00:00:00Z", minutes=1440, step=120)),  # a full day
    ("job_running", dict(seed=9002, start="2026-11-09T10:00:00Z", minutes=120, step=30)),    # fine-tune job loaded
]
MIN_SAMPLES = 10


def _ts(s): return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
def _r(x): return float(f"{x:.6g}")


def _stats(xs):
    xs = sorted(xs); n = len(xs); med = statistics.median(xs)
    q = lambda p: xs[min(n - 1, max(0, round(p * (n - 1))))]
    return {"mean": _r(statistics.fmean(xs)), "median": _r(med), "stddev": _r(statistics.pstdev(xs)),
            "mad": _r(statistics.median([abs(x - med) for x in xs])), "p05": _r(q(0.05)), "p95": _r(q(0.95)), "n": n}


def derive_baselines():
    groups, pairs, resid = {}, {}, {}        # key -> values ; key -> (driver, value) ; key -> residuals
    counters, hours_seen = {}, {}
    runs_meta = []
    for preset, kw in BASELINE_RUNS:
        sc = generate.build(preset, **kw)
        assert sc["expected"]["flag"] is False, "baseline run must be fault-free"
        recs = [r for r in sc["telemetry"] if r["quality"]["valid"] and not r["quality"]["stale"]]
        runs_meta.append({"scenario": preset, **kw, "records": len(recs)})
        hours = (_ts(sc["window"]["end"]) - _ts(sc["window"]["start"])) / 3600
        # state lookups from the telemetry itself: last known status / phase of each workload
        state = {}
        for r in recs:
            if r["metric"] in ("workload.status", "workload.phase"):
                state.setdefault((r["entityId"], r["metric"]), []).append((_ts(r["timestamp"]), r["value"]))
        def last(wid, metric, t):
            pts = state.get((wid, metric))
            if not pts: return None
            i = bisect.bisect_right(pts, (t, "￿")) - 1
            return pts[i][1] if i >= 0 else pts[0][1]
        rps = {(r["entityId"], r["timestamp"]): r["value"] for r in recs if r["metric"] == "inference.requests_per_second"}
        peers = {}
        for r in recs:
            eid, metric, v = r["entityId"], r["metric"], r["value"]
            d = catalog.get(metric)
            if not d or isinstance(v, bool) or not isinstance(v, (int, float)): continue
            t_ = _ts(r["timestamp"]); e = ENT.get(eid, {})
            if d["kind"] == C:
                if metric != "workload.steps_completed":
                    k = (e.get("type") or "port", metric)
                    c = counters.setdefault((k, eid, preset), [v, v]); c[1] = v
                    hours_seen[(k, eid, preset)] = hours
                continue
            if d["kind"] != G: continue
            wid = (r.get("labels") or {}).get("workloadId")
            if e.get("type") == "gpu":
                sel = {"entityType": "gpu", "model": e["model"]}
                status, phase = last(wid, "workload.status", t_), last(wid, "workload.phase", t_)
                if wid is None or (status is not None and status != "running"): cond = {"workloadState": "idle"}
                else: cond = {"workloadType": WL[wid]["type"], "workloadState": "checkpoint" if phase == "checkpoint" else "loaded"}
                if metric == "gpu.temperature_c" and cond["workloadState"] == "loaded":
                    peers.setdefault((wid, r["timestamp"]), []).append(v)
            elif eid in WL and metric.startswith("workload."):
                sel, cond = {"entityIds": [eid]}, {"phase": last(eid, "workload.phase", t_) or "train"}
            elif metric == "storage.write_bytes_per_second":
                busy = any(eid in (w_["allocation"].get("storageIds") or []) and last(w_["id"], "workload.phase", t_) == "checkpoint"
                           for w_ in t["workloads"])
                sel, cond = {"entityIds": [eid]}, {"activity": "checkpoint_burst" if busy else "background"}
            elif metric.split(".")[0] in ("room", "crac", "chiller") or metric == "switch.temperature_c":
                sel, cond = {"entityIds": [eid]}, {"hourOfDay": datetime.fromtimestamp(t_, timezone.utc).hour}
            else:
                sel, cond = {"entityIds": [eid]}, {}
            key = (metric, json.dumps(sel, sort_keys=True), json.dumps(cond, sort_keys=True))
            groups.setdefault(key, []).append(v)
            drv_wl = wid if e.get("type") == "gpu" else eid
            if (drv_wl, r["timestamp"]) in rps and metric != "inference.requests_per_second":
                pairs.setdefault(key, []).append((rps[(drv_wl, r["timestamp"])], v))
        for (wid, _), vals in peers.items():
            if len(vals) < 4: continue
            k = WL[wid]["type"]
            for i, v in enumerate(vals):
                resid.setdefault(k, []).append(v - statistics.median(vals[:i] + vals[i + 1:]))

    out = []
    def add(metric, sel, cond, body):
        parts = [metric] + [f"{k}={v}" for k, v in sorted(sel.items())] + [f"{k}={v}" for k, v in sorted(cond.items())]
        out.append({"id": "bl:" + "|".join(str(p) for p in parts), "metric": metric, "selector": sel, "conditionedOn": cond,
                    **body, "windowDays": 1, "computedAt": COMPUTED_AT,
                    "source": "measured from fault-free generated runs (see baselineRuns)"})
    for key in sorted(groups):
        vals = groups[key]
        if len(vals) < MIN_SAMPLES: continue
        metric, sel, cond = key[0], json.loads(key[1]), json.loads(key[2])
        body = _stats(vals)
        pr = pairs.get(key, [])
        if len(pr) >= MIN_SAMPLES and len({x for x, _ in pr}) > 5:
            xs, ys = [x for x, _ in pr], [y for _, y in pr]
            mx, my = statistics.fmean(xs), statistics.fmean(ys)
            sxx = sum((x - mx) ** 2 for x in xs)
            slope = sum((x - mx) * (y - my) for x, y in pr) / sxx if sxx else 0.0
            icpt = my - slope * mx
            res = [y - (icpt + slope * x) for x, y in pr]
            syy = sum((y - my) ** 2 for y in ys)
            r2 = 1 - sum(e_ ** 2 for e_ in res) / syy if syy else 0.0
            if r2 >= 0.5:   # the metric really does follow demand
                mres = statistics.median(res)
                body["demandModel"] = {"driverMetric": "inference.requests_per_second", "intercept": _r(icpt), "slope": _r(slope),
                                       "r2": _r(r2), "residualMad": _r(statistics.median([abs(e_ - mres) for e_ in res])),
                                       "note": "expected value = intercept + slope * requests/s"}
        add(metric, sel, cond, body)
    for wtype in sorted(resid):
        add("gpu.temperature_c", {"entityType": "gpu", "model": "H100 SXM5 80GB"},
            {"workloadType": wtype, "relativeTo": "workload_peers"},
            dict(_stats(resid[wtype]), note="value minus the median of the other GPUs on the same workload at the same instant"))
    rates = {}
    for (k, eid, preset), (first, lastv) in counters.items():
        a_ = rates.setdefault(k, [0.0, 0.0, 0]); a_[0] += max(0, lastv - first); a_[1] += hours_seen[(k, eid, preset)]; a_[2] += 1
    rate_bl = [{"id": f"rate:{metric}|entityType={etype}", "metric": metric, "selector": {"entityType": etype},
                "ratePerHour": _r(inc / hrs) if hrs else 0.0, "entityHours": _r(hrs), "entities": n_,
                "computedAt": COMPUTED_AT, "source": "measured from fault-free generated runs (see baselineRuns)"}
               for (etype, metric), (inc, hrs, n_) in sorted(rates.items())]
    return out, rate_bl, runs_meta


baselines, rate_baselines, runs_meta = derive_baselines()
t["baselines"] = baselines
t["counterRateBaselines"] = rate_baselines
t["baselineRuns"] = runs_meta

# ---------------------------------------------------------------- workloads: phase etc.
PHASES = ["pending", "starting", "train", "eval", "checkpoint", "serving", "finishing", "finished", "failed"]
t["workloadPhaseEnum"] = {
    "pending": "Queued, no GPUs running it yet.",
    "starting": "Loading model/data; GPUs warming up. Load ramps are expected.",
    "train": "Normal training steps. Full load expected.",
    "eval": "Evaluation pass. Load may be lower.",
    "checkpoint": "Writing a checkpoint to storage. GPUs mostly idle, storage write burst expected.",
    "serving": "Inference serving. Load follows user demand.",
    "finishing": "Draining / writing final outputs.",
    "finished": "Completed. GPUs expected idle.",
    "failed": "Crashed or killed.",
}

w = WL["training-job-104"]
w["phase"] = "train"
w["phaseSince"] = "2026-11-13T23:58:00Z"
w["checkpointPolicy"] = {"mode": "synchronous", "intervalSteps": w["attributes"]["checkpointIntervalSteps"],
                         "expectedDurationSeconds": w["attributes"]["expectedCheckpointDurationSeconds"],
                         "lastCheckpointStep": 41000, "lastCheckpointAt": "2026-11-13T23:58:00Z",
                         "storageId": "storage-01"}
w["progress"] = {"stepsCompleted": w["training"]["stepsCompleted"], "stepsTotal": w["training"]["stepsTotal"],
                 "lastStepAt": "2026-11-14T00:00:00Z", "expectedStepTimeMs": w["training"]["stepTimeMs"]}
w["expectedUtilizationPercent"] = {"starting": [0, 100], "train": [90, 100], "checkpoint": [0, 15],
                                   "eval": [40, 100], "finishing": [0, 100], "finished": [0, 5]}

w = WL["inference-svc-207"]
w["phase"] = "serving"
w["phaseSince"] = "2026-10-01T00:05:00Z"
w["inference"]["desiredReplicas"] = 2
w["inference"]["replicas"] = [
    {"id": "replica-0", "gpuIds": [f"gpu-13-01-{g:02d}" for g in range(0, 4)]},
    {"id": "replica-1", "gpuIds": [f"gpu-13-01-{g:02d}" for g in range(4, 8)]}]
w["expectedUtilizationPercent"] = {"serving": [5, 95]}

w = WL["finetune-job-311"]
w["phase"] = "pending"
w["phaseSince"] = w["createdAt"]
w["checkpointPolicy"] = {"mode": "synchronous", "intervalSteps": 1000, "expectedDurationSeconds": 30,
                         "storageId": "storage-01"}
w["progress"] = {"stepsCompleted": 0, "stepsTotal": w["training"]["stepsTotal"], "lastStepAt": None}
w["expectedUtilizationPercent"] = {"starting": [0, 100], "train": [85, 100], "checkpoint": [0, 15],
                                   "finished": [0, 5]}

# ---------------------------------------------------------------- interventions
# B11: our own grid demand-response power cap. Previously only visible as a telemetry string.
r12_gpus = sorted(g for g in ENT if g.startswith("gpu-12-"))
t["interventions"] = [{
    "id": "int-0091",
    "decisionId": "dec-0091",
    "actionType": "power_cap",
    "reason": "Grid demand-response event (National Grid DFS window)",
    "targetEntityIds": ["rack-12"] + r12_gpus,
    "affectedWorkloadIds": ["training-job-104"],
    "parameters": {"gpuPowerLimitW": {"from": 700, "to": 500}},
    "executedBy": "agent-datacenter-energy",
    "activeFrom": "2026-11-14T16:40:00Z",
    "activeUntil": "2026-11-14T18:00:00Z",
    "expectedThrottleReasons": ["SW_POWER_CAP"],
    # predicted effects: direction + largest change we'd accept as "as planned".
    # Bigger than this = the intervention is hurting more than predicted -> flag it.
    "expectedEffects": [
        {"metric": "gpu.power_w", "direction": "down", "maxChangePercent": 30},
        {"metric": "gpu.power_limit_w", "direction": "down", "maxChangePercent": 30},
        {"metric": "gpu.sm_clock_mhz", "direction": "down", "maxChangePercent": 20},
        {"metric": "gpu.temperature_c", "direction": "down", "maxChangePercent": 15},
        {"metric": "workload.step_time_ms", "direction": "up", "maxChangePercent": 15},
        {"metric": "workload.tokens_per_second", "direction": "down", "maxChangePercent": 15}],
    "status": "active",
}]
if not any(a["id"] == "agent-datacenter-energy" for a in t["agents"]):
    t["agents"].append({"id": "agent-datacenter-energy", "name": "Datacentre energy agent", "level": "datacenter",
                        "assignedEntityIds": ["feed-a", "feed-b"], "modelId": "energy-agent-v0",
                        "policyVersion": "0.1", "permissions": ["read", "act"],
                        "allowedActions": ["power_cap", "recommend"], "status": "active",
                        "lastHeartbeatAt": "2026-11-14T00:00:00Z"})

# ---------------------------------------------------------------- maintenance validation
MAINT_REQ = {"id", "entityIds", "type", "scheduledStart", "scheduledEnd", "status", "expectedEffects"}
MAINT_STATUS = {"scheduled", "in_progress", "completed", "cancelled"}
MAINT_EFFECTS = {"offline", "link_down", "telemetry_gap", "power_off", "reduced_capacity"}

# ---------------------------------------------------------------- port admin state
reserved = {eid: m["id"] for m in t["maintenance"] if m["type"] == "port_reservation" for eid in m["entityIds"]}
for e in all_entities(t):
    for p in e.get("ports", []):
        if p["id"] in reserved:
            p["adminState"] = "reserved"; p["adminStateRef"] = reserved[p["id"]]
        elif p.get("connectionId"):
            p["adminState"] = "up"
        else:
            p["adminState"] = "down"   # administratively disabled, nothing plugged in

# ---------------------------------------------------------------- redundancy groups
# mode 2N = two independent paths, each able to carry the full load alone.
# requiredHealthy = members that must be up for redundancy to hold.
# minForService   = members needed just to keep the lights on (no redundancy left).
groups = {}
for e in t["powerSystems"]:
    g = e.get("redundancyGroupId")
    if g: groups.setdefault(g, []).append(e["id"])
t["redundancyGroups"] = [{"id": g, "members": sorted(m), "mode": "2N", "requiredHealthy": len(m),
                          "minForService": 1,
                          "perMemberCapacityW": ENT[sorted(m)[0]]["electrical"]["capacityW"]}
                         for g, m in sorted(groups.items())]

t["schemaVersion"] = SCHEMA_VERSION
t["updatedAt"] = "2026-11-14T00:00:00Z"

# ---------------------------------------------------------------- validation
errors = []
ids = set(ENT) | set(WL) | set(PORTS)
for m in t["maintenance"]:
    if not MAINT_REQ <= set(m): errors.append(f"maintenance {m['id']} missing {MAINT_REQ - set(m)}")
    if m["status"] not in MAINT_STATUS: errors.append(f"maintenance {m['id']} bad status")
    if not set(m["expectedEffects"]) <= MAINT_EFFECTS: errors.append(f"maintenance {m['id']} bad effects")
    if m["scheduledEnd"] <= m["scheduledStart"]: errors.append(f"maintenance {m['id']} ends before start")
    errors += [f"maintenance {m['id']} unknown entity {x}" for x in m["entityIds"] if x not in ids]
for iv in t["interventions"]:
    errors += [f"intervention {iv['id']} unknown target {x}" for x in iv["targetEntityIds"] if x not in ids]
    errors += [f"intervention {iv['id']} effect on uncatalogued metric {x['metric']}"
               for x in iv["expectedEffects"] if x["metric"] not in catalog]
for b in baselines:
    if b["metric"] not in catalog: errors.append(f"baseline {b['id']} metric not in catalog")
    for x in b["selector"].get("entityIds", []):
        if x not in ids: errors.append(f"baseline {b['id']} unknown entity {x}")
for m, d in catalog.items():
    if d.get("baselineRef") and not any(b["metric"] == d["baselineRef"] for b in baselines):
        errors.append(f"catalog {m} baselineRef has no baseline")
for w in t["workloads"]:
    if w["phase"] not in PHASES: errors.append(f"workload {w['id']} bad phase")
if len({b["id"] for b in baselines}) != len(baselines): errors.append("duplicate baseline ids")

# every metric seen in any scenario must be catalogued
scen_dir = os.path.join(HERE, "scenarios")
seen = set()
for f in sorted(os.listdir(scen_dir)):
    for r in json.load(open(os.path.join(scen_dir, f)))["telemetry"]:
        seen.add(r["metric"])
missing = sorted(seen - set(catalog))
errors += [f"scenario metric not in catalog: {m}" for m in missing]

if errors:
    print("VALIDATION FAILED:"); [print("  -", e) for e in errors]; sys.exit(1)

with open(OUT, "w") as f:
    json.dump(t, f, indent=2)
    f.write("\n")

print(f"wrote {os.path.relpath(OUT, HERE)}")
print(f"  metricCatalog: {len(t['metricCatalog'])} metrics (covers all {len(seen)} metrics seen in scenarios)")
print(f"  baselines: {len(baselines)} + {len(rate_baselines)} counter rates, measured from "
      f"{sum(r['records'] for r in runs_meta)} fault-free records")
print(f"  interventions: {len(t['interventions'])}  "
      f"maintenance: {len(t['maintenance'])} (validated)  redundancyGroups: {len(t['redundancyGroups'])}")
print(f"  ports with adminState: {sum(1 for p in PORTS.values())} "
      f"(reserved: {sorted(reserved)})")
print(f"  workloads with phase: {[(w['id'], w['phase']) for w in t['workloads']]}")
