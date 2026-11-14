/** Optional TypeScript client for the website's same-origin Databricks proxy.
 * Never put Databricks credentials in frontend code or VITE_/NEXT_PUBLIC_ values.
 * Keep the displayed replay time, source and synthetic-data label visible.
 */
const API_BASE = "";

type Envelope<T> = {
  schema_version: string;
  source: "databricks";
  synthetic: true;
  delivery?: "connected" | "saved_replay";
  exported_at?: string;
  run_id?: string;
  as_of_ts?: string;
  data: T;
};

export type ReplayRun = { run_id: string; start_ts: string; end_ts: string; sample_count: number };
export type Overview = {
  total_gpu_count: number;
  observed_gpu_count: number;
  unknown_gpu_count: number;
  power_reporting_gpu_count: number;
  observed_gpu_power_kw: number | null;
  observed_avg_gpu_util_pct: number | null;
  observed_avg_temp_c: number | null;
};
export type OfficialSnapshot = {
  source: "databricks"; synthetic: true; run_id: string; as_of_ts: string;
  delivery?: "connected" | "saved_replay"; exported_at?: string;
  state_scope: "analysis_window"; window: {start: string; end: string};
  overview: Overview;
  racks: {id: string; gpus: {
    id: string; state: "clear" | "warning" | "critical" | "unknown";
    telemetry_is_observed: boolean; metrics: Record<string, number | boolean | null>;
  }[]}[];
  incidents: {severity: string; rootEntity: string; affectedGpuIds: string[]; message: string; findings: Record<string, unknown>[]}[];
  engine: {module: "flagging.flagger.flag_window"; version: string; deterministic: true};
  persistence: {status: "saved"; table: string; evidence_sha256: string}
    | {status: "not_saved"; reason: "saved_replay"};
  limitations: string[];
};

async function getJson<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}${path}?${query}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.error === "string" ? body.error : typeof body.detail === "string" ? body.detail : `Data request failed (${response.status})`);
  }
  return response.json();
}

export const listRuns = () => getJson<Envelope<ReplayRun[]>>("/api/databricks/runs");
export const getSnapshot = (runId: string, replayTime: string) =>
  getJson<Envelope<OfficialSnapshot>>("/api/databricks/snapshot", {run_id: runId, as_of_ts: replayTime});
export const getGpuHistory = (runId: string, gpuId: string, replayTime: string) =>
  getJson<Envelope<Record<string, unknown>[]>>(`/api/databricks/gpus/${encodeURIComponent(gpuId)}/history`, {run_id: runId, as_of_ts: replayTime, window_minutes: "5"});
export const getWorkloadHistory = (runId: string, gpuId: string, replayTime: string) =>
  getJson<Envelope<Record<string, unknown>[]>>(`/api/databricks/gpus/${encodeURIComponent(gpuId)}/workload-history`, {run_id: runId, as_of_ts: replayTime, window_minutes: "5"});

// Example usage inside your page's load handler:
// const { data: runs } = await listRuns();
// const snapshot = await getSnapshot(runs[0].run_id, "2026-11-14T09:10:45Z");
// const wattsLabel = snapshot.data.overview.observed_gpu_power_kw?.toFixed(2) ?? "Unknown";
// Display wattsLabel + " kW observed GPU power" with observed/total GPU counts.
// Use snapshot.data.incidents as the sole official findings; unknown is separate.
// Always display delivery/exported_at. Saved replay runs the detector locally and makes no cloud write.
// Clear means no findings in the analysis window, not a guarantee of healthy hardware.
// Await each replay refresh before starting the next. SQL cold starts can take time.
