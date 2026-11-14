#!/usr/bin/env python3
"""Connector between the deterministic flagger and the Crosslayer operator console.

    python3 bridge.py catalog                         # scenarios the console can replay
    python3 bridge.py analyse  <scenarioId>           # run the flagger at 24 replay cut-offs
    python3 bridge.py entity   <scenarioId> <twinId>  # telemetry log + series behind one map entity

Every command prints one JSON document on stdout. Errors print {"error": "..."} and exit 2.

The console's map uses its own stable IDs (rack-a01, gpu-a01-01, link-a01 ...). The flagging
topology uses the site's source IDs (server-12-01, gpu-12-01-00, leaf-12-p07 ...). This module is
the only place the two are translated, so neither side has to know the other's naming.

The replay mirrors demo.py: at every cut-off the flagger sees only the records up to that
instant, exactly as it would live. Nothing here is random or reads the clock, so the same
scenario always produces the same analysis (the cache is keyed on the code and config that
produced it). Generated "showcase" scenarios come from generate.py with fixed seeds.
"""
from __future__ import annotations
import hashlib, json, os, re, sys, time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from flagger import Graph, flag_window, merge_context, ts   # noqa: E402

SCHEMA_VERSION = "1.0.0"
STEPS = 24                       # replay cut-offs; matches the console's 24-position replay slider
MAX_LOG_RECORDS = 4000           # bound the per-entity payload
CACHE = os.path.join(HERE, "generated", "console")
SEV_RANK = {"impacted": 1, "stale": 2, "warning": 3, "critical": 4}

# ------------------------------------------------------------------ scenario catalogue
# Showcase scenarios: the full 32-GPU hall from generate.py (every GPU, power, cooling and network
# series), with one or more injectors. Fixed seeds, so they are reproducible byte for byte.
D = "2026-11-14T"
SHOWCASE = {
    "S01-straggler-gpu-thermal-throttle": dict(
        title="One GPU overheats and throttles; the 16-GPU training job slows to its pace",
        window=dict(start=D + "09:00:00Z", minutes=30, step=30),
        injectors=[("thermal_throttle", dict(gpu="gpu-12-02-05", onset=6, peak=90.0, ramp_min=9))]),
    "S02-cooling-pump-fault": dict(
        title="CDU pump fault: coolant flow halves and every loaded GPU heats up",
        window=dict(start=D + "15:00:00Z", minutes=40, step=60),
        injectors=[("cdu_flow_drop", dict(onset=8, frac=0.5))]),
    "S03-ecc-burst-gpu-off-bus": dict(
        title="Memory errors burst on one GPU, then it falls off the bus (a second GPU shows normal background ECC)",
        window=dict(start=D + "11:00:00Z", minutes=20, step=30),
        injectors=[("ecc_burst", dict(gpu="gpu-12-01-03", at=8)), ("background_ecc", dict(gpu="gpu-12-02-03", extra=2))]),
    "S04-two-faults-one-shift": dict(
        title="Two unrelated faults at once: a PCIe link downtrains on rack 12 while an inference replica crash-loops on rack 13",
        window=dict(start=D + "18:00:00Z", minutes=30, step=30),
        injectors=[("pcie_downtrain", dict(gpu="gpu-12-01-00", onset=6)), ("replica_crash", dict(onset=11))]),
    "S05-exporter-hang-stale-telemetry": dict(
        title="Metrics exporter hangs on server 13-01: values freeze, so its GPUs become unknown, not healthy",
        window=dict(start=D + "19:00:00Z", minutes=30, step=30),
        injectors=[("exporter_hang", dict(server="server-13-01", onset=8))]),
    "S06-silent-hung-rank": dict(
        title="One training rank hangs; peers busy-wait at full utilisation while the step counter stops",
        window=dict(start=D + "21:00:00Z", minutes=30, step=30),
        injectors=[("hung_rank", dict(gpu="gpu-12-02-02", onset=9))]),
    "S07-pdu-feed-loss": dict(
        title="Power feed B trips on rack 13; PDU A carries the whole rack with no redundancy left",
        window=dict(start=D + "17:00:00Z", minutes=30, step=60),
        injectors=[("pdu_feed_loss", dict(rack="13", side="b", onset=6))]),
    "S08-uplink-crc-errors": dict(
        title="A leaf-to-spine optic degrades: CRC errors climb and distributed training slows",
        window=dict(start=D + "13:00:00Z", minutes=40, step=60),
        injectors=[("link_crc", dict(port="leaf-12-p07", onset=10))]),
    "S09-busy-but-healthy-shift": dict(
        title="Benign noise only: a fine-tune job starts, traffic rises 25%, one impossible sensor sample",
        window=dict(start=D + "17:00:00Z", minutes=30, step=30),
        injectors=[("job_start", dict(at=12)), ("traffic_change", dict(onset=5, factor=1.25)),
                   ("spurious_sample", dict(gpu="gpu-12-02-07", at=19))]),
    "S10-unlogged-power-cap": dict(
        title="Rack 12 GPUs are power-capped with no intervention record: unexplained slowdown",
        window=dict(start="2026-11-15T16:30:00Z", minutes=30, step=30),   # 14 Nov 16:40 has a real logged cap (int-0091)
        injectors=[("power_cap", dict(onset=10, logged=False))]),
    "S11-logged-power-cap": dict(
        title="Benign twin of S10: the same power cap, but it is our own logged demand-response intervention",
        window=dict(start="2026-11-15T16:30:00Z", minutes=30, step=30),
        injectors=[("power_cap", dict(onset=10, logged=True))]),
    "S12-gradual-thermal-drift": dict(
        title="Three-day creep: one GPU warms 14 C relative to its peers without crossing any limit",
        window=dict(start="2026-11-12T00:00:00Z", minutes=4320, step=1800),
        injectors=[("thermal_drift", dict(gpu="gpu-12-01-01", total_c=14.0))]),
}
SHOWCASE_SEED = 7

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,80}$")


