"""Read-only connection check: python -m backend.check_connection.

Loads this project's .env, queries replay metadata, and reads one overview at
7m30s into the first replay. It never creates or changes Databricks tables.
"""

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys

from dotenv import load_dotenv

from .databricks_data import DataQueryError, DataQueryTimeout, DataSettings, DatabricksData


def _utc_result_time(value: str) -> datetime:
    """The fixture is UTC; accept SQL timestamps with or without a UTC suffix."""
    if not isinstance(value, str):
        raise DataQueryError("Databricks returned a replay without a valid time range.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise DataQueryError("Databricks returned a replay without a valid time range.") from None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def connection_report(data: DatabricksData) -> dict:
    runs = data.list_runs()
    if not runs:
        raise DataQueryError("The telemetry table has no replay rows. Run the starter notebook in the configured catalog/schema.")
    run = runs[0]
    try:
        start = _utc_result_time(run["start_ts"])
        end = _utc_result_time(run["end_ts"])
        run_id = run["run_id"]
    except KeyError:
        raise DataQueryError("Databricks replay metadata is missing required columns.") from None
    if end < start or not isinstance(run_id, str) or not run_id:
        raise DataQueryError("Databricks returned invalid replay metadata.")
    cursor = min(start + timedelta(minutes=7, seconds=30), end).isoformat().replace("+00:00", "Z")
    overview = data.overview(run_id, cursor)
    if len(overview) != 1:
        raise DataQueryError("Databricks did not return exactly one overview row.")
    return {
        "status": "connected",
        "source": "databricks",
        "data_mode": "synthetic_replay",
        "run_count": len(runs),
        "selected_run": run,
        "as_of_ts": cursor,
        "overview": overview[0],
    }


def main() -> int:
    try:
        # Existing exported environment variables take precedence over the local file.
        load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
        settings = DataSettings.from_env()
        report = connection_report(DatabricksData(settings))
        print(json.dumps(report, indent=2, allow_nan=False))
        return 0
    except DataQueryTimeout as error:
        print(f"Connection check timed out: {error}", file=sys.stderr)
        print("In Databricks, open SQL Warehouses, confirm the configured warehouse is available, then run this check again.", file=sys.stderr)
    except (DataQueryError, ValueError) as error:
        print(f"Connection check failed: {error}", file=sys.stderr)
        print("Check .env for DATABRICKS_CATALOG, DATABRICKS_SCHEMA and DATABRICKS_SQL_WAREHOUSE_ID. Confirm the notebook created its tables there, then sign in with the documented Databricks CLI OAuth profile and retry. Do not paste tokens into chat or the website.", file=sys.stderr)
    except Exception:
        # Keep SDK/request details and credentials out of a copy-pasted diagnostic.
        print("Connection check failed unexpectedly. Verify the backend dependencies and Databricks configuration, then retry.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
