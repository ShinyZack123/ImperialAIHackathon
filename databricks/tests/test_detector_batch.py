"""Batch assessment used by the Databricks detector job. Uses the bundled replay; no cloud calls."""
from datetime import datetime, timezone
import json
import unittest

from backend.detector_batch import ReplayEvidence, assess
from backend.replay_bridge import SavedReplay
from backend.result_store import detector_version


COOLING_RUN = "43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c"
NOW = datetime(2026, 11, 15, 4, 0, tzinfo=timezone.utc)


def at(minute, second=0):
    return datetime(2026, 11, 14, 9, minute, second, tzinfo=timezone.utc)


class DetectorBatchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replay = SavedReplay()
        cls.evidence = {run_id: ReplayEvidence(run_id, {name: [row for _, row in cls.replay.rows[name][run_id]] for name in ("telemetry", "workloads", "cooling")})
                        for run_id in cls.replay.intervals}
        cls.cooling = next(run for run in cls.replay.runs if run["run_id"] == COOLING_RUN)

    def test_skips_saved_cursors_and_matches_the_backend_snapshot(self):
        result = assess(self.replay.inventory, [self.cooling], self.evidence, step_seconds=300,
                        already_assessed={(COOLING_RUN, at(5))}, now=NOW)
        self.assertEqual((result["planned"], result["already_assessed"], result["deferred"]), (3, 1, 0))
        self.assertEqual([record["as_of_ts"] for record in result["records"]], [at(10), at(14, 55)])
        self.assertEqual(result["detector_version"], detector_version())
        record = result["records"][0]
        self.assertEqual((record["as_of_iso"], record["generated_at"], len(record["evidence_sha256"])), ("2026-11-14T09:10:00Z", NOW, 64))
        bridge = self.replay.request("snapshot", [COOLING_RUN, "2026-11-14T09:10:00Z"])["data"]
        for key in ("delivery", "exported_at", "persistence"):
            bridge.pop(key)
        self.assertEqual(json.loads(record["snapshot_json"]), bridge)
        self.assertEqual(record["incident_count"], len(bridge["incidents"]))

    def test_window_holds_only_the_fifteen_minutes_up_to_the_cursor(self):
        window = self.evidence[COOLING_RUN].window(at(0, 30), at(1))
        self.assertEqual(len(window["telemetry"]), 24 * 7)
        self.assertEqual(len(window["cooling"]), 3 * 7)
        self.assertTrue(all("2026-11-14T09:00:30" <= row["event_ts"][:19] <= "2026-11-14T09:01:00" for row in window["workloads"]))

    def test_cursor_limit_defers_the_rest(self):
        result = assess(self.replay.inventory, self.replay.runs, self.evidence, step_seconds=300,
                        already_assessed=set(), max_cursors=2, now=NOW)
        self.assertEqual((len(result["records"]), result["planned"], result["deferred"]), (2, 12, 10))

    def test_rejects_rows_from_another_replay_and_bad_limits(self):
        rows = {name: [row for _, row in self.replay.rows[name][COOLING_RUN]][:3] for name in ("telemetry", "workloads", "cooling")}
        with self.assertRaises(ValueError):
            ReplayEvidence("522f5945-1c4b-5bbd-afe4-807ae90cdd84", rows)
        for limit in (0, True, 10_000):
            with self.assertRaises(ValueError):
                assess(self.replay.inventory, [self.cooling], self.evidence, step_seconds=300, already_assessed=set(), max_cursors=limit)


if __name__ == "__main__":
    unittest.main()
