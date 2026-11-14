"""SDK response-contract tests. Never authenticates or contacts a workspace."""

import unittest
from unittest.mock import MagicMock, patch

from databricks.sdk.service.sql import ResultData, StatementResponse

from backend.databricks_data import DataQueryError, DataQueryTimeout, DataSettings, DatabricksData


CURSOR = "2026-11-14T09:05:00Z"


def pending(state="PENDING"):
    return StatementResponse.from_dict({"statement_id": "statement-test", "status": {"state": state}})


def success(columns=None, rows=None, **manifest_changes):
    columns = columns if columns is not None else [("gpu_id", "STRING")]
    rows = rows if rows is not None else [["gpu-a"]]
    manifest = {
        "format": "JSON_ARRAY",
        "schema": {"columns": [{"name": name, "type_name": kind, "position": index} for index, (name, kind) in enumerate(columns)]},
        "total_row_count": len(rows), "total_chunk_count": 1, "truncated": False,
    }
    manifest.update(manifest_changes)
    return StatementResponse.from_dict({
        "statement_id": "statement-test", "status": {"state": "SUCCEEDED"},
        "manifest": manifest,
        "result": {"chunk_index": 0, "row_count": len(rows), "row_offset": 0, "data_array": rows},
    })


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class SettingsTests(unittest.TestCase):
    def test_environment_requires_catalog_and_warehouse(self):
        with self.assertRaisesRegex(ValueError, "DATABRICKS_SQL_WAREHOUSE_ID.*DATABRICKS_CATALOG"):
            DataSettings.from_env({})

    def test_defaults_and_overrides(self):
        settings = DataSettings.from_env({"DATABRICKS_SQL_WAREHOUSE_ID": "abc123", "DATABRICKS_CATALOG": "workspace"})
        self.assertEqual((settings.schema, settings.profile), ("gpu_ops_hackathon", "gpu-ops"))
        changed = DataSettings.from_env({
            "DATABRICKS_SQL_WAREHOUSE_ID": "abc123", "DATABRICKS_CATALOG": "workspace",
            "DATABRICKS_SCHEMA": "gpu_ops_v2", "DATABRICKS_CONFIG_PROFILE": "my-profile",
        })
        self.assertEqual((changed.schema, changed.profile), ("gpu_ops_v2", "my-profile"))

    def test_identifiers_reject_sql_and_paths(self):
        for kwargs in ({"catalog": "workspace; DROP TABLE inventory"}, {"schema": "x.y"}, {"warehouse_id": "/sql/1.0/warehouses/abc"}, {"warehouse_id": " "}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DataSettings(**({"warehouse_id": "abc123", "catalog": "workspace"} | kwargs))

    def test_constructing_data_does_not_authenticate(self):
        with patch("databricks.sdk.WorkspaceClient") as constructor:
            DatabricksData(DataSettings("abc123", "workspace"))
        constructor.assert_not_called()


class DatabricksDataTests(unittest.TestCase):
    def setUp(self):
        self.workspace = MagicMock()
        self.api = self.workspace.statement_execution
        self.api.execute_statement.return_value = success()
        self.clock = FakeClock()
        self.data = DatabricksData(
            DataSettings("abc123", "workspace"), workspace_client=self.workspace,
            poll_interval=1, timeout_seconds=3,
            monotonic=self.clock.monotonic, sleep=self.clock.sleep,
        )

    def test_pending_running_then_success(self):
        self.api.execute_statement.return_value = pending()
        self.api.get_statement.side_effect = [pending("RUNNING"), success()]
        self.assertEqual(self.data.overview("run-a", CURSOR), [{"gpu_id": "gpu-a"}])
        self.assertEqual(self.api.get_statement.call_count, 2)
        self.api.get_statement.assert_called_with(statement_id="statement-test")
        self.api.cancel_execution.assert_not_called()

    def test_bound_parameters_do_not_enter_sql_text(self):
        malicious_id = "gpu-a'; DROP TABLE telemetry; --"
        self.data.gpu_history("run-a", malicious_id, "2026-11-14T09:00:00Z", CURSOR)
        kwargs = self.api.execute_statement.call_args.kwargs
        self.assertNotIn(malicious_id, kwargs["statement"])
        self.assertIn("gpu_id = :gpu_id", kwargs["statement"])
        self.assertEqual({p.name: (p.value, p.type) for p in kwargs["parameters"]}, {
            "run_id": ("run-a", "STRING"), "gpu_id": (malicious_id, "STRING"),
            "window_start_ts": ("2026-11-14T09:00:00Z", "TIMESTAMP"), "as_of_ts": (CURSOR, "TIMESTAMP"),
        })
        self.assertEqual(kwargs["wait_timeout"], "0s")
        self.assertEqual(kwargs["format"].value, "JSON_ARRAY")
        self.assertEqual(kwargs["disposition"].value, "INLINE")
        self.assertEqual((kwargs["catalog"], kwargs["schema"]), ("workspace", "gpu_ops_hackathon"))

    def test_manifest_types_become_native_json_values(self):
        self.api.execute_statement.return_value = success(
            [("power_w", "DOUBLE"), ("ecc", "LONG"), ("observed", "BOOLEAN"), ("throttled", "BOOLEAN"), ("missing", "DOUBLE"), ("event_ts", "TIMESTAMP")],
            [["301.5", "9007199254740993", "true", "false", None, "2026-11-14T09:05:00.000Z"]],
        )
        self.assertEqual(self.data.overview("run-a", CURSOR), [{
            "power_w": 301.5, "ecc": 9007199254740993, "observed": True,
            "throttled": False, "missing": None, "event_ts": "2026-11-14T09:05:00.000Z",
        }])

    def test_invalid_boolean_and_nonfinite_number_are_rejected(self):
        for kind, value in (("BOOLEAN", "0"), ("BOOLEAN", "unknown"), ("DOUBLE", "NaN"), ("DOUBLE", "Infinity"), ("LONG", "1.2")):
            with self.subTest(kind=kind, value=value):
                self.api.execute_statement.return_value = success([("value", kind)], [[value]])
                with self.assertRaisesRegex(DataQueryError, "invalid"):
                    self.data.list_runs()

    def test_terminal_failure_and_closed_results_are_not_empty_successes(self):
        for state in ("FAILED", "CANCELED", "CLOSED"):
            with self.subTest(state=state):
                self.api.execute_statement.return_value = pending(state)
                with self.assertRaisesRegex(DataQueryError, state.lower()):
                    self.data.list_runs()
        self.api.get_statement.assert_not_called()

    def test_timeout_requests_cancellation_and_raises(self):
        self.api.execute_statement.return_value = pending()
        self.api.get_statement.return_value = pending("RUNNING")
        with self.assertRaises(DataQueryTimeout):
            self.data.list_runs()
        self.api.cancel_execution.assert_called_once_with(statement_id="statement-test")
        self.assertEqual(self.clock.now, 3)

    def test_cancellation_failure_does_not_hide_timeout(self):
        self.api.execute_statement.return_value = pending()
        self.api.get_statement.return_value = pending("RUNNING")
        self.api.cancel_execution.side_effect = RuntimeError("secret from SDK")
        with self.assertRaises(DataQueryTimeout) as caught:
            self.data.list_runs()
        self.assertNotIn("secret", str(caught.exception))

    def test_transport_errors_do_not_leak_sdk_details(self):
        self.api.execute_statement.side_effect = RuntimeError("Authorization: Bearer SECRET")
        with self.assertRaisesRegex(DataQueryError, "OAuth") as caught:
            self.data.list_runs()
        self.assertNotIn("SECRET", str(caught.exception))

    def test_truncated_and_excessive_results_are_rejected(self):
        for overrides in ({"truncated": True}, {"total_row_count": 1001}):
            with self.subTest(overrides=overrides):
                self.api.execute_statement.return_value = success(**overrides)
                with self.assertRaises(DataQueryError):
                    self.data.list_runs()

    def test_result_chunks_are_collected_and_decoded(self):
        first = success(rows=[["gpu-a"]], total_row_count=2, total_chunk_count=2)
        first.result.next_chunk_index = 1
        first.result.next_chunk_internal_link = "/api/2.0/sql/statements/statement-test/result/chunks/1"
        self.api.execute_statement.return_value = first
        self.api.get_statement_result_chunk_n.return_value = ResultData.from_dict({
            "chunk_index": 1, "row_count": 1, "row_offset": 1, "data_array": [["gpu-b"]],
        })
        self.assertEqual(self.data.list_runs(), [{"gpu_id": "gpu-a"}, {"gpu_id": "gpu-b"}])
        self.api.get_statement_result_chunk_n.assert_called_once_with(statement_id="statement-test", chunk_index=1)

    def test_missing_chunk_fails_instead_of_returning_partial_data(self):
        self.api.execute_statement.return_value = success(total_row_count=2, total_chunk_count=2)
        with self.assertRaisesRegex(DataQueryError, "incomplete"):
            self.data.list_runs()

    def test_cyclic_chunk_pagination_fails(self):
        first = success()
        first.result.next_chunk_index = 0
        self.api.execute_statement.return_value = first
        with self.assertRaisesRegex(DataQueryError, "pagination"):
            self.data.list_runs()

    def test_empty_result_stays_empty(self):
        self.api.execute_statement.return_value = success(rows=[])
        self.assertEqual(self.data.list_runs(), [])

    def test_timestamps_require_timezone_and_history_requires_order(self):
        with self.assertRaises(ValueError):
            self.data.overview("run-a", "2026-11-14T09:05:00")
        with self.assertRaises(ValueError):
            self.data.workload_history("run-a", "gpu-a", "2026-11-14T09:06:00Z", CURSOR)
        self.api.execute_statement.assert_not_called()

    def test_timestamp_offsets_normalize_to_utc_without_losing_microseconds(self):
        self.data.overview("run-a", "2026-11-14T10:05:00.123456+01:00")
        parameters = self.api.execute_statement.call_args.kwargs["parameters"]
        self.assertEqual({p.name: p.value for p in parameters}["as_of_ts"], "2026-11-14T09:05:00.123456Z")

    def test_health_alerts_binds_run_and_exact_cursor_without_fallback(self):
        self.api.execute_statement.return_value = success(rows=[])
        suspicious_run = "run-a' OR 1=1 --"
        self.assertEqual(self.data.health_alerts(suspicious_run, CURSOR), [])
        self.api.execute_statement.assert_called_once()
        kwargs = self.api.execute_statement.call_args.kwargs
        self.assertNotIn(suspicious_run, kwargs["statement"])
        self.assertEqual({p.name: (p.value, p.type) for p in kwargs["parameters"]}, {
            "run_id": (suspicious_run, "STRING"), "as_of_ts": (CURSOR, "TIMESTAMP"),
        })

    def test_missing_health_table_is_an_error_not_an_empty_result(self):
        self.api.execute_statement.return_value = pending("FAILED")
        with self.assertRaisesRegex(DataQueryError, "failed"):
            self.data.health_alerts("run-a", CURSOR)
        self.api.execute_statement.assert_called_once()

    def test_all_public_methods_use_the_packaged_query_and_scope(self):
        calls = [
            (lambda: self.data.list_runs(), "GROUP BY run_id", set()),
            (lambda: self.data.overview("run-a", CURSOR), "FROM telemetry", {"run_id", "as_of_ts"}),
            (lambda: self.data.health_alerts("run-a", CURSOR), "FROM health_alerts", {"run_id", "as_of_ts"}),
            (lambda: self.data.rack_context("run-a", "rack-a", CURSOR), "rack_id = :rack_id", {"run_id", "rack_id", "as_of_ts"}),
            (lambda: self.data.gpu_history("run-a", "gpu-a", "2026-11-14T09:00:00Z", CURSOR), "FROM telemetry", {"run_id", "gpu_id", "window_start_ts", "as_of_ts"}),
            (lambda: self.data.workload_history("run-a", "gpu-a", "2026-11-14T09:00:00Z", CURSOR), "FROM workloads", {"run_id", "gpu_id", "window_start_ts", "as_of_ts"}),
        ]
        for call, sql_fragment, names in calls:
            with self.subTest(sql_fragment=sql_fragment):
                call()
                kwargs = self.api.execute_statement.call_args.kwargs
                self.assertIn(sql_fragment, kwargs["statement"])
                self.assertEqual({p.name for p in kwargs["parameters"]}, names)


if __name__ == "__main__":
    unittest.main()
