"""Lakehouse control-plane checks. Never authenticates or contacts a workspace."""
from datetime import datetime, timedelta, timezone
import io
import json
import math
import os
from pathlib import Path
import re
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

from databricks.sdk.service.sql import StatementResponse

from backend import lakehouse
from backend.databricks_data import DataQueryError, DataQueryTimeout, DataSettings, DatabricksData
from backend.lakehouse import (
    EXPERIMENTS_FILE, LAKEHOUSE_DIR, RUN_FIELDS, RUN_KEYS, Lakehouse, LakehouseError,
    eval_schema, governance_statements, ground_truth_rows, packaged_statements, plan_cursors,
    render, validate_experiments,
)


SETTINGS = DataSettings("abc123", "workspace")
ROOT = Path(__file__).resolve().parents[1]
RUN = "43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c"
EVIDENCE = "`workspace`.`gpu_ops_hackathon`"
EVALUATION = "`workspace`.`gpu_ops_hackathon_eval`"


def statements():
    return dict(packaged_statements(SETTINGS))


def response(state="SUCCEEDED", **extra):
    return StatementResponse.from_dict({"statement_id": "statement-1", "status": {"state": state, **extra}})


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class StatementPackageTests(unittest.TestCase):
    def test_every_file_renders_to_one_create_statement(self):
        rendered = packaged_statements(SETTINGS)
        self.assertEqual([name for name, _ in rendered], sorted(path.stem for path in LAKEHOUSE_DIR.glob("*.sql")))
        for name, sql in rendered:
            # Leading SQL comments are kept; the statement itself must be a CREATE.
            self.assertTrue(lakehouse._without_literals(sql).lstrip().startswith("CREATE "), name)
            self.assertNotIn("${", sql, name)
            self.assertTrue(lakehouse._is_single_statement(sql), name)

    def test_placeholders_become_quoted_validated_schemas(self):
        sql = statements()
        self.assertIn(f"{EVIDENCE}.telemetry", sql["32_tool_gpu_history"])
        self.assertIn(f"{EVALUATION}.ground_truth", sql["41_eval_ground_truth"])
        with self.assertRaises(ValueError):
            eval_schema(DataSettings("abc123", "workspace", schema="s" * 252))

    def test_render_rejects_unknown_placeholders_extra_statements_and_destructive_sql(self):
        for text in (
            "CREATE OR REPLACE VIEW ${evidence}.v AS SELECT ${other}",
            "CREATE OR REPLACE VIEW ${evidence}.v AS SELECT 1; DROP TABLE ${evidence}.telemetry",
            "DROP TABLE ${evidence}.telemetry",
            "INSERT OVERWRITE ${evidence}.telemetry SELECT * FROM ${evidence}.telemetry",
            "CREATE OR REPLACE VIEW ${evidence}.v AS SELECT * FROM a WHERE x IN (DELETE)",
            "CREATE OR REPLACE VIEW ${evidence}.v AS SELECT 'unterminated",
        ):
            with self.assertRaises(LakehouseError, msg=text):
                render(text, SETTINGS)

    def test_keywords_and_semicolons_inside_text_are_not_statements(self):
        sql = render("CREATE OR REPLACE VIEW ${evidence}.v COMMENT 'Never grant or drop; it''s fine' AS SELECT 1 -- no delete; here", SETTINGS)
        self.assertIn("it''s fine", sql)

    def test_agent_tools_are_cursor_bound_and_never_read_answer_keys(self):
        tools = {name: sql for name, sql in statements().items() if "_tool_" in name}
        self.assertEqual(len(tools), 8)
        for name, sql in tools.items():
            self.assertIn("CREATE OR REPLACE FUNCTION", sql, name)
            self.assertNotIn("_eval", sql, name)
            self.assertNotIn("ground_truth", sql.lower(), name)
            reads_samples = re.search(r"\.(telemetry|workloads|cooling)\b", sql)
            # Run discovery returns only each replay's recorded interval, as the website does.
            if reads_samples and name != "30_tool_list_runs":
                self.assertIn("event_ts <= p_as_of_ts", sql, name)
            if "window_minutes" in sql:
                self.assertIn("LEAST(GREATEST(COALESCE(p_window_minutes, 5), 1), 15)", sql, name)
                self.assertIn("LIMIT 180", sql, name)
        self.assertIn("as_of_ts <= p_as_of_ts", tools["37_tool_detector_incidents"])

    def test_evidence_objects_never_reference_the_evaluation_schema(self):
        for name, sql in statements().items():
            header = sql.split("(", 1)[0].split("\n", 1)[0]
            if EVIDENCE + "." in header:
                self.assertNotIn("gpu_ops_hackathon_eval", sql, name)

    def test_governance_changes_metadata_only(self):
        rendered = governance_statements(SETTINGS)
        self.assertTrue(rendered)
        for name, sql in rendered:
            self.assertRegex(sql, r"^(COMMENT ON TABLE |ALTER TABLE )", name)
            if sql.startswith("ALTER TABLE"):
                self.assertRegex(sql, r"(SET TAGS|ALTER COLUMN `[a-z0-9_]+` COMMENT) ", name)
            self.assertNotRegex(sql.upper(), r"\b(DROP|DELETE|INSERT|UPDATE|MERGE|TRUNCATE)\b", name)
        self.assertEqual(lakehouse._quoted("it's"), "'it''s'")


