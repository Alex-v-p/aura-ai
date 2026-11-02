# ADR-0017: Governed production run evidence

- **Status:** Accepted
- **Date:** 2026-10-25
- **Scope:** Aura Core run evidence export and Aura Observatory inspection

## Context

Metadata-only telemetry explains timing, topology, health, and outcomes, but it
cannot show an owner the exact request, admitted context, retrieved chunks,
compiled prompt, displayed thinking, generated response, or tool interaction
that produced one of their runs. Those values are private production content
and cannot be added to ordinary OpenTelemetry or the live invalidation stream
without collapsing the Core/Observatory trust and availability boundaries.

## Decision

An owner-controlled deployment policy may enable `full_content` run-evidence
capture for ordinary Core runs. Core emits explicitly serialized,
model-visible or executor-visible evidence through the versioned
`run-evidence-v1` one-way asynchronous channel. It never reads from
Observatory, waits for acknowledgement, or changes a run outcome because the
export queue, transport, Observatory, PostgreSQL, or object storage is
unavailable. Backpressure and failures produce only bounded content-free
completeness signals.

Ordinary OTLP traces, metrics, logs, and all SSE events remain metadata-only.
SSE may identify a changed run so the selected inspector can invalidate its
REST snapshot, but never carries evidence, manifests, digests, deletion
details, or content.

Eligible evidence is exact admitted requests and triggers, compiled prompts,
admitted conversation context, memory and document chunks, model-visible
attachments, model routing, displayed thinking, generated output,
model-visible tool arguments and results, policy and approval decisions,
execution outcomes, response synthesis, persistence, delivery, worker,
automation, schedule, and evaluation inputs and outputs. Each execution
boundary uses an explicit closed serializer. Arbitrary object dumping is not
allowed.

Credentials, secret-bearing fields, bearer material, and provider-hidden
reasoning are permanently ineligible. Core does not request, infer, or
reconstruct hidden reasoning. A prohibited or oversized item is rejected as a
whole before export; a bounded content-free partial or excluded reason is
emitted instead of presenting a redaction as exact evidence.

Whole-item inspection applies to every approved media type, including
model-visible images and PDFs. Producers decode and validate all bounded bytes
and attach a closed, content-free admission attestation containing allowlisted
scanner and policy revisions, inspection time, performed modes, and pass or
whole-item-rejection outcome. Text requires decoded-text inspection; JSON
requires decoded-text and container inspection. Images require format-aware container validation, metadata
extraction, and OCR; PDFs require sandboxed parsing, metadata and decoded-text
extraction, plus rendered-page OCR. Every extracted representation is checked
for credential, secret, and bearer material. Ingestion validates the
attestation shape, revisions, outcome, and media-specific mode set without
receiving scanner output. Unsupported, malformed, or incompletely inspectable
media is rejected whole as partial; no redacted substitute is stored under an
exact-evidence claim.

The byte domain is exact and portable. Structured evidence uses RFC 8785
canonical JSON; non-attachment text uses exact UTF-8 content bytes; every
attachment, including JSON and text, uses decoded base64 bytes and is streamed
with its declared media type. Digest and byte count cover those exact bytes.
`eventId` and `evidenceId` are stable logical identities while each attempt has
a distinct `deliveryId`; initial delivery is attempt one and retries are
attempt two or greater. Repeating identical immutable evidence is idempotent.
Reusing either stable identifier with changed correlation, sequence, kind,
policy revision, content-admission attestation, digest, count, or canonical
bytes is a content-free duplicate collision and is rejected.

Observatory PostgreSQL owns evidence manifests, authorization, durable
deletion truth, and content-free audit records. Evidence bytes use a dedicated
Observatory Garage bucket and least-privilege identity separate from
evaluation artifacts. ClickHouse remains content-free. Evidence has
manual-only retention and owner-only access. Reads use run-scoped,
owner-authorized, `no-store` streaming of approved media types. Manual deletion
is CSRF-protected, idempotent, durable, and removes bytes while retaining
content-free inspection metadata and audit truth.

Aura Web remains completely unaware of Observatory. It contains no
Observatory link, label, route, import, configuration, shared session, browser
communication, run-evidence dependency, or evidence content. Observatory Web
uses its separate owner session and must render evidence inertly and lazily;
content may not enter URLs, logs, metrics, browser storage, service-worker
caches, or screenshots.

