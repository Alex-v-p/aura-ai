# Architecture overview

> **Status:** Architecture baseline summary. See the canonical [architecture handover](./aura-ai-architecture-handover.md) and the [envisioned feature catalogue](../specifications/aura-ai-envisioned-feature-catalogue.md).

## Product boundary

**Aura AI** is a local-first, extensible, house-wide personal AI platform. It provides shared intelligence for conversations, independently configured agents, memory, local knowledge, research, home control, automation, integrations, artifacts, and clients.

**Aura Observatory** is a separate product and service for operational monitoring, trace analysis, component evaluation, experiments, regression detection, and performance attribution. It is not an administration page inside Aura Core and does not read the Core database directly.

## Baseline principles

- Run private conversation, memory, document, voice, home-control, and core inference workloads locally whenever practical. Cloud access is explicit, optional, permissioned, auditable, and replaceable.
- Implement Aura Core as a domain-oriented modular monolith with multiple thin deployable entrypoints over shared application and domain logic.
- Treat agents as persistent identities composed from versioned data and policies over shared runtime engines, not as separate applications.
- Route HTTP, tools, voice, schedules, workers, CLI commands, and workflows through the same application use cases.
- Keep deterministic responsibilities such as permissions, schedules, persistence, and state transitions outside language models.
- Depend on narrow internal ports; concrete providers are selected only at composition boundaries.
- Give every domain ownership of its rules, writes, persistence mappings, and migrations.
- Make important state inspectable and correctable, and bound every run, retry, tool call, and delegated task.

## Logical shape

Aura Core contains shared runtime engines, cohesive domain modules, generic platform services, and provider adapters. Its API, worker, scheduler, evaluation runner, and CLI are separate processes for deployment and resource control, not separate implementations.

Observatory owns evaluation definitions, datasets, results, baselines, analytics, dashboards, alerts, and incidents. Core owns production execution and the controlled evaluation runner. They communicate through versioned contracts, generated clients, events, telemetry, and controlled evaluation jobs.

## Current technology baseline

The authoritative baseline is Python 3.14 with FastAPI and `uv`; PostgreSQL with Alembic and replaceable `pgvector` indexing; Node 24 LTS with strict TypeScript, Angular 22, Nx 23, pnpm 12, and Tailwind 4; Ollama and Home Assistant behind adapters; OpenTelemetry-compatible OTLP; ClickHouse, Prometheus, Loki, and Grafana; Garage-backed production object storage; and Docker Compose with Ansible and Proxmox support. See the [technology matrix](./technology-stack.md) and [accepted ADRs](../adr/README.md) for the full boundary and version policy.

Provider examples and envisioned capabilities do not settle the choices listed in [open decisions](../specifications/open-decisions.md).

## Related working summaries

[System context](./system-context.md) · [Module boundaries](./module-boundaries.md) · [Repository structure](./repository-structure.md) · [Open decisions](../specifications/open-decisions.md)
