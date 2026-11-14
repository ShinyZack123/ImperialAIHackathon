#!/usr/bin/env python3
"""Seeded generator of synthetic datacentre telemetry. Reuses gen.py's helpers.

    python3 generate.py --list
    python3 generate.py --scenario checkpoint_overrun --seed 7 --out generated/
    python3 generate.py --scenario normal --seed 1 --minutes 120 --step 60 --out generated/

How it works
  1. normal(world) lays down fault-free telemetry for the whole topology: 16 GPUs training,
     8 serving inference, 8 idle, plus cooling, power, network, storage, room and job metrics.
     Periodic checkpoints, the daily temperature cycle and the day/night traffic curve are
     part of "normal".
  2. Injectors (one per fault or benign deviation) modify that world. They wrap the existing
     signal functions, so they compose: several can be applied to one world.
  3. render() draws noise with a seeded random generator and returns a scenario dict in the
     same shape as scenarios/*.json (TelemetryRecord list + label + context records).

Same (scenario, seed, params) -> byte-identical output. Error counters only ever increase
and every timestamp lies inside the window: render() asserts both.

Reuse of gen.py: the topology build and the helper section (Scn, ramp, SOURCES, ALL_IDS)
are executed from gen.py's own source, up to its first scenario. gen.py is not modified.
"""
import argparse, json, math, os, random, shutil, sys, tempfile
from datetime import timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
_MARK = "# ================================================================== FLAG scenarios"


def _load_gen_helpers():
    src = open(os.path.join(HERE, "gen.py")).read()
    assert _MARK in src, "gen.py layout changed: helper/scenario marker not found"
    head = src.split(_MARK)[0]
    tmp, argv = tempfile.mkdtemp(), sys.argv
    sys.argv = ["gen.py", tmp]            # gen.py reads its output folder from argv at import
    ns = {"__name__": "gen_helpers"}
    try:
        exec(compile(head, "gen.py", "exec"), ns)
    finally:
        sys.argv = argv
        shutil.rmtree(tmp, ignore_errors=True)
    return ns


GEN = _load_gen_helpers()
Scn, ramp, iso, T = GEN["Scn"], GEN["ramp"], GEN["iso"], GEN["T"]
ALL_IDS = GEN["ALL_IDS"]

J104, SVC, FTJ = "training-job-104", "inference-svc-207", "finetune-job-311"
TRAIN = [f"gpu-12-{s}-{g:02d}" for s in ("01", "02") for g in range(8)]
INFER = [f"gpu-13-01-{g:02d}" for g in range(8)]
FT = [f"gpu-13-02-{g:02d}" for g in range(8)]
SERVERS = ["server-12-01", "server-12-02", "server-13-01", "server-13-02"]
UPLINKS = {"leaf-12-p07": "spine-01-p01", "leaf-12-p08": "spine-01-p02",
           "leaf-13-p07": "spine-01-p03", "leaf-13-p08": "spine-01-p04"}
STEP_MS = 2410.0                      # normal training step time
STEPS_PER_MIN = 60_000 / STEP_MS
CP_EXPECTED_S = 90                    # expected checkpoint duration (from the workload record)


def gpus_of(server): return [f"gpu-{server[7:]}-{g:02d}" for g in range(8)]
def nics_of(server): return [f"nic-{server[7:]}-01", f"nic-{server[7:]}-02"]


# ====================================================================== world
class World:
    def __init__(self, name, seed, start, minutes, step=30):
        self.name, self.seed = name, seed
        self.sid = f"{name}-s{seed}"
        self.sc = Scn(self.sid, start, minutes, step)
        self.sc.rng = random.Random(f"noise|{name}|{seed}")     # string seed: stable across runs
        self.aux = random.Random(f"aux|{name}|{seed}")          # background events (ECC etc.)
        self.step, self.minutes = step, minutes
        self.start_hour = self.sc.start.hour + self.sc.start.minute / 60
        self.s = {}          # (entity, metric) -> series spec, in insertion order
        self.events = []     # one-off records
        self.context = {"maintenance": [], "interventions": []}
        self.labels = []     # one dict per injector: flag / category / reason / root entity
        # --- lazily-read state that injectors may change after normal() ---
        self.cps = []                # checkpoints: [start_minute, duration_minutes]
        self.storage_stall_from = None
        self.rps_mult = lambda m: 1.0
        self.diurnal_amp = 2.6
        self.ft_start, self.ft_end = None, None   # fine-tune job: start / end minute (None = queued)
        if step < 300:               # coarser data are multi-minute means: 90 s dips average out
            period = 500 / STEPS_PER_MIN + CP_EXPECTED_S / 60
            t = self.aux.uniform(3, 18)
            while t < minutes:
                self.cps.append([round(t, 2), CP_EXPECTED_S / 60]); t += period

    # ---- time helpers
    def hour(self, m): return (self.start_hour + m / 60) % 24
    def at(self, m): return self.sc.start + timedelta(seconds=round(m * 60))
    def every(self, seconds): return max(1, round(seconds / self.step))
    def grid(self, m): return round(m * 60 / self.step) * self.step / 60   # snap to a sample time

    # ---- series registry
    def put(self, eid, metric, unit, fn, src, noise=0.0, nd=1, labels=None, every=1, qual=None):
        assert eid in ALL_IDS, eid
        self.s[(eid, metric)] = dict(unit=unit, fn=fn, src=src, noise=noise, nd=nd, labels=labels,
                                     every=every, qual=qual, freeze=None)

    def wrap(self, eid, metric, make):
        """Replace a signal by make(old_fn) -> new_fn. This is how injectors compose."""
        spec = self.s[(eid, metric)]
        spec["fn"] = make(spec["fn"])

    def event(self, eid, metric, unit, minute, value, src, labels=None):
        self.events.append((eid, metric, unit, minute, value, src, labels))

    def label(self, flag, category, reason, root=None, severity=None, accept=()):
        self.labels.append({"flag": flag, "category": category, "reason": reason, "rootCauseEntityId": root,
                            "severity": severity, "acceptEntityIds": list(accept)})

    # ---- checkpoint / job state
    def cp_at(self, m):
        for a, d in self.cps:
            if a <= m < a + d: return a, d
        return None

    def active_minutes(self, m):
        return m - sum(min(max(m - a, 0.0), d) for a, d in self.cps)

    def steps0(self):
        return 41_500 - int(self.cps[0][0] * STEPS_PER_MIN) if self.cps else 41_200

    def ft_state(self, m):
        if self.ft_start is None or m < self.ft_start: return "queued"
        if self.ft_end is not None and m >= self.ft_end: return "completed"
        return "running"

    # ---- render
    def render(self, title=None):
        sc = self.sc
        for (eid, metric), sp in self.s.items():
            frozen, lab = None, sp["labels"]
            for i in range(0, sc.n, sp["every"]):
                m = sc.minute(i)
                v = sp["fn"](m)
                if v is None: continue
                nz = sp["noise"](m) if callable(sp["noise"]) else sp["noise"]
                if isinstance(v, (int, float)) and not isinstance(v, bool) and nz:
                    v = v + sc.rng.gauss(0, nz)
                    if sp["unit"] == "%": v = min(100.0, max(0.0, v))
                    elif sp["unit"] is not None: v = max(0, v)
                if isinstance(v, float): v = round(v, sp["nd"])
                q = sp["qual"](m) if sp["qual"] else {}
                if sp["freeze"] is not None and m >= sp["freeze"]:
                    # hung exporter: collector re-sends the last real value with its old scrape time
                    if frozen is None: frozen = (v, iso(sc.t(max(0, i - sp["every"]))))
                    age = m - sp["freeze"]
                    sc.add(eid, metric, sp["unit"], sc.t(i), frozen[0], sp["src"],
                           labels=dict(lab or {}, scrapeTimestamp=frozen[1]),
                           stale=age >= 2, confidence=0.9 if age < 2 else 0.1)
                    continue
                if sp["freeze"] is not None: frozen = (v, iso(sc.t(i)))
                sc.add(eid, metric, sp["unit"], sc.t(i), v, sp["src"], labels=lab, **q)
        for eid, metric, unit, minute, value, src, labels in self.events:
            sc.add(eid, metric, unit, self.at(minute), value, src, labels=labels)

        recs = sorted(sc.recs, key=lambda r: r["timestamp"])           # replay order (stable sort)
        start, end = iso(sc.start), iso(sc.t(sc.n))
        last = {}
        for i, r in enumerate(recs):
            r["id"] = f"tel-{self.sid}-{i + 1:06d}"
            assert start <= r["timestamp"] < end, ("timestamp outside window", r)
            if r["metric"].endswith("_total") or r["metric"] == "workload.steps_completed":
                k = (r["entityId"], r["metric"])
                assert r["value"] >= last.get(k, r["value"]), ("counter went down", r)
                last[k] = r["value"]
        flag = any(l["flag"] for l in self.labels)
        prim = [l for l in self.labels if l["flag"]] or self.labels
        expected = {"flag": flag,
                    "severity": next((l["severity"] for l in prim if l["flag"]), None),
                    "category": "+".join(l["category"] for l in prim) or "normal_operation",
                    "reason": " | ".join(l["reason"] for l in self.labels) or "Fault-free run.",
                    "rootCauseEntityId": next((l["rootCauseEntityId"] for l in prim if l["flag"]), None),
                    "acceptEntityIds": sorted({x for l in prim if l["flag"] for x in l["acceptEntityIds"]})}
        return {"scenarioId": self.sid, "title": title or self.name, "expected": expected,
                "window": {"start": start, "end": end, "sampleIntervalSeconds": self.step},
                "generator": {"scenario": self.name, "seed": self.seed},
                "contextRecords": self.context, "telemetry": recs}


