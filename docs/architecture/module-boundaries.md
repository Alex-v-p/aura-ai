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

## Integrated roots

The AURA-0015 conversation vertical is registered in
[`tooling/architecture/modules.yaml`](../../tooling/architecture/modules.yaml).
The registry records existing roots and public API markers; it does not create
new product behavior.

| Module | Root | Public boundary | Depends on |
|---|---|---|---|
| `aura-core.service` | `services/aura-core` | `entrypoints/api/app.py` | Registered Core packages and file modules |
| `aura-core.package` and namespace packages (`aura-core.domains`, `aura-core.entrypoints`, `aura-core.platform`, `aura-core.providers`, `aura-core.runtime`, and their registered subpackages) | `services/aura-core/src/aura_core/**` | Concrete public file selected per package | Registered file modules and deliberate public surfaces |
| `aura-core.bootstrap` and `aura-core.bootstrap.*` | `services/aura-core/src/aura_core/bootstrap` | `conversation_uow.py`, `database.py`, `health.py` | Core public domain, platform, and runtime modules |
| `aura-core.domains.*` | `services/aura-core/src/aura_core/domains/**` | Each domain's `public.py` plus registered file modules | Platform, runtime, and deliberate public domain surfaces |
| `aura-core.domains.interaction.agents` | `services/aura-core/src/aura_core/domains/interaction/agents` | `agents/public.py`; SQL and provider adapters remain persistence implementations | Persona public API, runtime capacity and prompting, conversation public API, database primitives |
| `aura-core.domains.interaction.personas` | `services/aura-core/src/aura_core/domains/interaction/personas` | `personas/public.py`; persistence and adapters remain private | Audit and identity public APIs, database primitives |
| `aura-core.platform.*` | `services/aura-core/src/aura_core/platform/{auth,database,oidc,outbox,readiness,telemetry}` | Component file or outbox package exports | Core public types and persistence primitives |
| `aura-core.runtime.*` | `services/aura-core/src/aura_core/runtime/{models,streaming}` | Model ports and stream publisher | Execution event/types and platform primitives |
| `aura-core.runtime.prompting` | `services/aura-core/src/aura_core/runtime/prompting` | `prompting/public.py` | Agent and persona public APIs |
| `aura-core.providers.models.ollama.*` | `services/aura-core/src/aura_core/providers/models/ollama` | `adapter.py` | Runtime model ports and execution run ports |
| `aura-core.entrypoints.*` | `services/aura-core/src/aura_core/entrypoints/**` | API app, worker app, CLI, and route modules, including the agents and personas routes | Bootstrap, public APIs, platform adapters, Ollama adapter |
| `aura-web.interaction-conversations` | `frontend/libs/aura/interaction/conversations` | `src/index.ts` | Transport client, agents feature, personas feature, shared UI |
| `aura-web.interaction-agents` | `frontend/libs/aura/interaction/agents` | `src/index.ts` | Transport client, personas feature |
| `aura-web.interaction-personas` | `frontend/libs/aura/interaction/personas` | `src/index.ts` | Transport client |
| `aura-web.api-client` | `frontend/libs/platform/aura-api-client` | `src/index.ts` | OpenAPI and run-event contracts |
| `aura-contract.openapi` | `contracts/openapi/aura-v1.yaml` | Versioned OpenAPI document | — |
| `aura-contract.run-events` | `contracts/events/runs/v1` | Versioned JSON Schema | — |

Generated clients remain transport-only. Core remains provider-agnostic at its
domain and runtime boundaries, and Observatory remains a separate service with
no Core implementation or database dependency.

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
