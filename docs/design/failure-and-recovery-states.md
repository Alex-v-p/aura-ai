# Aura Web failure and recovery states

Failure UX is part of the product's trust model. A failure message leads with
calm plain language, identifies what was affected, preserves user-authored
work and partial results when possible, explains the impact, and offers the
safest next action. Color is supplemental; state meaning is also conveyed by
text, iconography, structure, or announcement.

Technical detail is redacted by default. Do not expose secrets, tokens,
private request payloads, hidden reasoning, stack traces, or raw provider
diagnostics. If authorized support detail is useful, place a sanitized summary
behind an explicit disclosure and avoid making it the primary recovery path.

## State matrix

| Situation | User-facing explanation | Preserve | Recovery and accessibility |
| --- | --- | --- | --- |
| Initial loading | “Loading your conversations” or the specific surface | Route context and any draft | Structural placeholder; announce status without moving focus |
| Empty collection | Explain what belongs here and why it is empty | Navigation and preferences | One clear next action; do not present emptiness as an error |
| Field validation | Identify the field and state what to change | All valid fields and the draft | Inline text tied to the field, summary for multi-field forms, focus only when helpful |
| Offline | “You’re offline. Your draft is still here.” | Draft and unsent attachments where policy allows | Disable only network-dependent actions; offer retry when online |
| Reconnecting | “Connection lost; trying again” with attempt context | Draft, transcript, partial content | Live status; explicit retry/cancel; no rapid flashing |
| Interrupted streaming | Explain that generation stopped before completion | Partial assistant output and user prompt | Keep partial result visibly labeled; offer resume/retry/new prompt |
| Permission denied | Name the action or resource, not hidden policy detail | Draft and current location | Explain required access or contact path; do not offer an impossible retry loop |
| Approval required | Describe the consequential action and its scope | Proposed action and draft | Present approve, reject, and inspect options; do not imply approval happened |
| Upload rejected | Identify the file and supported limitation | Other files and composer text | State supported type/size or permission reason; replace/remove just the affected file |
| Tool failure | Name the user-visible task that could not complete | Tool input and any safe partial result | Explain whether retry may repeat side effects; offer retry or alternate path |
| Provider unavailable | State that the selected provider is unavailable | Prompt, partial content, selected context | Offer safe provider choice or retry; do not silently switch policy-sensitive providers |
| Timeout | Explain that the operation took too long | Draft, progress, and safe partial output | Offer retry, cancel, or inspect; indicate whether retry may duplicate work |
| Partial result | Identify what completed and what remains | Completed content and context | Label completeness; allow save/copy; offer bounded continuation |
| Canceled | Confirm that the user canceled and whether anything completed | Draft and completed partial result | Return to an editable state; avoid presenting cancellation as failure |
| Unknown failure | Say the task could not be completed and give a support-safe reference | Draft and available context | Retry only when safe; provide a human-readable support path without raw diagnostics |

## Recovery rules

1. Keep the user's text visible. Never clear a composer because a request was
   rejected, timed out, or disconnected.
2. Preserve a partial answer as partial. Do not merge regenerated content into
   it invisibly or imply that an interrupted answer is complete.
3. Make retry semantics explicit where a tool or integration could have side
   effects. Prefer inspect, resume, or cancel when repeating is ambiguous.
4. Keep the recovery action near the affected surface and make it keyboard and
   screen-reader reachable. A toast alone is insufficient for a blocking error.
5. Avoid infinite reconnect or retry loops. Show current status and give the
   user control to stop, change context, or continue later.
6. Announce dynamic state through an appropriate status/live region without
   stealing focus. Move focus only when the user cannot reasonably recover
   without it, such as a dialog requiring a decision.
7. Use separate status roles for success, warning, danger, and information.
   Never use the dusty-rose accent as an error shorthand.
8. Sanitize support references and diagnostics. The UI may provide a stable
   human-readable incident reference, but must not expose credentials,
   internal URLs, personal payloads, or model-private reasoning.

## Preservation and identity invariant

Drafts, attachments, tool inputs, and partial results remain bound to their
originating authenticated principal and to the applicable household,
workspace, agent, conversation, and policy context. A context change retains
the material with its origin, or requires an explicitly authorized transfer
and fresh policy validation; it must never silently broaden visibility or
reuse authorization. “Your draft is still here” describes continuity in the
current authorized context, not a promise of durable storage.

Durable or on-device draft and attachment storage requires a separately
approved retention, encryption, deletion, logout-clearing, and
identity-isolation policy. This design guide does not choose those policies or
imply that offline material survives logout, device cleanup, account changes, or
retention expiry.

## Sensitive and consequential workflows

Permission, approval, deletion, automation, and external side-effect states
must distinguish proposed, authorized, attempted, completed, rejected, and
unknown outcomes in plain language. An unknown outcome is not success and is
not permission to repeat automatically. When an action could affect another
person, device, service, or durable record, show scope and consequence before
the final confirmation.

## Copy patterns

Prefer: “We couldn’t send this message because the connection was lost. Your
draft is still here. Retry when you’re ready.”

Avoid: “Error 502,” “Something went wrong” with no action, or a raw exception
that forces the user to interpret system internals. A diagnostic disclosure
can supplement the plain-language explanation only when it is sanitized and
appropriate to the user's authorization.
