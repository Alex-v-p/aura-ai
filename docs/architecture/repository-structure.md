# Repository structure

> **Status:** Architecture baseline summary of the target placement map. See [architecture handover §18](./aura-ai-architecture-handover.md#18-full-target-repository-tree). This is not an instruction to scaffold empty directories.

## Target top-level areas

| Area | Intended contents |
|---|---|
| `apps/` | Core and Observatory backend deployables plus Aura Web, Aura Wall, and Observatory Web applications. |
| `packages/` | Deliberate Python and frontend packages, including Core modules, contracts, SDKs, generated clients, and shared UI primitives. |
| `satellites/` | Thin deployable clients such as the voice satellite. |
| `contracts/` | Versioned OpenAPI, event, telemetry, evaluation, tool, voice, and extension schemas. |
| `evaluations/`, `benchmarks/`, `tests/` | Evaluation data and fixtures, performance work, and cross-system verification. |
| `extensions/` | Maintained examples for supported extension points. |
| `deploy/`, `infra/` | Deployment composition and repeatable infrastructure configuration. |
| `docs/` | Canonical sources, compact working summaries, and later documentation justified by implemented needs. |
| `scripts/` | Repository-level development, validation, generation, migration, evaluation, and diagnostic commands. |

## Application of the map

The full tree describes where concerns belong once they exist. It does not require all envisioned features, packages, applications, documents, or directories to be created at once. Add structure only with substantive content.

Organize backend code by cohesive domain and capability rather than repository-wide `models`, `services`, `repositories`, `controllers`, `schemas`, or `utils` folders. A mature module may have domain, application, and adapter layers; a small cohesive module may remain shallow. The public boundary matters more than ceremonial depth.

Frontend feature behavior belongs in libraries rather than application shells. Core and Observatory contracts remain independent, generated clients follow those contracts, and service-owned migrations stay with their owning service.

The current technology placement assumes one locked Python workspace and an Angular Nx/pnpm workspace while retaining package and dependency boundaries. Exact runtime choices that remain open are listed in [open decisions](../specifications/open-decisions.md).

## Related working summaries

[Architecture overview](./overview.md) · [Module boundaries](./module-boundaries.md) · [Open decisions](../specifications/open-decisions.md)
