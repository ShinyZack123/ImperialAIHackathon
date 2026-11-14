#!/usr/bin/env python3
"""Incident grouping, root choice, identity/status and optimisation opportunities.

    python3 test_incidents.py

Covers behaviour the labelled scenarios do not exercise:
  - two faults on one job are split or merged by time, and the root is what went wrong first;
  - an incident keeps one ID across a replay and is re-escalated only when something changes;
  - "recovered" needs positive evidence (silence and latched events stay active);
  - idle-but-healthy GPUs are reported as opportunities, never as flags, and unknown is never idle.
"""
import copy, json, os, sys, unittest
from datetime import datetime, timedelta, timezone
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bridge, generate                                                                     # noqa: E402
from flagger import IncidentTracker, find_opportunities, flag, flag_window, merge_context, ts   # noqa: E402

with open(os.path.join(HERE, "topology.enriched.json")) as _f: TOPO = json.load(_f)
D = "2026-11-14T"
GPU, PORT, JOB = "gpu-12-02-05", "leaf-12-p07", "training-job-104"


def two_faults(crc_onset, minutes=70):
    """A GPU overheats at minute 6; an uplink on the same training job starts throwing CRC errors later."""
    return generate.build("normal", 7, D + "09:00:00Z", minutes, 30, name="T", title="t", injectors=[
        ("thermal_throttle", dict(gpu=GPU, onset=6, peak=90.0, ramp_min=9)), ("link_crc", dict(port=PORT, onset=crc_onset))])


def run(sc, upto=None):
    recs = [r for r in sc["telemetry"] if upto is None or r["timestamp"] <= upto]
    ctx = merge_context(TOPO, sc)
    incidents, _ = flag_window(ctx, recs)
    return ctx, recs, incidents


def hot_gpu(hot_from, hot_to, total, gpu="gpu-12-01-00"):
    """Hand-made series: one GPU above its critical temperature limit between two minutes, normal otherwise."""
    t0 = datetime(2026, 11, 14, 9, tzinfo=timezone.utc)
    return [{"entityId": gpu, "metric": "gpu.temperature_c", "unit": "C", "value": 91.0 if hot_from <= i / 2 < hot_to else 66.0 + (i % 3) * 0.4,
             "timestamp": (t0 + timedelta(seconds=30 * i)).strftime("%Y-%m-%dT%H:%M:%SZ"), "quality": {"valid": True, "stale": False}}
            for i in range(total * 2)]


class Grouping(unittest.TestCase):
    def test_faults_far_apart_on_one_job_are_two_incidents(self):
        _, _, incidents = run(two_faults(40))
        self.assertEqual(sorted(i.root_entity for i in incidents), [GPU, PORT])
        by_root = {i.root_entity: i for i in incidents}
        self.assertLess(by_root[GPU].onset, by_root[PORT].onset)
        for inc in incidents:                      # each one carries its own share of the job's slowdown
            self.assertIn(JOB, inc.workloads)
            self.assertTrue(any(f.entity == JOB for f in inc.findings))
        self.assertFalse(any(f.entity == PORT for f in by_root[GPU].findings))
        self.assertFalse(any(f.entity == GPU for f in by_root[PORT].findings))

    def test_root_is_what_went_wrong_first_not_the_layer_nearest_the_facility(self):
        _, _, incidents = run(two_faults(14))      # close enough in time to be one incident
        self.assertEqual(len(incidents), 1)
        self.assertEqual(incidents[0].root_entity, GPU)
        self.assertIn(PORT, incidents[0].entities)  # the later fault is still reported as evidence

    def test_near_simultaneous_faults_fall_back_to_layer_order(self):
        _, _, incidents = run(two_faults(8, minutes=40))
        self.assertEqual([i.root_entity for i in incidents], [PORT])

    def test_facility_fault_still_owns_its_slow_downstream_effects(self):
        sc = bridge.load_scenario("S02-cooling-pump-fault")
        _, _, incidents = run(sc)
        self.assertEqual([i.root_entity for i in incidents], ["cdu-02"])
        self.assertTrue(any(f.entity.startswith("gpu-") for f in incidents[0].findings))


