#!/usr/bin/env python3
"""Step 7: generalisation check on fresh scenarios the flagger was not tuned on.

    python3 generalise.py            # 20 fresh scenarios + extras, prints false positives / misses
    python3 generalise.py --save     # also write them to generated/
    python3 generalise.py --seeds 5  # repeat every scenario with 5 different noise seeds

Each fresh scenario = the full fault-free world (all 32 GPUs, cooling, power, network, jobs)
plus one injector with a different seed, entity, timing and magnitude from the 23 labelled ones.
Labels come from the injector's own rule (e.g. checkpoint longer than 3x expected = fault),
never from the flagger. Magnitudes in the undefined grey zone (e.g. a checkpoint between 1.5x
and 3x expected) are deliberately not used: the answer key does not say which side they fall.
"""
import argparse, json, os, sys, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import generate
from test_scenarios import TOPO, run, root_ok, why, table

D = "2026-11-17T"
# (name, kwargs for the window, [(injector, params)])
FRESH = [
    ("G01-throttle-other-gpu-hotter",   dict(start=D + "03:00:00Z", minutes=30, step=30), [("thermal_throttle", dict(gpu="gpu-12-01-02", onset=4, peak=93, ramp_min=8))]),
    ("G02-hot-gpu-below-limit",         dict(start=D + "05:10:00Z", minutes=30, step=30), [("thermal_throttle", dict(gpu="gpu-12-02-01", onset=5, peak=82, ramp_min=12))]),
    ("G03-spike-not-marked-invalid",    dict(start=D + "14:20:00Z", minutes=15, step=30), [("spurious_sample", dict(gpu="gpu-12-01-07", at=9, value=118.0, invalid=False))]),
    ("G04-ecc-burst-no-dbe",            dict(start=D + "11:30:00Z", minutes=20, step=30), [("ecc_burst", dict(gpu="gpu-12-02-06", at=6, sbe=25, dbe=False))]),
    ("G05-background-ecc-busier",       dict(start=D + "00:00:00Z", minutes=1440, step=3600), [("background_ecc", dict(gpu="gpu-12-01-05", extra=5))]),
    ("G06-crc-trickle",                 dict(start=D + "13:15:00Z", minutes=40, step=60), [("link_crc", dict(port="leaf-12-p08", onset=5, crc_per_min=0.05, impact=False))]),
    ("G07-cdu-flow-62pct",              dict(start=D + "15:30:00Z", minutes=40, step=60), [("cdu_flow_drop", dict(onset=12, frac=0.62))]),
    ("G08-pdu-rack12-feed-a-lost",      dict(start=D + "08:00:00Z", minutes=30, step=60), [("pdu_feed_loss", dict(rack="12", side="a", onset=9))]),
    ("G09-exporter-hang-training-node", dict(start=D + "19:40:00Z", minutes=30, step=30), [("exporter_hang", dict(server="server-12-02", onset=11))]),
    ("G10-hung-rank-later",             dict(start=D + "22:00:00Z", minutes=30, step=30), [("hung_rank", dict(gpu="gpu-12-01-05", onset=15))]),
    ("G11-job-completes-early",         dict(start=D + "23:20:00Z", minutes=30, step=30), [("job_complete", dict(at=7))]),
    ("G12-job-starts-early",            dict(start=D + "17:40:00Z", minutes=30, step=30), [("job_start", dict(at=5))]),
    ("G13-replica-crash-milder",        dict(start=D + "12:30:00Z", minutes=30, step=30), [("replica_crash", dict(onset=10, peak_p99=1400.0))]),
    ("G14-traffic-surge-within-sla",    dict(start=D + "10:00:00Z", minutes=30, step=30), [("traffic_change", dict(onset=6, factor=1.25))]),
    ("G15-checkpoint-overrun-8min",     dict(start=D + "07:00:00Z", minutes=30, step=30), [("checkpoint", dict(at=8, duration_s=480))]),
    ("G16-checkpoint-130s",             dict(start=D + "08:30:00Z", minutes=20, step=10), [("checkpoint", dict(at=5, duration_s=130))]),
    ("G17-down-past-maintenance-end",   dict(start=D + "04:00:00Z", minutes=60, step=60), [("offline", dict(server="server-13-02", down_from=0, maint=(-60, 12)))]),
    ("G18-outage-inside-new-window",    dict(start=D + "01:00:00Z", minutes=90, step=60), [("offline", dict(server="server-13-02", down_from=10, down_to=70, maint=(5, 80)))]),
    ("G19-drift-10C-two-days",          dict(start="2026-11-18T00:00:00Z", minutes=2880, step=1800), [("thermal_drift", dict(gpu="gpu-12-02-06", total_c=10.0))]),
    ("G20-logged-power-cap-450W",       dict(start=D + "16:00:00Z", minutes=30, step=30), [("power_cap", dict(onset=8, to_w=450, slowdown=0.125))]),
]
EXTRA = [   # more near-miss twins and plain fault-free runs
    ("X01-power-cap-not-logged",        dict(start=D + "16:00:00Z", minutes=30, step=30), [("power_cap", dict(onset=8, to_w=450, slowdown=0.125, logged=False))]),
    ("X02-power-cap-overshoots",        dict(start=D + "16:00:00Z", minutes=30, step=30), [("power_cap", dict(onset=8, to_w=450, slowdown=0.30))]),
    ("X03-uplink-down-in-service",      dict(start=D + "09:00:00Z", minutes=30, step=60), [("link_down", dict(port="leaf-13-p07", onset=8))]),
    ("X04-hall-overheat",               dict(start=D + "13:00:00Z", minutes=60, step=60), [("hall_overheat", dict(onset=10, peak=29.5))]),
    ("X05-warm-day-bigger-swing",       dict(start=D + "00:00:00Z", minutes=1440, step=1800), [("diurnal_swing", dict(amp=3.3))]),
    ("X06-crc-slow-climb",              dict(start=D + "13:15:00Z", minutes=40, step=60), [("link_crc", dict(port="leaf-12-p08", onset=8, crc_per_min=150.0))]),
    ("X07-pcie-fewer-replays",          dict(start=D + "06:30:00Z", minutes=20, step=30), [("pcie_downtrain", dict(gpu="gpu-12-02-03", onset=7, replay_per_min=300.0, gen=4, width=16))]),
    ("X08-small-drift-2C",              dict(start="2026-11-18T00:00:00Z", minutes=2880, step=1800), [("thermal_drift", dict(gpu="gpu-12-02-06", total_c=2.0))]),
    ("X09-power-transient-699W",        dict(start=D + "10:40:00Z", minutes=10, step=5), [("power_transient", dict(gpu="gpu-12-02-00", at=(2.1, 5.5, 8.0), peak=699.0, samples=2))]),
    ("X10-two-faults-at-once",          dict(start=D + "18:00:00Z", minutes=30, step=30), [("pcie_downtrain", dict(gpu="gpu-12-01-00", onset=6)), ("replica_crash", dict(onset=9))]),
    ("N01-fault-free-morning",          dict(start=D + "09:00:00Z", minutes=60, step=30), []),
    ("N02-fault-free-overnight",        dict(start=D + "21:00:00Z", minutes=360, step=300), []),
    ("N03-fault-free-full-day",         dict(start="2026-11-19T00:00:00Z", minutes=1440, step=600), []),
    ("N04-fault-free-job-running",      dict(start=D + "14:00:00Z", minutes=60, step=30), [("job_running", {})]),
]


