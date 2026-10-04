# Clients and voice

> **Status:** Architecture baseline plus envisioned capabilities. Sources: [architecture handover §§5.3 and 10](../architecture/aura-ai-architecture-handover.md#53-client-applications) and catalogue groups [`VOI`](./aura-ai-envisioned-feature-catalogue.md#14-voice-and-room-aware-interaction), [`AUI`](./aura-ai-envisioned-feature-catalogue.md#15-aura-web-application), and [`WAL`](./aura-ai-envisioned-feature-catalogue.md#16-aura-wall-application).

## Client boundaries

- **Aura Web** is the primary responsive PWA for conversation, agent and persona management, memory, knowledge, automation, integrations, approvals, artifacts, privacy, audit, and system controls.
- **Aura Wall** is a restricted kiosk experience for room-aware interaction, voice state, timers, reminders, notifications, selected home state, and limited history and settings.
- **Voice satellites** are thin deployable clients for local wake-word detection, voice activity detection, capture and streaming, playback, room/device identity, reconnect behavior, and diagnostics.
- **Observatory Web** is a separate operational and evaluation application and does not import Aura feature implementations.

The frontend baseline is Node 24 LTS with Angular 22, Nx 23, pnpm 12, strict TypeScript, Tailwind 4, Angular CDK, Signals and RxJS. Application shells compose routing, layout, navigation, and global providers. Feature behavior belongs in libraries; applications do not import from one another. Shared UI stays generic, and service-specific clients are generated from independent OpenAPI 3.1 contracts. Workbox 7 caches public/static shell assets only by default; authenticated responses, transcripts, attachments, and drafts require a policy defining principal binding, encryption, expiry/deletion, logout/account-switch clearing, and kiosk behavior before offline persistence. Marked + DOMPurify, Shiki, ECharts, Storybook, axe, Vitest, and Playwright are approved support tools. See [technology stack](../architecture/technology-stack.md), [ADR-0012](../adr/0012-python-angular-tailwind-toolchain.md), and [ADR-0016](../adr/0016-api-realtime-pwa-and-voice-protocols.md).

Browser authentication uses the same-origin BFF through Authentik's OIDC Authorization Code flow. The BFF validates issuer, audience, state, and nonce, keeps tokens server-side, rotates sessions after authentication or privilege changes, and uses an explicit SameSite cookie policy plus origin-aware anti-CSRF validation for every state-changing HTTP command. Bearer credentials never appear in browser storage, URLs, or SSE parameters.

## Envisioned voice flow

```text
local wake word → voice activity detection → bounded audio stream
→ speech-to-text provider → Aura run or deterministic intent path
→ text-to-speech provider → approved device or room playback
```

Every satellite has authenticated device and room identity. Requests carry channel and room context; output normally returns to the originating device unless policy permits another destination. Consequential voice actions require confirmation. Recording, transcript, telemetry, and deletion behavior follow explicit privacy policy.

Voice satellites use Python with openWakeWord, Silero VAD, faster-whisper, and an isolated Piper-compatible provider. Satellite streaming uses Protobuf/gRPC; browser audio uses a WebRTC gateway. These are protocol and boundary decisions, not permission to add runtime code in this documentation change. Fetched, parsed, and extension-provided content is untrusted evidence, never policy or instruction authority. Exact provider models, voice artifacts/configuration, voice policies, and streaming payloads remain [open decisions](./open-decisions.md). Aura Wall remains a restricted browser-kiosk PWA under this decision; Piper-compatible licensing requires review.

## Related working summaries

[System context](../architecture/system-context.md) · [Module boundaries](../architecture/module-boundaries.md) · [Tools, automation, and integrations](./tools-automation-and-integrations.md) · [Open decisions](./open-decisions.md)