## Consequences

- Full run inspection is opt-in deployment policy, not ordinary telemetry.
- Exact content is available only through governed Observatory resources.
- Missing, rejected, dropped, late, unavailable, and deleted evidence remains
  explicit instead of being synthesized.
- At-least-once delivery requires stable event/evidence identifiers,
  per-run sequence, retry identity, digest verification, idempotent ingestion,
  and deterministic handling of duplicates and out-of-order delivery.
- Core remains usable with Observatory and all Observatory-owned stores down.
- Runtime capture, storage/API implementation, UI, and full-stack acceptance
  remain separate sequential work items governed by this decision and the
  versioned contracts.

## Required follow-up observability

Runtime work registers versioned components
`aura.platform.run_evidence_export@1`,
`aura.observatory.run_evidence_ingestion@1`,
`aura.observatory.run_evidence_storage@1`, and
`aura.observatory.run_inspection_query@1`. Their bounded operation spans are
`run_evidence.serialize`, `run_evidence.queue_offer`,
`run_evidence.transport_publish`, `run_evidence.ingest_validate`,
`run_evidence.ingest_deduplicate`, `run_evidence.manifest_write`,
`run_evidence.object_write`, `run_evidence.object_delete`,
`run_evidence.deletion_finalize`, `run_inspection.metadata_query`,
`run_inspection.manifest_query`, `run_inspection.merge`, and
`run_evidence.object_stream`. Each span begins immediately before its named
operation and ends after its success or bounded failure outcome; no span spans
an idle queue wait or evidence rendering. Under metadata-observation-v2, spans
correlate only `traceId`, `spanId`, `parentSpanId`, and bounded `runId`.
`eventId`, `evidenceId`, capture state, evidence kind, and media classification
remain only in the separate run-evidence channel/manifests; adding them to OTLP
would require a future separately versioned metadata contract. Telemetry never
contains evidence content, digests, object keys, or owner data.

The exporter records `run_evidence_export_outcome_count` and
`run_evidence_export_dropped_count`; ingestion records
`run_evidence_ingest_outcome_count`; storage and deletion record
`run_evidence_storage_outcome_count` and
`run_evidence_deletion_outcome_count`. Component ID/version remain structural
metadata-observation fields. Metric dimensions use only the v2-supported,
finite `operation`, `outcome`, `status`, `dependency`, and bounded
`errorClass` keys. Error classes are
`queue_pressure`, `serialization_rejected`, `transport_unavailable`,
`ingest_rejected`, `manifest_store_unavailable`,
`object_store_unavailable`, `digest_mismatch`, `duplicate_collision`, and
`deletion_failed`; exception text and content are forbidden dimensions.

Component latency histograms are `run_evidence_serialize_latency_ms`,
`run_evidence_queue_offer_latency_ms`, `run_evidence_transport_latency_ms`,
`run_evidence_ingest_validation_latency_ms`,
`run_evidence_ingest_dedupe_latency_ms`,
`run_evidence_manifest_storage_latency_ms`,
`run_evidence_object_storage_latency_ms`, `run_evidence_deletion_latency_ms`,
`run_inspection_query_latency_ms`, and `run_inspection_merge_latency_ms`.
Exporter saturation is measured by `run_evidence_export_queue_depth` and
`run_evidence_export_queue_capacity`. These measurements use the same
v2-supported finite dimensions; correlation identifiers are never metric
labels. Follow-up contract and runtime tests must assert these exact names,
operation boundaries, finite dimensions, and absence of content-derived or
unbounded labels.

Declared dependencies are the one-way evidence transport for export,
Observatory PostgreSQL for manifests and deletion truth, and the dedicated
Garage bucket for bytes. Required evaluators are
`run_evidence_schema_compatibility`, `run_evidence_failure_isolation`, and
`observatory_run_evidence_privacy`. Replay of production evidence is disabled
by default and requires a separately owner-authorized controlled evaluation;
inspection itself is read-only. Cardinality tests must prove that only the
finite dimensions above and bounded correlation identifiers are emitted.

## Non-goals

This ADR does not add runtime capture, migrations, a Garage bucket, deployment
configuration, API implementation, or UI behavior. It does not authorize
credentials, hidden chain-of-thought, arbitrary runtime serialization, cloud
export, or automatic retention expiry.
