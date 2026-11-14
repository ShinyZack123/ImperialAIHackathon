#!/usr/bin/env python3
"""Rebuild the self-contained Databricks notebook from generator and SQL files.

No Databricks connection or third-party packages are required to build the file.
Every Python cell is compiled locally, but Spark cells must be run in Databricks.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
import textwrap


ROOT = Path(__file__).resolve().parent
DESTINATION = ROOT / "notebooks" / "01_gpu_data_starter.ipynb"


def _cell(kind: str, cell_id: str, source: str) -> dict:
    source = textwrap.dedent(source).strip() + "\n"
    cell = {"cell_type": kind, "id": cell_id, "metadata": {}, "source": source.splitlines(keepends=True)}
    if kind == "code":
        ast.parse(source, filename=f"notebook:{cell_id}")
        compile(source, f"notebook:{cell_id}", "exec")
        cell.update({"execution_count": None, "outputs": []})
    return cell


def _generator_without_entrypoint() -> str:
    source = (ROOT / "generate.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    main_guard = next((node for node in tree.body if isinstance(node, ast.If) and ast.unparse(node.test) == "__name__ == '__main__'"), None)
    if main_guard is None or main_guard is not tree.body[-1]:
        raise ValueError("Expected a final __main__ guard in generate.py; inspect before embedding.")
    lines = source.splitlines(keepends=True)
    return "".join(lines[:main_guard.lineno - 1])


def build_notebook() -> dict:
    queries = {path.stem: path.read_text(encoding="utf-8") for path in sorted((ROOT / "sql").glob("*.sql"))}
    required_queries = {"overview", "gpu_history", "rack_context"}
    if not required_queries.issubset(queries):
        raise ValueError(f"Missing SQL files: {sorted(required_queries - queries.keys())}")

    cells = [
        _cell("markdown", "start", """
        # GPU operations: Databricks data starter

        **Synthetic data only. This is a prerecorded replay with scheduled recovery.
        Recovery in these readings does not demonstrate that an agent repaired a fault.**

        This notebook creates four Delta tables for your website and model teammates:
        `inventory`, `telemetry`, `workloads`, and `cooling`. It generates 24 simulated
        GPUs across NVIDIA, AMD, and Intel labels. Device limits are demo settings,
        not specifications for real hardware.

        ## Import and run

        1. In your Databricks workspace, use **Workspace → Import** (or **Import** in
           the target folder menu), choose **File**, and select this `.ipynb` file.
        2. Open the imported notebook and select **Serverless** compute.
        3. Run the first code cell to see accessible catalogs. `CATALOG` defaults to
           your current catalog; if necessary, replace it with a catalog where you
           may create a schema. Leave `SCHEMA` and `SEED` unchanged initially.
        4. Click **Run all**. No package installation, API key, file upload or external
           data download is needed after importing the notebook.
        5. Confirm the final counts and the three example query results. Your four
           tables will also appear in **Catalog Explorer** under the printed path.

        Re-running with identical data leaves existing tables unchanged. If a table
        has different schema or data, this notebook stops before writing any tables.
        Choose a **new SCHEMA name**, such as `gpu_ops_hackathon_v2`, to load a new seed
        or changed generator. It never overwrites existing table data.

        Evaluation labels are generated in a temporary local directory and deleted.
        They are **never uploaded to Delta tables or displayed**. Give the agent access
        only to the four evidence tables through bounded queries, not to this notebook
        or the generator source. Replay queries must filter to the selected `run_id`
        and `event_ts <= as_of_ts` so the agent cannot see future samples.

        If creation fails with a permission error, select a writable catalog/schema
        or ask the workspace owner to grant the required access. Do not delete existing
        data to resolve it.
        """),
        _cell("code", "configuration", """
        # First inspect the catalogs available to your account.
        display(spark.sql("SHOW CATALOGS"))

        CATALOG = spark.sql("SELECT current_catalog()").first()[0]
        SCHEMA = "gpu_ops_hackathon"
        SEED = 42

        import re

        for label, identifier in (("CATALOG", CATALOG), ("SCHEMA", SCHEMA)):
            if not isinstance(identifier, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", identifier):
                raise ValueError(f"{label} must contain only letters, digits and underscores and must start with a letter or underscore.")
        spark.conf.set("spark.sql.session.timeZone", "UTC")
        spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{CATALOG}`.`{SCHEMA}`")
        spark.sql(f"USE CATALOG `{CATALOG}`")
        spark.sql(f"USE SCHEMA `{SCHEMA}`")
        print(f"Evidence table location: {CATALOG}.{SCHEMA}")
        """),
        _cell("markdown", "generator-note", """
        ## Reproducible fixture generator

        The following cell embeds `generate.py` so this notebook can run on its own.
        It defines functions without running its command-line entry point.
        """),
        _cell("code", "embedded-generator", _generator_without_entrypoint()),
        _cell("code", "generate-and-check", """
        # Keep every file, including evaluation labels, on the driver in /tmp.
        # Copy only the four CSV evidence datasets into memory, then delete all files.
        with tempfile.TemporaryDirectory(prefix="gpu-ops-notebook-", dir="/tmp") as temp_dir:
            fixture_dir = Path(temp_dir) / "dataset"
            generation_info = generate_dataset(fixture_dir, seed=SEED, scenario="all")
            evidence_rows = {}
            for table_name, field_names in SCHEMAS.items():
                with (fixture_dir / "evidence" / f"{table_name}.csv").open(newline="", encoding="utf-8") as handle:
                    reader = csv.DictReader(handle)
                    if tuple(reader.fieldnames) != field_names:
                        raise ValueError(f"Unexpected CSV schema for {table_name}")
                    evidence_rows[table_name] = list(reader)

        expected_counts = {"inventory": 24, "telemetry": 17280, "workloads": 17280, "cooling": 2160}
        for table_name, expected in expected_counts.items():
            if len(evidence_rows[table_name]) != expected:
                raise ValueError(f"Unexpected row count for {table_name}")

        # Fixture integrity checks are plain Python and run before any table writes.
        gpu_ids = {row["gpu_id"] for row in evidence_rows["inventory"]}
        if len(gpu_ids) != 24:
            raise ValueError("Inventory must contain 24 unique GPUs")
        if any(row["model"] != "Synthetic-" + row["vendor"] for row in evidence_rows["inventory"]):
            raise ValueError("Fixture models must be labelled synthetic")
        for table_name, entity in (("telemetry", "gpu_id"), ("workloads", "gpu_id"), ("cooling", "cooling_zone_id")):
            rows = evidence_rows[table_name]
            keys = {(row["run_id"], row["event_ts"], row[entity]) for row in rows}
            if len(keys) != len(rows):
                raise ValueError(f"Duplicate temporal keys in {table_name}")
            if len({row["run_id"] for row in rows}) != 4:
                raise ValueError(f"Expected four opaque runs in {table_name}")
            if any(not row["event_ts"].endswith("Z") for row in rows):
                raise ValueError("Every timestamp must be explicitly UTC")
        for row in evidence_rows["telemetry"]:
            if row["gpu_id"] not in gpu_ids:
                raise ValueError("Unknown GPU in telemetry")
            if row["heartbeat_ok"] == "false" and any(row[field] != "" for field in NULLABLE_METRICS):
                raise ValueError("Missing telemetry must contain empty metrics, not zeroes")

        print("Synthetic CSV checks passed. Temporary evidence and evaluation files have been deleted.")
        """),
        _cell("code", "spark-types", """
        from pyspark.sql.types import (
            StructType, StructField, StringType, DoubleType, LongType,
            BooleanType, TimestampType,
        )

        double_columns = {
            "power_limit_w", "temperature_limit_c", "power_w", "gpu_util_pct",
            "memory_used_gb", "memory_total_gb", "temp_c", "clock_mhz",
            "inlet_temp_c", "flow_pct",
        }
        long_columns = {"ecc_uncorrected_total", "progress_units"}
        boolean_columns = {"throttled", "heartbeat_ok"}

        def field_type(name):
            if name == "event_ts":
                return TimestampType()
            if name in double_columns:
                return DoubleType()
            if name in long_columns:
                return LongType()
            if name in boolean_columns:
                return BooleanType()
            return StringType()

        def convert_cell(table_name, name, value):
            if value == "":
                allowed = (table_name == "telemetry" and name in NULLABLE_METRICS) or (table_name == "workloads" and name == "job_id")
                if not allowed:
                    raise ValueError(f"Unexpected empty value: {table_name}.{name}")
                return None
            if name == "event_ts":
                return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
            if name in double_columns:
                converted = float(value)
                if not math.isfinite(converted):
                    raise ValueError(f"Non-finite number in {table_name}.{name}")
                return converted
            if name in long_columns:
                return int(value)
            if name in boolean_columns:
                if value not in ("true", "false"):
                    raise ValueError(f"Invalid boolean in {table_name}.{name}")
                return value == "true"
            return value

        frames = {}
        for table_name, columns in SCHEMAS.items():
            # Nullable Spark fields match persisted Delta schemas. Required values
            # are checked during conversion; empty metrics are explicitly None.
            explicit_schema = StructType([StructField(name, field_type(name), True) for name in columns])
            converted_rows = [tuple(convert_cell(table_name, name, row[name]) for name in columns) for row in evidence_rows[table_name]]
            frames[table_name] = spark.createDataFrame(converted_rows, schema=explicit_schema)
        print("Created explicitly typed Spark DataFrames; no schema inference was used.")
        """),
        _cell("markdown", "persist-note", """
        ## Persist only evidence

        The next cell checks **all existing target tables before writing**. Identical
        schema and data are reused. A difference stops the load and asks for a new
        schema name. New tables use SQL `CREATE TABLE ... USING DELTA AS SELECT`.
        This avoids a DataFrame write mode that some environments do not support.
        """),
        _cell("code", "persist-delta", """
        qualified_names = {name: f"`{CATALOG}`.`{SCHEMA}`.`{name}`" for name in SCHEMAS}
        already_loaded = set()
        conflicts = []
        for table_name, incoming in frames.items():
            qualified_name = qualified_names[table_name]
            if spark.catalog.tableExists(qualified_name):
                existing = spark.table(qualified_name)
                if existing.schema != incoming.schema:
                    conflicts.append(f"{table_name}: schema differs")
                    continue
                # exceptAll preserves duplicate counts. Check both directions.
                different = existing.exceptAll(incoming).limit(1).count() > 0 or incoming.exceptAll(existing).limit(1).count() > 0
                if different:
                    conflicts.append(f"{table_name}: data differs")
                else:
                    already_loaded.add(table_name)

        if conflicts:
            raise ValueError("Existing tables were not changed. Choose a NEW SCHEMA name in the configuration cell and run all again. Conflicts: " + "; ".join(conflicts))

        from uuid import uuid4

        for table_name, incoming in frames.items():
            if table_name in already_loaded:
                print(f"Unchanged: {qualified_names[table_name]}")
            else:
                temp_view = f"_gpu_ops_load_{uuid4().hex}"
                incoming.createOrReplaceTempView(temp_view)
                try:
                    # CREATE fails if another writer creates the target after preflight.
                    # Collect finishes the command before its source view is removed.
                    spark.sql(
                        f"CREATE TABLE {qualified_names[table_name]} "
                        f"USING DELTA AS SELECT * FROM `{temp_view}`"
                    ).collect()
                finally:
                    spark.catalog.dropTempView(temp_view)
                print(f"Created: {qualified_names[table_name]}")

        counts = []
        for table_name, expected in expected_counts.items():
            actual = spark.table(qualified_names[table_name]).count()
            if actual != expected:
                raise ValueError(f"Stored row count mismatch in {table_name}: {actual}, expected {expected}")
            counts.append((table_name, actual))
        display(spark.createDataFrame(counts, "table_name STRING, row_count LONG"))
        display(spark.table(qualified_names["inventory"]).orderBy("gpu_id"))
        """),
        _cell("markdown", "queries-note", """
        ## Example queries for the website and agent

        These are the exact SQL files shipped in the starter. Parameters are bound
        through `spark.sql(..., args=...)`; they are not pasted into SQL strings.
        Every temporal source is filtered by an opaque run ID and the replay cursor.
        Unknown or stale telemetry remains distinguishable from a healthy reading.

        The initial run is the first opaque ID; it is not chosen using its answer
        label. Different runs can represent different conditions. Advance or move
        the cursor between `09:00:00Z` and `09:14:55Z`, or select a different ID from
        the displayed list. Queries do not create diagnoses or model outputs.
        """),
        _cell("code", "embedded-queries", "QUERIES = " + repr(queries)),
        _cell("code", "run-queries", """
        runs = spark.table(qualified_names["telemetry"]).select("run_id").distinct().orderBy("run_id")
        display(runs)
        RUN_ID = runs.first()["run_id"]
        AS_OF_TS = datetime(2026, 11, 14, 9, 7, 30, tzinfo=timezone.utc)
        WINDOW_START_TS = AS_OF_TS - timedelta(minutes=5)
        first_gpu = spark.table(qualified_names["inventory"]).orderBy("gpu_id").first()
        GPU_ID = first_gpu["gpu_id"]
        RACK_ID = first_gpu["rack_id"]

        print(f"Replay: run={RUN_ID}, cursor={AS_OF_TS.isoformat()}, gpu={GPU_ID}, rack={RACK_ID}")
        display(spark.sql(QUERIES["overview"], args={"run_id": RUN_ID, "as_of_ts": AS_OF_TS}))
        display(spark.sql(QUERIES["gpu_history"], args={
            "run_id": RUN_ID, "gpu_id": GPU_ID,
            "window_start_ts": WINDOW_START_TS, "as_of_ts": AS_OF_TS,
        }))
        display(spark.sql(QUERIES["rack_context"], args={
            "run_id": RUN_ID, "rack_id": RACK_ID, "as_of_ts": AS_OF_TS,
        }))
        """),
        _cell("markdown", "handoff", """
        ## Share with teammates

        Share the four table names, the SQL files and their parameter names. The
        website should display telemetry timestamps, use “unknown” for unavailable
        readings, and label summed power as **GPU power**, not total facility power.

        For the model teammates, agree on one incident-output format and evaluate
        them on the same evidence windows. This notebook creates neither the
        deterministic detector nor the fine-tuned model nor an autonomous repair loop.
        Keep answer labels outside the agent's data access; use separate evaluation
        data when measuring accuracy.

        References: [Databricks serverless limits](https://docs.databricks.com/aws/en/compute/serverless/limitations),
        [Spark SQL bound parameters](https://spark.apache.org/docs/latest/api/python/reference/pyspark.sql.SparkSession.sql.html),
        [SQL Statement Execution API](https://docs.databricks.com/aws/en/dev-tools/sql-execution-tutorial).
        """),
    ]
    return {
        "cells": cells,
        "metadata": {
            "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
            "language_info": {"name": "python"},
        },
        "nbformat": 4,
        "nbformat_minor": 5,
    }


def main() -> None:
    notebook = build_notebook()
    DESTINATION.parent.mkdir(parents=True, exist_ok=True)
    DESTINATION.write_text(json.dumps(notebook, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    replacement = next(cell for cell in notebook["cells"] if cell["id"] == "persist-delta")
    (DESTINATION.parent / "replace_failed_cell.py").write_text(
        "# Paste this into the existing failed persistence cell, replacing that entire cell.\n"
        "# Earlier notebook cells must already have run in this session.\n"
        + "".join(replacement["source"]), encoding="utf-8"
    )
    code_count = sum(cell["cell_type"] == "code" for cell in notebook["cells"])
    print(f"Built {DESTINATION} ({code_count} Python cells compiled; Spark execution requires Databricks).")


if __name__ == "__main__":
    main()
