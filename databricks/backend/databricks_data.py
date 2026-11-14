"""Bound, read-only Databricks queries for the website and model services.

No workspace is contacted at import time. Authentication uses the local
Databricks SDK profile/environment; credentials never enter query results.
"""

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import time
from typing import Any, Callable, Mapping


SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
MAX_ROWS = 1000
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,254}\Z")
_WAREHOUSE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
_QUERIES = {"runs", "overview", "rack_context", "gpu_history", "workload_history", "health_alerts", "fleet_evidence", "official_results"}


class DataQueryError(RuntimeError):
    """The data source could not return a complete, valid result."""


class DataQueryTimeout(DataQueryError):
    """The query exceeded the local polling deadline."""


@dataclass(frozen=True)
class DataSettings:
    warehouse_id: str
    catalog: str
    schema: str = "gpu_ops_hackathon"
    profile: str = "gpu-ops"
    host: str | None = None

    def __post_init__(self):
        if not isinstance(self.warehouse_id, str) or not _WAREHOUSE_ID.fullmatch(self.warehouse_id):
            raise ValueError("DATABRICKS_SQL_WAREHOUSE_ID must be a nonblank warehouse ID, not an HTTP path.")
        for name, value in (("DATABRICKS_CATALOG", self.catalog), ("DATABRICKS_SCHEMA", self.schema)):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ValueError(f"{name} must use letters, digits and underscores and not start with a digit.")
        if not isinstance(self.profile, str) or not self.profile.strip():
            raise ValueError("DATABRICKS_CONFIG_PROFILE must be nonblank.")

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None):
        env = os.environ if environ is None else environ
        missing = [key for key in ("DATABRICKS_SQL_WAREHOUSE_ID", "DATABRICKS_CATALOG") if not env.get(key)]
        if missing:
            raise ValueError("Missing configuration: " + ", ".join(missing))
        return cls(
            warehouse_id=env["DATABRICKS_SQL_WAREHOUSE_ID"],
            catalog=env["DATABRICKS_CATALOG"],
            schema=env.get("DATABRICKS_SCHEMA", "gpu_ops_hackathon"),
            profile=env.get("DATABRICKS_CONFIG_PROFILE", "gpu-ops"),
            host=env.get("DATABRICKS_HOST") or None,
        )


def _enum_value(value):
    return getattr(value, "value", value)


def _reject_json_constant(value):
    raise ValueError("Non-finite JSON value")


def _timestamp(value: str | datetime) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")) if isinstance(value, str) else value
        if not isinstance(parsed, datetime) or parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (ValueError, TypeError, OverflowError):
        raise ValueError("Query timestamps must be ISO 8601 timestamps with a timezone, such as 2026-11-14T09:05:00Z.") from None


def _decode(value: Any, column_type: str):
    """JSON_ARRAY encodes non-null values as strings; decode only known types."""
    if value is None:
        return None
    try:
        if column_type in {"BYTE", "SHORT", "INT", "LONG", "TINYINT", "SMALLINT", "INTEGER", "BIGINT"}:
            if isinstance(value, bool) or not re.fullmatch(r"[+-]?\d+", str(value)):
                raise ValueError
            return int(value)
        if column_type in {"FLOAT", "DOUBLE", "DECIMAL"}:
            if isinstance(value, bool):
                raise ValueError
            number = float(value)
            if not math.isfinite(number):
                raise ValueError
            return number
        if column_type == "BOOLEAN":
            if isinstance(value, bool):
                return value
            if isinstance(value, str) and value.lower() in {"true", "false"}:
                return value.lower() == "true"
            raise ValueError
        if column_type in {"STRING", "CHAR", "VARCHAR", "TIMESTAMP", "TIMESTAMP_NTZ", "DATE"}:
            if not isinstance(value, str):
                raise ValueError
            return value
    except (ValueError, TypeError, OverflowError):
        raise DataQueryError(f"Databricks returned an invalid {column_type} value.") from None
    raise DataQueryError(f"Databricks returned an unsupported column type: {column_type}.")


