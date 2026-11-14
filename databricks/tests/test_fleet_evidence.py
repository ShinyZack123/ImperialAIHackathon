"""Bounded evidence decoding tests using SDK response objects, no network."""
import json
import unittest
from unittest.mock import MagicMock

from databricks.sdk.service.sql import StatementResponse
from backend.databricks_data import DataQueryError, DataSettings, DatabricksData


def response(streams=None):
    streams = streams if streams is not None else {
        "inventory": [{"gpu_id": "gpu-001"}],
        "telemetry": [{"gpu_id": "gpu-001", "power_w": None, "heartbeat_ok": False}],
        "workloads": [], "cooling": [],
    }
    rows = [[name, str(len(values)), json.dumps(values)] for name, values in streams.items()]
    return StatementResponse.from_dict({
        "statement_id": "fleet", "status": {"state": "SUCCEEDED"},
        "manifest": {"format": "JSON_ARRAY", "truncated": False, "total_row_count": len(rows), "total_chunk_count": 1,
                     "schema": {"columns": [{"name": name, "type_name": kind, "position": index} for index, (name, kind) in enumerate([("dataset", "STRING"), ("row_count", "LONG"), ("payload", "STRING")])]}},
        "result": {"chunk_index": 0, "row_count": len(rows), "row_offset": 0, "data_array": rows},
    })


class FleetEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.statement_execution.execute_statement.return_value = response()
        self.data = DatabricksData(DataSettings("warehouse", "workspace"), workspace_client=self.client)

    def query(self):
        return self.data.fleet_evidence("run-1", "2026-11-14T09:10:45Z", "2026-11-14T08:55:45Z")

    def test_preserves_missing_metrics_and_binds_one_window(self):
        evidence = self.query()
        self.assertIsNone(evidence["telemetry"][0]["power_w"])
        self.assertIs(evidence["telemetry"][0]["heartbeat_ok"], False)
        self.assertEqual(evidence["workloads"], [])
        call = self.client.statement_execution.execute_statement.call_args.kwargs
        self.assertEqual({p.name: p.value for p in call["parameters"]}, {"run_id": "run-1", "as_of_ts": "2026-11-14T09:10:45Z", "window_start_ts": "2026-11-14T08:55:45Z"})

    def test_rejects_window_over_fifteen_minutes_before_query(self):
        with self.assertRaises(ValueError):
            self.data.fleet_evidence("run-1", "2026-11-14T09:10:45Z", "2026-11-14T08:55:44Z")
        self.client.statement_execution.execute_statement.assert_not_called()

    def test_missing_stream_and_malformed_json_are_errors(self):
        for mutate in (lambda r: r.result.data_array.pop(), lambda r: r.result.data_array[1].__setitem__(2, "not JSON"), lambda r: r.result.data_array[1].__setitem__(2, '[{"power_w":NaN}]')):
            item = response()
            mutate(item)
            item.manifest.total_row_count = item.result.row_count = len(item.result.data_array)
            self.client.statement_execution.execute_statement.return_value = item
            with self.assertRaises(DataQueryError):
                self.query()

    def test_overflow_is_not_returned_as_partial_evidence(self):
        item = response()
        item.result.data_array[1][1] = "4345"
        self.client.statement_execution.execute_statement.return_value = item
        with self.assertRaisesRegex(DataQueryError, "limits"):
            self.query()

    def test_duplicate_stream_and_wrong_record_counts_are_rejected(self):
        for column, value in ((0, "inventory"), (1, "2")):
            item = response()
            item.result.data_array[1][column] = value
            self.client.statement_execution.execute_statement.return_value = item
            with self.assertRaises(DataQueryError):
                self.query()


if __name__ == "__main__":
    unittest.main()
