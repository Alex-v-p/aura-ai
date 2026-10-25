# System context

> **Status:** Architecture baseline summary. Sources: [architecture handover](./aura-ai-architecture-handover.md) and [envisioned feature catalogue](../specifications/aura-ai-envisioned-feature-catalogue.md).

## Products and clients

- **Aura Core** exposes the central platform through its API and executes background, scheduled, and controlled evaluation work through thin companion processes.
- **Aura Web** is the primary responsive client for conversation and platform management.
- **Aura Wall** is a restricted, room-aware kiosk client.
- **Voice satellites** are thin room devices for wake word, capture, streaming, playback, identity, and diagnostics.
- **Aura Observatory** has its own API, workers, scheduler, web application, database, authorization boundary, retention policy, migrations, and deployment lifecycle.

## Interaction topology

```text
Aura Web / Wall / voice / external clients
                    │
                    ▼
        Aura Core entrypoints
                    │
                    ▼
     shared runtime and domain modules
       │                         │
       ▼                         ▼
provider adapters          telemetry export
                                  │
                                  ▼
                         telemetry stores
                                  │
                                  ▼
                         Aura Observatory

Observatory ── controlled evaluation request ──► Core evaluation runner
Observatory ◄── structured evaluation result ─── Core evaluation runner
```

Provider adapters cover models, embeddings, reranking, Home Assistant, search and fetch, knowledge sources, calendars, voice, notifications, and object storage. Home Assistant remains authoritative for devices, rooms, scenes, and automations.

## Data and telemetry ownership

Core owns agent, conversation, run, task, memory, knowledge, automation, integration, governance, audit, artifact, and outbox state. PostgreSQL is its primary source of truth; object content may use local or S3-compatible storage.

Observatory owns the component and deployment catalogues, telemetry metadata, datasets, suites, experiments, results, baselines, regressions, dashboards, alerting, incidents, annotations, and retention configuration. High-volume traces and observations use ClickHouse; infrastructure metrics use Prometheus; logs use Loki; large captured artifacts use object storage.

Core applies capture, sampling, and redaction policy before telemetry leaves its boundary. Ordinary OTLP and SSE are metadata-only. When explicitly enabled by owner deployment policy, Core may also export closed, bounded, exact model-visible and executor-visible run evidence through the separate one-way asynchronous `run-evidence-v1` channel. Observatory stores manifests and deletion truth in its PostgreSQL database and bytes in a dedicated Garage bucket; ClickHouse remains content-free. Ordinary Core requests must not depend on Observatory availability or query Observatory storage.

## Communication rules

Use direct application calls for ordinary synchronous work and events for decoupled reactions. Durable publication uses a transactional outbox, at-least-once delivery assumptions, idempotent consumers, stable schema versions, and explicit correlation and causation identifiers. The system is event-enabled, not fully event-sourced.

The production run-evidence exporter is bounded and nonblocking. Queue
pressure, transport loss, Observatory outages, and evidence storage failure
produce content-free completeness signals and never alter normal execution.
Aura Web has no Observatory or run-evidence dependency.

## Related working summaries

[Architecture overview](./overview.md) · [Module boundaries](./module-boundaries.md) · [Observatory and evaluation](../specifications/observatory-and-evaluation.md) · [Open decisions](../specifications/open-decisions.md)
