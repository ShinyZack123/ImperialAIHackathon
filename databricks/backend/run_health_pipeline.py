"""Upload a versioned notebook and submit ONE serverless health-processing run.

This is an explicit write operation: the notebook updates health_features and
health_alerts in the configured schema. It never overwrites a workspace notebook,
creates a classic cluster, schedules recurring work, or changes source evidence.

Run from the project directory after OAuth login:
  .venv/bin/python -m backend.run_health_pipeline --run-id UUID --as-of-ts TIMESTAMP

The five-minute polling limit is checked between SDK calls. Requests have their
own finite timeout; a request already in progress can extend elapsed wall time.
"""

import argparse
import base64
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import time
from uuid import UUID, uuid4

from dotenv import load_dotenv

from .databricks_data import DataSettings


PROJECT_DIR = Path(__file__).resolve().parents[1]
NOTEBOOK = PROJECT_DIR / "notebooks" / "02_gpu_health_pipeline.ipynb"
MAX_POLL_SECONDS = 300


def _value(value):
    return getattr(value, "value", value)


def _parameters(settings, run_id, as_of_ts):
    try:
        replay_id = str(UUID(str(run_id)))
        cursor = datetime.fromisoformat(as_of_ts.replace("Z", "+00:00"))
        if cursor.tzinfo is None or cursor.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError, AttributeError):
        raise ValueError("Provide a UUID run ID and an ISO timestamp with a timezone.") from None
    return {
        "catalog": settings.catalog,
        "schema": settings.schema,
        "run_id": replay_id,
        "as_of_ts": cursor.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _state(run):
    """Return enum fields only; unstructured remote messages may contain secrets."""
    legacy = getattr(run, "state", None)
    current = getattr(run, "status", None)
    termination = getattr(current, "termination_details", None)
    lifecycle = _value(getattr(current, "state", None)) or _value(getattr(legacy, "life_cycle_state", None))
    result = _value(getattr(legacy, "result_state", None)) or _value(getattr(termination, "code", None))
    return {"lifecycle_state": lifecycle, "result_state": result}


def execute_pipeline(
    settings,
    run_id,
    as_of_ts,
    *,
    client=None,
    notebook=NOTEBOOK,
    poll_interval=5,
    max_poll_seconds=MAX_POLL_SECONDS,
    monotonic=time.monotonic,
    sleep=time.sleep,
    emit=None,
):
    """Submit once, then return the actual terminal state or explicitly unknown state.

    The injected client/clock arguments support tests without authenticating.
    A polling timeout requests cancellation but does not claim it completed.
    """
    from databricks.sdk import WorkspaceClient
    from databricks.sdk.config import Config
    from databricks.sdk.service.compute import Environment
    from databricks.sdk.service.jobs import JobEnvironment, NotebookTask, PerformanceTarget, Source, SubmitTask
    from databricks.sdk.service.workspace import ImportFormat

    if not 0 < max_poll_seconds <= MAX_POLL_SECONDS or not 0 < poll_interval <= 30:
        raise ValueError("Polling must be positive, at most five minutes, and use intervals up to 30 seconds.")
    parameters = _parameters(settings, run_id, as_of_ts)
    payload = Path(notebook).read_bytes()
    document = json.loads(payload)
    if document.get("nbformat") != 4 or not document.get("cells"):
        raise ValueError("The health pipeline file is not a nonempty Jupyter notebook.")
    digest = hashlib.sha256(payload).hexdigest()
    report = {"status": "not_submitted", "parameters": parameters, "notebook_sha256": digest}
    stage = "authentication"
    last_state = None
    try:
        if client is None:
            kwargs = {"profile": settings.profile, "http_timeout_seconds": 30, "retry_timeout_seconds": 30}
            if settings.host:
                kwargs["host"] = settings.host
            client = WorkspaceClient(config=Config(**kwargs))
        stage = "identify_workspace_user"
        user_name = client.current_user.me().user_name
        if not isinstance(user_name, str) or not user_name or "/" in user_name or user_name in {".", ".."} or any(ord(c) < 32 for c in user_name):
            raise ValueError("Unexpected workspace user name.")
        folder = f"/Workspace/Users/{user_name}/GPU"
        # A content hash identifies the code; a random suffix prevents collisions
        # with existing notebooks without ever using overwrite=True.
        notebook_path = f"{folder}/02_gpu_health_pipeline_{digest[:12]}_{uuid4().hex[:8]}"
        report["notebook_path"] = notebook_path
        stage = "upload_notebook"
        client.workspace.mkdirs(path=folder)
        client.workspace.import_(path=notebook_path, content=base64.b64encode(payload).decode("ascii"), format=ImportFormat.JUPYTER, overwrite=False)
        stage = "submit_run"
        submitted = client.jobs.submit(
            run_name="GPU Ops health snapshot",
            idempotency_token=uuid4().hex,
            timeout_seconds=MAX_POLL_SECONDS,
            performance_target=PerformanceTarget.PERFORMANCE_OPTIMIZED,
            environments=[JobEnvironment(environment_key="health_env", spec=Environment(environment_version="4"))],
            tasks=[SubmitTask(
                task_key="health_snapshot",
                notebook_task=NotebookTask(notebook_path=notebook_path, source=Source.WORKSPACE, base_parameters=parameters),
                environment_key="health_env",
                max_retries=0,
                retry_on_timeout=False,
                disable_auto_optimization=True,
                timeout_seconds=MAX_POLL_SECONDS,
            )],
        )
        remote_run_id = submitted.response.run_id
        if not isinstance(remote_run_id, int) or isinstance(remote_run_id, bool) or remote_run_id <= 0:
            raise ValueError("Submit returned no usable run ID.")
        report.update(status="submitted", job_run_id=remote_run_id)
        if emit:
            emit(dict(report))
        stage = "monitor_run"
        deadline = monotonic() + max_poll_seconds
        while monotonic() < deadline:
            run = client.jobs.get_run(run_id=remote_run_id)
            observed = _state(run)
            report.update(observed)
            if run.run_page_url:
                report["run_page_url"] = run.run_page_url
            terminal = observed["lifecycle_state"] in {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}
            if terminal:
                report["status"] = "succeeded" if observed["result_state"] == "SUCCESS" else "finished_without_success"
                return report
            report["status"] = "running_or_waiting"
            if observed != last_state and emit:
                emit(dict(report))
            last_state = observed
            remaining = deadline - monotonic()
            if remaining > 0:
                sleep(min(poll_interval, remaining))
        report["status"] = "polling_timed_out"
        report["cancellation_requested"] = True
        try:
            client.jobs.cancel_run(run_id=remote_run_id)
            report["cancellation_request_accepted"] = True
        except Exception:
            report["cancellation_request_accepted"] = False
        report["message"] = "Stopped polling after five minutes or the configured shorter limit. The last observed state is shown; cancellation is not yet confirmed. Check the Databricks run page."
        return report
    except Exception:
        # Never copy an SDK exception, response body, notebook output, or trace.
        report["status"] = "monitoring_failed" if "job_run_id" in report else "setup_or_submission_failed"
        report["failed_stage"] = stage
        report["message"] = "Could not complete this stage. Check OAuth, workspace access, notebook path and available Free Edition quota. If submission failed without a run ID, inspect Databricks runs before retrying; the service may have accepted it. No alternate compute or automatic second run was started."
        return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Upload and run one versioned GPU health notebook on serverless compute.")
    parser.add_argument("--run-id", required=True, help="Synthetic replay UUID returned by the evidence API")
    parser.add_argument("--as-of-ts", required=True, help="Replay timestamp including timezone, e.g. 2026-11-14T09:07:30Z")
    args = parser.parse_args(argv)
    try:
        load_dotenv(PROJECT_DIR / ".env", override=False)
        settings = DataSettings.from_env()
        report = execute_pipeline(settings, args.run_id, args.as_of_ts, emit=lambda value: print(json.dumps(value), flush=True))
        print(json.dumps(report, indent=2), flush=True)
        return 0 if report["status"] == "succeeded" else 1
    except (ValueError, OSError, json.JSONDecodeError):
        print(json.dumps({"status": "not_submitted", "message": "Check the local notebook, .env configuration, UUID run ID and timezone-aware replay timestamp."}), flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
