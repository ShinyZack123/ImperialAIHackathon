"""One official detector path shared by the website and diagnostic model tools."""
from datetime import datetime, timedelta, timezone

from backend.databricks_data import DataQueryError, DatabricksData
from backend.flagging_adapter import generate_snapshot
from backend.result_store import DatabricksResultStore


class OfficialDetector:
    def __init__(self, data: DatabricksData, store=None):
        self.data = data
        self.store = store if store is not None else DatabricksResultStore(data)

    def snapshot(self, run_id: str, as_of_ts: datetime):
        if as_of_ts.tzinfo is None or as_of_ts.utcoffset() is None:
            raise ValueError("Official assessments require a timezone-aware replay time.")
        cursor = as_of_ts.astimezone(timezone.utc)
        start = cursor - timedelta(minutes=15)
        evidence = self.data.fleet_evidence(run_id, cursor, start)
        try:
            snapshot = generate_snapshot(evidence, run_id, cursor, start)
        except ValueError:
            raise DataQueryError("Databricks evidence does not match the detector contract. Check source IDs, units, values and timestamps.") from None
        except Exception:
            raise DataQueryError("The official deterministic detector is unavailable. No baseline substitute was used.") from None
        # Never replace a detector/store failure with the six comparison-baseline rules.
        snapshot["persistence"] = self.store.save(snapshot, evidence)
        return snapshot
