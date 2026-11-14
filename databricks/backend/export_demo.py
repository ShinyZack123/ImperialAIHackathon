"""Export the synthetic Databricks fixture for credential-free local replay.

Run from databricks/: python -m backend.export_demo
This performs evidence reads only. No credentials, workspace URLs, evaluation
labels or saved diagnoses are included. The local bridge runs the real detector.
"""
from datetime import datetime, timezone
import gzip
import hashlib
import json
from pathlib import Path
import re

from dotenv import load_dotenv

from backend.databricks_data import DatabricksData, DataSettings
from backend.flagging_adapter import _time, _validate


ROOT = Path(__file__).resolve().parents[1]


def export(source, destination):
    destination = Path(destination)
    if destination.exists():
        raise ValueError("Export destination already exists. Choose a new directory to review a replacement.")
    runs = source.list_runs()
    if len(runs) != 4:
        raise ValueError("Only the four-run synthetic hackathon fixture may be bundled.")
    combined = {name: [] for name in ("inventory", "telemetry", "workloads", "cooling")}
    for run in runs:
        start, end = _time(run["start_ts"]), _time(run["end_ts"])
        if (end - start).total_seconds() != 895 or run["sample_count"] != 4320:
            raise ValueError("The source does not match the fifteen-minute synthetic fixture.")
        evidence = source.fleet_evidence(run["run_id"], end, start)
        _validate(evidence, run["run_id"], start, end)
        inventory = sorted(evidence["inventory"], key=lambda row: row["gpu_id"])
        if {row["gpu_id"] for row in inventory} != {f"gpu-{n:03}" for n in range(1, 25)}:
            raise ValueError("Unexpected inventory; do not export real infrastructure.")
        for item in inventory:
            if item["vendor"] not in {"NVIDIA", "AMD", "Intel"} or item["model"] != "Synthetic-" + item["vendor"]:
                raise ValueError("Only explicitly synthetic hardware may be bundled.")
            if not re.fullmatch(r"rack-[abc]", item["rack_id"]) or not re.fullmatch(r"server-[abc][12]", item["server_id"]) or not re.fullmatch(r"zone-[abc]", item["cooling_zone_id"]):
                raise ValueError("Unexpected fixture identity.")
        if combined["inventory"] and combined["inventory"] != inventory:
            raise ValueError("Inventory changed during export.")
        combined["inventory"] = inventory
        for row in evidence["workloads"]:
            if row["job_id"] is not None and not re.fullmatch(r"job-gpu-\d{3}", row["job_id"]):
                raise ValueError("Unexpected workload identity; export stopped.")
        for name in ("telemetry", "workloads", "cooling"):
            combined[name].extend(evidence[name])
        print(f"Read synthetic replay {run['run_id'][:8]}", flush=True)
    expected = {"inventory": 24, "telemetry": 17280, "workloads": 17280, "cooling": 2160}
    if {name: len(rows) for name, rows in combined.items()} != expected:
        raise ValueError("Incomplete synthetic export.")
    for name, rows in combined.items():
        rows.sort(key=lambda row: (row.get("run_id", ""), row.get("event_ts", ""), row.get("gpu_id", row.get("cooling_zone_id", ""))))
    payload = json.dumps(combined, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
    if len(payload) > 20 * 1024 * 1024:
        raise ValueError("Export exceeds the local demo size limit.")
    archive = gzip.compress(payload, compresslevel=9, mtime=0)
    manifest = {
        "schema_version": "1.0", "source": "databricks", "synthetic": True,
        "delivery": "saved_replay", "exported_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "archive_sha256": hashlib.sha256(archive).hexdigest(),
        "runs": runs, "row_counts": expected,
    }
    destination.mkdir(parents=True)
    (destination / "evidence.json.gz").write_bytes(archive)
    (destination / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps({"exported": True, "row_counts": expected, "archive_bytes": len(archive)}))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "demo")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    export(DatabricksData(DataSettings.from_env()), args.output)
