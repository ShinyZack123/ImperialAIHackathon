#!/usr/bin/env python3
"""Isolated, standard-library-only bridge for the exported Databricks replay.

The archive contains evidence, never cached assessments. Snapshot requests run
the repository's current deterministic detector and never authenticate or save
results to Databricks. Compatible with Python 3.9 and later.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import gzip
import hashlib
import importlib.util
import io
import json
import math
from pathlib import Path
import re
import sys
from uuid import UUID


DEMO = Path(__file__).resolve().parents[1] / "demo"
MAX_BYTES = 20 * 1024 * 1024
TABLE_FIELDS = {
    "inventory": {"gpu_id", "vendor", "model", "rack_id", "server_id", "cooling_zone_id", "power_limit_w", "temperature_limit_c"},
    "telemetry": {"run_id", "event_ts", "gpu_id", "power_w", "gpu_util_pct", "memory_used_gb", "memory_total_gb", "temp_c", "clock_mhz", "throttled", "ecc_uncorrected_total", "heartbeat_ok"},
    "workloads": {"run_id", "event_ts", "gpu_id", "job_id", "job_state", "progress_units", "priority"},
    "cooling": {"run_id", "event_ts", "cooling_zone_id", "inlet_temp_c", "flow_pct"},
}
ROW_LIMITS = {"inventory": 24, "telemetry": 17280, "workloads": 17280, "cooling": 2160}
ARG_COUNTS = {"runs": 0, "snapshot": 2, "history": 4, "workload-history": 4}
INVALID_REQUEST = "Choose a known replay run/GPU and a timezone-aware time inside its recorded interval; history windows must be 1–15 minutes."
INVALID_EXPORT = "Saved Databricks replay failed validation. Restore databricks/demo from the repository."


class ReplayError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.status = status


def _time(value):
    if not isinstance(value, str):
        raise ValueError
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError
    return result.astimezone(timezone.utc)


def _iso(value):
    return value.isoformat().replace("+00:00", "Z")


def _uuid(value):
    if not isinstance(value, str):
        raise ValueError
    return str(UUID(value))


def _finite(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError
    return number


def _reject_constant(value):
    raise ValueError


def _json(payload):
    return json.loads(payload, parse_float=_finite, parse_constant=_reject_constant)


def _read_bounded(path, limit):
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise ValueError
    return value


def _adapter():
    path = Path(__file__).with_name("flagging_adapter.py")
    spec = importlib.util.spec_from_file_location("gpu_ops_replay_adapter", path)
    if spec is None or spec.loader is None:
        raise RuntimeError
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class SavedReplay:
    def __init__(self, directory=DEMO):
        try:
            directory = Path(directory)
            manifest = _json(_read_bounded(directory / "manifest.json", 64 * 1024))
            archive = _read_bounded(directory / "evidence.json.gz", MAX_BYTES)
            if not isinstance(manifest, dict) or any(manifest.get(key) != value for key, value in {
                "schema_version": "1.0", "source": "databricks", "synthetic": True, "delivery": "saved_replay",
            }.items()) or manifest.get("synthetic") is not True:
                raise ValueError
            for field in ("archive_sha256", "payload_sha256"):
                if not isinstance(manifest.get(field), str) or not re.fullmatch(r"[a-f0-9]{64}", manifest[field]):
                    raise ValueError
            if hashlib.sha256(archive).hexdigest() != manifest["archive_sha256"]:
                raise ValueError
            with gzip.GzipFile(fileobj=io.BytesIO(archive)) as handle:
                payload = handle.read(MAX_BYTES + 1)
            if len(payload) > MAX_BYTES or hashlib.sha256(payload).hexdigest() != manifest["payload_sha256"]:
                raise ValueError
            evidence = _json(payload)
            if not isinstance(evidence, dict) or set(evidence) != set(TABLE_FIELDS):
                raise ValueError
            counts = manifest.get("row_counts")
            if not isinstance(counts, dict) or set(counts) != set(TABLE_FIELDS):
                raise ValueError
            for table, fields in TABLE_FIELDS.items():
                rows = evidence[table]
                if not isinstance(rows, list) or not 0 < len(rows) <= ROW_LIMITS[table] or type(counts[table]) is not int or counts[table] != len(rows):
                    raise ValueError
                if any(not isinstance(row, dict) or set(row) != fields for row in rows):
                    raise ValueError
            self.exported_at = _iso(_time(manifest["exported_at"]))
            self.runs = manifest.get("runs")
            if not isinstance(self.runs, list) or not 1 <= len(self.runs) <= 4:
                raise ValueError
            self.intervals = {}
            for run in self.runs:
                if not isinstance(run, dict) or set(run) != {"run_id", "start_ts", "end_ts", "sample_count"}:
                    raise ValueError
                run_id = _uuid(run["run_id"])
                start, end = _time(run["start_ts"]), _time(run["end_ts"])
                if run_id != run["run_id"] or run_id in self.intervals or start > end or type(run["sample_count"]) is not int or run["sample_count"] <= 0:
                    raise ValueError
                self.intervals[run_id] = (start, end)
            self.gpus = {row["gpu_id"] for row in evidence["inventory"]}
            zones = {row["cooling_zone_id"] for row in evidence["inventory"]}
            if len(self.gpus) != len(evidence["inventory"]) or any(not isinstance(gpu, str) for gpu in self.gpus):
                raise ValueError
            self.rows = {name: defaultdict(list) for name in ("telemetry", "workloads", "cooling")}
            for table, by_run in self.rows.items():
                identities = set()
                entity_key, known = ("cooling_zone_id", zones) if table == "cooling" else ("gpu_id", self.gpus)
                for row in evidence[table]:
                    run_id, point = row["run_id"], _time(row["event_ts"])
                    if run_id not in self.intervals or row[entity_key] not in known:
                        raise ValueError
                    start, end = self.intervals[run_id]
                    identity = (run_id, row[entity_key], point)
                    if not start <= point <= end or point.microsecond or identity in identities:
                        raise ValueError
                    identities.add(identity)
                    by_run[run_id].append((point, row))
                if set(by_run) != set(self.intervals):
                    raise ValueError
                for rows in by_run.values():
                    rows.sort(key=lambda item: (item[0], item[1][entity_key]))
            for run in self.runs:
                rows = self.rows["telemetry"][run["run_id"]]
                if len(rows) != run["sample_count"] or (rows[0][0], rows[-1][0]) != self.intervals[run["run_id"]]:
                    raise ValueError
            self.inventory = evidence["inventory"]
        except (OSError, EOFError, ValueError, TypeError, KeyError, OverflowError, RecursionError):
            raise ReplayError(INVALID_EXPORT, 503) from None

    def envelope(self, data, run_id=None, cursor=None):
        result = {"schema_version": "1.0", "source": "databricks", "synthetic": True,
                  "delivery": "saved_replay", "exported_at": self.exported_at, "data": data}
        if run_id is not None:
            result.update(run_id=run_id, as_of_ts=_iso(cursor))
        return result

    def request(self, action, args):
        if action not in ARG_COUNTS or len(args) != ARG_COUNTS[action]:
            raise ReplayError(INVALID_REQUEST, 400)
        if action == "runs":
            return self.envelope(self.runs)
        try:
            run_id, cursor = _uuid(args[0]), _time(args[1])
            if run_id not in self.intervals or not self.intervals[run_id][0] <= cursor <= self.intervals[run_id][1]:
                raise ValueError
            if action != "snapshot":
                gpu_id, window = args[2], int(args[3])
                if gpu_id not in self.gpus or not 1 <= window <= 15 or str(window) != args[3]:
                    raise ValueError
        except (ValueError, TypeError, OverflowError):
            raise ReplayError(INVALID_REQUEST, 400) from None
        if action != "snapshot":
            table = "telemetry" if action == "history" else "workloads"
            start = cursor - timedelta(minutes=window)
            data = [row for point, row in reversed(self.rows[table][run_id]) if start <= point <= cursor and row["gpu_id"] == gpu_id]
            return self.envelope(data, run_id, cursor)
        start = cursor - timedelta(minutes=15)
        evidence = {"inventory": self.inventory, **{name: [row for point, row in by_run[run_id] if start <= point <= cursor] for name, by_run in self.rows.items()}}
        try:
            result = _adapter().generate_snapshot(evidence, run_id, cursor, start)
        except Exception:
            raise ReplayError("The shared detector could not analyse this saved replay. No substitute assessment was used.", 502) from None
        result.update(delivery="saved_replay", exported_at=self.exported_at,
                      persistence={"status": "not_saved", "reason": "saved_replay"})
        return self.envelope(result, run_id, cursor)


def main(argv=None):
    try:
        args = sys.argv[1:] if argv is None else argv
        if not args or args[0] not in ARG_COUNTS or len(args) - 1 != ARG_COUNTS[args[0]]:
            raise ReplayError(INVALID_REQUEST, 400)
        result = SavedReplay().request(args[0], args[1:])
        print(json.dumps(result, allow_nan=False, separators=(",", ":")))
        return 0
    except ReplayError as error:
        print(json.dumps({"error": str(error), "status": error.status}))
        return 1
    except Exception:
        print(json.dumps({"error": "Saved replay is unavailable. Check the Python runtime and repository files.", "status": 503}))
        return 1


if __name__ == "__main__":
    sys.exit(main())
