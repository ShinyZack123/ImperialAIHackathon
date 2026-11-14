#!/usr/bin/env python3
"""Deterministic telemetry flagger: decides what to escalate to the low-level agent.

    flag(telemetry, context) -> list[str]            one plain-English string per flag
    flag_window(context, telemetry) -> (incidents, suppressed)   structured version
    find_opportunities(context, telemetry) -> list[Opportunity]  capacity that is healthy but wasted
    IncidentTracker().update(incidents)              keeps incident IDs stable across successive calls

`context` is topology.enriched.json (built by build_context.py), optionally merged with a
scenario's own maintenance / intervention records via merge_context(). From it the flagger reads:
  metricCatalog         hard limits, plausible ranges, fatal error codes, bad state values
  baselines             what normal looks like, measured from fault-free runs
  counterRateBaselines  how fast error counters normally climb
  maintenance, interventions, port adminState, workload checkpoint policy  (the context gates)
All detector tunables are in config.json. Nothing here is random, learned, or reads the clock:
same input -> same output.

Pipeline:
  validate -> Hampel de-spike -> detectors -> context gates -> graph correlation -> message
"""
from __future__ import annotations
import bisect, hashlib, json, math, os, statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

# ============================================================ configuration (config.json)
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")) as _f: _CONF = json.load(_f)
CFG = {k: v for k, v in _CONF["CFG"].items() if not k.startswith("_")}
ROBUST = {k: tuple(v) for k, v in _CONF["ROBUST"].items()}          # metric -> (direction, min rel change, spread floor)
RATE = dict(_CONF["RATE_FLOOR_PER_MIN"])                            # counter -> background-rate floor per minute
PEER = {k: tuple(v) for k, v in _CONF["PEER"].items()}              # metric -> (min abs diff, spread floor)
LAYER = dict(_CONF["LAYER"])
GAUGE_UNITS = set(_CONF["GAUGE_UNITS"])
GATES = "maintenance window, workload phase (checkpoint/finished), our own interventions, invalid or stale samples, ports unused by design"

