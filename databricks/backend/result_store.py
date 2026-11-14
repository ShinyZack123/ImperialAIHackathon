"""Persist the shared detector's observed results without modifying source evidence.

The polling deadline covers DDL and MERGE together. As with the read client,
blocking SDK requests/retries can extend wall time past that deadline. The shared
source client configures finite HTTP/retry timeouts. No snapshot is reported saved
until Databricks reports the MERGE statement SUCCEEDED.
"""
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import threading
import time
from uuid import UUID

from backend.databricks_data import DataQueryError, DataQueryTimeout


ROOT = Path(__file__).resolve().parents[2]
TABLE = "official_detector_results"
MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_EVIDENCE_BYTES = 16 * 1024 * 1024
DATASETS = {"inventory", "telemetry", "workloads", "cooling"}
SNAPSHOT_FIELDS = {
    "schemaVersion", "source", "synthetic", "run_id", "as_of_ts", "window",
    "state_scope", "overview", "racks", "incidents", "suppressed", "engine",
    "coverage", "limitations",
}
FORBIDDEN_FIELDS = {"ground_truth", "groundTruth", "answer_key", "answerKey", "evaluation", "scenario", "scenario_name", "scenarioName", "expected_cause", "expectedCause"}


def detector_version():
    return hashlib.sha256(
        (ROOT / "flagging/flagger.py").read_bytes()
        + (ROOT / "flagging/config.json").read_bytes()
        + (ROOT / "databricks/backend/flagging_adapter.py").read_bytes()
    ).hexdigest()


def _timestamp(value):
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("A timezone-aware timestamp is required.")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def _exclude_evaluation_labels(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            if FORBIDDEN_FIELDS.intersection(item):
                raise ValueError("Evaluation labels cannot enter official detector results.")
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)


