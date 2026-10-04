# Tools, automation, and integrations

> **Status:** Architecture baseline plus envisioned capabilities. Sources: [architecture handover §§9 and 13](../architecture/aura-ai-architecture-handover.md#9-tool-and-integration-architecture) and catalogue groups [`TOL`](./aura-ai-envisioned-feature-catalogue.md#11-tool-runtime-and-integrations), [`HOM`](./aura-ai-envisioned-feature-catalogue.md#12-home-assistant-and-household-control), [`AUT`](./aura-ai-envisioned-feature-catalogue.md#13-timers-reminders-schedules-workflows-and-notifications), and [`INT`](./aura-ai-envisioned-feature-catalogue.md#24-envisioned-integration-targets).

## Baseline separation

```text
model-facing tool contract
        → application use case
        → inward-facing port
        → provider / adapter
```

A tool states what a model may request. The application use case owns validation, policy, confirmation, audit, idempotency, and business rules. A port states what Core needs from an external capability. An adapter implements that port for a concrete provider.

Every tool has a stable identifier and version, typed input and result schemas, read/mutation classification, permission and confirmation policy, timeout and bounded retry rules, idempotency expectations, audit fields, sensitivity classification, validation requirements, and an observable component identifier.

## Deterministic automation

Timers, reminders, schedules, workflow state, approvals, notification delivery, and calendar mutations are deterministic domain functions. Models may interpret intent or assist with ambiguous steps, but they do not simulate durable scheduling, permissions, state transitions, or delivery state.

The catalogue envisions durable timers and reminders, explicit recurrence, reusable schedules, multi-step workflows, approved event triggers, persisted progress, cancellation, approvals, multi-channel notification routing, delivery status, calendar read/write operations, conflict awareness, time-zone handling, ownership, history, and evaluation.

## Home Assistant

Home Assistant remains authoritative for devices, entities, areas, scenes, and automations. Aura provides governed queries and typed actions through scoped adapters. Clear approved commands may use a deterministic intent path; ambiguous targets require clarification. High-impact actions require explicit policy evaluation and configured confirmation.

## Envisioned integrations

Potential adapters include Ollama, Home Assistant, SearXNG, local files, SMB, Nextcloud, calendars, speech services, Web Push, object and database stores, telemetry systems, GitHub, Proxmox, TrueNAS, printers, media services, cameras, email, and approved OpenAPI or MCP services. Listing an integration does not select its provider, authorize access, or promote it into the architecture baseline. Credentials remain isolated from prompts, logs, source control, and ordinary tool results.

## Related working summaries

[Module boundaries](../architecture/module-boundaries.md) · [Agents and execution](./agents-and-execution.md) · [Clients and voice](./clients-and-voice.md) · [Open decisions](./open-decisions.md)
