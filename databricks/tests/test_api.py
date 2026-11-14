"""HTTP contract tests; no Databricks authentication or network access."""
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from backend.app import app, get_data
from backend.databricks_data import DataQueryError, DataQueryTimeout

RUN = "c573ec80-c960-451a-92b3-a177f8ef4005"
PARAMS = {"run_id": RUN, "as_of_ts": "2026-11-14T09:07:30Z"}


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.data = MagicMock()
        app.dependency_overrides[get_data] = lambda: self.data
        self.client = TestClient(app)

    def tearDown(self):
        app.dependency_overrides.clear()

    def test_overview_preserves_missing_values_and_marks_synthetic(self):
        self.data.overview.return_value = [{"total_gpu_count": 24, "observed_gpu_power_kw": None}]
        response = self.client.get("/api/overview", params=PARAMS)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIsNone(body["data"]["observed_gpu_power_kw"])
        self.assertTrue(body["synthetic"])
        self.assertEqual(body["as_of_ts"], PARAMS["as_of_ts"])

    def test_gpu_history_uses_one_cursor_and_bounded_window(self):
        self.data.gpu_history.return_value = []
        response = self.client.get("/api/gpus/gpu-001/history", params=PARAMS | {"window_minutes": 3})
        self.assertEqual(response.status_code, 200)
        self.data.gpu_history.assert_called_once_with(RUN, "gpu-001", datetime(2026, 11, 14, 9, 4, 30, tzinfo=timezone.utc), datetime(2026, 11, 14, 9, 7, 30, tzinfo=timezone.utc))

    def test_invalid_parameters_never_query_source(self):
        for overrides in ({"as_of_ts": "2026-11-14T09:07:30"}, {"run_id": "bad"}, {"window_minutes": 16}):
            with self.subTest(overrides=overrides):
                response = self.client.get("/api/gpus/gpu-001/history", params=PARAMS | overrides)
                self.assertEqual(response.status_code, 422)
        self.data.gpu_history.assert_not_called()

    def test_source_errors_are_not_replaced_with_fixture_data(self):
        for error, status in ((DataQueryError("Query failed."), 502), (DataQueryTimeout("Query timed out."), 504)):
            self.data.overview.side_effect = error
            response = self.client.get("/api/overview", params=PARAMS)
            self.assertEqual(response.status_code, status)
            self.assertNotIn("data", response.json())

    def test_healthz_is_not_a_databricks_connection_check(self):
        self.assertEqual(self.client.get("/healthz").json()["databricks_connection"], "not_checked")
        self.assertEqual(self.data.mock_calls, [])

    def test_missing_assessment_is_not_a_healthy_verdict(self):
        self.data.health_alerts.return_value = []
        response = self.client.get("/api/health-assessments", params=PARAMS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["assessment_available"])
        self.data.health_alerts.assert_called_once_with(RUN, datetime(2026, 11, 14, 9, 7, 30, tzinfo=timezone.utc))

    def test_assessment_counts_rules_and_preserves_unknown(self):
        self.data.health_alerts.return_value = [{"status": "active"}, {"status": "clear"}, {"status": "unknown"}]
        body = self.client.get("/api/health-assessments", params=PARAMS).json()
        self.assertTrue(body["assessment_available"])
        self.assertEqual(body["counts"], {"active": 1, "clear": 1, "unknown": 1})

    def test_browser_cors_allows_local_frontend(self):
        for origin in ("http://localhost:3000", "http://127.0.0.1:4397"):
            response = self.client.get("/healthz", headers={"Origin": origin})
            self.assertEqual(response.headers["access-control-allow-origin"], origin)
        rejected = self.client.get("/healthz", headers={"Origin": "https://unconfigured.example"})
        self.assertNotIn("access-control-allow-origin", rejected.headers)


if __name__ == "__main__":
    unittest.main()