class IdentityAndStatus(unittest.TestCase):
    def replay(self, sc, steps=24):
        t0, t1 = ts(sc["window"]["start"]), ts(sc["window"]["end"])
        tracker, seen = IncidentTracker(), []
        for i in range(steps):
            cut = datetime.fromtimestamp(t0 + (t1 - t0) * (i + 1) / steps, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            _, _, incidents = run(sc, cut)
            seen.append(tracker.update(incidents))
        return seen

    def test_one_fault_keeps_one_id_and_escalates_only_on_change(self):
        seen = self.replay(bridge.load_scenario("S01-straggler-gpu-thermal-throttle"))
        flagged = [step[0] for step in seen if step]
        self.assertGreater(len(flagged), 10)
        self.assertEqual(len({i.id for i in flagged}), 1)
        changes = [i.change for i in flagged if i.escalate]
        self.assertEqual(changes, ["new", "escalated"])          # warning first, critical once the limit is crossed
        self.assertTrue(all(i.change == "unchanged" for i in flagged if not i.escalate))

    def test_second_fault_gets_its_own_id_without_disturbing_the_first(self):
        seen = self.replay(two_faults(40), steps=28)
        final = {i.root_entity: i.id for i in seen[-1]}
        self.assertEqual(set(final), {GPU, PORT})
        self.assertNotEqual(final[GPU], final[PORT])
        first_ids = {i.id for step in seen for i in step if i.root_entity == GPU}
        self.assertEqual(first_ids, {final[GPU]})
        new = [i.id for step in seen for i in step if i.change == "new"]
        self.assertEqual(sorted(new), sorted(final.values()))

    def test_ids_are_deterministic_and_input_is_untouched(self):
        sc = two_faults(40)
        before = copy.deepcopy(sc)
        a = [(i.id, i.status, i.root_entity) for i in run(sc)[2]]
        b = [(i.id, i.status, i.root_entity) for i in run(sc)[2]]
        self.assertEqual(a, b)
        self.assertEqual(sc, before)

    def test_recovered_needs_the_condition_to_be_seen_clear(self):
        recovered, _ = flag_window(TOPO, hot_gpu(5, 15, 30))     # hot for 10 min, then 15 min of normal readings
        self.assertEqual([(i.status, i.severity) for i in recovered], [("recovered", "critical")])
        self.assertIn("Status: recovered", recovered[0].message)
        still_hot, _ = flag_window(TOPO, hot_gpu(5, 15, 15))     # window ends while it is still hot
        self.assertEqual([i.status for i in still_hot], ["active"])
        just_cooled, _ = flag_window(TOPO, hot_gpu(5, 15, 17))   # 2 min of normal readings is not yet recovery
        self.assertEqual([i.status for i in just_cooled], ["active"])
        went_silent, _ = flag_window(TOPO, hot_gpu(5, 15, 15) + [                 # other telemetry carries on,
            dict(r, entityId="gpu-12-01-01") for r in hot_gpu(99, 99, 30)])       # this GPU just stops reporting
        self.assertEqual([i.status for i in went_silent], ["active"])

    def test_tracker_reports_recovery_once(self):
        tracker = IncidentTracker()
        changes = []
        for total in (15, 17, 30, 32):
            incidents, _ = flag_window(TOPO, hot_gpu(5, 15, total))
            changes += [(i.change, i.escalate) for i in tracker.update(incidents)]
        self.assertEqual(changes, [("new", True), ("unchanged", False), ("recovered", True), ("unchanged", False)])

    def test_latched_events_never_recover(self):
        sc = bridge.load_scenario("S03-ecc-burst-gpu-off-bus")
        _, _, incidents = run(sc)
        self.assertEqual([i.status for i in incidents], ["active"])


class Opportunities(unittest.TestCase):
    def opportunities(self, sid):
        sc = bridge.load_scenario(sid)
        ctx, recs, incidents = run(sc)
        return find_opportunities(ctx, recs, incidents=incidents), incidents, ctx, recs

    def test_finished_job_still_holding_gpus_is_an_opportunity_not_a_flag(self):
        found, incidents, ctx, recs = self.opportunities("B10-job-completed-gpus-idle")
        self.assertEqual(incidents, [])
        self.assertEqual(flag(recs, ctx), [])                                    # the answer key still says: no flag
        self.assertEqual([(o.kind, len(o.entities), o.workloads) for o in found], [("reclaimable_gpus", 8, ["finetune-job-311"])])
        est = found[0].estimate
        self.assertGreater(est["idleGpuHours"], 1.5)
        self.assertGreater(est["idleEnergyKwh"], 0)
        self.assertIn("Release the allocation", found[0].proposal)

    def test_gpus_held_for_a_queued_job_are_reported_until_it_starts(self):
        found, _, _, _ = self.opportunities("S11-logged-power-cap")              # fine-tune job queued all window
        self.assertEqual([(o.kind, len(o.entities)) for o in found], [("reserved_idle_gpus", 8)])
        started, _, _, _ = self.opportunities("S09-busy-but-healthy-shift")      # the same job starts at minute 12
        self.assertEqual(started, [])

    def test_idle_gpus_of_a_running_job_are_a_fault_not_an_opportunity(self):
        found, incidents, _, _ = self.opportunities("F07-hung-rank-silent-stall")
        self.assertTrue(incidents)
        self.assertFalse(any(g.startswith("gpu-12-") for o in found for g in o.entities))

    def test_unknown_is_never_idle(self):
        sc = bridge.load_scenario("S11-logged-power-cap")
        ctx = merge_context(TOPO, sc)
        held = set(next(w for w in TOPO["workloads"] if w["id"] == "finetune-job-311")["allocation"]["gpuIds"])
        victim = sorted(held)[0]
        cut = sc["telemetry"][len(sc["telemetry"]) // 2]["timestamp"]
        stale = [dict(r, quality=dict(r["quality"], stale=True)) if r["entityId"] == victim and r["timestamp"] > cut else r for r in sc["telemetry"]]
        silent = [r for r in sc["telemetry"] if not (r["entityId"] == victim and r["timestamp"] > cut)]
        for recs in (stale, silent):
            found = find_opportunities(ctx, recs)
            self.assertEqual(len(found), 1)
            self.assertNotIn(victim, found[0].entities)
            self.assertEqual(len(found[0].entities), 7)

    def test_capacity_behind_an_open_incident_is_withheld(self):
        found, incidents, ctx, recs = self.opportunities("S02-cooling-pump-fault")   # every GPU depends on the failed CDU
        self.assertTrue(incidents)
        self.assertEqual(found, [])
        self.assertTrue(find_opportunities(ctx, recs))                               # ...and listed if incidents are not supplied

    def test_unallocated_idle_gpus_and_determinism(self):
        topo = copy.deepcopy(TOPO)
        topo["workloads"] = [w for w in topo["workloads"] if w["id"] != "finetune-job-311"]
        sc = bridge.load_scenario("S11-logged-power-cap")
        a = find_opportunities(merge_context(topo, sc), sc["telemetry"])
        b = find_opportunities(merge_context(topo, sc), sc["telemetry"])
        self.assertEqual([(o.kind, len(o.entities), o.workloads) for o in a], [("unallocated_idle_gpus", 8, [])])
        self.assertEqual([(o.id, o.text) for o in a], [(o.id, o.text) for o in b])


class BridgeOutput(unittest.TestCase):
    def test_replay_steps_carry_identity_escalations_and_opportunities(self):
        body = bridge.analyse("S01-straggler-gpu-thermal-throttle")
        flagged = [s for s in body["steps"] if s["incidents"]]
        self.assertEqual(len({s["incidents"][0]["id"] for s in flagged}), 1)
        self.assertEqual(sum(s["escalations"] for s in body["steps"]), 2)
        self.assertLess(sum(s["escalations"] for s in body["steps"]), len(flagged))
        for key in ("id", "status", "change", "escalate", "onset", "lastSeen"):
            self.assertIn(key, flagged[-1]["incidents"][0])
        last = body["steps"][-1]["opportunities"]
        self.assertEqual([(o["kind"], len(o["entities"])) for o in last], [("reserved_idle_gpus", 8)])
        self.assertEqual(len(last[0]["twin"]), 8)
        self.assertTrue(all(t.startswith("gpu-b02-") for t in last[0]["twin"]))
        self.assertFalse(set(last[0]["twin"]) & set(body["steps"][-1]["states"]))   # opportunities never change health state
        json.dumps(body)


if __name__ == "__main__":
    unittest.main(verbosity=1)
