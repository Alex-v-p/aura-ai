# Agents and execution

> **Status:** Architecture baseline plus envisioned capabilities. Sources: [architecture handover §§4 and 7](../architecture/aura-ai-architecture-handover.md#4-core-terminology) and catalogue groups [`AGT`](./aura-ai-envisioned-feature-catalogue.md#3-agents-and-personas), [`RUN`](./aura-ai-envisioned-feature-catalogue.md#4-conversations-runs-and-tasks), [`DLG`](./aura-ai-envisioned-feature-catalogue.md#5-delegation-and-multi-agent-collaboration), [`MOD`](./aura-ai-envisioned-feature-catalogue.md#6-model-runtime-and-ai-workloads), and [`CTX`](./aura-ai-envisioned-feature-catalogue.md#7-prompting-and-context-assembly).

## Baseline model

An agent is a persistent profile with immutable revisions, not a separate implementation. A revision references its persona, prompt bundle, model policy, tool policy, memory policy, execution policy, optional workspace, and configuration metadata. Every run records the exact revision and policy versions it used.

Personas control expression and interaction style. They do not change factual truth, override platform governance, or grant access. Conversations are user-facing message threads; runs are individual executions; durable tasks may outlive a request.

Every run receives an explicit, lifetime-immutable context containing the principal, household, workspace, agent revision, conversation, run lineage, task, channel, device, room, memory scopes, tool grants, model policy, execution budget, deadline, and trace identity. New state is recorded explicitly rather than through mutable global context.

## Authoritative implementation direction

Aura owns the run lifecycle, budgets, permissions, jobs, events, model policy, persistence, cancellation, approvals, and audit boundary. A lightweight Aura executor handles simple runs. An optional LangGraph executor may implement branching, loops, pauses, and resumable graphs behind an Aura-owned interface; LangGraph checkpoints are executor-internal and reference Aura run IDs. LangGraph state and node concepts must not become Aura domain contracts or client API concepts. PostgreSQL remains authoritative for run and workflow state, with transactional outbox publication to NATS JetStream and idempotent consumers.

The Python runtime uses FastAPI/Pydantic at transport and validation edges and shared application use cases below thin API, worker, scheduler, CLI, and evaluation entrypoints. Ollama is the local model default. Cloud providers and judges are explicit opt-in adapters. Exact model inventory, routing policy, graph persistence details, and evaluation calibration remain [open decisions](./open-decisions.md). See [technology stack](../architecture/technology-stack.md) and [ADR-0014](../adr/0014-aura-owned-execution-langgraph-backend.md).

### Governed tool execution v1

Aura implements one bounded native-tool cycle behind provider-neutral runtime contracts. An immutable agent revision pins an immutable tool-policy revision, and run admission snapshots that policy identifier, the Aura-enforced call limit, and usage. A policy grants an exact stable tool identifier and integer version; absence of that grant denies execution. The built-in Aura profile advances through immutable successor revisions as governed tools are approved; its document-library successor adds exactly `document.search@1` to the previously granted tools, while historical revisions are unchanged and custom profiles remain deny-by-default. Policy creation and attachment are separate owner-scoped, idempotent, optimistic-concurrency commands so historical agent revisions retain their original behavior.

A run with a grant may make at most one tool call. Aura validates the requested identity, version, arguments, permission, budget, and deadline before invoking application behavior. A successful call is followed by one tools-disabled synthesis request. Denied, malformed, over-budget, timed-out, provider-protocol, and execution failures invoke no prohibited behavior and may continue with a content-free tools-disabled synthesis. Models without native tool support continue on the existing text-only path with a visible capability outcome. Native Ollama request and response shapes remain adapter-private and never become Aura API, event, domain, or generated-client contracts.

Tool lifecycle is durable and ordered through `tool.status` run events. These events contain only an Aura-generated call identifier, stable tool identity and version, lifecycle status, bounded error classification/code, and optional result count. They never contain arguments, queries, results, memory text, prompts, responses, credentials, or hidden reasoning.

### Governed document retrieval and citations

`document.search@1` is a read-only, owner-bound search over the admitted
principal's entire ready document library. Its closed input contains only a
bounded query; Core fixes hybrid retrieval, result, excerpt, and context limits
and excludes failed, deleting, deleted, and other-owner records. The tool cannot
select a document or owner, fetch a raw object, or alter lifecycle and policy.
Document text is explicitly delimited untrusted evidence: embedded instructions
cannot grant tools, change permissions or system behavior, trigger a second
tool call, or create long-term memory.

The model may cite only opaque evidence identifiers returned by that tool during
the same run. Core discards malformed, invented, stale, and duplicate markers
before persistence. Valid assistant-message and run responses may include an
optional bounded citation list containing only document identity, an
owner-authorized display label, and a page-or-section locator. Evidence
identifiers, excerpts, queries, filenames as independent persisted fields,
object keys, and model-provided URLs are excluded. Clients construct accessible
internal links to the authorized document detail route from the document
identifier; authorization is re-evaluated when the target is opened.

### Approval-backed reminder mutation

`reminder.create@1` is a typed, idempotent mutation requiring the exact pinned
`reminders:write` grant and unconditional owner confirmation. Aura validates and
normalizes its content and offset-aware due instant before creating an
automation-owned proposal. The proposal keeps private payload in Core's
PostgreSQL domain; the governance approval references proposal, run,
conversation, tool, and owner identifiers without duplicating content.

When confirmation is required, the proposal and approval commit before the
worker lease is released. The run enters `waiting_for_confirmation` and its
tool lifecycle enters `awaiting_confirmation`; no reminder exists. PostgreSQL
is authoritative for pending decisions and replay cursors. Approve, reject,
expiry, and cancellation each lock the approval and use expected versions and
idempotency so exactly one decision wins. Approval queues a durable resume
signal; the resumed worker re-reads the decision, invokes the shared reminder
use case at most once with approval-derived idempotency, and performs one
tools-disabled synthesis step. Denial, expiry, or cancellation resumes with a
typed denied result and creates no reminder.

Approval SSE and transactional-outbox/NATS wakeups carry only bounded
identifiers, statuses, versions, and timestamps. Clients fetch authorized
details separately, so reconnect, duplicate wakeups, and offline catch-up
cannot disclose proposal content or cause a second side effect. The optional
browser IANA timezone is pinned to a new run; prompt compilation derives local
date/time from the server clock. Legacy runs remain valid and ambiguous times
must be clarified with an explicit offset rather than guessed.

## Execution rules

- Independent conversations and agents may run concurrently within resource limits.
- Mutating work within one conversation is ordered unless an explicit branch exists.
- Independent read-only tools may run concurrently; conflicting mutations require serialization, locks, version checks, or idempotency.
- Runs have explicit time, step, token, tool, retry, cost, and resource limits with cancellation and structured failure records.
- Cancellation propagates through tool selection, execution, and response synthesis; a cancelled attempt settles with metadata-only lifecycle state before the run settles.
- Prompts are compiled from versioned platform, governance, agent, persona, context, memory, document, tool, task, and output components. Selected context and compiled provenance remain traceable.
- Agents select capability and workload policies, not concrete GPUs. Scheduling accounts for provider availability and asymmetric resources.

## Envisioned capabilities

The catalogue envisions user-defined agents and personas, revision comparison, ownership and visibility controls, conversation branching and search, resumable tasks, scheduled and event-driven runs, structured outputs, capability-aware model routing, capacity isolation, context ranking and budgeting, and detailed run inspection.

Delegation creates bounded child runs with a precise objective, explicitly transferred context, narrowed permissions, independent budgets, lineage, and a typed result or artifact. Delegation must never silently widen the parent's access.

## Related working summaries

[Terminology](../architecture/terminology.md) · [Module boundaries](../architecture/module-boundaries.md) · [Memory and knowledge](./memory-and-knowledge.md) · [Tools, automation, and integrations](./tools-automation-and-integrations.md) · [Open decisions](./open-decisions.md)