class BridgeError(Exception):
    pass


def _load_json(path):
    with open(path) as f: return json.load(f)


def _index():
    return _load_json(os.path.join(HERE, "index.json"))


def catalog():
    out = []
    for sid, s in SHOWCASE.items():
        out.append({"id": sid, "title": s["title"], "group": "showcase", "minutes": s["window"]["minutes"],
                    "sampleIntervalSeconds": s["window"]["step"], "fullHall": True})
    for s in _index()["scenarios"]:
        out.append({"id": s["scenarioId"], "title": s["title"], "group": "fault" if s["flag"] else "benign",
                    "fullHall": False})
    return {"schemaVersion": SCHEMA_VERSION, "origin": "synthetic", "scenarios": out}


def _code_key():
    h = hashlib.sha256()
    for name in ("flagger.py", "config.json", "topology.enriched.json", "bridge.py", "generate.py", "gen.py"):
        with open(os.path.join(HERE, name), "rb") as f: h.update(f.read())
    return h.hexdigest()[:16]


def _cached(kind, sid, build):
    """Deterministic outputs are cached on disk, keyed on the code + config that produced them."""
    path = os.path.join(CACHE, kind, f"{sid}.{_code_key()}.json")
    if os.path.exists(path):
        try: return _load_json(path)
        except (OSError, ValueError): pass            # corrupt cache: rebuild
    value = build()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    for old in os.listdir(os.path.dirname(path)):            # drop results made by older code
        if old.startswith(sid + ".") and old != os.path.basename(path):
            try: os.remove(os.path.join(os.path.dirname(path), old))
            except OSError: pass
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(value, f)
    os.replace(tmp, path)                             # atomic: concurrent readers never see half a file
    return value


