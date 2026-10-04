# Open decisions

> **Status:** Unresolved. This list preserves [architecture handover §21](../architecture/aura-ai-architecture-handover.md#21-intentionally-unresolved-decisions). Do not infer decisions from examples or envisioned features in the [catalogue](./aura-ai-envisioned-feature-catalogue.md).

## Identity, security, and privacy

- Household role and membership semantics, including how OIDC claims become Aura household permissions.
- Retention and encryption periods for memory, files, credentials, recordings, telemetry, and evaluation artifacts.
- File-source inclusion, exclusion, authorization, and confirmation rules.
- Home Assistant allowlists and high-impact action classification.
- Detailed telemetry and captured-content retention periods.
- Repository licensing and public/private policy.
- External-provider credential retention, rotation, and revocation periods.

## Runtime, data, and extensibility

- The benchmark thresholds and measurement method for escalating from pgvector to dedicated vector infrastructure.
- Exact model routing policy and model inventory, including hardware placement and fallback policy.
- Exact provider discovery and extension sandbox policy within the selected isolated process/container boundary.
- Exact graph checkpoint retention and recovery policy within the Aura-owned execution boundary.

## Providers, clients, and evaluation

- Evaluation judge models, local/cloud policy, and human calibration rules.
- Alert delivery provider selection beyond the initial Web Push and Home Assistant channels.
- Exact wake-word/STT/TTS provider models, voice artifacts/configuration, voice policy, and voice protocol payload details.

## Source reconciliation

No material conflict exists between the current architecture handover and envisioned feature catalogue. Catalogue entries describing cloud services, integration targets, voice examples, storage options, or client capabilities remain envisioned and do not resolve the choices above.

The following distinctions are already settled and are not open decisions:

- Authentik OIDC Authorization Code flow, separate Aura and Observatory clients/audiences, and BFF-managed browser sessions are settled. Remaining identity questions concern household role semantics and the detailed Observatory isolation policy, not the selected client or session mechanism.
- PostgreSQL is authoritative for Core and Observatory state; NATS JetStream, Valkey, Garage, and pgvector have the roles described in [the technology matrix](../architecture/technology-stack.md).
- Aura owns the execution boundary; LangGraph is an optional executor implementation behind that boundary.
- OpenAPI 3.1, JSON Schema 2020-12, SSE, Protobuf/gRPC, WebRTC, and generated transport clients are the selected protocol categories.
- Google Calendar is the first calendar adapter; Home Assistant remains authoritative for home control.
- Python voice satellites use the selected openWakeWord, Silero VAD, faster-whisper, and isolated Piper-compatible provider categories, subject to provider/licensing review.
- Aura Wall begins as a restricted browser-kiosk PWA.
- Provider-specific examples remain behind ports and adapters unless the architecture identifies a service-owned store or protocol.
- Core operation does not require a cloud model.
- The target repository tree does not require every folder or feature to exist immediately.

## Related working summaries

[Architecture overview](../architecture/overview.md) · [Technology matrix](../architecture/technology-stack.md) · [ADR index](../adr/README.md) · [Module boundaries](../architecture/module-boundaries.md) · [Product overview](./product-overview.md)
