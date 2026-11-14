#!/usr/bin/env python3
"""Create synthetic, reproducible GPU telemetry for a Databricks demo.

Public API: generate_dataset(output_dir, seed=42, scenario="all", overwrite=False).
Only evidence/*.csv should be exposed to a model or an agent. The evaluation/
directory contains scenario labels and must remain outside their data access.

This is a prerecorded replay: recovery happens on a fixed schedule. It is not
proof that an agent action repaired anything. All device limits are demo values,
not manufacturer specifications. No third-party packages are required.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
from pathlib import Path
import random
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from uuid import NAMESPACE_URL, uuid5


SCENARIOS = ("cooling_fault", "healthy_load", "telemetry_gap", "isolated_error")
START_TIME = datetime(2026, 11, 14, 9, 0, tzinfo=timezone.utc)
SAMPLE_COUNT = 180
INTERVAL_SECONDS = 5
SCHEMA_VERSION = "1.0"
GENERATOR_ID = "gpu-ops-data"

SCHEMAS = {
    "inventory": (
        "gpu_id", "vendor", "model", "rack_id", "server_id", "cooling_zone_id",
        "power_limit_w", "temperature_limit_c",
    ),
    "telemetry": (
        "run_id", "event_ts", "gpu_id", "power_w", "gpu_util_pct", "memory_used_gb",
        "memory_total_gb", "temp_c", "clock_mhz", "throttled",
        "ecc_uncorrected_total", "heartbeat_ok",
    ),
    "workloads": (
        "run_id", "event_ts", "gpu_id", "job_id", "job_state", "progress_units", "priority",
    ),
    "cooling": (
        "run_id", "event_ts", "cooling_zone_id", "inlet_temp_c", "flow_pct",
    ),
}
NULLABLE_METRICS = SCHEMAS["telemetry"][3:-1]


def timestamp(sample_index: int) -> str:
    """Return the timestamp for a sample, including an exclusive end boundary."""
    return (START_TIME + timedelta(seconds=sample_index * INTERVAL_SECONDS)).isoformat().replace("+00:00", "Z")


def build_inventory() -> list[dict]:
    """Return 24 synthetic GPUs: eight per vendor across three mixed-vendor racks."""
    profiles = {
        "NVIDIA": (450.0, 90.0),
        "AMD": (500.0, 88.0),
        "Intel": (350.0, 85.0),
    }
    inventory = []
    for rack, vendors in (("a", ("NVIDIA", "AMD")), ("b", ("AMD", "Intel")), ("c", ("Intel", "NVIDIA"))):
        for server_number, vendor in enumerate(vendors, start=1):
            for _ in range(4):
                power_limit, temperature_limit = profiles[vendor]
                inventory.append({
                    "gpu_id": f"gpu-{len(inventory) + 1:03d}",
                    "vendor": vendor,
                    "model": f"Synthetic-{vendor}",
                    "rack_id": f"rack-{rack}",
                    "server_id": f"server-{rack}{server_number}",
                    "cooling_zone_id": f"zone-{rack}",
                    "power_limit_w": power_limit,
                    "temperature_limit_c": temperature_limit,
                })
    return inventory


def _cooling_factor(index: int) -> float:
    if 60 <= index < 120:
        return min(1.0, (index - 59) / 12)
    if 120 <= index < 132:
        return max(0.0, 1 - (index - 119) / 12)
    return 0.0


def _csv_row(row: dict) -> dict:
    return {key: (str(value).lower() if isinstance(value, bool) else value) for key, value in row.items()}


def _write_run(writers: dict, inventory: list[dict], seed: int, scenario: str) -> dict:
    run_id = str(uuid5(NAMESPACE_URL, f"{GENERATOR_ID}/{SCHEMA_VERSION}/{seed}/{scenario}"))
    # The same seeded background makes scenario comparisons reproducible.
    rng = random.Random(seed)
    progress = {gpu["gpu_id"]: 0 for gpu in inventory}
    for index in range(SAMPLE_COUNT):
        event_ts = timestamp(index)
        phase_active = 60 <= index < 120
        cooling_factor = _cooling_factor(index) if scenario == "cooling_fault" else 0.0
        for zone in ("zone-a", "zone-b", "zone-c"):
            factor = cooling_factor if zone == "zone-b" else 0.0
            writers["cooling"].writerow({
                "run_id": run_id, "event_ts": event_ts, "cooling_zone_id": zone,
                "inlet_temp_c": round(21 + 17 * factor + rng.uniform(-0.3, 0.3), 3),
                "flow_pct": round(100 - 80 * factor + rng.uniform(-0.5, 0), 3),
            })
        for gpu_index, gpu in enumerate(inventory):
            gpu_id = gpu["gpu_id"]
            idle = gpu_id == "gpu-024"
            util = max(0, min(100, 65 + 13 * math.sin(index / 13 + gpu_index) + rng.uniform(-2, 2)))
            power_limit = gpu["power_limit_w"]
            power = power_limit * (0.12 + 0.67 * util / 100)
            temperature = 42 + 0.055 * power + rng.uniform(-1.2, 1.2)
            clock = 1500.0
            memory_total = 64.0
            memory_used = 28 + gpu_index % 5 + rng.uniform(-0.2, 0.2)
            throttled = False
            ecc = 0
            heartbeat = True
            stalled = False
            if idle:
                util = rng.uniform(0, 2)
                power = power_limit * 0.08
                temperature = 35 + rng.uniform(-1, 1)
                clock = 300.0
                memory_used = 0.5
            elif scenario == "healthy_load" and phase_active:
                util = 94 + rng.uniform(-2, 2)
                power = power_limit * (0.86 + rng.uniform(0, 0.03))
                temperature = gpu["temperature_limit_c"] - 10 + rng.uniform(-1, 1)
                memory_used = 48 + rng.uniform(-0.2, 0.2)
            elif scenario == "cooling_fault" and gpu["rack_id"] == "rack-b" and cooling_factor:
                temperature += (gpu["temperature_limit_c"] + 5 - temperature) * cooling_factor
                throttled = temperature >= gpu["temperature_limit_c"]
                if throttled:
                    clock = 825.0
                    power *= 0.78
            elif scenario == "isolated_error" and gpu_id == "gpu-001":
                # These are cumulative lifetime counters. Recovery does not reset them.
                ecc = int(index >= 60) + int(index >= 90)
                if phase_active:
                    util = 2.0
                    power = power_limit * 0.18
                    temperature = 47.0
                    clock = 500.0
                    stalled = True
            if scenario == "telemetry_gap" and gpu_id == "gpu-017" and phase_active:
                heartbeat = False

            if not idle and not stalled:
                progress[gpu_id] += max(1, int(util * clock / 5000))
            # Workload progress is an independent scheduler/application signal,
            # so it can remain available while the GPU collector is unreachable.
            writers["workloads"].writerow({
                "run_id": run_id, "event_ts": event_ts, "gpu_id": gpu_id,
                "job_id": None if idle else f"job-{gpu_id}",
                "job_state": "idle" if idle else "running",
                "progress_units": progress[gpu_id],
                "priority": "high" if gpu_index % 6 == 0 else "normal",
            })
            row = {
                "run_id": run_id, "event_ts": event_ts, "gpu_id": gpu_id,
                "power_w": round(power, 3), "gpu_util_pct": round(util, 3),
                "memory_used_gb": round(memory_used, 3), "memory_total_gb": memory_total,
                "temp_c": round(temperature, 3), "clock_mhz": clock,
                "throttled": throttled, "ecc_uncorrected_total": ecc,
                "heartbeat_ok": heartbeat,
            }
            if not heartbeat:
                for name in NULLABLE_METRICS:
                    row[name] = None
            writers["telemetry"].writerow(_csv_row(row))

    expected = {
        "cooling_fault": {"cause": "shared_cooling_degradation", "affected_gpu_ids": [g["gpu_id"] for g in inventory if g["rack_id"] == "rack-b"], "cooling_zone_id": "zone-b"},
        "healthy_load": {"cause": "healthy_workload_increase", "affected_gpu_ids": [], "should_raise_fault": False},
        "telemetry_gap": {"cause": "collector_unreachable", "affected_gpu_ids": ["gpu-017"], "hardware_failure_established": False},
        "isolated_error": {"cause": "isolated_gpu_error_with_stalled_workload", "affected_gpu_ids": ["gpu-001"]},
    }[scenario]
    return {
        "run_id": run_id, "scenario": scenario, "expected": expected,
        "baseline_start": timestamp(0), "event_start": timestamp(60),
        "scheduled_recovery_start": timestamp(120), "fully_recovered_by": timestamp(132),
        "end_exclusive": timestamp(SAMPLE_COUNT), "idle_negative_control_gpu": "gpu-024",
    }


def _check_target(target: Path, overwrite: bool) -> None:
    if target.is_symlink():
        raise ValueError("Refusing to replace a symbolic-link output directory.")
    if not target.exists():
        return
    if not overwrite:
        raise FileExistsError(f"Output already exists: {target}. Choose another path or use --overwrite.")
    marker = target / "evaluation" / "generation.json"
    try:
        metadata = json.loads(marker.read_text())
    except (OSError, ValueError) as exc:
        raise ValueError("--overwrite only replaces a dataset previously made by this generator.") from exc
    if metadata.get("generator") != GENERATOR_ID or set(p.name for p in target.iterdir()) != {"evidence", "evaluation"}:
        raise ValueError("Refusing to overwrite an unrecognised output directory or extra files.")
    for directory, expected_names in (
        (target / "evidence", {f"{table}.csv" for table in SCHEMAS}),
        (target / "evaluation", {"generation.json", "ground_truth.json"}),
    ):
        if directory.is_symlink() or not directory.is_dir() or {p.name for p in directory.iterdir()} != expected_names:
            raise ValueError("Refusing to overwrite a modified dataset directory.")
        if any(path.is_symlink() or not path.is_file() for path in directory.iterdir()):
            raise ValueError("Refusing to overwrite symlinks or nested directories in a dataset.")


def generate_dataset(output_dir: str | Path, seed: int = 42, scenario: str = "all", overwrite: bool = False) -> dict:
    """Write CSV evidence and private evaluation labels; return generation metadata.

    Writes into a staging directory before swapping the completed dataset into
    place. --overwrite is restricted to recognised generated directories.
    """
    if scenario != "all" and scenario not in SCENARIOS:
        raise ValueError(f"Unknown scenario: {scenario}")
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise TypeError("seed must be an integer")
    target = Path(output_dir).expanduser().absolute()
    _check_target(target, overwrite)
    target.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix=f".{target.name}-staging-", dir=target.parent))
    staged = stage_root / "dataset"
    backup = stage_root / "previous"
    preserve_backup = False
    try:
        (staged / "evidence").mkdir(parents=True)
        (staged / "evaluation").mkdir()
        inventory = build_inventory()
        selected = SCENARIOS if scenario == "all" else (scenario,)
        handles = {}
        writers = {}
        try:
            for table, columns in SCHEMAS.items():
                handles[table] = (staged / "evidence" / f"{table}.csv").open("w", newline="", encoding="utf-8")
                writers[table] = csv.DictWriter(handles[table], fieldnames=columns, lineterminator="\n")
                writers[table].writeheader()
            writers["inventory"].writerows(inventory)
            labels = [_write_run(writers, inventory, seed, name) for name in selected]
        finally:
            for handle in handles.values():
                handle.close()
        metadata = {
            "generator": GENERATOR_ID, "schema_version": SCHEMA_VERSION,
            "seed": seed, "scenario_selection": scenario, "run_count": len(selected),
            "samples_per_run": SAMPLE_COUNT, "interval_seconds": INTERVAL_SECONDS,
            "start_time": timestamp(0), "end_exclusive": timestamp(SAMPLE_COUNT),
            "row_counts": {"inventory": 24, "telemetry": 24 * SAMPLE_COUNT * len(selected), "workloads": 24 * SAMPLE_COUNT * len(selected), "cooling": 3 * SAMPLE_COUNT * len(selected)},
            "synthetic": True,
            "note": "Prerecorded replay. Recovery is scheduled, not caused by agent action. Limits are demo settings, not manufacturer specifications.",
        }
        for name, value in (("generation.json", metadata), ("ground_truth.json", {"schema_version": SCHEMA_VERSION, "runs": labels})):
            (staged / "evaluation" / name).write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        # Recheck immediately before mutation; never silently replace a new target.
        _check_target(target, overwrite)
        if target.exists():
            os.replace(target, backup)
        try:
            os.replace(staged, target)
        except BaseException:
            if backup.exists():
                try:
                    os.replace(backup, target)
                except OSError as restore_error:
                    preserve_backup = True
                    raise RuntimeError(f"Could not restore the previous dataset; preserved it at {backup}") from restore_error
            raise
        return metadata
    finally:
        if not preserve_backup:
            shutil.rmtree(stage_root)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="data", help="New dataset directory (default: data).")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--scenario", choices=("all",) + SCENARIOS, default="all")
    parser.add_argument("--overwrite", action="store_true", help="Replace a recognised generated dataset.")
    args = parser.parse_args()
    try:
        metadata = generate_dataset(args.output, args.seed, args.scenario, args.overwrite)
    except (OSError, ValueError, TypeError) as exc:
        parser.exit(1, f"Error: {exc}\n")
    print(json.dumps({"output": str(Path(args.output).absolute()), **metadata}, indent=2))


if __name__ == "__main__":
    main()
