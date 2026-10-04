# Aura AI architecture decision records

This directory contains accepted, durable architecture decisions. ADRs refine the [architecture handover](../architecture/aura-ai-architecture-handover.md) and [authoritative technology stack](../architecture/technology-stack.md); they do not create runtime code, contracts, manifests, or delivery scope by themselves.

## Numbering and authority

The handover reserved ADR numbers `0001`–`0011` for earlier decisions that are not present in this documentation-only repository snapshot. New accepted records therefore begin at `0012`. Do not reuse a number or infer that an absent reserved record has been superseded. A new decision that materially changes one of these records must add a new ADR and update the handover and affected focused specifications.

Authority follows the repository documentation policy: the current user/work item, accepted ADRs and approved specifications, then implementation guidance. The feature catalogue remains envisioned context and never becomes implementation scope through an ADR.

## Accepted records

| ADR | Decision | Applies to |
|---|---|---|
| [0012](0012-python-angular-tailwind-toolchain.md) | Python, Angular, Tailwind, workspace, and test toolchain | Core, clients, shared quality gates |
| [0013](0013-postgresql-nats-valkey-garage-consistency.md) | Data authority, transport, cache, storage, scheduling, and consistency | Core and Observatory platform services |
| [0014](0014-aura-owned-execution-langgraph-backend.md) | Aura-owned execution with optional LangGraph backend | Agent runtime and evaluation |
| [0015](0015-authentik-bff-secrets-extensions.md) | Authentik BFF sessions, SOPS/age, and isolated extensions | Identity, secrets, integrations, extensions |
| [0016](0016-api-realtime-pwa-and-voice-protocols.md) | API/realtime, generated clients, PWA, gRPC/WebRTC, and local voice | Web, Wall, satellites, Core API |

## Review expectation

Reviewers should check that implementation changes preserve the decision's context, consequences, security and privacy constraints, and replaceable boundaries. A provider update within the stated major line is not an ADR change; a new authority, trust boundary, language, protocol, or ownership relationship is.