# ====================================================================== fault-free world
def normal(w):
    """Everything a healthy hall emits. All set points mirror gen.py's normal operation."""
    slow = w.every(300)
    dcgm, lab = "dcgm", {"workloadId": J104}

    # ---- rack 12: 16 GPUs training (dip during synchronous checkpoints)
    def tr_temp(m):
        cp = w.cp_at(m)
        return 66.0 if not cp else max(45.0, 63.0 - 2.0 * max(0.0, m - cp[0] - 1.5))
    for g in TRAIN:
        w.put(g, "gpu.temperature_c", "C", tr_temp, dcgm, noise=0.6, labels=lab)
        w.put(g, "gpu.power_w", "W", lambda m: 160 if w.cp_at(m) else 610, dcgm, noise=12, nd=0, labels=lab)
        w.put(g, "gpu.sm_clock_mhz", "MHz", lambda m: 1830, dcgm, noise=8, nd=0, labels=lab)
        w.put(g, "gpu.utilization_percent", "%", lambda m: 4 if w.cp_at(m) else 97, dcgm, noise=1.0, labels=lab)
        w.put(g, "gpu.memory_temperature_c", "C", lambda m: 58, dcgm, noise=0.5, labels=lab, every=w.every(60))
        w.put(g, "gpu.throttle_reasons", None, lambda m: "none", dcgm, labels=lab, every=w.every(60))
        w.put(g, "gpu.power_limit_w", "W", lambda m: 700, dcgm, labels=lab, every=w.every(120))
        w.put(g, "gpu.pcie_link_gen", "gen", lambda m: 5, dcgm, every=slow)
        w.put(g, "gpu.pcie_link_width", "lanes", lambda m: 16, dcgm, every=slow)
        w.put(g, "gpu.pcie_replay_total", "count", lambda m: 0, dcgm, every=slow)
        w.put(g, "gpu.pcie_rx_throughput_gbps", "GB/s", lambda m: 21.0, dcgm, noise=0.6, labels=lab, every=slow)

    # ---- rack 13 server 01: 8 GPUs serving inference; load follows user traffic
    def rps(m):
        h = w.hour(m)
        if 7 <= h < 21: base = 420.0
        elif h >= 21: base = ramp(h, 21, 25, 420, 105)
        elif h < 1: base = ramp(h + 24, 21, 25, 420, 105)
        elif h < 5: base = 105.0
        else: base = ramp(h, 5, 7, 105, 420)
        return base * w.rps_mult(m)
    w.rps = rps
    ilab = {"workloadId": SVC}
    for g in INFER:
        w.put(g, "gpu.temperature_c", "C", lambda m: 50 + 8 * rps(m) / 420, dcgm, noise=0.6, labels=ilab)
        w.put(g, "gpu.utilization_percent", "%", lambda m: min(99.0, 71 * rps(m) / 420), dcgm, noise=2.0, labels=ilab)
        w.put(g, "gpu.power_w", "W", lambda m: 120 + 310 * min(1.4, rps(m) / 420), dcgm, noise=10, nd=0, labels=ilab)
    w.put(SVC, "inference.requests_per_second", "req/s", rps, "infer", noise=10, nd=0)
    w.put(SVC, "inference.p99_latency_ms", "ms", lambda m: 397 + 213 * rps(m) / 420, "infer", noise=20, nd=0)
    w.put(SVC, "inference.ttft_ms", "ms", lambda m: 140, "infer", noise=10, nd=0)
    w.put(SVC, "inference.queue_depth", "requests", lambda m: 1 + 5 * min(1.0, rps(m) / 420), "infer", noise=1.0, nd=0)
    w.put(SVC, "inference.error_rate", "ratio", lambda m: 0.001, "infer", nd=4)
    w.put(SVC, "inference.healthy_replicas", "count", lambda m: 2, "infer")

    # ---- rack 13 server 02: fine-tune job (queued by default -> GPUs idle)
    def ft_temp(m):
        st = w.ft_state(m)
        if st == "queued": return 34.0
        hot = lambda x: 34 + 33 * (1 - math.exp(-(x - w.ft_start) / 3.5))
        if st == "running": return hot(m)
        return 34 + (hot(w.ft_end) - 34) * math.exp(-(m - w.ft_end) / 4)
    def ft_power(m):
        st = w.ft_state(m)
        return ramp(m, w.ft_start, w.ft_start + 1, 88, 575) if st == "running" else 88.0
    w.ft_power = ft_power
    flab = {"workloadId": FTJ}
    for g in FT:
        w.put(g, "gpu.temperature_c", "C", ft_temp, dcgm, noise=0.6, labels=flab)
        w.put(g, "gpu.power_w", "W", ft_power, dcgm, noise=12, nd=0, labels=flab)
        w.put(g, "gpu.sm_clock_mhz", "MHz", lambda m: 1830 if w.ft_state(m) == "running" else 345, dcgm, noise=8, nd=0, labels=flab)
        w.put(g, "gpu.utilization_percent", "%", lambda m: 94 if w.ft_state(m) == "running" else 0, dcgm, noise=1.0, labels=flab)
    w.put(FTJ, "workload.status", None, w.ft_state, "slurm", every=w.every(60))
    def ft_steps(m):
        if w.ft_start is None: return None
        if w.ft_end is not None: return max(0, 6000 - int(max(0.0, w.ft_end - m) * 11))
        if m < w.ft_start: return None            # a queued job reports no steps
        return 1000 + int((m - w.ft_start) * 11)
    w.put(FTJ, "workload.steps_completed", "count", ft_steps, "slurm", every=w.every(60))

    # ---- memory error counters on every GPU: a few corrected errors per day is normal
    for g in TRAIN + INFER + FT:
        base, times, t = w.aux.randint(0, 40), [], 0.0
        while True:
            t += w.aux.expovariate(4 / 1440)          # ~4 per day
            if t >= w.minutes: break
            times.append(t)
        w.put(g, "gpu.ecc_sbe_total", "count", lambda m, b=base, ts=tuple(times): b + sum(1 for x in ts if x <= m), dcgm, every=slow)
        w.put(g, "gpu.ecc_dbe_total", "count", lambda m: 0, dcgm, every=slow)

    # ---- training job + storage
    w.put(J104, "workload.status", None, lambda m: "running", "slurm", every=w.every(60))
    w.put(J104, "workload.phase", None, lambda m: "checkpoint" if w.cp_at(m) else "train", "slurm")
    w.put(J104, "workload.steps_completed", "count",
          lambda m: w.steps0() + int(w.active_minutes(m) * STEPS_PER_MIN), "slurm", every=w.every(60))
    w.put(J104, "workload.step_time_ms", "ms", lambda m: STEP_MS, "slurm", noise=15, nd=0, every=w.every(60))
    w.put(J104, "workload.tokens_per_second", "tokens/s",
          lambda m: 1024 * 8192 / (w.s[(J104, "workload.step_time_ms")]["fn"](m) / 1000.0), "slurm",
          noise=8000, nd=0, every=w.every(60))
    w.put(J104, "workload.comm_overhead_percent", "%", lambda m: 14, "slurm", noise=0.4, every=w.every(60))
    w.put(J104, "workload.last_log_line_age_s", "s", lambda m: 3, "slurm", noise=1.0, nd=0, every=w.every(60))
    def stalled(m):
        return w.cp_at(m) and w.storage_stall_from is not None and m >= w.storage_stall_from
    w.put("storage-01", "storage.write_bytes_per_second", "B/s",
          lambda m: 0.4e9 if stalled(m) else (95e9 if w.cp_at(m) else 2e9), "stor", noise=0.3e9, nd=0)
    w.put("storage-01", "storage.metadata_latency_ms", "ms", lambda m: 4800 if stalled(m) else 2, "stor", noise=0.3)

    # ---- liquid cooling
    w.put("cdu-02", "cdu.flow_lpm", "L/min", lambda m: 115, "bms", noise=1.2, every=w.every(60))
    w.put("cdu-02", "cdu.supply_temp_c", "C", lambda m: 30.0, "bms", noise=0.15, every=w.every(60))
    w.put("cdu-02", "cdu.return_temp_c", "C",
          lambda m: 30 + 10 * 115 / max(1.0, w.s[("cdu-02", "cdu.flow_lpm")]["fn"](m)), "bms", noise=0.2, every=w.every(60))
    w.put("cdu-02", "cdu.differential_pressure_kpa", "kPa", lambda m: 210, "bms", noise=2, every=w.every(60))
    w.put("cdu-02", "cdu.pump_a_status", None, lambda m: "running", "bms", every=w.every(60))
    w.put("cdu-02", "cdu.pump_b_status", None, lambda m: "running", "bms", every=w.every(60))

    # ---- power: A/B feeds share each rack's load 50/50
    load = {"12": lambda m: 17_800.0, "13": lambda m: 17_400.0 + 8 * (ft_power(m) - 88)}
    for r in ("12", "13"):
        for side in ("a", "b"):
            p = f"pdu-{r}-{side}"
            w.put(p, "pdu.active_power_w", "W", lambda m, r=r: load[r](m) / 2, "pdu", noise=70, nd=0, every=w.every(60))
            w.put(p, "pdu.current_a", "A", lambda m, p=p: w.s[(p, "pdu.active_power_w")]["fn"](m) / (415 * 1.732 * 0.98),
                  "pdu", noise=0.1, every=w.every(60))
            w.put(p, "pdu.load_percent_of_capacity", "%", lambda m, p=p: w.s[(p, "pdu.active_power_w")]["fn"](m) / 220,
                  "pdu", noise=0.3, every=w.every(60))
            w.put(p, "pdu.input_status", None, lambda m: "ok", "pdu", every=w.every(60))

    # ---- hall air: daily (diurnal) cycle, peak mid-afternoon
    wave = lambda m: math.sin((w.hour(m) - 9) / 24 * 2 * math.pi)
    w.put("crac-01", "crac.supply_air_temp_c", "C", lambda m: 19.5 + 0.6 * wave(m), "bms", noise=0.15, every=w.every(300))
    w.put("room-01", "room.inlet_temp_c", "C", lambda m: 22.5 + w.diurnal_amp * wave(m), "bms", noise=0.2, every=w.every(300))
    w.put("room-01", "room.humidity_percent", "%", lambda m: 45 - 6 * wave(m), "bms", noise=0.5, every=w.every(300))
    w.put("chiller-01", "chiller.outdoor_temp_c", "C", lambda m: 8 + 5 * wave(m), "bms", noise=0.3, every=w.every(300))
    for sw in ("leaf-12", "leaf-13", "spine-01"):
        w.put(sw, "switch.temperature_c", "C", lambda m: 48 + 2 * wave(m), "ufm", noise=0.3, every=w.every(300))

    # ---- network: uplinks carry static historic error totals; reserved ports are down by design
    for i, (lp, sp) in enumerate(UPLINKS.items()):
        for j, p in enumerate((lp, sp)):
            w.put(p, "port.link_up", "bool", lambda m: True, "ufm", every=w.every(60))
            w.put(p, "port.crc_errors_total", "count", lambda m, c=312 + 197 * i + 451 * j: c, "ufm", every=w.every(60))
        w.put(lp, "port.symbol_errors_total", "count", lambda m, c=88_000 + 9_100 * i: c, "ufm", every=w.every(60))
        w.put(lp, "port.effective_ber", "ratio", lambda m: 1e-15, "ufm", nd=18, every=w.every(60))
        w.put(lp, "port.link_downed_total", "count", lambda m, c=i: c, "ufm", every=w.every(60))
    for p in ("leaf-13-p05", "leaf-13-p06"):
        w.put(p, "port.link_up", "bool", lambda m: False, "ufm", every=slow)
        w.put(p, "port.rx_bytes_total", "B", lambda m: 0, "ufm", every=slow)
    for s in SERVERS:
        w.put(s, "node.up", "bool", lambda m: True, "node", every=w.every(60))
        w.put(s, "exporter.dcgm_up", "bool", lambda m: True, "node")
        for n in nics_of(s):
            w.put(n, "port.link_up", "bool", lambda m: True, "ufm", every=w.every(60))


