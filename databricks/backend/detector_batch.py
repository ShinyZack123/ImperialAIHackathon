"""Assess many replay cursors in one process with the official detector.

Shared by the Databricks detector job (notebook 04) and the tests. Evidence is
read once per replay, then each cursor sees only its own 15-minute window,
exactly as `OfficialDetector` does through the SQL warehouse. Results are
prepared by the result store's own validation and hashing, so a cursor assessed
here has the same audit key as one assessed by the backend. No I/O happens in
this module: callers supply evidence rows and write the returned records.
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta, timezone

from backend.flagging_adapter import generate_snapshot
from backend.lakehouse import _utc, plan_cursors
from backend.result_store import _prepare, detector_version


WINDOW = timedelta(minutes=15)
STREAMS = ("telemetry", "workloads", "cooling")
MAX_CURSORS = 2000


class ReplayEvidence:
    """One replay's rows, indexed by time so each window is a slice, not a scan."""

    def __init__(self, run_id: str, streams: dict[str, list[dict]]):
        if set(streams) != set(STREAMS):
            raise ValueError("Evidence must contain telemetry, workloads and cooling rows.")
        self.run_id = run_id
        self._rows, self._times = {}, {}
        for name in STREAMS:
            ordered = sorted(((_utc(row["event_ts"]), row) for row in streams[name]), key=lambda item: item[0])
            if any(row.get("run_id") != run_id for _, row in ordered):
                raise ValueError("Evidence rows from another replay were supplied.")
            self._times[name] = [point for point, _ in ordered]
            self._rows[name] = [row for _, row in ordered]

    def window(self, start: datetime, end: datetime) -> dict[str, list[dict]]:
        result = {}
        for name in STREAMS:
            times = self._times[name]
            result[name] = self._rows[name][bisect_left(times, start):bisect_right(times, end)]
        return result


def assess(inventory: list[dict], replays: list[dict], evidence: dict[str, ReplayEvidence], *,
           step_seconds: int, already_assessed: set, max_cursors: int = 500, now: datetime | None = None) -> dict:
    """Assess every planned cursor not yet saved by this detector version.

    `replays` are rows of (run_id, start_ts, end_ts); `already_assessed` holds
    (run_id, aware datetime) pairs saved by the current detector version.
    Returns audit records ready for official_detector_results plus counts.
    """
    if isinstance(max_cursors, bool) or not isinstance(max_cursors, int) or not 1 <= max_cursors <= MAX_CURSORS:
        raise ValueError(f"max_cursors must be an integer from 1 to {MAX_CURSORS}.")
    generated_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    version = detector_version()
    records, skipped, incidents, planned = [], 0, 0, 0
    for replay in replays:
        run_id = replay["run_id"]
        if run_id not in evidence:
            raise ValueError("A planned replay has no evidence.")
        for cursor in plan_cursors(replay["start_ts"], replay["end_ts"], step_seconds):
            planned += 1
            if (run_id, cursor) in already_assessed:
                skipped += 1
                continue
            if len(records) >= max_cursors:
                continue
            start = cursor - WINDOW
            window = {"inventory": inventory, **evidence[run_id].window(start, cursor)}
            snapshot = generate_snapshot(window, run_id, cursor, start)
            run, cursor_iso, saved_version, evidence_hash, incident_count, snapshot_json = _prepare(snapshot, window)
            if saved_version != version:
                raise RuntimeError("The detector changed while the job was running.")
            incidents += incident_count
            records.append({
                "run_id": run, "as_of_ts": cursor, "as_of_iso": cursor_iso, "detector_version": saved_version,
                "evidence_sha256": evidence_hash, "incident_count": incident_count,
                "snapshot_json": snapshot_json, "generated_at": generated_at,
            })
    deferred = planned - skipped - len(records)
    return {"detector_version": version, "records": records, "planned": planned, "already_assessed": skipped,
            "deferred": deferred, "incident_observations": incidents}
