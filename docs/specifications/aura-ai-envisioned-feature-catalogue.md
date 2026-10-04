---
title: Aura AI Envisioned Feature Catalogue
status: Current product vision
version: 0.1
last_updated: 2026-10-04
audience:
  - Codex
  - future contributors
  - product and specification work
---

# Aura AI — Envisioned Feature Catalogue

## 1. Purpose

This document lists the capabilities currently envisioned for **Aura AI** and **Aura Observatory**.

It is a product feature catalogue, not a roadmap. Feature identifiers exist only for stable reference. They do not indicate implementation order, priority, release grouping, dependency order, or commitment date.

The catalogue includes user-facing capabilities, platform behaviour, developer-facing extension points, operational controls, and evaluation functions that have emerged from the project discussions.

## 2. Product-wide characteristics

| ID | Feature | Description |
|---|---|---|
| GEN-001 | Local-first operation | Core conversation, memory, document, voice, home-control, and inference capabilities run locally whenever practical. |
| GEN-002 | Explicit optional cloud access | Cloud models or services may be connected through explicit providers, policies, permissions, and audit records rather than becoming invisible dependencies. |
| GEN-003 | Central platform intelligence | Web clients, wall displays, voice satellites, automations, and integrations use one central Aura platform rather than maintaining separate assistant implementations. |
| GEN-004 | Provider-agnostic design | Models, embeddings, rerankers, search, calendars, voice services, storage, and notification systems are accessed through replaceable provider interfaces. |
| GEN-005 | Inspectable behaviour | Users and developers can inspect agents, memories, tool calls, confirmations, runs, artifacts, and observable component results. |
| GEN-006 | Correctable state | Durable memories, configurations, permissions, and selected records can be corrected, superseded, merged, disabled, or deleted. |
| GEN-007 | Deterministic critical functions | Timers, schedules, permissions, approvals, state changes, persistence, and other critical operations use deterministic logic rather than simulated model behaviour. |
| GEN-008 | Bounded agent execution | Runs have explicit step, time, token, tool, retry, and resource limits with cancellation support. |
| GEN-009 | Shared platform logic | API routes, workers, schedulers, voice intents, CLI commands, and model-facing tools invoke shared application use cases. |
| GEN-010 | Auditable side effects | Mutating actions record the principal, agent, policy, target, confirmation decision, request, result, and outcome. |
| GEN-011 | Multi-user-capable foundation | The identity and permission design does not assume that only one user, device, room, or household role will ever exist. |
| GEN-012 | Configuration-driven deployment | Endpoints, models, credentials, hardware assignments, storage locations, and feature providers are configuration rather than hard-coded constants. |

## 3. Agents and personas

| ID | Feature | Description |
|---|---|---|
| AGT-001 | Persistent agent profiles | Define named agents with stable identity, purpose, ownership, and revision history. |
| AGT-002 | Immutable agent revisions | Record the exact prompt, persona, tool, memory, model, workspace, and execution policies used by a run. |
| AGT-003 | Multiple independent agents | Run general, development, home-management, research, health, or other specialist agents over the same platform engines. |
| AGT-004 | Reusable personas | Apply reusable tone and interaction profiles independently from an agent's functional purpose. |
| AGT-005 | Persona revisions | Version persona tone, verbosity, vocabulary, initiative, speech style, and behavioural instructions. |
| AGT-006 | Custom user-defined agents | Allow users to create and configure additional agents without implementing new orchestration code. |
| AGT-007 | Custom user-defined personas | Allow users to define personalities and voices while preserving platform rules and factual consistency. |
| AGT-008 | Agent-specific memory policy | Give each agent explicit readable scopes, default write scopes, prohibited scopes, and confirmation rules. |
| AGT-009 | Agent-specific tool policy | Give each agent a capability allowlist, action restrictions, confirmation rules, and resource boundaries. |
| AGT-010 | Agent-specific model policy | Select model capability classes, context limits, latency preferences, fallback rules, and workload priority per agent. |
| AGT-011 | Agent-specific execution policy | Configure step budgets, deadlines, delegation limits, retry limits, and background permissions. |
| AGT-012 | Agent workspace binding | Associate an agent or conversation with a project workspace containing dedicated documents, memories, tools, and artifacts. |
| AGT-013 | Multiple conversations per agent | Maintain independent conversation threads under one agent identity. |
| AGT-014 | Agent selection | Select the appropriate agent when starting or continuing a conversation. |
| AGT-015 | Persona selection | Change presentation style without creating a separate factual reality. |
| AGT-016 | Agent templates | Store reusable agent definitions for common purposes while creating distinct profiles and revisions. |
| AGT-017 | Agent enable and disable controls | Temporarily disable agents without deleting their history or configuration. |
| AGT-018 | Agent ownership and visibility | Restrict which users or household roles may see, invoke, edit, or delegate to an agent. |
| AGT-019 | Agent configuration inspection | Display the effective revision, policies, tools, memory scopes, model policy, and prompt component versions. |
| AGT-020 | Agent configuration comparison | Compare two revisions and show material differences affecting behaviour. |

## 4. Conversations, runs, and tasks

| ID | Feature | Description |
|---|---|---|
| RUN-001 | Persistent conversations | Store ordered conversation messages and metadata. |
| RUN-002 | Streaming assistant responses | Stream model output and execution state to clients in realtime. |
| RUN-003 | Conversation branches | Create explicit branches without corrupting the original conversation sequence. |
| RUN-004 | Message attachments | Attach files and artifacts to messages with scoped access. |
| RUN-005 | Message editing and regeneration | Support corrected user input or alternate assistant runs while preserving history. |
| RUN-006 | Conversation search | Search conversations by title, participant, agent, time, content, tool activity, or workspace. |
| RUN-007 | Conversation summaries | Maintain compact summaries for context assembly while preserving original messages. |
| RUN-008 | Run records | Record each execution separately from the conversation message that initiated it. |
| RUN-009 | Run status | Expose queued, running, waiting-for-confirmation, waiting-for-tool, completed, cancelled, and failed states. |
| RUN-010 | Run cancellation | Allow users, policies, deadlines, or parent tasks to cancel work. |
| RUN-011 | Run lineage | Link parent and child runs, retries, branches, evaluation runs, and originating events. |
| RUN-012 | Run budgets | Enforce explicit time, step, token, tool-call, retry, and cost budgets. |
| RUN-013 | Run checkpoints | Persist recoverable checkpoints for durable or long-running work. |
| RUN-014 | Durable tasks | Represent background work that may outlive a request or conversation response. |
| RUN-015 | Task progress | Expose durable task state, progress, current stage, produced artifacts, and failure information. |
| RUN-016 | Task cancellation | Cancel durable tasks with well-defined cleanup and status transitions. |
| RUN-017 | Task ownership | Associate tasks with users, agents, workspaces, conversations, and triggering events. |
| RUN-018 | Scheduled agent runs | Invoke an agent from a schedule using a defined agent revision and bounded context. |
| RUN-019 | Event-driven agent runs | Invoke an agent from approved Home Assistant, file, integration, or platform events. |
| RUN-020 | Parallel conversations | Execute unrelated conversations concurrently within resource limits. |
| RUN-021 | Ordered conversation mutations | Prevent conflicting simultaneous writes within one conversation unless an explicit branch exists. |
| RUN-022 | Parallel read-only tools | Execute independent read-only operations concurrently when permitted. |
| RUN-023 | Mutating action coordination | Use serialization, resource locks, version checks, and idempotency for conflicting actions. |
| RUN-024 | Structured run result | Return typed status, response, artifacts, tool results, warnings, and error information. |
| RUN-025 | Explicit failure records | Preserve structured failure cause, component, retryability, user-facing message, and trace identifier. |