# ====================================================================== injectors
# Each injector changes the world and records its own label. Times are minutes from window start.

def thermal_throttle(w, gpu="gpu-12-02-05", onset=6, peak=89.5, ramp_min=10):
    """One GPU overheats. At >= 87C (its thermal limit) it throttles and slows the whole job."""
    rise = peak - 66
    w.wrap(gpu, "gpu.temperature_c", lambda old: lambda m: old(m) + ramp(m, onset, onset + ramp_min, 0, rise))
    w.wrap(gpu, "gpu.memory_temperature_c", lambda old: lambda m: old(m) + ramp(m, onset, onset + ramp_min, 0, rise * 1.1))
    if peak >= 87:
        t0 = onset + ramp_min * (87 - 66) / rise
        w.wrap(gpu, "gpu.sm_clock_mhz", lambda old: lambda m: old(m) - ramp(m, t0, t0 + 4, 0, 465))
        w.wrap(gpu, "gpu.power_w", lambda old: lambda m: old(m) - ramp(m, t0, t0 + 4, 0, 140))
        w.wrap(gpu, "gpu.throttle_reasons", lambda old: lambda m: "HW_THERMAL_SLOWDOWN" if m >= t0 else old(m))
        w.wrap(J104, "workload.step_time_ms", lambda old: lambda m: old(m) * ramp(m, t0, t0 + 4, 1, 1.31))
        for g in TRAIN:
            if g != gpu:
                w.wrap(g, "gpu.utilization_percent", lambda old: lambda m: old(m) * (0.887 if m >= t0 + 1 else 1))
    w.label(True, "thermal_throttling" if peak >= 87 else "thermal_outlier",
            f"{gpu} runs {rise:.0f}C hotter than identical peers" + (" and throttles" if peak >= 87 else " (below its limit)"),
            root=gpu, severity="critical" if peak >= 87 else "warning")