def load_scenario(sid):
    if not isinstance(sid, str) or not ID_RE.match(sid): raise BridgeError("invalid scenario id")
    if sid in SHOWCASE:
        def build():
            import generate                           # imported lazily: it executes gen.py's helpers
            s = SHOWCASE[sid]
            w = s["window"]
            sc = generate.build("normal", SHOWCASE_SEED, w["start"], w["minutes"], w["step"],
                                injectors=s["injectors"], name=sid, title=s["title"])
            sc["scenarioId"] = sid
            return sc
        return _cached("scenarios", sid, build)
    known = {s["scenarioId"]: s["file"] for s in _index()["scenarios"]}
    if sid not in known: raise BridgeError("unknown scenario")
    return _load_json(os.path.join(HERE, known[sid]))


# ------------------------------------------------------------------ twin <-> source translation
class TwinMap:
    """Console rack A01..B02 = server 12-01, 12-02, 13-01, 13-02; console slot n = source GPU n-1."""
    RACKS = [("rack-a01", "server-12-01"), ("rack-a02", "server-12-02"),
             ("rack-b01", "server-13-01"), ("rack-b02", "server-13-02")]

    def __init__(self, topo):
        self.G = Graph(topo)
        self.topo = topo
        self.gpu_src, self.gpu_twin = {}, {}
        self.rack_src = dict(self.RACKS)
        for twin_rack, server in self.RACKS:
            tag = twin_rack[5:]
            for g in range(8):
                src, twin = f"gpu-{server[7:]}-{g:02d}", f"gpu-{tag}-{g + 1:02d}"
                self.gpu_src[twin], self.gpu_twin[src] = src, twin
        self.server_of_twin_rack = {r: s for r, s in self.RACKS}
        self.twin_rack_of_server = {s: r for r, s in self.RACKS}
        # dependencies per console rack (everything any of its GPUs depends on)
        self.rack_closure = {}
        for twin_rack, server in self.RACKS:
            deps = set()
            for g in range(8): deps |= self.G.closure(f"gpu-{server[7:]}-{g:02d}")
            self.rack_closure[twin_rack] = deps
        self.workload_gpus = {w["id"]: list((w.get("allocation") or {}).get("gpuIds", []))
                              for w in topo.get("workloads", [])}

    def owner(self, eid):
        return self.G.port_owner.get(eid, eid)

    def kind(self, eid):
        """Which console layer a source entity belongs to."""
        o = self.owner(eid)
        t = self.G.etype(o) or ""
        if eid != o or t in ("switch", "nic", "cable") or eid.startswith(("leaf-", "spine-", "nic-", "cable-")): return "fabric"
        if t == "power_system": return "power"
        if t in ("cooling_system", "room") or eid.startswith(("cdu-", "crac-", "chiller-", "coolant-")): return "cooling"
        if t == "workload" or eid in self.workload_gpus: return "workload"
        if t == "storage_system" or eid.startswith("storage-"): return "storage"
        return t

    def twin_for(self, eid):
        """Console entities that a source entity shows up on, as {twinId: role}."""
        o = self.owner(eid)
        if o in self.gpu_twin: return {self.gpu_twin[o]: "direct"}
        if o in self.twin_rack_of_server:
            r = self.twin_rack_of_server[o]
            out = {r: "direct"}
            out.update({self.gpu_twin[f"gpu-{o[7:]}-{g:02d}"]: "direct" for g in range(8)})
            return out
        k = self.kind(eid)
        if k == "workload":
            return {self.gpu_twin[g]: "impacted" for g in self.workload_gpus.get(o, []) if g in self.gpu_twin}
        if k == "storage":
            out = {}
            for w in self.topo.get("workloads", []):
                if o in ((w.get("allocation") or {}).get("storageIds") or []):
                    out.update({self.gpu_twin[g]: "impacted" for g in w["allocation"]["gpuIds"] if g in self.gpu_twin})
            return out
        racks = [r for r, deps in self.rack_closure.items() if o in deps]
        if o.startswith("rack-"):                     # physical rack 12 -> console racks A01 + A02
            racks = [r for r, s in self.RACKS if self.G.ent.get(s, {}).get("parentId") == o] or racks
        if k == "fabric" and o.startswith("nic-"):
            racks = [self.twin_rack_of_server.get(self.G.ent.get(o, {}).get("parentId"))] if self.G.ent.get(o, {}).get("parentId") in self.twin_rack_of_server else racks
        out = {}
        for r in racks:
            if not r: continue
            tag = r[5:]
            if k == "fabric": out[f"link-{tag}"] = "direct"
            elif k == "power": out[f"power-{r}"] = "direct"
            elif k == "cooling": out[f"cooling-{r}"] = "direct"
            else: out[r] = "direct"
        return out

    def sources_for(self, twin_id):
        """Source entities behind one console entity (what the inspector shows when it is clicked)."""
        if twin_id in self.gpu_src: return [self.gpu_src[twin_id]]
        if twin_id in self.server_of_twin_rack:
            s = self.server_of_twin_rack[twin_id]
            return [s] + [f"nic-{s[7:]}-01", f"nic-{s[7:]}-02"]
        m = re.match(r"^(link|power|cooling)-(?:rack-)?([ab]0[12])$", twin_id)
        if m:
            layer, rack = m.group(1), "rack-" + m.group(2)
            server = self.server_of_twin_rack[rack]
            if layer == "link":
                leaf = f"leaf-{server[7:9]}"
                ports = [p["id"] for p in (self.G.ent.get(leaf, {}).get("ports") or []) if p["id"].endswith(("p07", "p08"))]
                return [f"nic-{server[7:]}-01", f"nic-{server[7:]}-02"] + ports
            want = {"power": "power_system", "cooling": "cooling_system"}[layer]
            deps = [e for e in sorted(self.rack_closure[rack]) if self.G.etype(e) == want]
            if layer == "cooling": deps += ["room-01"]
            return deps
        fixed = {"sw-spine": ["spine-01"], "grid-01": ["feed-a", "feed-b", "ups-a", "ups-b"],
                 "pdu-a": ["pdu-12-a", "pdu-12-b"], "pdu-b": ["pdu-13-a", "pdu-13-b"],
                 "cdu-a": ["cdu-02", "coolant-loop-02"], "cdu-b": ["cdu-02", "coolant-loop-02"],
                 "site-lon01": ["room-01"]}
        if twin_id in fixed: return fixed[twin_id]
        raise BridgeError("unknown console entity")

    def layout(self, scenario):
        """Per-GPU role at the start of the window, from the workload allocations."""
        role = {}
        for w in self.topo.get("workloads", []):
            r = "inference" if w.get("type") == "inference" else "training"
            if w.get("status") not in ("running", None) and w.get("type") != "inference": r = "idle"
            for g in (w.get("allocation") or {}).get("gpuIds", []): role[g] = (r, w["id"])
        seen = {r["entityId"] for r in scenario["telemetry"]}
        racks = []
        for twin_rack, server in self.RACKS:
            gpus = []
            for g in range(8):
                src = f"gpu-{server[7:]}-{g:02d}"
                r, wl = role.get(src, ("idle", None))
                gpus.append({"twinId": self.gpu_twin[src], "sourceId": src, "label": f"{g:02d}", "role": r,
                             "workloadId": wl, "hasTelemetry": src in seen})
            racks.append({"twinId": twin_rack, "sourceId": server, "label": f"Server {server[7:]}", "gpus": gpus})
        wls = [{"id": w["id"], "name": w.get("name"), "type": w.get("type"), "priority": w.get("priority")}
               for w in self.topo.get("workloads", [])]
        return {"racks": racks, "workloads": wls}


