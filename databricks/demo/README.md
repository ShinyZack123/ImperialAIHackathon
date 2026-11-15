# Saved Databricks evidence

`twin_policy_experiments.json` holds the digital twin's baseline and coordinated results for four scenarios and 11 seeds; `npm run export:experiments` regenerates it and `python -m backend.lakehouse experiments` loads it into Databricks.

`evidence.json.gz` contains the four synthetic replays read from the team's Databricks tables on the UTC date in `manifest.json`. It contains 24 inventory rows, 17,280 GPU readings, 17,280 workload readings and 2,160 cooling readings. The manifest records replay UUIDs, time bounds, counts, and SHA-256 hashes of both the compressed archive and its uncompressed JSON.

There are no workspace URLs, credentials, scenario answer keys or cached detector assessments in this bundle. The local replay bridge runs the repository's current `flagging.flagger.flag_window` for each requested cursor. Readings retain native IDs, timestamps, units and nulls. The website labels this as a saved export and never claims that local assessments were saved back to Databricks.

From the repository root, `npm run dev` serves this mode when `DATABRICKS_API_URL` is unset. It needs Node.js 22.18+ and Python 3.9+; no Python package installation or Databricks sign-in is needed. See [the connection guide](../CONNECT.md) for the optional connected mode.

To refresh the export, an authenticated backend operator can run `python -m backend.export_demo --output demo-review` from `databricks/`. Exporting reads only the known synthetic fixture and refuses an existing destination. Review the new bundle before replacing these files and update the associated tests if intentionally changing its contract. The exporter is separate from the local replay runtime and requires the connected backend dependencies.
