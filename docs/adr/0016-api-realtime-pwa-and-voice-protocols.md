# ADR-0016: API/realtime, generated clients, PWA, and voice protocols

- **Status:** Accepted
- **Date:** 2026-10-04
- **Scope:** Core API, Aura Web, Aura Wall, voice satellites, browser audio, and integration clients

## Decision

Use REST with OpenAPI 3.1 for HTTP APIs. Generate transport-only Angular and Python clients from service-owned OpenAPI descriptions; domain logic remains in Core/Observatory use cases and frontend feature libraries. Use JSON Schema 2020-12 for event, tool, manifest, and structured payload validation. Use SSE for text and run-state updates. Cancellation, approval, and retry are HTTP commands.

Aura Web and Aura Wall begin as Angular PWAs using Workbox 7. Workbox caches public/static shell assets only by default. Authenticated responses, transcripts, attachments, and drafts are excluded until a policy defines principal binding, required encryption, expiry/deletion, logout and account-switch clearing, and kiosk behavior. Aura Wall is a restricted browser-kiosk experience; this ADR does not assume a native wrapper.

Voice satellites use Python with openWakeWord, Silero VAD, faster-whisper, and an isolated Piper-compatible provider. Satellite audio uses Protobuf/gRPC streaming. Browser audio uses a WebRTC gateway. Voice providers remain adapters; Piper compatibility is isolated pending licensing review. Fetched, parsed, or extension-provided content is untrusted evidence, never policy or instruction authority. Fetchers have no ambient credentials; they enforce allowed schemes, destination and redirect revalidation, private/link-local/loopback blocking, and bounded size, type, time, and resource use. Parser sandboxes receive no unnecessary network or secret access.

## Consequences

HTTP commands are inspectable and retryable; SSE provides one-way server-originated updates without making the browser a transport authority. Generated clients reduce drift while keeping domain behavior out of generated code. gRPC/WebRTC address the distinct satellite and browser audio constraints.

## Open decisions retained

Only exact voice wake-word/STT/TTS models, voice artifacts and configuration, voice policy, and voice protocol payload details remain future decisions. Notification channels remain open only beyond the initial Web Push and Home Assistant channels. API resources, non-voice event schemas, and the browser-kiosk Wall form follow the selected protocol and client boundaries; their concrete implementation details do not reopen this ADR.
