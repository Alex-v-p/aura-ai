# ADR-0013: PostgreSQL, NATS JetStream, Valkey, Garage, scheduling, and consistency

- **Status:** Accepted
- **Date:** 2026-10-04
- **Scope:** Aura Core and Aura Observatory data and platform services

## Decision

Use separate PostgreSQL databases for Aura Core and Aura Observatory. PostgreSQL owns transactional business state, workflow and scheduler state, service-owned migrations, and the authoritative metadata for objects and vector indexes. Use pgvector initially behind a port. A dedicated vector database is permitted only after documented benchmark thresholds and a new ADR.

Publish durable asynchronous work through a transactional outbox to NATS JetStream. Assume at-least-once delivery, stable event IDs and schema versions, explicit correlation/causation IDs, and idempotent consumers. NATS is a transport and does not own business truth. Use PostgreSQL row/advisory locks for transactional coordination. Use Valkey only for disposable cache, rate limits, leases, and short-lived session state.

Use Garage as the production S3-compatible object provider and a local filesystem adapter for development only. Core retains object authorization and metadata. Use format-specific parsers with an isolated Apache Tika fallback.

## Consequences

The Core scheduler and workers dispatch due durable work from PostgreSQL-owned state. Cache or transport loss is recoverable. Observatory stores its own transactional metadata and high-cardinality observations without reading Core tables. Storage and transport providers remain replaceable behind ports.

## Open decisions retained

Exact retention/encryption periods, vector escalation thresholds, object deployment details, and extension discovery/sandbox mechanism remain open. This ADR selects a category and initial provider boundary, not operational configuration.
