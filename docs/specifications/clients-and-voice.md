# Clients and voice

> **Status:** Architecture baseline plus envisioned capabilities. Sources: [architecture handover §§5.3 and 10](../architecture/aura-ai-architecture-handover.md#53-client-applications) and catalogue groups [`VOI`](./aura-ai-envisioned-feature-catalogue.md#14-voice-and-room-aware-interaction), [`AUI`](./aura-ai-envisioned-feature-catalogue.md#15-aura-web-application), and [`WAL`](./aura-ai-envisioned-feature-catalogue.md#16-aura-wall-application).

## Client boundaries

- **Aura Web** is the primary responsive PWA for conversation, agent and persona management, memory, knowledge, automation, integrations, approvals, artifacts, privacy, audit, and system controls.
- **Aura Wall** is a restricted kiosk experience for room-aware interaction, voice state, timers, reminders, notifications, selected home state, and limited history and settings.
- **Voice satellites** are thin deployable clients for local wake-word detection, voice activity detection, capture and streaming, playback, room/device identity, reconnect behavior, and diagnostics.
- **Observatory Web** is a separate operational and evaluation application and does not import Aura feature implementations.

The frontend baseline is an Angular Nx workspace. Application shells compose routing, layout, navigation, and global providers. Feature behavior belongs in libraries; applications do not import from one another. Shared UI stays generic, and service-specific clients are generated from independent OpenAPI contracts.

## Envisioned voice flow

```text
local wake word → voice activity detection → bounded audio stream
→ speech-to-text provider → Aura run or deterministic intent path
→ text-to-speech provider → approved device or room playback
```

Every satellite has authenticated device and room identity. Requests carry channel and room context; output normally returns to the originating device unless policy permits another destination. Consequential voice actions require confirmation. Recording, transcript, telemetry, and deletion behavior follow explicit privacy policy.

Exact wake-word, speech-to-text, text-to-speech, streaming protocol, and wall-wrapper choices remain [open decisions](./open-decisions.md). Whisper- and Piper-compatible services are examples, not settled providers.

## Related working summaries

[System context](../architecture/system-context.md) · [Module boundaries](../architecture/module-boundaries.md) · [Tools, automation, and integrations](./tools-automation-and-integrations.md) · [Open decisions](./open-decisions.md)