class DatabricksData:
    def __init__(
        self,
        settings: DataSettings,
        workspace_client=None,
        poll_interval: float = 1,
        timeout_seconds: float = 120,
        monotonic: Callable[[], float] | None = None,
        sleep: Callable[[float], None] | None = None,
    ):
        if not math.isfinite(poll_interval) or poll_interval <= 0:
            raise ValueError("poll_interval must be finite and positive.")
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be finite and positive.")
        self.settings = settings
        self._workspace_client = workspace_client
        self.poll_interval = poll_interval
        self.timeout_seconds = timeout_seconds
        self._monotonic = monotonic or time.monotonic
        self._sleep = sleep or time.sleep

    def _client(self):
        if self._workspace_client is None:
            try:
                from databricks.sdk import WorkspaceClient
                from databricks.sdk.core import Config

                kwargs = {"profile": self.settings.profile, "http_timeout_seconds": 30, "retry_timeout_seconds": 30}
                if self.settings.host:
                    kwargs["host"] = self.settings.host
                self._workspace_client = WorkspaceClient(config=Config(**kwargs))
            except Exception:
                raise DataQueryError("Databricks authentication/configuration failed. Check the server's OAuth profile and workspace host.") from None
        return self._workspace_client

    def list_runs(self) -> list[dict]:
        return self._query("runs", {})

    def official_results(self, run_id: str, as_of_ts: str | datetime) -> list[dict]:
        """Read previously persisted official results for audit or model handoff."""
        return self._query("official_results", {"run_id": (run_id, "STRING"), "as_of_ts": (_timestamp(as_of_ts), "TIMESTAMP")})

    def fleet_evidence(self, run_id: str, as_of_ts: str | datetime, window_start_ts: str | datetime) -> dict[str, list[dict]]:
        """One bounded SQL request for the detector, preserving each source's IDs/units."""
        start, end = _timestamp(window_start_ts), _timestamp(as_of_ts)
        duration = (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds()
        if not 0 <= duration <= 900:
            raise ValueError("Fleet evidence requires an ordered window of at most 15 minutes.")
        rows = self._query("fleet_evidence", {"run_id": (run_id, "STRING"), "as_of_ts": (end, "TIMESTAMP"), "window_start_ts": (start, "TIMESTAMP")})
        limits = {"inventory": 24, "telemetry": 4344, "workloads": 4344, "cooling": 4344}
        result = {}
        for row in rows:
            name, count, payload = row.get("dataset"), row.get("row_count"), row.get("payload")
            if name not in limits or name in result or not isinstance(count, int) or isinstance(count, bool) or not 0 <= count <= limits[name]:
                raise DataQueryError("Databricks fleet evidence is incomplete or exceeds the bounded demo limits.")
            if not isinstance(payload, str) or len(payload.encode("utf-8")) > 8 * 1024 * 1024:
                raise DataQueryError("Databricks fleet evidence exceeds the bounded payload limit.")
            try:
                values = json.loads(payload, parse_constant=_reject_json_constant)
            except (ValueError, TypeError):
                raise DataQueryError("Databricks returned invalid fleet evidence JSON.") from None
            if not isinstance(values, list) or len(values) != count or any(not isinstance(value, dict) for value in values):
                raise DataQueryError("Databricks returned incomplete fleet evidence records.")
            result[name] = values
        if set(result) != set(limits) or not result["inventory"]:
            raise DataQueryError("Databricks returned no inventory or an incomplete set of evidence streams.")
        return result

    def overview(self, run_id: str, as_of_ts: str | datetime) -> list[dict]:
        return self._query("overview", {"run_id": (run_id, "STRING"), "as_of_ts": (_timestamp(as_of_ts), "TIMESTAMP")})

    def health_alerts(self, run_id: str, as_of_ts: str | datetime) -> list[dict]:
        """All baseline evaluations at this exact replay cursor, including clear/unknown."""
        return self._query("health_alerts", {"run_id": (run_id, "STRING"), "as_of_ts": (_timestamp(as_of_ts), "TIMESTAMP")})

    def rack_context(self, run_id: str, rack_id: str, as_of_ts: str | datetime) -> list[dict]:
        return self._query("rack_context", {
            "run_id": (run_id, "STRING"), "rack_id": (rack_id, "STRING"),
            "as_of_ts": (_timestamp(as_of_ts), "TIMESTAMP"),
        })

    def gpu_history(self, run_id: str, gpu_id: str, window_start_ts: str | datetime, as_of_ts: str | datetime) -> list[dict]:
        return self._history("gpu_history", run_id, gpu_id, window_start_ts, as_of_ts)

    def workload_history(self, run_id: str, gpu_id: str, window_start_ts: str | datetime, as_of_ts: str | datetime) -> list[dict]:
        return self._history("workload_history", run_id, gpu_id, window_start_ts, as_of_ts)

    def _history(self, query, run_id, gpu_id, window_start_ts, as_of_ts):
        start, end = _timestamp(window_start_ts), _timestamp(as_of_ts)
        if datetime.fromisoformat(start) > datetime.fromisoformat(end):
            raise ValueError("window_start_ts must be no later than as_of_ts.")
        return self._query(query, {
            "run_id": (run_id, "STRING"), "gpu_id": (gpu_id, "STRING"),
            "window_start_ts": (start, "TIMESTAMP"), "as_of_ts": (end, "TIMESTAMP"),
        })

    def _cancel(self, api, statement_id):
        if statement_id:
            try:
                api.cancel_execution(statement_id=statement_id)
            except Exception:
                pass  # Best effort: never imply that receipt guarantees cancellation.

    def _deadline(self, deadline, api, statement_id):
        if self._monotonic() >= deadline:
            self._cancel(api, statement_id)
            raise DataQueryTimeout("Databricks query timed out; a cancellation was requested. The warehouse may still be starting.")

    def _query(self, name: str, parameters: dict[str, tuple[str, str]]) -> list[dict]:
        if name not in _QUERIES:
            raise ValueError("Only the packaged read-only queries are available.")
        for value, _ in parameters.values():
            if not isinstance(value, str) or not value:
                raise ValueError("Query parameters must be nonempty strings.")
        try:
            from databricks.sdk.service.sql import Disposition, Format, StatementParameterListItem
        except ImportError:
            raise DataQueryError("Install the backend requirements to enable Databricks access.") from None
        api = self._client().statement_execution
        deadline = self._monotonic() + self.timeout_seconds
        statement_id = None
        try:
            response = api.execute_statement(
                statement=(SQL_DIR / f"{name}.sql").read_text(encoding="utf-8"),
                warehouse_id=self.settings.warehouse_id,
                catalog=self.settings.catalog,
                schema=self.settings.schema,
                parameters=[StatementParameterListItem(name=key, value=value, type=kind) for key, (value, kind) in parameters.items()],
                disposition=Disposition.INLINE,
                format=Format.JSON_ARRAY,
                wait_timeout="0s",
                row_limit=MAX_ROWS,
            )
            statement_id = response.statement_id
            while True:
                self._deadline(deadline, api, statement_id)
                state = _enum_value(getattr(response.status, "state", None))
                if state == "SUCCEEDED":
                    return self._read_result(response, api, deadline)
                if state not in {"PENDING", "RUNNING"}:
                    if state in {"FAILED", "CANCELED", "CLOSED"}:
                        raise DataQueryError(f"Databricks statement {state.lower()}. Check the warehouse, catalog/schema, table access and query history in Databricks.")
                    raise DataQueryError("Databricks returned an unrecognized statement state.")
                if not statement_id:
                    raise DataQueryError("Databricks did not return an ID for the pending statement.")
                self._sleep(min(self.poll_interval, max(0, deadline - self._monotonic())))
                self._deadline(deadline, api, statement_id)
                response = api.get_statement(statement_id=statement_id)
        except DataQueryError:
            raise
        except Exception:
            self._cancel(api, statement_id)
            raise DataQueryError("Databricks request failed. Check the server's OAuth login, warehouse access and network connection.") from None

    def _read_result(self, response, api, deadline) -> list[dict]:
        manifest = response.manifest
        if manifest is None or manifest.schema is None or not manifest.schema.columns:
            raise DataQueryError("Databricks returned no result schema.")
        if manifest.truncated:
            raise DataQueryError("Databricks truncated the result; partial evidence will not be returned.")
        if _enum_value(manifest.format) not in {None, "JSON_ARRAY"}:
            raise DataQueryError("Databricks returned an unexpected result format.")
        if manifest.total_row_count is not None and manifest.total_row_count > MAX_ROWS:
            raise DataQueryError("Databricks result exceeds the 1000-row safety limit.")
        columns = manifest.schema.columns
        if any(c.position is not None for c in columns):
            if sorted(c.position for c in columns if c.position is not None) != list(range(len(columns))):
                raise DataQueryError("Databricks returned invalid result column positions.")
            columns = sorted(columns, key=lambda c: c.position)
        names = [c.name for c in columns]
        if any(not name for name in names) or len(set(names)) != len(names):
            raise DataQueryError("Databricks returned missing or duplicate column names.")
        rows = []
        result = response.result
        if result is None and manifest.total_row_count != 0:
            raise DataQueryError("Databricks returned no result data.")
        seen = set()
        while result is not None:
            self._deadline(deadline, api, response.statement_id)
            index = result.chunk_index if result.chunk_index is not None else 0
            if index in seen or result.external_links:
                raise DataQueryError("Databricks returned unexpected result pagination.")
            seen.add(index)
            batch = result.data_array or []
            if len(rows) + len(batch) > MAX_ROWS:
                raise DataQueryError("Databricks result exceeds the 1000-row safety limit.")
            if result.row_count is not None and result.row_count != len(batch):
                raise DataQueryError("Databricks returned an incomplete result chunk.")
            if result.row_offset is not None and result.row_offset != len(rows):
                raise DataQueryError("Databricks returned an out-of-order result chunk.")
            for values in batch:
                if len(values) != len(columns):
                    raise DataQueryError("Databricks returned a row with the wrong column count.")
                rows.append({column.name: _decode(value, _enum_value(column.type_name)) for column, value in zip(columns, values)})
            next_index = result.next_chunk_index
            if next_index is None:
                if result.next_chunk_internal_link:
                    raise DataQueryError("Databricks returned a chunk link without a chunk index.")
                break
            if not isinstance(next_index, int) or isinstance(next_index, bool) or next_index <= index or next_index in seen or not response.statement_id:
                raise DataQueryError("Databricks returned unexpected result pagination.")
            result = api.get_statement_result_chunk_n(statement_id=response.statement_id, chunk_index=next_index)
            if result.chunk_index != next_index:
                raise DataQueryError("Databricks returned an unexpected result chunk.")
        if manifest.total_row_count is not None and len(rows) != manifest.total_row_count:
            raise DataQueryError("Databricks returned incomplete results; partial evidence will not be returned.")
        if manifest.total_chunk_count is not None and manifest.total_chunk_count != len(seen):
            # Empty results may have no chunk, or one empty initial chunk.
            if rows or not (manifest.total_chunk_count == 0 and len(seen) <= 1):
                raise DataQueryError("Databricks returned incomplete result pagination.")
        return rows