class CursorPlanTests(unittest.TestCase):
    def test_cursors_are_evenly_spaced_and_end_on_the_last_sample(self):
        cursors = plan_cursors("2026-11-14T09:00:00.000Z", "2026-11-14T09:14:55.000Z", 30)
        self.assertEqual(len(cursors), 30)
        self.assertEqual(cursors[0], datetime(2026, 11, 14, 9, 0, 30, tzinfo=timezone.utc))
        self.assertEqual(cursors[-2], datetime(2026, 11, 14, 9, 14, 30, tzinfo=timezone.utc))
        self.assertEqual(cursors[-1], datetime(2026, 11, 14, 9, 14, 55, tzinfo=timezone.utc))
        self.assertTrue(all(a < b for a, b in zip(cursors, cursors[1:])))

    def test_an_exact_multiple_does_not_repeat_the_last_cursor(self):
        start = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
        self.assertEqual(plan_cursors(start, start + timedelta(seconds=60), 30), [start + timedelta(seconds=30), start + timedelta(seconds=60)])

    def test_invalid_steps_and_reversed_intervals_are_rejected(self):
        for step in (0, 7, 905, True, 30.0):
            with self.assertRaises(ValueError, msg=step):
                plan_cursors("2026-11-14T09:00:00Z", "2026-11-14T09:10:00Z", step)
        with self.assertRaises(ValueError):
            plan_cursors("2026-11-14T09:10:00Z", "2026-11-14T09:00:00Z", 30)


class GroundTruthTests(unittest.TestCase):
    def test_answer_key_matches_the_bundled_replays(self):
        rows = {row["scenario"]: row for row in ground_truth_rows()}
        manifest = json.loads((ROOT / "demo" / "manifest.json").read_text())
        self.assertEqual({row["run_id"] for row in rows.values()}, {run["run_id"] for run in manifest["runs"]})
        self.assertEqual((rows["cooling_fault"]["expected_root_entity"], len(rows["cooling_fault"]["affected_gpu_ids"])), ("zone-b", 8))
        self.assertEqual((rows["healthy_load"]["expected_flag"], rows["healthy_load"]["expected_root_entity"]), (False, None))
        self.assertEqual(rows["telemetry_gap"]["expected_root_entity"], "gpu-017")
        self.assertEqual(rows["isolated_error"]["expected_root_entity"], "gpu-001")
        self.assertTrue(all(row["event_start_ts"] == "2026-11-14T09:05:00Z" for row in rows.values()))

    def test_answer_key_table_lives_only_in_the_evaluation_schema(self):
        sql = statements()
        self.assertIn(f"{EVALUATION}.ground_truth", sql["41_eval_ground_truth"])
        self.assertIn("Never grant this schema to an agent", sql["40_eval_schema"])


