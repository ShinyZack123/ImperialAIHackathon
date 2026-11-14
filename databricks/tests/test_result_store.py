"""Persistence contract checks using SDK response models and no remote writes."""
from datetime import datetime, timezone
import json
import unittest
from unittest.mock import MagicMock

from databricks.sdk.service.sql import StatementResponse

from backend.databricks_data import DataQueryError, DataQueryTimeout, DataSettings, DatabricksData
from backend.result_store import DatabricksResultStore, MAX_SNAPSHOT_BYTES, detector_version


RUN = "43cd1ebe-e8e5-5ccf-b2c4-8ab0e1a8523c"
CURSOR = "2026-11-14T09:07:30Z"
NOW = datetime(2026, 11, 14, 16, 30, tzinfo=timezone.utc)


def response(state="SUCCEEDED", ident="write-1"):
    return StatementResponse.from_dict({"statement_id": ident, "status": {"state": state}})


def snapshot():
    return {
        "schemaVersion": "1.0", "source": "databricks", "synthetic": True,
        "run_id": RUN, "as_of_ts": CURSOR,
        "window": {"start": "2026-11-14T09:00:00Z", "end": CURSOR},
        "incidents": [], "racks": [], "overview": {"observed_gpu_power_kw": None},
        "engine": {"module": "flagging.flagger.flag_window", "deterministic": True, "version": detector_version()},
    }


def evidence():
    return {"inventory": [{"gpu_id": "gpu-001"}, {"gpu_id": "gpu-002"}],
            "telemetry": [{"run_id": RUN, "event_ts": CURSOR, "gpu_id": "gpu-001", "temp_c": None},
                          {"run_id": RUN, "event_ts": CURSOR, "gpu_id": "gpu-002", "temp_c": 55.0}],
            "workloads": [], "cooling": []}


class Clock:
    def __init__(self):
        self.value = 0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


