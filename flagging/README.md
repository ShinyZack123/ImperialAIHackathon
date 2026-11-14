# Deterministic telemetry flagging

Decides which datacentre telemetry is worth escalating to a low-level agent, and writes each flag as a plain-English string. No ML in the flagging path: same input, same output.

Run from this folder (Python 3, standard library only):

    python3 build_context.py      # builds topology.enriched.json (limits, measured baselines, interventions)
    python3 test_scenarios.py     # one test per labelled scenario (23/23)
    python3 generalise.py         # fresh generated scenarios
    python3 demo.py               # stuck checkpoint vs normal checkpoint, side by side
    python3 generate.py --list    # seeded telemetry generator

See `FLAGGER_METHODS.md` for how the detectors and gates work, and `WORKLOG.md` for the build log.