def experiment_document(**changes):
    run = {name: None for name, _ in RUN_FIELDS}
    run.update(scenario="network", seed=0, policy="baseline", policy_kind="heuristic", origin="simulated",
               twin_schema_version="1.0.0", minutes=2, training_samples_per_second=400.0, training_samples=48000,
               inference_tokens_per_second=3000.0, average_p95_ms=120.0, slo_met_minutes=2,
               energy_kwh=1.0, cost_gbp=0.2, samples_per_kwh=48000.0)
    run.update(changes)
    point = {"scenario": "network", "seed": 0, "policy": "baseline", "samples_per_second": 400.0, "p95_ms": 120.0,
             "tokens_per_second": 3000.0, "power_kw": 30.0, "price_per_kwh": 0.17, "energy_kwh": 0.5}
    return {"origin": "simulated", "runs": [run], "timeline": [{**point, "minute": 0}, {**point, "minute": 1}]}


class ExperimentTests(unittest.TestCase):
    def test_committed_twin_export_validates(self):
        runs, timeline = validate_experiments(json.loads(EXPERIMENTS_FILE.read_text(encoding="utf-8")))
        self.assertEqual((len(runs), len(timeline)), (88, 2112))
        self.assertTrue(all(run["constraints_passed"] is True for run in runs if run["policy"] == "coordinated"))
        self.assertTrue(all(run["constraints_passed"] is None for run in runs if run["policy"] == "baseline"))

    def test_minimal_document_validates(self):
        runs, timeline = validate_experiments(experiment_document())
        self.assertEqual((len(runs), len(timeline)), (1, 2))

    def test_rejects_unknown_values_duplicates_gaps_and_non_finite_numbers(self):
        bad = [
            experiment_document(scenario="cosmic"),
            experiment_document(origin="measured"),
            experiment_document(policy_kind="oracle"),
            experiment_document(policy="drop table"),
            experiment_document(energy_kwh=math.nan),
            experiment_document(seed=True),
            experiment_document(plan_json="{not json"),
        ]
        duplicate = experiment_document()
        duplicate["runs"].append(dict(duplicate["runs"][0]))
        gap = experiment_document()
        gap["timeline"].pop()
        extra = experiment_document()
        extra["runs"][0]["secret"] = "x"
        stray = experiment_document()
        stray["timeline"][1]["minute"] = 5
        for document in bad + [duplicate, gap, extra, stray, {"origin": "simulated"}]:
            with self.assertRaises(LakehouseError):
                validate_experiments(document)

    def test_merge_binds_rows_as_one_parameter(self):
        sql = lakehouse._merge_statement("`c`.`s`.`policy_runs`", RUN_FIELDS, RUN_KEYS, True)
        self.assertIn("from_json(:payload,", sql)
        self.assertIn("ON target.scenario = source.scenario AND target.seed = source.seed AND target.policy = source.policy", sql)
        self.assertIn("current_timestamp() AS loaded_at", sql)
        self.assertNotIn(";", sql)


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self.workspace = MagicMock()
        self.api = self.workspace.statement_execution
        self.clock = Clock()
        self.lakehouse = Lakehouse(DatabricksData(SETTINGS, workspace_client=self.workspace), timeout_seconds=3,
                                   monotonic=self.clock.monotonic, sleep=self.clock.sleep)

    def test_waits_for_success_and_binds_parameters(self):
        self.api.execute_statement.return_value = response("PENDING")
        self.api.get_statement.return_value = response()
        self.assertEqual(self.lakehouse.execute("CREATE OR REPLACE VIEW v AS SELECT 1", {"x": ("1", "STRING")}), [])
        call = self.api.execute_statement.call_args.kwargs
        self.assertEqual((call["warehouse_id"], call["wait_timeout"]), ("abc123", "0s"))
        self.assertEqual([(p.name, p.value, p.type) for p in call["parameters"]], [("x", "1", "STRING")])

    def test_failed_statement_reports_the_sql_error_without_urls(self):
        self.api.execute_statement.return_value = response("FAILED", error={"message": "[TABLE_OR_VIEW_NOT_FOUND] see https://example.cloud.databricks.com/x?o=1"})
        with self.assertRaises(LakehouseError) as caught:
            self.lakehouse.execute("CREATE OR REPLACE VIEW v AS SELECT 1")
        self.assertIn("TABLE_OR_VIEW_NOT_FOUND", str(caught.exception))
        self.assertNotIn("example.cloud", str(caught.exception))

    def test_timeout_requests_cancellation(self):
        self.api.execute_statement.return_value = response("PENDING")
        self.api.get_statement.return_value = response("RUNNING")
        with self.assertRaises(DataQueryTimeout):
            self.lakehouse.execute("CREATE OR REPLACE VIEW v AS SELECT 1")
        self.api.cancel_execution.assert_called_with(statement_id="statement-1")

    def test_sdk_errors_are_wrapped_without_their_text(self):
        self.api.execute_statement.side_effect = RuntimeError("Bearer secret-token")
        with self.assertRaises(LakehouseError) as caught:
            self.lakehouse.execute("CREATE OR REPLACE VIEW v AS SELECT 1")
        self.assertNotIn("secret-token", str(caught.exception))

    def test_fetch_decodes_rows(self):
        self.api.execute_statement.return_value = StatementResponse.from_dict({
            "statement_id": "statement-1", "status": {"state": "SUCCEEDED"},
            "manifest": {"format": "JSON_ARRAY", "total_row_count": 1, "total_chunk_count": 1, "truncated": False,
                         "schema": {"columns": [{"name": "run_id", "type_name": "STRING", "position": 0},
                                                {"name": "saved", "type_name": "LONG", "position": 1}]}},
            "result": {"chunk_index": 0, "row_offset": 0, "row_count": 1, "data_array": [[RUN, "3"]]},
        })
        self.assertEqual(self.lakehouse.execute("SELECT 1", fetch=True), [{"run_id": RUN, "saved": 3}])

    def test_fetch_decodes_array_columns_from_json_and_rejects_bad_json(self):
        def result(value):
            return StatementResponse.from_dict({
                "statement_id": "statement-1", "status": {"state": "SUCCEEDED"},
                "manifest": {"format": "JSON_ARRAY", "total_row_count": 1, "total_chunk_count": 1, "truncated": False,
                             "schema": {"columns": [{"name": "affected_gpu_ids", "type_name": "ARRAY", "position": 0}]}},
                "result": {"chunk_index": 0, "row_offset": 0, "row_count": 1, "data_array": [[value]]},
            })
        self.api.execute_statement.return_value = result('["gpu-009","gpu-010"]')
        self.assertEqual(self.lakehouse.execute("SELECT 1", fetch=True), [{"affected_gpu_ids": ["gpu-009", "gpu-010"]}])
        for bad in ("[NaN]", "[not json"):
            self.api.execute_statement.return_value = result(bad)
            with self.assertRaises(DataQueryError):
                self.lakehouse.execute("SELECT 1", fetch=True)