class ResultStoreTests(unittest.TestCase):
    def setUp(self):
        self.workspace = MagicMock()
        self.api = self.workspace.statement_execution
        self.api.execute_statement.return_value = response()
        self.source = DatabricksData(DataSettings("abc123", "workspace"), workspace_client=self.workspace)
        self.clock = Clock()
        self.store = DatabricksResultStore(self.source, timeout_seconds=3, monotonic=self.clock.monotonic, sleep=self.clock.sleep, now=lambda: NOW)

    def test_ddl_then_parameterized_merge_and_zero_incidents_are_saved(self):
        source = snapshot()
        result = self.store.save(source, evidence())
        self.assertEqual(result["status"], "saved")
        self.assertEqual(result["table"], "workspace.gpu_ops_hackathon.official_detector_results")
        self.assertEqual(result["detector_version"], detector_version())
        self.assertEqual(result["generated_at"], "2026-11-14T16:30:00Z")
        self.assertEqual(len(result["evidence_sha256"]), 64)
        self.assertEqual(self.api.execute_statement.call_count, 2)
        ddl, merge = [call.kwargs for call in self.api.execute_statement.call_args_list]
        self.assertIn("CREATE TABLE IF NOT EXISTS `workspace`.`gpu_ops_hackathon`.`official_detector_results`", ddl["statement"])
        self.assertIn("USING DELTA", ddl["statement"])
        self.assertNotIn("REPLACE", ddl["statement"])
        self.assertTrue(merge["statement"].startswith("MERGE INTO"))
        self.assertNotIn(RUN, merge["statement"])
        bound = {p.name: (p.value, p.type) for p in merge["parameters"]}
        self.assertEqual(bound["run_id"], (RUN, "STRING"))
        self.assertEqual(bound["as_of_ts"], (CURSOR, "TIMESTAMP"))
        self.assertEqual(bound["incident_count"], ("0", "BIGINT"))
        self.assertEqual(bound["generated_at"], ("2026-11-14T16:30:00Z", "TIMESTAMP"))
        self.assertEqual(json.loads(bound["snapshot_json"][0]), source)
        self.assertIsNone(json.loads(bound["snapshot_json"][0])["overview"]["observed_gpu_power_kw"])
        for key in ("run_id", "as_of_ts", "detector_version", "evidence_sha256"):
            self.assertIn(f"target.{key} = source.{key}", merge["statement"])
        self.assertEqual(merge["wait_timeout"], "0s")
        self.assertEqual(merge["warehouse_id"], "abc123")

    def test_table_readiness_is_cached_and_repeated_key_is_idempotent(self):
        first = self.store.save(snapshot(), evidence())
        shuffled = evidence()
        shuffled["inventory"].reverse()
        shuffled["telemetry"].reverse()
        second = self.store.save(snapshot(), shuffled)
        self.assertEqual(first["evidence_sha256"], second["evidence_sha256"])
        statements = [call.kwargs["statement"] for call in self.api.execute_statement.call_args_list]
        self.assertEqual(sum(s.startswith("CREATE TABLE") for s in statements), 1)
        self.assertEqual(sum(s.startswith("MERGE INTO") for s in statements), 2)

    def test_changed_evidence_or_analysis_window_changes_hash(self):
        baseline = self.store.save(snapshot(), evidence())["evidence_sha256"]
        changed = evidence()
        changed["telemetry"][1]["temp_c"] = 56.0
        self.assertNotEqual(self.store.save(snapshot(), changed)["evidence_sha256"], baseline)
        source = snapshot()
        source["window"]["start"] = "2026-11-14T09:01:00Z"
        self.assertNotEqual(self.store.save(source, evidence())["evidence_sha256"], baseline)

    def test_equivalent_timestamp_offsets_have_same_evidence_hash(self):
        baseline = self.store.save(snapshot(), evidence())["evidence_sha256"]
        changed = evidence()
        for row in changed["telemetry"]:
            row["event_ts"] = "2026-11-14T10:07:30+01:00"
        self.assertEqual(self.store.save(snapshot(), changed)["evidence_sha256"], baseline)

    def test_pending_running_success_is_polled_before_saved(self):
        self.api.execute_statement.side_effect = [response("PENDING", "ddl"), response("RUNNING", "merge")]
        self.api.get_statement.side_effect = [response("SUCCEEDED", "ddl"), response("SUCCEEDED", "merge")]
        self.assertEqual(self.store.save(snapshot(), evidence())["status"], "saved")
        self.assertEqual([c.kwargs["statement_id"] for c in self.api.get_statement.call_args_list], ["ddl", "merge"])

    def test_failed_ddl_stops_before_merge_and_is_not_cached(self):
        self.api.execute_statement.return_value = response("FAILED")
        with self.assertRaises(DataQueryError):
            self.store.save(snapshot(), evidence())
        self.api.execute_statement.assert_called_once()
        self.api.execute_statement.return_value = response()
        self.store.save(snapshot(), evidence())
        self.assertTrue(self.api.execute_statement.call_args_list[1].kwargs["statement"].startswith("CREATE TABLE"))

    def test_failed_canceled_closed_or_unknown_merge_never_reports_success(self):
        for state in ("FAILED", "CANCELED", "CLOSED", "UNKNOWN"):
            with self.subTest(state=state):
                self.store._table_ready = True
                self.api.execute_statement.return_value = response(state)
                with self.assertRaises(DataQueryError):
                    self.store.save(snapshot(), evidence())

    def test_ddl_and_merge_share_one_deadline_and_timeout_cancels_merge(self):
        self.api.execute_statement.side_effect = [response("PENDING", "ddl"), response("PENDING", "merge")]
        self.api.get_statement.side_effect = [response("SUCCEEDED", "ddl"), response("RUNNING", "merge")]
        with self.assertRaises(DataQueryTimeout):
            self.store.save(snapshot(), evidence())
        self.assertEqual(self.clock.value, 3)
        self.api.cancel_execution.assert_called_once_with(statement_id="merge")

    def test_network_errors_are_sanitized(self):
        self.api.execute_statement.side_effect = RuntimeError("TOKEN secret private details")
        with self.assertRaises(DataQueryError) as caught:
            self.store.save(snapshot(), evidence())
        self.assertNotIn("secret", str(caught.exception))
        self.assertNotIn("TOKEN", str(caught.exception))

    def test_invalid_provenance_and_evaluation_labels_prevent_all_queries(self):
        for mutate in (
            lambda s: s.update(source="fixture"),
            lambda s: s.update(synthetic=False),
            lambda s: s.update(run_id="not-a-uuid"),
            lambda s: s.update(run_id=123),
            lambda s: s.update(as_of_ts="2026-11-14T09:07:30"),
            lambda s: s["engine"].update(module="baseline-v1"),
            lambda s: s["engine"].update(version="wrong"),
            lambda s: s.update(ground_truth="answer"),
            lambda s: s["incidents"].append({"findings": [{"answerKey": "answer"}]}),
        ):
            bad = snapshot()
            mutate(bad)
            with self.assertRaises(DataQueryError):
                self.store.save(bad, evidence())
        self.api.execute_statement.assert_not_called()

    def test_future_or_cross_run_evidence_prevents_writes(self):
        for changes in ({"event_ts": "2026-11-14T09:07:31Z"}, {"run_id": "c573ec80-c960-451a-92b3-a177f8ef4005"}, {"groundTruth": "label"}):
            bad = evidence()
            bad["telemetry"][0].update(changes)
            with self.assertRaises(DataQueryError):
                self.store.save(snapshot(), bad)
        self.api.execute_statement.assert_not_called()

    def test_nonfinite_or_oversized_snapshot_prevents_writes(self):
        bad = snapshot()
        bad["overview"]["observed_gpu_power_kw"] = float("inf")
        with self.assertRaises(DataQueryError):
            self.store.save(bad, evidence())
        bad = snapshot()
        bad["limitations"] = ["x" * MAX_SNAPSHOT_BYTES]
        with self.assertRaises(DataQueryError):
            self.store.save(bad, evidence())
        self.api.execute_statement.assert_not_called()


if __name__ == "__main__":
    unittest.main()