## 5. Delegation and multi-agent collaboration

| ID | Feature | Description |
|---|---|---|
| DLG-001 | Specialist child runs | Allow an agent to delegate a bounded objective to another agent. |
| DLG-002 | Explicit context transfer | Select which instructions, memories, documents, messages, and artifacts a child receives. |
| DLG-003 | Permission narrowing | Ensure delegation can reduce permissions but cannot silently grant more access than the parent has. |
| DLG-004 | Child execution budgets | Apply independent time, token, tool, and step limits to delegated work. |
| DLG-005 | Structured child results | Return typed results or artifacts rather than blindly copying full child transcripts into the parent. |
| DLG-006 | Parallel specialist work | Run independent delegated tasks concurrently when resources permit. |
| DLG-007 | Delegation lineage | Record parent, child, objective, transferred context, policy, result, and cancellation relationships. |
| DLG-008 | Result joining | Combine multiple specialist outputs using a defined merge or synthesis contract. |
| DLG-009 | Delegation policy | Control which agents may delegate, which targets are available, and what data may be transferred. |
| DLG-010 | Delegation evaluation | Measure child-task success, overhead, redundant delegation, and result-merge quality. |

## 6. Model runtime and AI workloads

| ID | Feature | Description |
|---|---|---|
| MOD-001 | Ollama model provider | Use the existing local Ollama environment through an internal provider interface. |
| MOD-002 | Multiple model providers | Support additional local or explicitly authorized remote providers without changing domain logic. |
| MOD-003 | Model capability registry | Describe tool calling, structured output, context size, modalities, throughput, and provider constraints. |
| MOD-004 | Policy-based model routing | Route requests by required capability, workload class, latency, context, priority, and fallback policy. |
| MOD-005 | Capacity-aware scheduling | Queue and place inference requests according to available model workers, hosts, GPUs, and memory. |
| MOD-006 | Asymmetric GPU support | Treat the RTX 3080 12 GB and RTX 2080 8 GB as distinct resources rather than assuming one pooled device. |
| MOD-007 | Interactive workload priority | Give latency-sensitive conversations an explicit scheduling class. |
| MOD-008 | Background workload priority | Separate lower-priority indexing, summarization, extraction, and evaluation work from interactive requests. |
| MOD-009 | Evaluation workload isolation | Prevent large evaluation workloads from consuming all capacity needed by normal interactions. |
| MOD-010 | Model fallback | Select an approved alternate model or return a controlled capacity result when the preferred model is unavailable. |
| MOD-011 | Model health monitoring | Track availability, load status, context failures, time to first token, throughput, errors, and resource usage. |
| MOD-012 | Streaming inference | Stream model output and timing events to the run coordinator and clients. |
| MOD-013 | Structured-output validation | Validate model-generated JSON or typed output and perform bounded repair where allowed. |
| MOD-014 | Intent and extraction models | Support smaller fast models for classification, routing, extraction, and validation. |
| MOD-015 | General reasoning models | Support the primary conversational and planning workload. |
| MOD-016 | Coding models | Route software-development tasks to dedicated coding-capable models. |
| MOD-017 | Embedding models | Provide replaceable local embedding services for memory and document retrieval. |
| MOD-018 | Reranking models | Rerank memory and document candidates independently from embedding retrieval. |
| MOD-019 | Vision models | Inspect selected images or visual artifacts under explicit access and capture policy. |
| MOD-020 | Speech models | Expose speech-to-text and text-to-speech through provider interfaces. |
| MOD-021 | Model and quantization comparison | Compare quality, latency, throughput, memory use, and reliability across model builds and quantizations. |
| MOD-022 | Model policy versioning | Record the exact routing and fallback policy used by each run. |
| MOD-023 | Model request accounting | Record prompt tokens, generated tokens, context size, queue time, inference time, and resource assignment where available. |
| MOD-024 | Model concurrency controls | Limit simultaneous loads and requests per worker, host, GPU, or model. |

## 7. Prompting and context assembly

| ID | Feature | Description |
|---|---|---|
| CTX-001 | Versioned prompt components | Store platform, governance, persona, agent, tool, memory, and output instructions as separate versioned components. |
| CTX-002 | Prompt compilation | Build an effective prompt from selected components and current run context. |
| CTX-003 | Prompt hash and provenance | Record the effective component versions and compiled prompt hash used by a run. |
| CTX-004 | Context source selection | Select messages, summaries, memories, documents, tool descriptions, and task state for a run. |
| CTX-005 | Context token budgeting | Allocate context space across source categories rather than relying on uncontrolled truncation. |
| CTX-006 | Context ranking | Rank selected evidence by relevance, recency, confidence, scope, and policy. |
| CTX-007 | Context deduplication | Avoid repeated memories, documents, or equivalent instructions. |
| CTX-008 | Required-context protection | Reserve space for mandatory policy, task, and recent interaction context. |
| CTX-009 | Context snapshots | Persist a structured record of what was selected for reproducibility and evaluation. |
| CTX-010 | Context privacy filtering | Exclude content not permitted for the current user, agent, device, room, channel, or action. |
| CTX-011 | Conversation summarization | Produce versioned summaries while retaining source message references. |
| CTX-012 | Workspace context | Prefer workspace-specific memories, documents, tools, and terminology when a workspace is active. |
| CTX-013 | Component-level context evaluation | Measure required-context inclusion, irrelevant-context rate, truncation, latency, and token efficiency. |

## 8. Long-term memory and world model

| ID | Feature | Description |
|---|---|---|
| MEM-001 | Working memory | Maintain short-lived state for the current run, task, or conversation. |
| MEM-002 | Episodic memory | Store time-bound events and interactions with provenance. |
| MEM-003 | Semantic memory | Store durable facts and relationships. |
| MEM-004 | Procedural memory | Store instructions, methods, and recurring workflows. |
| MEM-005 | Preference memory | Store user choices, interaction preferences, and response preferences. |
| MEM-006 | System memory | Store facts about rooms, devices, services, infrastructure, and configured integrations. |
| MEM-007 | Scoped memory ownership | Assign memories to platform, household, user, workspace, agent, conversation, task, or run scopes. |
| MEM-008 | Scoped memory visibility | Enforce which identities, agents, devices, and channels may read each memory. |
| MEM-009 | Default write scope | Define where each agent normally writes durable memories. |
| MEM-010 | Proposed broader writes | Require policy or user approval before promoting information into broader scopes. |
| MEM-011 | Memory candidate extraction | Extract possible durable memories from conversations, tool results, events, and documents. |
| MEM-012 | Memory candidate review | Present uncertain or sensitive candidates for user approval. |
| MEM-013 | Memory deduplication | Detect exact, semantic, and entity-level duplicates. |
| MEM-014 | Memory reinforcement | Increase confidence or importance when independent observations support an existing memory. |
| MEM-015 | Memory merging | Combine compatible records while preserving sources and history. |
| MEM-016 | Memory disputes | Mark conflicting information as disputed rather than silently overwriting it. |
| MEM-017 | Memory supersession | Replace current truth while retaining historical validity and lineage. |
| MEM-018 | Temporal validity | Record observed time, valid-from, valid-to, creation time, and supersession state. |
| MEM-019 | Memory provenance | Link memories to messages, documents, tool results, events, imports, and manual edits. |
| MEM-020 | Memory confidence | Store confidence separately from importance and recency. |
| MEM-021 | Memory sensitivity | Classify personal, household, credential-adjacent, health, financial, or other sensitive content. |
| MEM-022 | Memory search | Search memories by text, embedding, entity, scope, time, source, type, and status. |
| MEM-023 | Memory ranking | Rank by relevance, scope, recency, confidence, importance, and current validity. |
| MEM-024 | Memory inspection UI | View stored content, provenance, scope, confidence, relationships, and history. |
| MEM-025 | Memory correction | Edit incorrect content while preserving an audit and supersession trail. |
| MEM-026 | Memory deletion | Delete memories according to authorization, retention, and dependency rules. |
| MEM-027 | Memory export | Export selected memory scopes in a portable format. |
| MEM-028 | Memory import | Import approved structured memory records with provenance and conflict handling. |
| MEM-029 | Entity resolution | Connect references such as names, aliases, devices, projects, and services to canonical entities. |
| MEM-030 | World-model entities | Represent people, projects, devices, rooms, services, places, procedures, decisions, and problems. |
| MEM-031 | World-model relationships | Represent ownership, location, participation, dependency, preference, membership, and historical relationships. |
| MEM-032 | Decision memory | Store decisions together with rationale, date, participants, and superseding decisions. |
| MEM-033 | Problem and fix history | Store problems, attempted solutions, outcomes, and relevant systems. |
| MEM-034 | Memory maintenance | Identify stale, low-confidence, duplicated, orphaned, or contradictory records. |
| MEM-035 | Memory retrieval evaluation | Measure recall, precision, MRR, stale-result rate, irrelevant-context rate, and latency. |