def _iso(t): return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _finding(f, tm):
    return {"detector": f.detector, "severity": f.severity, "entity": f.entity, "metric": f.metric,
            "start": _iso(f.start), "end": _iso(f.end), "text": f.text, "method": f.method,
            "workloads": sorted(f.workloads), "twin": tm.twin_for(f.entity)}


def _incident(inc, tm):
    states = {}
    def mark(tid, state):
        if SEV_RANK.get(state, 0) > SEV_RANK.get(states.get(tid), 0): states[tid] = state
    for f in inc.findings:
        state = "stale" if f.detector == "telemetry" else f.severity if f.severity in ("warning", "critical") else "warning"
        for tid, how in tm.twin_for(f.entity).items():
            mark(tid, state if how == "direct" else "impacted")
    for w in inc.workloads:                            # blast radius: GPUs whose work is affected
        for tid in tm.twin_for(w): mark(tid, "impacted")
    lines = inc.message.split("\n")
    root = tm.G.ent.get(tm.owner(inc.root_entity), {})
    return {"severity": inc.severity, "rootEntity": inc.root_entity, "rootName": tm.G.name(inc.root_entity),
            "rootDisplay": root.get("name") or inc.root_entity, "rootTwin": next(iter(tm.twin_for(inc.root_entity)), None),
            "rootLayer": tm.kind(inc.root_entity), "headline": lines[0], "message": inc.message,
            "detectors": inc.detectors, "methods": list(dict.fromkeys(f.method for f in inc.findings)),
            "entities": inc.entities, "workloads": inc.workloads,
            "findings": [_finding(f, tm) for f in inc.findings], "twinStates": states}


