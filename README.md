# ImperialAIHackathon

The Crosslayer console connects Databricks evidence to the team's deterministic detector. Open the **Databricks** tab to select a replay, inspect 24 GPUs across three racks, and review the detector's observations at an explicit UTC replay time.

## Run after cloning

With Node.js 22.18+ and Python 3.9+ on PATH, run from the repository root:

```sh
npm run dev
```

Open http://127.0.0.1:4397/#databricks. No Databricks account, credentials, Python virtual environment or pip install is needed for this default mode. The repository includes four synthetic replays exported from Databricks; the server reads that saved evidence and runs the current `flagging.flagger.flag_window` detector locally. The website clearly labels **Saved Databricks replay**, shows when the evidence was exported, and states that assessments are not written to Databricks.

Choose a **Databricks replay**, set **Replay time · UTC**, then click **Load snapshot**. Click a GPU for its history and inspect **Deterministic detector findings**. If an old terminal has `DATABRICKS_API_URL` set, unset it before running the default saved replay.

This requires the running local Node server. Opening the HTML with `file://`, a static preview, or GitHub Pages does not provide the replay API.

## Run the connected demo

Follow the [connection guide](databricks/CONNECT.md) to import the evidence notebook, configure local credentials and grant access to the shared SQL warehouse and tables. Then use two terminals:

```sh
# Terminal 1, from the repository root; after Python setup and Databricks login
cd databricks
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8011
```

```sh
# Terminal 2, from the repository root; Node.js 22.18+
DATABRICKS_API_URL=http://127.0.0.1:8011 npm run dev
```

The website labels this mode **Connected Databricks**. The Node preview proxies same-origin requests to the configured Python backend; credentials stay on the server. The official snapshot is returned only after Databricks confirms its result was saved. An explicitly configured backend never falls back to saved replay when a query, detector or write fails.

```text
Connected: Delta evidence → bounded query → detector → saved Databricks result → website
Default:   exported Delta evidence → local detector → website (no Databricks writes)
```

The existing 32-GPU simulated twin and its scenario controls remain separate. Both Databricks modes preserve the 24-GPU source inventory, replay UUID and timestamp without mapping them onto the twin's slots. All demo data is synthetic. Each assessment is requested explicitly; this is not a continuous monitor, diagnostic LLM integration or hardware-control system.

[`flagging/flagger.py`](flagging/flagger.py) is the sole official detector. Notebook 02's six `baseline-v1` rules and `health_alerts` table remain comparison fixtures only. See the [data contract](databricks/contracts/data-contract.md) for units, unknown values, analysis-window semantics and persisted provenance, and the [flagging guide](flagging/README.md) for both detector entry points.