## 9. Local documents and filesystem knowledge

| ID | Feature | Description |
|---|---|---|
| KNO-001 | Knowledge-source connections | Configure approved local filesystem, SMB, Nextcloud, and other document sources. |
| KNO-002 | Source-specific access policy | Define which users, agents, workspaces, devices, and channels may inspect each source or path. |
| KNO-003 | Directory and file catalogue | Index filenames, paths, parent directories, extensions, types, sizes, timestamps, tags, and source identifiers. |
| KNO-004 | Directory hierarchy retrieval | Retrieve surrounding folder structure so an agent can reason about project context before opening content. |
| KNO-005 | Content hashes | Track content identity and avoid repeated processing of unchanged files. |
| KNO-006 | Availability state | Track disconnected shares, missing paths, permission failures, and stale catalogue entries. |
| KNO-007 | Lexical and exact search | Search names, paths, tags, and metadata using exact and full-text retrieval. |
| KNO-008 | Faceted search | Filter by source, path, extension, media type, date, size, tags, project, and access scope. |
| KNO-009 | Lightweight semantic metadata | Store optional summaries, keywords, entities, classifications, and compact metadata embeddings. |
| KNO-010 | Candidate presentation | Show likely files and path context before opening ambiguous or sensitive content. |
| KNO-011 | Confirmation-aware inspection | Ask for approval when content is private, ambiguous, unusually large, expensive, or outside the default policy. |
| KNO-012 | On-demand parsing | Parse file contents only when selected or required. |
| KNO-013 | On-demand chunking | Create retrieval chunks for selected content rather than pre-chunking every file. |
| KNO-014 | On-demand embedding | Embed selected content at query time or through a managed cache. |
| KNO-015 | Hot semantic cache | Reuse processed chunks and embeddings for frequently accessed unchanged content. |
| KNO-016 | Cache invalidation | Invalidate processed content when a content hash or source revision changes. |
| KNO-017 | Document versioning | Preserve source-revision identity and processing generation metadata. |
| KNO-018 | Reranking | Rerank candidate files and chunks before adding evidence to context. |
| KNO-019 | Source citations | Preserve source, path, version, chunk, and retrieval-time references in answers. |
| KNO-020 | Structured document parsing | Extract text and structure from supported document formats through replaceable parsers. |
| KNO-021 | Image candidate discovery | Search image names, folders, tags, and metadata without automatically processing pixels. |
| KNO-022 | Selected image inspection | Use an approved vision provider after the image is selected or authorized. |
| KNO-023 | Media metadata search | Search supported audio, video, and image metadata without treating all media as text. |
| KNO-024 | Knowledge-source health | Report connectivity, indexing lag, errors, excluded paths, and last successful scan. |
| KNO-025 | Indexing task control | Start, inspect, pause, cancel, or retry durable catalogue and processing tasks. |
| KNO-026 | Source exclusions | Exclude backups, secrets, caches, temporary files, model weights, or other configured paths. |
| KNO-027 | Query-time source narrowing | Let an agent or user restrict a search to a project, folder, source, or file type. |
| KNO-028 | Retrieval diagnostics | Display candidates, scores, reranking decisions, processing latency, and chosen evidence. |
| KNO-029 | Retrieval evaluation datasets | Maintain labelled queries, relevant files, relevant chunks, and expected source support. |
| KNO-030 | Document retrieval quality metrics | Measure file recall, chunk recall, citation support, irrelevant evidence, and processing cost. |

## 10. Web research

| ID | Feature | Description |
|---|---|---|
| WEB-001 | Search provider abstraction | Use a controlled search provider such as SearXNG through a replaceable port. |
| WEB-002 | Controlled page retrieval | Fetch approved pages through a bounded retrieval and extraction service. |
| WEB-003 | Source metadata | Preserve title, URL, publisher, retrieval time, content type, and other provenance. |
| WEB-004 | Freshness awareness | Distinguish recently retrieved information from durable local memory. |
| WEB-005 | Multi-source synthesis | Combine evidence from multiple sources while keeping citations attached to claims. |
| WEB-006 | Citation generation | Return source references with research answers and artifacts. |
| WEB-007 | Domain and URL policy | Allow, deny, or require confirmation for configured domains and content classes. |
| WEB-008 | Retrieval limits | Bound the number of searches, pages, bytes, and processing time per run. |
| WEB-009 | Web content capture policy | Apply redaction and retention controls before content enters telemetry or long-term storage. |
| WEB-010 | Research artifacts | Produce structured notes, source lists, comparisons, or reports as artifacts. |
| WEB-011 | Research task durability | Support research work as a durable task with progress and cancellation. |
| WEB-012 | Research evaluation | Measure source relevance, citation support, freshness, coverage, and unsupported claims. |

## 11. Tool runtime and integrations

| ID | Feature | Description |
|---|---|---|
| TOL-001 | Typed tool registry | Register tools with stable identifiers, versions, descriptions, and schemas. |
| TOL-002 | Typed tool inputs | Validate every requested argument before execution. |
| TOL-003 | Typed tool results | Return structured success, warning, partial, and error results. |
| TOL-004 | Read versus mutation classification | Distinguish inspection from state-changing operations. |
| TOL-005 | Tool permission scopes | Require explicit capability grants for users and agents. |
| TOL-006 | Confirmation policy | Require confirmation according to action type, target, reversibility, sensitivity, and context. |
| TOL-007 | Timeout policy | Define execution deadlines per tool and provider. |
| TOL-008 | Retry policy | Define bounded retry behaviour and retryable error classes. |
| TOL-009 | Idempotency | Prevent duplicate side effects across retries and at-least-once delivery. |
| TOL-010 | Result validation | Validate that a tool's reported result matches deterministic expectations where possible. |
| TOL-011 | Tool audit records | Record request, principal, agent, target, policy decision, confirmation, result, and timing. |
| TOL-012 | Tool health | Expose provider availability, authentication state, latency, and recent failures. |
| TOL-013 | Native adapters | Support strongly typed native providers when safety or performance requires them. |
| TOL-014 | OpenAPI tools | Generate or configure selected tools from approved OpenAPI definitions. |
| TOL-015 | MCP integration | Connect selected MCP-compatible capabilities behind Aura governance and validation. |
| TOL-016 | Tool result artifacts | Store large or file-like results as artifacts rather than overloading conversation messages. |
| TOL-017 | Tool-call streaming | Show pending, awaiting approval, executing, completed, and failed states in clients. |
| TOL-018 | Tool-selection evaluation | Measure correct-tool rate, unnecessary-tool rate, missing-tool rate, and planning latency. |
| TOL-019 | Tool-argument evaluation | Measure field accuracy, schema validity, entity resolution, and retry rate. |
| TOL-020 | Tool-execution evaluation | Measure latency, errors, task completion, side-effect correctness, and idempotency. |
| TOL-021 | Integration connection management | Configure endpoints, credentials, capabilities, health, and ownership for external systems. |
| TOL-022 | Integration capability discovery | Discover and expose only the provider capabilities approved for Aura. |
| TOL-023 | Credential isolation | Keep provider credentials outside prompts, logs, source control, and ordinary tool results. |
| TOL-024 | Integration disable controls | Disable a connection or capability without deleting its configuration history. |

