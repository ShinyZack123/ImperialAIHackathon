-- Gold: per-GPU state at every assessed cursor. Unknown telemetry is its own state, never healthy.
CREATE OR REPLACE VIEW ${evidence}.gpu_state_timeline
COMMENT 'Per-GPU detector state (clear, warning, critical, unknown) at every assessed replay cursor, with the latest observed readings. Unknown means telemetry was missing or stale, not healthy.'
AS
SELECT s.run_id, s.as_of_ts, g.id AS gpu_id, g.vendor, g.rack_id, g.server_id, g.cooling_zone_id,
       g.state, g.telemetry_is_observed,
       g.metrics.temp_c AS temp_c, g.metrics.power_w AS power_w, g.metrics.gpu_util_pct AS gpu_util_pct, g.metrics.throttled AS throttled
FROM ${evidence}.detector_snapshots AS s
LATERAL VIEW explode(s.racks) exploded_racks AS rack
LATERAL VIEW explode(rack.gpus) exploded_gpus AS g
