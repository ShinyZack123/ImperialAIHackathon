#!/usr/bin/env python3
"""Build the lakehouse workbench notebook (03). No cloud calls.

Run `python -m backend.lakehouse apply` (and backfill, ground-truth, experiments)
first; this notebook only reads the objects those commands create.
"""

import ast
import json
from pathlib import Path
import textwrap

ROOT = Path(__file__).resolve().parent
DESTINATION = ROOT / "notebooks" / "03_lakehouse_workbench.ipynb"


def cell(kind, name, source):
    source = textwrap.dedent(source).strip() + "\n"
    result = {"cell_type": kind, "id": name, "metadata": {}, "source": source.splitlines(keepends=True)}
    if kind == "code":
        ast.parse(source)
        result.update(execution_count=None, outputs=[])
    return result


def build_notebook():
    cells = [
        cell("markdown", "intro", """
        # GPU operations lakehouse workbench

        **Synthetic data only.** This notebook reads the objects created by
        `python -m backend.lakehouse` and shows how the platform uses Databricks:

        1. **Escalation funnel**: how much telemetry the deterministic detector absorbs
           before anything reaches a reasoning agent.
        2. **Incident timeline**: official detector incidents at every assessed replay time,
           each with a stable ID and an active/recovered status.
        3. **An investigation with the agent tools**: the same Unity Catalog functions an
           agent calls, run step by step. Each takes the replay time and cannot return a
           later sample.
        4. **Detection scorecard**: the official detector graded against the scenario
           answer key, which lives in a separate schema that agents are never granted.
        5. **Policy ledger**: baseline and coordinated scheduling decisions from the digital
           twin, with their simulated outcomes on identical seeded traces.

        Run all on Serverless. Nothing here writes to a table.
        """),
        cell("code", "configuration", """
        from datetime import datetime, timezone
        import re

        dbutils.widgets.text("catalog", "workspace")
        dbutils.widgets.text("schema", "gpu_ops_hackathon")
        dbutils.widgets.text("run_id", "43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c")
        dbutils.widgets.text("as_of_ts", "2026-11-14T09:10:45Z")

        CATALOG = dbutils.widgets.get("catalog")
        SCHEMA = dbutils.widgets.get("schema")
        for value in (CATALOG, SCHEMA):
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value):
                raise ValueError("Catalog and schema must contain letters, digits and underscores.")
        EVAL = f"`{CATALOG}`.`{SCHEMA}_eval`"
        spark.conf.set("spark.sql.session.timeZone", "UTC")
        spark.sql(f"USE CATALOG `{CATALOG}`")
        spark.sql(f"USE SCHEMA `{SCHEMA}`")

        RUN_ID = dbutils.widgets.get("run_id").strip()
        AS_OF = datetime.fromisoformat(dbutils.widgets.get("as_of_ts").replace("Z", "+00:00"))
        if AS_OF.tzinfo is None:
            raise ValueError("as_of_ts must include a timezone, for example 2026-11-14T09:10:45Z.")
        AS_OF = AS_OF.astimezone(timezone.utc)
        CURSOR = {"run_id": RUN_ID, "as_of_ts": AS_OF}
        print(f"Reading {CATALOG}.{SCHEMA}; investigation replay {RUN_ID} at {AS_OF.isoformat()}")
        """),
        cell("markdown", "funnel-note", """
        ## 1. Escalation funnel

        Evidence rows stored per replay, metric records the detector analysed across all
        assessed replay times, and how many distinct incidents it raised. Only the last
        column needs an agent's attention.
        """),
        cell("code", "funnel", """
        display(spark.sql("SELECT * FROM escalation_funnel ORDER BY run_id"))
        """),
        cell("markdown", "timeline-note", """
        ## 2. Incident timeline

        Every assessed replay time for the selected replay. `incident_id` stays the same
        while the root entity and onset are unchanged. `recovered` needs positive evidence:
        every flagged condition observed clear for five minutes.
        """),
        cell("code", "timeline", """
        display(spark.sql(
            "SELECT as_of_ts, incident_id, status, severity, root_entity, affected_gpu_count, finding_count "
            "FROM incident_timeline WHERE run_id = :run_id ORDER BY as_of_ts, incident_id",
            args={"run_id": RUN_ID}))
        display(spark.sql(
            "SELECT as_of_ts, state, COUNT(*) AS gpus FROM gpu_state_timeline "
            "WHERE run_id = :run_id GROUP BY as_of_ts, state ORDER BY as_of_ts, state",
            args={"run_id": RUN_ID}))
        """),
        cell("markdown", "investigation-note", """
        ## 3. An investigation with the agent tools

        These are the Unity Catalog functions an agent receives. The replay time is a
        parameter of every function, so the agent cannot read the future. Start from what
        the detector escalated, then test the hypothesis against the graph and the raw
        evidence.
        """),
        cell("code", "investigation", """
        incidents = spark.sql("SELECT * FROM tool_detector_incidents(:run_id, :as_of_ts)", args=CURSOR)
        display(incidents)
        top = incidents.first()
        if top is None:
            print("The detector escalated nothing at or before this replay time. Choose a later as_of_ts.")
        else:
            root = top["root_entity"]
            print(f"Hypothesis from the detector: {root} ({top['severity']}, {top['status']}). Not a confirmed cause.")
            radius = spark.sql("SELECT * FROM tool_blast_radius(:entity)", args={"entity": root})
            display(radius)
            if root.startswith("zone-"):
                display(spark.sql("SELECT * FROM tool_cooling_history(:run_id, :zone, :as_of_ts, 10)",
                                  args={**CURSOR, "zone": root}))
            affected = list(top["affected_gpu_ids"] or [])
            if affected:
                gpu = affected[0]
                display(spark.sql("SELECT event_ts, temp_c, power_w, clock_mhz, throttled, heartbeat_ok "
                                  "FROM tool_gpu_history(:run_id, :gpu, :as_of_ts, 10) ORDER BY event_ts",
                                  args={**CURSOR, "gpu": gpu}))
                display(spark.sql("SELECT event_ts, job_id, job_state, progress_units "
                                  "FROM tool_workload_history(:run_id, :gpu, :as_of_ts, 10) ORDER BY event_ts",
                                  args={**CURSOR, "gpu": gpu}))
        """),
        cell("markdown", "scorecard-note", """
        ## 4. Detection scorecard

        The official detector against the generator's answer key. Latency from onset includes
        the detector's 300-second persistence requirement and the 30-second spacing of assessed
        replay times. These are four synthetic scenarios, not a hardware benchmark.
        """),
        cell("code", "scorecard", """
        display(spark.sql(f"SELECT * FROM {EVAL}.detection_scorecard ORDER BY scenario"))
        """),
        cell("markdown", "ledger-note", """
        ## 5. Policy ledger

        The twin replays each incident scenario under a heuristic baseline and a coordinated
        optimiser on identical seeded traces. Results are simulated. The coordinated plan sees
        the whole 24-minute trace in advance and is applied at minute 0 without a migration
        cost. A model's decisions can be appended to `policy_runs` and compared the same way.
        """),
        cell("code", "ledger", """
        display(spark.sql("SELECT * FROM policy_uplift ORDER BY scenario, policy"))
        display(spark.sql("SELECT * FROM policy_leaderboard ORDER BY scenario, policy"))
        display(spark.sql(
            "SELECT minute, policy, samples_per_second, p95_ms, power_kw FROM policy_timeline "
            "WHERE scenario = 'network' AND seed = 0 ORDER BY minute, policy"))
        """),
        cell("markdown", "limits", """
        ## Limits

        All readings, scenarios and limits are synthetic, and recovery in the replays is
        prerecorded rather than caused by an agent. Detector findings describe an analysis
        window, not a confirmed physical cause. Twin results are simulated outcomes, not
        measured savings.
        """),
    ]
    return {"cells": cells, "metadata": {"kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"}, "language_info": {"name": "python", "version": "3.10"}}, "nbformat": 4, "nbformat_minor": 5}


if __name__ == "__main__":
    DESTINATION.write_text(json.dumps(build_notebook(), indent=2) + "\n")
    print(DESTINATION)
