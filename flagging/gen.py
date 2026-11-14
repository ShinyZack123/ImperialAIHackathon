#!/usr/bin/env python3
"""Generate example datacentre topology + labelled telemetry scenarios.

Output: <out>/topology.json, <out>/scenarios/*.json, <out>/index.json
Deterministic (seeded) so reruns produce identical files.
"""
import json, math, os, random, sys
from datetime import datetime, timedelta, timezone

OUT = sys.argv[1]
os.makedirs(os.path.join(OUT, "scenarios"), exist_ok=True)
DC = "dc-lon-01"
SCHEMA_VERSION = "0.1.0-examples"

def iso(t): return t.strftime("%Y-%m-%dT%H:%M:%SZ")
def T(s): return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

SOURCES = {
    "dcgm":  {"connectorId": "dcgm-prod-01", "system": "NVIDIA DCGM", "collectionMethod": "metrics-export"},
    "snmp":  {"connectorId": "snmp-net-01", "system": "Switch SNMP/gNMI", "collectionMethod": "streaming-telemetry"},
    "ufm":   {"connectorId": "ufm-ib-01", "system": "NVIDIA UFM", "collectionMethod": "rest-poll"},
    "bms":   {"connectorId": "bms-01", "system": "Building Management System", "collectionMethod": "bacnet-poll"},
    "pdu":   {"connectorId": "pdu-redfish-01", "system": "PDU Redfish", "collectionMethod": "rest-poll"},
    "slurm": {"connectorId": "slurm-01", "system": "Slurm + job metrics", "collectionMethod": "prometheus-scrape"},
    "infer": {"connectorId": "serving-metrics-01", "system": "Inference gateway", "collectionMethod": "prometheus-scrape"},
    "node":  {"connectorId": "node-exporter-01", "system": "Prometheus node_exporter", "collectionMethod": "prometheus-scrape"},
    "stor":  {"connectorId": "storage-metrics-01", "system": "Parallel FS metrics", "collectionMethod": "prometheus-scrape"},
    "dcim":  {"connectorId": "dcim-01", "system": "DCIM / CMMS", "collectionMethod": "api-sync"},
}

# ------------------------------------------------------------------ topology
RACKS = ["12", "13"]
SERVERS = ["01", "02"]
GPUS = [f"{i:02d}" for i in range(8)]

def gpu_id(r, s, g): return f"gpu-{r}-{s}-{g}"
def server_id(r, s): return f"server-{r}-{s}"
def nic_id(r, s, n): return f"nic-{r}-{s}-{n}"

BASE_TS = "2026-11-14T00:00:00Z"

def entity(id, name, type, parent=None, rack=None, status="healthy", src="dcim", **kw):
    e = {
        "id": id, "name": name, "type": type, "datacenterId": DC,
        "capabilities": [], "metricDefinitions": [], "currentMetricValues": {},
        "controls": [], "connectionIds": [], "operationalStatus": status,
        "sources": [{"connectorId": SOURCES[src]["connectorId"]}],
        "lastObservedAt": BASE_TS, "attributes": {},
    }
    if parent: e["parentId"] = parent
    if rack: e["rackId"] = rack
    e.update(kw)
    return e

entities, connections, conn_n = [], [], [0]

def connect(src, dst, rel, **kw):
    conn_n[0] += 1
    c = {"id": f"conn-{conn_n[0]:04d}", "sourceEntityId": src, "destinationEntityId": dst,
         "relationship": rel, "status": "active", "lastVerifiedAt": BASE_TS}
    c.update(kw)
    connections.append(c)
    for e in entities:
        if e["id"] in (src, dst):
            e["connectionIds"].append(c["id"])
    return c["id"]

entities.append(entity("bldg-01", "Building 1", "building"))
entities.append(entity("room-01", "Data hall A", "room", parent="bldg-01"))
connect("bldg-01", "room-01", "contains")

# power chain: feed -> ups -> pdu (A and B paths)
for side in ["a", "b"]:
    entities.append(entity(f"feed-{side}", f"Grid feed {side.upper()}", "power_system", category="grid_feed",
                           electrical={"voltageV": 11000, "capacityW": 2_000_000},
                           upstreamEntityIds=[], downstreamEntityIds=[f"ups-{side}"], redundancyGroupId="rg-feed"))
    entities.append(entity(f"ups-{side}", f"UPS {side.upper()}", "power_system", category="ups",
                           electrical={"voltageV": 415, "capacityW": 500_000, "efficiencyPercent": 96.5},
                           upstreamEntityIds=[f"feed-{side}"], downstreamEntityIds=[f"pdu-{r}-{side}" for r in RACKS],
                           redundancyGroupId="rg-ups"))
    connect(f"ups-{side}", f"feed-{side}", "powered_by", criticality="critical", redundant=True)

# cooling: chiller -> cdu-02 -> coolant loop -> GPUs ; crac-01 for room air
entities.append(entity("chiller-01", "Chiller 1", "cooling_system", src="bms", category="chiller",
                       thermal={"coolingCapacityWatts": 1_200_000}, servedEntityIds=["cdu-02"]))
entities.append(entity("cdu-02", "CDU 2 (racks 12-13)", "cooling_system", src="bms", category="cdu",
                       manufacturer="CoolCo", model="CDU-800",
                       thermal={"inletTemperatureC": 30.0, "outletTemperatureC": 40.0, "flowRateLitersPerMinute": 115.0,
                                "pressureKPa": 210.0, "coolingCapacityWatts": 120_000, "currentCoolingWatts": 72_000},
                       servedEntityIds=["coolant-loop-02"]))
entities.append(entity("coolant-loop-02", "Coolant loop 2", "cooling_system", src="bms", category="coolant_loop",
                       thermal={}, servedEntityIds=[gpu_id(r, s, g) for r in RACKS for s in SERVERS for g in GPUS]))
entities.append(entity("crac-01", "CRAC 1 (hall A)", "cooling_system", src="bms", category="crac",
                       thermal={"inletTemperatureC": 27.0, "outletTemperatureC": 20.0, "humidityPercent": 45},
                       servedEntityIds=["room-01"]))
connect("cdu-02", "chiller-01", "depends_on", criticality="critical", redundant=False)
connect("coolant-loop-02", "cdu-02", "depends_on", criticality="critical", redundant=False)

# network: spine-01, leaf-12, leaf-13 (InfiniBand NDR 400G)
NDR = 400_000_000_000
def switch(id, name, nports, rack=None):
    ports = []
    for p in range(1, nports + 1):
        ports.append({"id": f"{id}-p{p:02d}", "deviceId": id, "name": f"port{p}", "medium": "fiber",
                      "connectorType": "OSFP", "supportedSpeedBps": [200_000_000_000, NDR],
                      "negotiatedSpeedBps": None, "mtu": 4096, "linkUp": False,
                      "rxBytes": 0, "txBytes": 0, "rxErrors": 0, "txErrors": 0, "packetDrops": 0})
    return entity(id, name, "switch", rack=rack, src="ufm", manufacturer="NVIDIA", model="QM9700",
                  firmwareVersion="31.2012.1010", ports=ports, switchingCapacityBps=51_200_000_000_000,
                  crcErrors=0, packetDrops=0, congestionEvents=0, temperatureC=48, powerWatts=1100)
entities.append(switch("spine-01", "Spine 1", 8))
for r in RACKS:
    entities.append(switch(f"leaf-{r}", f"Leaf {r}", 8, rack=f"rack-{r}"))

def ent(id):
    for e in entities:
        if e["id"] == id: return e
    raise KeyError(id)

def port(sw, p):
    for x in ent(sw)["ports"]:
        if x["id"] == f"{sw}-p{p:02d}": return x
    raise KeyError((sw, p))

def cable(cid, a_port, b_port, length):
    entities.append(entity(cid, cid, "cable", src="dcim", medium="fiber", cableCategory="OSFP-AOC",
                           connectorA="OSFP", connectorB="OSFP", lengthMeters=length, ratedBandwidthBps=NDR,
                           measuredErrorRate=1e-15, sourcePortId=a_port, destinationPortId=b_port))
    return cid

# leaf uplinks to spine: leaf-12 p07,p08 -> spine p01,p02 ; leaf-13 p07,p08 -> spine p03,p04
for i, r in enumerate(RACKS):
    for j, lp in enumerate([7, 8]):
        sp = 1 + i * 2 + j
        cid = cable(f"cable-leaf{r}-p{lp:02d}", f"leaf-{r}-p{lp:02d}", f"spine-01-p{sp:02d}", 15.0)
        cn = connect(f"leaf-{r}", "spine-01", "connected_to", criticality="high", redundant=True,
                     physical={"cableId": cid, "sourcePortId": f"leaf-{r}-p{lp:02d}",
                               "destinationPortId": f"spine-01-p{sp:02d}", "lengthMeters": 15.0},
                     network={"protocol": "InfiniBand NDR", "maxBandwidthBps": NDR, "latencyMicroseconds": 0.6,
                              "packetLossRate": 0.0, "errorRate": 1e-15})
        for pp in (port(f"leaf-{r}", lp), port("spine-01", sp)):
            pp.update(linkUp=True, negotiatedSpeedBps=NDR, connectionId=cn)
        port(f"leaf-{r}", lp)["connectedPortId"] = f"spine-01-p{sp:02d}"
        port("spine-01", sp)["connectedPortId"] = f"leaf-{r}-p{lp:02d}"

entities.append(entity("cluster-a", "Training/Inference cluster A", "cluster"))

