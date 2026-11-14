"""Connection diagnostic behavior without Databricks authentication."""

from contextlib import redirect_stderr, redirect_stdout
import io
import json
import unittest
from unittest.mock import MagicMock, patch

from backend.check_connection import connection_report, main
from backend.databricks_data import DataQueryError


class ConnectionCheckTests(unittest.TestCase):
    def setUp(self):
        self.data = MagicMock()
        self.data.list_runs.return_value = [{
            "run_id": "run-a", "start_ts": "2026-11-14T09:00:00Z",
            "end_ts": "2026-11-14T09:14:55Z", "sample_count": 4320,
        }]
        self.data.overview.return_value = [{"total_gpu_count": 24, "observed_gpu_power_kw": 4.8}]

    def test_checks_overview_at_seven_minutes_thirty_seconds(self):
        report = connection_report(self.data)
        self.data.overview.assert_called_once_with("run-a", "2026-11-14T09:07:30Z")
        self.assertEqual(report["source"], "databricks")
        self.assertEqual(report["run_count"], 1)
        self.assertEqual(report["overview"]["total_gpu_count"], 24)

    def test_caps_cursor_at_end_and_accepts_utc_sql_timestamp(self):
        self.data.list_runs.return_value[0]["start_ts"] = "2026-11-14 09:00:00.000"
        self.data.list_runs.return_value[0]["end_ts"] = "2026-11-14T09:01:00Z"
        connection_report(self.data)
        self.data.overview.assert_called_once_with("run-a", "2026-11-14T09:01:00Z")

    def test_empty_runs_fail_clearly(self):
        self.data.list_runs.return_value = []
        with self.assertRaisesRegex(DataQueryError, "no replay rows"):
            connection_report(self.data)
        self.data.overview.assert_not_called()

    def test_main_prints_json_and_returns_zero(self):
        output = io.StringIO()
        with patch("backend.check_connection.load_dotenv"), patch("backend.check_connection.DataSettings.from_env"), patch("backend.check_connection.DatabricksData", return_value=self.data), redirect_stdout(output):
            self.assertEqual(main(), 0)
        self.assertEqual(json.loads(output.getvalue())["status"], "connected")

    def test_unexpected_errors_do_not_leak_secrets_or_tracebacks(self):
        output = io.StringIO()
        with patch("backend.check_connection.load_dotenv"), patch("backend.check_connection.DataSettings.from_env", side_effect=RuntimeError("SECRET")), redirect_stderr(output):
            self.assertEqual(main(), 1)
        self.assertNotIn("SECRET", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())


if __name__ == "__main__":
    unittest.main()
