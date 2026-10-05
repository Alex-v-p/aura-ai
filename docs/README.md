# Aura AI documentation

Aura AI is a local-first, extensible personal AI platform. Aura Observatory is its separate operational monitoring and evaluation service. This documentation is arranged so ordinary work can use small, focused context rather than loading the complete project vision.

## Documentation authority

Use this order when sources differ:

1. The current user request or work item.
2. The canonical architecture handover for architecture, service boundaries, ownership, dependency direction, repository topology, technology baseline, and unresolved architectural decisions.
3. The canonical feature catalogue for envisioned capabilities, feature identifiers, product constraints, and potential integration targets.
4. Focused summaries, applicable accepted ADRs, and module-specific documentation, where they exist and do not conflict with the canonical sources.
5. Existing implementation as evidence of current state, not automatic architectural authority.

**Architecture baseline** means a settled boundary or constraint in the handover. **Envisioned** means a catalogue capability, not a priority, sequence, release commitment, or settled implementation. **Open decision** means the sources intentionally leave the choice unresolved.

## Minimal default reading path

For an ordinary bounded task, read:

1. This index.
2. The current request or work item.
3. The relevant focused architecture summary.
4. The relevant focused product summary.
5. Applicable ADRs or module documentation, when they exist.

This normally produces a context packet of two to five documents. Do not read every file under `/docs`, and do not load both canonical references by default. See [Context routing](context-routing.md) for the task lookup, selective locators, and context-packet convention.

## Canonical deep references

These are authoritative escalation references, not normal default context. Search for the relevant heading, term, or feature ID before reading unrelated sections.

- [Aura AI — Architecture and Repository Handover](architecture/aura-ai-architecture-handover.md) — canonical architecture, ownership rules, full target topology, technology baseline, and unresolved decisions.
- [Aura AI — Envisioned Feature Catalogue](specifications/aura-ai-envisioned-feature-catalogue.md) — canonical envisioned capabilities, feature identifiers, integration targets, and product constraints.

The canonical handover and feature catalogue are deep references. Their presence in the repository does not make them mandatory reading for every task.

## Focused architecture summaries

- [Overview](architecture/overview.md) — product boundaries, principles, logical architecture, and technology baseline.
- [System context](architecture/system-context.md) — services, clients, telemetry, storage, and communication boundaries.
- [Terminology](architecture/terminology.md) — stable meanings for core project concepts.
- [Module boundaries](architecture/module-boundaries.md) — ownership, dependency direction, and prohibited coupling.
- [Repository structure](architecture/repository-structure.md) — target placement map without the full repository tree.
- [Authoritative technology stack](architecture/technology-stack.md) — selected major lines, feature-to-tool mappings, provider boundaries, licensing cautions, and official sources.

### Accepted architecture decisions

- [ADR index](adr/README.md) — accepted decisions `0012`–`0016` for the implementation toolchain, data/coordination, execution, security, and client/voice protocols.

## Focused product summaries

- [Product overview](specifications/product-overview.md) — envisioned capability areas and product constraints.
- [Agents and execution](specifications/agents-and-execution.md)
- [Memory and knowledge](specifications/memory-and-knowledge.md)
- [Tools, automation, and integrations](specifications/tools-automation-and-integrations.md)
- [Clients and voice](specifications/clients-and-voice.md)
- [Observatory and evaluation](specifications/observatory-and-evaluation.md)
- [Open decisions](specifications/open-decisions.md) — unresolved choices preserved without selecting an outcome.

## Design guidance

- [Aura Web design foundation](design/README.md) — semantic themes, visual foundations, component interactions, failure recovery, accessibility, and attributed reference research.

Design guidance is implementation-facing visual and interaction direction. It
does not create runtime contracts, architecture decisions, frontend packages,
or a delivery roadmap. Read the design entry point first, then only the
focused design document relevant to the task.

Read the technology matrix and only the applicable ADRs for architecture, backend, frontend, deployment, integration, voice, or Observatory work. Do not load the full ADR set by default.

## Development governance

- [Codex agent system](development/codex-agent-system.md) — roles, permissions, activation, model tiers, delegation, and hook trust.
- [Work-item process](development/work-item-process.md) — scoped work authorization and structured handoffs.
- [Architecture enforcement](development/architecture-enforcement.md) — manifests, hooks, validators, and current limitations.
- [Worktree collaboration](development/worktree-collaboration.md) — writer isolation, concurrency, resources, and integration.

## Operational runbooks

- [Authentik relocation](runbooks/authentik-relocation.md) — move the temporary local identity provider to an external instance while preserving Aura's OIDC issuer and subject boundary.

## Local platform stack

The base Compose file contains only the normal Aura runtime. Copy
`.env.example` to `.env`, create the referenced runtime secret files, set
`AURA_STATE_ROOT` to an encrypted host directory, and set the required owner
subject before starting Aura:

```sh
docker compose up --build --detach --wait aura-core-api aura-core-worker aura-web
```

The temporary Authentik deployment is an explicit, optional override. It is
never interpolated by normal Compose commands. The override defaults to the
verified immutable Authentik 2026.5.7 multi-architecture digest. Operators may
override `AUTHENTIK_IMAGE` only with another verified digest-qualified image.
Set stable HTTPS issuer and browser-host inputs, stable OIDC
audience/client/redirect values, and the runtime secret files, then include
the override for every identity command. Authentik's startup guard rejects any
image value that is not `...@sha256:<digest>` before it runs its lifecycle:

```sh
docker compose -f compose.yaml -f deploy/compose/local-identity.yaml \
  --profile local-identity config --quiet
docker compose -f compose.yaml -f deploy/compose/local-identity.yaml \
  --profile local-identity up --build --detach --wait authentik-server authentik-worker
```

Aura Web forwards `/api/` and SSE requests to Core on the same origin. Core
connects to Ollama through `AURA_OLLAMA_URL`; that endpoint is never exposed
by nginx or returned by the API. The API receives narrow external egress for
OIDC discovery/JWKS and Ollama; the worker receives it only for Ollama. Linux
hosts map the local Ollama default through Docker's `host-gateway`; configure
an operator-controlled LAN URL instead when appropriate. The browser-facing,
database, NATS, and Valkey networks remain internal and no Core provider
endpoint is host-published. When the optional identity override is selected,
only the Core API also joins Authentik's control network so it can discover the
temporary issuer; workers, Core persistence, Valkey, and NATS never join it.
Nginx access logs record normalized paths without query strings, and Core's
Uvicorn access log is disabled, so OIDC callback `code` and `state` values do
not enter ordinary request logs. Core and Authentik use separate nonpersistent,
password-protected Valkey services and isolated Compose networks; identity
never shares Aura's cache.
Every durable PostgreSQL, JetStream, and Authentik media mount requires the
operator-provided encrypted `AURA_STATE_ROOT`. The normal config command is
`docker compose config --quiet`; it does not read the optional override or
require any Authentik variables.

### Disposable full-stack verification

The test-only full-stack fixture uses disposable state and deterministic fake
credentials. External mode starts OIDC and Ollama in a separate Compose project
and confirms Aura reaches them solely through configured provider URLs:

```sh
AURA_E2E_MODE=external sh deploy/compose/run-full-stack-e2e.sh
```

Local-identity mode exercises the optional Authentik profile using the pinned
test default. Set `AURA_E2E_AUTHENTIK_IMAGE` only to override it with another
digest-qualified, operator-approved test image; no real operator credentials
or endpoints are used:

```sh
AURA_E2E_MODE=local-identity sh deploy/compose/run-full-stack-e2e.sh
```
