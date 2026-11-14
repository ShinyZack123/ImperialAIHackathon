"""Local, read-only HTTP bridge from the demo UI/models to Databricks.

Run from the project directory with:
  .venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8010
This prototype has no user authentication; do not expose it publicly.
"""

from datetime import timedelta, timezone
from functools import lru_cache
import os
from pathlib import Path
from typing import Annotated
from uuid import UUID

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path as PathParameter, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import AwareDatetime

from backend.databricks_data import DataQueryError, DataQueryTimeout, DataSettings, DatabricksData

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


@lru_cache
def get_data() -> DatabricksData:
    try:
        return DatabricksData(DataSettings.from_env())
    except ValueError:
        raise HTTPException(503, "Databricks settings are incomplete. Check the server's .env file.") from None


DataDependency = Annotated[DatabricksData, Depends(get_data)]
GpuId = Annotated[str, PathParameter(pattern=r"^gpu-[0-9]{3}$")]
RackId = Annotated[str, PathParameter(pattern=r"^rack-[a-z]+$")]
WindowMinutes = Annotated[int, Query(ge=1, le=15)]

app = FastAPI(title="GPU Ops evidence API", version="0.1.0", description="Synthetic replay data from Databricks. Local development only.")
origins = os.getenv("FRONTEND_ORIGINS", "http://localhost:4397,http://127.0.0.1:4397,http://localhost:3000,http://localhost:5173")
app.add_middleware(CORSMiddleware, allow_origins=[s.strip() for s in origins.split(",") if s.strip()], allow_methods=["GET"], allow_headers=["Accept"])


@app.exception_handler(DataQueryError)
async def data_error_handler(request: Request, exc: DataQueryError):
    # The data client deliberately removes SDK response bodies and credentials.
    return JSONResponse(status_code=504 if isinstance(exc, DataQueryTimeout) else 502, content={"detail": str(exc)})


def envelope(data, run_id=None, as_of_ts=None):
    response = {"schema_version": "1.0", "source": "databricks", "synthetic": True, "data": data}
    if run_id is not None:
        response["run_id"] = str(run_id)
    if as_of_ts is not None:
        response["as_of_ts"] = as_of_ts.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return response


@app.get("/healthz")
def healthz():
    return {"status": "running", "databricks_connection": "not_checked", "mode": "local_development"}


@app.get("/api/runs")
def runs(data: DataDependency):
    return envelope(data.list_runs())


@app.get("/api/overview")
def overview(run_id: UUID, as_of_ts: AwareDatetime, data: DataDependency):
    rows = data.overview(str(run_id), as_of_ts)
    if len(rows) != 1:
        raise DataQueryError("Databricks returned an unexpected overview result.")
    return envelope(rows[0], run_id, as_of_ts)


@app.get("/api/racks/{rack_id}")
def rack_context(rack_id: RackId, run_id: UUID, as_of_ts: AwareDatetime, data: DataDependency):
    return envelope(data.rack_context(str(run_id), rack_id, as_of_ts), run_id, as_of_ts)


@app.get("/api/gpus/{gpu_id}/history")
def gpu_history(gpu_id: GpuId, run_id: UUID, as_of_ts: AwareDatetime, data: DataDependency, window_minutes: WindowMinutes = 5):
    return envelope(data.gpu_history(str(run_id), gpu_id, as_of_ts - timedelta(minutes=window_minutes), as_of_ts), run_id, as_of_ts)


@app.get("/api/gpus/{gpu_id}/workload-history")
def workload_history(gpu_id: GpuId, run_id: UUID, as_of_ts: AwareDatetime, data: DataDependency, window_minutes: WindowMinutes = 5):
    return envelope(data.workload_history(str(run_id), gpu_id, as_of_ts - timedelta(minutes=window_minutes), as_of_ts), run_id, as_of_ts)


@app.get("/api/health-assessments")
def health_assessments(run_id: UUID, as_of_ts: AwareDatetime, data: DataDependency):
    rows = data.health_alerts(str(run_id), as_of_ts)
    response = envelope(rows, run_id, as_of_ts)
    response["assessment_available"] = bool(rows)
    response["rule_version"] = "baseline-v1"
    response["counts"] = {status: sum(row["status"] == status for row in rows) for status in ("active", "clear", "unknown")}
    return response
