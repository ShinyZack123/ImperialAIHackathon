#!/usr/bin/env python3
"""Run the flagger on every scenario (labels hidden) and compare with expected labels."""
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from flagger import flag_window, merge_context
root = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))
topo = json.load(open(os.path.join(root, "topology.enriched.json")))
idx = json.load(open(os.path.join(root, "index.json")))
rows, outputs, ok = [], {}, 0
for s in idx["scenarios"]:
    sc = json.load(open(os.path.join(root, s["file"])))
    expected = sc.pop("expected")            # the flagger never sees the label
    t0 = time.perf_counter()
    incidents, suppressed = flag_window(merge_context(topo, sc), sc["telemetry"])
    ms = (time.perf_counter() - t0) * 1000
    got = bool(incidents)
    hit = got == expected["flag"]
    flagged_ents = set([i.root_entity for i in incidents] + sum([i.entities for i in incidents], []))
    rc = expected.get("rootCauseEntityId")
    cab = next((c for c in topo["components"] if c["id"] == rc and c["type"] == "cable"), None)
    root_ok = (not got) or rc in flagged_ents or bool(cab and {cab["sourcePortId"], cab["destinationPortId"]} & flagged_ents)
    ok += hit
    rows.append((s["scenarioId"], expected["flag"], got, hit, root_ok, sorted({d for i in incidents for d in i.detectors}),
                 sorted({f"{f.detector}:{w}" for f, w in suppressed}), ms, len(sc["telemetry"])))
    outputs[s["scenarioId"]] = {"expectedFlag": expected["flag"], "flagged": got,
                                "messages": [i.message for i in incidents],
                                "suppressed": [f"{f.entity} {f.metric} ({f.detector}): {w}" for f, w in suppressed]}
print(f"{'scenario':42} exp  got  ok  root  detectors / suppressed")
for r in rows:
    print(f"{r[0]:42} {str(r[1])[0]}    {str(r[2])[0]}    {'Y' if r[3] else 'N'}   {'Y' if r[4] else 'N'}    {r[5]} {('SUPPRESSED ' + str(r[6])) if r[6] else ''}  {r[7]:.0f}ms/{r[8]}rec")
print(f"\n{ok}/{len(rows)} correct")
json.dump(outputs, open(os.path.join(root, "flagger_results.json"), "w"), indent=2)