class FakeSource:
    settings = SETTINGS

    def __init__(self, runs):
        self.runs = runs

    def list_runs(self):
        return self.runs


class FakeDetector:
    def __init__(self):
        self.cursors = []

    def snapshot(self, run_id, cursor):
        self.cursors.append((run_id, cursor))
        return {"incidents": []}


class BackfillAndLoaderTests(unittest.TestCase):
    def manifest_runs(self):
        return json.loads((ROOT / "demo" / "manifest.json").read_text())["runs"]

    def test_backfill_skips_current_results_and_reassesses_older_versions(self):
        source = FakeSource([{"run_id": RUN, "start_ts": "2026-11-14T09:00:00.000Z", "end_ts": "2026-11-14T09:02:00.000Z", "sample_count": 25}])
        house = Lakehouse(source)
        house.execute = MagicMock(return_value=[
            {"run_id": RUN, "as_of_ts": "2026-11-14T09:01:00.000Z", "detector_version": "current"},
            {"run_id": RUN, "as_of_ts": "2026-11-14T09:01:10.000Z", "detector_version": "older"},
        ])
        detector = FakeDetector()
        with patch("backend.result_store.detector_version", return_value="current"):
            result = house.backfill(30, detector=detector, emit=lambda *_: None)
        at = lambda seconds: datetime(2026, 11, 14, 9, tzinfo=timezone.utc) + timedelta(seconds=seconds)
        self.assertEqual(detector.cursors, [(RUN, at(30)), (RUN, at(70)), (RUN, at(90)), (RUN, at(120))])
        self.assertEqual((result["saved"], result["already_assessed"]), (4, 1))

    def test_backfill_rejects_unknown_runs(self):
        house = Lakehouse(FakeSource(self.manifest_runs()))
        with self.assertRaises(LakehouseError):
            house.backfill(30, run_ids=["not-a-run"], detector=FakeDetector(), emit=lambda *_: None)

    def test_experiments_merge_in_bounded_parameterised_chunks(self):
        house = Lakehouse(FakeSource([]))
        house.execute = MagicMock(return_value=[])
        self.assertEqual(house.load_experiments(EXPERIMENTS_FILE, emit=lambda *_: None), {"policy_runs": 88, "policy_timeline": 2112})
        calls = house.execute.call_args_list
        self.assertEqual(len(calls), 1 + 5)
        total = 0
        for call in calls:
            sql, params = call.args
            self.assertRegex(sql, r"^MERGE INTO `workspace`\.`gpu_ops_hackathon`\.`policy_(runs|timeline)`")
            self.assertNotIn("network", sql)
            payload, kind = params["payload"]
            self.assertEqual(kind, "STRING")
            rows = json.loads(payload)
            self.assertLessEqual(len(rows), lakehouse.CHUNK_ROWS)
            total += len(rows)
        self.assertEqual(total, 88 + 2112)

    def test_ground_truth_requires_the_same_replays(self):
        house = Lakehouse(FakeSource([{"run_id": "00000000-0000-0000-0000-000000000000"}]))
        house.execute = MagicMock()
        with self.assertRaises(LakehouseError):
            house.load_ground_truth()
        house.execute.assert_not_called()

    def test_ground_truth_binds_null_root_for_the_benign_replay(self):
        house = Lakehouse(FakeSource(self.manifest_runs()))
        house.execute = MagicMock(return_value=[])
        self.assertEqual(house.load_ground_truth(), 4)
        bound = [call.args[1] for call in house.execute.call_args_list]
        benign = next(params for params in bound if params["scenario"][0] == "healthy_load")
        self.assertEqual(benign["expected_root_entity"], (None, "STRING"))
        self.assertEqual(benign["expected_flag"], ("false", "STRING"))
        self.assertTrue(all(call.args[0].startswith(f"MERGE INTO {EVALUATION}.`ground_truth`") for call in house.execute.call_args_list))