## 12. Home Assistant and household control

| ID | Feature | Description |
|---|---|---|
| HOM-001 | Home Assistant connection | Connect to the existing Home Assistant instance through a scoped provider. |
| HOM-002 | Entity state queries | Read approved entity state and attributes. |
| HOM-003 | Room and area context | Use Home Assistant areas and Aura device context to resolve room-specific requests. |
| HOM-004 | Approved service calls | Invoke allowlisted Home Assistant services through typed application commands. |
| HOM-005 | Entity and service allowlists | Restrict which entities, domains, services, and parameters an agent may use. |
| HOM-006 | High-impact action confirmation | Require confirmation for locks, alarms, water shutoff, safety systems, high-power devices, or other configured actions. |
| HOM-007 | Deterministic intent path | Route clear commands directly to validated home-action use cases without requiring a full general agent loop. |
| HOM-008 | Ambiguity resolution | Ask the user to resolve uncertain rooms, devices, entities, or desired states. |
| HOM-009 | Scene activation | Activate approved Home Assistant scenes through the same governance and audit path. |
| HOM-010 | Automation interaction | Trigger, enable, disable, or inspect approved Home Assistant automations according to policy. |
| HOM-011 | Home event triggers | Use approved Home Assistant events as triggers for notifications, workflows, or agent runs. |
| HOM-012 | Household announcements | Send approved announcements through Home Assistant or room output providers. |
| HOM-013 | Home notifications | Deliver system, reminder, printer, camera, or automation notifications through Home Assistant. |
| HOM-014 | Home action audit | Record requested entity, service, parameters, policy decision, confirmation, provider response, and final state where available. |
| HOM-015 | Home integration health | Display connectivity, authentication, event-stream status, latency, and failed actions. |
| HOM-016 | Home-action evaluation | Measure entity resolution, correct action selection, confirmation correctness, latency, and observed side-effect correctness. |

## 13. Timers, reminders, schedules, workflows, and notifications

| ID | Feature | Description |
|---|---|---|
| AUT-001 | Durable timers | Create, list, inspect, cancel, and fire deterministic timers. |
| AUT-002 | Durable reminders | Store reminder content, owner, schedule, status, delivery channels, and completion state. |
| AUT-003 | Recurring reminders | Support explicitly configured recurrence rules. |
| AUT-004 | Schedules | Represent reusable or recurring time-based triggers independently from chat history. |
| AUT-005 | Workflow definitions | Define multi-step deterministic or agent-assisted workflows. |
| AUT-006 | Event triggers | Start workflows, tasks, notifications, or agent runs from approved events. |
| AUT-007 | Trigger filters | Apply conditions based on source, entity, state, user, room, time, or configured predicates. |
| AUT-008 | Workflow state | Persist progress, current step, waiting conditions, errors, and produced artifacts. |
| AUT-009 | Workflow approvals | Pause a workflow until an authorized user approves a consequential step. |
| AUT-010 | Workflow cancellation | Cancel work with deterministic status and cleanup behaviour. |
| AUT-011 | Notification routing | Deliver notifications through web push, Home Assistant, voice, wall display, email, or other providers. |
| AUT-012 | Notification preferences | Select delivery channels, quiet hours, urgency, room, and user scope. |
| AUT-013 | Delivery status | Track pending, delivered, acknowledged, failed, expired, and retried notifications. |
| AUT-014 | Calendar read | Query approved calendars and events through scoped adapters. |
| AUT-015 | Calendar write | Create, update, and cancel calendar events under explicit permission and confirmation policy. |
| AUT-016 | Calendar conflict awareness | Detect overlapping events or constraints when creating or moving events. |
| AUT-017 | Time-zone awareness | Store and interpret schedules with explicit time zones. |
| AUT-018 | Ownership and visibility | Restrict timers, reminders, schedules, and workflows by user, household, workspace, and agent. |
| AUT-019 | Automation history | Show trigger, actions, approvals, results, and failures. |
| AUT-020 | Automation evaluation | Measure trigger correctness, delivery reliability, latency, policy decisions, and side-effect outcomes. |

## 14. Voice and room-aware interaction

| ID | Feature | Description |
|---|---|---|
| VOI-001 | Voice satellite service | Run a thin deployable client on room devices. |
| VOI-002 | Local wake-word detection | Detect an approved wake word locally through a replaceable provider. |
| VOI-003 | Voice activity detection | Identify speech boundaries before and during audio streaming. |
| VOI-004 | Audio streaming | Stream captured speech to Aura using a versioned voice protocol. |
| VOI-005 | Speech-to-text | Transcribe audio through a local or approved provider. |
| VOI-006 | Text-to-speech | Synthesize responses through a local or approved provider. |
| VOI-007 | Persona voice selection | Associate personas or agents with selected voices and speech settings. |
| VOI-008 | Device identity | Authenticate and identify each satellite. |
| VOI-009 | Room identity | Attach room context to voice requests and output routing. |
| VOI-010 | Originating-device playback | Return spoken output to the device that initiated the interaction. |
| VOI-011 | Selected-room playback | Route approved announcements or responses to a chosen room. |
| VOI-012 | Satellite reconnect | Recover from server or network interruption without duplicating completed requests. |
| VOI-013 | Satellite diagnostics | Report microphone, speaker, wake-word, connection, latency, and provider health. |
| VOI-014 | Voice conversation continuity | Continue an existing conversation or start a new one according to device and user context. |
| VOI-015 | Voice confirmation | Ask for and capture confirmation before consequential voice-initiated actions. |
| VOI-016 | Voice privacy controls | Control retention, telemetry capture, and deletion of recordings and transcripts. |
| VOI-017 | Voice latency monitoring | Measure wake-word, upload, transcription, agent, synthesis, and playback latency separately. |
| VOI-018 | Voice quality evaluation | Evaluate transcription accuracy, intent resolution, response suitability, and synthesis success. |

## 15. Aura Web application

