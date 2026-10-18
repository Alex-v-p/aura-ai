# Observatory and evaluation

> **Status:** Observatory separation and ownership are architecture baseline; dashboards, analyses, evaluators, and workflows are envisioned capabilities. Sources: [architecture handover §11](../architecture/aura-ai-architecture-handover.md#11-aura-observatory-architecture) and catalogue groups [`OBS`](./aura-ai-envisioned-feature-catalogue.md#19-aura-observatory-operational-monitoring), [`EVA`](./aura-ai-envisioned-feature-catalogue.md#20-evaluation-experiments-and-regression-analysis), and [`CMP`](./aura-ai-envisioned-feature-catalogue.md#21-component-specific-evaluation-coverage).

## Service boundary

Aura Observatory is a separate backend and web product with its own database, workers, scheduler, authentication and authorization boundary, migrations, retention, and deployment lifecycle. It owns monitoring and evaluation definitions and analysis. Core owns production modules and the controlled evaluation runner that executes the exact deployed code.

Observatory does not import Core private modules or read Core tables. It submits versioned evaluation requests, receives structured results, and consumes policy-filtered telemetry and deployment metadata. Normal Core requests do not depend on Observatory.

## Observability model

Meaningful runtime modules have stable component identifiers, versions, typed inputs and outputs, declared dependencies, operational metrics, quality evaluators, capture policy, and optional replay support. Runs produce correlated component spans covering context, retrieval, prompts, model routing and inference, tools and policy, response synthesis, memory processing, and persistence.

Capture levels are `none`, `metadata_only`, `hashed`, `redacted`, `sampled_content`, and `full_content`. Core enforces capture and redaction before export. Full content is limited to explicitly authorized evaluation or equivalent controlled contexts.

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