def spurious_sample(w, gpu="gpu-12-02-07", at=7, value=127.0, invalid=True):
    """A single impossible temperature reading between normal ones: sensor glitch, not a fault."""
    at = w.grid(at)
    hit = lambda m: abs(m - at) < 1e-6
    w.wrap(gpu, "gpu.temperature_c", lambda old: lambda m: value if hit(m) else old(m))
    if invalid:
        w.s[(gpu, "gpu.temperature_c")]["qual"] = lambda m: {"valid": False, "confidence": 0.15} if hit(m) else {}
    w.label(False, "sensor_noise", f"one {value:g}C sample on {gpu}" + (" marked invalid" if invalid else " (not marked invalid)"))


def ecc_burst(w, gpu="gpu-12-01-03", at=8, sbe=12, dbe=True, fatal=True):
    """Burst of corrected memory errors; optionally an uncorrectable one and the GPU dying."""
    w.s[(gpu, "gpu.ecc_sbe_total")]["every"] = 1
    w.s[(gpu, "gpu.ecc_dbe_total")]["every"] = 1
    w.wrap(gpu, "gpu.ecc_sbe_total", lambda old: lambda m: old(m) + (sbe if m >= at else 0))
    if dbe:
        w.wrap(gpu, "gpu.ecc_dbe_total", lambda old: lambda m: old(m) + (1 if m >= at + 0.5 else 0))
        w.event(gpu, "gpu.xid", "code", at + 0.52, 48, "dcgm", {"workloadId": J104, "message": "Double Bit ECC Error"})
    if dbe and fatal:
        dead = at + 1.5
        w.event(gpu, "gpu.xid", "code", at + 1.37, 79, "dcgm", {"workloadId": J104, "message": "GPU has fallen off the bus"})
        for (e, metric) in list(w.s):
            if e == gpu: w.wrap(e, metric, lambda old: lambda m: None if m >= dead else old(m))
        for g in TRAIN:
            if g == gpu: continue
            w.wrap(g, "gpu.utilization_percent", lambda old: lambda m: 0 if m >= at + 2 else old(m))
            w.wrap(g, "gpu.power_w", lambda old: lambda m: 95 if m >= at + 2 else old(m))
            w.wrap(g, "gpu.temperature_c", lambda old: lambda m: old(m) - ramp(m, at + 2, at + 12, 0, 25))
        w.wrap(J104, "workload.status", lambda old: lambda m: "failed" if m >= at + 2.5 else old(m))
        w.wrap(J104, "workload.steps_completed", lambda old: lambda m: old(min(m, at + 2)))
        for metric in ("workload.step_time_ms", "workload.tokens_per_second", "workload.comm_overhead_percent",
                       "workload.last_log_line_age_s", "workload.phase"):
            w.wrap(J104, metric, lambda old: lambda m: None if m >= at + 2 else old(m))
    w.label(True, "gpu_hardware_error",
            f"{sbe} corrected errors in one sample on {gpu}" + (", then an uncorrectable error" if dbe else "") +
            (" and the GPU fell off the bus" if dbe and fatal else ""), root=gpu,
            severity="critical" if dbe else "warning")


