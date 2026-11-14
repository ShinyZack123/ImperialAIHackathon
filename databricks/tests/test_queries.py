"""Exercise SQL selection/aggregation using SQLite and timestamp UDFs.

This validates the query logic on fixtures; it does NOT certify Databricks
syntax/runtime support. Run the packaged notebook for Databricks validation.
"""

import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path


SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
CURSOR = "2026-11-14T12:00:20Z"


def epoch(value):
    return None if value is None else int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def epoch_micros(value):
    if value is None:
        return None
    delta = datetime.fromisoformat(value.replace("Z", "+00:00")) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


class QueryTests(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.row_factory = sqlite3.Row
        self.db.create_function("unix_timestamp", 1, epoch)
        self.db.create_function("unix_micros", 1, epoch_micros)
        self.db.executescript("""
          CREATE TABLE inventory (gpu_id TEXT, vendor TEXT, model TEXT,
            rack_id TEXT, server_id TEXT, cooling_zone_id TEXT,
            power_limit_w REAL, temperature_limit_c REAL);
          CREATE TABLE telemetry (run_id TEXT, event_ts TEXT, gpu_id TEXT,
            power_w REAL, gpu_util_pct REAL, memory_used_gb REAL,
            memory_total_gb REAL, temp_c REAL, clock_mhz REAL,
            throttled INTEGER, ecc_uncorrected_total INTEGER, heartbeat_ok INTEGER);
          CREATE TABLE workloads (run_id TEXT, event_ts TEXT, gpu_id TEXT,
            job_id TEXT, job_state TEXT, progress_units INTEGER, priority TEXT);
          CREATE TABLE cooling (run_id TEXT, event_ts TEXT, cooling_zone_id TEXT,
            inlet_temp_c REAL, flow_pct REAL);
        """)
        for gpu in ("a", "b", "c", "d", "e"):
            self.db.execute("INSERT INTO inventory VALUES (?, 'NVIDIA', 'Synthetic-NVIDIA', 'rack-a', 'server-a', 'zone-a', 500, 85)", (gpu,))
        # A current reading, stale B, missing heartbeat C, absent D,
        # partially instrumented E, future A, and an unrelated replay run.
        rows = [
            ("run-a", "2026-11-14T12:00:15Z", "a", 300, 80, 20, 40, 70, 1400, 0, 0, 1),
            ("run-a", "2026-11-14T12:00:00Z", "b", 400, 100, 30, 40, 95, 900, 1, 0, 1),
            ("run-a", CURSOR, "c", None, None, None, None, None, None, None, None, 0),
            ("run-a", CURSOR, "e", None, 40, 10, 40, 60, 1400, 0, 0, 1),
            ("run-a", "2026-11-14T12:00:25Z", "a", 999, 100, 40, 40, 100, 500, 1, 9, 1),
            ("run-b", CURSOR, "a", 9000, 100, 40, 40, 100, 500, 1, 9, 1),
        ]
        self.db.executemany("INSERT INTO telemetry VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        self.db.executemany("INSERT INTO workloads VALUES (?, ?, ?, ?, ?, ?, ?)", [
            ("run-a", "2026-11-14T12:00:15Z", "a", "job-a", "running", 10, "normal"),
            ("run-a", "2026-11-14T12:00:25Z", "a", "future-job", "running", 999, "high"),
            ("run-b", CURSOR, "a", "other-run-job", "running", 888, "high"),
        ])
        self.db.executemany("INSERT INTO cooling VALUES (?, ?, ?, ?, ?)", [
            ("run-a", "2026-11-14T12:00:00Z", "zone-a", 20, 95),
            ("run-a", "2026-11-14T12:00:25Z", "zone-a", 99, 0),
            ("run-b", CURSOR, "zone-a", 88, 0),
        ])

    def query(self, name, **extra):
        return self.db.execute((SQL_DIR / name).read_text(),
                               {"run_id": "run-a", "as_of_ts": CURSOR, **extra}).fetchall()

    def test_overview_excludes_future_other_runs_stale_and_missing(self):
        result = dict(self.query("overview.sql")[0])
        self.assertEqual(result, {
            "total_gpu_count": 5, "observed_gpu_count": 2, "unknown_gpu_count": 3,
            "power_reporting_gpu_count": 1, "observed_gpu_power_kw": 0.3,
            "observed_avg_gpu_util_pct": 60.0, "observed_avg_temp_c": 65.0,
        })

    def test_freshness_boundary_includes_fifteen_seconds(self):
        self.db.execute("UPDATE telemetry SET event_ts='2026-11-14T12:00:05Z' WHERE gpu_id='b'")
        self.assertEqual(self.query("overview.sql")[0]["observed_gpu_count"], 3)

    def test_fractional_cursor_cannot_extend_freshness_to_fifteen_and_a_half_seconds(self):
        self.db.execute("DELETE FROM telemetry WHERE gpu_id != 'a' OR event_ts != '2026-11-14T12:00:15Z'")
        self.db.execute("UPDATE telemetry SET event_ts='2026-11-14T12:00:05Z'")
        self.db.execute("UPDATE workloads SET event_ts='2026-11-14T12:00:05Z' WHERE run_id='run-a' AND job_id='job-a'")
        self.db.execute("UPDATE cooling SET event_ts='2026-11-14T12:00:05Z' WHERE run_id='run-a' AND inlet_temp_c=20")
        self.assertEqual(self.query("overview.sql")[0]["observed_gpu_count"], 1)
        on_boundary = self.query("rack_context.sql", rack_id="rack-a")[0]
        self.assertEqual(on_boundary["telemetry_age_seconds"], 15)
        for field in ("telemetry_is_fresh", "telemetry_is_observed", "workload_is_fresh", "cooling_is_fresh"):
            self.assertEqual(on_boundary[field], 1)
        cursor = "2026-11-14T12:00:20.500000Z"
        beyond = self.query("overview.sql", as_of_ts=cursor)[0]
        self.assertEqual(beyond["observed_gpu_count"], 0)
        self.assertEqual(beyond["unknown_gpu_count"], 5)
        self.assertIsNone(beyond["observed_gpu_power_kw"])
        context = self.query("rack_context.sql", rack_id="rack-a", as_of_ts=cursor)[0]
        self.assertEqual(context["telemetry_age_seconds"], 15.5)
        for field in ("telemetry_is_fresh", "telemetry_is_observed", "workload_is_fresh", "cooling_is_fresh"):
            self.assertEqual(context[field], 0)

    def test_no_data_reports_unknown_and_null_aggregates_not_zero(self):
        result = self.query("overview.sql", run_id="missing")[0]
        self.assertEqual(result["unknown_gpu_count"], 5)
        self.assertEqual(result["observed_gpu_count"], 0)
        self.assertIsNone(result["observed_gpu_power_kw"])
        self.assertIsNone(result["observed_avg_temp_c"])

    def test_history_is_windowed_and_preserves_null(self):
        result = self.query("gpu_history.sql", gpu_id="a", window_start_ts="2026-11-14T12:00:10Z")
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["power_w"], 300)
        missing = self.query("gpu_history.sql", gpu_id="c", window_start_ts="2026-11-14T12:00:10Z")[0]
        self.assertIsNone(missing["power_w"])
        self.assertEqual(missing["heartbeat_ok"], 0)
        self.assertEqual(self.query("gpu_history.sql", gpu_id="a", window_start_ts="2026-11-14T12:00:21Z"), [])

    def test_context_applies_cursor_and_run_to_every_stream(self):
        result = {row["gpu_id"]: row for row in self.query("rack_context.sql", rack_id="rack-a")}
        self.assertEqual(len(result), 5)
        self.assertEqual(result["a"]["power_w"], 300)
        self.assertEqual(result["a"]["job_id"], "job-a")
        self.assertEqual(result["a"]["inlet_temp_c"], 20)
        self.assertEqual(result["a"]["workload_is_fresh"], 1)
        self.assertEqual(result["a"]["cooling_is_fresh"], 0)
        self.assertEqual(result["b"]["telemetry_is_fresh"], 0)
        self.assertEqual(result["c"]["telemetry_is_fresh"], 1)
        self.assertEqual(result["c"]["telemetry_is_observed"], 0)
        self.assertIsNone(result["d"]["power_w"])
        self.assertEqual(result["d"]["telemetry_is_observed"], 0)
        self.assertEqual(self.query("rack_context.sql", rack_id="absent"), [])


if __name__ == "__main__":
    unittest.main()
