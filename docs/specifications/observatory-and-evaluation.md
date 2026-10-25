# Observatory and evaluation

> **Status:** Observatory separation and ownership are architecture baseline; dashboards, analyses, evaluators, and workflows are envisioned capabilities. Sources: [architecture handover §11](../architecture/aura-ai-architecture-handover.md#11-aura-observatory-architecture) and catalogue groups [`OBS`](./aura-ai-envisioned-feature-catalogue.md#19-aura-observatory-operational-monitoring), [`EVA`](./aura-ai-envisioned-feature-catalogue.md#20-evaluation-experiments-and-regression-analysis), and [`CMP`](./aura-ai-envisioned-feature-catalogue.md#21-component-specific-evaluation-coverage).

## Service boundary

Aura Observatory is a separate backend and web product with its own database, workers, scheduler, authentication and authorization boundary, migrations, retention, and deployment lifecycle. It owns monitoring and evaluation definitions and analysis. Core owns production modules and the controlled evaluation runner that executes the exact deployed code.

Observatory does not import Core private modules or read Core tables. It submits versioned evaluation requests, receives structured results, and consumes policy-filtered telemetry and deployment metadata. Normal Core requests do not depend on Observatory.

## Observability model

Meaningful runtime modules have stable component identifiers, versions, typed inputs and outputs, declared dependencies, operational metrics, quality evaluators, capture policy, and optional replay support. Runs produce correlated component spans covering context, retrieval, prompts, model routing and inference, tools and policy, response synthesis, memory processing, and persistence.

Capture levels are `none`, `metadata_only`, `hashed`, `redacted`, `sampled_content`, and `full_content`. Core enforces capture policy before export. Ordinary OTLP and SSE remain metadata-only. In addition to controlled evaluation capture, [ADR-0017](../adr/0017-governed-production-run-evidence.md) authorizes an owner-controlled deployment policy to capture exact model-visible and executor-visible evidence for ordinary production runs on a separate one-way asynchronous `run-evidence-v1` channel. This exception does not place content in telemetry.

## Governed run inspection

Observatory assembles the owner-only run inspection REST snapshot from
content-free ClickHouse observations and Observatory PostgreSQL evidence
manifests. Evidence bytes are fetched lazily from a dedicated Observatory
Garage bucket, separate from evaluation artifacts. PostgreSQL owns manifests,
authorization, durable deletion state, and content-free audit; ClickHouse
remains content-free. Metadata-only SSE may invalidate one run snapshot by run
identifier, but carries no evidence content or manifest.

Exact evidence is eligible only through explicit serializers for admitted
requests and triggers, compiled prompts, admitted conversation context,
retrieved memory and document chunks, model-visible attachments, routing,
displayed thinking, generation output, model-visible tool activity, policy and
approval decisions, synthesis, persistence, delivery, workers, automations,
schedules, and evaluations. Credentials, secret-bearing fields, bearer
material, arbitrary object dumps, and provider-hidden reasoning are
unrepresentable. A prohibited item is rejected whole and reported as a
bounded content-free partial reason.

Every captured item carries a content-free admission attestation with
allowlisted scanner/policy revisions, inspection time, pass outcome, and the
performed inspection modes. Text requires decoded-text inspection; JSON
requires decoded-text and container inspection; images require format/container validation, metadata extraction,
and OCR; PDFs additionally require sandboxed decoded-text extraction and
rendered-page OCR. Unsupported, malformed, or incompletely inspectable media
is rejected whole, and ingestion validates the attestation rather than trusting
an unchecked media declaration.

Evidence is retained until explicit owner deletion. Access is owner-only and
`no-store`; deletion is CSRF-protected, idempotent, durable, and auditable
without content. Export pressure or any Observatory failure cannot change or
delay a Core run. Missing, rejected, dropped, late, unavailable, and deleted
evidence remains visible as completeness state.

Follow-up runtime instrumentation uses versioned components
`aura.platform.run_evidence_export@1`,
`aura.observatory.run_evidence_ingestion@1`,
`aura.observatory.run_evidence_storage@1`, and
`aura.observatory.run_inspection_query@1`. Metadata-only spans correlate run,
trace, span, and parent-span identifiers supported by metadata-observation-v2.
Event/evidence identifiers, capture state, evidence kind, and media
classification stay exclusively in the separate evidence channel/manifests;
telemetry support would require a future separately versioned metadata
contract. Export outcome/drop, ingest outcome, storage outcome, and deletion
outcome counters use only v2-supported finite `operation`, `outcome`,
`status`, `dependency`, and bounded `errorClass` dimensions; component ID and
version remain structural fields. PostgreSQL, the
evidence transport, and the dedicated Garage bucket are declared
dependencies. Schema-compatibility, failure-isolation, and privacy evaluators
are mandatory. Production evidence is not a replay grant; replay remains
disabled unless a controlled evaluation is separately owner-authorized. No
evidence content, digest, object key, exception text, or owner value may
become a span attribute, metric label, log, or evaluator diagnostic.

The required operation spans are `run_evidence.serialize`,
`run_evidence.queue_offer`, `run_evidence.transport_publish`,
`run_evidence.ingest_validate`, `run_evidence.ingest_deduplicate`,
`run_evidence.manifest_write`, `run_evidence.object_write`,
`run_evidence.object_delete`, `run_evidence.deletion_finalize`,
`run_inspection.metadata_query`, `run_inspection.manifest_query`,
`run_inspection.merge`, and `run_evidence.object_stream`; each covers only its
named active operation. Corresponding bounded latency histograms are
`run_evidence_serialize_latency_ms`, `run_evidence_queue_offer_latency_ms`,
`run_evidence_transport_latency_ms`,
`run_evidence_ingest_validation_latency_ms`,
`run_evidence_ingest_dedupe_latency_ms`,
`run_evidence_manifest_storage_latency_ms`,
`run_evidence_object_storage_latency_ms`, `run_evidence_deletion_latency_ms`,
`run_inspection_query_latency_ms`, and `run_inspection_merge_latency_ms`.
`run_evidence_export_queue_depth` and `run_evidence_export_queue_capacity`
measure saturation. Runtime acceptance asserts these names and boundaries and
proves that metrics use only the finite dimensions above, never correlation
identifiers or content-derived values.

## Evaluation modes

- **Passive production monitoring** measures ordinary operational behavior and approved quality signals.
- **Module replay** runs saved or synthetic input against one component version, optionally with versioned dependency fixtures.
- **End-to-end evaluation** exercises a complete scenario through Core's controlled evaluation runner.

The envisioned evaluation system manages versioned datasets, suites, deterministic and model-assisted evaluators, human review, experiments, baselines, thresholds, regression and change-point detection, cohort comparison, component attribution, reproducibility metadata, exports, and retention. Operational and quality metrics remain distinct.

## AURA-0089 owner decisions

The following decisions are authoritative for the versioned evaluation
contracts and their downstream implementations:

- Content-bearing evaluation data is permitted only when an owner-authorized
  dataset or controlled evaluation request explicitly selects a non-
  `metadata_only` capture classification and declares its authorization
  purpose, scope, and retention mode. Ordinary production telemetry remains
  `metadata_only`.
- Evaluation artifacts have manual-only retention. They remain available until
  an explicitly authorized manual deletion; no automatic expiry or background
  purge is implied by this policy. Storage credentials, object layout, and
  deletion mechanics are assigned to the artifact-storage work item.
- Deterministic evaluators are preferred. Model-assisted judges are local
  Ollama only; cloud judges are not enabled by this policy. The exact local
  model inventory, prompts, rubrics, seeds, calibration datasets, and advisory
  thresholds remain deferred to the calibration work item.
- Baselines are immutable after creation and may be promoted only by an
  explicit owner action from a defined result set. Automatic promotion and
  mutation of protected repository baselines are prohibited.

These decisions do not resolve global retention or encryption periods for
memory, files, credentials, recordings, telemetry, or other platform data.

Observatory PostgreSQL owns transactional metadata; ClickHouse stores high-cardinality observations; Prometheus stores infrastructure metrics; Loki stores logs; object storage holds approved large artifacts. Grafana remains a supplementary low-level diagnostic interface.

## Authoritative implementation direction

Observatory uses separate Python and Angular products and databases. OpenTelemetry SDKs and Collector provide the telemetry boundary; ClickHouse, Prometheus, Loki, and Grafana remain separate stores and views. Polars, PyArrow, NumPy/SciPy, and ECharts support analysis. ClickHouse and PostgreSQL migrations are service-owned and versioned. Core controls redaction and capture before telemetry leaves its boundary, and Observatory never reads Core tables.

Evaluation prefers deterministic checks, versioned local Ollama judges, and
human calibration. Cloud judges are not enabled. Exact judge inventory,
calibration rules, telemetry retention, and captured-content retention remain
open where they are not covered by the AURA-0089 owner decisions above. See
[technology stack](../architecture/technology-stack.md),
[ADR-0013](../adr/0013-postgresql-nats-valkey-garage-consistency.md), and
[open decisions](./open-decisions.md).

## Related working summaries

[System context](../architecture/system-context.md) · [Module boundaries](../architecture/module-boundaries.md) · [Product overview](./product-overview.md) · [Open decisions](./open-decisions.md)
