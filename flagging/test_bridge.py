#!/usr/bin/env python3
"""Tests for the console bridge: label agreement, ID translation and input validation.

    python3 test_bridge.py          # first run builds the showcase scenarios (~45 s), then cached
"""
import os, sys, tempfile, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import bridge


class Catalog(unittest.TestCase):
    def test_catalog_lists_showcase_and_labelled_scenarios(self):
        cat = bridge.catalog()["scenarios"]
        groups = {s["group"] for s in cat}
        self.assertEqual(groups, {"showcase", "fault", "benign"})
        self.assertEqual(len([s for s in cat if s["group"] == "showcase"]), len(bridge.SHOWCASE))

    def test_rejects_unknown_or_malformed_ids(self):
        for bad in ("../topology", "x;rm", "", "nope-scenario"):
            with self.assertRaises(bridge.BridgeError): bridge.load_scenario(bad)
        with self.assertRaises(bridge.BridgeError): bridge.entity("F01-gpu-thermal-throttle", "../../etc")
        with self.assertRaises(bridge.BridgeError): bridge.entity("F01-gpu-thermal-throttle", "gpu-z99-01")


class Mapping(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sc = bridge.load_scenario("F01-gpu-thermal-throttle")
        topo = bridge._load_json(os.path.join(HERE, "topology.enriched.json"))
        cls.tm = bridge.TwinMap(bridge.merge_context(topo, sc))

    def test_gpu_ids_round_trip(self):
        self.assertEqual(self.tm.sources_for("gpu-a02-06"), ["gpu-12-02-05"])
        self.assertEqual(self.tm.twin_for("gpu-12-02-05"), {"gpu-a02-06": "direct"})
        self.assertEqual(len(self.tm.gpu_src), 32)

    def test_shared_infrastructure_lands_on_every_rack_it_serves(self):
        self.assertEqual(set(self.tm.twin_for("pdu-13-b")), {"power-rack-b01", "power-rack-b02"})
        self.assertEqual(set(self.tm.twin_for("leaf-12-p07")), {"link-a01", "link-a02"})
        self.assertEqual(len(self.tm.twin_for("cdu-02")), 4)

    def test_workload_findings_mark_blast_radius_not_fault(self):
        states = set(self.tm.twin_for("training-job-104").values())
        self.assertEqual(states, {"impacted"})


class Analysis(unittest.TestCase):
    def test_every_showcase_scenario_matches_its_label(self):
        for sid in bridge.SHOWCASE:
            with self.subTest(sid):
                a = bridge.analyse(sid)
                self.assertEqual(len(a["steps"]), bridge.STEPS)
                self.assertTrue(a["verdict"]["match"], a["verdict"])
                self.assertTrue(a["verdict"]["rootMatch"], a["verdict"])

    def test_replay_is_causal_and_deterministic(self):
        a = bridge.analyse("F01-gpu-thermal-throttle")
        counts = [s["records"] for s in a["steps"]]
        self.assertEqual(counts, sorted(counts))                 # never sees the future
        self.assertEqual(a["steps"][0]["incidents"], [])          # nothing flagged before onset
        self.assertEqual(a["steps"][-1]["states"]["gpu-a02-06"], "critical")
        saved = bridge.CACHE
        with tempfile.TemporaryDirectory() as fresh:            # recompute without the cache
            bridge.CACHE = fresh
            try: again = bridge.analyse("F01-gpu-thermal-throttle")
            finally: bridge.CACHE = saved
        self.assertEqual(a["steps"], again["steps"])

    def test_stale_telemetry_is_unknown_not_healthy(self):
        a = bridge.analyse("S05-exporter-hang-stale-telemetry")
        states = a["steps"][-1]["states"]
        self.assertEqual({states[f"gpu-b01-{g:02d}"] for g in range(1, 9)}, {"stale"})

    def test_benign_twin_stays_quiet_and_reports_its_gate(self):
        a = bridge.analyse("S11-logged-power-cap")
        self.assertFalse(a["verdict"]["flagged"])
        self.assertTrue(any("intervention" in g for g in a["steps"][-1]["gates"]))

    def test_entity_log_and_limits(self):
        e = bridge.entity("S01-straggler-gpu-thermal-throttle", "gpu-a02-06")
        self.assertEqual([s["id"] for s in e["sources"]], ["gpu-12-02-05"])
        self.assertTrue(e["log"])
        temp = e["limits"]["gpu-12-02-05|gpu.temperature_c"]
        self.assertIn(87, [l["value"] for l in temp])


if __name__ == "__main__":
    unittest.main(verbosity=2)
