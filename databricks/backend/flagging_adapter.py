"""Translate Databricks evidence into the team's actual deterministic flagger.

The connector preserves source IDs, units and replay time. It does not load the
flagging module's separate scenario fixtures or answer keys. Its result describes
findings in a bounded analysis window, not a confirmed current hardware diagnosis.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from functools import lru_cache
import hashlib
import importlib.util
import math
from pathlib import Path
import re
import sys
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
# Fingerprint the code actually imported into this process. The result store
# separately compares it to current files and rejects stale processes after edits.
_ADAPTER_SOURCE_AT_IMPORT = Path(__file__).read_bytes()
ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
SEVERITY = {"clear": 0, "warning": 1, "critical": 2}
LIMITATIONS = [
    "Synthetic replay data; configured limits are illustrative, not manufacturer specifications.",
    "Flags describe observations in the selected analysis window, including earlier events; they are not confirmed current hardware failures.",
    "The existing detector requires 300 seconds of persistence for hard limits. A short or early window can have no flags while readings are unusual.",
    "Maintenance, operator interventions, workload phase, calibrated baselines and throttle reasons are not supplied by these tables; their absence does not rule out those explanations.",
    "Cooling flow remains percent of nominal, and workload progress remains synthetic work units. No litres/minute, training steps, facility power or energy are inferred.",
    "Clock readings remain generic GPU clock MHz; domain-specific SM or memory clock checks are unavailable without a source-domain contract.",
    "The detector applies its existing 15 percent relative flow-drop policy to percent-of-nominal measurements; no conversion to volumetric flow is made.",
    "Unknown or stale latest telemetry is shown separately from the detector's findings. Clear means no finding in this window, not verified healthy hardware.",
]


@lru_cache(maxsize=1)
def _flagger():
    """Import by file without changing sys.path or colliding with generate.py."""
    path = ROOT / "flagging" / "flagger.py"
    config_path = ROOT / "flagging" / "config.json"
    flagger_source, configuration = path.read_bytes(), config_path.read_bytes()
    spec = importlib.util.spec_from_file_location("gpu_ops_shared_flagger", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("The shared flagging module is unavailable.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass annotation resolution uses this entry.
    try:
        spec.loader.exec_module(module)
        if path.read_bytes() != flagger_source or config_path.read_bytes() != configuration:
            raise RuntimeError("The detector changed during loading. Restart the backend.")
        module.GPU_OPS_ENGINE_VERSION = hashlib.sha256(flagger_source + configuration + _ADAPTER_SOURCE_AT_IMPORT).hexdigest()
    except Exception:
        sys.modules.pop(spec.name, None)
        raise
    return module


def _time(value: str | datetime) -> datetime:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError("Evidence timestamps must be timezone-aware ISO 8601 values.") from None
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Evidence timestamps must include a timezone.")
    return value.astimezone(timezone.utc)


def _iso(value: datetime | float) -> str:
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _cursor_iso(value: datetime) -> str:
    """Preserve the exact caller cutoff; only engine observations use whole seconds."""
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _identifier(value: Any, name: str) -> str:
    if not isinstance(value, str) or not ID.fullmatch(value):
        raise ValueError(f"Invalid evidence {name}.")
    return value


def _number(value: Any, name: str, nullable: bool = True) -> float | int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Evidence {name} must be a finite number.")
    return value


def _latest(rows, key):
    result = {}
    for row in rows:
        ident = row[key]
        if ident not in result or row["_time"] > result[ident]["_time"]:
            result[ident] = row
    return result


def _validate(evidence, run_id, start, end):
    if not isinstance(evidence, dict) or set(evidence) != {"inventory", "telemetry", "workloads", "cooling"}:
        raise ValueError("Evidence must contain the four documented data tables.")
    if not isinstance(run_id, str) or not run_id:
        raise ValueError("A replay run ID is required.")
    inventory = evidence["inventory"]
    if not isinstance(inventory, list) or not 1 <= len(inventory) <= 24:
        raise ValueError("The demo adapter requires 1–24 inventory GPUs.")
    items, servers, identifiers = {}, {}, set()
    for source in inventory:
        if not isinstance(source, dict):
            raise ValueError("Invalid inventory row.")
        row = dict(source)
        for key in ("gpu_id", "rack_id", "server_id", "cooling_zone_id"):
            _identifier(row.get(key), key)
        for key in ("vendor", "model"):
            if not isinstance(row.get(key), str) or not 1 <= len(row[key]) <= 128:
                raise ValueError(f"Invalid inventory {key}.")
        for key in ("power_limit_w", "temperature_limit_c"):
            if _number(row.get(key), key, False) <= 0:
                raise ValueError("Configured limits must be positive.")
        if row["gpu_id"] in items:
            raise ValueError("Duplicate inventory GPU IDs.")
        relation = (row["rack_id"], row["cooling_zone_id"])
        if row["server_id"] in servers and servers[row["server_id"]] != relation:
            raise ValueError("A server has conflicting rack/cooling identities.")
        servers[row["server_id"]] = relation
        items[row["gpu_id"]] = row
    categories = [set(items), {r["rack_id"] for r in items.values()}, set(servers), {r["cooling_zone_id"] for r in items.values()}]
    for group in categories:
        if identifiers & group:
            raise ValueError("Entity IDs must be unique across inventory types.")
        identifiers |= group
    tables = {}
    for table, key, known in (("telemetry", "gpu_id", set(items)), ("workloads", "gpu_id", set(items)), ("cooling", "cooling_zone_id", categories[-1])):
        rows = evidence[table]
        if not isinstance(rows, list) or len(rows) > 5000:
            raise ValueError("Evidence stream exceeds the bounded demo size.")
        cleaned, seen = [], set()
        for source in rows:
            if not isinstance(source, dict) or source.get("run_id") != run_id or source.get(key) not in known:
                raise ValueError("Evidence contains an unknown entity or another replay run.")
            row = dict(source)
            point = _time(row.get("event_ts"))
            if point.microsecond:
                raise ValueError("The shared detector requires whole-second evidence timestamps; fractional samples need an explicit adapter extension.")
            if not start <= point <= end:
                raise ValueError("Evidence lies outside the requested replay window.")
            identity = (row[key], point)
            if identity in seen:
                raise ValueError("Duplicate evidence sample identity.")
            seen.add(identity)
            row["_time"] = point
            row["event_ts"] = _iso(point)
            if table == "telemetry":
                if not isinstance(row.get("heartbeat_ok"), bool):
                    raise ValueError("GPU heartbeat must be boolean.")
                for field in ("power_w", "gpu_util_pct", "temp_c", "clock_mhz", "memory_used_gb", "memory_total_gb", "ecc_uncorrected_total"):
                    _number(row.get(field), field)
                if row.get("throttled") is not None and not isinstance(row["throttled"], bool):
                    raise ValueError("GPU throttling must be boolean or null.")
                if row.get("ecc_uncorrected_total") is not None and (not isinstance(row["ecc_uncorrected_total"], int) or row["ecc_uncorrected_total"] < 0):
                    raise ValueError("Error counters must be nonnegative integers.")
            elif table == "workloads":
                if row.get("job_id") is not None:
                    _identifier(row["job_id"], "job_id")
                    if row["job_id"] in identifiers:
                        raise ValueError("A workload ID collides with an inventory entity.")
                if row.get("job_state") not in {"running", "idle"}:
                    raise ValueError("Unsupported workload state.")
                if row["job_state"] == "running" and not row.get("job_id"):
                    raise ValueError("A running workload requires a job ID.")
                if not isinstance(row.get("progress_units"), int) or isinstance(row["progress_units"], bool) or row["progress_units"] < 0:
                    raise ValueError("Progress counters must be nonnegative integers.")
            else:
                _number(row.get("inlet_temp_c"), "inlet_temp_c")
                _number(row.get("flow_pct"), "flow_pct")
            cleaned.append(row)
        tables[table] = sorted(cleaned, key=lambda r: (r["_time"], r[key]))
    return items, tables


def _context_and_records(inventory, tables):
    racks = sorted({row["rack_id"] for row in inventory.values()})
    servers = {row["server_id"]: row["rack_id"] for row in inventory.values()}
    zones = sorted({row["cooling_zone_id"] for row in inventory.values()})
    context = {
        "schemaVersion": "1.0", "racks": [{"id": r, "type": "rack", "name": r} for r in racks],
        "servers": [{"id": s, "type": "server", "name": s, "parentId": r} for s, r in sorted(servers.items())],
        "gpus": [{"id": r["gpu_id"], "type": "gpu", "name": r["gpu_id"], "parentId": r["server_id"], "vendor": r["vendor"], "model": r["model"],
                  "attributes": {"temperatureLimitC": r["temperature_limit_c"], "powerLimitW": r["power_limit_w"]}} for r in inventory.values()],
        "cooling": [{"id": z, "type": "cooling_system", "name": z} for z in zones],
        "connections": [{"sourceEntityId": r["gpu_id"], "destinationEntityId": r["cooling_zone_id"], "relationship": "cooled_by"} for r in inventory.values()],
        "workloads": [], "maintenance": [], "interventions": [], "baselines": [], "counterRateBaselines": [],
        "metricCatalog": {
            "gpu.temperature_c": {"unit": "C", "kind": "gauge", "limits": {"critical": {"fromEntity": "attributes.temperatureLimitC"}}},
            "gpu.power_w": {"unit": "W", "kind": "gauge", "limits": {"warn": {"fromEntity": "attributes.powerLimitW"}}},
            "gpu.utilization_percent": {"unit": "%", "kind": "gauge", "plausibleRange": {"min": 0, "max": 100}},
            "gpu.ecc_uncorrected_total": {"unit": "count", "kind": "counter", "rate": {"criticalOnAnyIncrease": True}},
            "cdu.flow_percent": {"unit": "%", "kind": "gauge", "limits": {"warnBelow": 50}},
            "workload.progress_units": {"unit": "synthetic work units", "kind": "counter"},
        },
    }
    jobs = {}
    for row in tables["workloads"]:
        if row.get("job_id"):
            job = jobs.setdefault(row["job_id"], {"id": row["job_id"], "type": "workload", "name": row["job_id"],
                "allocation": {"gpuIds": [], "storageIds": []}, "progress": {"counterMetric": "workload.progress_units", "counterUnit": "synthetic work units"}})
            if row["gpu_id"] not in job["allocation"]["gpuIds"]:
                job["allocation"]["gpuIds"].append(row["gpu_id"])
            job.update(status=row["job_state"], priority=row.get("priority", "unknown"))
    context["workloads"] = list(jobs.values())
    records = []

    def add(eid, point, metric, value, unit):
        if value is not None:
            # The Databricks demo source contract samples every five seconds.
            context["metricCatalog"].setdefault(metric, {"unit": unit})["samplingIntervalSeconds"] = 5
            records.append({"entityId": eid, "timestamp": point, "metric": metric, "value": value, "unit": unit,
                            "quality": {"valid": True, "stale": False}, "source": {"connectorId": "databricks-synthetic-replay"}})

    mapping = (("temp_c", "gpu.temperature_c", "C"), ("power_w", "gpu.power_w", "W"),
               ("gpu_util_pct", "gpu.utilization_percent", "%"), ("clock_mhz", "gpu.clock_mhz", "MHz"),
               ("ecc_uncorrected_total", "gpu.ecc_uncorrected_total", "count"),
               ("throttled", "gpu.throttled", "boolean"))
    for row in tables["telemetry"]:
        add(row["gpu_id"], row["event_ts"], "exporter.gpu_up", row["heartbeat_ok"], "boolean")
        if row["heartbeat_ok"]:
            for field, metric, unit in mapping:
                add(row["gpu_id"], row["event_ts"], metric, row.get(field), unit)
    # One workload record per job/time; the fixture has one GPU per job. Reject
    # ambiguous shared-job counters rather than summing or inventing progress.
    emitted_jobs = {}
    for row in tables["workloads"]:
        if not row.get("job_id"):
            continue
        key = (row["job_id"], row["event_ts"])
        values = (row["job_state"], row["progress_units"])
        if key in emitted_jobs:
            if emitted_jobs[key] != values:
                raise ValueError("Shared workload progress requires an explicit aggregation contract.")
            continue
        emitted_jobs[key] = values
        add(row["job_id"], row["event_ts"], "workload.status", row["job_state"], "state")
        add(row["job_id"], row["event_ts"], "workload.progress_units", row["progress_units"], "synthetic work units")
    for row in tables["cooling"]:
        add(row["cooling_zone_id"], row["event_ts"], "cdu.flow_percent", row.get("flow_pct"), "%")
        add(row["cooling_zone_id"], row["event_ts"], "cdu.inlet_temperature_c", row.get("inlet_temp_c"), "C")
    records.sort(key=lambda r: (r["timestamp"], r["entityId"], r["metric"]))
    return context, records


def generate_snapshot(evidence: dict, run_id: str, as_of_ts: str | datetime, window_start_ts: str | datetime) -> dict:
    """Run the repository detector on one bounded Databricks evidence snapshot."""
    start, end = _time(window_start_ts), _time(as_of_ts)
    if not 0 <= (end - start).total_seconds() <= 15 * 60:
        raise ValueError("The analysis window must be ordered and no longer than 15 minutes.")
    inventory, tables = _validate(evidence, run_id, start, end)
    context, records = _context_and_records(inventory, tables)
    detector = _flagger()
    raw_incidents, raw_suppressed = detector.flag_window(context, records, now=end.timestamp())
    incidents = []
    states = {}
    jobs = {j["id"]: j for j in context["workloads"]}
    for incident in raw_incidents:
        findings = [{"detector": f.detector, "severity": f.severity, "entity": f.entity, "metric": f.metric,
                     "start": _iso(f.start), "end": _iso(f.end), "text": f.text, "method": f.method,
                     "workloads": sorted(f.workloads)} for f in incident.findings]
        affected = set()
        for finding in incident.findings:
            if finding.entity in inventory:
                affected.add(finding.entity)
            affected.update(g["gpu_id"] for g in inventory.values() if finding.entity in {g["rack_id"], g["server_id"], g["cooling_zone_id"]})
            affected.update(jobs.get(finding.entity, {}).get("allocation", {}).get("gpuIds", []))
        for gpu in affected:
            if SEVERITY.get(incident.severity, 0) > SEVERITY.get(states.get(gpu), 0):
                states[gpu] = incident.severity
        incidents.append({"severity": incident.severity, "rootEntity": incident.root_entity,
                          "entities": incident.entities, "workloads": incident.workloads,
                          "detectors": incident.detectors, "message": incident.message,
                          "findings": findings, "affectedGpuIds": sorted(affected), "scope": "analysis_window"})
    latest_t = _latest(tables["telemetry"], "gpu_id")
    latest_w = _latest(tables["workloads"], "gpu_id")
    latest_c = _latest(tables["cooling"], "cooling_zone_id")
    by_rack = defaultdict(list)
    all_gpus = []
    for gpu_id, item in sorted(inventory.items()):
        reading, work, cooling = latest_t.get(gpu_id), latest_w.get(gpu_id), latest_c.get(item["cooling_zone_id"])
        fresh = bool(reading and reading["heartbeat_ok"] and (end - reading["_time"]).total_seconds() <= 15)
        work_fresh = bool(work and (end - work["_time"]).total_seconds() <= 15)
        cooling_fresh = bool(cooling and (end - cooling["_time"]).total_seconds() <= 15)
        metrics = {name: reading.get(name) if fresh else None for name in ("power_w", "temp_c", "gpu_util_pct", "clock_mhz", "memory_used_gb", "memory_total_gb", "ecc_uncorrected_total", "throttled")}
        gpu = {"id": gpu_id, "vendor": item["vendor"], "model": item["model"], "rack_id": item["rack_id"],
               "server_id": item["server_id"], "cooling_zone_id": item["cooling_zone_id"],
               "state": states.get(gpu_id, "clear") if fresh else "unknown", "state_scope": "analysis_window",
               "telemetry_is_observed": fresh, "telemetry_ts": reading["event_ts"] if reading else None,
               "telemetry_age_seconds": (end - reading["_time"]).total_seconds() if reading else None,
               "metrics": metrics, "limits": {"power_w": item["power_limit_w"], "temperature_c": item["temperature_limit_c"]},
               "workload": {"is_fresh": work_fresh, "event_ts": work["event_ts"] if work else None,
                            "job_id": work.get("job_id") if work_fresh else None,
                            "state": work["job_state"] if work_fresh else "unknown",
                            "progress_units": work["progress_units"] if work_fresh else None},
               "cooling": {"is_fresh": cooling_fresh, "event_ts": cooling["event_ts"] if cooling else None,
                           "flow_pct": cooling.get("flow_pct") if cooling_fresh else None,
                           "inlet_temp_c": cooling.get("inlet_temp_c") if cooling_fresh else None}}
        by_rack[item["rack_id"]].append(gpu)
        all_gpus.append(gpu)
    observed = [g for g in all_gpus if g["telemetry_is_observed"]]

    def values(metric):
        return [g["metrics"][metric] for g in observed if g["metrics"][metric] is not None]

    def average(metric):
        found = values(metric)
        return sum(found) / len(found) if found else None

    power = values("power_w")
    overview = {"total_gpu_count": len(all_gpus), "observed_gpu_count": len(observed),
                "unknown_gpu_count": len(all_gpus) - len(observed), "power_reporting_gpu_count": len(power),
                "observed_gpu_power_kw": sum(power) / 1000 if power else None,
                "observed_avg_gpu_util_pct": average("gpu_util_pct"), "observed_avg_temp_c": average("temp_c")}
    code_hash = detector.GPU_OPS_ENGINE_VERSION
    return {"schemaVersion": "1.0", "source": "databricks", "synthetic": True, "run_id": run_id,
            "as_of_ts": _cursor_iso(end), "window": {"start": _cursor_iso(start), "end": _cursor_iso(end)},
            "state_scope": "analysis_window", "overview": overview,
            "racks": [{"id": rack, "label": rack, "gpus": gpus} for rack, gpus in sorted(by_rack.items())],
            "incidents": incidents,
            "suppressed": [{"entity": f.entity, "metric": f.metric, "detector": f.detector, "reason": why} for f, why in raw_suppressed],
            "engine": {"module": "flagging.flagger.flag_window", "version": code_hash, "deterministic": True,
                       "codeKey": code_hash[:16], "persistenceSeconds": detector.CFG["persist_s"], "recordsAnalyzed": len(records)},
            "coverage": {"maintenance": False, "interventions": False, "workload_phase": False, "calibrated_baselines": False,
                         "gpu_telemetry": len(observed), "inventory": len(all_gpus)}, "limitations": list(LIMITATIONS)}
