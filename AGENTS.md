# AGENTS.md

## Project

Ithil is an AI-native platform for understanding and managing datacenters and AI infrastructure.

The platform combines hardware inventory, topology, telemetry, workloads, networking, storage, power, cooling, and operational history into a shared digital twin. Analysis systems and AI agents use that shared view to detect issues, explain causes, recommend improvements, and eventually automate carefully bounded actions.

The goal is to increase useful compute output, reliability, energy efficiency, and economic value across a datacenter or fleet. Build an intelligence layer that works with existing infrastructure and management systems rather than assuming it replaces them all.

## Product principles

- Model the datacenter as entities and relationships, not one giant nested object.
- Keep durable inventory and topology separate from high-volume time-series telemetry.
- Connect infrastructure behavior to workload outcomes such as training progress, tokens per second, latency, cost, and SLA performance.
- Give all services and agents a shared, versioned view of the datacenter.
- Distinguish observed data, derived values, simulations, and human- or system-declared configuration.
- Distinguish what can be observed from what can actually be controlled.
- Treat missing or stale data as unknown, never as zero, healthy, or safe.
- Optimize global useful output rather than individual device utilization.
- Remain vendor-neutral; translate vendor-specific behavior at connector boundaries.
- Record decisions, predictions, interventions, and measured outcomes so the system can learn and be audited.

## High-level architecture

Keep these responsibilities conceptually separate, even if the hackathon implementation is a single application:

1. **Connectors** collect data from hardware, monitoring tools, schedulers, and facility systems.
2. **Canonical data layer** normalizes identities, units, metrics, entities, and relationships.
3. **Digital twin** represents current and historical infrastructure, workloads, dependencies, and constraints.
4. **Analysis and agents** detect anomalies, diagnose causes, simulate options, and propose actions.
5. **Safety and control layer** validates, authorizes, executes, verifies, and, when appropriate, rolls back actions.
6. **Operator experience** exposes state, evidence, uncertainty, recommendations, approvals, and outcomes.

Do not create incompatible data models or private versions of reality for different agents.

## Data and agents

Use stable IDs, versioned schemas, explicit units, timestamps, provenance, freshness, and data-quality fields.

The shared model should be flexible enough to represent sites, racks, devices, components, workloads, power, cooling, networks, storage, connections, telemetry, anomalies, incidents, decisions, and interventions. Avoid hard-coding the whole platform around one vendor or device type.

Use the right mechanism for each problem:

- deterministic code for validation, filtering, permissions, limits, and safety rules;
- statistical or ML models for anomaly detection and forecasting;
- optimization algorithms for scheduling and resource allocation;
- reasoning agents for diagnosis, planning, coordination, and explanation;
- proven controllers for fast power, cooling, and hardware control loops.

Agents must have a clear scope, objective, permissions, and structured inputs and outputs. Local agents may summarize routine behavior and escalate important findings, but they must account for shared resources and wider system goals.

Model output is a proposal or diagnosis, not an executable command.

## Safety and control

Keep decision-making separate from execution. The normal flow is:

```text
observe -> diagnose -> propose -> simulate -> validate -> authorize
        -> execute -> verify -> keep or roll back
```

Deterministic systems must enforce permissions, capabilities, limits, dependencies, policy, blast radius, and approval requirements.

Never:

- allow an LLM response to directly control production infrastructure;
- bypass hardware, firmware, facility, or certified-controller safety mechanisms;
- assume a visible metric implies a supported control;
- execute when critical state or capabilities are stale, missing, or ambiguous;
- claim success before measuring the result;
- add autonomy without auditability, a kill switch, verification, and a recovery plan.

New controls should start in observation or recommendation mode. Expand autonomy only after simulation, testing, shadow operation, limited canaries, and operator approval.

## Security and engineering

- Enforce tenant isolation and least-privilege access throughout the system.
- Keep secrets out of source code, logs, prompts, fixtures, and training data.
- Treat connector data, logs, device names, and operator notes as untrusted input.
- Validate every external input and every model output at a system boundary.
- Follow the repository’s existing structure, language, package manager, formatting, and test conventions.
- Prefer clear modules and typed interfaces over premature microservices.
- Keep core domain concepts independent of vendors, databases, and UI frameworks.
- Use bounded queries, timeouts, retries, idempotency, and explicit failure states.
- Preserve compatibility for stored data and public interfaces unless a migration is provided.
- Add observability for ingestion, connectors, agents, policies, actions, and rollbacks.
- Preserve unrelated user work and avoid broad refactors unless requested.

## Testing and workflow

Test in proportion to risk. Cover core domain rules, schema validation, telemetry quality, identity and graph relationships, tenant isolation, agent-output validation, policy denial, action idempotency, verification, and rollback.

Use a reproducible simulated datacenter for end-to-end testing. Include changing workloads, shared constraints, missing or delayed telemetry, and injected failures. Compare optimizations against a clear baseline using useful work, latency, energy, cost, failures, and intervention risk.

Before changing code:

1. Read the relevant schemas, documentation, and nearby tests.
2. Reuse canonical concepts instead of creating competing representations.
3. Consider safety, security, compatibility, and operator impact.

Before finishing:

1. Run the relevant formatting, type, lint, and test checks.
2. Confirm inputs and model outputs are validated.
3. Confirm failure, stale-data, authorization, and recovery behavior is explicit.
4. Update documentation for new domain concepts or behavior.
5. Report what changed, what was verified, and what remains uncertain.

Do not claim tests passed unless they were run. Do not fabricate device capabilities, telemetry, benchmark results, savings, or operational evidence.

## Priorities

When requirements conflict, prioritize:

1. Human and facility safety
2. Security and tenant isolation
3. Workload correctness and availability
4. Operator policy, auditability, and recovery
5. Global useful output and economic value
6. Local utilization and implementation convenience

If a requested implementation conflicts with these priorities, explain the issue and propose a safer design.