def _prepare(snapshot, evidence):
    """Validate provenance, then hash canonical source records independent of row order."""
    try:
        if not isinstance(snapshot, dict) or set(snapshot) - SNAPSHOT_FIELDS:
            raise ValueError
        if snapshot.get("source") != "databricks" or snapshot.get("synthetic") is not True:
            raise ValueError
        engine = snapshot.get("engine")
        version = detector_version()
        if not isinstance(engine, dict) or engine.get("module") != "flagging.flagger.flag_window" or engine.get("deterministic") is not True or engine.get("version") != version:
            raise ValueError
        if not isinstance(snapshot.get("run_id"), str):
            raise ValueError
        run_id = str(UUID(snapshot["run_id"]))
        cursor = _timestamp(snapshot["as_of_ts"])
        window = snapshot["window"]
        start, end = _timestamp(window["start"]), _timestamp(window["end"])
        duration = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        if cursor != end or not 0 <= duration <= 900:
            raise ValueError
        incidents = snapshot.get("incidents")
        if not isinstance(incidents, list) or len(incidents) > 1000 or any(not isinstance(item, dict) for item in incidents):
            raise ValueError
        _exclude_evaluation_labels(snapshot)
        snapshot_json = _json(snapshot)
        if len(snapshot_json.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
            raise ValueError
        if not isinstance(evidence, dict) or set(evidence) != DATASETS:
            raise ValueError
        digest = hashlib.sha256()
        digest.update(_json({"run_id": run_id, "window_start_ts": start, "as_of_ts": cursor}).encode("utf-8"))
        total_bytes = 0
        for name in sorted(DATASETS):
            records = evidence[name]
            limit = 24 if name == "inventory" else 4344
            if not isinstance(records, list) or len(records) > limit or (name == "inventory" and not records):
                raise ValueError
            canonical = []
            for record in records:
                if not isinstance(record, dict):
                    raise ValueError
                _exclude_evaluation_labels(record)
                normalized = dict(record)
                if name != "inventory":
                    if not isinstance(record.get("run_id"), str) or str(UUID(record["run_id"])) != run_id:
                        raise ValueError
                    point = _timestamp(record["event_ts"])
                    if not datetime.fromisoformat(start) <= datetime.fromisoformat(point) <= datetime.fromisoformat(cursor):
                        raise ValueError
                    normalized.update(run_id=run_id, event_ts=point)
                encoded = _json(normalized).encode("utf-8")
                total_bytes += len(encoded)
                if total_bytes > MAX_EVIDENCE_BYTES:
                    raise ValueError
                canonical.append(encoded)
            digest.update(name.encode("ascii") + b"\0")
            for encoded in sorted(canonical):
                # Length framing makes concatenation unambiguous; duplicate rows remain counted.
                digest.update(str(len(encoded)).encode("ascii") + b":" + encoded)
        return run_id, cursor, version, digest.hexdigest(), len(incidents), snapshot_json
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
        raise DataQueryError("The official detector result failed provenance or bounded-payload validation; nothing was saved.") from None


class DatabricksResultStore:
    def __init__(self, source, *, timeout_seconds=90, poll_interval=1, monotonic=None, sleep=None, now=None):
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 90 or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Persistence requires a positive deadline of at most 90 seconds and a positive polling interval.")
        self.source = source
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._lock = threading.Lock()
        self._table_ready = False

    @property
    def table(self):
        return f"{self.source.settings.catalog}.{self.source.settings.schema}.{TABLE}"

    def _cancel(self, api, statement_id):
        if statement_id:
            try:
                api.cancel_execution(statement_id=statement_id)
            except Exception:
                pass

    def _deadline(self, deadline, api=None, statement_id=None):
        if self._monotonic() >= deadline:
            self._cancel(api, statement_id)
            raise DataQueryTimeout("Saving the official detector result timed out. Cancellation was requested where possible; persistence is not confirmed.")

    def _execute(self, api, statement, parameters, deadline):
        from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem

        statement_id = None
        try:
            self._deadline(deadline)
            response = api.execute_statement(
                statement=statement,
                warehouse_id=self.source.settings.warehouse_id,
                catalog=self.source.settings.catalog,
                schema=self.source.settings.schema,
                parameters=[StatementParameterListItem(name=name, value=value, type=kind) for name, (value, kind) in parameters.items()],
                disposition=Disposition.INLINE, format=Format.JSON_ARRAY, wait_timeout="0s",
            )
            statement_id = response.statement_id
            while True:
                self._deadline(deadline, api, statement_id)
                status = getattr(getattr(response, "status", None), "state", None)
                status = getattr(status, "value", status)
                if status == "SUCCEEDED":
                    return
                if status not in {"PENDING", "RUNNING"} or not statement_id:
                    raise DataQueryError("Databricks did not confirm the official detector write. Check table permissions and SQL query history; no saved result is being reported.")
                self._sleep(min(self.poll_interval, max(0, deadline - self._monotonic())))
                self._deadline(deadline, api, statement_id)
                response = api.get_statement(statement_id=statement_id)
        except DataQueryError:
            raise
        except Exception:
            self._cancel(api, statement_id)
            raise DataQueryError("Databricks could not confirm the official detector write. Check the backend login, warehouse and table permissions.") from None

    def save(self, snapshot, evidence):
        deadline = self._monotonic() + self.timeout_seconds
        run_id, cursor, version, evidence_hash, incident_count, snapshot_json = _prepare(snapshot, evidence)
        generated_at = _timestamp(self._now())
        if not self._lock.acquire(timeout=max(0, deadline - self._monotonic())):
            raise DataQueryTimeout("Saving the official detector result timed out waiting for another write; nothing was submitted.")
        try:
            self._deadline(deadline)
            api = self.source._client().statement_execution
            settings = self.source.settings  # DataSettings validates these identifiers.
            qualified = f"`{settings.catalog}`.`{settings.schema}`.`{TABLE}`"
            if not self._table_ready:
                self._execute(api, f"""CREATE TABLE IF NOT EXISTS {qualified} (
                    run_id STRING NOT NULL, as_of_ts TIMESTAMP NOT NULL,
                    detector_version STRING NOT NULL, evidence_sha256 STRING NOT NULL,
                    incident_count BIGINT NOT NULL, snapshot_json STRING NOT NULL,
                    generated_at TIMESTAMP NOT NULL
                ) USING DELTA""", {}, deadline)
                self._table_ready = True
            parameters = {
                "run_id": (run_id, "STRING"), "as_of_ts": (cursor, "TIMESTAMP"),
                "detector_version": (version, "STRING"), "evidence_sha256": (evidence_hash, "STRING"),
                "incident_count": (str(incident_count), "BIGINT"), "snapshot_json": (snapshot_json, "STRING"),
                "generated_at": (generated_at, "TIMESTAMP"),
            }
            self._execute(api, f"""MERGE INTO {qualified} AS target
                USING (SELECT :run_id AS run_id, :as_of_ts AS as_of_ts,
                    :detector_version AS detector_version, :evidence_sha256 AS evidence_sha256,
                    :incident_count AS incident_count, :snapshot_json AS snapshot_json,
                    :generated_at AS generated_at) AS source
                ON target.run_id = source.run_id AND target.as_of_ts = source.as_of_ts
                    AND target.detector_version = source.detector_version
                    AND target.evidence_sha256 = source.evidence_sha256
                WHEN MATCHED THEN UPDATE SET snapshot_json = source.snapshot_json,
                    incident_count = source.incident_count, generated_at = source.generated_at
                WHEN NOT MATCHED THEN INSERT (run_id, as_of_ts, detector_version,
                    evidence_sha256, incident_count, snapshot_json, generated_at)
                VALUES (source.run_id, source.as_of_ts, source.detector_version,
                    source.evidence_sha256, source.incident_count, source.snapshot_json, source.generated_at)
                """, parameters, deadline)
            return {"status": "saved", "table": self.table, "detector_version": version,
                    "evidence_sha256": evidence_hash, "generated_at": generated_at}
        finally:
            self._lock.release()
