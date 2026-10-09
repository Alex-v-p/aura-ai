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

## Authoritative provider and platform direction

Tool descriptors, events, and structured payloads use JSON Schema 2020-12. REST integrations use OpenAPI 3.1 and generated transport-only clients; approved third-party extensions run in isolated processes or containers behind versioned MCP, OpenAPI, or gRPC boundaries. Untrusted extension packages are never imported into Core, and extension-provided content is untrusted evidence rather than policy or instruction authority.

PostgreSQL owns deterministic automation, workflow and scheduler state, and service migrations. A transactional outbox publishes durable reactions to NATS JetStream with at-least-once delivery and idempotent consumers. Valkey is limited to disposable cache, rate limits, leases, and short-lived session state. Home Assistant remains authoritative for devices, entities, areas, scenes, and automations. Google Calendar is the first selected calendar adapter using per-user OAuth; household sharing is a distinct Aura permission. SearXNG, HTTPX, Trafilatura, and isolated Playwright are the governed web-research adapters. Fetchers have no ambient credentials, enforce allowed schemes and destination/redirect revalidation, block private/link-local/loopback destinations, and bound size, type, time, and resources. Parsed content is untrusted evidence. See [technology stack](../architecture/technology-stack.md), [ADR-0013](../adr/0013-postgresql-nats-valkey-garage-consistency.md), and [ADR-0015](../adr/0015-authentik-bff-secrets-extensions.md).

Every tool has a stable identifier and version, typed input and result schemas, read/mutation classification, permission and confirmation policy, timeout and bounded retry rules, idempotency expectations, audit fields, sensitivity classification, validation requirements, and an observable component identifier.

## Future governed memory search

A future read-only `memory.search` model-facing tool may let an agent decide when deeper recall is useful. It must enter through the general tool runtime and reuse the existing authorized memory-recall use case rather than create a parallel retrieval path. Aura policy remains authoritative for owner and agent scope, lifecycle and validity filters, fallback grants, result and context limits, and metadata-only audit. Returned memory remains untrusted evidence and never policy or instruction authority.

This specification reserves the capability only. It does not define a tool schema, grant, executor, or `on_demand` or `hybrid` recall mode, and it does not authorize executable behavior before the general tool runtime is implemented.

## Deterministic automation

Timers, reminders, schedules, workflow state, approvals, notification delivery, and calendar mutations are deterministic domain functions. Models may interpret intent or assist with ambiguous steps, but they do not simulate durable scheduling, permissions, state transitions, or delivery state.

The catalogue envisions durable timers and reminders, explicit recurrence, reusable schedules, multi-step workflows, approved event triggers, persisted progress, cancellation, approvals, multi-channel notification routing, delivery status, calendar read/write operations, conflict awareness, time-zone handling, ownership, history, and evaluation.

## Home Assistant

Home Assistant remains authoritative for devices, entities, areas, scenes, and automations. Aura provides governed queries and typed actions through scoped adapters. Clear approved commands may use a deterministic intent path; ambiguous targets require clarification. High-impact actions require explicit policy evaluation and configured confirmation.

## Envisioned integrations

Potential adapters include Ollama, Home Assistant, SearXNG, local files, SMB, Nextcloud, calendars beyond the initial Google Calendar adapter, speech services, Web Push, object and database stores, telemetry systems, GitHub, Proxmox, TrueNAS, printers, media services, cameras, email, and approved OpenAPI or MCP services. Listing an integration does not authorize access or promote a catalogue item into implementation scope. Credentials remain isolated from prompts, logs, source control, and ordinary tool results; retention, allowlists, and exact discovery policies remain open.

## Related working summaries

[Module boundaries](../architecture/module-boundaries.md) · [Agents and execution](./agents-and-execution.md) · [Clients and voice](./clients-and-voice.md) · [Open decisions](./open-decisions.md)