for r in RACKS:
    rid = f"rack-{r}"
    entities.append(entity(rid, f"Rack {r}", "rack", parent="room-01",
                           attributes={"rackPowerBudgetW": 22_000, "uHeight": 48}))
    connect("room-01", rid, "contains")
    for side in ["a", "b"]:
        pid = f"pdu-{r}-{side}"
        entities.append(entity(pid, f"PDU {r}{side.upper()}", "power_system", rack=rid, src="pdu", category="pdu",
                               manufacturer="PowerCo", model="3ph-32A-415V",
                               electrical={"voltageV": 415, "capacityW": 22_000, "powerFactor": 0.98},
                               upstreamEntityIds=[f"ups-{side}"],
                               downstreamEntityIds=[server_id(r, s) for s in SERVERS], redundancyGroupId=f"rg-pdu-{r}"))
        connect(pid, f"ups-{side}", "powered_by", criticality="critical", redundant=True)
    for s in SERVERS:
        sid = server_id(r, s)
        entities.append(entity(sid, f"Rack {r} / Server {s}", "server", parent=rid, rack=rid, src="node",
                               manufacturer="NVIDIA", model="DGX H100", firmwareVersion="BMC 24.09",
                               physicalLocation={"rackUnit": 4 if s == "01" else 14},
                               attributes={"maxPowerW": 10_200, "hostname": f"dgx-{r}-{s}"}))
        connect(rid, sid, "contains")
        for side in ["a", "b"]:
            connect(sid, f"pdu-{r}-{side}", "powered_by", criticality="critical", redundant=True)
        # 2 IB NICs per server (simplified from 8), each to a leaf port
        for n in ["01", "02"]:
            nid = nic_id(r, s, n)
            lp = (SERVERS.index(s)) * 2 + int(n)  # 1..4
            entities.append(entity(nid, f"{sid} IB NIC {n}", "nic", parent=sid, rack=rid, src="ufm",
                                   manufacturer="NVIDIA", model="ConnectX-7",
                                   ports=[{"id": f"{nid}-p1", "deviceId": nid, "name": "p1", "medium": "fiber",
                                           "connectorType": "OSFP", "supportedSpeedBps": [NDR], "negotiatedSpeedBps": NDR,
                                           "mtu": 4096, "linkUp": True, "connectedPortId": f"leaf-{r}-p{lp:02d}"}]))
            cid = cable(f"cable-{nid}", f"{nid}-p1", f"leaf-{r}-p{lp:02d}", 3.0)
            cn = connect(nid, f"leaf-{r}", "connected_to", criticality="high", redundant=False,
                         physical={"cableId": cid, "sourcePortId": f"{nid}-p1",
                                   "destinationPortId": f"leaf-{r}-p{lp:02d}", "lengthMeters": 3.0},
                         network={"protocol": "InfiniBand NDR", "maxBandwidthBps": NDR, "latencyMicroseconds": 0.6,
                                  "packetLossRate": 0.0, "errorRate": 1e-15})
            ent(nid)["ports"][0]["connectionId"] = cn
            lpp = port(f"leaf-{r}", lp)
            lpp.update(linkUp=True, negotiatedSpeedBps=NDR, connectedPortId=f"{nid}-p1", connectionId=cn)
            connect(sid, nid, "contains")
        for g in GPUS:
            gid = gpu_id(r, s, g)
            entities.append(entity(
                gid, f"Rack {r} / Server {s} / GPU {int(g)}", "gpu", parent=sid, rack=rid, src="dcgm",
                manufacturer="NVIDIA", model="H100 SXM5 80GB", firmwareVersion="96.00.89.00.01",
                serialNumber=f"16523{r}{s}{g}0042", architecture="Hopper", computeCapability="9.0",
                compute={"smCount": 132, "maxClockMHz": 1980, "bf16Tflops": 989, "fp8Tflops": 1979},
                memory={"type": "HBM3", "capacityBytes": 85_899_345_920, "bandwidthBytesPerSecond": 3_350_000_000_000,
                        "eccCorrectedErrors": 0, "eccUncorrectedErrors": 0},
                power={"limitWatts": 700, "maxWatts": 700, "throttlingReasons": []},
                thermal={"thermalLimitC": 87, "memoryTemperatureC": 50},
                connectivity={"pcieGeneration": 5, "pcieLanes": 16, "nvlinkCount": 18,
                              "nvlinkBandwidthBytesPerSecond": 900_000_000_000, "numaNode": 0 if int(g) < 4 else 1},
                virtualization={"migEnabled": False, "assignedWorkloadIds": []},
                errors={"xidEvents": 0, "retiredPages": 0, "pcieReplayErrors": 0},
                metricDefinitions=[
                    {"metric": "gpu.temperature_c", "unit": "C", "kind": "gauge", "samplingIntervalSeconds": 30},
                    {"metric": "gpu.power_w", "unit": "W", "kind": "gauge", "samplingIntervalSeconds": 30},
                    {"metric": "gpu.sm_clock_mhz", "unit": "MHz", "kind": "gauge", "samplingIntervalSeconds": 30},
                    {"metric": "gpu.utilization_percent", "unit": "%", "kind": "gauge", "samplingIntervalSeconds": 30},
                    {"metric": "gpu.ecc_dbe_total", "unit": "count", "kind": "counter", "samplingIntervalSeconds": 30},
                    {"metric": "gpu.ecc_sbe_total", "unit": "count", "kind": "counter", "samplingIntervalSeconds": 30},
                    {"metric": "gpu.xid", "unit": "code", "kind": "event", "samplingIntervalSeconds": 0},
                ]))
            connect(sid, gid, "contains")
            connect(gid, "coolant-loop-02", "cooled_by", criticality="critical", redundant=False)
            connect(gid, nic_id(r, s, "01" if int(g) < 4 else "02"), "depends_on", criticality="high")

entities.append(entity("storage-01", "Parallel FS (checkpoints)", "storage_system", src="stor",
                       attributes={"capacityBytes": 2_000_000_000_000_000, "maxWriteBytesPerSecond": 400_000_000_000}))

workloads = [
    {"id": "training-job-104", "name": "LLM-70B pretrain run 4", "type": "training", "ownerId": "team-research",
     "priority": 70, "modelId": "model-llm70b-v4", "clusterId": "cluster-a", "status": "running",
     "allocation": {"gpuIds": [gpu_id("12", s, g) for s in SERVERS for g in GPUS],
                    "storageIds": ["storage-01"], "reservedNetworkBandwidthBps": 1_600_000_000_000},
     "requirements": {"minGpus": 16, "maxGpus": 16, "checkpointable": True, "preemptible": False,
                      "deadline": "2026-11-28T00:00:00Z"},
     "training": {"globalBatchSize": 1024, "sequenceLength": 8192, "precision": "bf16", "tensorParallelism": 8,
                  "pipelineParallelism": 1, "dataParallelism": 2, "stepsCompleted": 41200, "stepsTotal": 120000,
                  "tokensPerSecond": 3_480_000, "stepTimeMs": 2410, "communicationOverheadPercent": 14},
     "economics": {"computeCostPerHourUsd": 52.0},
     "attributes": {"checkpointIntervalSteps": 500, "expectedCheckpointDurationSeconds": 90},
     "createdAt": "2026-11-10T08:00:00Z", "startedAt": "2026-11-10T09:12:00Z"},
    {"id": "inference-svc-207", "name": "Chat-assistant serving (8B)", "type": "inference", "ownerId": "team-product",
     "priority": 90, "modelId": "model-chat8b-v2", "clusterId": "cluster-a", "status": "running",
     "allocation": {"gpuIds": [gpu_id("13", "01", g) for g in GPUS]},
     "requirements": {"minGpus": 4, "maxGpus": 8, "latencySlaMs": 800, "preemptible": False},
     "inference": {"requestsPerSecond": 420, "p50LatencyMs": 210, "p95LatencyMs": 470, "p99LatencyMs": 610,
                   "timeToFirstTokenMs": 140, "queueDepth": 6, "errorRate": 0.001},
     "economics": {"revenuePerHourUsd": 310.0, "computeCostPerHourUsd": 26.0, "slaPenaltyCostUsd": 1500.0},
     "createdAt": "2026-10-01T00:00:00Z", "startedAt": "2026-10-01T00:05:00Z"},
    {"id": "finetune-job-311", "name": "Customer fine-tune (8B LoRA)", "type": "fine_tuning", "ownerId": "team-solutions",
     "priority": 40, "modelId": "model-chat8b-v2", "clusterId": "cluster-a", "status": "queued",
     "allocation": {"gpuIds": [gpu_id("13", "02", g) for g in GPUS]},
     "requirements": {"minGpus": 8, "checkpointable": True, "preemptible": True},
     "training": {"precision": "bf16", "stepsCompleted": 0, "stepsTotal": 6000},
     "createdAt": "2026-11-14T01:30:00Z"},
]
for w in workloads:
    for gid in w["allocation"]["gpuIds"]:
        ent(gid)["virtualization"]["assignedWorkloadIds"].append(w["id"])
        connect(w["id"], gid, "allocated_to")

maintenance = [
    {"id": "maint-0042", "entityIds": ["server-13-02"] + [gpu_id("13", "02", g) for g in GPUS] + ["nic-13-02-01", "nic-13-02-02"],
     "type": "firmware_upgrade", "description": "GPU VBIOS + BMC firmware upgrade, node drained",
     "scheduledStart": "2026-11-14T02:00:00Z", "scheduledEnd": "2026-11-14T04:00:00Z",
     "actualStart": "2026-11-14T02:03:00Z", "actualEnd": "2026-11-14T03:41:00Z",
     "status": "completed", "expectedEffects": ["offline", "link_down", "telemetry_gap"], "ticketId": "CHG-88213"},
    {"id": "maint-0043", "entityIds": ["leaf-13-p05", "leaf-13-p06"],
     "type": "port_reservation", "description": "Ports cabled for future server-13-03; intentionally unconnected",
     "scheduledStart": "2026-11-01T00:00:00Z", "scheduledEnd": "2026-12-31T00:00:00Z",
     "status": "in_progress", "expectedEffects": ["link_down"], "ticketId": "CHG-87990"},
]

