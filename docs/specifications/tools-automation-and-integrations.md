# Tools, automation, and integrations

> **Status:** Architecture baseline with implemented governed tools (`memory.search@1` and approval-backed `reminder.create@1`), plus envisioned capabilities. Sources: [architecture handover §§9 and 13](../architecture/aura-ai-architecture-handover.md#9-tool-and-integration-architecture), [ADR-0014](../adr/0014-aura-owned-execution-langgraph-backend.md), and catalogue groups [`TOL`](./aura-ai-envisioned-feature-catalogue.md#11-tool-runtime-and-integrations), [`HOM`](./aura-ai-envisioned-feature-catalogue.md#12-home-assistant-and-household-control), [`AUT`](./aura-ai-envisioned-feature-catalogue.md#13-timers-reminders-schedules-workflows-and-notifications), and [`INT`](./aura-ai-envisioned-feature-catalogue.md#24-envisioned-integration-targets).

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

## Governed memory search v1

The implemented v1 registry exposes exactly one executable product tool, `memory.search@1`. It is classified as read-only, requires `memory:read`, never requires confirmation, has a 30-second maximum timeout, allows no retries, is read-only-idempotent, carries private sensitivity, and is observed as `aura.runtime.tool_execution`. Its JSON Schema 2020-12 input is an object containing only a trimmed `query` string of 1–200 characters. Its bounded result contains memory identifier, content, and scope items plus a truncation indicator. The result is private, untrusted evidence supplied only to response synthesis; raw input and result values are not durable tool lifecycle or telemetry fields.

`memory.search@1` invokes the existing authorized memory-recall use case rather than a second retrieval path. Aura fixes the principal, current agent, lifecycle and validity filters, embedding generation, fallback grants, result limit, and context budget from the run's pinned policies. The model cannot request historical states, choose scopes or agents, change fallback grants, or raise result limits. Tool search bypasses only the ambient automatic-recall enablement and confidence gate: setting recall mode to `off` does not revoke an explicit `memory.search@1` tool grant and does not create `on_demand` or `hybrid` memory-policy modes.

The built-in Aura agent has an explicit seeded grant; custom agents are deny-by-default. Owners inspect the safe tool catalog, create immutable exact-version grant policies, and attach a policy through a new immutable agent revision. V1 policies derive a maximum of one call per run when any grant is present, a 30-second timeout, and zero retries. Runs pin these limits and never rely on provider behavior to enforce them.

Aura uses provider-native tool calls through a provider-neutral runtime interface. Unsupported models retain normal text chat and emit a content-free capability outcome. Unknown, ungranted, invalid, multiple, late, over-budget, timed-out, or provider-malformed requests execute nothing. Successful execution produces one tools-disabled synthesis step; unsuccessful attempts may do the same with only a typed content-free failure statement. Tool results never become policy, system instructions, or authority for additional grants.

The authenticated catalog and policy APIs, additive agent/run policy identifiers and counters, and metadata-only `tool.status` event are authoritative in `contracts/openapi/aura-v1.yaml` version 1.6.0 and `contracts/events/runs/v1/run-events.schema.json`. Provider-native call identifiers and payloads, tool arguments, queries, raw results, memory content, prompts, responses, credentials, and hidden reasoning are excluded from these public contracts.

## Approval-backed reminder creation v1

The additive registry entry `reminder.create@1` is the first governed
mutation. It accepts only an object containing trimmed private `content` and an
RFC3339 offset-aware `dueAt`; Core normalizes the instant to UTC, enforces a
strictly future one-shot reminder, and returns only the reminder identifier,
status, and UTC due time. The tool is private, requires `reminders:write`, has
unconditional confirmation, zero retries, a bounded timeout, and idempotent
execution. A model request can never create a reminder before the owner has
approved its durable proposal.

Approval is owner-scoped and expires at the earlier of one hour after creation
or the requested due instant. Pending, approved, rejected, expired, canceled,
executing, completed, and failed state remain inspectable. Approval and
resumption races use PostgreSQL row locks, expected versions, and idempotency;
approval atomically queues the waiting run and the worker performs exactly one
shared reminder-create use case before one tools-disabled synthesis step.

The built-in Aura successor policy grants exactly `memory.search@1` and
`reminder.create@1`; custom agents remain deny-by-default and existing
conversations retain their pinned immutable revisions. An optional browser
IANA timezone is captured at run admission and pinned immutably. Prompt
compilation supplies server-derived local date/time and zone provenance; if no
timezone is pinned, ambiguous natural-language times require clarification and
an explicit offset.

Approval detail responses may include private proposal content only after
owner authorization. Approval SSE, run events, audit, telemetry, NATS
wakeups, and transactional-outbox envelopes are metadata-only: identifiers,
bounded statuses, versions, and lifecycle timestamps only. Persisted
PostgreSQL replay is authoritative, so reconnects, duplicate wakeups, and
offline inbox visits cannot lose a decision or disclose reminder content.

This slice does not authorize shell or filesystem access, network fetching,
Home Assistant, calendars, documents, recurring or edited reminders, external
providers, or Observatory product behavior.

## Deterministic automation

Timers, reminders, schedules, workflow state, approvals, notification delivery, and calendar mutations are deterministic domain functions. Models may interpret intent or assist with ambiguous steps, but they do not simulate durable scheduling, permissions, state transitions, or delivery state.

The catalogue envisions durable timers and reminders, explicit recurrence, reusable schedules, multi-step workflows, approved event triggers, persisted progress, cancellation, approvals, multi-channel notification routing, delivery status, calendar read/write operations, conflict awareness, time-zone handling, ownership, history, and evaluation.

The first approved reminder slice is intentionally narrower: Aura Core owns
owner-scoped, one-shot reminders with trimmed private content, an explicit
offset-aware UTC due instant, deterministic scheduled/fired/completed/canceled
lifecycle transitions, optimistic versions, and UUID idempotency keys.  The
authenticated reminder API and management workspace may expose content only to
the owning principal.  Lifecycle events use the generic transactional outbox
with an `automation.reminder` aggregate and identifier-only metadata; content
does not enter transport envelopes, telemetry, or ordinary audit metadata.
Recurrence, editing, deletion, external delivery, and model-facing mutation
tools remain separate approved slices.  The follow-on delivery slice adds a
database-only one-second scheduler that claims due reminders with PostgreSQL
row locks, atomically creates one durable owner inbox notification, persists a
content-free replay event, and stages the identifier-only lifecycle outbox
event.  In-app delivery means durable inbox availability; acknowledgement
never completes the reminder.  Live notification SSE replays owner-authorized
PostgreSQL events before following ephemeral NATS wakeups, so reconnects and
offline visits recover from the inbox without retaining reminder content in
transport, telemetry, or ordinary audit metadata.

## Home Assistant

Home Assistant remains authoritative for devices, entities, areas, scenes, and automations. Aura provides governed queries and typed actions through scoped adapters. Clear approved commands may use a deterministic intent path; ambiguous targets require clarification. High-impact actions require explicit policy evaluation and configured confirmation.

## Envisioned integrations

Potential adapters include Ollama, Home Assistant, SearXNG, local files, SMB, Nextcloud, calendars beyond the initial Google Calendar adapter, speech services, Web Push, object and database stores, telemetry systems, GitHub, Proxmox, TrueNAS, printers, media services, cameras, email, and approved OpenAPI or MCP services. Listing an integration does not authorize access or promote a catalogue item into implementation scope. Credentials remain isolated from prompts, logs, source control, and ordinary tool results; retention, allowlists, and exact discovery policies remain open.

## Related working summaries

[Module boundaries](../architecture/module-boundaries.md) · [Agents and execution](./agents-and-execution.md) · [Clients and voice](./clients-and-voice.md) · [Open decisions](./open-decisions.md)