def background_ecc(w, gpu="gpu-12-02-03", extra=3):
    """A few extra corrected errors spread over the window. Normal background."""
    w.wrap(gpu, "gpu.ecc_sbe_total",
           lambda old: lambda m: old(m) + sum(1 for k in range(extra) if m >= (k + 0.5) * w.minutes / extra))
    w.label(False, "background_error_rate", f"{extra} extra corrected errors on {gpu} spread across the window")


def link_crc(w, port="leaf-12-p07", onset=10, crc_per_min=600.0, impact=True):
    """Cable/optic degrading: error counters climb. With impact, distributed training slows."""
    w.wrap(port, "port.crc_errors_total", lambda old: lambda m: old(m) + int(max(0.0, m - onset) * crc_per_min))
    if impact:
        w.wrap(port, "port.symbol_errors_total", lambda old: lambda m: old(m) + int(max(0.0, m - onset) * crc_per_min * 85))
        w.wrap(port, "port.effective_ber", lambda old: lambda m: 3.2e-9 if m >= onset else old(m))
        if port.startswith("leaf-12"):
            w.wrap(J104, "workload.step_time_ms", lambda old: lambda m: old(m) * ramp(m, onset, onset + 10, 1, 1.13))
            w.wrap(J104, "workload.comm_overhead_percent", lambda old: lambda m: old(m) + ramp(m, onset, onset + 10, 0, 9.5))
            for g in TRAIN:
                w.wrap(g, "gpu.utilization_percent", lambda old: lambda m: old(m) * ramp(m, onset, onset + 10, 1, 0.876))
        w.label(True, "network_link_degradation", f"CRC errors on {port} climbing ~{crc_per_min:g}/min", root=port,
                severity="warning", accept=[UPLINKS.get(port, port), "cable-" + port.replace("leaf-", "leaf")])
    else:
        w.label(False, "background_error_rate", f"{port} logs a trickle of CRC errors (~{crc_per_min * 60:g}/hour), no impact")


def cdu_flow_drop(w, onset=8, frac=0.5):
    """CDU pump fault: coolant flow falls to frac of normal; every liquid-cooled loaded GPU heats up."""
    w.wrap("cdu-02", "cdu.flow_lpm", lambda old: lambda m: old(m) * ramp(m, onset, onset + 4, 1, frac))
    w.wrap("cdu-02", "cdu.differential_pressure_kpa", lambda old: lambda m: old(m) * ramp(m, onset, onset + 4, 1, 0.22 + 0.78 * frac))
    w.wrap("cdu-02", "cdu.pump_a_status", lambda old: lambda m: "fault" if m >= onset else old(m))
    rise = 17 * (1 - frac) / 0.5
    for g in TRAIN + INFER:
        w.wrap(g, "gpu.temperature_c", lambda old: lambda m: old(m) + ramp(m, onset + 2, onset + 22, 0, rise))
    w.label(True, "facility_cooling_degradation", f"coolant flow fell to {frac * 100:.0f}% after pump A fault",
            root="cdu-02", severity="critical", accept=["coolant-loop-02"])


def pdu_feed_loss(w, rack="13", side="b", onset=6):
    """One power feed trips; the other PDU carries the whole rack with no redundancy left."""
    lost, keep = f"pdu-{rack}-{side}", f"pdu-{rack}-{'a' if side == 'b' else 'b'}"
    w.wrap(lost, "pdu.active_power_w", lambda old: lambda m: 0 if m >= onset else old(m))
    w.s[(lost, "pdu.active_power_w")]["noise"] = lambda m: 0 if m >= onset else 70   # a dead feed reads exactly 0
    w.wrap(keep, "pdu.active_power_w", lambda old: lambda m: old(m) * (2 if m >= onset else 1))
    w.wrap(lost, "pdu.input_status", lambda old: lambda m: "breaker_tripped" if m >= onset else old(m))
    for metric in ("pdu.current_a", "pdu.load_percent_of_capacity"):
        w.wrap(lost, metric, lambda old: lambda m: None if m >= onset else old(m))
    w.label(True, "power_redundancy_loss", f"{lost} breaker tripped; {keep} carries rack {rack} alone",
            root=lost, severity="critical", accept=[keep, f"rack-{rack}"])


def exporter_hang(w, server="server-13-01", onset=8):
    """GPU metrics exporter hangs: values freeze while the node keeps working."""
    onset = w.grid(onset)
    for (e, metric), sp in w.s.items():
        if e in gpus_of(server) and metric.startswith("gpu."):
            if sp["unit"] in ("C", "W", "MHz", "%"): sp["freeze"] = onset
            else: w.wrap(e, metric, lambda old: lambda m: None if m >= onset else old(m))
    w.wrap(server, "exporter.dcgm_up", lambda old: lambda m: False if m >= onset else old(m))
    w.label(True, "telemetry_quality", f"DCGM exporter on {server} hung; GPU readings frozen", root=server,
            severity="warning", accept=gpus_of(server))


def hung_rank(w, gpu="gpu-12-02-02", onset=9):
    """One training process hangs; the others busy-wait at 100% 'utilisation' with no progress."""
    for g in TRAIN:
        if g == gpu:
            w.wrap(g, "gpu.utilization_percent", lambda old: lambda m: 0 if m >= onset else old(m))
            w.wrap(g, "gpu.power_w", lambda old: lambda m: 118 if m >= onset else old(m))
            w.wrap(g, "gpu.sm_clock_mhz", lambda old: lambda m: 1980 if m >= onset else old(m))
            w.wrap(g, "gpu.temperature_c", lambda old: lambda m: 66 - ramp(m, onset, onset + 10, 0, 22) if m >= onset else old(m))
        else:
            w.wrap(g, "gpu.utilization_percent", lambda old: lambda m: 100 if m >= onset else old(m))
            w.wrap(g, "gpu.power_w", lambda old: lambda m: 255 if m >= onset else old(m))
            w.wrap(g, "gpu.temperature_c", lambda old: lambda m: 66 - ramp(m, onset, onset + 10, 0, 14) if m >= onset else old(m))
    w.cps[:] = [c for c in w.cps if c[0] < onset]
    w.wrap(J104, "workload.steps_completed", lambda old: lambda m: old(min(m, onset)))
    w.wrap(J104, "workload.last_log_line_age_s", lambda old: lambda m: (m - onset) * 60 if m >= onset else old(m))
    for metric in ("workload.step_time_ms", "workload.tokens_per_second", "workload.comm_overhead_percent"):
        w.wrap(J104, metric, lambda old: lambda m: None if m >= onset else old(m))
    w.label(True, "workload_stall", f"{gpu} rank hung; job reports running but steps stopped", root=gpu,
            severity="critical", accept=[J104])