agents = [
    {"id": f"agent-rack-{r}", "name": f"Rack {r} agent", "level": "rack", "assignedEntityIds": [f"rack-{r}"],
     "parentAgentId": "agent-cluster-a", "modelId": "local-agent-small-v0", "policyVersion": "0.1",
     "permissions": ["read"], "allowedActions": ["summarise", "recommend"], "status": "active",
     "lastHeartbeatAt": BASE_TS} for r in RACKS
] + [{"id": "agent-cluster-a", "name": "Cluster A agent", "level": "cluster", "assignedEntityIds": ["cluster-a"],
      "modelId": "cluster-agent-v0", "policyVersion": "0.1", "permissions": ["read"],
      "allowedActions": ["recommend", "reschedule"], "status": "active", "lastHeartbeatAt": BASE_TS}]

topology = {
    "id": DC, "name": "London DC 1 (example)",
    "organization": {"id": "org-example", "name": "Example AI Cloud"},
    "location": {"city": "London", "country": "GB", "timezone": "Europe/London"},
    "buildings": [e for e in entities if e["type"] == "building"],
    "rooms": [e for e in entities if e["type"] == "room"],
    "racks": [e for e in entities if e["type"] == "rack"],
    "devices": [e for e in entities if e["type"] in ("server", "switch", "nic")],
    "components": [e for e in entities if e["type"] in ("gpu", "cable")],
    "connections": connections,
    "powerSystems": [e for e in entities if e["type"] == "power_system"],
    "coolingSystems": [e for e in entities if e["type"] == "cooling_system"],
    "networkFabrics": [{"id": "fabric-ib-a", "name": "IB NDR fabric A", "switchIds": ["spine-01", "leaf-12", "leaf-13"]}],
    "storageSystems": [e for e in entities if e["type"] == "storage_system"],
    "clusters": [e for e in entities if e["type"] == "cluster"],
    "workloads": workloads, "models": [], "schedulers": [{"id": "slurm-a", "type": "slurm", "clusterId": "cluster-a"}],
    "telemetry": [{"connectorId": v["connectorId"], "system": v["system"]} for v in SOURCES.values()],
    "anomalies": [], "incidents": [], "maintenance": maintenance,
    "agents": agents, "policies": [], "decisions": [], "interventions": [], "simulations": [],
    "energyPricing": [], "costs": [], "sustainability": {},
    "createdAt": "2026-11-01T00:00:00Z", "updatedAt": BASE_TS, "schemaVersion": SCHEMA_VERSION,
}
ALL_IDS = {e["id"] for e in entities} | {w["id"] for w in workloads} | \
          {p["id"] for e in entities for p in e.get("ports", [])}

# ------------------------------------------------------------------ telemetry helpers
class Scn:
    def __init__(self, sid, start, minutes, step=30):
        self.sid, self.start, self.step = sid, T(start), step
        self.n = int(minutes * 60 / step)
        self.recs = []
        self.rng = random.Random(sid)
    def t(self, i): return self.start + timedelta(seconds=i * self.step)
    def minute(self, i): return i * self.step / 60.0
    def add(self, entity_id, metric, unit, ts, value, src, labels=None, valid=True, estimated=False,
            stale=False, confidence=None):
        assert entity_id in ALL_IDS, entity_id
        q = {"valid": valid, "estimated": estimated, "stale": stale}
        if confidence is not None: q["confidence"] = confidence
        r = {"id": f"tel-{self.sid}-{len(self.recs) + 1:05d}", "entityId": entity_id, "metric": metric,
             "timestamp": iso(ts), "value": value, "unit": unit}
        if labels: r["labels"] = labels
        r["source"] = SOURCES[src]; r["quality"] = q
        self.recs.append(r)
    def series(self, entity_id, metric, unit, fn, src, noise=0.0, nd=1, labels=None, every=1, qual=None):
        """fn(minute) -> value or None (gap). qual(minute) -> dict of quality overrides."""
        for i in range(0, self.n, every):
            m = self.minute(i)
            v = fn(m)
            if v is None: continue
            if isinstance(v, (int, float)) and not isinstance(v, bool) and noise:
                v = v + self.rng.gauss(0, noise)
                if unit == "%": v = min(100.0, max(0.0, v))
                elif unit is not None: v = max(0, v)
            if isinstance(v, float): v = round(v, nd)
            q = qual(m) if qual else {}
            self.add(entity_id, metric, unit, self.t(i), v, src, labels=labels, **q)

def ramp(m, m0, m1, v0, v1):
    if m <= m0: return v0
    if m >= m1: return v1
    return v0 + (v1 - v0) * (m - m0) / (m1 - m0)

def gpu_train(sc, gid, wl, temp=66, power=610, clock=1830, util=97, temp_fn=None, power_fn=None,
              clock_fn=None, util_fn=None, throttle_fn=None, every=1):
    lab = {"workloadId": wl} if wl else None
    sc.series(gid, "gpu.temperature_c", "C", temp_fn or (lambda m: temp), "dcgm", noise=0.6, labels=lab, every=every)
    sc.series(gid, "gpu.power_w", "W", power_fn or (lambda m: power), "dcgm", noise=12, nd=0, labels=lab, every=every)
    sc.series(gid, "gpu.sm_clock_mhz", "MHz", clock_fn or (lambda m: clock), "dcgm", noise=8, nd=0, labels=lab, every=every)
    sc.series(gid, "gpu.utilization_percent", "%", util_fn or (lambda m: util), "dcgm", noise=1.0, labels=lab, every=every)
    if throttle_fn:
        sc.series(gid, "gpu.throttle_reasons", None, throttle_fn, "dcgm", labels=lab, every=every)

def clampu(fn):
    return lambda m: min(100.0, max(0.0, fn(m)))

scenarios = []

def emit(sc, meta):
    meta = dict(meta)
    meta["scenarioId"] = sc.sid
    meta["window"] = {"start": iso(sc.start), "end": iso(sc.t(sc.n)), "sampleIntervalSeconds": sc.step}
    meta["telemetry"] = sc.recs
    order = ["scenarioId", "title", "expected", "window", "primaryEntityIds", "affectedWorkloadIds",
             "context", "stateAtWindowEnd", "telemetry"]
    meta = {k: meta[k] for k in order if k in meta}
    for eid in meta.get("primaryEntityIds", []): assert eid in ALL_IDS, eid
    with open(os.path.join(OUT, "scenarios", f"{sc.sid}.json"), "w") as f:
        json.dump(meta, f, indent=2)
    scenarios.append({"scenarioId": sc.sid, "title": meta["title"], "flag": meta["expected"]["flag"],
                      "severity": meta["expected"].get("severity"), "category": meta["expected"]["category"],
                      "file": f"scenarios/{sc.sid}.json", "telemetryRecords": len(sc.recs)})

J104 = "training-job-104"
R12_GPUS = [gpu_id("12", s, g) for s in SERVERS for g in GPUS]

def job_metrics(sc, step_fn, comm_fn=None, tps_fn=None, every=2):
    sc.series(J104, "workload.step_time_ms", "ms", step_fn, "slurm", noise=15, nd=0, every=every)
    sc.series(J104, "workload.tokens_per_second", "tokens/s",
              tps_fn or (lambda m: 1024 * 8192 / (step_fn(m) / 1000.0)), "slurm", noise=8000, nd=0, every=every)
    if comm_fn:
        sc.series(J104, "workload.comm_overhead_percent", "%", comm_fn, "slurm", noise=0.4, every=every)

# ================================================================== FLAG scenarios

# F01 thermal throttling on one GPU -> straggler slows whole job
sc = Scn("F01-gpu-thermal-throttle", "2026-11-14T09:00:00Z", 30)
hot = gpu_id("12", "02", "05")
for g in R12_GPUS:
    if g == hot:
        gpu_train(sc, g, J104,
                  temp_fn=lambda m: ramp(m, 6, 16, 67, 89.5),
                  clock_fn=lambda m: 1830 if m < 14 else ramp(m, 14, 18, 1830, 1365),
                  power_fn=lambda m: 615 if m < 14 else ramp(m, 14, 18, 615, 470),
                  throttle_fn=lambda m: "none" if m < 14 else "HW_THERMAL_SLOWDOWN")
        sc.series(g, "gpu.memory_temperature_c", "C", lambda m: ramp(m, 6, 16, 58, 84), "dcgm", noise=0.5,
                  labels={"workloadId": J104})
    else:
        gpu_train(sc, g, J104, temp=66 + (int(g[-2:]) % 4), util_fn=lambda m: 97 if m < 15 else 86, every=2)
job_metrics(sc, lambda m: 2410 if m < 14 else ramp(m, 14, 18, 2410, 3150))
emit(sc, {
    "title": "Single GPU thermal throttling turns it into a straggler for a 16-GPU training job",
    "expected": {"flag": True, "severity": "critical", "category": "thermal_throttling",
                 "reason": "GPU temp crosses its 87C slowdown limit and stays there; clocks drop 25% and the whole job's step time rises 30% because every other GPU waits for it.",
                 "keySignals": ["gpu.temperature_c > thermalLimitC for >= 5 min", "throttle_reasons = HW_THERMAL_SLOWDOWN",
                                "gpu.sm_clock_mhz -25% vs peers", "workload.step_time_ms +30% vs baseline"],
                 "rootCauseEntityId": hot, "escalateTo": "agent-rack-12"},
    "primaryEntityIds": [hot], "affectedWorkloadIds": [J104],
    "context": {"note": "Peers on the same CDU loop stay at 66-69C, so it is local (e.g. a cold plate or pump issue on that GPU), not facility cooling."},
    "stateAtWindowEnd": {hot: {"operationalStatus": "degraded", "power.throttlingReasons": ["HW_THERMAL_SLOWDOWN"]}},
})