| ID | Feature | Description |
|---|---|---|
| AUI-001 | Responsive PWA | Provide the main Aura experience across desktop, tablet, and phone browsers. |
| AUI-002 | Conversation list | Browse, search, filter, rename, archive, and organize conversations. |
| AUI-003 | Conversation view | Display messages, streaming responses, attachments, artifacts, citations, and run states. |
| AUI-004 | Rich message rendering | Render Markdown, code, structured data, citations, tool calls, and artifact cards. |
| AUI-005 | Composer | Support text, attachments, agent selection, workspace selection, and optional voice input. |
| AUI-006 | Tool-call cards | Show requested arguments, approval state, execution progress, result, and errors. |
| AUI-007 | Run details | Show status, component stages, selected model, timing, tools, memories, documents, and trace link. |
| AUI-008 | Agent management | Create, inspect, edit, revise, enable, disable, and compare agents. |
| AUI-009 | Persona management | Create, inspect, edit, revise, preview, and assign personas. |
| AUI-010 | Prompt-component management | Inspect and edit approved prompt components and revision history. |
| AUI-011 | Model-policy management | Configure model capability and fallback policies without binding agents directly to GPUs. |
| AUI-012 | Tool-policy management | Configure capability grants, targets, confirmation rules, and restrictions. |
| AUI-013 | Memory browser | Search, filter, inspect, correct, merge, supersede, and delete authorized memory. |
| AUI-014 | Memory-candidate review | Approve, edit, reject, or rescope proposed memories. |
| AUI-015 | World-model browser | Explore entities, relationships, history, and source-backed facts. |
| AUI-016 | Knowledge-source management | Configure, inspect, enable, disable, and diagnose local, SMB, and Nextcloud sources. |
| AUI-017 | File candidate confirmation | Review file paths and metadata before authorizing content inspection. |
| AUI-018 | Document retrieval inspector | Show candidate files, chunks, scores, citations, processing state, and cache status. |
| AUI-019 | Timer and reminder management | Create, edit, cancel, complete, and inspect deterministic records. |
| AUI-020 | Schedule management | View and edit recurring schedules and triggers. |
| AUI-021 | Workflow management | Inspect definitions, running instances, approvals, state, and history. |
| AUI-022 | Integration management | Configure provider connections, credentials references, capabilities, and health. |
| AUI-023 | Approval inbox | Review pending tool actions, workflow approvals, memory promotions, and sensitive content access. |
| AUI-024 | Artifact library | Browse, preview, download, revise, delete, and attach generated or uploaded artifacts. |
| AUI-025 | Notification centre | View pending, delivered, acknowledged, failed, and historical notifications. |
| AUI-026 | System status | Show Core API, workers, scheduler, providers, queues, database, model, and integration health. |
| AUI-027 | User and household settings | Manage profile, household, devices, rooms, preferences, and authorized identities. |
| AUI-028 | Privacy controls | Configure capture, retention, memory, recording, and data-export settings. |
| AUI-029 | Audit viewer | Search permitted action, approval, memory, configuration, and integration audit records. |
| AUI-030 | Realtime reconnect | Recover the user interface after network interruption and reconcile current run state. |

## 16. Aura Wall application

| ID | Feature | Description |
|---|---|---|
| WAL-001 | Kiosk-oriented interface | Provide a simplified large-screen experience for fixed household displays. |
| WAL-002 | Large voice interaction surface | Show listening, transcribing, thinking, tool, approval, and speaking states. |
| WAL-003 | Current agent display | Show and select from agents permitted for the room or device. |
| WAL-004 | Room-aware context | Include the display's configured room in requests and home-control resolution. |
| WAL-005 | Timers and reminders panel | Show active timers, upcoming reminders, and acknowledgement controls. |
| WAL-006 | Notification panel | Present household and room-specific notifications. |
| WAL-007 | Selected home status | Display approved Home Assistant state without replacing Home Assistant dashboards. |
| WAL-008 | Limited conversation history | Show a restricted local interaction history appropriate to the room. |
| WAL-009 | Restricted settings | Expose only settings permitted for the wall device and current user context. |
| WAL-010 | Shared frontend libraries | Reuse Aura domain and shared UI libraries without copying application code. |
| WAL-011 | Device lock and session controls | Limit access when the display is unattended or used by guests. |
| WAL-012 | Wall client health | Report browser, connection, audio, display, and realtime status. |

## 17. Artifacts and attachments

| ID | Feature | Description |
|---|---|---|
| ART-001 | User uploads | Store approved uploaded files with ownership, scope, provenance, and content metadata. |
| ART-002 | Generated artifacts | Store reports, plans, code patches, exports, images, datasets, and other generated outputs. |
| ART-003 | Artifact revisions | Preserve revisions and lineage instead of overwriting every generated result. |
| ART-004 | Artifact previews | Render supported text, Markdown, image, structured data, and code content. |
| ART-005 | Artifact download | Provide authorized access to the underlying file. |
| ART-006 | Artifact links | Associate artifacts with conversations, runs, tasks, workspaces, memories, and evaluations. |
| ART-007 | Artifact access scopes | Restrict artifacts by user, household, workspace, agent, task, or evaluation context. |
| ART-008 | Object-storage abstraction | Store content in local or S3-compatible storage while retaining metadata in the owning service. |
| ART-009 | Artifact checksums | Track integrity and deduplicate content where appropriate. |
| ART-010 | Artifact retention | Apply retention and deletion policy separately from conversation text. |
| ART-011 | Redacted evaluation artifacts | Store privacy-filtered component inputs and outputs for authorized analysis. |
| ART-012 | Export bundles | Package selected conversations, memories, artifacts, or evaluation results in portable exports. |

## 18. Identity, permissions, approvals, and privacy

| ID | Feature | Description |
|---|---|---|
| GOV-001 | User identities | Represent authenticated people separately from agents and devices. |
| GOV-002 | Household identities | Represent household membership and shared resources. |
| GOV-003 | Device identities | Authenticate web, wall, satellite, and service devices. |
| GOV-004 | Room context | Associate approved devices and requests with rooms. |
| GOV-005 | Service identities | Authenticate Core, Observatory, workers, schedulers, providers, and automation clients. |
| GOV-006 | Role and capability permissions | Grant explicit access to domains, agents, tools, sources, memories, and administrative functions. |
| GOV-007 | Resource-scoped access | Restrict access to specific workspaces, directories, entities, calendars, integrations, or artifacts. |
| GOV-008 | Channel restrictions | Restrict sensitive information or actions on wall displays, voice devices, guest sessions, or external clients. |
| GOV-009 | Policy engine | Evaluate principal, agent, action, resource, sensitivity, context, reversibility, presence, and time restrictions. |
| GOV-010 | Confirmation requests | Create durable confirmation records with expiry, context, requester, and target. |
| GOV-011 | Approval inbox | Allow authorized users to approve, reject, or modify pending actions. |
| GOV-012 | Multi-approver policy | Support actions that require approval from a particular role or more than one party. |
| GOV-013 | Audit log | Record security-relevant reads, writes, tool calls, approvals, configuration changes, and deletions. |
| GOV-014 | Secret references | Store credential references without exposing secret values to prompts or ordinary APIs. |
| GOV-015 | Credential rotation | Update provider credentials without recreating domain configuration. |
| GOV-016 | Data sensitivity classification | Classify memory, documents, tools, artifacts, telemetry, and evaluation data. |
| GOV-017 | Data retention policies | Configure retention by data class, source, service, and environment. |
| GOV-018 | Data deletion | Remove authorized data and derived indexes according to ownership and retention rules. |
| GOV-019 | Data export | Export authorized personal or operational data in documented formats. |
| GOV-020 | Telemetry capture policy | Select none, metadata-only, hashed, redacted, sampled-content, or full-content capture. |
| GOV-021 | Prompt and output redaction | Remove configured secrets and sensitive fields before telemetry export. |
| GOV-022 | Voice recording controls | Configure whether audio is retained, transient, redacted, or disabled. |
| GOV-023 | Least-privilege provider tokens | Use provider credentials limited to approved operations and targets. |
| GOV-024 | Revocation | Revoke sessions, devices, integrations, tool grants, or service credentials. |
| GOV-025 | Security-event alerts | Generate alerts for repeated denial, authentication failure, policy bypass attempts, or unusual sensitive access. |

## 19. Aura Observatory operational monitoring

