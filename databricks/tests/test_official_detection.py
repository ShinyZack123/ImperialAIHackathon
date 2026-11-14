"""Failure boundaries: no baseline fallback and no unsaved official success."""
from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock, patch

from backend.databricks_data import DataQueryError
from backend.official_detection import OfficialDetector


class OfficialDetectorTests(unittest.TestCase):
    def setUp(self):
        self.data, self.store = MagicMock(), MagicMock()
        self.cursor = datetime(2026, 11, 14, 9, 10, 45, tzinfo=timezone.utc)
        self.service = OfficialDetector(self.data, self.store)

    def test_one_bounded_evidence_window_is_saved_before_exposure(self):
        snapshot = {"incidents": [], "source": "databricks"}
        self.store.save.return_value = {"status": "saved"}
        with patch("backend.official_detection.generate_snapshot", return_value=snapshot) as detector:
            result = self.service.snapshot("run", self.cursor)
        start = datetime(2026, 11, 14, 8, 55, 45, tzinfo=timezone.utc)
        self.data.fleet_evidence.assert_called_once_with("run", self.cursor, start)
        detector.assert_called_once_with(self.data.fleet_evidence.return_value, "run", self.cursor, start)
        self.store.save.assert_called_once()
        self.assertEqual(result["persistence"]["status"], "saved")
        self.data.health_alerts.assert_not_called()

    def test_detector_failure_is_explicit_and_never_calls_baseline_or_store(self):
        with patch("backend.official_detection.generate_snapshot", side_effect=RuntimeError("private details")):
            with self.assertRaisesRegex(DataQueryError, "official deterministic detector") as caught:
                self.service.snapshot("run", self.cursor)
        self.assertNotIn("private details", str(caught.exception))
        self.data.health_alerts.assert_not_called()
        self.store.save.assert_not_called()

    def test_persistence_failure_is_not_a_successful_official_result(self):
        self.store.save.side_effect = DataQueryError("Could not save official result")
        with patch("backend.official_detection.generate_snapshot", return_value={"incidents": []}):
            with self.assertRaisesRegex(DataQueryError, "save"):
                self.service.snapshot("run", self.cursor)
        self.data.health_alerts.assert_not_called()

    def test_source_failure_is_not_substituted(self):
        self.data.fleet_evidence.side_effect = DataQueryError("Warehouse unavailable")
        with self.assertRaisesRegex(DataQueryError, "Warehouse"):
            self.service.snapshot("run", self.cursor)
        self.store.save.assert_not_called()
        self.data.health_alerts.assert_not_called()


if __name__ == "__main__":
    unittest.main()