# ============================================================ helpers
def ts(s): return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
def hm(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%H:%M")
def mins(s): return f"{s / 60:.0f} min" if s >= 60 else f"{s:.0f} s"
def pct(v, base):
    r = (v - base) / base if base else float("inf")
    return f"{v / base:.0f}x" if base and abs(r) >= 10 else f"{r * 100:+.0f}%"
def is_num(v): return isinstance(v, (int, float)) and not isinstance(v, bool)

def sample_ok(r):
    """A sample the detectors may use: valid, not stale, confident enough."""
    q = r.get("quality") or {}
    return q.get("valid", True) and not q.get("stale") and q.get("confidence", 1.0) >= CFG["min_confidence"]

def iso(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def short_id(prefix, *parts):
    return f"{prefix}-{hashlib.sha256('|'.join(str(x) for x in parts).encode()).hexdigest()[:10]}"

def mad_scale(xs):
    m = statistics.median(xs)
    return m, 1.4826 * statistics.median([abs(x - m) for x in xs])

def interval(pts):
    if len(pts) < 2: return 0
    return statistics.median([b[0] - a[0] for a, b in zip(pts, pts[1:])])

def sample_period(pts, declared=None):
    if is_num(declared) and math.isfinite(declared) and declared > 0: return declared
    return interval(pts)

def gap_limit(pts, declared=None):
    return sample_period(pts, declared) * CFG["max_sample_gap_multiple"]

def longest_run(pts, pred, sample_interval=None):
    """pts: [(t, v)]. Longest consecutive run where pred(v). Returns (start, end, duration_s, n) or None."""
    return max(all_runs(pts, pred, sample_interval), key=lambda run: run[3], default=None)

def all_runs(pts, pred, sample_interval=None):
    """Every consecutive run where pred(v): [(start, end, duration_s, n)]."""
    out, cur, dt = [], None, sample_period(pts, sample_interval)
    max_gap = gap_limit(pts, sample_interval)
    for t, v in pts:
        # Unobserved time is not evidence that the predicate persisted.
        if cur and t - cur[1] > max_gap:
            out.append((cur[0], cur[1], cur[1] - cur[0] + dt, cur[2])); cur = None
        if pred(v): cur = (cur[0], t, cur[2] + 1) if cur else (t, t, 1)
        elif cur: out.append((cur[0], cur[1], cur[1] - cur[0] + dt, cur[2])); cur = None
    if cur: out.append((cur[0], cur[1], cur[1] - cur[0] + dt, cur[2]))
    return out

def persistent(run):
    return run and run[2] >= CFG["persist_s"] and run[3] >= CFG["persist_min_samples"]

def hampel(pts, k=None, n_sigma=None):
    """Replace isolated spikes with the local median (classic Hampel identifier)."""
    k = CFG["hampel_k"] if k is None else k
    n_sigma = CFG["hampel_sigma"] if n_sigma is None else n_sigma
    out, vs = [], [v for _, v in pts]
    for i, (t, v) in enumerate(pts):
        w = vs[max(0, i - k): i + k + 1]
        if len(w) >= 5:
            m, s = mad_scale(w)
            if s > 0 and abs(v - m) > n_sigma * s:
                out.append((t, m)); continue
        out.append((t, v))
    return out

def poisson_sf_bound(n, lam):
    """Chernoff upper bound on P(X >= n) for X ~ Poisson(lam)."""
    if n <= lam: return 1.0
    if lam <= 0: return 0.0
    return math.exp(-lam + n * (1 + math.log(lam / n)))

# ============================================================ data model
@dataclass
class Finding:
    detector: str
    severity: str           # info | warning | critical
    entity: str
    metric: str
    start: float
    end: float
    text: str
    method: str             # plain-English stats method used
    workloads: set = field(default_factory=set)
    change: float = None    # typical relative change vs baseline (signed fraction), when known
    note: str = ""          # extra context added by a gate that looked but did not suppress
    period: float = 0.0     # sampling period of the series behind this finding (seconds), 0 if unknown
    cleared: bool = False   # True only when later samples show the condition ended (never inferred from silence)

@dataclass
class Incident:
    severity: str
    root_entity: str
    entities: list
    workloads: list
    detectors: list
    findings: list
    message: str
    id: str = ""            # deterministic from root entity + onset; IncidentTracker keeps it stable across calls
    onset: float = 0.0      # earliest supporting evidence
    last_seen: float = 0.0  # latest supporting evidence
    status: str = "active"  # active | recovered (recovered = every finding was observed to clear)
    change: str = "new"     # set by IncidentTracker: new | escalated | rerooted | recovered | recurred | unchanged
    escalate: bool = True   # set by IncidentTracker: False when nothing changed since the previous call

@dataclass
class Opportunity:
    """Healthy capacity that is doing no useful work. Never a fault, never a flag: a proposal for the optimiser."""
    id: str
    kind: str               # reclaimable_gpus | reserved_idle_gpus | unallocated_idle_gpus
    entities: list
    workloads: list
    start: float
    end: float
    text: str
    method: str
    estimate: dict
    proposal: str

class Graph:
    def __init__(self, topo):
        self.ent, self.port_owner = {}, {}
        for k, v in topo.items():
            if isinstance(v, list):
                for e in v:
                    if isinstance(e, dict) and "id" in e and "type" in e:
                        self.ent[e["id"]] = e
                        for p in e.get("ports", []) or []:
                            self.port_owner[p["id"]] = e["id"]
                            self.ent[p["id"]] = dict(p, type="port", parentId=e["id"])
        self.workloads = {w["id"]: w for w in topo.get("workloads", [])}
        for w in self.workloads.values(): self.ent.setdefault(w["id"], dict(w, type="workload"))
        self.maint = topo.get("maintenance", [])
        self.interventions = topo.get("interventions", []) or []
        self.catalog = topo.get("metricCatalog", {}) or {}
        self.baselines = defaultdict(list)
        for b in topo.get("baselines", []) or []: self.baselines[b["metric"]].append(b)
        self.rates = {(r["metric"], r["selector"].get("entityType")): r["ratePerHour"]
                      for r in topo.get("counterRateBaselines", []) or []}
        self.up = defaultdict(set)   # entity -> direct upstream dependencies
        for c in topo.get("connections", []):
            s, d, r = c["sourceEntityId"], c["destinationEntityId"], c["relationship"]
            if r in ("cooled_by", "depends_on", "powered_by", "connected_to"): self.up[s].add(d)
            elif r == "contains": self.up[d].add(s)
        for e in self.ent.values():
            if e.get("parentId"): self.up[e["id"]].add(e["parentId"])
            for p in e.get("upstreamEntityIds", []) or []: self.up[e["id"]].add(p)
        for e in self.ent.values():   # cooling: served entity depends on server
            for sv in e.get("servedEntityIds", []) or []: self.up[sv].add(e["id"])
        self._cl = {}
        self.live_status = {}    # latest workload.status seen in the telemetry window

    def closure(self, eid):
        if eid in self._cl: return self._cl[eid]
        seen, stack = set(), [eid]
        while stack:
            x = stack.pop()
            for y in self.up.get(x, ()):
                if y not in seen: seen.add(y); stack.append(y)
        self._cl[eid] = seen
        return seen

    def etype(self, eid):
        return self.ent.get(eid, {}).get("type")

    def sampling_interval(self, eid, metric):
        definitions = self.ent.get(eid, {}).get("metricDefinitions", []) or []
        for definition in definitions + [self.catalog.get(metric, {})]:
            if definition.get("metric", metric) != metric: continue
            value = definition.get("samplingIntervalSeconds")
            if is_num(value) and math.isfinite(value) and value > 0: return value
        return None

    def baseline(self, eid, metric, cond=None):
        """Most specific stored baseline for this entity/metric whose conditions all hold. None if absent."""
        cond, e, best = cond or {}, self.ent.get(eid, {}), None
        for b in self.baselines.get(metric, ()):
            sel = b["selector"]
            if "entityIds" in sel:
                if eid not in sel["entityIds"]: continue
            elif sel.get("entityType") != e.get("type") or any(e.get(k) != v for k, v in sel.items() if k != "entityType"):
                continue
            if any(cond.get(k) != v for k, v in b["conditionedOn"].items()): continue
            if best is None or len(b["conditionedOn"]) > len(best["conditionedOn"]): best = b
        return best

    def limit(self, spec, eid):
        """Resolve a catalog limit: a number, or a pointer to the entity's / workload's own attribute."""
        if spec is None or is_num(spec): return spec, ""
        path = spec.get("fromEntity") or spec.get("fromWorkload")
        cur = self.workloads.get(eid) if "fromWorkload" in spec else self.ent.get(eid)
        for part in path.split("."):
            cur = cur.get(part) if isinstance(cur, dict) else None
        src = f", from {path}" if cur is not None else ", catalog default"
        val = cur if cur is not None else spec.get("default")
        if val is None: return None, ""
        if "fraction" in spec: return val * spec["fraction"], f", {spec['fraction'] * 100:.0f}% of {path.split('.')[-1]} {val:g}"
        return val, src

    def counter_rate_per_min(self, eid, metric):
        typ = "port" if eid in self.port_owner else self.etype(eid)
        return max(self.rates.get((metric, typ), 0.0) / 60.0, RATE.get(metric, 0.0))

    def intervention_for(self, f):
        """An intervention of ours that targets this finding's entity/workload and is active for its whole span."""
        g, lapsed = CFG["intervention_grace_s"], None
        for iv in self.interventions:
            if iv.get("status") == "cancelled": continue
            tgt = set(iv.get("targetEntityIds", [])) | set(iv.get("affectedWorkloadIds", []))
            if f.entity not in tgt and not (f.workloads & set(iv.get("affectedWorkloadIds", []))): continue
            until = ts(iv["activeUntil"]) + g if iv.get("activeUntil") else float("inf")
            if not ts(iv["activeFrom"]) - g <= f.start <= until: continue    # finding did not start under this intervention
            if f.end <= until: return iv, False                              # active for the finding's whole span
            lapsed = iv                                                      # started under it, outlived it: gate expired
        return lapsed, lapsed is not None

    def name(self, eid):
        e = self.ent.get(eid, {})
        n = e.get("name")
        return f"{eid} ({n})" if n and n != eid else eid

    def affected_workloads(self, eid):
        if eid in self.workloads: return {eid}
        eid = self.port_owner.get(eid, eid)
        out = set()
        for w in self.workloads.values():
            if w.get("status") in ("completed", "failed"): continue
            if eid in (w["allocation"].get("storageIds") or []):
                out.add(w["id"]); continue
            for g in w["allocation"]["gpuIds"]:
                if g == eid or eid in self.closure(g) or g in self.closure(eid):
                    out.add(w["id"]); break
        return out

    def in_maintenance(self, eid, t):
        """Entity, an ancestor, or its link peer is covered by a maintenance/reservation record at time t."""
        cands = {eid} | self.closure(eid)
        p = self.ent.get(eid, {})
        if p.get("type") == "port" and p.get("connectedPortId"):
            peer = p["connectedPortId"]
            cands |= {peer, self.port_owner.get(peer, peer)}
        for m in self.maint:
            if m.get("status") == "cancelled": continue
            end = m.get("actualEnd") if m.get("status") == "completed" else m.get("scheduledEnd")
            end = ts(end or m["scheduledEnd"]) + CFG["maintenance_grace_s"]
            if ts(m.get("actualStart") or m["scheduledStart"]) <= t <= end and cands & set(m["entityIds"]):
                return m
        return None

    def last_maintenance(self, eid):
        for m in self.maint:
            if eid in m["entityIds"]: return m
        return None

# ============================================================ main entry point
def flag_window(topo, telemetry, now=None):
    G = Graph(topo)
    raw = defaultdict(list)       # (entity, metric) -> [record]
    for r in telemetry: raw[(r["entityId"], r["metric"])].append(r)
    for v in raw.values(): v.sort(key=lambda r: r["timestamp"])
    G.live_status = {eid: rs[-1]["value"] for (eid, metric), rs in raw.items() if metric == "workload.status"}
    if now is None and raw: now = max(ts(rs[-1]["timestamp"]) for rs in raw.values())
    good = sample_ok

    def plausible(metric, v):
        pr = G.catalog.get(metric, {}).get("plausibleRange")
        return not (pr and is_num(v) and not pr["min"] <= v <= pr["max"])   # impossible reading = sensor glitch

    clean = {}
    for k, rs in raw.items():
        pts = [(ts(r["timestamp"]), r["value"]) for r in rs if good(r) and plausible(k[1], r["value"])]
        if pts and all(is_num(v) for _, v in pts) and rs[0].get("unit") in GAUGE_UNITS:
            pts = hampel(pts)
        clean[k] = pts
    F: list[Finding] = []
    gate_log = []      # (Finding, why): things a gate explained before any detector had to fire
    for (eid, metric), rs in raw.items():
        dropped = [r for r in rs if is_num(r["value"]) and not (good(r) and plausible(metric, r["value"]))]
        if dropped:
            t0, t1 = ts(dropped[0]["timestamp"]), ts(dropped[-1]["timestamp"])
            vals = sorted({r["value"] for r in dropped})
            gate_log.append((Finding("quality", "info", eid, metric, t0, t1,
                                     f"{len(dropped)} sample(s) ignored (e.g. {vals[-1]:g})", "sample quality gate"),
                             f"{len(dropped)} sample(s) on {eid} marked invalid/stale/low-confidence or physically impossible: ignored"))

    def wl_of(eid): return G.affected_workloads(eid)

    # ---------- 1. robust z-score vs baseline (median / MAD) + persistence
    def series_at(key):
        pts = clean.get(key, [])
        tsx = [t for t, _ in pts]
        def f(t):
            if not pts: return None
            i = min(len(pts) - 1, max(0, bisect.bisect_right(tsx, t) - 1))
            return pts[i][1]
        return f if pts else None

    for (eid, metric), pts in clean.items():
        if metric not in ROBUST or len(pts) < 8: continue
        direction, min_rel, floor = ROBUST[metric]
        b = G.baseline(eid, metric, {"phase": "train"} if metric.startswith("workload.") else {})
        dm = (b or {}).get("demandModel")
        drv = series_at((eid, dm["driverMetric"])) if dm else None
        if b and dm and drv:      # normal depends on demand: expected = intercept + slope * requests/s
            exp = lambda t: dm["intercept"] + dm["slope"] * drv(t)
            s0, cand, src = 1.4826 * dm["residualMad"], pts, "stored baseline, adjusted for current traffic"
        elif b:
            exp = lambda t, m_=b["median"]: m_
            s0, cand, src = 1.4826 * b["mad"], pts, "stored baseline from fault-free runs"
        else:                     # fallback: no stored baseline -> first part of this window
            nb = max(4, int(len(pts) * CFG["baseline_frac"]))
            med0, s0 = mad_scale([v for _, v in pts[:nb]])
            exp = lambda t, m_=med0: m_
            cand, src = pts[nb:], "start of this window (no stored baseline)"
        sign = 1 if direction == "up" else -1
        def z(t, v):
            e_ = exp(t)
            return sign * (v - e_) / max(s0, floor * abs(e_), 1e-9)
        def bad(tv):
            t, v = tv
            return z(t, v) > CFG["robust_z"] and abs(v - exp(t)) > min_rel * max(abs(exp(t)), 1e-9)
        run = longest_run([(t, (t, v)) for t, v in cand], bad, G.sampling_interval(eid, metric))
        if persistent(run):
            seg = [(t, v) for t, v in pts if run[0] <= t <= run[1]]
            t_w, worst = max(seg, key=lambda x: sign * x[1])
            base = exp(t_w)
            typical = statistics.median([(v - exp(t)) / exp(t) for t, v in seg if exp(t)])
            F.append(Finding("robust_z", "warning", eid, metric, run[0], run[1],
                             f"{metric} {'rose' if sign > 0 else 'fell'} from baseline {base:.4g} to {worst:.4g} "
                             f"({pct(worst, base)}, robust z={z(t_w, worst):.0f}) for {mins(run[2])}",
                             f"robust z-score (median/MAD) vs {src}, with {CFG['persist_s'] // 60}-min persistence",
                             wl_of(eid), change=typical))

    # ---------- 2. hard limits from the metric catalog (entity specs / SLAs) + persistence
    for (eid, metric), pts in clean.items():
        d = G.catalog.get(metric, {})
        nums = [(t, v) for t, v in pts if is_num(v)]
        for key, sev, above in (("critical", "critical", True), ("criticalBelow", "critical", False),
                                ("warn", "warning", True), ("warnBelow", "warning", False)):
            lim, src = G.limit((d.get("limits") or {}).get(key), eid)
            if lim is None: continue
            run = longest_run(nums, (lambda v: v > lim) if above else (lambda v: v < lim), G.sampling_interval(eid, metric))
            if persistent(run):
                seg = [v for t, v in nums if run[0] <= t <= run[1]]
                peak = max(seg) if above else min(seg)
                F.append(Finding("hard_limit", sev, eid, metric, run[0], run[1],
                                 f"{metric} {'above' if above else 'below'} its {sev} limit ({lim:.4g}{src}) for {mins(run[2])}, "
                                 f"{'peak' if above else 'lowest'} {peak:.4g}",
                                 "hard limit from the metric catalog, with persistence window", wl_of(eid)))
                break    # most severe limit only
        path = d.get("expectedFromEntity")
        if path:         # state that must match the hardware's rated value (e.g. PCIe generation/width)
            spec, _ = G.limit({"fromEntity": path}, eid)
            run = longest_run(nums, lambda v: spec is not None and v < spec, G.sampling_interval(eid, metric))
            if run and run[3] >= CFG["persist_min_samples"]:
                lo = min(v for _, v in nums)
                what = "PCIe link " + ("generation" if metric.endswith("gen") else "width") if metric.startswith("gpu.pcie") else metric
                F.append(Finding("hard_limit", "warning", eid, metric, run[0], run[1],
                                 f"{what} downtrained {spec} -> {lo:g} for {mins(run[2])}",
                                 "spec comparison (negotiated vs rated value on the entity)", wl_of(eid)))

    # ---------- 3. peer comparison across GPUs of the same workload (cross-sectional robust z)
    by_wl_metric_t = defaultdict(lambda: defaultdict(dict))
    for (eid, metric), rs in raw.items():
        if metric not in PEER: continue
        for r in rs:
            wl = (r.get("labels") or {}).get("workloadId")
            if wl and good(r): by_wl_metric_t[(wl, metric)][ts(r["timestamp"])][eid] = r["value"]
    for (wl, metric), tmap in by_wl_metric_t.items():
        min_abs, floor = PEER[metric]
        dev = defaultdict(list)
        for t, vals in sorted(tmap.items()):
            if len(vals) < 4: continue
            m, s = mad_scale(list(vals.values())); s = max(s, floor)
            for eid, v in vals.items():
                dev[eid].append((t, (v - m, (v - m) / s, m)))
        for eid, pts in dev.items():
            run = longest_run(pts, lambda d: abs(d[1]) > CFG["peer_z"] and abs(d[0]) > min_abs, G.sampling_interval(eid, metric))
            if persistent(run):
                seg = [d for t, d in pts if run[0] <= t <= run[1]]
                d = max(seg, key=lambda x: abs(x[0]))
                F.append(Finding("peer_z", "warning", eid, metric, run[0], run[1],
                                 f"{metric} {d[0] + d[2]:.4g} vs median {d[2]:.4g} of the other GPUs in {wl} "
                                 f"({d[0]:+.0f}, peer z={d[1]:.0f}) for {mins(run[2])}",
                                 "peer comparison: robust z across GPUs doing identical work", {wl}))

    # ---------- 4. CUSUM on GPU-vs-peer temperature residual (slow drift)
    for (wl, metric), tmap in by_wl_metric_t.items():
        if metric != "gpu.temperature_c": continue
        resid = defaultdict(list)
        for t, vals in sorted(tmap.items()):
            if len(vals) < 2: continue
            for eid, v in vals.items():
                others = [x for k, x in vals.items() if k != eid]
                resid[eid].append((t, v - statistics.median(others)))
        for eid, pts in resid.items():
            if len(pts) < 16: continue
            b = G.baseline(eid, metric, {"workloadType": G.workloads.get(wl, {}).get("type"), "relativeTo": "workload_peers"})
            if b:    # stored: how far a healthy GPU normally sits from its peers
                mu0, sd, rest = b["median"], max(1.4826 * b["mad"], CFG["cusum_sd_floor_c"]), pts
            else:    # fallback: start of this window
                nb = max(4, int(len(pts) * CFG["baseline_frac"]))
                base = [r for _, r in pts[:nb]]
                mu0, sd, rest = statistics.mean(base), max(statistics.pstdev(base), CFG["cusum_sd_floor_c"]), pts[nb:]
            k, h, S, alarm = CFG["cusum_k_sigma"] * sd, CFG["cusum_h_sigma"] * sd, 0.0, None
            for t, r in rest:
                S = max(0.0, S + (r - mu0 - k))
                if S > h and alarm is None: alarm = t
            tail = statistics.mean([r for _, r in pts[-max(4, len(pts) // 4):]])
            if alarm is not None and tail - mu0 > CFG["cusum_min_drift_c"]:
                F.append(Finding("cusum", "warning", eid, metric, alarm, pts[-1][0],
                                 f"temperature drifting up relative to identical peers: offset {mu0:+.1f}C -> {tail:+.1f}C "
                                 f"over {mins(pts[-1][0] - pts[0][0]) if pts[-1][0] - pts[0][0] < 7200 else str(round((pts[-1][0] - pts[0][0]) / 3600)) + ' h'} (CUSUM alarm at {hm(alarm)})",
                                 "CUSUM change detection on peer-relative residual", {wl}))

    # ---------- 5. counters: Poisson rate test + any-increment counters (from the catalog)
    for (eid, metric), pts in clean.items():
        rate = G.catalog.get(metric, {}).get("rate") or {}
        any_inc = "critical" if rate.get("criticalOnAnyIncrease") else "warning" if rate.get("warnOnAnyIncrease") else None
        if any_inc and len(pts) >= 2:
            for (t0, a), (t1, b_) in zip(pts, pts[1:]):
                if b_ > a:
                    F.append(Finding("hard_counter", any_inc, eid, metric, t1, t1,
                                     f"{metric} {a:g} -> {b_:g} at {hm(t1)} (this counter should never increase)",
                                     "any-increment rule from the metric catalog (never normal)", wl_of(eid)))
                    break
        if metric not in RATE or len(pts) < 4: continue
        lam_rate = G.counter_rate_per_min(eid, metric)     # measured normal rate, floored by config
        win = max(CFG["rate_window_s"], interval(pts))
        best, first_sig, last_sig = None, None, None
        for i in range(len(pts)):
            j = i
            while j + 1 < len(pts) and pts[j + 1][0] - pts[i][0] <= win: j += 1
            if j == i: continue
            n = pts[j][1] - pts[i][1]
            if n <= 0: continue   # flat or counter reset
            lam = lam_rate * (pts[j][0] - pts[i][0]) / 60
            p = poisson_sf_bound(n, lam)
            if best is None or p < best[0]: best = (p, n, lam, pts[i][0], pts[j][0])
            if p < CFG["poisson_p"]:
                if first_sig is None:     # onset = first sample in this window that shows the counter climbing
                    first_sig = next(pts[k][0] for k in range(i + 1, j + 1) if pts[k][1] > pts[k - 1][1])
                last_sig = pts[j][0] if last_sig is None else max(last_sig, pts[j][0])
        if best and best[0] < CFG["poisson_p"]:
            p, n, lam, t0, t1 = best
            # text describes the most extreme window; the span (onset .. last evidence) covers every abnormal one
            F.append(Finding("poisson_rate", "warning", eid, metric, first_sig, last_sig,
                             f"{metric} increased by {n:g} in {mins(t1 - t0)} vs ~{lam:.2g} expected at the normal rate "
                             f"(Poisson p<{max(p, 1e-300):.0e})",
                             "Poisson rate test on error counter vs measured normal rate", wl_of(eid)))

    # ---------- 6. discrete events and states
    for (eid, metric), rs in raw.items():
        vals = [(ts(r["timestamp"]), r["value"], r) for r in rs]
        d = G.catalog.get(metric, {})
        if d.get("eventCodes"):
            for t, v, r in vals:
                code = d["eventCodes"].get(str(int(v)), {"severity": d.get("defaultEventSeverity", "warning"), "meaning": "unlisted code"})
                if code["severity"] in ("warning", "critical"):
                    F.append(Finding("event", code["severity"], eid, metric, t, t,
                                     f"{'XID' if metric == 'gpu.xid' else metric} {int(v)} ({code['meaning']}) at {hm(t)}",
                                     "error-code table in the metric catalog", wl_of(eid)))
        elif metric == "inference.replica_event":
            for t, v, r in vals:
                if any(k in str(v) for k in ("OOMKilled", "CrashLoop", "Error")):
                    F.append(Finding("event", "warning", eid, metric, t, t, f"replica event at {hm(t)}: {v}",
                                     "known-bad event keywords", {eid}))
        elif d.get("badValues") and metric.startswith(("cdu.", "pdu.")):
            run = longest_run([(t, v) for t, v, _ in vals], lambda v: v in d["badValues"], G.sampling_interval(eid, metric))
            if run and run[3] >= 2:
                bad_v = next(v for t, v, _ in vals if t >= run[0])
                F.append(Finding("state", "critical", eid, metric, run[0], run[1],
                                 f"{metric} = {bad_v} since {hm(run[0])}", "state change to a fault value", wl_of(eid)))
        elif metric == "workload.status":
            for t, v, r in vals:
                if v == "failed":
                    F.append(Finding("state", "critical", eid, metric, t, t, f"workload status went to failed at {hm(t)}",
                                     "state change to a fault value", {eid})); break
        elif metric == "gpu.throttle_reasons":
            cls = d.get("valueClasses", {})
            kind = lambda v: cls.get(str(v), "hardware" if str(v).startswith("HW_") else "software" if str(v).startswith("SW_") else "ok")
            for want, det, label in (("hardware", "state", "hardware throttling"), ("software", "sw_throttle", "software-requested throttling")):
                run = longest_run([(t, v) for t, v, _ in vals], lambda v: kind(v) == want, G.sampling_interval(eid, metric))
                if run and run[3] >= CFG["persist_min_samples"]:
                    F.append(Finding(det, "warning", eid, metric, run[0], run[1],
                                     f"{label} ({next(v for t, v, _ in vals if t >= run[0] and kind(v) == want)}) for {mins(run[2])}",
                                     f"{want} throttle reason (class from the metric catalog), persistent", wl_of(eid)))

    # ---------- 7. liveness: is the training job actually making progress?
    for wid, w in G.workloads.items():
        status = [(ts(r["timestamp"]), r["value"]) for r in raw.get((wid, "workload.status"), [])]
        phase = [(ts(r["timestamp"]), r["value"]) for r in raw.get((wid, "workload.phase"), [])]
        cp_exp = (w.get("checkpointPolicy") or {}).get("expectedDurationSeconds") or (w.get("attributes") or {}).get("expectedCheckpointDurationSeconds")
        budget = CFG["checkpoint_overrun_multiple"] * cp_exp if cp_exp else None
        # 7a. workload-phase gate with a time budget: a checkpoint explains a pause, but only for so long
        cps = all_runs(phase, lambda v: v == "checkpoint", G.sampling_interval(wid, "workload.phase"))
        for c0, c1, dur, n in cps:
            if budget is None: continue
            if dur > budget:
                F.append(Finding("liveness", "critical", wid, "workload.phase", c0, c1,
                                 f"checkpoint running {mins(dur)} vs expected {cp_exp} s ({dur / cp_exp:.0f}x) with no step progress",
                                 f"phase-duration bound: checkpoint gate expired (budget = {CFG['checkpoint_overrun_multiple']}x expected = {budget:g} s)", {wid}))
            else:
                gate_log.append((Finding("liveness", "info", wid, "workload.phase", c0, c1, f"checkpoint for {mins(dur)}", "workload phase gate"),
                                 f"checkpoint lasted ~{dur:.0f} s, within its {budget:g} s budget ({CFG['checkpoint_overrun_multiple']}x the expected {cp_exp} s): GPU dip expected"))
        # 7b. progress heartbeat: the step counter must advance while the scheduler says "running"
        progress_config = w.get("progress") or {}
        progress_metric = progress_config.get("counterMetric", "workload.steps_completed")
        progress_unit = progress_config.get("counterUnit", "training steps")
        steps = clean.get((wid, progress_metric), [])
        if len(steps) < 3: continue
        step_s = ((w.get("progress") or {}).get("expectedStepTimeMs") or w.get("training", {}).get("stepTimeMs") or 1000) / 1000
        limit = max(CFG["liveness_min_s"], CFG["liveness_step_multiple"] * step_s)
        flats, cur = [], None
        max_gap = gap_limit(steps, G.sampling_interval(wid, progress_metric))
        for (ta, a), (tb, b) in zip(steps, steps[1:]):
            if tb - ta > max_gap or b > a:
                if cur: flats.append(cur); cur = None
            else: cur = (cur[0], tb) if cur else (ta, tb)
        if cur: flats.append(cur)
        for f0, f1 in flats:
            in_cp = sum(max(0, min(f1, c1 + 60) - max(f0, c0 - 60)) for c0, c1, _, _ in cps)
            flat = f1 - f0
            if flat - in_cp <= limit: continue          # short, or covered by a checkpoint (handled in 7a)
            st = next((v for t, v in reversed(status) if t <= f1), status[0][1] if status else "running")
            # only time spent in state "running" counts as stalled (queued or finished time does not)
            not_running = [t for t, v in status if v != "running" and f0 <= t <= f1]
            if st == "running" and not_running and f1 - max(not_running) - in_cp <= limit: continue
            if st != "running":
                gate_unit = "steps" if progress_metric == "workload.steps_completed" else progress_unit
                gate_log.append((Finding("liveness", "info", wid, progress_metric, f0, f1, f"no {gate_unit} for {mins(flat)}", "workload status gate"),
                                 f"{wid} status is '{st}': no further steps and idle GPUs are expected"))
                continue
            if progress_metric == "workload.steps_completed":
                description = (f"no training steps for {mins(flat)} (stuck at step {steps[-1][1]}) while scheduler reports running; "
                               f"expected one step every {step_s:.1f} s")
            else:
                description = (f"no increase in {progress_unit} for {mins(flat)} (counter {steps[-1][1]}) while scheduler reports running; "
                               f"configured progress timeout {limit:g} s")
            F.append(Finding("liveness", "critical", wid, progress_metric, f0, f1,
                             description, "progress heartbeat (counter must advance)", {wid}))

    # ---------- 8. availability and telemetry health
    for (eid, metric), rs in raw.items():
        pts = [(ts(r["timestamp"]), r["value"]) for r in rs]
        if metric in ("node.up", "port.link_up"):
            target = (rs[0].get("labels") or {}).get("port", eid)
            run = longest_run(pts, lambda v: v is False, G.sampling_interval(eid, metric))
            if run and run[3] >= 2:
                F.append(Finding("availability", "warning", target, metric, run[0], run[1],
                                 f"{'node down' if metric == 'node.up' else 'link down'} for {mins(run[2])}",
                                 "down-state persistence", wl_of(target)))
        elif metric.startswith("exporter.") and metric.endswith("_up"):
            run = longest_run(pts, lambda v: v is False, G.sampling_interval(eid, metric))
            if run and run[3] >= 2:
                F.append(Finding("telemetry", "warning", eid, metric, run[0], run[1],
                                 f"metrics exporter {metric.split('.')[1]} down for {mins(run[2])}: node is unmonitored",
                                 "collector health check", wl_of(eid)))
    stale_by_entity = {}
    for (eid, metric), rs in raw.items():
        run = longest_run([(ts(r["timestamp"]), r["quality"].get("stale", False)) for r in rs], lambda v: v, G.sampling_interval(eid, metric))
        if run and run[3] >= 4:
            stale_by_entity.setdefault(eid, (metric, run))
        nums = [(ts(r["timestamp"]), r["value"]) for r in rs if is_num(r["value"])]
        if rs[0].get("unit") in GAUGE_UNITS and len(nums) >= 2 * CFG["frozen_samples"]:
            nb = max(4, int(len(nums) * CFG["baseline_frac"]))
            if len({v for _, v in nums[:nb]}) > 1:   # it used to move...
                same = [(t, v == nums[i - 1][1] if i else False) for i, (t, v) in enumerate(nums)]
                r2 = longest_run(same, lambda v: v, G.sampling_interval(eid, metric))
                frozen_v = next((v for t, v in nums if r2 and t == r2[1]), None)
                at_bound = frozen_v is not None and (frozen_v == 0 or (rs[0].get("unit") == "%" and frozen_v >= 100))  # dead or saturated is not frozen
                # a coarsely-rounded gauge repeats by chance: judge the run against this signal's own repeat habit
                outside = [v for t, v in same if not (r2 and r2[0] <= t <= r2[1])]
                p_rep = (sum(outside) + 1) / (len(outside) + 2)          # chance two consecutive samples are equal
                by_chance = p_rep ** r2[3] if r2 else 1.0
                if r2 and r2[3] + 1 >= CFG["frozen_samples"] and not at_bound and by_chance < CFG["frozen_p"]:
                    stale_by_entity.setdefault(eid, (metric, r2))
    for eid, (metric, run) in stale_by_entity.items():
        F.append(Finding("telemetry", "warning", eid, metric, run[0], run[1],
                         f"{metric} frozen/stale for {mins(run[2])} (identical or stale-marked values)",
                         "frozen-value / staleness detector", wl_of(eid)))

    # ============================================================ context gates
    # A gate suppresses a statistical hit that the context explains. Every gate expires:
    # maintenance until the window closes (+grace), an intervention while it is active and
    # only up to the effect it predicted, a checkpoint up to its time budget (detector 7).
    kept, suppressed = [], list(gate_log)
    for f in F:
        why = None
        if f.detector in ("availability", "telemetry", "robust_z", "hard_limit"):
            m = G.in_maintenance(f.entity, f.start)
            if m and G.in_maintenance(f.entity, f.end) is m:
                why = f"inside {m['id']} ({m['type']})"
            elif m:      # started inside the window but is still there after it closed: gate expired
                end = ts((m.get("actualEnd") if m.get("status") == "completed" else None) or m["scheduledEnd"])
                f.note = f"began inside {m['id']} ({m['type']}) but persists {mins(f.end - end)} after that window closed"
        if not why and f.detector == "availability":
            p = G.ent.get(f.entity, {})
            if p.get("type") == "port":
                admin = p.get("adminState") or ("up" if p.get("connectionId") else "down")
                if admin != "up": why = f"port is administratively '{admin}' (unused by design)"
        if not why and f.detector in ("robust_z", "sw_throttle"):
            iv, expired = G.intervention_for(f)
            if iv and expired:
                f.note = f"intervention {iv['id']} ended {iv['activeUntil']} but the effect persists"
            elif iv and f.detector == "sw_throttle":
                if any(r in f.text for r in iv.get("expectedThrottleReasons", [])):
                    why = f"requested by our own intervention {iv['id']} ({iv.get('actionType', '?')})"
            elif iv:
                eff = next((e for e in iv.get("expectedEffects", []) if e["metric"] == f.metric), None)
                got = abs(f.change or 0) * 100
                if eff and (f.change or 0) * (1 if eff["direction"] == "up" else -1) > 0:
                    if got <= eff["maxChangePercent"]:
                        why = (f"expected effect of our own intervention {iv['id']} "
                               f"({got:.0f}% vs up to {eff['maxChangePercent']:g}% predicted)")
                    else:
                        f.note = (f"our intervention {iv['id']} predicted at most {eff['maxChangePercent']:g}% "
                                  f"but the change is {got:.0f}%: worse than planned")
        (suppressed if why else kept).append((f, why))
    F = [f for f, _ in kept]

    # Recovery needs positive evidence: the same series kept reporting after the condition ended.
    # Discrete events latch, and a series that simply went quiet is unknown, not recovered.
    for f in F:
        rs = raw.get((f.entity, f.metric)) or []
        times = [ts(r["timestamp"]) for r in rs if sample_ok(r)]
        f.period = sample_period([(t, 0) for t in times], G.sampling_interval(f.entity, f.metric)) if times else 0.0
        latched = f.detector in ("event", "hard_counter") or (f.detector == "state" and f.start == f.end)
        quiet_for = max(CFG["recovered_after_s"], CFG["max_sample_gap_multiple"] * f.period)
        f.cleared = bool(times) and not latched and f.detector != "cusum" and times[-1] - f.end >= quiet_for

    return correlate(G, F, now), [(f, w) for f, w in suppressed]


def merge_context(topology, scenario=None):
    """Context = enriched topology + any maintenance/intervention records shipped with a scenario."""
    extra = (scenario or {}).get("contextRecords") or {}
    if not any(extra.values()): return topology
    ctx = dict(topology)
    for k in ("maintenance", "interventions"):
        ctx[k] = list(topology.get(k, [])) + list(extra.get(k, []))
    return ctx


def flag(telemetry, context):
    """The pure function from the brief: telemetry + context in, one plain-English string per flag out."""
    incidents, _ = flag_window(context, telemetry)
    return [i.message for i in incidents]

# ============================================================ correlation + message
def correlate(G, F, now):
    """Group findings into incidents and pick each incident's likely root.

    Sharing a workload is not enough to merge: two faults at different times on one job are two incidents.
      1. Findings on hardware/facility entities join when they are on the same component, server or
         redundancy group and their spans touch.
      2. Such clusters join when one depends on the other (cooling, power, network path), their spans
         touch and the downstream one did not begin first; or when they hit the same workload and
         began together (one common cause).
      3. A workload-level symptom (slow steps, stalled progress) goes to the cluster on that workload
         that began most recently before it. With no such cluster, symptoms group among themselves.
    "Together"/"touch" = within correlate_onset_gap_s (or a few sampling periods for sparse data).
    """
    if not F: return []
    if now is None: now = max(f.end for f in F)
    owner = lambda f: G.port_owner.get(f.entity, f.entity)
    def gap(a, b): return max(CFG["correlate_onset_gap_s"], CFG["max_sample_gap_multiple"] * max(a.period, b.period))
    def touch(a, b): return max(a.start, b.start) - min(a.end, b.end) <= gap(a, b)
    def structural(f):
        k = {f.entity, owner(f)}
        rg = G.ent.get(f.entity, {}).get("redundancyGroupId")
        if rg: k.add(rg)
        par = G.ent.get(f.entity, {}).get("parentId")
        if par and G.ent.get(par, {}).get("type") == "server": k.add(par)
        return k
    def upstream_of(a, b):       # b depends on a
        return owner(a) in G.closure(owner(b)) or owner(a) in G.closure(b.entity)
    keys = [structural(f) for f in F]
    parent = list(range(len(F)))
    def find(i):
        while parent[i] != i: parent[i] = parent[parent[i]]; i = parent[i]
        return i
    def clusters(idx):
        out = defaultdict(list)
        for i in idx: out[find(i)].append(i)
        return list(out.values())
    phys = [i for i, f in enumerate(F) if f.entity not in G.workloads]
    symptoms = [i for i, f in enumerate(F) if f.entity in G.workloads]
    for n, i in enumerate(phys):                                   # 1. same component, spans touch
        for j in phys[n + 1:]:
            if keys[i] & keys[j] and touch(F[i], F[j]): parent[find(i)] = find(j)
    def summary(c):              # (earliest finding, last evidence, workloads hit, representative index, members)
        first = min(c, key=lambda i: F[i].start)
        return (F[first], max(F[i].end for i in c), set().union(*[F[i].workloads for i in c]), c[0], c)
    stage1 = [summary(c) for c in clusters(phys)]
    for n, (fa, la, wa, ia, ca) in enumerate(stage1):              # 2. dependency, or one cause hitting one workload
        for fb, lb, wb, ib, cb in stage1[n + 1:]:
            g = gap(fa, fb)
            together = abs(fa.start - fb.start) <= g
            overlap = max(fa.start, fb.start) - min(la, lb) <= g
            a_up = any(upstream_of(F[i], F[j]) for i in ca for j in cb)
            b_up = any(upstream_of(F[j], F[i]) for i in ca for j in cb)
            caused = overlap and ((a_up and fb.start >= fa.start - g) or (b_up and fa.start >= fb.start - g))
            if caused or (wa & wb and together): parent[find(ia)] = find(ib)
    causes = [summary(c) for c in clusters(phys)]
    loose = []
    for i in symptoms:                                             # 3. symptom -> most recent cause before it
        f = F[i]
        cands = [c for c in causes if f.workloads & c[2] and c[0].start <= f.start + gap(f, c[0]) and f.start <= c[1] + gap(f, c[0])]
        if cands: parent[find(i)] = find(max(cands, key=lambda c: c[0].start)[3])
        else: loose.append(i)
    for n, i in enumerate(loose):                                  # symptoms with no cause in sight
        for j in loose[n + 1:]:
            a_, b_ = F[i], F[j]
            if ({a_.entity} | a_.workloads) & ({b_.entity} | b_.workloads) and (abs(a_.start - b_.start) <= gap(a_, b_) or touch(a_, b_)):
                parent[find(i)] = find(j)
    groups = defaultdict(list)
    for i, f in enumerate(F): groups[find(i)].append(f)

    out = []
    sev_rank = {"info": 0, "warning": 1, "critical": 2}
    for fs in groups.values():
        def layer(f):
            typ = G.ent.get(G.port_owner.get(f.entity, f.entity), {}).get("type") or ("port" if f.entity in G.port_owner else "")
            if f.detector == "telemetry": return 3
            if f.detector == "availability": return {"server": 0, "nic": 1, "port": 2}.get(typ, 1)
            if f.entity in G.port_owner: return 1
            return LAYER.get(typ, 3)
        # Root = what went wrong first. Findings that began clearly after the first physical evidence
        # are effects or later developments; among the early ones, prefer the layer closest to the facility.
        physical = [f for f in fs if f.entity not in G.workloads]
        first = min(f.start for f in (physical or fs))
        def late(f):
            return f.start - first > max(CFG["root_onset_tolerance_s"], CFG["max_sample_gap_multiple"] * f.period)
        fs.sort(key=lambda f: (late(f), layer(f), -sev_rank[f.severity], f.start))
        root = fs[0]
        sev = max(fs, key=lambda f: sev_rank[f.severity]).severity
        wls = sorted(set().union(*[f.workloads for f in fs]))
        ents = sorted({f.entity for f in fs})
        lines = [f"[{sev.upper()}] {G.name(root.entity)}: {root.text}."]
        if len(fs) > 1:
            lines.append("Corroborating evidence:")
            buckets = defaultdict(list)
            for f in fs[1:]: buckets[(f.detector, f.metric)].append(f)
            for (det, metric), bs in buckets.items():
                if len(bs) > 2:
                    lines.append(f"  - {len(bs)} entities ({', '.join(b.entity for b in bs)}): {bs[0].text} [{bs[0].method}]")
                else:
                    for f in bs: lines.append(f"  - {G.name(f.entity)}: {f.text} [{f.method}]")
        methods = list(dict.fromkeys(f.method for f in fs))       # every method that fired, root cause first
        lines.append("Why flagged: " + "; ".join(methods) + ".")
        notes = sorted({f.note for f in fs if f.note})
        lines.append(f"Not explained by: {GATES}." + (" Gate notes: " + "; ".join(notes) + "." if notes else ""))
        # graph context
        ctx = context_notes(G, root, fs)
        if ctx: lines.append("Context: " + " ".join(ctx))
        if wls:
            imp = []
            for w in wls:
                wd = G.workloads.get(w, {})
                gpus = (wd.get("allocation") or {}).get("gpuIds", [])
                hosts = sorted({G.ent.get(g, {}).get("parentId") for g in gpus} - {None})
                imp.append(f"{w} ({wd.get('type', '?')}, priority {wd.get('priority', '?')}, status {G.live_status.get(w, wd.get('status', '?'))}, "
                           f"{len(gpus)} GPUs on {', '.join(hosts)})")
            lines.append("Blast radius: " + "; ".join(imp) + ".")
        t0, t1 = min(f.start for f in fs), max(f.end for f in fs)
        fmt = (lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d %H:%M"))
        status = "recovered" if all(f.cleared for f in fs) else "active"
        lines.append(f"Window: {fmt(t0)} to {fmt(t1)} UTC. Likely root entity: {root.entity}. Status: "
                     + ("recovered (every flagged condition was observed to clear)." if status == "recovered"
                        else "active (no evidence that it has cleared)."))
        out.append(Incident(sev, root.entity, ents, wls, sorted({f.detector for f in fs}), fs, "\n".join(lines),
                            id=short_id("inc", root.entity, iso(t0)), onset=t0, last_seen=t1, status=status))
    out.sort(key=lambda i: (-sev_rank[i.severity], i.onset, i.root_entity))
    return out


# ============================================================ incident identity across calls
class IncidentTracker:
    """Gives incidents a stable identity across successive flag_window calls on a growing or sliding window.

    flag_window is pure and recomputes from scratch, so the same fault would otherwise look new on
    every call. The tracker matches each incident to the one it continues (same root, or overlapping
    entities), carries the first ID forward, and says whether anything changed. Only changed incidents
    need to be sent to the agent again:
        new | escalated (severity rose) | rerooted (likely root changed) | recovered | recurred | unchanged
    State lives here, not in the detector: same sequence of calls -> same IDs.
    """
    RANK = {"info": 0, "warning": 1, "critical": 2}

    def __init__(self):
        self.open = {}      # id -> {"root", "entities", "severity", "status", "onset"}
        self.closed = []    # ids that dropped out of the window on the last update

    def update(self, incidents):
        prev, cur, used = self.open, {}, set()
        for inc in incidents:
            ents = set(inc.entities) | {inc.root_entity}
            best = None
            for tid, t in prev.items():
                if tid in used: continue
                overlap = len(ents & t["entities"])
                if not overlap: continue
                score = (t["root"] == inc.root_entity, overlap, -abs(t["onset"] - inc.onset))
                if best is None or score > best[0]: best = (score, tid)
            if best is None:
                tid = inc.id
                while tid in cur or tid in prev: tid += "x"      # never reuse a live ID
                inc.id, inc.change = tid, "new"
            else:
                tid = best[1]; t = prev[tid]; used.add(tid)
                inc.id = tid
                if t["status"] != inc.status: inc.change = "recovered" if inc.status == "recovered" else "recurred"
                elif self.RANK[inc.severity] > self.RANK[t["severity"]]: inc.change = "escalated"
                elif t["root"] != inc.root_entity: inc.change = "rerooted"
                else: inc.change = "unchanged"
            inc.escalate = inc.change != "unchanged"
            cur[tid] = {"root": inc.root_entity, "entities": ents, "severity": inc.severity,
                        "status": inc.status, "onset": inc.onset}
        self.closed = sorted(set(prev) - used)
        self.open = cur
        return incidents


# ============================================================ optimisation opportunities
FINISHED = {"completed", "succeeded", "failed", "cancelled", "canceled"}
WAITING = {"queued", "pending", "suspended", "paused"}

def find_opportunities(topo, telemetry, now=None, incidents=()):
    """Healthy GPUs doing no useful work at `now`: the optimiser's input, kept apart from fault flags.

    A GPU qualifies when fresh, valid utilisation samples stay under idle_util_percent for at least
    persist_s, right up to `now`, and it is one of:
      reclaimable_gpus      still allocated to a job that has finished
      reserved_idle_gpus    held for a job that has not started
      unallocated_idle_gpus allocated to nothing
    Idle GPUs of a *running* job are not listed: that is a possible fault and belongs to flag_window.
    Unknown is never idle: stale or missing telemetry, maintenance, and anything an open incident
    touches (pass `incidents`) are excluded.
    """
    G = Graph(topo)
    util, power, status_at, last = defaultdict(list), defaultdict(list), {}, None
    for r in telemetry:
        t = ts(r["timestamp"])
        last = t if last is None else max(last, t)
        m = r["metric"]
        if m == "workload.status":
            if r["entityId"] not in status_at or t >= status_at[r["entityId"]][0]: status_at[r["entityId"]] = (t, r["value"])
        elif m in ("gpu.utilization_percent", "gpu.power_w") and sample_ok(r) and is_num(r["value"]):
            pr = G.catalog.get(m, {}).get("plausibleRange")
            if pr and not pr["min"] <= r["value"] <= pr["max"]: continue
            (util if m == "gpu.utilization_percent" else power)[r["entityId"]].append((t, r["value"]))
    if last is None: return []
    if now is None: now = last
    blocked = set()
    for inc in incidents:
        if inc.status == "active": blocked |= set(inc.entities) | {inc.root_entity}
    holder = {}                                   # gpu -> workload that holds it (a live job wins over a finished one)
    for w in G.workloads.values():
        st = status_at.get(w["id"], (0, w.get("status")))[1]
        for g in (w.get("allocation") or {}).get("gpuIds", []) or []:
            if g not in holder or holder[g][1] in FINISHED: holder[g] = (w, st)
    found = defaultdict(list)                     # (kind, workload id) -> [(gpu, start, end, mean idle watts)]
    for g, e in sorted(G.ent.items()):
        if e.get("type") != "gpu" or not util.get(g): continue
        w, st = holder.get(g, (None, None))
        kind = ("unallocated_idle_gpus" if w is None else "reclaimable_gpus" if st in FINISHED
                else "reserved_idle_gpus" if st in WAITING else None)
        if kind is None: continue
        if (blocked & ({g} | G.closure(g))) or G.in_maintenance(g, now): continue
        pts = sorted(util[g])
        declared = G.sampling_interval(g, "gpu.utilization_percent")
        runs = all_runs(pts, lambda v: v < CFG["idle_util_percent"], declared)
        run = runs[-1] if runs else None
        if not persistent(run) or run[1] != pts[-1][0] or now - run[1] > gap_limit(pts, declared): continue
        watts = [v for t, v in power.get(g, ()) if run[0] <= t <= run[1]]
        found[(kind, w["id"] if w else "")].append((g, run[0], run[1], statistics.mean(watts) if watts else None))
    out = []
    for (kind, wid), rows in sorted(found.items()):
        gpus = [g for g, *_ in rows]
        start, end = max(r[1] for r in rows), max(r[2] for r in rows)       # since when the whole group has been idle
        gpu_hours = sum(r[2] - r[1] for r in rows) / 3600
        known = [r for r in rows if r[3] is not None]
        est = {"gpuCount": len(gpus), "idleSeconds": round(end - start), "idleGpuHours": round(gpu_hours, 2),
               "idlePowerW": round(sum(r[3] for r in known)) if len(known) == len(rows) else None,
               "idleEnergyKwh": round(sum(r[3] * (r[2] - r[1]) for r in known) / 3.6e6, 3) if len(known) == len(rows) else None}
        w = G.workloads.get(wid, {})
        req = w.get("requirements") or {}
        st = status_at.get(wid, (0, w.get("status")))[1]
        cost = f"{est['idleGpuHours']:g} GPU-hours" + (f" and {est['idleEnergyKwh']:g} kWh" if est["idleEnergyKwh"] is not None else "") + " so far"
        if kind == "reclaimable_gpus":
            text = f"{len(gpus)} GPU(s) still allocated to {G.name(wid)} (status {st}) have been idle for {mins(end - start)}: {cost}."
            proposal = f"Release the allocation of {wid} so the scheduler can place other work on these GPUs."
        elif kind == "reserved_idle_gpus":
            extra = ", ".join(x for x in (f"priority {w['priority']}" if "priority" in w else "",
                                          "preemptible" if req.get("preemptible") else "") if x)
            text = (f"{len(gpus)} GPU(s) held for {G.name(wid)} (status {st}{', ' + extra if extra else ''}) "
                    f"have been idle for {mins(end - start)}: {cost}.")
            proposal = f"Start {wid}, or lend these GPUs to other work until it starts."
        else:
            text = f"{len(gpus)} unallocated GPU(s) have been idle for {mins(end - start)}: {cost}."
            proposal = "Available capacity: place queued or latency-sensitive work here."
        out.append(Opportunity(short_id("opp", kind, wid, *gpus), kind, gpus, [wid] if wid else [], start, end, text,
                               f"idle run: valid utilisation under {CFG['idle_util_percent']:g}% for at least "
                               f"{CFG['persist_s'] // 60} min up to the evaluation time, allocation and job status from context",
                               est, proposal))
    return out

def context_notes(G, root, fs):
    notes = []
    e = G.ent.get(root.entity, {})
    if e.get("type") == "cooling_system":
        served = [g for g, x in G.ent.items() if x.get("type") == "gpu" and root.entity in G.closure(g)]
        notes.append(f"{len(served)} GPUs depend on this unit via the coolant loop; expect correlated temperature rise across them.")
    if e.get("type") == "power_system" and e.get("redundancyGroupId"):
        peers = [x for x, y in G.ent.items() if y.get("redundancyGroupId") == e["redundancyGroupId"]]
        failed = {f.entity for f in fs if f.detector == "state"}
        notes.append(f"Redundancy group {e['redundancyGroupId']} = {peers}; " +
                     (f"{len(failed)} member(s) in fault, so the rack has no power redundancy." if failed else "no member reports an input fault."))
    if root.entity in G.port_owner:
        p = G.ent[root.entity]
        cab = next((x for x, y in G.ent.items() if y.get("type") == "cable" and root.entity in (y.get("sourcePortId"), y.get("destinationPortId"))), None)
        notes.append(f"Link {root.entity} <-> {p.get('connectedPortId')} via {cab}; if the far end is clean, suspect the cable/optic.")
    if e.get("type") == "gpu" and root.detector in ("peer_z", "hard_limit", "cusum"):
        notes.append("Peers in the same job are normal, so this is local to the GPU (not facility cooling or workload-wide).")
    if e.get("type") == "server" or root.detector == "availability":
        m = G.last_maintenance(G.port_owner.get(root.entity, root.entity))
        if m: notes.append(f"Last maintenance {m['id']} ({m['type']}) ended {m.get('actualEnd') or m['scheduledEnd']} with status {m['status']}; entity has not come back.")
    if root.detector == "telemetry":
        notes.append("Readings from this entity cannot be trusted; treat its metrics as unknown, not as normal.")
    return notes

# ============================================================ CLI
if __name__ == "__main__":
    import json, sys
    topo = json.load(open(sys.argv[1]))
    sc = json.load(open(sys.argv[2]))
    incidents, _ = flag_window(merge_context(topo, sc), sc["telemetry"])
    for i in incidents: print(i.message, "\n")
    if not incidents: print("No flags.")
