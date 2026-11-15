#!/usr/bin/env python3
"""Build notebook 04: the official detector as a Databricks job. No cloud calls.

The notebook carries the repository's detector code as a compressed payload and
checks each file's SHA-256 before importing it, so the job runs exactly the
detector the website and backend run, and saves results under the same
detector version. Rebuild after changing any packed file:
  python3 build_detector_job_notebook.py
"""

import ast
import base64
import gzip
import hashlib
import json
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
DESTINATION = ROOT / "notebooks" / "04_detector_job.ipynb"
PACKED_FILES = (
    "flagging/flagger.py",
    "flagging/config.json",
    "databricks/backend/__init__.py",
    "databricks/backend/databricks_data.py",
    "databricks/backend/flagging_adapter.py",
    "databricks/backend/result_store.py",
    "databricks/backend/lakehouse.py",
    "databricks/backend/detector_batch.py",
)


def cell(kind, name, source):
    source = textwrap.dedent(source).strip() + "\n"
    result = {"cell_type": kind, "id": name, "metadata": {}, "source": source.splitlines(keepends=True)}
    if kind == "code":
        ast.parse(source)
        result.update(execution_count=None, outputs=[])
    return result


def packed_code():
    files = {path: (REPO / path).read_text(encoding="utf-8") for path in PACKED_FILES}
    manifest = {path: hashlib.sha256(text.encode("utf-8")).hexdigest() for path, text in files.items()}
    payload = base64.b64encode(gzip.compress(json.dumps(files, sort_keys=True).encode("utf-8"), mtime=0)).decode("ascii")
    return manifest, payload