# F02 ECC double-bit error + XID 48 then XID 79 (fell off bus) -> job crash
sc = Scn("F02-gpu-ecc-xid-fell-off-bus", "2026-11-14T11:00:00Z", 20)
bad = gpu_id("12", "01", "03")
def dead_after(m0, v):
    return lambda m: v if m < m0 else None
for g in R12_GPUS:
    if g == bad:
        gpu_train(sc, g, J104, temp_fn=dead_after(9.5, 65), power_fn=dead_after(9.5, 605),
                  clock_fn=dead_after(9.5, 1830), util_fn=dead_after(9.5, 97))
        sc.series(g, "gpu.ecc_sbe_total", "count", lambda m: 2 if m < 8 else (14 if m < 9.5 else None), "dcgm")
        sc.series(g, "gpu.ecc_dbe_total", "count", lambda m: 0 if m < 8.5 else (1 if m < 9.5 else None), "dcgm")
    else:
        gpu_train(sc, g, J104, util_fn=lambda m: 97 if m < 10 else 0, power_fn=lambda m: 610 if m < 10 else 95,
                  temp_fn=lambda m: 66 if m < 10 else ramp(m, 10, 20, 66, 41), every=2)
sc.add(bad, "gpu.xid", "code", sc.start + timedelta(minutes=8, seconds=31), 48, "dcgm",
       labels={"workloadId": J104, "message": "Double Bit ECC Error"})
sc.add(bad, "gpu.xid", "code", sc.start + timedelta(minutes=9, seconds=22), 79, "dcgm",
       labels={"workloadId": J104, "message": "GPU has fallen off the bus"})
# exporter keeps emitting last values as stale after the GPU vanishes
for i in range(int(9.5 * 2), sc.n, 2):
    sc.add(bad, "gpu.temperature_c", "C", sc.t(i), 65.0, "dcgm", labels={"workloadId": J104}, valid=False, stale=True, confidence=0.0)
sc.series(J104, "workload.status", None, lambda m: "running" if m < 10.5 else "failed", "slurm", every=2)
emit(sc, {
    "title": "Uncorrectable (double-bit) ECC error followed by GPU falling off the bus; training job crashes",
    "expected": {"flag": True, "severity": "critical", "category": "gpu_hardware_error",
                 "reason": "An uncorrectable memory error (XID 48) then XID 79 means the GPU is gone from the PCIe bus. Needs a node drain and RMA (return to vendor). The job failed.",
                 "keySignals": ["gpu.ecc_dbe_total increments", "gpu.xid in {48, 79}", "metrics stop / go stale",
                                "workload.status running -> failed"],
                 "rootCauseEntityId": bad, "escalateTo": "agent-rack-12"},
    "primaryEntityIds": [bad, "server-12-01"], "affectedWorkloadIds": [J104],
    "context": {"glossary": {"XID": "NVIDIA driver error code. 48 = double-bit ECC, 79 = GPU fell off the bus.",
                             "ECC SBE/DBE": "Single-bit errors are auto-corrected; double-bit errors corrupt data."}},
    "stateAtWindowEnd": {bad: {"operationalStatus": "failed", "memory.eccUncorrectedErrors": 1, "errors.xidEvents": 2},
                         J104: {"status": "failed"}},
})

# F03 CRC errors on leaf-12 uplink cable -> comm overhead up, step time up
sc = Scn("F03-network-link-crc-errors", "2026-11-14T13:00:00Z", 40, step=60)
up_port = "leaf-12-p07"
crc0 = 1_204
sc.series(up_port, "port.crc_errors_total", "count",
          lambda m: crc0 + (0 if m < 10 else int((m - 10) ** 2 * 310)), "ufm")
sc.series(up_port, "port.symbol_errors_total", "count",
          lambda m: 88_000 + (0 if m < 10 else int((m - 10) * 52_000)), "ufm")
sc.series(up_port, "port.effective_ber", "ratio", lambda m: 1e-15 if m < 10 else 3.2e-9, "ufm")
sc.series("spine-01-p01", "port.crc_errors_total", "count", lambda m: 312, "ufm")
sc.series("leaf-12-p08", "port.crc_errors_total", "count", lambda m: 640, "ufm")
sc.series(up_port, "port.link_downed_total", "count", lambda m: 3 if m < 25 else 4, "ufm")
job_metrics(sc, lambda m: ramp(m, 10, 20, 2410, 2720), comm_fn=lambda m: ramp(m, 10, 20, 14, 23.5), every=1)
for g in R12_GPUS[:4]:
    sc.series(g, "gpu.utilization_percent", "%", clampu(lambda m: ramp(m, 10, 20, 97, 85)), "dcgm", noise=1.0,
              labels={"workloadId": J104})
emit(sc, {
    "title": "Rising CRC/symbol errors on a leaf-to-spine cable slow down distributed training",
    "expected": {"flag": True, "severity": "warning", "category": "network_link_degradation",
                 "reason": "CRC errors (corrupted packets) on one uplink grow quadratically, BER jumps 6 orders of magnitude and the link retrained once. Job comm overhead goes 14% -> 23.5%, throughput -11%. Likely a dirty or failing optic.",
                 "keySignals": ["rate(port.crc_errors_total) >> baseline", "port.effective_ber > 1e-12",
                                "port.link_downed_total increments", "workload.comm_overhead_percent +9 pts"],
                 "rootCauseEntityId": "cable-leaf12-p07", "escalateTo": "agent-cluster-a"},
    "primaryEntityIds": [up_port, "cable-leaf12-p07", "leaf-12"], "affectedWorkloadIds": [J104],
    "context": {"note": "Sister uplink leaf-12-p08 and far-end spine-01-p01 have static counters: the fault is on that single cable/optic.",
                "glossary": {"CRC error": "Checksum mismatch: the packet was corrupted in transit and must be resent.",
                             "BER": "Bit error rate: fraction of bits received wrong. Healthy NDR links are ~1e-15."}},
    "stateAtWindowEnd": {"cable-leaf12-p07": {"operationalStatus": "degraded", "measuredErrorRate": 3.2e-9}},
})

# F04 CDU flow drop -> all GPUs on loop heat up together
sc = Scn("F04-cdu-flow-drop", "2026-11-14T15:00:00Z", 40, step=60)
flow = lambda m: 115 if m < 8 else ramp(m, 8, 12, 115, 58)
sc.series("cdu-02", "cdu.flow_lpm", "L/min", flow, "bms", noise=1.2)
sc.series("cdu-02", "cdu.supply_temp_c", "C", lambda m: 30.0, "bms", noise=0.15)
sc.series("cdu-02", "cdu.return_temp_c", "C", lambda m: 30 + 10 * 115 / flow(m), "bms", noise=0.2)
sc.series("cdu-02", "cdu.differential_pressure_kpa", "kPa", lambda m: 210 if m < 8 else ramp(m, 8, 12, 210, 128), "bms", noise=2)
sc.series("cdu-02", "cdu.pump_a_status", None, lambda m: "running" if m < 8 else "fault", "bms")
sc.series("cdu-02", "cdu.pump_b_status", None, lambda m: "running", "bms")
for r in RACKS:
    for s in SERVERS:
        if (r, s) == ("13", "02"): continue  # idle node
        for g in GPUS[::2]:  # sample half the GPUs to keep file size sane
            gid = gpu_id(r, s, g)
            wl = J104 if r == "12" else "inference-svc-207"
            base = 66 if r == "12" else 58
            gpu_train(sc, gid, wl, temp_fn=lambda m, b=base: ramp(m, 10, 30, b, b + 17),
                      clock_fn=lambda m, b=base: 1830 if b + 17 * min(1, max(0, (m - 10) / 20)) < 82 else 1700,
                      util=97 if r == "12" else 71, power=610 if r == "12" else 430)
emit(sc, {
    "title": "CDU pump fault halves coolant flow; every liquid-cooled GPU on the loop heats up together",
    "expected": {"flag": True, "severity": "critical", "category": "facility_cooling_degradation",
                 "reason": "Coolant flow halves after pump A faults (pump B alone can't hold flow). Return temperature rises 10C and all GPUs on the loop climb ~17C in 20 minutes, heading for throttling across 2 racks.",
                 "keySignals": ["cdu.flow_lpm -50% vs baseline", "cdu.pump_a_status = fault",
                                "cdu.return_temp_c rising", "correlated temp rise across all GPUs cooled_by coolant-loop-02"],
                 "rootCauseEntityId": "cdu-02", "escalateTo": "agent-cluster-a"},
    "primaryEntityIds": ["cdu-02", "coolant-loop-02"], "affectedWorkloadIds": [J104, "inference-svc-207"],
    "context": {"note": "Use the graph: every affected GPU has a cooled_by edge to coolant-loop-02 which depends_on cdu-02. One root cause, not 24 separate alerts."},
    "stateAtWindowEnd": {"cdu-02": {"operationalStatus": "degraded", "thermal.flowRateLitersPerMinute": 58}},
})

# F05 PDU B feed lost -> PDU A near capacity, redundancy gone
sc = Scn("F05-pdu-overload-redundancy-lost", "2026-11-14T17:00:00Z", 30, step=60)
load = lambda m: 17_400 if m < 12 else ramp(m, 12, 22, 17_400, 21_350)  # rack load grows as job starts
sc.series("pdu-13-a", "pdu.active_power_w", "W", lambda m: load(m) / 2 if m < 6 else load(m), "pdu", noise=60, nd=0)
sc.series("pdu-13-b", "pdu.active_power_w", "W", lambda m: load(m) / 2 if m < 6 else 0, "pdu", noise=0 , nd=0)
sc.series("pdu-13-a", "pdu.current_a", "A", lambda m: (load(m) / 2 if m < 6 else load(m)) / (415 * 1.732 * 0.98), "pdu", noise=0.1)
sc.series("pdu-13-b", "pdu.input_status", None, lambda m: "ok" if m < 6 else "breaker_tripped", "pdu")
sc.series("pdu-13-a", "pdu.load_percent_of_capacity", "%", lambda m: (load(m) / 2 if m < 6 else load(m)) / 220, "pdu", noise=0.3)
for g in GPUS[::2]:
    sc.series(gpu_id("13", "01", g), "gpu.power_w", "W", lambda m: 430, "dcgm", noise=10, nd=0,
              labels={"workloadId": "inference-svc-207"})
    sc.series(gpu_id("13", "02", g), "gpu.power_w", "W", lambda m: 90 if m < 12 else ramp(m, 12, 22, 90, 560),
              "dcgm", noise=10, nd=0, labels={"workloadId": "finetune-job-311"})
