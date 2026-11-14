"""Evidence adapter for either model teammate; this is not a model implementation.

Use in the backend/model process after loading its .env. Never expose evaluation
labels or credentials to an inference tool. The orchestrator creates one adapter
per run/cursor so an investigating model cannot request future replay data.
"""
from dataclasses import dataclass
from datetime import datetime, timedelta

from backend.databricks_data import DatabricksData


@dataclass(frozen=True)
class InvestigationEvidence:
    data: DatabricksData
    run_id: str
    as_of_ts: datetime

    def overview(self):
        return self.data.overview(self.run_id, self.as_of_ts)

    def gpu_history(self, gpu_id: str):
        return self.data.gpu_history(self.run_id, gpu_id, self.as_of_ts - timedelta(minutes=5), self.as_of_ts)

    def workload_history(self, gpu_id: str):
        return self.data.workload_history(self.run_id, gpu_id, self.as_of_ts - timedelta(minutes=5), self.as_of_ts)

    def rack_context(self, rack_id: str):
        return self.data.rack_context(self.run_id, rack_id, self.as_of_ts)

    def health_assessments(self):
        return self.data.health_alerts(self.run_id, self.as_of_ts)


# The orchestrator selects the cursor, then gives bound methods above to the model.
# evidence = InvestigationEvidence(data, run_id, datetime.fromisoformat("2026-11-14T09:07:30+00:00"))
# candidates = [row for row in evidence.health_assessments() if row["status"] == "active"]
# The deterministic teammate can replace the baseline rules. The diagnostic model
# should gather histories/context and emit the proposed incident contract.
# An inference result and any mitigation still need separate implementation.