| ID | Feature | Description |
|---|---|---|
| OBS-001 | Dedicated Observatory service | Run monitoring and evaluation separately from Aura Core. |
| OBS-002 | Dedicated Observatory web app | Provide a focused operational and evaluation interface. |
| OBS-003 | Separate authentication boundary | Control access to operational traces, captured content, datasets, and evaluations independently. |
| OBS-004 | Separate transactional database | Store Observatory metadata without sharing Core tables. |
| OBS-005 | Telemetry ingestion | Consume OTLP traces, metrics, logs, structured run events, deployment events, and evaluation results. |
| OBS-006 | Live run view | Show queued and active runs, current component, elapsed time, agent, model, tools, and resource assignment. |
| OBS-007 | Trace explorer | Display a hierarchical or waterfall view of every instrumented stage in a run. |
| OBS-008 | Run search | Search by run, trace, conversation, agent, component, model, deployment, host, GPU, status, or time. |
| OBS-009 | Run comparison | Compare component timing, outputs, policies, models, prompts, and results between runs. |
| OBS-010 | Component catalogue | List observable components, versions, owners, dependencies, schemas, metrics, and evaluators. |
| OBS-011 | Component health | Show error rate, latency, throughput, quality score, saturation, and dependency state per component. |
| OBS-012 | Dependency map | Visualize component and provider dependencies and their current health. |
| OBS-013 | Module health trends | Track operational and quality metrics across time and deployments. |
| OBS-014 | Metric explorer | Query and graph defined metrics by agent, component, model, host, GPU, provider, version, and environment. |
| OBS-015 | Latency percentiles | Show p50, p90, p95, p99, distribution, and tail-latency changes. |
| OBS-016 | Error analysis | Group failures by component, provider, exception class, retryability, deployment, and input cohort. |
| OBS-017 | Queue monitoring | Show queue depth, age, throughput, retry backlog, dead letters, and worker saturation. |
| OBS-018 | Model performance monitoring | Show queue time, time to first token, tokens per second, total latency, context size, and structured-output failures. |
| OBS-019 | GPU monitoring | Show utilization, VRAM, temperature, power, process assignment, model load, and saturation where exporters permit it. |
| OBS-020 | Retrieval monitoring | Show candidate counts, search latency, rerank latency, cache hits, selected evidence, and quality metrics. |
| OBS-021 | Tool monitoring | Show planning, validation, approval, execution, timeout, retry, and side-effect outcomes. |
| OBS-022 | Memory monitoring | Show candidate extraction, write decisions, retrieval performance, contradictions, and maintenance results. |
| OBS-023 | Automation monitoring | Show trigger latency, due-job lag, delivery success, workflow state, and failed steps. |
| OBS-024 | Voice monitoring | Show wake-word, streaming, transcription, agent, synthesis, playback, and device latency. |
| OBS-025 | Deployment markers | Annotate graphs and regressions with git commit, image version, configuration version, and deployment time. |
| OBS-026 | Prompt and policy markers | Correlate behaviour with prompt, agent, model, memory, and tool policy revisions. |
| OBS-027 | Dashboards | Compose reusable operational and quality dashboards. |
| OBS-028 | Dashboard presets | Provide predefined system, agent, module, model, and regression views. |
| OBS-029 | Saved queries | Save and share reusable trace and metric filters. |
| OBS-030 | Alerts | Evaluate latency, error, saturation, quality, availability, and privacy alert rules. |
| OBS-031 | Alert grouping | Group repeated symptoms into coherent alert instances. |
| OBS-032 | Alert suppression | Silence or suppress known maintenance and duplicate conditions. |
| OBS-033 | Incidents | Track incident status, impact, evidence, annotations, related alerts, and resolution notes. |
| OBS-034 | Annotations | Attach human notes to traces, deployments, regressions, experiments, and incidents. |
| OBS-035 | System health overview | Summarize Core, Observatory, telemetry stores, queues, models, providers, databases, and clients. |
| OBS-036 | Retention status | Show telemetry volume, age, retention policy, deletion progress, and storage pressure. |
| OBS-037 | Captured artifact viewer | Inspect authorized redacted inputs, outputs, context snapshots, and evaluation artifacts. |
| OBS-038 | Privacy-aware access | Apply separate permissions to metadata, redacted content, and full evaluation content. |
| OBS-039 | Observatory self-monitoring | Monitor the ingest pipeline, evaluation workers, analytics queries, storage, and UI itself. |
| OBS-040 | Low-level Grafana access | Retain infrastructure-focused Grafana dashboards as a supplementary diagnostic tool. |

## 20. Evaluation, experiments, and regression analysis

| ID | Feature | Description |
|---|---|---|
| EVA-001 | Evaluation datasets | Store versioned test cases, inputs, expected evidence, labels, rubrics, and provenance. |
| EVA-002 | Dataset scopes | Separate public, synthetic, project, private, sensitive, and environment-specific datasets. |
| EVA-003 | Module datasets | Maintain component-specific cases for retrieval, context, routing, tools, policy, synthesis, and delegation. |
| EVA-004 | End-to-end datasets | Maintain full conversation and task scenarios for complete agent evaluation. |
| EVA-005 | Evaluation suites | Group datasets, evaluators, thresholds, configuration, and execution environment. |
| EVA-006 | Deterministic evaluators | Score exact fields, schemas, expected tools, expected arguments, citations, side effects, and policy outcomes. |
| EVA-007 | Retrieval evaluators | Calculate recall@K, precision@K, MRR, ranking quality, stale-result rate, and irrelevant-context rate. |
| EVA-008 | Context evaluators | Measure required-context inclusion, unsupported context, token efficiency, conflicts, and truncation. |
| EVA-009 | Tool-selection evaluators | Measure correct, missing, redundant, and prohibited tool choices. |
| EVA-010 | Tool-argument evaluators | Measure schema validity, exactness, entity resolution, and required-field accuracy. |
| EVA-011 | Tool-outcome evaluators | Measure task completion, side-effect correctness, idempotency, and error handling. |
| EVA-012 | Policy evaluators | Measure false approval, false denial, confirmation correctness, and sensitive-data handling. |
| EVA-013 | Groundedness evaluators | Measure whether response claims are supported by selected memories, documents, tools, or sources. |
| EVA-014 | Response-quality evaluators | Score correctness, relevance, usefulness, clarity, completeness, and adherence to requested style. |
| EVA-015 | Resource evaluators | Score latency, throughput, token use, memory use, GPU use, queue time, and execution overhead. |
| EVA-016 | LLM-as-judge providers | Use configured judge models for rubric-based scoring where deterministic scoring is insufficient. |
| EVA-017 | Judge calibration | Compare judge outputs with human labels and track drift or disagreement. |
| EVA-018 | Judge ensembles | Combine multiple judges or deterministic and model-based scores. |
| EVA-019 | Human review | Record reviewer scores, labels, comments, and disagreement. |
| EVA-020 | Passive production evaluation | Derive operational and selected quality signals from ordinary runs under capture policy. |
| EVA-021 | Module replay | Replay a captured or synthetic input against one observable component. |
| EVA-022 | Dependency stubbing | Replace external dependencies with versioned fixtures during isolated component evaluation. |
| EVA-023 | End-to-end evaluation | Execute a complete agent scenario through the controlled Core evaluation runner. |
| EVA-024 | Exact deployed-code evaluation | Run evaluations against the actual deployed Core component version rather than copying its logic into Observatory. |
| EVA-025 | Experiment definitions | Compare selected code, model, prompt, policy, retrieval, provider, or configuration variants. |
| EVA-026 | Experiment cohorts | Segment results by agent, task type, model, source, user-approved category, or other defined dimensions. |
| EVA-027 | Baselines | Mark accepted result sets or metric distributions as comparison baselines. |
| EVA-028 | Baseline versioning | Retain baseline history and the configuration that produced it. |
| EVA-029 | Regression thresholds | Define absolute, relative, statistical, and cohort-specific thresholds. |
| EVA-030 | Regression detection | Identify operational or quality degradation against a baseline. |
| EVA-031 | Change-point detection | Detect when a metric distribution materially changed. |
| EVA-032 | Correlation analysis | Correlate changes with deployments, prompts, models, policies, providers, data sources, and hardware. |
| EVA-033 | Component attribution | Rank components likely responsible for an end-to-end degradation using traces and isolated results. |
| EVA-034 | Affected-agent analysis | Show which agents, workspaces, task types, or cohorts are affected. |
| EVA-035 | Unaffected-cohort analysis | Show comparable agents or scenarios that did not regress. |
| EVA-036 | Run-level evidence | Link a regression to representative traces, component outputs, and evaluation cases. |
| EVA-037 | Evaluation result comparison | Compare scores, distributions, failures, artifacts, and timings across experiments or versions. |
| EVA-038 | Evaluation history | Preserve who ran an evaluation, against what version, with which configuration, and what it produced. |
| EVA-039 | Evaluation cancellation | Cancel queued or active evaluation jobs with deterministic result state. |
| EVA-040 | Evaluation resource policy | Control model, GPU, worker, concurrency, and time budgets separately from interactive traffic. |
| EVA-041 | Scheduled evaluations | Execute configured suites from Observatory's scheduler. |
| EVA-042 | CI evaluation reports | Produce machine-readable and human-readable results for repository checks. |
| EVA-043 | Performance regression reports | Detect latency, throughput, memory, GPU, queue, and storage query degradation. |
| EVA-044 | Golden results | Store accepted expected outputs or structured outcomes where exact comparison is meaningful. |
| EVA-045 | Synthetic test generation | Generate controlled candidate cases while preserving clear synthetic provenance. |
| EVA-046 | Evaluation artifact retention | Store large inputs, outputs, traces, and reports according to explicit retention policy. |
| EVA-047 | Evaluation export | Export experiment configuration, cases, results, scores, and comparisons. |
| EVA-048 | Evaluation reproducibility metadata | Record code commit, image, environment, component versions, model digests, prompts, policies, seeds, and fixtures. |