emit(sc, {
    "title": "Rack 13 loses its B power feed; PDU A carries the whole rack and reaches 97% of capacity",
    "expected": {"flag": True, "severity": "critical", "category": "power_redundancy_loss",
                 "reason": "B-side breaker tripped, so the rack has no redundant power (N instead of 2N). The fine-tune job then ramps load and PDU A hits 97% of rated capacity. One more spike trips A and drops both servers.",
                 "keySignals": ["pdu.input_status = breaker_tripped", "pdu-13-b active power -> 0 while servers still draw",
                                "pdu.load_percent_of_capacity > 80% on surviving PDU", "redundancyGroupId rg-pdu-13 has 1 healthy member"],
                 "rootCauseEntityId": "pdu-13-b", "escalateTo": "agent-cluster-a"},
    "primaryEntityIds": ["pdu-13-a", "pdu-13-b", "rack-13"], "affectedWorkloadIds": ["inference-svc-207", "finetune-job-311"],
    "context": {"note": "finetune-job-311 is treated as having started at 17:12 for this scenario.",
                "glossary": {"2N redundancy": "Two independent power paths, each able to carry the full load alone."}},
    "stateAtWindowEnd": {"pdu-13-b": {"operationalStatus": "failed"}, "pdu-13-a": {"electrical.activePowerW": 21350},
                         "finetune-job-311": {"status": "running", "startedAt": "2026-11-14T17:12:00Z"}},
})

# F06 stale telemetry: DCGM exporter on server-13-01 freezes while serving traffic
sc = Scn("F06-stale-telemetry-exporter-hung", "2026-11-14T19:00:00Z", 30)
freeze = 8
for g in GPUS:
    gid = gpu_id("13", "01", g)
    lab = {"workloadId": "inference-svc-207"}
    for metric, unit, val, nz in [("gpu.temperature_c", "C", 58, 0.6), ("gpu.utilization_percent", "%", 71, 2.5),
                                  ("gpu.power_w", "W", 430, 15)]:
        frozen = {}
        for i in range(sc.n):
            m = sc.minute(i)
            if m < freeze:
                v = round(val + sc.rng.gauss(0, nz), 1); frozen[metric] = v
                sc.add(gid, metric, unit, sc.t(i), v, "dcgm", labels=lab)
            else:
                # collector still forwards the cached value with the original scrape timestamp
                sc.add(gid, metric, unit, sc.t(i), frozen[metric], "dcgm", labels=dict(lab, scrapeTimestamp=iso(sc.t(int(freeze * 2) - 1))),
                       stale=m - freeze >= 2, confidence=0.9 if m - freeze < 2 else 0.1)
sc.series("server-13-01", "exporter.dcgm_up", "bool", lambda m: True if m < freeze else False, "node")
sc.series("inference-svc-207", "inference.requests_per_second", "req/s", lambda m: 420, "infer", noise=12, nd=0, every=2)
sc.series("inference-svc-207", "inference.p99_latency_ms", "ms", lambda m: 610, "infer", noise=20, nd=0, every=2)
emit(sc, {
    "title": "GPU metrics exporter hangs; collector keeps re-sending frozen values while the node serves traffic",
    "expected": {"flag": True, "severity": "warning", "category": "telemetry_quality",
                 "reason": "8 GPUs report byte-identical values for 22 minutes while the workload is busy. The exporter is down, so the node is unmonitored: a real fault there would be invisible.",
                 "keySignals": ["exporter.dcgm_up = false", "identical values for > N samples on a busy GPU",
                                "labels.scrapeTimestamp not advancing", "quality.stale = true"],
                 "rootCauseEntityId": "server-13-01", "escalateTo": "agent-rack-13"},
    "primaryEntityIds": ["server-13-01"], "affectedWorkloadIds": ["inference-svc-207"],
    "context": {"note": "Flag the monitoring gap itself, not the GPUs. Do NOT treat the frozen values as real readings; the workload metrics prove the GPUs are busy."},
    "stateAtWindowEnd": {"server-13-01": {"operationalStatus": "unknown"}},
})

# F07 hung rank: one GPU drops to 0% while job is 'running', all others idle-wait at high power
sc = Scn("F07-hung-rank-silent-stall", "2026-11-14T21:00:00Z", 30)
hung = gpu_id("12", "02", "02")
for g in R12_GPUS:
    if g == hung:
        gpu_train(sc, g, J104, util_fn=lambda m: 97 if m < 9 else 0, power_fn=lambda m: 610 if m < 9 else 118,
                  clock_fn=lambda m: 1830 if m < 9 else 1980, temp_fn=lambda m: 66 if m < 9 else ramp(m, 9, 19, 66, 44))
    else:
        # NCCL spin-wait: utilisation stays 100% but power drops, no progress
        gpu_train(sc, g, J104, util_fn=lambda m: 97 if m < 9 else 100, power_fn=lambda m: 610 if m < 9 else 255,
                  temp_fn=lambda m: 66 if m < 9 else ramp(m, 9, 19, 66, 52), every=2)
sc.series(J104, "workload.status", None, lambda m: "running", "slurm", every=4)
sc.series(J104, "workload.steps_completed", "count", lambda m: 41_520 + int(min(m, 9) * 60 / 2.41), "slurm", every=2)
sc.series(J104, "workload.last_log_line_age_s", "s", lambda m: 3 if m < 9 else (m - 9) * 60, "slurm", every=2, nd=0)
emit(sc, {
    "title": "Training job silently stalls: one rank hangs, others spin at 100% 'utilisation' with no progress",
    "expected": {"flag": True, "severity": "critical", "category": "workload_stall",
                 "reason": "Step counter stops for 21 minutes while the scheduler still says running. 15 GPUs show 100% util (a busy-wait inside the collective, not real work) at ~40% power. Wastes ~$18/hour of compute and the deadline slips.",
                 "keySignals": ["workload.steps_completed flat for > 10x stepTimeMs", "one GPU util 0 while peers 100",
                                "util high but power low (spin-wait)", "workload.last_log_line_age_s growing"],
                 "rootCauseEntityId": hung, "escalateTo": "agent-cluster-a"},
    "primaryEntityIds": [hung], "affectedWorkloadIds": [J104],
    "context": {"note": "No checkpoint is due (last at step 41500, next 42000) and no storage writes. Compare with B01, which looks similar for ~90s but is a checkpoint.",
                "glossary": {"rank": "One process in a distributed job, usually one per GPU.",
                             "spin-wait": "GPU kernel busy-polling for data from another GPU; counts as 'utilised' but does no useful work."}},
    "stateAtWindowEnd": {J104: {"status": "running", "training.stepsCompleted": 41744}},
})

# F08 inference p99 SLA breach from queue build-up after a replica crash
sc = Scn("F08-inference-sla-breach", "2026-11-14T12:00:00Z", 30)
lost = [gpu_id("13", "01", g) for g in ("04", "05", "06", "07")]
sc.series("inference-svc-207", "inference.requests_per_second", "req/s", lambda m: 455, "infer", noise=10, nd=0)
sc.series("inference-svc-207", "inference.queue_depth", "requests", lambda m: 6 if m < 7 else ramp(m, 7, 15, 6, 190), "infer", noise=2, nd=0)
sc.series("inference-svc-207", "inference.p99_latency_ms", "ms", lambda m: 620 if m < 7 else ramp(m, 7, 15, 620, 2450), "infer", noise=30, nd=0)
sc.series("inference-svc-207", "inference.ttft_ms", "ms", lambda m: 140 if m < 7 else ramp(m, 7, 15, 140, 1100), "infer", noise=10, nd=0)
sc.series("inference-svc-207", "inference.error_rate", "ratio", lambda m: 0.001 if m < 9 else 0.034, "infer", nd=4)
sc.series("inference-svc-207", "inference.healthy_replicas", "count", lambda m: 2 if m < 7 else 1, "infer")
for g in GPUS:
    gid = gpu_id("13", "01", g)
    if gid in lost:
        sc.series(gid, "gpu.utilization_percent", "%", lambda m: 71 if m < 7 else 0, "dcgm", noise=1, labels={"workloadId": "inference-svc-207"}, every=2)
    else:
        sc.series(gid, "gpu.utilization_percent", "%", clampu(lambda m: 71 if m < 7 else 99), "dcgm", noise=1, labels={"workloadId": "inference-svc-207"}, every=2)
sc.add("inference-svc-207", "inference.replica_event", None, sc.start + timedelta(minutes=7, seconds=4), "replica-1 OOMKilled; CrashLoopBackOff", "infer")
emit(sc, {
    "title": "One of two inference replicas crash-loops; queue builds and p99 latency breaches the 800 ms SLA",
    "expected": {"flag": True, "severity": "critical", "category": "sla_breach",
                 "reason": "p99 latency reaches 3x the SLA (service-level agreement) for > 5 min with a $1.5k penalty clause. Half the GPUs sit idle because their replica keeps crashing, so it's fixable by restarting/rescheduling.",
                 "keySignals": ["inference.p99_latency_ms > requirements.latencySlaMs for >= 5 min", "queue_depth rising monotonically",
                                "healthy_replicas dropped", "allocated GPUs at 0% util"],
                 "rootCauseEntityId": "inference-svc-207", "escalateTo": "agent-cluster-a"},
    "primaryEntityIds": ["inference-svc-207"] + lost, "affectedWorkloadIds": ["inference-svc-207"],
    "context": {},
    "stateAtWindowEnd": {"inference-svc-207": {"inference.p99LatencyMs": 2450, "inference.queueDepth": 190}},
})

