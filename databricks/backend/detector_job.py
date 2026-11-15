"""Deploy the official detector as a scheduled Databricks job (notebook 04).

Run from databricks/ after the OAuth login in CONNECT.md:
  python -m backend.detector_job deploy              # upload notebook 04, create or update the job (schedule paused)
  python -m backend.detector_job deploy --run-now    # ... then run it once and wait for the result
  python -m backend.detector_job deploy --unpause    # ... and let the schedule run (uses Free Edition quota)

The notebook goes to a folder this command manages, GPU/detector_job/, and is
replaced on every deploy. The job is found by name and updated in place, so
deploying twice never creates a second job. Nothing else in the workspace changes.
"""
from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import sys
import time

from backend.databricks_data import DataSettings


ROOT = Path(__file__).resolve().parents[1]
NOTEBOOK = ROOT / "notebooks" / "04_detector_job.ipynb"
JOB_NAME = "GPU Ops official detector"
CRON = "0 0/30 * * * ?"
MAX_WAIT_SECONDS = 1200


def _value(item):
    return getattr(item, "value", item)


def job_settings(notebook_path: str, settings: DataSettings, *, step_seconds: int, max_cursors: int, paused: bool) -> dict:
    from databricks.sdk.service.compute import Environment
    from databricks.sdk.service.jobs import (CronSchedule, JobEnvironment, NotebookTask, PauseStatus,
                                             PerformanceTarget, Source, Task)

    if isinstance(step_seconds, bool) or not isinstance(step_seconds, int) or not 5 <= step_seconds <= 900 or step_seconds % 5:
        raise ValueError("step_seconds must be a multiple of 5 between 5 and 900.")
    if isinstance(max_cursors, bool) or not isinstance(max_cursors, int) or not 1 <= max_cursors <= 2000:
        raise ValueError("max_cursors must be from 1 to 2000.")
    return {
        "name": JOB_NAME,
        "description": "Runs flagging.flagger.flag_window over the synthetic replays on serverless compute and saves each assessment to official_detector_results. Managed by backend.detector_job.",
        "tasks": [Task(
            task_key="assess_replays",
            notebook_task=NotebookTask(notebook_path=notebook_path, source=Source.WORKSPACE, base_parameters={
                "catalog": settings.catalog, "schema": settings.schema,
                "step_seconds": str(step_seconds), "max_cursors": str(max_cursors),
                "job_run_id": "{{job.run_id}}",
            }),
            environment_key="detector_env", max_retries=0, retry_on_timeout=False, timeout_seconds=MAX_WAIT_SECONDS,
        )],
        "environments": [JobEnvironment(environment_key="detector_env", spec=Environment(environment_version="4"))],
        "schedule": CronSchedule(quartz_cron_expression=CRON, timezone_id="UTC",
                                 pause_status=PauseStatus.PAUSED if paused else PauseStatus.UNPAUSED),
        "max_concurrent_runs": 1,
        "timeout_seconds": MAX_WAIT_SECONDS,
        "performance_target": PerformanceTarget.PERFORMANCE_OPTIMIZED,
        "tags": {"project": "gpu-ops-hackathon", "data": "synthetic"},
    }


