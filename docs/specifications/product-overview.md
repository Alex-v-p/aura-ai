# Product overview

> **Status:** Product capability summary. Architecture statements come from the [architecture handover](../architecture/aura-ai-architecture-handover.md); capabilities are **envisioned** unless explicitly described as baseline in that handover. See the canonical [feature catalogue](./aura-ai-envisioned-feature-catalogue.md).

Aura AI is intended to provide one local-first intelligence platform across text, voice, web, wall displays, automations, integrations, and household devices. It supports multiple independently configured agents and personas over shared runtime, memory, governance, provider, and infrastructure engines.

Aura Observatory is the separate operational and evaluation product. It explains system health and behavior at run and component level, compares experiments and baselines, and correlates regressions with code, models, prompts, policies, providers, deployments, data, and hardware.

## Envisioned capability areas

- Local-first, provider-agnostic, inspectable, bounded, and configuration-driven platform behavior (`GEN-001`–`GEN-012`).
- Persistent agents, reusable personas, conversations, bounded runs, durable tasks, delegation, and policy-based model execution (`AGT-001`–`AGT-020`, `RUN-001`–`RUN-025`, `DLG-001`–`DLG-010`, `MOD-001`–`MOD-024`, `CTX-001`–`CTX-013`).
- Scoped long-term memory, an inspectable world model, local document discovery and on-demand retrieval, and controlled web research (`MEM-001`–`MEM-035`, `KNO-001`–`KNO-030`, `WEB-001`–`WEB-012`).
- Typed tools, governed integrations, Home Assistant control, deterministic timers and reminders, schedules, workflows, calendars, and notifications (`TOL-001`–`TOL-024`, `HOM-001`–`HOM-016`, `AUT-001`–`AUT-020`).
- Local voice, room-aware interaction, a responsive Aura Web application, and a restricted Aura Wall application (`VOI-001`–`VOI-018`, `AUI-001`–`AUI-030`, `WAL-001`–`WAL-012`).
- Uploaded and generated artifacts with ownership, revisions, access scopes, integrity, retention, and links to their producing work (`ART-001`–`ART-012`).
- Identity, permissions, approvals, privacy, audit, secret isolation, retention, deletion, and export controls (`GOV-001`–`GOV-025`).
- Observatory monitoring, evaluation, experiments, regression analysis, component attribution, and reproducibility (`OBS-001`–`OBS-040`, `EVA-001`–`EVA-048`, `CMP-001`–`CMP-022`).
- Versioned contracts, SDKs, extensions, tests, and configurable local/homelab operations (`DEV-001`–`DEV-030`, `OPS-001`–`OPS-035`, `INT-001`–`INT-025`).

## Product constraints

Aura does not replace Home Assistant, pre-embed every file, give agents broad access by default, use prompts as permission enforcement, or require cloud models for core operation. Agent work is bounded. Personalities do not create conflicting factual realities. Observatory remains separate from Core, evaluation does not duplicate Core's private implementation, and telemetry does not indiscriminately capture private content.

Feature identifiers are stable references only. They do not communicate priority, dependency order, release grouping, or commitment.

## Related working summaries

[Architecture overview](../architecture/overview.md) · [Agents and execution](./agents-and-execution.md) · [Memory and knowledge](./memory-and-knowledge.md) · [Tools, automation, and integrations](./tools-automation-and-integrations.md) · [Clients and voice](./clients-and-voice.md) · [Observatory and evaluation](./observatory-and-evaluation.md) · [Open decisions](./open-decisions.md)