# F09 PCIe link downtrained + replay errors climbing
sc = Scn("F09-pcie-downtrain-replays", "2026-11-14T06:00:00Z", 20)
pg = gpu_id("12", "01", "06")
sc.series(pg, "gpu.pcie_link_gen", "gen", lambda m: 5 if m < 5 else 4, "dcgm")
sc.series(pg, "gpu.pcie_link_width", "lanes", lambda m: 16 if m < 5 else 8, "dcgm")
sc.series(pg, "gpu.pcie_replay_total", "count", lambda m: 0 if m < 5 else int((m - 5) * 1450), "dcgm")
sc.series(pg, "gpu.pcie_rx_throughput_gbps", "GB/s", lambda m: 21.0 if m < 5 else 6.4, "dcgm", noise=0.6)
for g in [gpu_id("12", "01", x) for x in ("05", "07")]:
    sc.series(g, "gpu.pcie_link_gen", "gen", lambda m: 5, "dcgm", every=4)
    sc.series(g, "gpu.pcie_replay_total", "count", lambda m: 0, "dcgm", every=4)
job_metrics(sc, lambda m: 2410 if m < 5 else 2540, every=2)
emit(sc, {
    "title": "GPU PCIe link drops from Gen5 x16 to Gen4 x8 with thousands of replay errors",
    "expected": {"flag": True, "severity": "warning", "category": "pcie_degradation",
                 "reason": "Host link bandwidth falls to a quarter and replay (retransmit) errors climb ~1.4k/min. Data loading for that rank slows, adding ~5% to every step. Often a riser/slot fault that worsens.",
                 "keySignals": ["gpu.pcie_link_gen < connectivity.pcieGeneration", "gpu.pcie_link_width < connectivity.pcieLanes",
                                "rate(gpu.pcie_replay_total) > 0 sustained"],
                 "rootCauseEntityId": pg, "escalateTo": "agent-rack-12"},
    "primaryEntityIds": [pg], "affectedWorkloadIds": [J104],
    "context": {"glossary": {"PCIe replay": "Packet resent because it failed checksum on the CPU<->GPU link."}},
    "stateAtWindowEnd": {pg: {"operationalStatus": "degraded", "errors.pcieReplayErrors": 21750}},
})

# F10 near-miss of B01: 'checkpoint' that never finishes (storage stall)
sc = Scn("F10-checkpoint-overrun-storage-stall", "2026-11-14T07:30:00Z", 30)
cp = 6
for g in R12_GPUS[::2]:
    gpu_train(sc, g, J104, util_fn=lambda m: 97 if m < cp else 2, power_fn=lambda m: 610 if m < cp else 140,
              temp_fn=lambda m: 66 if m < cp else ramp(m, cp, cp + 10, 66, 45))
sc.series("storage-01", "storage.write_bytes_per_second", "B/s",
          lambda m: 2e9 if m < cp else (95e9 if m < cp + 0.75 else 0.4e9), "stor", noise=0.1e9, nd=0)
sc.series("storage-01", "storage.metadata_latency_ms", "ms", lambda m: 2 if m < cp + 0.5 else 4800, "stor", noise=0.3)
sc.series(J104, "workload.phase", None, lambda m: "train" if m < cp else "checkpoint", "slurm", every=2)
sc.series(J104, "workload.steps_completed", "count", lambda m: 41_851 + int(min(m, cp) * 60 / 2.41), "slurm", every=2)
emit(sc, {
    "title": "Checkpoint starts normally but stalls for 24 minutes because the storage metadata server hangs",
    "expected": {"flag": True, "severity": "critical", "category": "storage_stall",
                 "reason": "Looks like B01 at first, but the checkpoint phase lasts 24 min vs an expected 90 s (16x). Write bandwidth collapses and metadata latency is 4.8 s, so 16 GPUs idle on storage.",
                 "keySignals": ["workload.phase = checkpoint for > 3x expectedCheckpointDurationSeconds",
                                "storage.metadata_latency_ms >> baseline", "write throughput collapsed after initial burst"],
                 "rootCauseEntityId": "storage-01", "escalateTo": "agent-cluster-a"},
    "primaryEntityIds": ["storage-01"], "affectedWorkloadIds": [J104],
    "context": {"note": "Pair with B01. The deterministic rule must use the duration bound, not just 'phase = checkpoint => ignore'."},
    "stateAtWindowEnd": {"storage-01": {"operationalStatus": "degraded"}},
})

# F11 near-miss of B04: node still offline after maintenance window ended
sc = Scn("F11-offline-after-maintenance-window", "2026-11-14T04:30:00Z", 30, step=60)
mnode = "server-13-02"
sc.series(mnode, "node.up", "bool", lambda m: False, "node")
sc.series(mnode, "bmc.power_state", None, lambda m: "on", "node")
sc.series(mnode, "bmc.post_code", None, lambda m: "0x92 (PCI bus init) - stuck", "node", every=5)
for n in ["nic-13-02-01", "nic-13-02-02"]:
    sc.series(n, "port.link_up", "bool", lambda m: False, "ufm", every=5)
sc.series("finetune-job-311", "workload.status", None, lambda m: "queued", "slurm", every=5)
sc.series("finetune-job-311", "workload.queue_wait_s", "s", lambda m: 3 * 3600 + m * 60, "slurm", every=5, nd=0)
emit(sc, {
    "title": "Server still down 50+ minutes after its maintenance window closed and the change was marked complete",
    "expected": {"flag": True, "severity": "warning", "category": "failed_return_from_maintenance",
                 "reason": "maint-0042 ended at 03:41 (marked completed) but the node is stuck in POST (boot self-test) at PCI init, likely a bad firmware flash. A queued job is waiting for it.",
                 "keySignals": ["entity offline AND now > maintenance.scheduledEnd (+ grace)", "maintenance.status = completed but entity not healthy",
                                "bmc.post_code not advancing", "a workload is queued on the entity"],
                 "rootCauseEntityId": mnode, "escalateTo": "agent-rack-13"},
    "primaryEntityIds": [mnode], "affectedWorkloadIds": ["finetune-job-311"],
    "context": {"maintenanceRecordId": "maint-0042", "note": "Pair with B04: same signals, but outside the window."},
    "stateAtWindowEnd": {mnode: {"operationalStatus": "failed"}},
})

# F12 slow drift: rising GPU temps over 3 days at constant load (clogged cold plate / fouling)
sc = Scn("F12-gradual-thermal-drift", "2026-11-11T00:00:00Z", 72 * 60, step=1800)
dg = gpu_id("12", "01", "01")
peer = gpu_id("12", "01", "00")
sc.series(dg, "gpu.temperature_c", "C", lambda m: 66 + 14 * (m / (72 * 60)), "dcgm", noise=0.7, labels={"workloadId": J104, "aggregation": "30m_mean"})
sc.series(peer, "gpu.temperature_c", "C", lambda m: 66.0, "dcgm", noise=0.7, labels={"workloadId": J104, "aggregation": "30m_mean"})
sc.series(dg, "gpu.power_w", "W", lambda m: 612, "dcgm", noise=6, nd=0, labels={"workloadId": J104, "aggregation": "30m_mean"})
sc.series(peer, "gpu.power_w", "W", lambda m: 610, "dcgm", noise=6, nd=0, labels={"workloadId": J104, "aggregation": "30m_mean"})
sc.series("cdu-02", "cdu.supply_temp_c", "C", lambda m: 30.0, "bms", noise=0.2, labels={"aggregation": "30m_mean"})
emit(sc, {
    "title": "One GPU's temperature creeps up 14C over 3 days at unchanged power and coolant temperature",
    "expected": {"flag": True, "severity": "warning", "category": "gradual_degradation",
                 "reason": "No single reading crosses a limit yet (max ~80C < 87C), but the GPU runs ~14C hotter than its identical neighbour at the same power. The trend predicts throttling in ~1.5 days. Catches what a threshold-only rule misses.",
                 "keySignals": ["slope(gpu.temperature_c) > X C/day at stable power", "delta vs same-server peers > 8C",
                                "coolant supply temp flat (rules out facility)"],
                 "rootCauseEntityId": dg, "escalateTo": "agent-rack-12"},
    "primaryEntityIds": [dg], "affectedWorkloadIds": [J104],
    "context": {"note": "Telemetry is 30-minute means (labels.aggregation) because this is from the compressed-baseline store, not raw."},
    "stateAtWindowEnd": {dg: {"operationalStatus": "degraded"}},
})

# ================================================================== NO-FLAG scenarios

# B01 checkpoint dip
sc = Scn("B01-checkpoint-utilisation-dip", "2026-11-14T08:00:00Z", 20, step=10)
cps, cpe = 9.0, 10.5
in_cp = lambda m: cps <= m < cpe
for g in R12_GPUS[::2]:
    gpu_train(sc, g, J104, util_fn=lambda m: 4 if in_cp(m) else 97, power_fn=lambda m: 160 if in_cp(m) else 610,
              temp_fn=lambda m: 63 if in_cp(m) else 66, every=3)
sc.series("storage-01", "storage.write_bytes_per_second", "B/s", lambda m: 95e9 if in_cp(m) else 2e9, "stor", noise=1e9, nd=0, every=3)
sc.series(J104, "workload.phase", None, lambda m: "checkpoint" if in_cp(m) else "train", "slurm", every=3)
sc.series(J104, "workload.steps_completed", "count",
          lambda m: 41_276 + int((min(m, cps) + max(0, m - cpe)) * 60 / 2.41), "slurm", every=6)
