/** Copy into the coded website. These requests go to our local backend.
 * Never put Databricks credentials in frontend code or VITE_/NEXT_PUBLIC_ values.
 * Keep the displayed replay time, source and synthetic-data label visible.
 */
const API_BASE = "http://127.0.0.1:8010";

type Envelope<T> = {
  schema_version: string;
  source: "databricks";
  synthetic: true;
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
export type Assessment = {
  alert_id: string; gpu_id: string; rack_id: string;
  rule_id: string; status: "active" | "clear" | "unknown";
  severity: "info" | "warning" | "critical";
  summary: string; evidence_json: string;
};

async function getJson<T>(path: string, params: Record<string, string> = {}): Promise<T> {
  const query = new URLSearchParams(params);
  const response = await fetch(`${API_BASE}${path}?${query}`);
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new Error(typeof body.detail === "string" ? body.detail : `Data request failed (${response.status})`);
  }
  return response.json();
}

export const listRuns = () => getJson<Envelope<ReplayRun[]>>("/api/runs");
export const getOverview = (runId: string, replayTime: string) =>
  getJson<Envelope<Overview>>("/api/overview", {run_id: runId, as_of_ts: replayTime});
export const getRack = (runId: string, rackId: string, replayTime: string) =>
  getJson<Envelope<Record<string, unknown>[]>>(`/api/racks/${encodeURIComponent(rackId)}`, {run_id: runId, as_of_ts: replayTime});
export const getGpuHistory = (runId: string, gpuId: string, replayTime: string) =>
  getJson<Envelope<Record<string, unknown>[]>>(`/api/gpus/${encodeURIComponent(gpuId)}/history`, {run_id: runId, as_of_ts: replayTime, window_minutes: "5"});
export const getAssessments = (runId: string, replayTime: string) =>
  getJson<Envelope<Assessment[]> & {assessment_available: boolean; counts: Record<"active" | "clear" | "unknown", number>}>("/api/health-assessments", {run_id: runId, as_of_ts: replayTime});

// Example usage inside your page's load handler:
// const { data: runs } = await listRuns();
// const snapshot = await getOverview(runs[0].run_id, "2026-11-14T09:07:30Z");
// const wattsLabel = snapshot.data.observed_gpu_power_kw?.toFixed(2) ?? "Unknown";
// Display wattsLabel + " kW observed GPU power" with observed/total GPU counts.
// Only show "no active rule alerts" if assessment_available is true; unknown is separate.
// Await each replay refresh before starting the next. SQL cold starts can take time.
