// Regenerates databricks/demo/twin_policy_experiments.json from the twin.
// Load it into Databricks with: cd databricks && python -m backend.lakehouse experiments
import { writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';
import { buildPolicyExperiments, serializePolicyExperiments } from '../src/experiment-export.js';

const target = fileURLToPath(new URL('../databricks/demo/twin_policy_experiments.json', import.meta.url));
const document = buildPolicyExperiments();
await writeFile(target, serializePolicyExperiments(document));
console.log(`Wrote ${document.runs.length} policy runs and ${document.timeline.length} timeline points to ${target}`);