def _expected_match(expected, final_incidents):
    flagged = bool(final_incidents)
    exp_flag = bool(expected.get("flag"))
    roots = {i["rootEntity"] for i in final_incidents}
    want = {expected.get("rootCauseEntityId")} | set(expected.get("acceptEntityIds") or [])
    want.discard(None)
    involved = roots | {e for i in final_incidents for e in i["entities"]} | {w for i in final_incidents for w in i["workloads"]}
    return {"expectedFlag": exp_flag, "flagged": flagged, "match": flagged == exp_flag,
            "rootMatch": (not exp_flag) or bool(want & involved),
            "expectedRoot": expected.get("rootCauseEntityId"), "expectedSeverity": expected.get("severity"),
            "category": expected.get("category"), "reason": expected.get("reason")}


def analyse(sid):
    def build():
        started = time.perf_counter()
        sc = load_scenario(sid)
        topo = _load_json(os.path.join(HERE, "topology.enriched.json"))
        ctx = merge_context(topo, sc)
        tm = TwinMap(ctx)
        recs = sorted(sc["telemetry"], key=lambda r: r["timestamp"])
        times = [ts(r["timestamp"]) for r in recs]
        t0, t1 = ts(sc["window"]["start"]), ts(sc["window"]["end"])
        steps, first_flag, runs = [], None, 0
        for i in range(STEPS):
            cutoff = t0 + (t1 - t0) * (i + 1) / STEPS
            n = 0
            while n < len(times) and times[n] <= cutoff: n += 1
            incidents, suppressed = flag_window(ctx, recs[:n])
            runs += 1
            incs = [_incident(x, tm) for x in incidents]
            states = {}
            for x in incs:
                for tid, st in x["twinStates"].items():
                    if SEV_RANK[st] > SEV_RANK.get(states.get(tid), 0): states[tid] = st
            gates = sorted({w for _, w in suppressed})
            if incs and first_flag is None: first_flag = i
            steps.append({"index": i, "cutoff": _iso(cutoff), "minute": round((cutoff - t0) / 60, 2), "records": n,
                          "incidents": incs, "states": states, "gates": gates[:12], "gateCount": len(suppressed),
                          "detectorsFired": sorted({d for x in incs for d in x["detectors"]})})
        expected = sc.get("expected", {})
        return {"schemaVersion": SCHEMA_VERSION, "origin": "synthetic", "scenarioId": sid,
                "title": sc.get("title", sid), "group": "showcase" if sid in SHOWCASE else ("fault" if expected.get("flag") else "benign"),
                "window": sc["window"], "context": sc.get("context", {}), "records": len(recs),
                "engine": {"module": "flagger.flag_window", "deterministic": True, "runs": runs,
                           "codeKey": _code_key(), "elapsedMs": round((time.perf_counter() - started) * 1000)},
                "layout": tm.layout(sc), "steps": steps, "firstFlagStep": first_flag,
                "verdict": _expected_match(expected, steps[-1]["incidents"])}
    return _cached("analysis", sid, build)