emit(sc, {
    "title": "All 16 GPUs drop to ~4% utilisation for 90 seconds while the job writes a checkpoint",
    "expected": {"flag": False, "category": "expected_workload_phase",
                 "reason": "Synchronous checkpoint at step 41500 (interval 500). Duration 90 s matches expectedCheckpointDurationSeconds, storage shows the write burst, and training resumes.",
                 "naiveRuleThatWouldFalselyTrigger": "gpu.utilization_percent < 20% on all GPUs of a running job",
                 "suppressionEvidence": ["workload.phase = checkpoint", "duration <= expectedCheckpointDurationSeconds * 1.5",
                                         "storage write burst coincident", "steps resume afterwards"]},
    "primaryEntityIds": R12_GPUS[::2], "affectedWorkloadIds": [J104],
    "context": {"note": "Pair with F10 (checkpoint that overruns)."},
})

# B02 job start temperature rise (idle -> loaded)
sc = Scn("B02-job-start-temperature-rise", "2026-11-14T17:00:00Z", 30)
st = 12
for g in GPUS:
    gid = gpu_id("13", "02", g)
    gpu_train(sc, gid, "finetune-job-311",
              util_fn=lambda m: 0 if m < st else 94, power_fn=lambda m: 88 if m < st else ramp(m, st, st + 1, 88, 575),
              clock_fn=lambda m: 345 if m < st else 1830,
              temp_fn=lambda m: 34 if m < st else 34 + 33 * (1 - math.exp(-(m - st) / 3.5)), every=2)
sc.series("finetune-job-311", "workload.status", None, lambda m: "queued" if m < st else "running", "slurm", every=4)
emit(sc, {
    "title": "Fine-tune job starts: 8 idle GPUs warm from 34C to 67C and power jumps 6x in a minute",
    "expected": {"flag": False, "category": "expected_load_change",
                 "reason": "Workload transitioned queued -> running at that minute. Temperature settles exponentially at 67C, well under 87C, with clocks at normal boost. A big change, but the expected one.",
                 "naiveRuleThatWouldFalselyTrigger": "temperature rise > 20C in 10 min, or power change > 300% vs 1h baseline",
                 "suppressionEvidence": ["workload.status changed to running at deviation onset",
                                         "steady-state temp < thermalLimitC - 15", "no throttle reasons"]},
    "primaryEntityIds": [gpu_id("13", "02", g) for g in GPUS], "affectedWorkloadIds": ["finetune-job-311"],
    "context": {"note": "Baselines must be conditioned on workload state (idle vs loaded), not one global baseline per GPU."},
    "stateAtWindowEnd": {"finetune-job-311": {"status": "running", "startedAt": "2026-11-14T17:12:00Z"}},
})

# B03 power transient within limits
sc = Scn("B03-power-transient-within-limit", "2026-11-14T10:00:00Z", 10, step=5)
pgpu = gpu_id("12", "01", "04")
spikes = {41, 42, 77}
gpu_train(sc, pgpu, J104, power_fn=lambda m: 688 if int(round(m * 12)) in spikes else 612)
sc.series("pdu-12-a", "pdu.active_power_w", "W", lambda m: 8_900, "pdu", noise=80, nd=0, every=6)
emit(sc, {
    "title": "GPU power briefly spikes to 688 W (limit 700 W) for 5-10 seconds during large matrix ops",
    "expected": {"flag": False, "category": "within_limits_transient",
                 "reason": "Spikes stay under the 700 W enforced limit, last at most 2 samples, cause no throttling, and the PDU stays at 40% of capacity. Normal for bursty transformer kernels.",
                 "naiveRuleThatWouldFalselyTrigger": "gpu.power_w > 1.1x rolling mean",
                 "suppressionEvidence": ["value < power.limitWatts", "duration < 30 s", "no throttle_reasons", "PDU headroom > 50%"]},
    "primaryEntityIds": [pgpu], "affectedWorkloadIds": [J104],
    "context": {},
})

# B04 maintenance window outage
sc = Scn("B04-maintenance-window-outage", "2026-11-14T02:00:00Z", 105, step=60)
mup = lambda m: m < 3 or m > 101
sc.series(mnode, "node.up", "bool", lambda m: mup(m), "node", every=3)
for n in ["nic-13-02-01", "nic-13-02-02"]:
    sc.series(n, "port.link_up", "bool", lambda m: mup(m), "ufm", every=3)
for g in GPUS[:2]:
    sc.series(gpu_id("13", "02", g), "gpu.temperature_c", "C", lambda m: 33 if mup(m) else None, "dcgm", noise=0.4, every=3)
sc.series("leaf-13", "port.link_up", "bool", lambda m: mup(m), "ufm", labels={"port": "leaf-13-p03"}, every=3)
emit(sc, {
    "title": "Server, its NICs and GPUs go offline for 98 minutes during a scheduled firmware upgrade",
    "expected": {"flag": False, "category": "planned_maintenance",
                 "reason": "All outages fall inside maint-0042 (02:00-04:00), on entities listed in the record, with matching expectedEffects. The node was drained first, so no workload was affected.",
                 "naiveRuleThatWouldFalselyTrigger": "node.up = false, link down, telemetry gap",
                 "suppressionEvidence": ["entity in maintenance.entityIds", "timestamp within scheduledStart..scheduledEnd",
                                         "effect type in expectedEffects", "no running workload allocated"]},
    "primaryEntityIds": [mnode], "affectedWorkloadIds": [],
    "context": {"maintenanceRecordId": "maint-0042", "note": "Pair with F11."},
})

# B05 single noisy sample with low confidence
sc = Scn("B05-single-spurious-sample", "2026-11-14T14:00:00Z", 15)
ng = gpu_id("12", "02", "07")
spike_i = 14
for i in range(sc.n):
    v = 66 + sc.rng.gauss(0, 0.6)
    q = {}
    if i == spike_i:
        v, q = 127.0, {"valid": False, "confidence": 0.15}
    sc.add(ng, "gpu.temperature_c", "C", sc.t(i), round(v, 1), "dcgm", labels={"workloadId": J104}, **q)
sc.series(ng, "gpu.memory_temperature_c", "C", lambda m: 58, "dcgm", noise=0.5, labels={"workloadId": J104})
sc.series(ng, "gpu.sm_clock_mhz", "MHz", lambda m: 1830, "dcgm", noise=8, nd=0, labels={"workloadId": J104})
sc.series(ng, "gpu.throttle_reasons", None, lambda m: "none", "dcgm", labels={"workloadId": J104})
emit(sc, {
    "title": "One temperature reading of 127C, marked invalid/low-confidence, between normal 66C readings",
    "expected": {"flag": False, "category": "sensor_noise",
                 "reason": "Physically impossible jump (61C in 30 s and back), quality.valid=false with confidence 0.15, and no corroboration: memory temp, clocks and throttle state all unchanged.",
                 "naiveRuleThatWouldFalselyTrigger": "gpu.temperature_c > thermalLimitC",
                 "suppressionEvidence": ["quality.valid = false or confidence < 0.5", "single sample (needs >= N consecutive)",
                                         "no corroborating metric moved"]},
    "primaryEntityIds": [ng], "affectedWorkloadIds": [J104],
    "context": {"note": "If the same entity produced invalid samples repeatedly, that would become a sensor-health flag (low severity) instead."},
})

