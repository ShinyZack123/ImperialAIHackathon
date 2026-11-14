#!/usr/bin/env python3
"""Step 8 demo: replay a fault and its benign near-miss twin side by side.

    python3 demo.py                      # stuck checkpoint (F10) vs normal checkpoint (B01)
    python3 demo.py --pair 2             # pairs 1-5 from index.json
    python3 demo.py --list
    python3 demo.py --fault F01-gpu-thermal-throttle --benign B05-single-spurious-sample
    python3 demo.py --delay 0.3          # pause between ticks, for a live audience

The telemetry is replayed in time order. At every tick the flagger sees only the records
so far, exactly as it would in production, and the two columns show what it decides.
"""
import argparse, json, os, sys, textwrap, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from flagger import flag_window, merge_context, ts

W = 62   # column width


def load(sid):
    with open(os.path.join(HERE, "scenarios", sid + ".json")) as f: return json.load(f)


def status(topo, sc, upto):
    recs = [r for r in sc["telemetry"] if ts(r["timestamp"]) <= upto]
    incidents, suppressed = flag_window(merge_context(topo, sc), recs)
    if incidents:
        return "FLAG", incidents[0].message.split("\n")[0], incidents
    gates = sorted({w for f, w in suppressed if f.detector != "quality"} | {w for f, w in suppressed if f.detector == "quality"})
    return ("gate", gates[-1], []) if gates else ("ok", "normal", [])


def main():
    with open(os.path.join(HERE, "index.json")) as f: index = json.load(f)
    with open(os.path.join(HERE, "topology.enriched.json")) as f: topo = json.load(f)
    pairs = [(f_, b) if f_.startswith("F") else (b, f_) for f_, b in index["pairs"]]
    pairs.sort(key=lambda p: p[0] != "F10-checkpoint-overrun-storage-stall")
    ap = argparse.ArgumentParser(description="Replay a fault and a benign near-miss side by side")
    ap.add_argument("--pair", type=int, default=1, help="which near-miss pair (1-%d)" % len(pairs))
    ap.add_argument("--fault"); ap.add_argument("--benign")
    ap.add_argument("--tick", type=float, default=2.0, help="minutes of telemetry per replay step")
    ap.add_argument("--delay", type=float, default=0.0, help="seconds to pause between steps")
    ap.add_argument("--list", action="store_true")
    a = ap.parse_args()
    titles = {s["scenarioId"]: s["title"] for s in index["scenarios"]}
    if a.list:
        for i, (f_, b) in enumerate(pairs, 1): print(f"{i}. FAULT  {f_}: {titles[f_]}\n   BENIGN {b}: {titles[b]}")
        return
    fid, bid = (a.fault, a.benign) if a.fault and a.benign else pairs[a.pair - 1]
    left, right = load(fid), load(bid)

    def col(text): return textwrap.shorten(text, W, placeholder="...").ljust(W)
    def wrap2(l, r):
        ll, rr = textwrap.wrap(l, W) or [""], textwrap.wrap(r, W) or [""]
        for i in range(max(len(ll), len(rr))):
            print(f"       {(ll[i] if i < len(ll) else '').ljust(W)} | {rr[i] if i < len(rr) else ''}")

    print("=" * (2 * W + 10))
    print(f"       {col('FAULT:  ' + fid)} | {col('BENIGN: ' + bid)}")
    wrap2(titles[fid], titles[bid])
    print("=" * (2 * W + 10))
    starts = [ts(s["window"]["start"]) for s in (left, right)]
    length = max(ts(s["window"]["end"]) - st for s, st in zip((left, right), starts))
    final, first_flag, m = [None, None], [None, None], 0.0
    while m * 60 <= length:
        cells = []
        for i, (sc, st) in enumerate(zip((left, right), starts)):
            if st + m * 60 > ts(sc["window"]["end"]) + a.tick * 60:
                cells.append("(replay finished)"); continue
            kind, text, inc = status(topo, sc, st + m * 60)
            final[i] = inc
            if kind == "FLAG" and first_flag[i] is None: first_flag[i] = m
            cells.append({"FLAG": ">> FLAG  ", "gate": "ok (gate) ", "ok": "ok        "}[kind] + text)
        print(f"T+{m:4.0f}m {col(cells[0])} | {col(cells[1])}")
        if a.delay: time.sleep(a.delay)
        m += a.tick

    for i, (label, sc) in enumerate((("FAULT", left), ("BENIGN", right))):
        print("\n" + "-" * (2 * W + 10))
        inc = final[i] or []
        when = f", first raised at T+{first_flag[i]:.0f} min" if first_flag[i] is not None else ""
        print(f"{label} {sc['scenarioId']}: {len(inc)} flag(s) sent to the low-level agent{when}")
        for x in inc:
            print()
            print(x.message)
        if not inc:
            _, suppressed = flag_window(merge_context(topo, sc), sc["telemetry"])
            for w_ in sorted({w for _, w in suppressed}): print(f"  not flagged because: {w_}")
            if not suppressed: print("  not flagged because: no detector fired (the statistics alone show normal behaviour)")


if __name__ == "__main__":
    main()
