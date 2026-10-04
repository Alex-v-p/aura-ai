# Open decisions

> **Status:** Unresolved. This list preserves [architecture handover §21](../architecture/aura-ai-architecture-handover.md#21-intentionally-unresolved-decisions). Do not infer decisions from examples or envisioned features in the [catalogue](./aura-ai-envisioned-feature-catalogue.md).

## Identity, security, and privacy

- Exact authentication and household identity model.
- Exact Observatory authentication isolation.
- Whether any cloud fallback is allowed and under which policy.
- Memory retention and encryption policy.
- File-source inclusion, exclusion, and confirmation rules.
- Home Assistant allowlists and high-impact action classification.
- Detailed telemetry retention periods.
- Licensing and public/private repository policy.

## Runtime, data, and extensibility

- Queue and job transport.
- Cache and distributed-lock implementation.
- Exact object-storage deployment.
- Whether a dedicated vector database is ever required in addition to PostgreSQL.
- Exact model routing policy and model inventory.
- Extension discovery and sandboxing mechanism.

## Providers, clients, and evaluation

- Exact calendar provider and authentication flow.
- Exact wake-word, speech-to-text, and text-to-speech providers.
- Voice streaming protocol details beyond the contract boundary.
- Evaluation judge models and calibration rules.
- Alert delivery providers.
- Whether wall clients are browser kiosks only or require native wrappers.

## Source reconciliation

No material conflict exists between the current architecture handover and envisioned feature catalogue. Catalogue entries describing cloud services, integration targets, voice examples, storage options, or client capabilities remain envisioned and do not resolve the choices above.

The following distinctions are already settled and are not open decisions:

- Observatory has a separate authentication boundary; only its exact isolation mechanism is unresolved.
- Provider-specific examples remain behind ports and adapters unless the architecture identifies a service-owned store or protocol.
- Core operation does not require a cloud model.
- The target repository tree does not require every folder or feature to exist immediately.

## Related working summaries

[Architecture overview](../architecture/overview.md) · [Module boundaries](../architecture/module-boundaries.md) · [Product overview](./product-overview.md)
