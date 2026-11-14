"""Exercise the shared deterministic detector on actual generated evidence.

Only test setup reads private scenario labels. The adapter receives the four
evidence tables and never reads labels, the flagger's unrelated fixtures or data.
"""
import copy
import csv
from datetime import datetime, timedelta
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from backend.flagging_adapter import _context_and_records, _flagger, _time, _validate, generate_snapshot
from generate import generate_dataset, SCHEMAS


START = "2026-11-14T09:00:00Z"
CURSOR = "2026-11-14T09:07:30Z"


class FlaggerAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        target = Path(cls.temp.name) / "data"
        generate_dataset(target)
        cls.runs = {r["scenario"]: r["run_id"] for r in json.loads((target / "evaluation/ground_truth.json").read_text())["runs"]}
        strings = {"gpu_id", "rack_id", "server_id", "cooling_zone_id", "vendor", "model", "run_id", "event_ts", "job_id", "job_state", "priority"}
        cls.tables = {}
        for table in SCHEMAS:
            with (target / "evidence" / f"{table}.csv").open() as handle:
                rows = []
                for row in csv.DictReader(handle):
                    for field, value in row.items():
                        row[field] = None if value == "" else value if field in strings else (value == "true") if field in {"heartbeat_ok", "throttled"} else int(value) if field in {"ecc_uncorrected_total", "progress_units"} else float(value)
                    rows.append(row)
                cls.tables[table] = rows

    def evidence(self, scenario, cursor=CURSOR, start=START):
        run = self.runs[scenario]
        return copy.deepcopy({table: rows if table == "inventory" else [r for r in rows if r["run_id"] == run and start <= r["event_ts"] <= cursor] for table, rows in self.tables.items()})

    def snapshot(self, scenario, cursor=CURSOR, start=START):
        return generate_snapshot(self.evidence(scenario, cursor, start), self.runs[scenario], cursor, start)

    def test_calls_actual_shared_detector_and_preserves_source_topology(self):
        detector = _flagger()
        with patch.object(detector, "flag_window", wraps=detector.flag_window) as run:
            result = self.snapshot("healthy_load")
        run.assert_called_once()
        self.assertEqual(result["engine"]["module"], "flagging.flagger.flag_window")
        self.assertEqual([r["id"] for r in result["racks"]], ["rack-a", "rack-b", "rack-c"])
        self.assertEqual({g["id"] for r in result["racks"] for g in r["gpus"]}, {f"gpu-{n:03d}" for n in range(1, 25)})

    def test_healthy_high_load_and_idle_are_not_faults(self):
        result = self.snapshot("healthy_load")
        self.assertEqual(result["incidents"], [])
        self.assertEqual(result["overview"]["observed_gpu_count"], 24)
        self.assertEqual({g["state"] for r in result["racks"] for g in r["gpus"]}, {"clear"})

    def test_gap_is_unknown_and_still_has_independent_workload_progress(self):
        result = self.snapshot("telemetry_gap")
        gpu = next(g for r in result["racks"] for g in r["gpus"] if g["id"] == "gpu-017")
        self.assertEqual(gpu["state"], "unknown")
        self.assertFalse(gpu["telemetry_is_observed"])
        self.assertIsNone(gpu["metrics"]["power_w"])
        self.assertTrue(gpu["workload"]["is_fresh"])
        self.assertGreater(gpu["workload"]["progress_units"], 0)
        self.assertEqual(result["overview"]["unknown_gpu_count"], 1)
        self.assertTrue(any("telemetry" in i["detectors"] for i in result["incidents"]))

    def test_isolated_counter_and_generic_progress_use_actual_detector(self):
        result = self.snapshot("isolated_error")
        findings = [f for i in result["incidents"] for f in i["findings"]]
        self.assertTrue(any(f["detector"] == "hard_counter" and f["entity"] == "gpu-001" for f in findings))
        progress = next(f for f in findings if f["detector"] == "liveness")
        self.assertEqual(progress["entity"], "job-gpu-001")
        self.assertEqual(progress["metric"], "workload.progress_units")
        self.assertIn("synthetic work units", progress["text"])
        self.assertNotIn("training steps", progress["text"])

    def test_recent_recovered_window_does_not_reflag_old_counter(self):
        result = self.snapshot("isolated_error", "2026-11-14T09:14:55Z", "2026-11-14T09:11:00Z")
        self.assertEqual(result["incidents"], [])
        self.assertEqual(result["racks"][0]["gpus"][0]["metrics"]["ecc_uncorrected_total"], 2)

    def test_cooling_units_are_not_fabricated_and_short_window_does_not_bypass_persistence(self):
        result = self.snapshot("cooling_fault")
        self.assertEqual(result["engine"]["persistenceSeconds"], 300)
        inventory, tables = _validate(self.evidence("cooling_fault"), self.runs["cooling_fault"], _time(START), _time(CURSOR))
        context, records = _context_and_records(inventory, tables)
        flow = [r for r in records if r["metric"] == "cdu.flow_percent"]
        self.assertTrue(flow)
        self.assertEqual({r["unit"] for r in flow}, {"%"})
        self.assertFalse(any(r["metric"] == "cdu.flow_lpm" for r in records))
        self.assertEqual(context["baselines"], [])
        self.assertFalse(any(f["detector"] == "hard_limit" for i in result["incidents"] for f in i["findings"]))

    def test_no_samples_is_unknown_with_null_aggregates(self):
        data = self.evidence("healthy_load")
        for table in ("telemetry", "workloads", "cooling"):
            data[table] = []
        result = generate_snapshot(data, self.runs["healthy_load"], CURSOR, START)
        self.assertEqual(result["overview"]["unknown_gpu_count"], 24)
        self.assertIsNone(result["overview"]["observed_avg_temp_c"])
        self.assertIsNone(result["overview"]["observed_gpu_power_kw"])
        self.assertEqual(result["incidents"], [])

    def test_cooling_relative_flow_policy_correlates_the_affected_rack(self):
        result = self.snapshot("cooling_fault", "2026-11-14T09:10:45Z")
        self.assertEqual(_flagger().ROBUST["cdu.flow_percent"], _flagger().ROBUST["cdu.flow_lpm"])
        incident = next(i for i in result["incidents"] if i["rootEntity"] == "zone-b")
        self.assertTrue(any(f["metric"] == "cdu.flow_percent" and f["detector"] == "robust_z" for f in incident["findings"]))
        self.assertEqual(set(incident["affectedGpuIds"]), {f"gpu-{n:03d}" for n in range(9, 17)})

    def test_stale_latest_sample_stays_unknown_even_with_no_detector_findings(self):
        data = self.evidence("healthy_load", "2026-11-14T09:04:00Z")
        result = generate_snapshot(data, self.runs["healthy_load"], CURSOR, START)
        self.assertEqual(result["overview"]["unknown_gpu_count"], 24)
        self.assertIsNone(result["overview"]["observed_avg_temp_c"])

    def test_unobserved_gaps_do_not_prove_thermal_or_progress_persistence(self):
        source = self.evidence("healthy_load")
        start = _time(START)
        item = source["inventory"][0]
        telemetry = next(row for row in source["telemetry"] if row["gpu_id"] == item["gpu_id"])
        work = next(row for row in source["workloads"] if row["gpu_id"] == item["gpu_id"])
        for offsets in ([0, 5, 10, 595, 600, 605], [0, 300, 600]):
            with self.subTest(offsets=offsets):
                evidence = {"inventory": [item], "telemetry": [], "workloads": [], "cooling": []}
                for seconds in offsets:
                    timestamp = (start + timedelta(seconds=seconds)).isoformat()
                    evidence["telemetry"].append({**telemetry, "event_ts": timestamp, "temp_c": 100})
                    evidence["workloads"].append({**work, "event_ts": timestamp, "progress_units": 10})
                result = generate_snapshot(evidence, self.runs["healthy_load"], start + timedelta(seconds=offsets[-1]), start)
                findings = [f for incident in result["incidents"] for f in incident["findings"]]
                self.assertFalse(any(f["detector"] in {"hard_limit", "liveness"} for f in findings))
                self.assertEqual(result["overview"]["observed_gpu_count"], 1)

    def test_checkpoint_persistence_breaks_unobserved_gaps_and_honours_declared_cadence(self):
        detector = _flagger()
        points = [(t, "checkpoint") for t in [0, 5, 10, 595, 600, 605]]
        self.assertEqual(detector.all_runs(points, lambda value: value == "checkpoint"), [(0, 10, 15, 3), (595, 605, 15, 3)])
        self.assertEqual(detector.longest_run(points, lambda value: value == "checkpoint"), (0, 10, 15, 3))
        # Declared cadence prevents an entirely sparse window from redefining normal sampling.
        sparse = [(0, True), (300, True), (600, True)]
        self.assertEqual(detector.longest_run(sparse, bool, sample_interval=5), (0, 0, 5, 1))
        graph = detector.Graph({"gpus": [{"id": "gpu", "type": "gpu", "metricDefinitions": [{"metric": "gpu.temperature_c", "samplingIntervalSeconds": 5}]}]})
        self.assertEqual(graph.sampling_interval("gpu", "gpu.temperature_c"), 5)
        inventory, tables = _validate(self.evidence("healthy_load"), self.runs["healthy_load"], _time(START), _time(CURSOR))
        context, _ = _context_and_records(inventory, tables)
        job = context["workloads"][0]
        job["checkpointPolicy"] = {"expectedDurationSeconds": 30}
        phases = [{"entityId": job["id"], "timestamp": (_time(START) + timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "metric": "workload.phase", "value": value, "unit": "state", "quality": {"valid": True}} for t, value in points]
        incidents, _ = detector.flag_window(context, phases)
        self.assertFalse(any(f.detector == "liveness" for incident in incidents for f in incident.findings))

    def test_input_order_does_not_change_deterministic_result(self):
        data = self.evidence("isolated_error")
        baseline = generate_snapshot(data, self.runs["isolated_error"], CURSOR, START)
        shuffled = copy.deepcopy(data)
        for rows in shuffled.values():
            random.Random(7).shuffle(rows)
        self.assertEqual(generate_snapshot(shuffled, self.runs["isolated_error"], CURSOR, START), baseline)
        self.assertNotIn("_time", data["telemetry"][0])

    def test_fractional_cursor_precision_and_utc_offset_are_preserved(self):
        data = self.evidence("healthy_load")
        result = generate_snapshot(data, self.runs["healthy_load"], "2026-11-14T10:07:30.500+01:00", "2026-11-14T08:59:59.125Z")
        self.assertEqual(result["as_of_ts"], "2026-11-14T09:07:30.500000Z")
        self.assertEqual(result["window"], {"start": "2026-11-14T08:59:59.125000Z", "end": "2026-11-14T09:07:30.500000Z"})
        equivalent = generate_snapshot(data, self.runs["healthy_load"], "2026-11-14T09:07:30.500000Z", "2026-11-14T09:59:59.125+01:00")
        self.assertEqual(result, equivalent)

    def test_fractional_source_samples_are_rejected_instead_of_collapsed(self):
        data = self.evidence("healthy_load")
        data["telemetry"][0]["event_ts"] = "2026-11-14T09:00:00.500Z"
        with self.assertRaisesRegex(ValueError, "whole-second evidence"):
            generate_snapshot(data, self.runs["healthy_load"], CURSOR, START)

    def test_cached_engine_version_describes_loaded_code_not_later_file_edits(self):
        baseline = self.snapshot("healthy_load")
        self.assertEqual(len(baseline["engine"]["version"]), 64)
        with patch("pathlib.Path.read_bytes", return_value=b"changed code on disk"):
            after_edit = self.snapshot("healthy_load")
        self.assertEqual(after_edit["engine"]["version"], baseline["engine"]["version"])

    def test_future_cross_run_and_duplicate_samples_are_rejected(self):
        run = self.runs["healthy_load"]
        for change in (lambda row: row.update(event_ts="2026-11-14T09:08:00Z"), lambda row: row.update(run_id="another-run")):
            data = self.evidence("healthy_load")
            change(data["telemetry"][0])
            with self.assertRaises(ValueError):
                generate_snapshot(data, run, CURSOR, START)
        data = self.evidence("healthy_load")
        data["telemetry"].append(data["telemetry"][0])
        with self.assertRaises(ValueError):
            generate_snapshot(data, run, CURSOR, START)

    def test_windows_nonfinite_values_and_ambiguous_entities_rejected(self):
        data = self.evidence("healthy_load")
        for start, end in ((CURSOR, START), (START, "2026-11-14T09:16:00Z"), (START, "2026-11-14T09:07:30")):
            with self.assertRaises(ValueError):
                generate_snapshot(data, self.runs["healthy_load"], end, start)
        data["telemetry"][0]["temp_c"] = float("nan")
        with self.assertRaises(ValueError):
            generate_snapshot(data, self.runs["healthy_load"], CURSOR, START)
        data = self.evidence("healthy_load")
        data["inventory"][0]["cooling_zone_id"] = data["inventory"][0]["gpu_id"]
        with self.assertRaises(ValueError):
            generate_snapshot(data, self.runs["healthy_load"], CURSOR, START)

    def test_default_step_counter_contract_is_unchanged(self):
        detector = _flagger()
        context = {"workloads": [{"id": "job-test", "type": "workload", "status": "running", "allocation": {"gpuIds": []}}]}
        records = []
        for second in (0, 60, 120, 180):
            time = f"2026-11-14T09:{second // 60:02d}:00Z"
            for metric, value in (("workload.status", "running"), ("workload.steps_completed", 5)):
                records.append({"entityId": "job-test", "timestamp": time, "metric": metric, "value": value, "unit": "count", "quality": {"valid": True}})
        incidents, _ = detector.flag_window(context, records)
        finding = incidents[0].findings[0]
        self.assertEqual(finding.metric, "workload.steps_completed")
        self.assertIn("no training steps", finding.text)


if __name__ == "__main__":
    unittest.main()