## 21. Component-specific evaluation coverage

| ID | Observable component | Envisioned measurements |
|---|---|---|
| CMP-001 | Agent run coordinator | Run success, failure stage, step count, deadline use, cancellation, retry count, total latency |
| CMP-002 | Context assembly | Selection latency, selected tokens, source mix, truncation, required-context inclusion, irrelevant-context rate |
| CMP-003 | Memory retrieval | Candidate count, search latency, rerank latency, cache hit, recall@K, precision@K, MRR, stale-memory rate |
| CMP-004 | Document discovery | Catalogue latency, candidates, source availability, relevant-file recall, access-policy denials |
| CMP-005 | Document retrieval | Parse time, chunk count, embedding time, rerank time, relevant-chunk recall, citation support |
| CMP-006 | Prompt compilation | Render latency, output size, missing components, conflicts, schema validity |
| CMP-007 | Model routing | Route latency, queue selection, fallback rate, capability match, unnecessary expensive routing |
| CMP-008 | Model inference | Queue time, time to first token, tokens/sec, total latency, context failures, structured-output validity, task score |
| CMP-009 | Tool planning | Planning latency, planned tools, correct-tool rate, redundant-tool rate, prohibited choices |
| CMP-010 | Policy evaluation | Decision latency, confirmation requirement, false approval, false denial, policy source |
| CMP-011 | Tool argument generation | Validation errors, repair count, exactness, entity resolution, missing fields |
| CMP-012 | Tool execution | Provider latency, timeout, retry, error, idempotency, completion, observed side-effect correctness |
| CMP-013 | Response synthesis | Synthesis latency, output length, correctness, usefulness, groundedness, citation support |
| CMP-014 | Memory extraction | Candidate count, precision, sensitive-content classification, duplicate rate |
| CMP-015 | Memory persistence | Decision type, merge/supersession outcome, write latency, conflict rate |
| CMP-016 | Delegation | Child count, transfer size, permission narrowing, overhead, child success, merge quality |
| CMP-017 | Scheduler | Due-job lag, dispatch success, duplicate prevention, missed schedules |
| CMP-018 | Notification delivery | Routing latency, provider success, acknowledgement, retry, expiry |
| CMP-019 | Voice pipeline | Wake-word latency, VAD timing, upload, transcription, agent, synthesis, playback, transcription accuracy |
| CMP-020 | Observatory ingestion | Intake lag, validation errors, duplicates, storage latency, dropped telemetry |
| CMP-021 | Observatory analytics | Query latency, aggregation freshness, attribution confidence, comparison cost |
| CMP-022 | Evaluation runner | Queue time, execution time, fixture load, component result, resource use, reproducibility metadata completeness |

## 22. Developer platform and extensibility

| ID | Feature | Description |
|---|---|---|
| DEV-001 | Monorepo | Keep Core, Observatory, frontends, satellites, contracts, SDKs, evaluations, deployment, and documentation together. |
| DEV-002 | Python workspace | Manage Python services and packages through one locked workspace while retaining package boundaries. |
| DEV-003 | Angular Nx workspace | Manage web applications and libraries with enforceable frontend dependency boundaries. |
| DEV-004 | Public module APIs | Expose domain commands, queries, DTOs, and events through deliberate public interfaces. |
| DEV-005 | Architecture tests | Reject forbidden cross-domain, provider, entrypoint, service, and frontend imports. |
| DEV-006 | Instrumentation coverage tests | Verify stable component identifiers and required operational measurements. |
| DEV-007 | Versioned OpenAPI contracts | Define Core and Observatory APIs independently. |
| DEV-008 | Versioned event schemas | Define run, component, evaluation, deployment, and domain events with compatibility rules. |
| DEV-009 | Generated API clients | Generate Python and TypeScript clients from service contracts. |
| DEV-010 | Observability SDK | Share instrumentation mechanics without sharing private service logic. |
| DEV-011 | Evaluation SDK | Share replay and scoring contracts without duplicating Core components. |
| DEV-012 | Extension SDK | Define supported provider, tool, knowledge-source, and observable-component extension points. |
| DEV-013 | Test kit | Share builders, fakes, fixtures, and assertions with explicit scope. |
| DEV-014 | Provider examples | Include maintained examples for tool, model, knowledge-source, and observable-component extensions. |
| DEV-015 | Command-line tools | Provide Core and Observatory administrative and diagnostic commands. |
| DEV-016 | Local development environment | Supply repeatable configuration, service composition, migrations, and developer tooling. |
| DEV-017 | Contract tests | Test providers, clients, events, and cross-service payload compatibility. |
| DEV-018 | Unit tests | Test domain rules and runtime components without unnecessary external systems. |
| DEV-019 | Integration tests | Test databases, providers, queues, storage, and service boundaries. |
| DEV-020 | Acceptance tests | Test user-visible use cases and policy outcomes. |
| DEV-021 | System tests | Test complete multi-process scenarios across Core, Observatory, clients, and telemetry. |
| DEV-022 | Load tests | Test parallel runs, queues, model routing, ingestion, analytics, and client streaming. |
| DEV-023 | Resilience tests | Test provider outages, worker loss, queue backlog, database interruption, and recovery. |
| DEV-024 | Fault injection | Deliberately inject latency, timeout, malformed output, unavailable source, and partial failure. |
| DEV-025 | Security tests | Test authentication, authorization, confirmation, secret isolation, and policy bypass attempts. |
| DEV-026 | Privacy tests | Test redaction, capture levels, deletion, retention, and sensitive-source exclusion. |
| DEV-027 | Compatibility tests | Verify supported contract and migration compatibility. |
| DEV-028 | Architecture decision records | Record material decisions and their rationale. |
| DEV-029 | Module documentation | Document ownership, public API, dependencies, persistence, events, and observable components per module. |
| DEV-030 | Metric and evaluator catalogues | Document metric meaning, units, dimensions, expected ranges, scorers, and limitations. |