def deploy(client, settings: DataSettings, *, step_seconds: int = 15, max_cursors: int = 500, paused: bool = True,
           notebook: Path = NOTEBOOK) -> dict:
    from databricks.sdk.service.jobs import JobSettings
    from databricks.sdk.service.workspace import ImportFormat

    payload = Path(notebook).read_bytes()
    document = json.loads(payload)
    if document.get("nbformat") != 4 or not document.get("cells"):
        raise ValueError("Notebook 04 is missing or not a Jupyter notebook. Run build_detector_job_notebook.py.")
    user = client.current_user.me().user_name
    if not isinstance(user, str) or not user or "/" in user or user in {".", ".."}:
        raise ValueError("Unexpected workspace user name.")
    folder = f"/Workspace/Users/{user}/GPU/detector_job"
    path = f"{folder}/04_detector_job"
    client.workspace.mkdirs(path=folder)
    client.workspace.import_(path=path, content=base64.b64encode(payload).decode("ascii"), format=ImportFormat.JUPYTER, overwrite=True)
    config = job_settings(path, settings, step_seconds=step_seconds, max_cursors=max_cursors, paused=paused)
    existing = [job for job in client.jobs.list(name=JOB_NAME) if getattr(job.settings, "name", None) == JOB_NAME]
    if len(existing) > 1:
        raise ValueError(f"More than one job is named {JOB_NAME!r}; remove the extra one before deploying.")
    if existing:
        job_id = existing[0].job_id
        client.jobs.reset(job_id=job_id, new_settings=JobSettings(**config))
        action = "updated"
    else:
        job_id = client.jobs.create(**config).job_id
        action = "created"
    return {"job": JOB_NAME, "job_id": job_id, "action": action, "notebook": "GPU/detector_job/04_detector_job",
            "schedule": CRON + " UTC", "schedule_paused": paused, "step_seconds": step_seconds, "max_cursors": max_cursors}


def run_once(client, job_id: int, *, poll_seconds: float = 10, max_wait: float = MAX_WAIT_SECONDS,
             monotonic=time.monotonic, sleep=time.sleep, emit=print) -> dict:
    run_id = client.jobs.run_now(job_id=job_id).response.run_id
    emit(json.dumps({"job_run_id": run_id, "status": "submitted"}))
    deadline, last = monotonic() + max_wait, None
    while True:
        run = client.jobs.get_run(run_id=run_id)
        state = (_value(getattr(run.state, "life_cycle_state", None)), _value(getattr(run.state, "result_state", None)))
        if state != last:
            emit(json.dumps({"lifecycle": state[0], "result": state[1]}))
            last = state
        if state[0] in {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}:
            break
        if monotonic() >= deadline:
            return {"job_run_id": run_id, "status": "still_running", "message": "Stopped waiting; check the run in Jobs & Pipelines."}
        sleep(poll_seconds)
    report = {"job_run_id": run_id, "status": "succeeded" if state[1] == "SUCCESS" else "failed", "result": state[1]}
    if report["status"] == "failed" and run.tasks:
        output = client.jobs.get_run_output(run_id=run.tasks[0].run_id)
        if output.error:
            report["error"] = output.error[:1000]
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Deploy the official detector as a Databricks job.")
    commands = parser.add_subparsers(dest="command", required=True)
    deploy_parser = commands.add_parser("deploy", help="Upload notebook 04 and create or update the job")
    deploy_parser.add_argument("--step-seconds", type=int, default=15)
    deploy_parser.add_argument("--max-cursors", type=int, default=500)
    deploy_parser.add_argument("--run-now", action="store_true", help="Run the job once after deploying and wait for it")
    deploy_parser.add_argument("--unpause", action="store_true", help="Let the 30-minute schedule run")
    args = parser.parse_args(argv)
    try:
        from dotenv import load_dotenv
        from databricks.sdk import WorkspaceClient
        from databricks.sdk.config import Config

        load_dotenv(ROOT / ".env", override=False)
        settings = DataSettings.from_env()
        kwargs = {"profile": settings.profile, "http_timeout_seconds": 60, "retry_timeout_seconds": 60}
        if settings.host:
            kwargs["host"] = settings.host
        client = WorkspaceClient(config=Config(**kwargs))
        result = deploy(client, settings, step_seconds=args.step_seconds, max_cursors=args.max_cursors, paused=not args.unpause)
        print(json.dumps(result), flush=True)
        if args.run_now:
            report = run_once(client, result["job_id"])
            print(json.dumps(report), flush=True)
            return 0 if report["status"] == "succeeded" else 1
        return 0
    except ValueError as error:
        print(f"Detector job deploy failed: {error}", file=sys.stderr)
    except Exception:
        # SDK errors can include workspace URLs; keep the message generic.
        print("Detector job deploy failed. Check the Databricks login, workspace permissions and Free Edition job quota.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
