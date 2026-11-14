#!/usr/bin/env python3
"""Build a self-contained Databricks health-processing notebook. No cloud calls."""

import ast
import json
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parent


def cell(kind, name, source):
    source = textwrap.dedent(source).strip() + "\n"
    result = {"cell_type": kind, "id": name, "metadata": {}, "source": source.splitlines(keepends=True)}
    if kind == "code":
        ast.parse(source)
        result.update(execution_count=None, outputs=[])
    return result


def build_notebook():
    feature_sql = (ROOT / "sql/health_features.sql").read_text()
    rule_source = (ROOT / "health_rules.py").read_text()
    cells = [
        cell("markdown", "intro", """
        # GPU health processing on Databricks

        Run **01_gpu_data_starter** first. This second notebook reads its four
        evidence tables, computes time-bounded health features with Spark SQL,
        evaluates transparent baseline rules, and uses Delta **MERGE** to store
        repeatable snapshots for the website and diagnostic-model teammate.

        The data and thresholds are synthetic. These rules flag observations;
        they do not diagnose a physical cause, train a model or repair hardware.
        Your deterministic-model teammate can replace the rule implementation.

        1. Import this `.ipynb` into the same Databricks folder and select Serverless.
        2. Run all. Defaults use `workspace.gpu_ops_hackathon`, the first run ID,
           and replay time `2026-11-14T09:07:30Z`.
        3. Inspect the final active alerts and the explicit unknown/clear counts.
        4. In Catalog, confirm `health_features` and `health_alerts`.

        Widget parameters also work when this notebook is a Databricks Job task.
        To demonstrate another replay instant, rerun with another `as_of_ts`.
        Scheduling this fixed timestamp repeats the same snapshot; it does NOT
        create a live stream. A simulator/collector must advance the cursor first.

        Only the two derived tables are updated. Original evidence remains intact.
        Rerunning the same key refreshes that snapshot rather than duplicating it.
        Rules emit `active`, `clear` or `unknown` for every GPU. A clear evaluation
        is not a promise that the hardware is healthy, and snapshot records are
        not an incident lifecycle or evidence of an autonomous repair.
        """),
        cell("code", "configuration", """
        from datetime import datetime, timedelta, timezone
        import re

        dbutils.widgets.text("catalog", "workspace")
        dbutils.widgets.text("schema", "gpu_ops_hackathon")
        dbutils.widgets.text("run_id", "")
        dbutils.widgets.text("as_of_ts", "2026-11-14T09:07:30Z")

        CATALOG = dbutils.widgets.get("catalog")
        SCHEMA = dbutils.widgets.get("schema")
        for value in (CATALOG, SCHEMA):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError("Catalog and schema must contain letters, digits and underscores.")
        spark.conf.set("spark.sql.session.timeZone", "UTC")
        spark.sql(f"USE CATALOG `{CATALOG}`")
        spark.sql(f"USE SCHEMA `{SCHEMA}`")

        runs = spark.sql("SELECT run_id, MIN(event_ts) AS start_ts, MAX(event_ts) AS end_ts FROM telemetry GROUP BY run_id ORDER BY run_id LIMIT 100").collect()
        if not runs:
            raise ValueError("No telemetry exists. Run notebook 01 first.")
        RUN_ID = dbutils.widgets.get("run_id").strip() or runs[0]["run_id"]
        run = next((row for row in runs if row["run_id"] == RUN_ID), None)
        if run is None:
            raise ValueError("run_id was not found in the first 100 replay runs.")
        AS_OF = datetime.fromisoformat(dbutils.widgets.get("as_of_ts").replace("Z", "+00:00"))
        if AS_OF.tzinfo is None:
            raise ValueError("as_of_ts must include a timezone, for example 2026-11-14T09:07:30Z.")
        AS_OF = AS_OF.astimezone(timezone.utc)
        # Spark TIMESTAMP values collected in this UTC session are naive datetimes.
        if not run["start_ts"].replace(tzinfo=timezone.utc) <= AS_OF <= run["end_ts"].replace(tzinfo=timezone.utc):
            raise ValueError("as_of_ts is outside this run's recorded replay interval.")
        WINDOW_START = AS_OF - timedelta(seconds=60)
        print(f"Processing: {CATALOG}.{SCHEMA}; run_id={RUN_ID}; as_of_ts={AS_OF.isoformat()}")
        """),
        cell("markdown", "feature-explanation", """
        ## Feature pipeline

        Spark SQL joins GPU inventory, GPU telemetry, workload progress and cooling
        telemetry at one replay cursor. It derives power relative to each device's
        configured limit, temperature headroom and recent counter/progress changes.
        Missing or stale streams produce NULL/unknown. A 60-second counter window
        requires at least 55 seconds of coverage, no gap over 15 seconds, and no
        counter reset. Old cumulative errors do not remain active new-error alerts.

        This is a bounded demo query, not an optimised production ingestion pipeline.
        """),
        cell("code", "rules", rule_source),
        cell("code", "compute-features", f"""FEATURE_SQL = {feature_sql!r}
features = spark.sql(FEATURE_SQL, args={{"run_id": RUN_ID, "as_of_ts": AS_OF, "window_start_ts": WINDOW_START, "rule_version": RULE_VERSION}})
feature_rows = features.limit(25).collect()
if not feature_rows or len(feature_rows) > 24:
    raise ValueError("Expected 1–24 GPUs for this bounded demo. Increase backend result limits before scaling the fixture.")
if len({{row['gpu_id'] for row in feature_rows}}) != len(feature_rows):
    raise ValueError("Duplicate GPU IDs in feature output.")
display(features)
"""),
        cell("code", "evaluate-alerts", """
        from pyspark.sql.types import StructType, StructField, StringType, TimestampType

        alert_fields = ["alert_id", "run_id", "as_of_ts", "gpu_id", "rack_id", "vendor", "rule_id", "rule_version", "status", "severity", "summary", "evidence_json"]
        alert_schema = StructType([StructField(name, TimestampType() if name == "as_of_ts" else StringType(), False) for name in alert_fields])
        alert_rows = [alert for row in feature_rows for alert in evaluate_features(row.asDict(recursive=True))]
        if len(alert_rows) != len(feature_rows) * len(RULE_IDS):
            raise ValueError("Missing rule evaluations.")
        if len({row["alert_id"] for row in alert_rows}) != len(alert_rows):
            raise ValueError("Duplicate alert IDs.")
        alerts = spark.createDataFrame(alert_rows, schema=alert_schema)
        """),
        cell("code", "merge-derived-tables", """
        from uuid import uuid4

        # Each Delta MERGE is atomic. These two tables are separate transactions.
        # API consumers read health_alerts, whose entire snapshot is one MERGE.
        def merge_snapshot(frame, table_name, key_fields):
            qualified = f"`{CATALOG}`.`{SCHEMA}`.`{table_name}`"
            view = "gpu_health_" + uuid4().hex
            frame.createOrReplaceTempView(view)
            try:
                spark.sql(f"CREATE TABLE IF NOT EXISTS {qualified} USING DELTA AS SELECT * FROM `{view}` WHERE FALSE").collect()
                existing = spark.table(qualified)
                if sorted(existing.dtypes) != sorted(frame.dtypes):
                    raise ValueError(f"Existing {table_name} schema differs. Use a new derived-table name/version; do not overwrite source data.")
                condition = " AND ".join(f"target.`{key}` = source.`{key}`" for key in key_fields)
                spark.sql(f"MERGE INTO {qualified} AS target USING `{view}` AS source ON {condition} WHEN MATCHED THEN UPDATE SET * WHEN NOT MATCHED THEN INSERT *").collect()
            finally:
                spark.catalog.dropTempView(view)

        feature_keys = ["run_id", "as_of_ts", "gpu_id", "rule_version"]
        merge_snapshot(features, "health_features", feature_keys)
        merge_snapshot(alerts, "health_alerts", feature_keys + ["rule_id"])
        print("Stored health_features and health_alerts. Existing evidence tables were not changed.")
        """),
        cell("code", "verify-and-handoff", """
        parameters = {"run_id": RUN_ID, "as_of_ts": AS_OF, "rule_version": RULE_VERSION}
        persisted_features = spark.sql("SELECT * FROM health_features WHERE run_id = :run_id AND as_of_ts = :as_of_ts AND rule_version = :rule_version", args=parameters)
        persisted_alerts = spark.sql("SELECT * FROM health_alerts WHERE run_id = :run_id AND as_of_ts = :as_of_ts AND rule_version = :rule_version", args=parameters)
        if persisted_features.count() != len(feature_rows) or persisted_alerts.count() != len(alert_rows):
            raise ValueError("Persisted snapshot row counts differ from this calculation.")
        print(f"Validated {len(feature_rows)} features and {len(alert_rows)} rule evaluations.")
        display(persisted_alerts.groupBy("status", "severity").count().orderBy("status", "severity"))
        display(persisted_alerts.where("status = 'active'").orderBy("gpu_id", "rule_id"))
        print(f"Website/model replay selection: run_id={RUN_ID}, as_of_ts={AS_OF.isoformat()}")
        """),
        cell("markdown", "handoff", """
        ## Connect the website and model

        Use the exact run ID and timestamp printed above to read `health_alerts`
        through the local backend. An empty result means this snapshot has not been
        processed; it does not mean that zero problems were detected. `clear` and
        `unknown` rows allow the website to distinguish those outcomes explicitly.

        Give active observations, their `evidence_json`, GPU/rack IDs and the fixed
        replay cursor to the investigating model. It can retrieve the corresponding
        GPU history and cooling/workload context. Its output should state the evidence,
        uncertainty and recommended action. The baseline alerts are not model diagnoses.

        To use a Databricks Job, add this notebook as a notebook task and pass the
        widget parameters `catalog`, `schema`, `run_id`, `as_of_ts`. Start with a manual
        run. A changing replay cursor or real ingestion must be supplied before a
        repeating job can honestly be described as continuous monitoring.
        """),
    ]
    return {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.10"}}, "nbformat": 4, "nbformat_minor": 5}


if __name__ == "__main__":
    destination = ROOT / "notebooks/02_gpu_health_pipeline.ipynb"
    destination.write_text(json.dumps(build_notebook(), indent=2) + "\n")
    print(destination)
