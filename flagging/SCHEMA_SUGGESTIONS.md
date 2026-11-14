# Schema additions needed for deterministic flagging

Each item names the scenario that needs it. Without these, a rule cannot tell the fault from its benign twin.

## Must-have

1. **`MaintenanceRecord` definition.** It's referenced in `DataCenter` but never defined. Used by B04, B09 and F11.
   ```ts
   interface MaintenanceRecord {
     id: string; entityIds: string[]; type: string; description?: string;
     scheduledStart: string; scheduledEnd: string; actualStart?: string; actualEnd?: string;
     status: "scheduled" | "in_progress" | "completed" | "cancelled";
     expectedEffects: ("offline" | "link_down" | "telemetry_gap" | "power_off" | "reduced_capacity")[];
     ticketId?: string;
   }
   ```
2. **`MetricDefinition` with limits and baselines.** It's referenced but not defined. Rules need thresholds per metric, per entity model. Without them every rule hardcodes "87C".
   ```ts
   interface MetricDefinition {
     metric: string; unit: string; kind: "gauge" | "counter" | "event" | "state";
     samplingIntervalSeconds: number;
     limits?: { warn?: number; critical?: number; min?: number; max?: number }; // hard bounds
     plausibleRange?: { min: number; max: number; maxDeltaPerSecond?: number }; // B05 spike detection
     minConsecutiveSamples?: number;   // debounce: B03, B05
     baselineRef?: string;             // -> MetricBaseline
   }
   interface MetricBaseline {
     entityId: string; metric: string;
     conditionedOn?: { workloadState?: "idle" | "loaded"; workloadId?: string; hourOfDay?: number };
     mean: number; p05: number; p95: number; stddev: number; slopePerDay?: number;
     windowDays: number; computedAt: string;
   }
   ```
   Conditioning matters: B02 (idle to loaded) and B07 (time of day) look anomalous against one flat baseline.
3. **Workload phase / lifecycle events.** Used by B01, B10, F07 and F10. A `status` enum alone can't separate "checkpointing" from "hung".
   - Add `Workload.phase?: "init" | "train" | "eval" | "checkpoint" | "draining"`.
   - Add `Workload.checkpointPolicy?: { intervalSteps: number; expectedDurationSeconds: number; lastCheckpointStep?: number; lastCheckpointAt?: string }`.
   - Add `Workload.progress?: { lastStepAt: string }` for a progress heartbeat (F07).
4. **`Intervention` definition with an active window.** It's referenced but undefined. Used by B11: our own power cap must not be flagged as a fault.
   ```ts
   interface Intervention { id: string; decisionId?: string; actionType: string; targetEntityIds: string[];
     parameters: Record<string, unknown>; executedBy: string; activeFrom: string; activeUntil?: string;
     expectedEffects?: { metric: string; direction: "up" | "down"; maxChangePercent: number }[]; status: string; }
   ```
5. **Telemetry freshness.** Used by F02 and F06. Add `TelemetryRecord.observedAt` (when the device measured it) separate from `timestamp` (when it was ingested). `stale` alone is a judgement; the two timestamps make it computable. I used `labels.scrapeTimestamp` as a stand-in.
6. **Collector/connector health as an entity or metric.** Used by F06. Add `exporter.up` per entity, or `DataSourceReference.lastSuccessfulSyncAt`. "No data" must be distinguishable from "nothing happening".

## Should-have

7. **Throttle reasons as a typed enum**, split into `software` (SW_POWER_CAP, intentional) vs `hardware` (HW_THERMAL_SLOWDOWN, HW_POWER_BRAKE). Used by F01 vs B11.
8. **Redundancy state.** Used by F05. Add `RedundancyGroup { id; members: string[]; required: number; mode: "N" | "N+1" | "2N" }`. Today `redundancyGroupId` exists but not what "healthy" means.
9. **Event-type telemetry.** XID codes, breaker trips and replica crashes are events, not samples. Add `TelemetryRecord.kind: "sample" | "event"`, or a separate `DeviceEvent` store with `code` and `severityHint`. Used by F02, F05 and F08.
10. **Port-level entity status** (`NetworkPort.adminState: "up" | "down" | "reserved"`). Used by B09. "Down" is only a fault when admin state says it should be up.
11. **Counter reset handling.** Add `MetricDefinition.resetsOnReboot`, plus a `counterResetAt` label, so a rate rule doesn't misfire after a reboot.
12. **`Anomaly.suppressedBy?: { reason: string; refId: string }`.** Record why something was NOT flagged (maintenance, intervention, phase). This gives you audit data and training data for free.

## Nice-to-have

- `Workload.expectedUtilizationPercent` per phase, for "unexpected low throughput".
- `Connection.sharedRiskGroupId` for cables in the same tray or optics from the same batch.
- `PowerSystem.electrical.breakerRatingA`, because a PDU's capacity and its breaker limits differ.
