"""Evaluate the exact-snapshot SQL predicate on SQLite fixtures.

This checks selection semantics, not Databricks runtime compatibility.
"""

from pathlib import Path
import sqlite3
import unittest


SQL = (Path(__file__).resolve().parents[1] / "sql" / "health_alerts.sql").read_text()
CURSOR = "2026-11-14T09:05:00Z"


class HealthAlertsQueryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.execute("""CREATE TABLE health_alerts (
            alert_id TEXT, run_id TEXT, as_of_ts TEXT, gpu_id TEXT, rack_id TEXT,
            vendor TEXT, rule_id TEXT, rule_version TEXT, status TEXT,
            severity TEXT, summary TEXT, evidence_json TEXT
        )""")
        self.db.executemany("INSERT INTO health_alerts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", [
            ("active", "run-a", CURSOR, "gpu-001", "rack-a", "NVIDIA", "temperature", "baseline-v1", "active", "warning", "Hot", '{"temp_c": 90}'),
            ("clear", "run-a", CURSOR, "gpu-002", "rack-a", "AMD", "temperature", "baseline-v1", "clear", "info", "Within limit", '{"temp_c": 60}'),
            ("unknown", "run-a", CURSOR, "gpu-003", "rack-a", "Intel", "temperature", "baseline-v1", "unknown", "info", "No reading", '{"temp_c": null}'),
            ("older", "run-a", "2026-11-14T09:04:55Z", "gpu-001", "rack-a", "NVIDIA", "temperature", "baseline-v1", "active", "warning", "Old", '{}'),
            ("future", "run-a", "2026-11-14T09:05:05Z", "gpu-001", "rack-a", "NVIDIA", "temperature", "baseline-v1", "active", "warning", "Future", '{}'),
            ("other-run", "run-b", CURSOR, "gpu-001", "rack-a", "NVIDIA", "temperature", "baseline-v1", "active", "warning", "Other run", '{}'),
            ("other-version", "run-a", CURSOR, "gpu-001", "rack-a", "NVIDIA", "temperature", "baseline-v2", "clear", "info", "Other version", '{}'),
        ])

    def test_exact_run_cursor_version_and_all_statuses(self):
        rows = [dict(row) for row in self.db.execute(SQL, {"run_id": "run-a", "as_of_ts": CURSOR})]
        self.assertEqual([row["alert_id"] for row in rows], ["active", "clear", "unknown"])
        self.assertEqual([row["status"] for row in rows], ["active", "clear", "unknown"])
        self.assertEqual(rows[2]["evidence_json"], '{"temp_c": null}')

    def test_unevaluated_cursor_does_not_substitute_old_snapshot(self):
        self.assertEqual(self.db.execute(SQL, {"run_id": "run-a", "as_of_ts": "2026-11-14T09:05:01Z"}).fetchall(), [])


if __name__ == "__main__":
    unittest.main()