def build_notebook():
    manifest, payload = packed_code()
    lines = "\n".join(f"    {payload[i:i + 100]!r}" for i in range(0, len(payload), 100))
    code_cell = (
        "import base64, gzip, hashlib, importlib, json, sys, tempfile\n"
        "from pathlib import Path\n\n"
        f"MANIFEST = {json.dumps(manifest, indent=4)}\n"
        f"PAYLOAD = (\n{lines}\n)\n\n"
        "files = json.loads(gzip.decompress(base64.b64decode(PAYLOAD)))\n"
        "if set(files) != set(MANIFEST):\n"
        "    raise RuntimeError('The packed detector code is incomplete.')\n"
        "CODE_ROOT = Path(tempfile.mkdtemp(prefix='gpu_ops_detector_'))\n"
        "for path, text in files.items():\n"
        "    data = text.encode('utf-8')\n"
        "    if hashlib.sha256(data).hexdigest() != MANIFEST[path]:\n"
        "        raise RuntimeError(f'Packed file {path} does not match its checksum.')\n"
        "    target = CODE_ROOT / path\n"
        "    target.parent.mkdir(parents=True, exist_ok=True)\n"
        "    target.write_bytes(data)\n"
        "# A re-run in the same session must import this copy, not an earlier one.\n"
        "for name in [name for name in sys.modules if name == 'backend' or name.startswith('backend.') or name.startswith('gpu_ops_shared_flagger')]:\n"
        "    del sys.modules[name]\n"
        "sys.path.insert(0, str(CODE_ROOT / 'databricks'))\n"
        "detector_batch = importlib.import_module('backend.detector_batch')\n"
        "result_store = importlib.import_module('backend.result_store')\n"
        "DETECTOR_VERSION = result_store.detector_version()\n"
        "print(f'Official detector loaded: flagging.flagger.flag_window, version {DETECTOR_VERSION[:16]}')\n"
    )
    cells = [
        cell("markdown", "intro", """
        # Official detector job

        Runs the team's deterministic detector (`flagging.flagger.flag_window`) on Databricks
        serverless compute and saves each assessment to `official_detector_results`, the same
        audit table the website's connected backend writes. **Synthetic data only.**

        Each run plans replay cursors every `step_seconds` for every replay, skips the ones the
        current detector version has already saved, assesses the rest from a 15-minute evidence
        window ending at each cursor, and records what it did in `detector_job_runs`. When the
        detector code changes, its version fingerprint changes and the next run re-assesses
        everything. The gold views and the scorecard in notebook 03 update automatically.

        Deploy and schedule it with `python -m backend.detector_job deploy` from the
        repository's `databricks/` folder. The schedule is created paused to protect Free
        Edition compute quota.
        """),
        cell("code", "configuration", """
        from datetime import datetime, timezone
        import re

        dbutils.widgets.text("catalog", "workspace")
        dbutils.widgets.text("schema", "gpu_ops_hackathon")
        dbutils.widgets.text("step_seconds", "15")
        dbutils.widgets.text("max_cursors", "500")
        dbutils.widgets.text("job_run_id", "")

        CATALOG = dbutils.widgets.get("catalog")
        SCHEMA = dbutils.widgets.get("schema")
        for value in (CATALOG, SCHEMA):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError("Catalog and schema must contain letters, digits and underscores.")
        STEP_SECONDS = int(dbutils.widgets.get("step_seconds"))
        MAX_CURSORS = int(dbutils.widgets.get("max_cursors"))
        JOB_RUN_ID = dbutils.widgets.get("job_run_id").strip() or None
        if JOB_RUN_ID is not None and not re.fullmatch(r"[0-9]{1,30}", JOB_RUN_ID):
            JOB_RUN_ID = None  # A literal, unresolved reference means a manual run.
        STARTED_AT = datetime.now(timezone.utc)
        spark.conf.set("spark.sql.session.timeZone", "UTC")
        spark.sql(f"USE CATALOG `{CATALOG}`")
        spark.sql(f"USE SCHEMA `{SCHEMA}`")
        print(f"Assessing {CATALOG}.{SCHEMA} every {STEP_SECONDS} s, at most {MAX_CURSORS} cursors this run")
        """),
        cell("markdown", "code-note", """
        ## Load the repository's detector

        The next cell holds the detector, its configuration and the connector adapter from the
        repository, compressed. Each file is checked against its SHA-256 before it is imported.
        The printed version is the fingerprint stored with every saved assessment.
        """),
        cell("code", "load-detector", code_cell),
        cell("code", "read-evidence", """
        from collections import defaultdict
        from datetime import timezone as _tz

        def iso(value):
            return value.replace(tzinfo=_tz.utc).isoformat().replace("+00:00", "Z")

        def rows(sql):
            return [row.asDict() for row in spark.sql(sql).collect()]

        inventory = rows("SELECT gpu_id, vendor, model, rack_id, server_id, cooling_zone_id, power_limit_w, temperature_limit_c FROM inventory ORDER BY gpu_id")
        replays = rows("SELECT run_id, MIN(event_ts) AS start_ts, MAX(event_ts) AS end_ts FROM telemetry GROUP BY run_id ORDER BY run_id")
        columns = {
            "telemetry": "run_id, event_ts, gpu_id, power_w, gpu_util_pct, memory_used_gb, memory_total_gb, temp_c, clock_mhz, throttled, ecc_uncorrected_total, heartbeat_ok",
            "workloads": "run_id, event_ts, gpu_id, job_id, job_state, progress_units, priority",
            "cooling": "run_id, event_ts, cooling_zone_id, inlet_temp_c, flow_pct",
        }
        streams = {name: defaultdict(list) for name in columns}
        evidence_rows_read = len(inventory)
        for name, select in columns.items():
            for row in rows(f"SELECT {select} FROM {name}"):
                row["event_ts"] = iso(row["event_ts"])
                streams[name][row["run_id"]].append(row)
                evidence_rows_read += 1
        evidence = {replay["run_id"]: detector_batch.ReplayEvidence(replay["run_id"], {name: streams[name][replay["run_id"]] for name in columns})
                    for replay in replays}
        print(f"Read {evidence_rows_read} evidence rows: {len(inventory)} GPUs, {len(replays)} replays")
        """),
        cell("code", "assess", """
        spark.sql(\"\"\"CREATE TABLE IF NOT EXISTS official_detector_results (
            run_id STRING NOT NULL, as_of_ts TIMESTAMP NOT NULL,
            detector_version STRING NOT NULL, evidence_sha256 STRING NOT NULL,
            incident_count BIGINT NOT NULL, snapshot_json STRING NOT NULL,
            generated_at TIMESTAMP NOT NULL) USING DELTA\"\"\")
        saved = spark.sql("SELECT DISTINCT run_id, as_of_ts FROM official_detector_results WHERE detector_version = :version",
                          args={"version": DETECTOR_VERSION}).collect()
        already = {(row["run_id"], row["as_of_ts"].replace(tzinfo=timezone.utc)) for row in saved}
        result = detector_batch.assess(inventory, replays, evidence, step_seconds=STEP_SECONDS,
                                       already_assessed=already, max_cursors=MAX_CURSORS)
        records = result["records"]
        print(f"Planned {result['planned']} cursors: {result['already_assessed']} already saved by this version, "
              f"{len(records)} assessed now, {result['deferred']} left for a later run. "
              f"{result['incident_observations']} incident observations.")
        """),
        cell("code", "save", """
        from uuid import uuid4
        from pyspark.sql.types import LongType, StringType, StructField, StructType, TimestampType

        schema = StructType([
            StructField("run_id", StringType(), False), StructField("as_of_ts", TimestampType(), False),
            StructField("detector_version", StringType(), False), StructField("evidence_sha256", StringType(), False),
            StructField("incident_count", LongType(), False), StructField("snapshot_json", StringType(), False),
            StructField("generated_at", TimestampType(), False),
        ])
        for offset in range(0, len(records), 50):
            batch = [(r["run_id"], r["as_of_ts"], r["detector_version"], r["evidence_sha256"], r["incident_count"], r["snapshot_json"], r["generated_at"])
                     for r in records[offset:offset + 50]]
            view = "detector_job_" + uuid4().hex
            spark.createDataFrame(batch, schema).createOrReplaceTempView(view)
            try:
                spark.sql(f\"\"\"MERGE INTO official_detector_results AS target USING `{view}` AS source
                    ON target.run_id = source.run_id AND target.as_of_ts = source.as_of_ts
                       AND target.detector_version = source.detector_version AND target.evidence_sha256 = source.evidence_sha256
                    WHEN MATCHED THEN UPDATE SET snapshot_json = source.snapshot_json,
                         incident_count = source.incident_count, generated_at = source.generated_at
                    WHEN NOT MATCHED THEN INSERT (run_id, as_of_ts, detector_version, evidence_sha256, incident_count, snapshot_json, generated_at)
                         VALUES (source.run_id, source.as_of_ts, source.detector_version, source.evidence_sha256,
                                 source.incident_count, source.snapshot_json, source.generated_at)\"\"\").collect()
            finally:
                spark.catalog.dropTempView(view)
        stored = spark.sql("SELECT COUNT(DISTINCT run_id, as_of_ts) AS n FROM official_detector_results WHERE detector_version = :version",
                           args={"version": DETECTOR_VERSION}).first()["n"]
        if stored < len(already) + len(records):
            raise RuntimeError("Fewer assessments are stored than this run saved; check the table history.")
        print(f"Saved {len(records)} assessments. This detector version now covers {stored} replay cursors.")
        """),
        cell("code", "log-run", """
        spark.sql(\"\"\"CREATE TABLE IF NOT EXISTS detector_job_runs (
            job_run_id STRING, started_at TIMESTAMP NOT NULL, finished_at TIMESTAMP NOT NULL,
            compute STRING NOT NULL, detector_version STRING NOT NULL, step_seconds INT NOT NULL,
            cursors_planned INT NOT NULL, cursors_already_assessed INT NOT NULL, cursors_assessed INT NOT NULL,
            cursors_deferred INT NOT NULL, incident_observations BIGINT NOT NULL, evidence_rows_read BIGINT NOT NULL) USING DELTA\"\"\")
        spark.sql(
            "INSERT INTO detector_job_runs (job_run_id, started_at, finished_at, compute, detector_version, step_seconds, "
            "cursors_planned, cursors_already_assessed, cursors_assessed, cursors_deferred, incident_observations, evidence_rows_read) "
            "VALUES (CAST(:job_run_id AS STRING), :started_at, :finished_at, 'databricks_serverless', :version, :step, :planned, :already, "
            ":assessed, :deferred, :incidents, :rows_read)",
            args={"job_run_id": JOB_RUN_ID, "started_at": STARTED_AT, "finished_at": datetime.now(timezone.utc),
                  "version": DETECTOR_VERSION, "step": STEP_SECONDS, "planned": result["planned"],
                  "already": result["already_assessed"], "assessed": len(records), "deferred": result["deferred"],
                  "incidents": result["incident_observations"], "rows_read": evidence_rows_read})
        display(spark.sql("SELECT * FROM detector_job_runs ORDER BY started_at DESC LIMIT 10"))
        """),
        cell("markdown", "limits", """
        ## Limits

        The replays are a fixed synthetic recording, so a scheduled run finds new work only when
        the detector version changes or the cursor spacing is reduced. A live collector appending
        telemetry would give each run new cursors to assess. Findings describe an analysis window,
        not a confirmed physical cause.
        """),
    ]
    return {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.10"}}, "nbformat": 4, "nbformat_minor": 5}


if __name__ == "__main__":
    DESTINATION.write_text(json.dumps(build_notebook(), indent=2) + "\n")
    print(DESTINATION)