def replica_crash(w, onset=7, peak_p99=2450.0):
    """One of two inference replicas crash-loops; queue builds and latency breaches the SLA."""
    k = peak_p99 / 2450
    w.wrap(SVC, "inference.queue_depth", lambda old: lambda m: old(m) + ramp(m, onset, onset + 8, 0, 184 * k))
    w.wrap(SVC, "inference.p99_latency_ms", lambda old: lambda m: old(m) + ramp(m, onset, onset + 8, 0, peak_p99 - 620))
    w.wrap(SVC, "inference.ttft_ms", lambda old: lambda m: old(m) + ramp(m, onset, onset + 8, 0, 960 * k))
    w.wrap(SVC, "inference.error_rate", lambda old: lambda m: 0.034 if m >= onset + 2 else old(m))
    w.wrap(SVC, "inference.healthy_replicas", lambda old: lambda m: 1 if m >= onset else old(m))
    for g in INFER:
        dead = int(g[-2:]) >= 4
        w.wrap(g, "gpu.utilization_percent", lambda old, d=dead: lambda m: (0 if d else 99) if m >= onset else old(m))
        w.wrap(g, "gpu.power_w", lambda old, d=dead: lambda m: (110 if d else 560) if m >= onset else old(m))
    w.event(SVC, "inference.replica_event", None, onset + 0.07, "replica-1 OOMKilled; CrashLoopBackOff", "infer")
    w.label(True, "sla_breach", f"replica-1 crash-looping; p99 latency climbs to ~{peak_p99:.0f} ms (SLA 800)",
            root=SVC, severity="critical", accept=INFER)


def traffic_change(w, onset=5, factor=1.25, over=10):
    """User traffic changes; load and latency follow it. Demand-driven, inside the SLA."""
    w.rps_mult = lambda m: ramp(m, onset, onset + over, 1.0, factor)
    w.label(False, "demand_driven_change", f"inference traffic x{factor:g}; GPUs and latency track demand, SLA held")


def pcie_downtrain(w, gpu="gpu-12-01-06", onset=5, replay_per_min=1450.0, gen=4, width=8):
    """GPU's PCIe link renegotiates slower and logs replay errors."""
    for metric in ("gpu.pcie_link_gen", "gpu.pcie_link_width", "gpu.pcie_replay_total", "gpu.pcie_rx_throughput_gbps"):
        w.s[(gpu, metric)]["every"] = 1
    w.wrap(gpu, "gpu.pcie_rx_throughput_gbps", lambda old: lambda m: 6.4 if m >= onset else old(m))
    w.wrap(gpu, "gpu.pcie_link_gen", lambda old: lambda m: gen if m >= onset else old(m))
    w.wrap(gpu, "gpu.pcie_link_width", lambda old: lambda m: width if m >= onset else old(m))
    w.wrap(gpu, "gpu.pcie_replay_total", lambda old: lambda m: old(m) + int(max(0.0, m - onset) * replay_per_min))
    w.wrap(J104, "workload.step_time_ms", lambda old: lambda m: old(m) * (1.054 if m >= onset else 1))
    w.label(True, "pcie_degradation", f"{gpu} PCIe link down-trained to Gen{gen} x{width} with replay errors",
            root=gpu, severity="warning")


def checkpoint(w, at=9, duration_s=90):
    """A checkpoint of chosen length. Short = benign. Far beyond 3x expected = storage stall."""
    period = 500 / STEPS_PER_MIN
    w.cps[:] = [[at, duration_s / 60]]
    t = at + duration_s / 60 + period
    while t < w.minutes:
        w.cps.append([round(t, 2), CP_EXPECTED_S / 60]); t += period + CP_EXPECTED_S / 60
    if duration_s > 3 * CP_EXPECTED_S:
        w.storage_stall_from = at + 0.5
        w.label(True, "storage_stall", f"checkpoint lasts {duration_s} s vs {CP_EXPECTED_S} s expected "
                f"({duration_s / CP_EXPECTED_S:.1f}x); storage metadata latency 4.8 s", root="storage-01",
                severity="critical", accept=[J104])
    else:
        w.label(False, "expected_workload_phase", f"checkpoint lasts {duration_s} s vs {CP_EXPECTED_S} s expected")


def offline(w, server="server-13-02", down_from=3, down_to=None, maint=None):
    """Server goes offline. Benign if a maintenance record covers it, a fault if it outlasts it.
    maint = (start_minute, end_minute) of a maintenance window to record, or None."""
    end = w.minutes + 1 if down_to is None else down_to
    down = lambda m: down_from <= m < end
    w.wrap(server, "node.up", lambda old: lambda m: False if down(m) else old(m))
    w.wrap(server, "exporter.dcgm_up", lambda old: lambda m: None if down(m) else old(m))
    for n in nics_of(server):
        w.wrap(n, "port.link_up", lambda old: lambda m: False if down(m) else old(m))
    for (e, metric) in list(w.s):
        if e in gpus_of(server): w.wrap(e, metric, lambda old: lambda m: None if down(m) else old(m))
    w.put(server, "bmc.power_state", None, lambda m: "on", "node", every=w.every(60))
    if down_to is None:
        w.put(server, "bmc.post_code", None, lambda m: "0x92 (PCI bus init) - stuck" if down(m) else None,
              "node", every=w.every(300))
    covered = False
    if maint:
        ms, me = maint
        done = me <= w.minutes
        w.context["maintenance"].append({
            "id": f"maint-{w.sid}", "entityIds": [server] + gpus_of(server) + nics_of(server),
            "type": "firmware_upgrade", "description": "Scheduled firmware upgrade, node drained",
            "scheduledStart": iso(w.at(ms)), "scheduledEnd": iso(w.at(me)),
            "actualStart": iso(w.at(ms)), **({"actualEnd": iso(w.at(me))} if done else {}),
            "status": "completed" if done else "in_progress",
            "expectedEffects": ["offline", "link_down", "telemetry_gap"], "ticketId": f"CHG-{90000 + w.seed}"})
        covered = ms <= down_from and end <= me + 15
    if covered:
        w.label(False, "planned_maintenance", f"{server} offline inside its maintenance window")
    else:
        over = f"{min(end, w.minutes) - maint[1]:.0f} min after its maintenance window closed" if maint else "with no maintenance record"
        w.label(True, "failed_return_from_maintenance" if maint else "unplanned_outage", f"{server} down {over}",
                root=server, severity="warning", accept=nics_of(server))


