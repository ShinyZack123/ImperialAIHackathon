"""Lakehouse control plane for the synthetic GPU operations fixture.

Creates governed agent-tool functions, gold views over the official detector's
saved assessments, an evaluation scorecard and policy-experiment tables. Every
statement is packaged under databricks/lakehouse/; no SQL is built from request
input. Source evidence rows are never modified or deleted.

Run from databricks/ after the OAuth login in CONNECT.md:
  python -m backend.lakehouse plan           # list the statements; no cloud calls
  python -m backend.lakehouse apply          # create or refresh functions, views and tables
  python -m backend.lakehouse backfill       # assess every replay cursor with the official detector
  python -m backend.lakehouse ground-truth   # load the synthetic answer key into the eval schema
  python -m backend.lakehouse experiments    # load twin policy experiments
  python -m backend.lakehouse governance     # add comments and tags to the evidence tables
  python -m backend.lakehouse status         # funnel, scorecard and policy uplift as JSON

Polling deadlines are checked between SDK calls; a request already in progress can
extend wall time, as in the read client. All data is synthetic.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
from pathlib import Path
import re
import sys
import tempfile
import time

from backend.databricks_data import DataQueryError, DataQueryTimeout, DataSettings, DatabricksData


ROOT = Path(__file__).resolve().parents[1]
LAKEHOUSE_DIR = ROOT / "lakehouse"
EXPERIMENTS_FILE = ROOT / "demo" / "twin_policy_experiments.json"
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
_ALLOWED = re.compile(
    r"\s*CREATE\s+(?:OR\s+REPLACE\s+(?:VIEW|FUNCTION)|TABLE\s+IF\s+NOT\s+EXISTS|SCHEMA\s+IF\s+NOT\s+EXISTS)\s",
    re.IGNORECASE,
)
_FORBIDDEN = re.compile(r"\b(DROP|DELETE|TRUNCATE|OVERWRITE|GRANT|REVOKE)\b", re.IGNORECASE)
SCENARIOS = ("network", "demand", "power", "thermal")
POLICY_KINDS = ("heuristic", "optimizer", "llm_base", "llm_finetuned", "human")
_POLICY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}\Z")
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024
CHUNK_ROWS = 500

RUN_FIELDS = (
    ("scenario", "STRING"), ("seed", "INT"), ("policy", "STRING"), ("policy_kind", "STRING"),
    ("origin", "STRING"), ("twin_schema_version", "STRING"), ("minutes", "INT"),
    ("plan_json", "STRING"), ("constraints_passed", "BOOLEAN"), ("constraint_checks_json", "STRING"),
    ("candidates_considered", "INT"), ("candidates_feasible", "INT"),
    ("training_ranks_moved", "INT"), ("serving_slots_moved", "INT"),
    ("training_samples_per_second", "DOUBLE"), ("training_samples", "BIGINT"),
    ("inference_tokens_per_second", "DOUBLE"), ("average_p95_ms", "DOUBLE"),
    ("slo_met_minutes", "INT"), ("energy_kwh", "DOUBLE"), ("cost_gbp", "DOUBLE"),
    ("samples_per_kwh", "DOUBLE"),
)
TIMELINE_FIELDS = (
    ("scenario", "STRING"), ("seed", "INT"), ("policy", "STRING"), ("minute", "INT"),
    ("samples_per_second", "DOUBLE"), ("p95_ms", "DOUBLE"), ("tokens_per_second", "DOUBLE"),
    ("power_kw", "DOUBLE"), ("price_per_kwh", "DOUBLE"), ("energy_kwh", "DOUBLE"),
)
RUN_KEYS = ("scenario", "seed", "policy")
TIMELINE_KEYS = ("scenario", "seed", "policy", "minute")
_NULLABLE = {"plan_json", "constraints_passed", "constraint_checks_json", "candidates_considered",
             "candidates_feasible", "training_ranks_moved", "serving_slots_moved", "average_p95_ms", "p95_ms"}

# Metadata only. Applied by the explicit `governance` command, never by `apply`.
TABLE_DOCS = {
    "inventory": ("Synthetic GPU inventory: one row per device with its rack, server, cooling zone and illustrative limits.", "bronze"),
    "telemetry": ("Synthetic GPU telemetry sampled every five seconds per replay run. NULL metrics with heartbeat_ok = false mean unknown, not zero.", "bronze"),
    "workloads": ("Synthetic scheduler-side job state and cumulative progress per GPU and replay run.", "bronze"),
    "cooling": ("Synthetic cooling-zone inlet temperature and flow (percent of nominal) per replay run.", "bronze"),
    "official_detector_results": ("Audit record of every official deterministic-detector assessment, keyed by run, cursor, detector fingerprint and evidence hash.", "silver"),
}
COLUMN_DOCS = {
    "inventory": {
        "gpu_id": "Stable device ID", "vendor": "NVIDIA, AMD or Intel label on a synthetic device",
        "power_limit_w": "Illustrative configured power limit in watts; not a manufacturer specification",
        "temperature_limit_c": "Illustrative configured temperature limit in Celsius; not a manufacturer specification",
        "cooling_zone_id": "Cooling zone that this GPU depends on",
    },
    "telemetry": {
        "run_id": "Independent replay run; never join runs because their timestamps match",
        "event_ts": "Sample time in UTC",
        "power_w": "Instantaneous GPU power in watts; NULL when the collector had no sample",
        "temp_c": "GPU temperature in Celsius; NULL when the collector had no sample",
        "throttled": "Simulated hardware throttling signal",
        "ecc_uncorrected_total": "Cumulative uncorrected error counter within the run; compare changes, not levels",
        "heartbeat_ok": "Whether the collector obtained a GPU sample; false means the metrics are unknown",
    },
    "workloads": {
        "progress_units": "Cumulative synthetic work counter per job; not tokens or training steps",
        "job_state": "idle or running; an idle device has no job",
    },
    "cooling": {
        "flow_pct": "Coolant flow as a percentage of configured nominal flow; not litres per minute",
        "inlet_temp_c": "Coolant inlet temperature in Celsius",
    },
    "official_detector_results": {
        "detector_version": "SHA-256 fingerprint of the flagger, its configuration and the connector adapter",
        "evidence_sha256": "Hash of the bounded source evidence this assessment was computed from",
        "snapshot_json": "Full official snapshot as returned to the website",
    },
}


class LakehouseError(DataQueryError):
    """A packaged lakehouse statement or an input file was rejected."""


def eval_schema(settings: DataSettings) -> str:
    name = f"{settings.schema}_eval"
    if not _IDENTIFIER.fullmatch(name):
        raise ValueError("The evaluation schema name is too long for this schema.")
    return name


def _is_single_statement(sql: str) -> bool:
    """True when no statement terminator appears outside strings, identifiers or comments."""
    quote, index, length = None, 0, len(sql)
    while index < length:
        char = sql[index]
        if quote:
            if char == quote:
                if quote == "'" and sql[index + 1:index + 2] == "'":
                    index += 1
                else:
                    quote = None
        elif char in "'`":
            quote = char
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end
        elif char == ";":
            return False
        index += 1
    return quote is None


def _without_literals(sql: str) -> str:
    """Drop comments and string literals so keyword checks see only SQL structure."""
    result, quote, index, length = [], None, 0, len(sql)
    while index < length:
        char = sql[index]
        if quote:
            if char == quote:
                if quote == "'" and sql[index + 1:index + 2] == "'":
                    index += 1
                else:
                    quote = None
        elif char in "'`":
            quote = char
        elif sql.startswith("--", index):
            end = sql.find("\n", index)
            index = length if end == -1 else end
            continue
        else:
            result.append(char)
        index += 1
    return "".join(result)


def render(text: str, settings: DataSettings) -> str:
    """Substitute the two validated schema placeholders; reject anything else."""
    evidence = f"`{settings.catalog}`.`{settings.schema}`"
    evaluation = f"`{settings.catalog}`.`{eval_schema(settings)}`"
    rendered = text.replace("${evidence}", evidence).replace("${eval}", evaluation).strip()
    structure = _without_literals(rendered)
    if "${" in rendered or not _is_single_statement(rendered):
        raise LakehouseError("A packaged lakehouse statement has an unknown placeholder or more than one statement.")
    if not _ALLOWED.match(structure) or _FORBIDDEN.search(structure):
        raise LakehouseError("Packaged lakehouse statements may only create or refresh views, functions, tables and the evaluation schema.")
    return rendered


def packaged_statements(settings: DataSettings, directory: Path = LAKEHOUSE_DIR) -> list[tuple[str, str]]:
    files = sorted(Path(directory).glob("*.sql"))
    if not files:
        raise LakehouseError("No packaged lakehouse statements were found.")
    return [(path.stem, render(path.read_text(encoding="utf-8"), settings)) for path in files]


def _quoted(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "''") + "'"


def governance_statements(settings: DataSettings) -> list[tuple[str, str]]:
    """Comments and tags for existing tables. Metadata only; no rows change."""
    prefix = f"`{settings.catalog}`.`{settings.schema}`"
    result = []
    for table, (comment, layer) in TABLE_DOCS.items():
        result.append((f"comment/{table}", f"COMMENT ON TABLE {prefix}.`{table}` IS {_quoted(comment)}"))
        result.append((f"tags/{table}", f"ALTER TABLE {prefix}.`{table}` SET TAGS ('data_origin' = 'synthetic', 'medallion_layer' = {_quoted(layer)})"))
        for column, text in COLUMN_DOCS.get(table, {}).items():
            if not _IDENTIFIER.fullmatch(column):
                raise ValueError("Invalid documented column name.")
            result.append((f"comment/{table}.{column}", f"ALTER TABLE {prefix}.`{table}` ALTER COLUMN `{column}` COMMENT {_quoted(text)}"))
    return result


def _utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if not isinstance(value, datetime):
        raise ValueError("Expected a timestamp.")
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def plan_cursors(start, end, step_seconds: int) -> list[datetime]:
    """Evenly spaced replay cursors after the first sample, always ending on the last sample."""
    if isinstance(step_seconds, bool) or not isinstance(step_seconds, int) or not 5 <= step_seconds <= 900 or step_seconds % 5:
        raise ValueError("step_seconds must be a multiple of 5 between 5 and 900.")
    first, last = _utc(start), _utc(end)
    if last < first:
        raise ValueError("A replay cannot end before it starts.")
    cursors, point = [], first + timedelta(seconds=step_seconds)
    while point < last:
        cursors.append(point)
        point += timedelta(seconds=step_seconds)
    cursors.append(last)
    return cursors


def _load_generator():
    """Import databricks/generate.py by path; flagging/ has a different generate module."""
    path = ROOT / "generate.py"
    spec = importlib.util.spec_from_file_location("gpu_ops_fixture_generator", path)
    if spec is None or spec.loader is None:
        raise LakehouseError("The fixture generator is unavailable.")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def ground_truth_rows(seed: int = 42) -> list[dict]:
    """Regenerate the deterministic answer key locally. Nothing is read from Databricks."""
    generator = _load_generator()
    with tempfile.TemporaryDirectory(prefix="gpu-ops-truth-") as folder:
        generator.generate_dataset(Path(folder) / "dataset", seed=seed, scenario="all")
        labels = json.loads((Path(folder) / "dataset" / "evaluation" / "ground_truth.json").read_text(encoding="utf-8"))
    rows = []
    for run in labels["runs"]:
        expected = run["expected"]
        affected = list(expected.get("affected_gpu_ids", []))
        should_flag = expected.get("should_raise_fault", True) is not False
        root = expected.get("cooling_zone_id") or (affected[0] if len(affected) == 1 else None)
        if should_flag and not root:
            raise LakehouseError("A fault scenario has no single expected root entity.")
        rows.append({
            "run_id": run["run_id"], "scenario": run["scenario"], "expected_cause": expected["cause"],
            "expected_flag": should_flag, "expected_root_entity": root if should_flag else None,
            "affected_gpu_ids": affected, "event_start_ts": run["event_start"],
            "scheduled_recovery_start_ts": run["scheduled_recovery_start"],
            "fully_recovered_by_ts": run["fully_recovered_by"],
        })
    return rows


def _number(value, name, integer=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise LakehouseError(f"Experiment field {name} must be a finite number.")
    if integer and not isinstance(value, int):
        raise LakehouseError(f"Experiment field {name} must be an integer.")
    return value


def validate_experiments(document) -> tuple[list[dict], list[dict]]:
    """Check an exported twin experiment file before any row reaches Databricks."""
    if not isinstance(document, dict) or document.get("origin") != "simulated" or not isinstance(document.get("runs"), list) or not isinstance(document.get("timeline"), list):
        raise LakehouseError("Expected a simulated experiment export with runs and timeline.")
    runs, timeline, seen, lengths = [], [], set(), {}
    for source in document["runs"]:
        if not isinstance(source, dict) or set(source) != {name for name, _ in RUN_FIELDS}:
            raise LakehouseError("An experiment run has unexpected fields.")
        if source["scenario"] not in SCENARIOS or source["policy_kind"] not in POLICY_KINDS or source["origin"] != "simulated":
            raise LakehouseError("An experiment run has an unknown scenario, policy kind or origin.")
        if not isinstance(source["policy"], str) or not _POLICY.fullmatch(source["policy"]) or not isinstance(source["twin_schema_version"], str):
            raise LakehouseError("An experiment run has an invalid policy name or schema version.")
        if not 0 <= _number(source["seed"], "seed", True) <= 9999 or not 1 <= _number(source["minutes"], "minutes", True) <= 1440:
            raise LakehouseError("An experiment run has an out-of-range seed or duration.")
        for name, kind in RUN_FIELDS:
            value = source[name]
            if value is None:
                if name not in _NULLABLE:
                    raise LakehouseError(f"Experiment field {name} is required.")
            elif kind in {"INT", "BIGINT"}:
                _number(value, name, True)
            elif kind == "DOUBLE":
                _number(value, name)
            elif kind == "BOOLEAN" and not isinstance(value, bool):
                raise LakehouseError(f"Experiment field {name} must be boolean.")
            elif kind == "STRING" and not isinstance(value, str):
                raise LakehouseError(f"Experiment field {name} must be text.")
        for name in ("plan_json", "constraint_checks_json"):
            if source[name] is not None:
                try:
                    json.loads(source[name])
                except ValueError:
                    raise LakehouseError(f"Experiment field {name} must contain JSON.") from None
        key = tuple(source[name] for name in RUN_KEYS)
        if key in seen:
            raise LakehouseError("Duplicate experiment run.")
        seen.add(key)
        lengths[key] = source["minutes"]
        runs.append(dict(source))
    points = set()
    for source in document["timeline"]:
        if not isinstance(source, dict) or set(source) != {name for name, _ in TIMELINE_FIELDS}:
            raise LakehouseError("A timeline point has unexpected fields.")
        key = tuple(source[name] for name in RUN_KEYS)
        if key not in lengths or not 0 <= _number(source["minute"], "minute", True) < lengths[key]:
            raise LakehouseError("A timeline point does not belong to an exported run.")
        for name, kind in TIMELINE_FIELDS[4:]:
            if source[name] is None:
                if name not in _NULLABLE:
                    raise LakehouseError(f"Timeline field {name} is required.")
            else:
                _number(source[name], name)
        point = key + (source["minute"],)
        if point in points:
            raise LakehouseError("Duplicate timeline point.")
        points.add(point)
        timeline.append(dict(source))
    if not runs or len(timeline) != sum(lengths.values()):
        raise LakehouseError("The experiment export is empty or its timeline is incomplete.")
    return runs, timeline


def _merge_statement(table: str, fields, keys, stamp: bool) -> str:
    schema = ", ".join(f"{name}: {kind}" for name, kind in fields)
    names = [name for name, _ in fields]
    extra = ", current_timestamp() AS loaded_at" if stamp else ""
    columns = names + (["loaded_at"] if stamp else [])
    condition = " AND ".join(f"target.{key} = source.{key}" for key in keys)
    updates = ", ".join(f"{name} = source.{name}" for name in columns if name not in keys)
    return (
        f"MERGE INTO {table} AS target USING ("
        f"SELECT item.*{extra} FROM (SELECT explode(from_json(:payload, 'ARRAY<STRUCT<{schema}>>')) AS item)"
        f") AS source ON {condition} "
        f"WHEN MATCHED THEN UPDATE SET {updates} "
        f"WHEN NOT MATCHED THEN INSERT ({', '.join(columns)}) VALUES ({', '.join('source.' + name for name in columns)})"
    )


class Lakehouse:
    def __init__(self, source: DatabricksData, *, timeout_seconds=180, poll_interval=1, monotonic=None, sleep=None):
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0 or not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("Timeouts must be finite and positive.")
        self.source = source
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep

    @property
    def settings(self) -> DataSettings:
        return self.source.settings

    @property
    def evidence(self) -> str:
        return f"`{self.settings.catalog}`.`{self.settings.schema}`"

    @property
    def evaluation(self) -> str:
        return f"`{self.settings.catalog}`.`{eval_schema(self.settings)}`"

    def execute(self, statement: str, parameters: dict | None = None, *, fetch: bool = False) -> list[dict]:
        """Run one statement on the SQL warehouse and wait for a confirmed result."""
        from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem

        api = self.source._client().statement_execution
        deadline = self._monotonic() + self.timeout_seconds
        statement_id = None

        def check_deadline():
            if self._monotonic() >= deadline:
                if statement_id:
                    try:
                        api.cancel_execution(statement_id=statement_id)
                    except Exception:
                        pass
                raise DataQueryTimeout("A lakehouse statement timed out; cancellation was requested. The warehouse may still be starting.")

        try:
            response = api.execute_statement(
                statement=statement, warehouse_id=self.settings.warehouse_id,
                catalog=self.settings.catalog, schema=self.settings.schema,
                parameters=[StatementParameterListItem(name=name, value=value, type=kind) for name, (value, kind) in (parameters or {}).items()],
                disposition=Disposition.INLINE, format=Format.JSON_ARRAY, wait_timeout="0s",
            )
            statement_id = response.statement_id
            while True:
                check_deadline()
                status = getattr(response, "status", None)
                state = getattr(getattr(status, "state", None), "value", getattr(status, "state", None))
                if state == "SUCCEEDED":
                    if not fetch:
                        return []
                    # The read client checks deadlines on its own clock; hand it the remaining time.
                    remaining = max(0.0, deadline - self._monotonic())
                    return self.source._read_result(response, api, self.source._monotonic() + remaining)
                if state not in {"PENDING", "RUNNING"} or not statement_id:
                    detail = getattr(getattr(status, "error", None), "message", None)
                    # SQL error text names the object and clause; URLs are removed before it is shown.
                    detail = re.sub(r"https?://\S+", "<url>", detail)[:600] if isinstance(detail, str) else "no detail returned"
                    raise LakehouseError(f"Databricks did not complete the statement ({str(state).lower()}): {detail}")
                self._sleep(min(self.poll_interval, max(0, deadline - self._monotonic())))
                check_deadline()
                response = api.get_statement(statement_id=statement_id)
        except DataQueryError:
            raise
        except Exception:
            if statement_id:
                try:
                    api.cancel_execution(statement_id=statement_id)
                except Exception:
                    pass
            raise LakehouseError("The Databricks request failed. Check the backend login, warehouse and permissions.") from None

    def apply(self, emit=print) -> list[str]:
        applied = []
        for name, statement in packaged_statements(self.settings):
            self.execute(statement)
            applied.append(name)
            emit(f"applied {name}")
        return applied

    def governance(self, emit=print) -> int:
        statements = governance_statements(self.settings)
        for name, statement in statements:
            self.execute(statement)
            emit(f"applied {name}")
        return len(statements)

    def assessed_cursors(self) -> dict[tuple[str, datetime], set[str]]:
        """Every saved (run, cursor) with the detector versions that assessed it."""
        rows = self.execute(
            f"SELECT DISTINCT run_id, as_of_ts, detector_version FROM {self.evidence}.`official_detector_results` LIMIT 1000",
            fetch=True)
        result: dict[tuple[str, datetime], set[str]] = {}
        for row in rows:
            result.setdefault((row["run_id"], _utc(row["as_of_ts"])), set()).add(row["detector_version"])
        return result

    def backfill(self, step_seconds: int = 30, run_ids=None, detector=None, emit=print) -> dict:
        """Assess every planned cursor with the official detector; each result is saved by MERGE.

        Cursors saved by an older detector version are assessed again, so the gold
        views describe one detector version instead of a mixture.
        """
        from backend.official_detection import OfficialDetector
        from backend.result_store import detector_version

        detector = detector or OfficialDetector(self.source)
        runs = [run for run in self.source.list_runs() if not run_ids or run["run_id"] in run_ids]
        if not runs:
            raise LakehouseError("No matching replay runs were found.")
        current = detector_version()
        known = self.assessed_cursors()
        done = {key for key, versions in known.items() if current in versions}
        saved = skipped = 0
        for run in runs:
            start, end = _utc(run["start_ts"]), _utc(run["end_ts"])
            earlier = {cursor for (run_id, cursor) in known if run_id == run["run_id"] and start <= cursor <= end}
            for cursor in sorted(set(plan_cursors(start, end, step_seconds)) | earlier):
                if (run["run_id"], cursor) in done:
                    skipped += 1
                    continue
                snapshot = detector.snapshot(run["run_id"], cursor)
                saved += 1
                emit(f"saved {run['run_id'][:8]} {_iso(cursor)} incidents={len(snapshot['incidents'])}")
        return {"saved": saved, "already_assessed": skipped, "step_seconds": step_seconds}

    def load_ground_truth(self, seed: int = 42) -> int:
        rows = ground_truth_rows(seed)
        known = {run["run_id"] for run in self.source.list_runs()}
        if {row["run_id"] for row in rows} != known:
            raise LakehouseError("The workspace replays do not match this generator seed; the answer key was not loaded.")
        statement = (
            f"MERGE INTO {self.evaluation}.`ground_truth` AS target USING (SELECT :run_id AS run_id, :scenario AS scenario, "
            ":expected_cause AS expected_cause, CAST(:expected_flag AS BOOLEAN) AS expected_flag, :expected_root_entity AS expected_root_entity, "
            "from_json(:affected_gpu_ids, 'ARRAY<STRING>') AS affected_gpu_ids, :event_start_ts AS event_start_ts, "
            ":scheduled_recovery_start_ts AS scheduled_recovery_start_ts, :fully_recovered_by_ts AS fully_recovered_by_ts, "
            "current_timestamp() AS loaded_at) AS source ON target.run_id = source.run_id "
            "WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *"
        )
        for row in rows:
            self.execute(statement, {
                "run_id": (row["run_id"], "STRING"), "scenario": (row["scenario"], "STRING"),
                "expected_cause": (row["expected_cause"], "STRING"),
                "expected_flag": ("true" if row["expected_flag"] else "false", "STRING"),
                "expected_root_entity": (row["expected_root_entity"], "STRING"),
                "affected_gpu_ids": (json.dumps(row["affected_gpu_ids"]), "STRING"),
                "event_start_ts": (row["event_start_ts"], "TIMESTAMP"),
                "scheduled_recovery_start_ts": (row["scheduled_recovery_start_ts"], "TIMESTAMP"),
                "fully_recovered_by_ts": (row["fully_recovered_by_ts"], "TIMESTAMP"),
            })
        return len(rows)

    def load_experiments(self, path: Path = EXPERIMENTS_FILE, emit=print) -> dict:
        try:
            raw = Path(path).read_bytes()
            if len(raw) > 32 * 1024 * 1024:
                raise ValueError
            document = json.loads(raw)
        except (OSError, ValueError):
            raise LakehouseError("The experiment export could not be read as JSON.") from None
        runs, timeline = validate_experiments(document)
        for table, rows, fields, keys, stamp in (
            ("policy_runs", runs, RUN_FIELDS, RUN_KEYS, True),
            ("policy_timeline", timeline, TIMELINE_FIELDS, TIMELINE_KEYS, False),
        ):
            statement = _merge_statement(f"{self.evidence}.`{table}`", fields, keys, stamp)
            for offset in range(0, len(rows), CHUNK_ROWS):
                payload = json.dumps(rows[offset:offset + CHUNK_ROWS], separators=(",", ":"), allow_nan=False)
                if len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
                    raise LakehouseError("An experiment batch exceeds the bounded payload size.")
                self.execute(statement, {"payload": (payload, "STRING")})
                emit(f"merged {table} rows {offset + 1}-{min(offset + CHUNK_ROWS, len(rows))}")
        return {"policy_runs": len(runs), "policy_timeline": len(timeline)}

    def status(self) -> dict:
        def rows(sql):
            return self.execute(sql, fetch=True)
        return {
            "synthetic": True,
            "escalation_funnel": rows(f"SELECT * FROM {self.evidence}.`escalation_funnel` ORDER BY run_id LIMIT 100"),
            "detection_scorecard": rows(
                f"SELECT scenario, expected_cause, expected_root_entity, cursors_assessed, first_flag_cursor, "
                f"seconds_from_onset_to_first_flag, first_root_named_cursor, seconds_from_onset_to_root_named, "
                f"distinct_incident_ids, flagged_root_entities, verdict "
                f"FROM {self.evaluation}.`detection_scorecard` ORDER BY scenario LIMIT 100"),
            "policy_uplift": rows(f"SELECT * FROM {self.evidence}.`policy_uplift` ORDER BY scenario, policy LIMIT 100"),
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Create and fill the lakehouse control-plane objects for the synthetic fixture.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("plan", help="List packaged statements without contacting Databricks")
    commands.add_parser("apply", help="Create or refresh functions, views and tables")
    commands.add_parser("governance", help="Add comments and tags to the evidence tables (metadata only)")
    backfill = commands.add_parser("backfill", help="Assess every replay cursor with the official detector")
    backfill.add_argument("--step-seconds", type=int, default=30)
    backfill.add_argument("--run-id", action="append")
    truth = commands.add_parser("ground-truth", help="Load the synthetic answer key into the evaluation schema")
    truth.add_argument("--seed", type=int, default=42)
    experiments = commands.add_parser("experiments", help="Load twin policy experiments")
    experiments.add_argument("--file", type=Path, default=EXPERIMENTS_FILE)
    commands.add_parser("status", help="Print the funnel, scorecard and policy uplift")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env", override=False)
        settings = DataSettings.from_env()
        if args.command == "plan":
            names = [name for name, _ in packaged_statements(settings)]
            print(json.dumps({"statements": names, "governance_statements": len(governance_statements(settings)),
                              "evidence_schema": settings.schema, "evaluation_schema": eval_schema(settings)}, indent=2))
            return 0
        lakehouse = Lakehouse(DatabricksData(settings))
        if args.command == "apply":
            print(json.dumps({"applied": len(lakehouse.apply())}))
        elif args.command == "governance":
            print(json.dumps({"applied": lakehouse.governance()}))
        elif args.command == "backfill":
            print(json.dumps(lakehouse.backfill(args.step_seconds, args.run_id)))
        elif args.command == "ground-truth":
            print(json.dumps({"ground_truth_rows": lakehouse.load_ground_truth(args.seed)}))
        elif args.command == "experiments":
            print(json.dumps(lakehouse.load_experiments(args.file)))
        else:
            print(json.dumps(lakehouse.status(), indent=2, allow_nan=False))
        return 0
    except (DataQueryError, ValueError) as error:
        print(f"Lakehouse command failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
