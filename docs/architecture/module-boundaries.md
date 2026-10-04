# Module boundaries

> **Status:** Architecture baseline summary. Sources: [architecture handover](./aura-ai-architecture-handover.md) and [catalogue constraints](../specifications/aura-ai-envisioned-feature-catalogue.md#25-product-constraints-accompanying-the-catalogue).

## Core internal boundaries

| Area | Responsibility |
|---|---|
| Runtime engines | Coordinate runs, context, prompts, models, tools, delegation, streaming, replay, and instrumentation without absorbing domain rules. |
| Domain modules | Own persistent business concepts, rules, writes, mappings, migrations, and deliberate public APIs. |
| Platform services | Provide generic database, event, outbox, queue, scheduling, locking, cache, storage, secrets, serialization, and telemetry mechanisms. |
| Providers | Implement inward-facing ports for external capabilities. Only bootstrap and composition code select concrete providers. |
| Entrypoints | Translate HTTP, tool, voice, job, worker, scheduler, or CLI input into shared application use cases. |

Cross-domain calls go through public commands, queries, DTOs, or events. A domain must not import another domain's adapters, ORM tables, internal handlers, or persistence mappings. The `kernel` remains limited to genuinely universal primitives.

## Core and Observatory

Core owns production behavior and data. Observatory owns monitoring, analysis, evaluation definitions, datasets, scoring, comparisons, regressions, dashboards, alerts, and incidents. Observatory may request controlled execution from Core, but it must not import Core private modules, share database schemas, or read Core tables. Each service owns its migrations.

Shared packages are limited to intentional contracts and SDK machinery: versioned schemas and identifiers, observability helpers, evaluation/replay contracts, generated clients, and supported extension interfaces. They must not become a home for private business logic or miscellaneous helpers.

## Frontend boundaries

Frontend applications compose routes, layout, navigation, and global providers. Feature behavior belongs in domain libraries. Applications do not import from one another; Observatory feature code does not import Aura feature implementations; generic UI libraries contain no Aura or Observatory business logic; generated API types are not manually duplicated.

## Governance and dependency direction

Model-facing tools invoke application use cases, which apply validation, policy, confirmation, audit, idempotency, and domain rules before reaching a port and provider. Models never replace deterministic permission enforcement. No agent receives unrestricted shell, root, Home Assistant administrator, or whole-filesystem access by default.

Architecture and instrumentation tests are expected to enforce import direction, ownership, stable component identifiers, and required measurements when implementation exists.

## Related working summaries

[System context](./system-context.md) · [Repository structure](./repository-structure.md) · [Tools, automation, and integrations](../specifications/tools-automation-and-integrations.md) · [Open decisions](../specifications/open-decisions.md)
