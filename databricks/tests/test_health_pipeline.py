"""Exercise the actual feature SQL + rules on synthetic evidence using SQLite.

This verifies logic, not Spark/Delta runtime support; notebook execution remains
a separate Databricks integration check. Evaluator labels stay in this test only.
"""

import csv
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from generate import generate_dataset, SCHEMAS
from health_rules import evaluate_features, RULE_VERSION
from build_health_notebook import build_notebook

ROOT = Path(__file__).resolve().parents[1]


def epoch_micros(value):
    if value is None:
        return None
    delta = datetime.fromisoformat(value.replace("Z", "+00:00")) - datetime(1970, 1, 1, tzinfo=timezone.utc)
    return (delta.days * 86400 + delta.seconds) * 1000000 + delta.microseconds


class HealthPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        target = Path(cls.temp.name) / "data"
        generate_dataset(target)
        cls.runs = {r["scenario"]: r["run_id"] for r in json.loads((target / "evaluation/ground_truth.json").read_text())["runs"]}
        cls.db = sqlite3.connect(":memory:")
        cls.addClassCleanup(cls.db.close)
        cls.db.row_factory = sqlite3.Row
        cls.db.create_function("unix_timestamp", 1, lambda s: None if s is None else int(datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()))
        cls.db.create_function("unix_micros", 1, epoch_micros)
        text_fields = {"gpu_id", "vendor", "model", "rack_id", "server_id", "cooling_zone_id", "run_id", "event_ts", "job_id", "job_state", "priority"}
        integer_fields = {"throttled", "heartbeat_ok", "ecc_uncorrected_total", "progress_units"}
        for table, fields in SCHEMAS.items():
            cls.db.execute(f"CREATE TABLE {table} (" + ",".join(f"{field} {'TEXT' if field in text_fields else 'INTEGER' if field in integer_fields else 'REAL'}" for field in fields) + ")")
            with (target / "evidence" / f"{table}.csv").open() as handle:
                rows = []
                for row in csv.DictReader(handle):
                    converted = []
                    for field in fields:
                        value = row[field]
                        if value == "":
                            value = None
                        elif field in ("throttled", "heartbeat_ok"):
                            value = int(value == "true")
                        elif field in integer_fields:
                            value = int(value)
                        elif field not in text_fields:
                            value = float(value)
                        converted.append(value)
                    rows.append(converted)
                cls.db.executemany(f"INSERT INTO {table} VALUES ({','.join('?' for _ in fields)})", rows)
        cls.db.commit()

    def snapshot(self, scenario, cursor="2026-11-14T09:07:30Z"):
        end = datetime.fromisoformat(cursor.replace("Z", "+00:00"))
        start = (end - timedelta(seconds=60)).isoformat().replace("+00:00", "Z")
        params = {"run_id": self.runs[scenario], "as_of_ts": cursor, "window_start_ts": start, "rule_version": RULE_VERSION}
        return [dict(row) for row in self.db.execute((ROOT / "sql/health_features.sql").read_text(), params)]

    def active(self, rows):
        return [alert for row in rows for alert in evaluate_features(row) if alert["status"] == "active"]

    def test_healthy_high_load_and_idle_negative_control_do_not_alert(self):
        rows = self.snapshot("healthy_load")
        self.assertEqual(len(rows), 24)
        self.assertEqual(self.active(rows), [])
        self.assertGreater(rows[0]["gpu_util_pct"], 90)
        idle = next(row for row in rows if row["gpu_id"] == "gpu-024")
        self.assertEqual(next(a for a in evaluate_features(idle) if a["rule_id"] == "workload_stall")["status"], "clear")

    def test_shared_cooling_evidence_stays_in_affected_rack(self):
        alerts = self.active(self.snapshot("cooling_fault"))
        self.assertEqual({a["rule_id"] for a in alerts}, {"thermal_limit", "cooling_flow_low"})
        self.assertEqual({a["rack_id"] for a in alerts}, {"rack-b"})
        self.assertEqual(len(alerts), 16)

    def test_missing_telemetry_is_unknown_without_erasing_workload_evidence(self):
        row = next(r for r in self.snapshot("telemetry_gap") if r["gpu_id"] == "gpu-017")
        self.assertIsNone(row["power_w"])
        self.assertIsNone(row["ecc_delta_60s"])
        self.assertGreater(row["progress_delta_60s"], 0)
        statuses = {a["rule_id"]: a["status"] for a in evaluate_features(row)}
        self.assertEqual(statuses["telemetry_missing"], "active")
        self.assertEqual(statuses["thermal_limit"], "unknown")
        self.assertEqual(statuses["power_limit"], "unknown")
        self.assertEqual(statuses["workload_stall"], "clear")

    def test_new_error_and_stall_then_old_cumulative_error_clears(self):
        alerts = self.active(self.snapshot("isolated_error"))
        self.assertEqual({a["rule_id"] for a in alerts}, {"new_ecc_errors", "workload_stall"})
        self.assertEqual({a["gpu_id"] for a in alerts}, {"gpu-001"})
        recovered = self.snapshot("isolated_error", "2026-11-14T09:12:00Z")
        self.assertEqual(self.active(recovered), [])
        self.assertEqual(recovered[0]["ecc_delta_60s"], 0)

    def test_baseline_cannot_see_future_failure_or_another_run(self):
        for scenario in self.runs:
            with self.subTest(scenario=scenario):
                rows = self.snapshot(scenario, "2026-11-14T09:04:55Z")
                self.assertEqual(self.active(rows), [])
                self.assertTrue(all(row["run_id"] == self.runs[scenario] for row in rows))

    def test_reset_or_missing_interval_does_not_become_false_zero_delta(self):
        self.db.execute("SAVEPOINT changed")
        try:
            run_id = self.runs["healthy_load"]
            self.db.execute("UPDATE telemetry SET ecc_uncorrected_total=10 WHERE run_id=? AND gpu_id='gpu-001' AND event_ts='2026-11-14T09:07:00Z'", (run_id,))
            self.db.execute("DELETE FROM workloads WHERE run_id=? AND gpu_id='gpu-002' AND event_ts BETWEEN '2026-11-14T09:06:40Z' AND '2026-11-14T09:07:10Z'", (run_id,))
            rows = self.snapshot("healthy_load")
            self.assertIsNone(rows[0]["ecc_delta_60s"])
            self.assertIsNone(rows[1]["progress_delta_60s"])
        finally:
            self.db.execute("ROLLBACK TO changed")
            self.db.execute("RELEASE changed")

    def test_stale_data_and_short_initial_windows_remain_unknown(self):
        rows = self.snapshot("healthy_load", "2026-11-14T09:16:00Z")
        self.assertTrue(all(r["power_w"] is None and not r["telemetry_is_observed"] for r in rows))
        self.assertEqual({a["rule_id"] for a in self.active(rows)}, {"telemetry_missing"})
        initial = self.snapshot("healthy_load", "2026-11-14T09:00:10Z")
        self.assertTrue(all(r["ecc_delta_60s"] is None for r in initial))

    def test_fractional_freshness_matches_the_official_adapter(self):
        boundary = self.snapshot("healthy_load", "2026-11-14T09:15:10Z")
        beyond = self.snapshot("healthy_load", "2026-11-14T09:15:10.500000Z")
        self.assertTrue(all(r["telemetry_is_observed"] and r["workload_is_fresh"] and r["cooling_is_fresh"] for r in boundary))
        self.assertTrue(all(not r["telemetry_is_observed"] and not r["workload_is_fresh"] and not r["cooling_is_fresh"] for r in beyond))
        self.assertTrue(all(r["power_w"] is None and r["flow_pct"] is None and r["progress_delta_60s"] is None for r in beyond))
        # The persisted comparison-feature schema retains its original BIGINT age.
        self.assertTrue(all(r["telemetry_age_seconds"] == 15 for r in beyond))

    def test_repeat_has_stable_keys_and_structured_evidence(self):
        row = self.snapshot("cooling_fault")[8]
        first, second = evaluate_features(row), evaluate_features(row)
        self.assertEqual(first, second)
        self.assertEqual(len({a["alert_id"] for a in first}), 6)
        self.assertTrue(all(isinstance(json.loads(a["evidence_json"]), dict) for a in first))
        same_instant = {**row, "as_of_ts": "2026-11-14T10:07:30+01:00"}
        self.assertEqual([a["alert_id"] for a in first], [a["alert_id"] for a in evaluate_features(same_instant)])

    def test_embedded_notebook_builds_without_cloud_access(self):
        notebook = build_notebook()
        source = "\n".join("".join(c["source"]) for c in notebook["cells"] if c["cell_type"] == "code")
        self.assertIn("MERGE INTO", source)
        self.assertIn("evaluate_features", source)
        self.assertNotIn("data/evaluation", source)


if __name__ == "__main__":
    unittest.main()