def evaluate(specs, seeds, save=False):
    rows, bad = [], []
    for name, win, inj in specs:
        for seed in seeds:
            sc = generate.build("normal", seed, injectors=inj, name=name, **win)
            if save:
                os.makedirs(os.path.join(HERE, "generated"), exist_ok=True)
                with open(os.path.join(HERE, "generated", sc["scenarioId"] + ".json"), "w") as f: f.write(generate.dumps(sc))
            exp = sc["expected"]
            res = run(sc)
            ent = root_ok(res, exp["rootCauseEntityId"], exp["acceptEntityIds"])
            rows.append((sc["scenarioId"], exp["flag"], res, ent))
            if res["flagged"] != exp["flag"] or not ent:
                kind = "FALSE POSITIVE" if res["flagged"] and not exp["flag"] else "MISS" if exp["flag"] and not res["flagged"] else "WRONG ENTITY"
                bad.append((kind, sc["scenarioId"], exp["reason"], res))
    return rows, bad


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=1, help="noise seeds per scenario")
    ap.add_argument("--save", action="store_true")
    ap.add_argument("--verbose", action="store_true", help="print flag strings for failures")
    a = ap.parse_args()
    seeds = [101 + i for i in range(a.seeds)]
    t0 = time.time()
    total_bad = 0
    for title, specs in (("20 fresh scenarios", FRESH), ("extra twins and fault-free runs", EXTRA)):
        rows, bad = evaluate(specs, seeds, a.save)
        print(f"\n=== {title} ({len(rows)} runs) ===")
        table(rows)
        fp = sum(1 for k, *_ in bad if k == "FALSE POSITIVE"); miss = sum(1 for k, *_ in bad if k == "MISS")
        wrong = len(bad) - fp - miss
        print(f"{len(rows) - len(bad)}/{len(rows)} correct. False positives: {fp}. Misses: {miss}. Wrong entity: {wrong}.")
        for kind, sid, reason, res in bad:
            print(f"  {kind}: {sid} (truth: {reason})")
            for m in res["messages"][: (99 if a.verbose else 1)]:
                print("      " + m.replace("\n", "\n      ")[: (10 ** 6 if a.verbose else 600)])
        total_bad += len(bad)
    print(f"\n{time.time() - t0:.0f} s")
    sys.exit(1 if total_bad else 0)
