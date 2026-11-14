# Crosslayer

Crosslayer is the operator UI prototype for the Imperial AI Hackathon datacenter platform. The console runs locally against a reproducible digital twin, and read-only MCP tools expose the same simulated data.

## Repository layout

| Section | Location | Responsibility |
| --- | --- | --- |
| **Operator UI** | [`public/`](public/) | Overview and Experiments screens, topology interactions, inspector, charts and styling |
| **Simulation and scheduling** | [`src/twin.js`](src/twin.js) | Four-rack digital twin, incident scenarios, telemetry, policy comparison and constraint checks |
| **MCP** | [`src/tool-catalog.js`](src/tool-catalog.js), [`src/mcp.js`](src/mcp.js), [`src/mcp-stdio.js`](src/mcp-stdio.js) | Shared read-only tool definitions, input validation and the local MCP server |
| **Local build and preview** | [`scripts/`](scripts/) | Browser build, standalone HTML and local preview server |
| **Tests** | [`test/`](test/) | Twin integrity, replay constraints, policy trade-offs and MCP verification |

## Run

Requires Node.js 22.18 or newer. From this directory run:

```sh
npm run dev
```

Open [http://127.0.0.1:4397/](http://127.0.0.1:4397/). The server binds to the loopback address. If 4397 is in use, set `CROSSLAYER_PORT` to a free port when starting it. To open a copy directly from disk, run `npm run build` and open `dist/Crosslayer.html`.

## Demo

1. Open **Overview** and choose an incident. Switch between fabric, power and cooling. Inspect a GPU, rack or connection. Drag rack headers to rearrange the map.
2. Scrub or play the 24-minute telemetry trace. For the thermal incident, inspect the stale A02 cooling evidence.
3. Choose **Compare policies**. The experiments view shows the same trace for the fixed-placement baseline and coordinated scheduler, alongside energy, throughput, latency and modeled electricity cost. Switch chart metrics, export the run as JSON, or inspect the coordinated placement on the map.

## Simulation

The four incident inputs are fabric degradation, inference demand, a reduced site power budget and a thermal constraint. The fixed-placement baseline scales the inference pool as load rises. In the thermal incident, an existing serving pool is concentrated in B02. The coordinated policy has access to the full replay and enumerates a bounded set of placements and training power settings. It keeps twelve training ranks and rejects candidates that fail latency, power, temperature or telemetry freshness constraints. Among feasible candidates, it compares training throughput, electricity cost, and relocation effort, in that order. The whole replay is visible to both the evaluator and the coordinated search, so the comparison illustrates the effect of placement under known conditions.

The simulation uses a simplified synchronous training bottleneck, a serving queue proxy and rack power and cooling derates. The latency summary averages the modeled P95 of each minute. Energy includes a fixed facility overhead. The price trace is synthetic and expressed in GBP. Layout changes and power settings are simulation inputs. They have no connection to a physical facility, hardware control or a trained decision policy.

## MCP

The current browser page exposes read-only WebMCP tools when its browser supports them. A local MCP client can use the same four tools over stdio by launching `node src/mcp-stdio.js` from this directory. For clients configured using a command and arguments, use `node` and the absolute path to `src/mcp-stdio.js`.

| Tool | What it returns |
| --- | --- |
| `get_snapshot` | Versioned inventory, topology, assignments and readings for one simulated minute |
| `inspect_entity` | Telemetry and relationships for a rack, GPU, switch, workload or connection |
| `trace_incident` | Deterministic signals, evidence and a proposed response |
| `compare_policies` | Replay timelines, aggregate outcomes and candidate constraints |

The default seed is 73. Tools accept bounded parameters and share the same simulation as the UI. Sensor values are null when stale or missing, with quality and sample timestamps carried separately.

## Verify

Run `npm test` for graph integrity, elapsed replay coverage, constraint and stale-data checks, scenario trade-offs and a full MCP stdio request round trip. Run `npm run build` to produce the local site.
