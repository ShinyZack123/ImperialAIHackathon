"""Transparent demo rules over Databricks-derived features, not a trained model.

Every rule emits active/clear/unknown for a snapshot. No lifecycle or root-cause
claim is inferred, and no action is performed. The deterministic-model teammate
can replace these rules while retaining this handoff contract.
"""

from datetime import datetime, timezone
import hashlib
import json

RULE_VERSION = "baseline-v1"
RULE_IDS = ("telemetry_missing", "thermal_limit", "power_limit", "new_ecc_errors", "workload_stall", "cooling_flow_low")


def evaluate_features(row: dict) -> list[dict]:
    if row["rule_version"] != RULE_VERSION:
        raise ValueError("Feature rule_version must match the implemented rule version.")
    alerts = []

    def emit(rule_id, condition, severity, summary, fields):
        timestamp = row["as_of_ts"]
        parsed = timestamp if isinstance(timestamp, datetime) else datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        # Spark collects TIMESTAMP as a naive datetime in the notebook's UTC session.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        canonical_ts = parsed.astimezone(timezone.utc).isoformat()
        identity = "|".join((str(row["run_id"]), canonical_ts, row["gpu_id"], rule_id, RULE_VERSION))
        evidence = {field: row.get(field) for field in fields}
        alerts.append({
            "alert_id": hashlib.sha256(identity.encode()).hexdigest(),
            "run_id": row["run_id"], "as_of_ts": timestamp, "gpu_id": row["gpu_id"],
            "rack_id": row["rack_id"], "vendor": row["vendor"],
            "rule_id": rule_id, "rule_version": RULE_VERSION,
            "status": "unknown" if condition is None else "active" if condition else "clear",
            "severity": severity, "summary": summary,
            "evidence_json": json.dumps(evidence, sort_keys=True, default=str, allow_nan=False),
        })

    observed = bool(row["telemetry_is_observed"])
    emit("telemetry_missing", not observed, "warning", "GPU telemetry unavailable or older than 15 seconds",
         ("telemetry_ts", "telemetry_age_seconds", "telemetry_is_observed"))
    thermal = None
    if observed and row.get("temp_c") is not None and row.get("temperature_limit_c") is not None:
        thermal = row["temp_c"] >= row["temperature_limit_c"]
    emit("thermal_limit", thermal, "critical", "GPU temperature reaches its configured synthetic limit",
         ("telemetry_ts", "temp_c", "temperature_limit_c", "temperature_headroom_c", "throttled"))
    power = None if row.get("power_ratio") is None else row["power_ratio"] > 1.0
    emit("power_limit", power, "warning", "GPU power exceeds its configured synthetic limit",
         ("telemetry_ts", "power_w", "power_limit_w", "power_ratio"))
    ecc = None if row.get("ecc_delta_60s") is None else row["ecc_delta_60s"] > 0
    emit("new_ecc_errors", ecc, "critical", "Uncorrected error counter increased in the recent complete window",
         ("telemetry_ts", "ecc_delta_60s", "ecc_window_seconds"))
    stalled = None
    if row["workload_is_fresh"]:
        if row.get("job_state") == "idle":
            stalled = False
        elif row.get("job_state") == "running" and row.get("progress_delta_60s") is not None:
            stalled = row["progress_delta_60s"] == 0
    emit("workload_stall", stalled, "warning", "Running job made no progress in the recent complete window",
         ("workload_ts", "job_id", "job_state", "progress_delta_60s", "progress_window_seconds"))
    cooling = None if row.get("flow_pct") is None else row["flow_pct"] < 50.0
    emit("cooling_flow_low", cooling, "warning", "Cooling-zone flow is below the illustrative 50 percent threshold",
         ("cooling_ts", "cooling_zone_id", "flow_pct", "inlet_temp_c"))
    return alerts