# B06 background correctable ECC errors
sc = Scn("B06-background-correctable-ecc", "2026-11-14T00:00:00Z", 24 * 60, step=3600)
eg = gpu_id("12", "02", "03")
inc = [0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0, 1, 0, 0]
cum = [37 + sum(inc[:k + 1]) for k in range(24)]
sc.series(eg, "gpu.ecc_sbe_total", "count", lambda m: cum[int(m // 60)], "dcgm")
sc.series(eg, "gpu.ecc_dbe_total", "count", lambda m: 0, "dcgm")
sc.series(eg, "gpu.retired_pages_total", "count", lambda m: 0, "dcgm")
sc.series(eg, "gpu.remapped_rows_pending", "bool", lambda m: False, "dcgm")
emit(sc, {
    "title": "A GPU logs 4 corrected single-bit memory errors in 24 hours with no uncorrectable errors",
    "expected": {"flag": False, "category": "background_error_rate",
                 "reason": "Single-bit errors are corrected by ECC hardware and a few per day is normal for 80 GB of HBM. No double-bit errors, no retired pages, no pending row remaps.",
                 "naiveRuleThatWouldFalselyTrigger": "any increase in an error counter",
                 "suppressionEvidence": ["only ecc_sbe increments", "rate < SBE threshold (e.g. 100/day)", "ecc_dbe = 0", "retired_pages flat"]},
    "primaryEntityIds": [eg], "affectedWorkloadIds": [J104],
    "context": {"note": "Contrast with F02: there SBE jumped 12 in 1.5 min and was followed by a DBE."},
})

# B07 room inlet diurnal rise within ASHRAE envelope
sc = Scn("B07-diurnal-ambient-rise", "2026-11-14T00:00:00Z", 24 * 60, step=1800)
diurnal = lambda m: 22.5 + 2.6 * math.sin((m / 60 - 9) / 24 * 2 * math.pi)
sc.series("crac-01", "crac.supply_air_temp_c", "C", lambda m: 19.5 + 0.6 * math.sin((m / 60 - 9) / 24 * 2 * math.pi), "bms", noise=0.15)
sc.series("room-01", "room.inlet_temp_c", "C", diurnal, "bms", noise=0.2)
sc.series("room-01", "room.humidity_percent", "%", lambda m: 45 - 6 * math.sin((m / 60 - 9) / 24 * 2 * math.pi), "bms", noise=0.5)
sc.series("chiller-01", "chiller.outdoor_temp_c", "C", lambda m: 8 + 5 * math.sin((m / 60 - 9) / 24 * 2 * math.pi), "bms", noise=0.3)
sc.series("leaf-12", "switch.temperature_c", "C", lambda m: 48 + 2 * math.sin((m / 60 - 9) / 24 * 2 * math.pi), "ufm", noise=0.3)
emit(sc, {
    "title": "Hall inlet air temperature swings 20-25C over the day, tracking outdoor temperature",
    "expected": {"flag": False, "category": "seasonal_or_diurnal_pattern",
                 "reason": "Daily cycle that matches outdoor temperature and stays inside the ASHRAE recommended 18-27C band. Switch temps follow by 2C. Repeats every day.",
                 "naiveRuleThatWouldFalselyTrigger": "inlet temp > 1h rolling mean + 2C",
                 "suppressionEvidence": ["value within envelope 18-27C", "matches same-hour baseline from prior days",
                                         "correlates with chiller.outdoor_temp_c"]},
    "primaryEntityIds": ["room-01"], "affectedWorkloadIds": [],
    "context": {"glossary": {"ASHRAE": "Industry body whose guidelines set recommended data hall air temperatures (18-27C)."},
                "note": "Baselines need time-of-day seasonality, not a flat mean."},
})

# B08 inference traffic falls overnight, util drops, latency fine
sc = Scn("B08-overnight-inference-traffic-drop", "2026-11-14T21:00:00Z", 6 * 60, step=300)
rps = lambda m: 420 * (0.25 + 0.75 * (1 - min(1, m / 240)))
sc.series("inference-svc-207", "inference.requests_per_second", "req/s", rps, "infer", noise=6, nd=0)
sc.series("inference-svc-207", "inference.p99_latency_ms", "ms", lambda m: 610 - 160 * min(1, m / 240), "infer", noise=15, nd=0)
sc.series("inference-svc-207", "inference.queue_depth", "requests", lambda m: max(0, 6 - 5 * min(1, m / 240)), "infer", nd=0)
for g in GPUS[::2]:
    gid = gpu_id("13", "01", g)
    sc.series(gid, "gpu.utilization_percent", "%", lambda m: 71 * rps(m) / 420, "dcgm", noise=1.5, labels={"workloadId": "inference-svc-207"})
    sc.series(gid, "gpu.power_w", "W", lambda m: 120 + 310 * rps(m) / 420, "dcgm", noise=8, nd=0, labels={"workloadId": "inference-svc-207"})
emit(sc, {
    "title": "Inference GPUs fall from 71% to 18% utilisation overnight as user traffic drops",
    "expected": {"flag": False, "category": "demand_driven_change",
                 "reason": "Utilisation tracks request rate exactly, latency improves, nothing errors. Low demand, not a fault. (A separate optimiser may want to consolidate replicas, but that's not an anomaly flag.)",
                 "naiveRuleThatWouldFalselyTrigger": "gpu.utilization_percent < 30% on an allocated GPU",
                 "suppressionEvidence": ["util proportional to requests_per_second", "latency within SLA and improving", "error_rate flat"]},
    "primaryEntityIds": [gpu_id("13", "01", g) for g in GPUS[::2]], "affectedWorkloadIds": ["inference-svc-207"],
    "context": {"note": "Could be routed to an 'optimisation opportunity' channel rather than the anomaly channel."},
})

# B09 unconnected reserved ports are down
sc = Scn("B09-reserved-ports-link-down", "2026-11-14T00:00:00Z", 60, step=300)
for p in ["leaf-13-p05", "leaf-13-p06"]:
    sc.series(p, "port.link_up", "bool", lambda m: False, "ufm")
    sc.series(p, "port.rx_bytes_total", "B", lambda m: 0, "ufm")
sc.series("leaf-13-p03", "port.link_up", "bool", lambda m: True, "ufm")
emit(sc, {
    "title": "Two switch ports report link down permanently",
    "expected": {"flag": False, "category": "expected_state",
                 "reason": "Ports have no connection/cable in the graph and are reserved for a future server under maint-0043. Down is their correct state.",
                 "naiveRuleThatWouldFalselyTrigger": "port.link_up = false",
                 "suppressionEvidence": ["port has no connectedPortId / connectionId", "port listed in an active reservation record"]},
    "primaryEntityIds": ["leaf-13-p05", "leaf-13-p06"], "affectedWorkloadIds": [],
    "context": {"maintenanceRecordId": "maint-0043"},
})

# B10 job completes -> GPUs go idle
sc = Scn("B10-job-completed-gpus-idle", "2026-11-14T23:00:00Z", 30)
end = 11
for g in GPUS:
    gid = gpu_id("13", "02", g)
    gpu_train(sc, gid, "finetune-job-311", util_fn=lambda m: 94 if m < end else 0,
              power_fn=lambda m: 575 if m < end else 86, clock_fn=lambda m: 1830 if m < end else 345,
              temp_fn=lambda m: 67 if m < end else 34 + 33 * math.exp(-(m - end) / 4), every=2)
sc.series("finetune-job-311", "workload.status", None, lambda m: "running" if m < end else "completed", "slurm", every=2)
sc.series("finetune-job-311", "workload.steps_completed", "count", lambda m: min(6000, 5880 + int(m * 11)), "slurm", every=2)
emit(sc, {
    "title": "Fine-tune job finishes; its 8 GPUs drop to 0% utilisation and cool down",
    "expected": {"flag": False, "category": "expected_workload_phase",
                 "reason": "Job reached stepsTotal (6000) and the scheduler marked it completed at the same minute utilisation dropped.",
                 "naiveRuleThatWouldFalselyTrigger": "utilisation drop > 80 pts in 1 min",
                 "suppressionEvidence": ["workload.status = completed", "steps_completed = stepsTotal"]},
    "primaryEntityIds": [gpu_id("13", "02", g) for g in GPUS], "affectedWorkloadIds": ["finetune-job-311"],
    "context": {"note": "Contrast with F07: there status stays 'running' and steps stop short of the total. Idle allocated-but-finished GPUs could go to an optimisation channel."},
    "stateAtWindowEnd": {"finetune-job-311": {"status": "completed", "finishedAt": "2026-11-14T23:11:00Z"}},
})

# B11 scheduled power cap reduction (demand response) lowers clocks
sc = Scn("B11-operator-power-cap-demand-response", "2026-11-14T16:30:00Z", 30)
capt = 10
for g in R12_GPUS[::2]:
    gpu_train(sc, g, J104, power_fn=lambda m: 610 if m < capt else 495, clock_fn=lambda m: 1830 if m < capt else 1590,
              temp_fn=lambda m: 66 if m < capt else ramp(m, capt, capt + 6, 66, 60),
              throttle_fn=lambda m: "none" if m < capt else "SW_POWER_CAP", every=2)
    sc.series(g, "gpu.power_limit_w", "W", lambda m: 700 if m < capt else 500, "dcgm", every=4)
job_metrics(sc, lambda m: 2410 if m < capt else 2690, every=2)
sc.add("rack-12", "control.power_cap_change", None, sc.start + timedelta(minutes=capt), "700W->500W per GPU; intervention int-0091 (grid demand-response 16:40-18:00)", "dcim",
       labels={"interventionId": "int-0091", "initiatedBy": "agent-datacenter-energy"})
emit(sc, {
    "title": "GPU clocks drop and training slows 12% after a deliberate power cap for a grid demand-response event",
    "expected": {"flag": False, "category": "intended_intervention",
                 "reason": "The slowdown is the intended effect of our own logged intervention (power limit 700 -> 500 W at 16:40). Throttle reason is SW_POWER_CAP (software-imposed), not thermal or hardware.",
                 "naiveRuleThatWouldFalselyTrigger": "throttle_reasons != none, or step_time +10%",
                 "suppressionEvidence": ["control.power_cap_change event with interventionId at onset",
                                         "throttle reason SW_POWER_CAP matches the change", "magnitude within predictedEffects"]},
    "primaryEntityIds": ["rack-12"], "affectedWorkloadIds": [J104],
    "context": {"note": "If step time rose much more than the intervention predicted, that would be a flag (outcome verification). Intervention record not in topology because the schema doesn't define it yet."},
})

# ------------------------------------------------------------------ write
with open(os.path.join(OUT, "topology.json"), "w") as f:
    json.dump(topology, f, indent=2)
index = {
    "description": "Scenarios are independent snapshots: counters and step numbers do not carry over between files. Labelled example scenarios for a deterministic 'escalate to agent?' filter. Each scenario file = window of TelemetryRecord[] plus expected label. topology.json = shared baseline DataCenter object that all scenarios reference by ID.",
    "datacenterId": DC, "topologyFile": "topology.json", "generatedBy": "gen.py (seeded, deterministic)",
    "labelFormat": {"expected.flag": "true = escalate to the low-level agent", "expected.keySignals": "signals that justify flagging",
                    "expected.naiveRuleThatWouldFalselyTrigger": "the simple rule this benign case is designed to trip",
                    "expected.suppressionEvidence": "the context that shows it is benign"},
    "pairs": [["B01-checkpoint-utilisation-dip", "F10-checkpoint-overrun-storage-stall"],
              ["B04-maintenance-window-outage", "F11-offline-after-maintenance-window"],
              ["B06-background-correctable-ecc", "F02-gpu-ecc-xid-fell-off-bus"],
              ["B10-job-completed-gpus-idle", "F07-hung-rank-silent-stall"],
              ["B05-single-spurious-sample", "F01-gpu-thermal-throttle"]],
    "scenarios": scenarios,
}
with open(os.path.join(OUT, "index.json"), "w") as f:
    json.dump(index, f, indent=2)
print(f"{len(scenarios)} scenarios, {sum(s['telemetryRecords'] for s in scenarios)} telemetry records")