## 23. Deployment, infrastructure, and operations

| ID | Feature | Description |
|---|---|---|
| OPS-001 | Containerized services | Build separate images for Core, Observatory, web apps, and voice satellites. |
| OPS-002 | Docker Compose deployment | Compose Core, Observatory, telemetry, storage, frontend, and provider infrastructure through profiles. |
| OPS-003 | Homelab deployment profile | Configure deployment for the current Proxmox, Ollama, GPU, TrueNAS, Home Assistant, and reverse-proxy environment. |
| OPS-004 | Development deployment profile | Provide isolated local dependencies and developer-friendly configuration. |
| OPS-005 | GPU deployment profile | Configure model workers and GPU exporters separately from ordinary services. |
| OPS-006 | Core-only profile | Run Aura Core without requiring Observatory. |
| OPS-007 | Observatory profile | Run Observatory and telemetry infrastructure independently of Core process code. |
| OPS-008 | Full-stack profile | Run the complete integrated environment. |
| OPS-009 | Ansible automation | Manage repeatable server, AI node, telemetry, database, reverse-proxy, and satellite configuration. |
| OPS-010 | Proxmox support | Store cloud-init, VM-template, and network configuration relevant to the homelab. |
| OPS-011 | Reverse-proxy configuration | Support the existing Nginx Proxy Manager environment and optional documented alternatives. |
| OPS-012 | TLS termination | Expose web services through configured secure reverse-proxy routes. |
| OPS-013 | Service health endpoints | Report liveness, readiness, dependency health, and degraded state. |
| OPS-014 | Graceful shutdown | Stop accepting work, checkpoint or requeue supported tasks, flush telemetry, and close resources. |
| OPS-015 | Configuration validation | Reject invalid endpoints, missing secrets, incompatible policies, and unsupported provider combinations. |
| OPS-016 | Secret management | Load secrets through environment, files, or a dedicated provider without committing them. |
| OPS-017 | Database migrations | Maintain service-owned PostgreSQL and ClickHouse migrations. |
| OPS-018 | Backup | Back up Core, Observatory, telemetry metadata, and object artifacts according to policy. |
| OPS-019 | Restore | Restore documented service-owned data and validate integrity. |
| OPS-020 | Retention jobs | Apply telemetry, log, artifact, recording, and evaluation retention rules. |
| OPS-021 | OpenTelemetry Collector | Buffer, transform, route, and export traces, metrics, and logs. |
| OPS-022 | ClickHouse telemetry store | Store high-cardinality run, component, trace, and evaluation observations. |
| OPS-023 | Prometheus metrics | Store infrastructure and low-cardinality service metrics. |
| OPS-024 | Loki logs | Store searchable application and infrastructure logs. |
| OPS-025 | Grafana diagnostics | Provide low-level operational dashboards separate from agent-focused Observatory views. |
| OPS-026 | NVIDIA GPU exporter | Collect supported GPU utilization, VRAM, thermal, power, and process metrics. |
| OPS-027 | Database exporters | Collect PostgreSQL and ClickHouse health and performance metrics. |
| OPS-028 | Queue backlog recovery | Detect stale work, retry permitted jobs, and expose dead-letter or failed states. |
| OPS-029 | Transactional outbox | Reliably publish committed events without distributed transactions. |
| OPS-030 | Idempotent consumers | Safely process at-least-once event and job delivery. |
| OPS-031 | Service runbooks | Document recovery for databases, telemetry, queues, model providers, GPU capacity, and voice devices. |
| OPS-032 | Deployment metadata | Record git commit, image digest, configuration revision, host, and deployment timestamp. |
| OPS-033 | Rollback visibility | Show which versions are active and retain historical deployment markers for analysis. |
| OPS-034 | Storage-pressure monitoring | Track database, ClickHouse, object-store, log, and artifact usage. |
| OPS-035 | Network and provider diagnostics | Diagnose DNS, route, TLS, authentication, endpoint, and latency problems. |

## 24. Envisioned integration targets

These are integration targets already discussed or consistent with the intended platform boundary. They remain provider-based capabilities rather than assumptions baked into Core.

| ID | Integration | Envisioned use |
|---|---|---|
| INT-001 | Ollama | Local chat, reasoning, coding, embedding, and evaluation model workloads |
| INT-002 | Home Assistant | Authoritative device, room, scene, automation, notification, and event integration |
| INT-003 | SearXNG | Local metasearch provider for controlled web research |
| INT-004 | Local filesystem | Approved directory catalogue and on-demand document inspection |
| INT-005 | SMB | TrueNAS and other network-share catalogue and content access |
| INT-006 | Nextcloud | Approved personal and project document source |
| INT-007 | CalDAV or calendar providers | Calendar read and write operations |
| INT-008 | Whisper-compatible STT | Local speech transcription |
| INT-009 | Piper-compatible TTS | Local speech synthesis |
| INT-010 | Web Push | Browser and PWA notifications |
| INT-011 | S3-compatible storage | Artifact and evaluation object storage |
| INT-012 | PostgreSQL | Core and Observatory transactional stores |
| INT-013 | pgvector | Vector indexing within PostgreSQL |
| INT-014 | ClickHouse | High-cardinality telemetry and evaluation observations |
| INT-015 | Prometheus | Infrastructure and service metrics |
| INT-016 | Loki | Logs |
| INT-017 | Grafana | Low-level diagnostic dashboards |
| INT-018 | GitHub | Source-control metadata, issue or repository tools, and deployment correlation |
| INT-019 | Proxmox | Homelab status and approved management tools |
| INT-020 | TrueNAS | Storage status and approved dataset or service operations |
| INT-021 | 3D printer systems | Printer status, completion, failure, and notification workflows |
| INT-022 | Media services | Approved media lookup, status, and control tools |
| INT-023 | Cameras and vision | Explicitly authorized image or event inspection |
| INT-024 | Email providers | Approved notifications, research inputs, and message-related workflows |
| INT-025 | Additional OpenAPI or MCP services | Governed extension through typed contracts and Aura policy |

## 25. Product constraints accompanying the catalogue

The following boundaries remain part of the envisioned product:

- Aura does not replace Home Assistant.
- Aura does not require every file in all storage pools to be parsed and embedded in advance.
- Personalities do not maintain separate conflicting factual realities.
- Model prompts do not replace deterministic permission enforcement.
- Core operation does not require a cloud model.
- Agents do not receive unrestricted root, shell, Home Assistant administrator, or whole-filesystem access by default.
- Agent loops and retries are not unbounded.
- Observatory does not directly read Aura Core's database.
- Evaluation logic does not duplicate Aura Core's private component implementation.
- Telemetry does not indiscriminately capture private prompts, documents, memories, calendar data, or voice recordings.
- Frontend applications do not share feature logic by importing from one another; they reuse explicit libraries.
- Shared packages are not miscellaneous dumping grounds.
- The complete repository topology does not require all envisioned features or folders to exist at once.
- This catalogue does not define implementation order, delivery phases, or priorities.