def thermal_drift(w, gpu="gpu-12-01-01", total_c=14.0):
    """One GPU's temperature creeps up across the whole window at unchanged power and coolant."""
    w.wrap(gpu, "gpu.temperature_c", lambda old: lambda m: old(m) + total_c * m / w.minutes)
    if total_c >= 8:
        w.label(True, "gradual_degradation", f"{gpu} drifts +{total_c:g}C against identical peers", root=gpu, severity="warning")
    else:
        w.label(False, "within_normal_variation", f"{gpu} wanders +{total_c:g}C, within normal spread")


def power_transient(w, gpu="gpu-12-01-04", at=(3.4, 6.4), peak=688.0, samples=2):
    """Brief power spikes below the 700 W limit during big matrix operations."""
    hits = {round(w.grid(a) * 60 + k * w.step) for a in at for k in range(samples)}
    w.wrap(gpu, "gpu.power_w", lambda old: lambda m: peak if round(m * 60) in hits else old(m))
    w.label(False, "within_limits_transient", f"{gpu} power spikes to {peak:g} W for {samples * w.step} s (limit 700 W)")


def power_cap(w, onset=10, to_w=500, slowdown=0.116, logged=True, predicted_max=15):
    """GPU power cap on rack 12. Benign when it is our own logged intervention and the slowdown
    is within what the intervention predicted; a fault if unlogged or much worse than predicted."""
    k = (700 - to_w) / 200
    for g in TRAIN:
        w.wrap(g, "gpu.power_w", lambda old: lambda m: min(old(m), to_w - 5) if m >= onset else old(m))
        w.wrap(g, "gpu.sm_clock_mhz", lambda old: lambda m: old(m) * (1 - 0.131 * k) if m >= onset else old(m))
        w.wrap(g, "gpu.temperature_c", lambda old: lambda m: old(m) - ramp(m, onset, onset + 6, 0, 6 * k))
        w.wrap(g, "gpu.throttle_reasons", lambda old: lambda m: "SW_POWER_CAP" if m >= onset else old(m))
        w.wrap(g, "gpu.power_limit_w", lambda old: lambda m: to_w if m >= onset else old(m))
    w.wrap(J104, "workload.step_time_ms", lambda old: lambda m: old(m) * (1 + slowdown if m >= onset else 1))
    iid = f"int-{w.sid}"
    w.event("rack-12", "control.power_cap_change", None, onset, f"700W->{to_w}W per GPU" + (f"; intervention {iid}" if logged else ""),
            "dcim", {"interventionId": iid, "initiatedBy": "agent-datacenter-energy"} if logged else None)
    if logged:
        w.context["interventions"].append({
            "id": iid, "actionType": "power_cap", "reason": "Grid demand-response event",
            "targetEntityIds": ["rack-12"] + TRAIN, "affectedWorkloadIds": [J104],
            "parameters": {"gpuPowerLimitW": {"from": 700, "to": to_w}}, "executedBy": "agent-datacenter-energy",
            "activeFrom": iso(w.at(onset)), "activeUntil": iso(w.at(w.minutes + 60)),
            "expectedThrottleReasons": ["SW_POWER_CAP"],
            "expectedEffects": [
                {"metric": "gpu.power_w", "direction": "down", "maxChangePercent": 100 * (700 - to_w) / 700 + 5},
                {"metric": "gpu.sm_clock_mhz", "direction": "down", "maxChangePercent": 20 * k},
                {"metric": "workload.step_time_ms", "direction": "up", "maxChangePercent": predicted_max},
                {"metric": "workload.tokens_per_second", "direction": "down", "maxChangePercent": predicted_max}],
            "status": "active"})
    pct = slowdown * 100
    if logged and pct <= predicted_max:
        w.label(False, "intended_intervention", f"logged power cap to {to_w} W; step time +{pct:.0f}% (predicted <= {predicted_max}%)")
    elif logged:
        w.label(True, "intervention_overshoot", f"logged power cap to {to_w} W but step time +{pct:.0f}% vs <= {predicted_max}% predicted",
                root=J104, severity="warning", accept=["rack-12"] + TRAIN)
    else:
        w.label(True, "unexplained_power_cap", f"GPUs capped to {to_w} W with no intervention record; step time +{pct:.0f}%",
                root="rack-12", severity="warning", accept=[J104] + TRAIN)


def hall_overheat(w, onset=5, peak=30.0):
    """Hall air handler fails: inlet air climbs past the recommended 27C band."""
    for e, metric in (("room-01", "room.inlet_temp_c"), ("crac-01", "crac.supply_air_temp_c")):
        w.s[(e, metric)]["every"] = w.every(60)
        w.wrap(e, metric, lambda old: lambda m: old(m) + ramp(m, onset, onset + 10, 0, peak - 23.5))
    w.label(True, "facility_cooling_degradation", f"hall inlet air climbs to ~{peak:g}C (recommended max 27C)",
            root="room-01", severity="warning", accept=["crac-01"])


def diurnal_swing(w, amp=3.3):
    """A warmer day: bigger daily swing in hall temperature, still inside the recommended band."""
    w.diurnal_amp = amp
    w.label(False, "seasonal_or_diurnal_pattern", f"hall inlet swings +/-{amp:g}C over the day, inside 18-27C")


def link_down(w, port="leaf-12-p08", onset=6):
    """An in-service uplink drops (the benign twin is a reserved port that is down by design)."""
    for p in (port, UPLINKS[port]):
        w.wrap(p, "port.link_up", lambda old: lambda m: False if m >= onset else old(m))
    w.wrap(port, "port.link_downed_total", lambda old: lambda m: old(m) + (1 if m >= onset else 0))
    w.label(True, "network_link_down", f"in-service uplink {port} went down", root=port, severity="warning",
            accept=[UPLINKS[port], port.rsplit("-", 1)[0], "spine-01"])


def job_start(w, at=12):
    """Queued fine-tune job starts: idle GPUs warm up and power jumps. Expected load change."""
    w.ft_start = at
    w.label(False, "expected_load_change", "fine-tune job starts; its 8 GPUs go from idle to loaded")


def job_complete(w, at=11):
    """Running fine-tune job finishes: GPUs go idle and cool. Expected."""
    w.ft_start, w.ft_end = -120, at
    w.label(False, "expected_workload_phase", "fine-tune job completes; its 8 GPUs go idle")


def job_running(w):
    """Fine-tune job running for the whole window (normal operation, used for baselines)."""
    w.ft_start = -120


INJECTORS = {f.__name__: f for f in (
    thermal_throttle, spurious_sample, ecc_burst, background_ecc, link_crc, cdu_flow_drop, pdu_feed_loss,
    exporter_hang, hung_rank, replica_crash, traffic_change, pcie_downtrain, checkpoint, offline, thermal_drift,
    power_transient, power_cap, hall_overheat, diurnal_swing, link_down, job_start, job_complete, job_running)}

