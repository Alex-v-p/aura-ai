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

No ADR or module-specific documentation exists yet. When added, those documents belong in a task's context only when their scope applies.

## Development governance

- [Codex agent system](development/codex-agent-system.md) — roles, permissions, activation, model tiers, delegation, and hook trust.
- [Work-item process](development/work-item-process.md) — scoped work authorization and structured handoffs.
- [Architecture enforcement](development/architecture-enforcement.md) — manifests, hooks, validators, and current limitations.
- [Worktree collaboration](development/worktree-collaboration.md) — writer isolation, concurrency, resources, and integration.
