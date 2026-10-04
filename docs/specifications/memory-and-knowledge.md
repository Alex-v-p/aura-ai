# Memory and knowledge

> **Status:** Architecture baseline plus envisioned capabilities. Sources: [architecture handover §8](../architecture/aura-ai-architecture-handover.md#8-memory-and-knowledge-architecture) and catalogue groups [`MEM`](./aura-ai-envisioned-feature-catalogue.md#8-long-term-memory-and-world-model), [`KNO`](./aura-ai-envisioned-feature-catalogue.md#9-local-documents-and-filesystem-knowledge), and [`WEB`](./aura-ai-envisioned-feature-catalogue.md#10-web-research).

## Information boundaries

Conversation messages, assembled run context, agent state, long-term memory, source documents, generated artifacts, evaluation fixtures, and run checkpoints are distinct information types. A vector index is an index over owned records, not the universal source of truth.

Long-term memory may be working, episodic, semantic, procedural, preference, or system memory. Each durable record carries ownership and visibility scope, provenance, observed and validity times, confidence, importance, sensitivity, status, disputes, supersession, reinforcement, and entity relationships as applicable.

The expected scope hierarchy is platform, household, user, workspace, agent, conversation, task, and run. Agent memory policy determines readable scopes, default searches and writes, broader writes requiring confirmation, exclusions, and channel/device sensitivity restrictions.

## Memory lifecycle

Envisioned memory processing extracts candidates from approved sources, classifies sensitivity and scope, resolves duplicates and entities, and then creates, reinforces, merges, disputes, or supersedes records. Historical truth and provenance remain inspectable. Authorized users can search, correct, delete, import, export, and review memory while preserving the required audit and lineage.

The world model represents inspectable entities and relationships such as people, projects, devices, rooms, services, decisions, procedures, problems, fixes, and events. It complements rather than replaces source-backed memories and documents.

## Local knowledge

Aura does not pre-parse and embed every byte of local, SMB, or other connected storage. The baseline strategy is hierarchical and on demand:

1. Catalogue approved sources using paths, directory context, types, sizes, timestamps, tags, hashes, access scopes, and availability.
2. Optionally derive lightweight summaries, keywords, entities, classifications, and metadata embeddings.
3. Search and present candidates before opening ambiguous, sensitive, large, or expensive content.
4. Parse, chunk, embed, and rerank selected content; answer with source provenance.
5. Cache processing by content identity and invalidate it when the source changes.

## Authoritative storage and processing direction

Aura Core PostgreSQL owns memory, document metadata, provenance, authorization, and lifecycle state. pgvector is the initial index behind a replaceable port; a dedicated vector database requires measured benchmark thresholds and an accepted decision. Production object content uses the Garage S3-compatible boundary, while local filesystem storage is development-only. Format-specific Python parsers are preferred, with an isolated Apache Tika fallback. Search and web-source adapters use SearXNG, HTTPX, Trafilatura, or isolated Playwright as appropriate. Full captured content remains policy-gated and is never implied by an index or telemetry record. See [technology stack](../architecture/technology-stack.md) and [ADR-0013](../adr/0013-postgresql-nats-valkey-garage-consistency.md).

Controlled web research follows the same provenance and policy principles: bounded search and retrieval, freshness awareness, source metadata, claim-linked citations, retention controls, and structured research artifacts. Fetched, parsed, and extension-provided content is untrusted evidence, never policy or instruction authority. Fetchers have no ambient credentials; allowed schemes, destination and redirect revalidation, private/link-local/loopback blocking, size/type/time/resource limits, and parser sandbox restrictions are mandatory.

## Related working summaries

[Terminology](../architecture/terminology.md) · [Module boundaries](../architecture/module-boundaries.md) · [Agents and execution](./agents-and-execution.md) · [Open decisions](./open-decisions.md)