# name -> (window defaults, [(injector, params)])
PRESETS = {
    "normal":               (dict(start="2026-11-16T10:00:00Z", minutes=30, step=30), []),
    "thermal_throttle":     (dict(start="2026-11-16T09:00:00Z", minutes=30, step=30), [("thermal_throttle", {})]),
    "spurious_sample":      (dict(start="2026-11-16T14:00:00Z", minutes=15, step=30), [("spurious_sample", {})]),
    "ecc_fatal":            (dict(start="2026-11-16T11:00:00Z", minutes=20, step=30), [("ecc_burst", {})]),
    "background_ecc":       (dict(start="2026-11-16T00:00:00Z", minutes=1440, step=3600), [("background_ecc", {})]),
    "link_crc":             (dict(start="2026-11-16T13:00:00Z", minutes=40, step=60), [("link_crc", {})]),
    "cdu_flow_drop":        (dict(start="2026-11-16T15:00:00Z", minutes=40, step=60), [("cdu_flow_drop", {})]),
    "pdu_feed_loss":        (dict(start="2026-11-16T17:00:00Z", minutes=30, step=60), [("pdu_feed_loss", {})]),
    "exporter_hang":        (dict(start="2026-11-16T19:00:00Z", minutes=30, step=30), [("exporter_hang", {})]),
    "hung_rank":            (dict(start="2026-11-16T21:00:00Z", minutes=30, step=30), [("hung_rank", {})]),
    "replica_crash":        (dict(start="2026-11-16T12:00:00Z", minutes=30, step=30), [("replica_crash", {})]),
    "traffic_change":       (dict(start="2026-11-16T12:00:00Z", minutes=30, step=30), [("traffic_change", {})]),
    "pcie_downtrain":       (dict(start="2026-11-16T06:00:00Z", minutes=20, step=30), [("pcie_downtrain", {})]),
    "checkpoint_normal":    (dict(start="2026-11-16T08:00:00Z", minutes=20, step=10), [("checkpoint", {"duration_s": 90})]),
    "checkpoint_overrun":   (dict(start="2026-11-16T07:30:00Z", minutes=30, step=30), [("checkpoint", {"at": 6, "duration_s": 1440})]),
    "maintenance_outage":   (dict(start="2026-11-16T02:00:00Z", minutes=105, step=60), [("offline", {"down_from": 3, "down_to": 101, "maint": (0, 120)})]),
    "offline_after_maintenance": (dict(start="2026-11-16T04:00:00Z", minutes=60, step=60), [("offline", {"down_from": 0, "maint": (-120, 10)})]),
    "thermal_drift":        (dict(start="2026-11-16T00:00:00Z", minutes=4320, step=1800), [("thermal_drift", {})]),
    "power_transient":      (dict(start="2026-11-16T10:00:00Z", minutes=10, step=5), [("power_transient", {})]),
    "power_cap_logged":     (dict(start="2026-11-16T16:30:00Z", minutes=30, step=30), [("power_cap", {})]),
    "power_cap_unlogged":   (dict(start="2026-11-16T16:30:00Z", minutes=30, step=30), [("power_cap", {"logged": False})]),
    "hall_overheat":        (dict(start="2026-11-16T13:00:00Z", minutes=60, step=60), [("hall_overheat", {})]),
    "diurnal_swing":        (dict(start="2026-11-16T00:00:00Z", minutes=1440, step=1800), [("diurnal_swing", {})]),
    "link_down":            (dict(start="2026-11-16T09:00:00Z", minutes=30, step=60), [("link_down", {})]),
    "job_start":            (dict(start="2026-11-16T17:00:00Z", minutes=30, step=30), [("job_start", {})]),
    "job_complete":         (dict(start="2026-11-16T23:00:00Z", minutes=30, step=30), [("job_complete", {})]),
    "job_running":          (dict(start="2026-11-16T10:00:00Z", minutes=30, step=30), [("job_running", {})]),
}


def build(scenario, seed, start=None, minutes=None, step=None, injectors=None, name=None, title=None):
    """Build one scenario dict. `injectors` = [(injector_name, params)] overrides the preset's list."""
    win, inj = PRESETS[scenario]
    w = World(name or scenario, seed, start or win["start"], minutes or win["minutes"], step or win["step"])
    normal(w)
    for fn, params in (inj if injectors is None else injectors):
        INJECTORS[fn](w, **params)
    return w.render(title)


def dumps(sc):
    """Canonical serialisation: the same scenario always gives the same bytes."""
    return json.dumps(sc, indent=1, sort_keys=False) + "\n"


def main():
    ap = argparse.ArgumentParser(description="Seeded synthetic datacentre telemetry generator")
    ap.add_argument("--list", action="store_true", help="list scenarios and injectors")
    ap.add_argument("--scenario", default="normal")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--minutes", type=float, help="duration (default: the scenario's own)")
    ap.add_argument("--step", type=int, help="sample interval in seconds")
    ap.add_argument("--start", help="window start, e.g. 2026-11-16T10:00:00Z")
    ap.add_argument("--topology", default=os.path.join(HERE, "topology.json"),
                    help="topology the telemetry refers to (entity IDs are checked against it)")
    ap.add_argument("--out", default=os.path.join(HERE, "generated"))
    ap.add_argument("--jsonl", action="store_true", help="also write telemetry as one record per line")
    a = ap.parse_args()
    if a.list:
        for k, (win, inj) in PRESETS.items():
            print(f"{k:28} {win['minutes']:>5} min @ {win['step']:>4}s  {', '.join(f for f, _ in inj) or '(fault-free)'}")
        return
    topo = json.load(open(a.topology))
    ids = {e["id"] for v in topo.values() if isinstance(v, list) for e in v if isinstance(e, dict) and "id" in e}
    ids |= {p["id"] for v in topo.values() if isinstance(v, list) for e in v if isinstance(e, dict) for p in e.get("ports", []) or []}
    sc = build(a.scenario, a.seed, a.start, a.minutes, a.step)
    unknown = sorted({r["entityId"] for r in sc["telemetry"]} - ids)
    assert not unknown, f"telemetry refers to entities missing from the topology: {unknown[:5]}"
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, sc["scenarioId"] + ".json")
    open(path, "w").write(dumps(sc))
    if a.jsonl:
        with open(path + "l", "w") as f:
            for r in sc["telemetry"]: f.write(json.dumps(r) + "\n")
    print(f"{path}: {len(sc['telemetry'])} records, expected flag = {sc['expected']['flag']}")


if __name__ == "__main__":
    main()
