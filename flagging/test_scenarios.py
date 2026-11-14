#!/usr/bin/env python3
"""Step 6: one test per labelled scenario, with index.json as the answer key.

    python3 test_scenarios.py          # table + unittest result, exit code 1 on any failure

A scenario passes when
  - flag / no-flag matches index.json, and
  - for a flagged scenario, the labelled root-cause entity is among the flagged entities
    (a cable counts if either of its two ports is flagged), and
  - running it twice gives identical strings (determinism) and leaves the input untouched (purity).
The flagger never sees the `expected` block.
"""
import copy, json, os, sys, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from flagger import flag, flag_window, merge_context

with open(os.path.join(HERE, "topology.enriched.json")) as _f: TOPO = json.load(_f)
with open(os.path.join(HERE, "index.json")) as _f: INDEX = json.load(_f)
CABLES = {c["id"]: {c["sourcePortId"], c["destinationPortId"]} for c in TOPO["components"] if c["type"] == "cable"}


def run(scenario, topo=TOPO):
    """Run the flagger on one scenario dict (label removed). Returns a result dict."""
    sc = {k: v for k, v in scenario.items() if k != "expected"}
    ctx = merge_context(topo, sc)
    incidents, suppressed = flag_window(ctx, sc["telemetry"])
    ents = set()
    for i in incidents: ents |= {i.root_entity} | set(i.entities) | set(i.workloads)
    return {"flagged": bool(incidents), "entities": ents, "messages": [i.message for i in incidents],
            "roots": [i.root_entity for i in incidents],
            "methods": sorted({d for i in incidents for d in i.detectors}),
            "gates": sorted({f"{f.detector}: {why}" for f, why in suppressed})}


def root_ok(res, root, accept=()):
    if not res["flagged"] or root is None: return True
    want = {root} | set(accept) | CABLES.get(root, set())
    return bool(want & res["entities"])


def why(res):
    if res["flagged"]: return "fired: " + ", ".join(res["methods"])
    return ("suppressed by gate: " + "; ".join(res["gates"])) if res["gates"] else "nothing fired (statistics alone)"


def table(rows):
    print(f"{'scenario':44} {'expected':9} {'actual':9} {'entity':7} {'result':6} method that fired / gate that suppressed")
    for name, exp, res, ent_ok in rows:
        ok = res["flagged"] == exp and ent_ok
        print(f"{name:44} {'FLAG' if exp else 'no flag':9} {'FLAG' if res['flagged'] else 'no flag':9} "
              f"{'ok' if ent_ok else 'WRONG':7} {'PASS' if ok else 'FAIL':6} {why(res)}")


class ScenarioTests(unittest.TestCase):
    rows = []


def _make(entry):
    def test(self):
        with open(os.path.join(HERE, entry["file"])) as fh: sc = json.load(fh)
        before = copy.deepcopy(sc["telemetry"][:50])
        res = run(sc)
        ent = root_ok(res, sc["expected"].get("rootCauseEntityId"))
        ScenarioTests.rows.append((entry["scenarioId"], entry["flag"], res, ent))
        self.assertEqual(res["flagged"], entry["flag"], f"flag mismatch: {why(res)}")
        self.assertTrue(ent, f"root cause {sc['expected'].get('rootCauseEntityId')} not in {sorted(res['entities'])}")
        self.assertEqual(flag(sc["telemetry"], TOPO), res["messages"], "not deterministic")
        self.assertEqual(sc["telemetry"][:50], before, "flagger modified its input")
    return test


for _e in INDEX["scenarios"]:
    setattr(ScenarioTests, "test_" + _e["scenarioId"].replace("-", "_"), _make(_e))

if __name__ == "__main__":
    result = unittest.main(exit=False, verbosity=0).result
    order = {e["scenarioId"]: i for i, e in enumerate(INDEX["scenarios"])}
    rows = sorted(ScenarioTests.rows, key=lambda r: order[r[0]])
    print()
    table(rows)
    passed = sum(1 for n, exp, res, ent in rows if res["flagged"] == exp and ent)
    print(f"\n{passed}/{len(rows)} scenarios pass"
          f" (near-miss pairs: {sum(1 for a, b in INDEX['pairs'] if all(any(r[0] == x and r[2]['flagged'] == r[1] and r[3] for r in rows) for x in (a, b)))}/{len(INDEX['pairs'])})")
    with open(os.path.join(HERE, "flagger_results.json"), "w") as fh:
        json.dump({n: {"expectedFlag": exp, "flagged": res["flagged"], "why": why(res), "messages": res["messages"],
                       "suppressed": res["gates"]} for n, exp, res, _ in rows}, fh, indent=2)
    sys.exit(0 if result.wasSuccessful() else 1)