def entity(sid, twin_id):
    if not isinstance(twin_id, str) or not ID_RE.match(twin_id): raise BridgeError("invalid entity id")
    started = time.perf_counter()
    sc = load_scenario(sid)
    topo = _load_json(os.path.join(HERE, "topology.enriched.json"))
    tm = TwinMap(merge_context(topo, sc))
    sources = tm.sources_for(twin_id)
    wanted = set(sources)
    rows = [r for r in sc["telemetry"] if r["entityId"] in wanted]
    rows.sort(key=lambda r: (r["timestamp"], r["entityId"], r["metric"]))
    truncated = len(rows) > MAX_LOG_RECORDS
    if truncated: rows = rows[-MAX_LOG_RECORDS:]
    series, log = {}, []
    for r in rows:
        q = r.get("quality") or {}
        status = "stale" if q.get("stale") else "invalid" if q.get("valid") is False else \
            "low-confidence" if q.get("confidence", 1.0) < 0.5 else "ok"
        key = f"{r['entityId']}|{r['metric']}"
        if isinstance(r["value"], (int, float)) and not isinstance(r["value"], bool):
            s = series.setdefault(key, {"entity": r["entityId"], "metric": r["metric"], "unit": r.get("unit"), "points": []})
            s["points"].append([r["timestamp"], r["value"], status])
        log.append({"t": r["timestamp"], "entity": r["entityId"], "metric": r["metric"], "value": r["value"],
                    "unit": r.get("unit"), "quality": status, "source": (r.get("source") or {}).get("connectorId")})
    catalog_limits = {}          # declared limits from the metric catalog, resolved for this entity
    for s in series.values():
        spec = ((topo.get("metricCatalog") or {}).get(s["metric"]) or {}).get("limits") or {}
        out = []
        for kind in ("warn", "critical", "warnBelow", "criticalBelow"):
            lim, why = tm.G.limit(spec.get(kind), s["entity"])
            if isinstance(lim, (int, float)) and not isinstance(lim, bool):
                out.append({"kind": kind, "value": lim, "note": why.strip(", ")})
        if out: catalog_limits[f"{s['entity']}|{s['metric']}"] = out
    ent = tm.G.ent
    info = [{"id": e, "name": (ent.get(tm.owner(e)) or {}).get("name") or e, "type": tm.G.etype(tm.owner(e)) if e == tm.owner(e) else "port",
             "model": (ent.get(e) or {}).get("model"), "parentId": (ent.get(e) or {}).get("parentId")} for e in sources]
    return {"schemaVersion": SCHEMA_VERSION, "origin": "synthetic", "scenarioId": sid, "twinId": twin_id,
            "sources": info, "series": list(series.values()), "limits": catalog_limits, "log": log,
            "truncated": truncated, "elapsedMs": round((time.perf_counter() - started) * 1000)}


def main(argv):
    try:
        if len(argv) == 1 and argv[0] == "catalog": out = catalog()
        elif len(argv) == 2 and argv[0] == "analyse": out = analyse(argv[1])
        elif len(argv) == 3 and argv[0] == "entity": out = entity(argv[1], argv[2])
        else: raise BridgeError("usage: bridge.py catalog | analyse <scenarioId> | entity <scenarioId> <twinId>")
    except BridgeError as e:
        json.dump({"error": str(e)}, sys.stdout); sys.stdout.write("\n"); return 2
    json.dump(out, sys.stdout, separators=(",", ":")); sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
