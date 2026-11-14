import csv
import json
from pathlib import Path
import tempfile
import unittest
from collections import Counter
from uuid import UUID

from generate import NULLABLE_METRICS, SCENARIOS, SCHEMAS, generate_dataset


class DatasetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.output = Path(cls.temp.name) / "dataset"
        cls.metadata = generate_dataset(cls.output)
        cls.rows = {}
        for name in SCHEMAS:
            with (cls.output / "evidence" / f"{name}.csv").open(newline="") as handle:
                reader = csv.DictReader(handle)
                assert tuple(reader.fieldnames) == SCHEMAS[name]
                cls.rows[name] = list(reader)
        cls.labels = json.loads((cls.output / "evaluation" / "ground_truth.json").read_text())["runs"]
        cls.run_ids = {run["scenario"]: run["run_id"] for run in cls.labels}

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_reproducible_and_seed_changes_data(self):
        second = Path(self.temp.name) / "same"
        generate_dataset(second)
        for file in self.output.rglob("*"):
            if file.is_file():
                self.assertEqual(file.read_bytes(), (second / file.relative_to(self.output)).read_bytes())
        changed = Path(self.temp.name) / "changed"
        generate_dataset(changed, seed=43, scenario="healthy_load")
        with (changed / "evidence" / "telemetry.csv").open() as handle:
            row = next(csv.DictReader(handle))
        self.assertNotIn(row["run_id"], self.run_ids.values())

    def test_topology_counts_and_units(self):
        inventory = {row["gpu_id"]: row for row in self.rows["inventory"]}
        self.assertEqual(Counter(row["vendor"] for row in inventory.values()), {"NVIDIA": 8, "AMD": 8, "Intel": 8})
        self.assertEqual(Counter(row["rack_id"] for row in inventory.values()), {"rack-a": 8, "rack-b": 8, "rack-c": 8})
        self.assertEqual(set(Counter(row["server_id"] for row in inventory.values()).values()), {4})
        for name, count in self.metadata["row_counts"].items():
            self.assertEqual(len(self.rows[name]), count)
        for row in self.rows["telemetry"]:
            UUID(row["run_id"])
            self.assertTrue(row["event_ts"].endswith("Z"))
            self.assertIn(row["heartbeat_ok"], ("true", "false"))
            if row["heartbeat_ok"] == "false":
                continue
            self.assertGreaterEqual(float(row["gpu_util_pct"]), 0)
            self.assertLessEqual(float(row["gpu_util_pct"]), 100)
            self.assertGreaterEqual(float(row["power_w"]), 0)
            self.assertLessEqual(float(row["power_w"]), float(inventory[row["gpu_id"]]["power_limit_w"]))
            self.assertLessEqual(float(row["memory_used_gb"]), float(row["memory_total_gb"]))
            self.assertIn(row["throttled"], ("true", "false"))
        for row in self.rows["cooling"]:
            self.assertTrue(0 <= float(row["flow_pct"]) <= 100)

    def test_no_ground_truth_in_evidence(self):
        for file in (self.output / "evidence").iterdir():
            content = file.read_text()
            for private_word in (*SCENARIOS, "scenario", "expected", "ground_truth"):
                self.assertNotIn(private_word, content)
        self.assertEqual(len(self.labels), 4)

    def test_missing_telemetry_is_null_and_workload_continues(self):
        run_id = self.run_ids["telemetry_gap"]
        rows = [row for row in self.rows["telemetry"] if row["run_id"] == run_id and row["gpu_id"] == "gpu-017"]
        self.assertEqual(sum(row["heartbeat_ok"] == "false" for row in rows), 60)
        for row in rows[60:120]:
            self.assertTrue(all(row[key] == "" for key in NULLABLE_METRICS))
        self.assertEqual(rows[59]["heartbeat_ok"], "true")
        self.assertEqual(rows[120]["heartbeat_ok"], "true")
        workloads = [row for row in self.rows["workloads"] if row["run_id"] == run_id and row["gpu_id"] == "gpu-017"]
        self.assertGreater(int(workloads[119]["progress_units"]), int(workloads[60]["progress_units"]))

    def test_error_counters_remain_cumulative_and_job_recovers(self):
        run_id = self.run_ids["isolated_error"]
        rows = [row for row in self.rows["telemetry"] if row["run_id"] == run_id and row["gpu_id"] == "gpu-001"]
        counters = [int(row["ecc_uncorrected_total"]) for row in rows]
        self.assertEqual(counters, sorted(counters))
        self.assertEqual(counters[59], 0)
        self.assertEqual(counters[119], 2)
        self.assertEqual(counters[-1], 2)
        workload = [row for row in self.rows["workloads"] if row["run_id"] == run_id and row["gpu_id"] == "gpu-001"]
        self.assertEqual(workload[59]["progress_units"], workload[119]["progress_units"])
        self.assertGreater(int(workload[120]["progress_units"]), int(workload[119]["progress_units"]))

    def test_cooling_fault_shared_and_healthy_load_no_throttle(self):
        bad = self.run_ids["cooling_fault"]
        bad_rows = [row for row in self.rows["telemetry"] if row["run_id"] == bad and row["throttled"] == "true"]
        affected = {row["gpu_id"] for row in bad_rows}
        self.assertEqual(affected, {f"gpu-{number:03d}" for number in range(9, 17)})
        good = self.run_ids["healthy_load"]
        inventory = {row["gpu_id"]: row for row in self.rows["inventory"]}
        for row in self.rows["telemetry"]:
            if row["run_id"] == good:
                self.assertEqual(row["throttled"], "false")
                self.assertLess(float(row["temp_c"]), float(inventory[row["gpu_id"]]["temperature_limit_c"]))
                self.assertEqual(row["ecc_uncorrected_total"], "0")

    def test_idle_negative_control(self):
        for row in self.rows["workloads"]:
            if row["gpu_id"] == "gpu-024":
                self.assertEqual(row["job_id"], "")
                self.assertEqual(row["job_state"], "idle")
                self.assertEqual(row["progress_units"], "0")
        for row in self.rows["telemetry"]:
            if row["gpu_id"] == "gpu-024":
                self.assertLessEqual(float(row["gpu_util_pct"]), 2)

    def test_keys_unique_and_time_window_bounded(self):
        for name, entity in (("telemetry", "gpu_id"), ("workloads", "gpu_id"), ("cooling", "cooling_zone_id")):
            keys = [(row["run_id"], row["event_ts"], row[entity]) for row in self.rows[name]]
            self.assertEqual(len(keys), len(set(keys)))
            self.assertTrue(all("2026-11-14T09:00:00Z" <= row["event_ts"] < "2026-11-14T09:15:00Z" for row in self.rows[name]))

    def test_overwrite_guard_and_invalid_arguments(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "out"
            generate_dataset(output, scenario="healthy_load")
            original = (output / "evidence" / "telemetry.csv").read_bytes()
            with self.assertRaises(FileExistsError):
                generate_dataset(output, seed=99)
            self.assertEqual(original, (output / "evidence" / "telemetry.csv").read_bytes())
            generate_dataset(output, seed=99, scenario="healthy_load", overwrite=True)
            self.assertNotEqual(original, (output / "evidence" / "telemetry.csv").read_bytes())
            (output / "keep.txt").write_text("user data")
            with self.assertRaises(ValueError):
                generate_dataset(output, overwrite=True)
            self.assertEqual((output / "keep.txt").read_text(), "user data")
            with self.assertRaises(ValueError):
                generate_dataset(Path(folder) / "invalid", scenario="not-a-scenario")
            with self.assertRaises(TypeError):
                generate_dataset(Path(folder) / "invalid", seed=True)


if __name__ == "__main__":
    unittest.main()
