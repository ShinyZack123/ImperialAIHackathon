# ImperialAIHackathon

The Crosslayer console now connects Databricks evidence to the team's deterministic detector and displays its saved assessments. Open the **Databricks** tab to select a replay, inspect 24 GPUs across three racks, and review the detector's observations at an explicit UTC replay time.

```text
Databricks Delta evidence → bounded 15-minute query → connector adapter
  → flagging.flagger.flag_window → official_detector_results in Databricks
  → website and diagnostic-model evidence tools
```

The existing 32-GPU simulated twin and its scenario controls remain separate. Databricks uses its own inventory IDs, replay UUID and timestamp; it is not mapped onto the twin's slots. All demo data is synthetic. Each assessment is requested explicitly; this is not a continuous monitor, diagnostic LLM integration or hardware-control system.

## Run the connected demo

Follow the [connection guide](databricks/CONNECT.md) to import the evidence notebook, configure local credentials and grant access to the shared SQL warehouse and tables. Then use two terminals:

```sh
# Terminal 1, from the repository root; after Python setup and Databricks login
cd databricks
.venv/bin/python -m uvicorn backend.app:app --host 127.0.0.1 --port 8010
```

```sh
# Terminal 2, from the repository root; Node.js 22.18+
DATABRICKS_API_URL=http://127.0.0.1:8010 npm run dev
```

Open http://127.0.0.1:4397/#databricks. The Node preview proxies same-origin requests to the Python backend; credentials stay on the server. The official snapshot is returned only after Databricks confirms its result was saved. Query, detector or persistence errors are visible and do not substitute demo responses.

[`flagging/flagger.py`](flagging/flagger.py) is the sole official detector. Notebook 02's six `baseline-v1` rules and `health_alerts` table remain comparison fixtures only. See the [data contract](databricks/contracts/data-contract.md) for units, unknown values, analysis-window semantics and persisted provenance, and the [flagging guide](flagging/README.md) for both detector entry points.