class WorkbenchNotebookTests(unittest.TestCase):
    def test_committed_notebook_matches_its_builder_and_only_reads(self):
        from build_lakehouse_notebook import DESTINATION, build_notebook
        built = build_notebook()
        self.assertEqual(json.loads(DESTINATION.read_text()), json.loads(json.dumps(built)))
        code = "\n".join("".join(c["source"]) for c in built["cells"] if c["cell_type"] == "code")
        self.assertNotRegex(code.upper(), r"\b(MERGE|INSERT|DELETE|DROP|UPDATE|TRUNCATE|CREATE|ALTER)\b")
        self.assertNotIn("ground_truth", code)


class CommandTests(unittest.TestCase):
    def test_plan_lists_statements_without_contacting_databricks(self):
        environment = {"DATABRICKS_SQL_WAREHOUSE_ID": "abc123", "DATABRICKS_CATALOG": "workspace"}
        output = io.StringIO()
        with patch.dict(os.environ, environment), patch("dotenv.load_dotenv"), \
                patch("backend.lakehouse.DatabricksData", side_effect=AssertionError("no workspace access in plan")), redirect_stdout(output):
            self.assertEqual(lakehouse.main(["plan"]), 0)
        report = json.loads(output.getvalue())
        self.assertEqual(len(report["statements"]), len(list(LAKEHOUSE_DIR.glob("*.sql"))))
        self.assertGreater(report["governance_statements"], 0)


if __name__ == "__main__":
    unittest.main()
