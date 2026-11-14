"""Saved cloud evidence still uses the current shared detector, without cloud APIs."""
import copy
import gzip
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import NAMESPACE_URL, uuid5

from backend import replay_bridge as bridge


ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "demo"
SCRIPT = ROOT / "backend/replay_bridge.py"
# Test expectations only. The replay bridge neither imports the generator nor sees labels.
RUNS = {name: str(uuid5(NAMESPACE_URL, "gpu-ops-data/1.0/42/" + name)) for name in
        ("healthy_load", "cooling_fault", "telemetry_gap", "isolated_error")}


class SavedReplayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.replay = bridge.SavedReplay(DEMO)
        cls.manifest = json.loads((DEMO / "manifest.json").read_text())
        cls.archive = (DEMO / "evidence.json.gz").read_bytes()

    def snapshot(self, name, cursor="2026-11-14T09:10:45Z"):
        return self.replay.request("snapshot", [RUNS[name], cursor])

    def temporary_export(self, *, manifest=None, archive=None, evidence=None):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        manifest = copy.deepcopy(self.manifest if manifest is None else manifest)
        if evidence is not None:
            payload = json.dumps(evidence, allow_nan=False, separators=(",", ":")).encode()
            archive = gzip.compress(payload)
            manifest.update(payload_sha256=hashlib.sha256(payload).hexdigest(), archive_sha256=hashlib.sha256(archive).hexdigest())
        (directory / "manifest.json").write_text(json.dumps(manifest))
        (directory / "evidence.json.gz").write_bytes(self.archive if archive is None else archive)
        return directory

    def test_catalogue_has_all_four_cloud_runs_and_explicit_delivery(self):
        response = self.replay.request("runs", [])
        self.assertEqual({row["run_id"] for row in response["data"]}, set(RUNS.values()))
        self.assertEqual(response["delivery"], "saved_replay")
        self.assertEqual(response["source"], "databricks")
        self.assertIs(response["synthetic"], True)
        self.assertEqual(response["exported_at"], self.manifest["exported_at"])
        self.assertTrue(all(row["sample_count"] == 4320 for row in response["data"]))

    def test_all_four_recorded_outcomes_come_from_actual_detector(self):
        healthy = self.snapshot("healthy_load")["data"]
        self.assertEqual(healthy["incidents"], [])
        self.assertEqual(healthy["overview"]["observed_gpu_count"], 24)
        cooling = self.snapshot("cooling_fault")["data"]
        incident = next(item for item in cooling["incidents"] if item["rootEntity"] == "zone-b")
        self.assertIn("robust_z", incident["detectors"])
        self.assertEqual(incident["affectedGpuIds"], ["gpu-%03d" % index for index in range(9, 17)])
        isolated = self.snapshot("isolated_error", "2026-11-14T09:07:30Z")["data"]
        methods = {finding["detector"] for item in isolated["incidents"] for finding in item["findings"]}
        self.assertTrue({"hard_counter", "liveness"} <= methods)
        gap = self.snapshot("telemetry_gap", "2026-11-14T09:07:30Z")["data"]
        gpu = next(gpu for rack in gap["racks"] for gpu in rack["gpus"] if gpu["id"] == "gpu-017")
        self.assertEqual(gpu["state"], "unknown")
        self.assertIsNone(gpu["metrics"]["power_w"])
        self.assertTrue(gpu["workload"]["is_fresh"])
        for snapshot in (healthy, cooling, isolated, gap):
            self.assertEqual(snapshot["engine"]["module"], "flagging.flagger.flag_window")
            self.assertEqual(snapshot["delivery"], "saved_replay")
            self.assertEqual(snapshot["exported_at"], self.manifest["exported_at"])
            self.assertEqual(snapshot["persistence"], {"status": "not_saved", "reason": "saved_replay"})

    def test_each_snapshot_invokes_current_adapter_without_cached_assessment(self):
        adapter = bridge._adapter()
        with patch.object(bridge, "_adapter", return_value=adapter), patch.object(adapter, "generate_snapshot", wraps=adapter.generate_snapshot) as detector:
            first = self.snapshot("healthy_load", "2026-11-14T09:01:00Z")
            second = self.snapshot("healthy_load", "2026-11-14T09:01:00Z")
        self.assertEqual(detector.call_count, 2)
        self.assertEqual(first, second)
        evidence, run_id, end, start = detector.call_args.args
        self.assertEqual((end - start).total_seconds(), 900)
        self.assertTrue(all(row["run_id"] == run_id and start <= bridge._time(row["event_ts"]) <= end for name, rows in evidence.items() if name != "inventory" for row in rows))

    def test_histories_preserve_nulls_and_never_include_future_or_another_run(self):
        cursor = "2026-11-14T09:07:30Z"
        run_id = RUNS["telemetry_gap"]
        history = self.replay.request("history", [run_id, cursor, "gpu-017", "1"])
        workload = self.replay.request("workload-history", [run_id, cursor, "gpu-017", "1"])
        self.assertEqual(len(history["data"]), 13)
        self.assertTrue(all(row["power_w"] is None and row["heartbeat_ok"] is False for row in history["data"]))
        self.assertGreater(workload["data"][0]["progress_units"], workload["data"][-1]["progress_units"])
        for response in (history, workload):
            self.assertEqual(response["as_of_ts"], cursor)
            self.assertEqual(response["delivery"], "saved_replay")
            self.assertTrue(all(row["run_id"] == run_id and row["gpu_id"] == "gpu-017" and bridge._time("2026-11-14T09:06:30Z") <= bridge._time(row["event_ts"]) <= bridge._time(cursor) for row in response["data"]))
            self.assertEqual(response["data"], sorted(response["data"], key=lambda row: row["event_ts"], reverse=True))

    def test_fractional_cursor_and_offset_are_preserved_and_equivalent(self):
        first = self.snapshot("healthy_load", "2026-11-14T09:01:00.500Z")
        second = self.snapshot("healthy_load", "2026-11-14T10:01:00.500+01:00")
        self.assertEqual(first, second)
        self.assertEqual(first["as_of_ts"], "2026-11-14T09:01:00.500000Z")
        self.assertEqual(first["data"]["racks"][0]["gpus"][0]["telemetry_age_seconds"], 0.5)

    def test_invalid_selection_and_outside_recording_are_rejected(self):
        for action, args in [
            ("snapshot", [RUNS["healthy_load"], "2026-11-14T09:15:00Z"]),
            ("snapshot", [RUNS["healthy_load"], "2026-11-14T08:59:59Z"]),
            ("snapshot", [RUNS["healthy_load"], "2026-11-14T09:01:00"]),
            ("snapshot", ["00000000-0000-0000-0000-000000000000", "2026-11-14T09:01:00Z"]),
            ("snapshot", ["not-a-uuid", "2026-11-14T09:01:00Z"]),
            ("history", [RUNS["healthy_load"], "2026-11-14T09:01:00Z", "gpu-999", "5"]),
            ("history", [RUNS["healthy_load"], "2026-11-14T09:01:00Z", "gpu-001", "16"]),
            ("history", [RUNS["healthy_load"], "2026-11-14T09:01:00Z", "gpu-001", "0"]),
            ("history", [RUNS["healthy_load"], "2026-11-14T09:01:00Z", "gpu-001", "1.5"]),
            ("runs", ["extra"]), ("unknown", []),
        ]:
            with self.subTest(action=action, args=args), self.assertRaises(bridge.ReplayError) as error:
                self.replay.request(action, args)
            self.assertEqual(error.exception.status, 400)

    def test_bad_archive_or_payload_hash_cannot_be_used(self):
        for field in ("archive_sha256", "payload_sha256"):
            manifest = {**self.manifest, field: "0" * 64}
            with self.subTest(field=field), self.assertRaises(bridge.ReplayError) as error:
                bridge.SavedReplay(self.temporary_export(manifest=manifest))
            self.assertEqual(error.exception.status, 503)

    def test_future_cross_run_and_extra_label_fields_fail_even_with_valid_hashes(self):
        original = json.loads(gzip.decompress(self.archive))
        for change in ({"event_ts": "2026-11-14T10:00:00Z"}, {"run_id": "00000000-0000-0000-0000-000000000000"}, {"scenario": "private-answer"}):
            evidence = copy.deepcopy(original)
            evidence["telemetry"][0].update(change)
            with self.subTest(change=change), self.assertRaises(bridge.ReplayError):
                bridge.SavedReplay(self.temporary_export(evidence=evidence))

    def test_decompression_is_bounded_before_parsing(self):
        payload = b" " * (bridge.MAX_BYTES + 1)
        archive = gzip.compress(payload)
        manifest = {**self.manifest, "archive_sha256": hashlib.sha256(archive).hexdigest(), "payload_sha256": hashlib.sha256(payload).hexdigest()}
        with self.assertRaises(bridge.ReplayError):
            bridge.SavedReplay(self.temporary_export(manifest=manifest, archive=archive))

    def test_detector_failure_is_explicit_without_substitution(self):
        with patch.object(bridge, "_adapter", side_effect=RuntimeError("private details")), self.assertRaises(bridge.ReplayError) as error:
            self.snapshot("healthy_load")
        self.assertEqual(error.exception.status, 502)
        self.assertNotIn("private details", str(error.exception))

    def test_isolated_cli_needs_no_packages_authentication_or_sql(self):
        code = """
import importlib.util, sys
spec = importlib.util.spec_from_file_location('saved_replay', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
status = module.main(['snapshot', sys.argv[2], '2026-11-14T09:01:00Z'])
assert not any(name in sys.modules for name in ('databricks.sdk', 'dotenv', 'backend.databricks_data', 'backend.result_store'))
raise SystemExit(status)
"""
        result = subprocess.run([sys.executable, "-I", "-c", code, str(SCRIPT), RUNS["healthy_load"]], text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        response = json.loads(result.stdout)
        self.assertEqual(response["data"]["persistence"]["status"], "not_saved")
        bad = subprocess.run([sys.executable, "-I", str(SCRIPT), "snapshot", "secret-like-input", "bad-time"], text=True, capture_output=True, timeout=30)
        self.assertNotEqual(bad.returncode, 0)
        self.assertEqual(json.loads(bad.stdout)["status"], 400)
        self.assertNotIn("secret-like-input", bad.stdout + bad.stderr)
        self.assertNotIn("Traceback", bad.stdout + bad.stderr)


if __name__ == "__main__":
    unittest.main()
