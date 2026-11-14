# ImperialAIHackathon

## Databricks data pipeline

The [`databricks/`](databricks/) module contains synthetic GPU evidence notebooks, Spark SQL health features, persisted rule assessments, a read-only local API, model/frontend examples and tests. Start with its [connection guide](databricks/CONNECT.md).

This module was validated against Databricks with a 24-GPU replay. It is not yet connected to the existing website/digital twin or the detector in [`flagging/`](flagging/); those use a different entity and replay contract. The [integration notes](databricks/CONNECT.md#integrating-with-this-repository) describe the boundary to reconcile.
