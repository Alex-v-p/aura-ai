# Governed research, generated artifacts, and Task Center

> **Status:** Approved architecture and delivery design
>
> **Decision:** [ADR-0018](../adr/0018-governed-web-research-providers.md)
>
> **Scope:** Implementation contract for `AURA-0141` through `AURA-0148`.
> This document does not itself create product code, wire contracts,
> migrations, deployables, or evaluation baselines.

## Purpose and invariants

Aura will provide owner-private web research from conversation and a dedicated
workspace, Core-owned generated text artifacts, and a federated Task Center.
The implementation must preserve these invariants:

- PostgreSQL is authoritative for research, artifact metadata and deletion
  truth, and each source domain's task lifecycle.
- Task Center is a rebuildable read projection, never a generic workflow
  engine or cross-domain table reader.
- Search and fetch providers are replaceable, credential-free with respect to
  Aura, and untrusted. Their output is evidence only.
- Every research request creates one durable task before provider work begins.
- All work is owner-scoped, bounded, cancellable, idempotent, and inspectable.
- Full fetched pages are transient. Only source metadata and bounded cited
  excerpts survive settlement.
- Generated artifacts are separate from source documents, memory, evaluation
  artifacts, and Observatory run evidence.
- Core operation and research completion do not depend on Observatory.
- Private content never enters metadata-only events, ordinary telemetry, URLs,
  browser persistence, or service-worker caches.

## Product behavior

### Research admission and lifecycle

Research may start through `web.research@1` in a conversation or from the
dedicated `/research` workspace. Admission performs owner authorization,
anti-CSRF for HTTP commands, idempotency, preset and override validation,
seed-URL validation, capability checks, and quota checks in one application
use case. It commits the research record, task, initial checkpoint, and outbox
event atomically before dispatch.

The internal research stages are `queued`, `planning`, `searching`, `fetching`,
`validating_evidence`, `synthesizing`, `persisting`, and `draining`. Draining is
nonterminal and means new provider dispatch is disabled while admitted calls
settle; research never pauses. The closed terminal outcome enum is
`completed`, `partial`, `budget_exhausted`, `failed`, `canceled`, and
`interrupted`. `provider_disabled` and `provider_unavailable` are bounded
failure classes, not outcomes. A task stores its exact admitted mode, counters,
deadline, capability snapshot, origin, policy revision, one-time extension
state, terminal outcome, and optional failure class.

Checkpoints occur after plan acceptance, every search result batch, every
settled fetch, evidence validation, synthesis, and artifact persistence. A
recovered worker reloads authoritative counters and immutable evidence IDs;
it never repeats a settled provider operation unless the operation is
explicitly retryable and its attempt budget remains. Provider request IDs and
task/version checks make late and duplicate results harmless.

Cancellation marks intent under a row lock, stops new dispatch, requests
provider cancellation, and waits for admitted attempts to settle or time out.
The final state records whether safe partial evidence or an artifact exists.
Cancellation never claims that a remote request was reversed. Deadline and
budget checks occur before and after every external or model operation.

Admission-disabled requests fail before task creation. A graceful static or
browser disable enters `draining`; Core then settles `completed` when the
existing evidence meets the sufficiency rule, `partial` when useful evidence
remains incomplete, or `failed` with `provider_disabled` when no usable result
can be produced. Provider outage follows the same rule with
`provider_unavailable`. An immediate operator stop requests cancellation of
admitted provider work and settles `interrupted` after bounded reconciliation.
Owner cancellation alone settles `canceled`; task-envelope exhaustion alone
settles `budget_exhausted`.

### Presets and extension

The normative maximums and client-subscription behavior are defined in ADR-0018. Owner
settings can only lower those envelopes. Per-request overrides are explicit
and bounded; omitted values use the current saved preset defaults. All retries,
fallbacks, redirects, search pages, browser fallbacks, and model repair calls
consume the admitted counters.

An active task exposes extension capability only when no extension was
previously accepted, the task has not entered a terminal or persistence stage,
and the selected preset envelope has unused capacity. The owner submits the
desired cumulative limits with the expected task version. The command is
idempotent and records one accepted or rejected decision. Extension cannot
change owner, origin, mode, policy revision, provider permissions, or exceed
the original mode envelope.

### Planning, search, retrieval, and evidence

The planner produces a closed research plan containing bounded subquestions,
search phrases, optional domain narrowing, expected evidence types, and the
conditions under which browser retrieval is justified. It cannot emit tools,
URLs, budgets, grants, or instructions outside its schema. Core validates and
clips it to the admitted policy.

SearXNG results enter a candidate set with normalized URL, result position,
bounded display metadata, and discovery provenance. Core canonicalizes URLs,
removes fragments and known tracking parameters, applies domain policy, and
deduplicates candidates without treating canonicalization as authorization.
Owner-supplied seed URLs use the same checks and are marked as owner-supplied.

Static retrieval is attempted first unless the accepted plan records a
JavaScript requirement. Browser retrieval is admitted only for that recorded
need or an explicit static `inadequate_extraction` outcome. Network errors,
unsupported types, policy rejection, timeouts, empty content, parser failure,
and resource-limit rejection remain distinct bounded outcomes. The
coordinator may choose another candidate but cannot silently raise limits.

Each accepted source receives an opaque task-scoped evidence ID. Evidence
stores normalized URL, owner-authorized label, publisher when available,
retrieval time, declared and detected type, final redirect target, extraction
method, freshness metadata, content digest, and only bounded excerpts used by
accepted claims. Evidence text is delimited and labeled untrusted before model
use. It cannot be interpolated into system, governance, tool, or policy
instructions.

### Synthesis and citations

Synthesis receives only the accepted plan and admitted evidence bundle. It
returns a closed result with a compact answer, report body, claim-to-evidence
markers, limitations, and evidence-sufficiency assessment. Core, not the
model, validates markers against the current task, removes duplicates,
rejects invented or stale evidence, verifies that every externally checkable
material claim is supported, and constructs public citations.

A public web citation identifies the source, uses an owner-authorized label,
contains the normalized public HTTPS URL and retrieval time, and may include a
bounded locator. It never contains a query, excerpt, object key, provider
payload, browser state, or model-supplied evidence ID. If a material claim
lacks valid support, Core requests a repair only while the model-call and time
budgets remain; otherwise the claim is removed, qualified, or reported as an
unsupported limitation. Conflicting sources are represented rather than
silently collapsed.

Agent-led work may stop early only under the sufficiency rule in ADR-0018. A
completed result has supported main claims and reports material uncertainty.
A usable but incomplete result is `partial`; exhausted work is
`budget_exhausted`. Neither state is presented as full completion.

### Conversation and notification delivery

Conversation admission pins the originating conversation and branch. The tool
attempt returns after durable admission, always within the existing 30-second
attempt ceiling. Its ordinary tools-disabled synthesis persists one stable
research acknowledgement/activity item containing only the opaque task
reference and admission state. It never waits for research or persists a
terminal research answer.

Quick keeps the initiating client's task subscription active through terminal
settlement while connected. Auto keeps that subscription in foreground for 30
seconds, then changes the same activity item to background presentation if the
task is unfinished. Deep and Agent-led use background presentation immediately.
This attachment state is client-only: it creates no new run state, wait state,
task, message, or terminal writer. Reconnect uses authoritative task REST state
and the same opaque reference.

Exactly one durable delivery record is keyed by research task and destination.
For every conversational mode it is the sole terminal writer. Its `append`
substate claims a unique `(task, conversation, terminal_result)` idempotency
key, appends one compact assistant message to the originating branch, and then
settles independently of its `notification` substate. The notification uses a
second unique key. Duplicate events, Auto's 30-second UI boundary, reconnect,
append timeout, and notification retry therefore cannot duplicate the terminal
message. A known append failure retries append only; an unknown append outcome
is reconciled by the unique key before retry; notification failure never rolls
back or repeats the append. If append ultimately fails, Task Center and the
notification remain recovery paths and the settled task is unchanged.

Every terminal message names the authoritative outcome. `partial` and
`budget_exhausted` show completed evidence, missing coverage, source counts,
and any partial artifact; `failed` shows the bounded failure class;
`interrupted` explains the operator/provider interruption; and `canceled`
explains that admitted network work may already have occurred. Retry always
creates a new task. Dedicated-page research has no conversation delivery
record and creates one idempotent notification.

### Conversational tool

`web.research@1` is a private, owner-bound, idempotent admission mutation requiring the
exact `research:run` grant. Its closed input contains the bounded research
question, selected mode, optional lowered limits, and optional public HTTPS
seed URLs or domain narrowing. It cannot select an owner, conversation,
branch, provider, permission, hard cap, completion destination, or artifact
scope. Core binds those values from the admitted run. The initiating owner
message is the explicit request, so the tool does not add a second confirmation
step; policy denial, ambiguity, or missing grant creates no task. One admitted
tool call creates exactly one durable task through an idempotency key derived
from the run and call identity. Its closed result contains only an opaque task
reference, admitted mode, and admission status. It must settle within the
existing maximum 30-second tool attempt and then use the ordinary tools-disabled
synthesis. No research result, source, URL, excerpt, citation, or artifact
content returns through the tool attempt. Custom agents remain deny-by-default,
and a grant is added only through a new immutable tool-policy and agent revision.

### Research and evidence deletion

Research and its retained source metadata/excerpts use manual-only retention.
The owner may delete a terminal research record through a same-origin
CSRF-protected command carrying idempotency key, expected version, and an
explicit `deleteLinkedArtifacts` boolean. Active research must first be
canceled and terminal; the delete command never doubles as cancellation.

Acceptance under a row lock changes the separate content lifecycle from `active` or
`deletion_failed` to `deletion_pending` and immediately removes the question,
plan, source list, URLs, excerpts, citation detail, and report linkage from all
owner reads and model access. Task Center retains only a content-free deleted
source presentation. Cleanup deletes retained evidence and private research
content. If linked-artifact deletion was explicitly selected, it submits
idempotent deletion commands to each linked artifact and records their opaque
cleanup states; otherwise artifacts remain independently authorized and
visible. A known cleanup failure settles `deletion_failed`; an ambiguous store
or command result settles `cleanup_unknown` until reconciliation proves absent
or retryable. `deleted` retains only opaque identity, versions, timestamps,
deletion outcome, and content-free audit.

Content deletion never rewrites the historical research terminal outcome or
Task Center grouping; owner reads show that outcome beside a deleted-content
tombstone.

Repeating the original deletion idempotency key returns its existing lifecycle
without starting more cleanup. A `deletion_failed` record exposes a separate
Retry cleanup command with expected version and a new idempotency key;
`cleanup_unknown` exposes reconciliation only until the outcome is known.

Conversation messages are immutable. Existing citation and task references are
not rewritten or expanded after deletion; authorization resolution returns a
content-free “Research source deleted” tombstone with no label, URL, excerpt,
or artifact relation. The prose already present in the assistant message
remains until separately authorized conversation deletion. A duplicate terminal
delivery cannot restore deleted research, evidence, citations, or artifacts.

Deletion tests cover cross-owner denial, CSRF, idempotency, stale versions,
delete-versus-delivery and delete-versus-retry races, immediate read
unavailability, partial linked-artifact cleanup, missing objects, known and
unknown cleanup outcomes, tombstone privacy, citation resolution, and recovery
after worker restart.

## Generated artifact model

### Ownership and revisions

The artifact aggregate is Core-owned and owner-private in v1. It contains an
opaque artifact ID, owner ID, kind, title, lifecycle, current revision, created
and updated timestamps, and optional opaque workspace, conversation, run, and
task links. These links do not transfer ownership or authorization.

Each revision is immutable and contains its revision number, media type,
SHA-256 digest, byte count, object reference, creator component, creation time,
and lineage to a preceding revision when present. Supported types are
`text/markdown; charset=utf-8`, `text/plain; charset=utf-8`, and
`application/json`. JSON is validated and canonically serialized before
storage. A failed object write does not publish a revision; an ambiguous write
is reconciled by digest before retry or cleanup.

Research creates a Markdown report revision and a JSON source manifest through
the artifacts public application API in the same durable workflow. The report
links citations by source ID. The manifest is not a dump of fetched content;
it contains authorized source metadata and citation relationships only.

### Read, preview, download, and deletion

List and detail are owner-authorized and return metadata only. Preview streams
bounded supported text through server-side type validation and client-side
sanitized rendering; Markdown never enables raw HTML, scripts, remote embeds,
or executable links. JSON preview is inert text or a non-executable structured
viewer. Download reauthorizes on every request, uses `Cache-Control: no-store`,
content-disposition attachment, a sanitized filename, and nosniff headers.

Deletion uses expected version plus idempotency. The aggregate first becomes
unavailable for preview/download and records deletion intent. Object deletion
is retried through durable work. Missing objects count as successfully absent;
admitted work is `deletion_pending`, known retryable failure is
`deletion_failed`, and a timeout or ambiguous provider outcome is
`cleanup_unknown` until reconciliation. Final deletion retains content-free identity, lineage,
digest, byte count, timestamps, and audit truth but no title or content. A
deleted artifact is never recreated by a duplicate research-completion event.

Repeating the original deletion key returns current state. Retry cleanup is a
separate idempotent expected-version command offered only for
`deletion_failed`; `cleanup_unknown` must reconcile before retry becomes
available.

No automatic expiry or background retention purge exists in v1. Public links,
sharing, uploads, binary content, archives, PDFs, images, code execution, and
cross-owner access are excluded.

## Federated Task Center model

### Source authority and projection

The Task Center projection key is `(source_kind, source_id)`. Included source
kinds are research task, workflow instance, briefing occurrence, document
processing, and memory reindexing. Reminders, approvals, calendar events,
ordinary conversation runs, notifications, and provider calls are excluded.

Each source module owns lifecycle, progress calculation, failure detail,
authorization, cancellation, retry, and specialized recovery. After commit it
publishes only a closed invalidation whose payload is exactly source kind,
opaque source ID, and source version. The standard envelope carries only event
ID, occurred-at, correlation, and causation. It contains no owner, state,
progress, capability, title, status, outcome, or private value.

The live projection consumer holds the narrow internal
`task_projection:read` capability and calls the source module's public
`query_task_projection_snapshots(TaskProjectionSnapshotQueryV1)` query with an
exact source ID and minimum version. That query authorizes only the execution
projection service and returns a closed
`TaskProjectionSnapshotV1` with owner scope, source identity/version, normalized
state inputs, authoritative source outcome/status, bounded progress/stage,
capability flags, timestamps, failure class, and route key. The projection
stores that DTO; no event payload is used as snapshot truth. Owner-facing list
and detail queries reauthorize against its owner scope. Human titles and
private payloads are resolved from the source only through a separately
owner-authorized bounded batch display query and are never copied into the
projection or event.

At-least-once and out-of-order invalidations are handled by source version.
Equal or older versions do not regress the row; a newer invalidation always
queries the source snapshot, whose returned version must be at least the
requested minimum. A missing, older, or unavailable snapshot marks the row
`unknown` and schedules bounded reconciliation. Upsert is idempotent on source
identity/version. Deletion returns a source-owned tombstone snapshot; source
unavailability yields unknown rather than a fabricated terminal result.

Projection rebuild invokes the same public
`query_task_projection_snapshots(TaskProjectionSnapshotQueryV1)` query with a
mutually exclusive stable cursor and page size under the same internal
capability. It returns `TaskProjectionSnapshotV1` items and the next cursor,
never ORM rows. Rebuild
upserts by source identity/version and tombstones rows absent after a complete
source scan. It is safe to restart and does not publish source commands. The
capability is usable only by the composed execution projection service, is not
delegable to agents or providers, and is audited without owner or content data.

### Status and commands

The normalized presentation state is:

| Task Center state | Source meaning |
|---|---|
| `queued` | Admitted or scheduled but not executing |
| `running` | Active work, including research planning, draining, and persistence |
| `waiting` | Deliberately paused for an external condition or owner decision |
| `completed` | Source confirms successful terminal completion |
| `failed` | Source confirms failed, partial, budget-exhausted, or interrupted terminal work; every row and detail preserves and labels the authoritative source outcome |
| `canceled` | Source confirms terminal cancellation |
| `unknown` | Projection gap, stale source, or ambiguous command/result outcome |

Task detail exposes source status and recovery copy in addition to the
normalized state. A source advertises `canCancel`, `canRetry`, and optional
specialized deep-link capability from its current authoritative state. The
generic command endpoint reauthorizes, reloads source detail, verifies the
advertised capability and expected source version, and routes to the source's
public command. Retry always creates or identifies a source-owned retry and
never resets the projection row in place. Unknown command outcomes are
reconciled before another mutation is offered.

## Aura Web information architecture and interaction contract

### Navigation and routes

Research and Artifacts are global destinations in the existing **Knowledge**
sidebar group. Tasks is a global destination in **Activity**, after Approvals
and Notifications. No separate research settings route is added: owner-editable
preset defaults appear in an **Advanced** disclosure on `/research`. These
destinations use the existing grouped sidebar, rail, and mobile drawer; they do
not create an Observatory link or a new shell hierarchy.

| Route | Page regions and primary actions | Wide | Tablet | Mobile |
|---|---|---|---|---|
| `/research` | Heading and privacy summary; question composer; Quick/Auto/Deep/Agent-led selector; Start research; Advanced defaults, seed URLs, domain narrowing, and lowered budgets; current task activity; recent research | Composer and current activity in a readable main column with recent work beside it | Single main column; Advanced and recent filters in overlays/disclosures | Stacked full-width composer, mode controls, activity, then recent work; sticky Start action only while form is valid |
| `/tasks` | Kind/status/time filters; task rows; reconnect/stale status; pagination | Filter bar plus list; selected row may open route-backed detail drawer | Compact list with filter overlay; detail overlays | Filters in full-screen sheet; row opens full-screen detail |
| `/tasks/:id` | Authoritative source outcome, normalized group, progress/stage, timestamps, artifacts, failure/recovery, source deep link, capability-gated Cancel/Retry, and terminal research Delete evidence action | Contextual drawer over `/tasks` when opened from list, otherwise full page | Overlay detail | Full-screen detail with actions after content |
| `/artifacts` | Type/time filters; artifact rows with lineage and deletion state | Filter bar plus list; selected row may open detail drawer | Compact list and filter overlay | Stacked list; row opens full-screen detail |
| `/artifacts/:id` | Metadata, lineage, revisions, cited-source manifest, inert preview, Download, Delete | Route-backed drawer with large preview region, otherwise full page | Overlay detail with preview below metadata | Full-screen detail; preview then actions; deletion confirmation is a focused dialog |

Every task row shows both the normalized grouping and the authoritative source
outcome. Research `partial`, `budget_exhausted`, and `interrupted` remain in the
normalized **Failed** filter but are visibly labeled in the row, not hidden in
detail. Rows show bounded progress or “Progress unavailable,” current stage,
updated time, source kind, and capability state without exposing query or URL.

### Research composer and transcript activity

The research composer requires a question and mode. Advanced starts collapsed
and contains only owner preset defaults, optional seed URLs/domain narrowing,
and values that lower the selected envelope. Validation preserves all valid
inputs and explains the exact rejected field. Starting research replaces no
draft; after admission it clears the submitted fields and creates one stable
task activity item keyed by the opaque task reference.

In a conversation, the tools-disabled acknowledgement renders that same stable
activity item. SSE invalidation plus REST reconciliation transitions its
presentation through admitted, running, draining, background, and terminal
states without rewriting the persisted acknowledgement or creating assistant
messages. Quick remains foreground while subscribed. Auto changes the existing
item to “Continuing in the background” at 30 seconds if unfinished. On terminal
delivery, the item links to the one appended cited result; reconnect never
creates another item or append.

### Focus, announcements, and responsive interaction

| Interaction | Focus behavior | Live-region behavior |
|---|---|---|
| Research admission | Keep focus on Start until response, then move to the new task activity heading | Polite: “Research started”; do not announce every progress tick |
| Stage/progress update | Do not move focus | Polite, throttled announcements only when stage changes; percentages are visual text |
| Auto background transition | Keep focus and current scroll position | Polite: “Research is continuing in the background” |
| Terminal result | Keep the user's current focus; expose a nearby “Open result” action | Polite for completed/partial/canceled; assertive only for a blocking failure on the active item |
| Open route-backed drawer | Move focus to drawer heading; trap within drawer | Announce heading and source outcome once |
| Close drawer | Return focus to the exact invoking row/control | No success announcement |
| Cancel or retry confirmation | Focus dialog heading, then safest action; return to source action on close | Announce accepted command and later authoritative outcome separately |
| Delete artifact/research | Focus consequence heading; after acceptance move to deletion status heading, not a vanished row | Polite: “Deletion started”; assertive for cleanup failure/unknown only while detail is active |
| Mobile filter sheet | Trap focus and return it to Filters | Announce applied filter count |

Desktop uses the existing approximately 280px labeled sidebar and route-backed
detail drawers without a permanent third pane. Tablet may use the 72–80px rail;
filters and details overlay instead of compressing content. Mobile uses the
grouped navigation drawer and full-screen route detail. All controls retain
44px preferred targets, keyboard order follows visual order, Escape closes the
top overlay, reduced motion removes progress animation, 200% zoom reflows
without horizontal page scrolling, and status always has text/icon structure
in addition to color.

### Loading, failure, deletion, and draft states

| State | Visible behavior and recovery |
|---|---|
| Loading / empty | Structural placeholders preserve route context; empty pages explain the capability and offer Start research or the relevant source action |
| Offline / reconnecting | Preserve the in-memory composer and visible task snapshot; disable only network actions; announce reconnect and reconcile REST before enabling commands |
| Projection stale / unknown | Show “Status needs refresh,” last authoritative update, and Refresh/Open source; never offer blind retry |
| Research partial | Show supported summary, source count, missing coverage, partial artifact, and “Start new research”; never label complete |
| Research budget exhausted | Show the exact exhausted envelope, retained sources, partial artifact, and whether the one-time extension was already used; retry creates a new task |
| Research interrupted / provider disabled | Show that Aura stopped because research capability was interrupted or disabled, what evidence survived, and a retry only when admission is currently available |
| Artifact `deletion_pending` | Hide preview/download immediately; retain status page with “Deletion in progress”; disable repeat Delete |
| Artifact `deletion_failed` | Keep bytes unavailable; explain cleanup failed and offer idempotent Retry cleanup plus sanitized support reference |
| Artifact `cleanup_unknown` | Keep bytes unavailable; explain outcome is being reconciled; offer Refresh, never re-upload or blind retry |
| Deleted research/evidence | Resolve task/citations to content-free tombstones; linked artifacts show their independent state; conversation prose remains |
| Permission / authentication | Preserve memory-only draft only for the still-authenticated same owner context; name required access and do not loop retry |

Research drafts and Advanced values are held only in owner-scoped in-memory
state for the current authenticated page session. They are never written to
localStorage, IndexedDB, service-worker caches, URLs, telemetry, or logs. A
recoverable request failure or route-backed drawer preserves them. Logout,
session loss, owner/household/workspace change, account switch, or explicit
Discard clears them synchronously; reload or browser closure may lose them and
the UI must not claim durable draft storage.

## Public contract intent for AURA-0141

The contract ticket must make additive changes and regenerate transport-only
clients. Exact OpenAPI names follow existing repository conventions, but the
following resource behavior is fixed:

- **Research:** create, owner settings, detail, evidence/source detail,
  cancellation, one-time extension, and explicit research/evidence deletion.
  Create accepts question, mode, lowered
  or bounded overrides, optional seed URLs/domain narrowing, conversation
  origin when invoked internally, and idempotency. Detail returns admitted
  budgets, counters, stage, outcome, citations, artifact links, capability
  flags, deletion/cleanup state, and version. Original deletion is idempotent;
  known failure has a separate cleanup retry and unknown outcome has a
  reconciliation command.
- **Artifacts:** owner-authorized list, detail, revision list, sanitized
  preview/content stream, download, explicit deletion, and idempotent cleanup
  retry/reconciliation. Artifact creation and revision append are internal Core
  application APIs, not public upload endpoints.
- **Tasks:** owner-authorized list and detail with kind/status/time filters;
  normalized grouping plus mandatory authoritative source status/outcome in
  every row; metadata-only SSE invalidation; capability-gated cancel and retry
  commands.
- **Conversation/run additions:** optional opaque task, research-origin,
  stable research-activity, artifact, and validated web-citation references.
  The research tool result is admission-only and carries only opaque task
  reference, admitted mode, and admission state. Existing clients may omit or
  ignore additive properties.
- **Events:** versioned metadata-only research-task and artifact-lifecycle
  events plus task-source identifier/version invalidations. Task invalidations
  have a payload containing only source kind/ID/version; the standard envelope
  adds only event ID, timestamp, correlation, and causation. Live and rebuild
  paths obtain the same source-owned
  `TaskProjectionSnapshotV1` through the internal projection capability.
  Private text, URLs, titles, excerpts, owner identity, object keys, digests,
  source state, progress, capabilities, and provider payloads are prohibited
  from the invalidation.

Mutations require same-origin anti-CSRF, idempotency, and expected versions
where state can race. SSE is invalidation only; authorized REST snapshots are
authoritative after reconnect. Private request values must be request bodies,
never query-string state.

## Security and privacy threat model

| Threat | Required mitigation | Verification evidence |
|---|---|---|
| SSRF and metadata access | HTTPS/443 only; controlled DNS; block prohibited ranges and mixed answers in Core and fetcher | Unit/property cases for IPv4/IPv6 literals, encodings, aliases, mixed answers, and metadata names |
| DNS rebinding / TOCTOU | Validate all resolved addresses immediately before connection; connect only to validated address while preserving TLS hostname | Resolver/connect integration test that changes answers between checks |
| Redirect abuse | Manual redirect handling and full scheme/host/DNS/address/port/policy validation at every hop; maximum five | Public-to-private, loop, userinfo, port, downgrade, and mixed-address redirect tests |
| Parser/type confusion | Declared/sniffed type agreement, closed type allowlist, whole-response and extracted-text limits, no archive/PDF/JSON parser in v1 | MIME mismatch, polyglot, malformed encoding, archive and decompression tests |
| Browser compromise | Separate no-secret service, non-root/read-only sandbox, seccomp/capability limits, ephemeral profile, disabled downloads/devices/persistence, narrow network | Container policy inspection and hostile-page escape/download/persistence tests |
| Credential discovery | No Aura/browser/cloud credentials or host mounts; reject URL userinfo; redact diagnostics; separate identities | Environment/filesystem/socket probes and secret-canary tests |
| Forged or replayed provider request | Core-signed short-lived closed envelope, public-key verification, request-ID replay rejection, and caller network allowlist | Invalid signature, expired envelope, replay, wrong audience, and duplicate request tests |
| Prompt injection | Delimit evidence; closed planner/synthesis schemas; deterministic policy, budget, URL, tool, citation, and transition checks | Pages instructing policy/tool/budget override cannot change execution |
| Content leakage | Owner authorization on every read; private fields excluded from events, telemetry, logs, URLs and caches | Cross-owner API tests plus telemetry/event/log/browser-persistence scans |
| Duplicate/out-of-order delivery | Stable IDs, source versions, idempotency keys, immutable revisions, delivery record uniqueness | Duplicate, reordered, restart, and version-gap tests |
| Tool/run lifecycle expansion | Admission-only tool settles within 30 seconds with opaque task reference; client subscription and worker task remain outside the run | Quick/Auto/Deep/Agent-led tests prove no waiting run state, second tool synthesis, or duplicate task |
| Cancellation races | Row locks/version checks, no dispatch after cancel intent, late result rejection, truthful settled state | Cancel-before/after-dispatch and simultaneous completion tests |
| Resource exhaustion | Per-service CPU/memory/PID/tmp/concurrency limits plus per-request/task byte/time/search/source/model/browser budgets | Oversize, slow-loris, redirect, decompression, high-DOM and concurrency tests |
| Artifact storage ambiguity | Digest reconciliation, unavailable-on-delete, durable deletion state, missing-object idempotency | Write timeout, delete timeout, missing object, collision and retry tests |
| Projection authority creep | Identifier/version-only invalidation; internal-capability source snapshot query shared by live/rebuild; source-owned commands; architecture rule against ORM/table imports | Event-privacy, capability-denial, import-boundary, live/rebuild-convergence, and source-adapter tests |
| Stale or invented citations | Task-scoped evidence IDs and deterministic citation validation | Cross-task, unknown, duplicate, stale, redirect and unsupported-claim tests |
| Malicious preview | Server type validation, sanitized/inert rendering, no raw HTML or remote embeds, `no-store`/nosniff | XSS, dangerous URL, HTML-in-Markdown and JSON payload tests |
| Research/evidence resurrection | Immediate unreadability, content-free tombstone, idempotent cleanup, citation tombstone, delivery/delete serialization | Delete-versus-delivery/retry, restart, linked-artifact, citation-resolution, and unknown-cleanup tests |

Security failures are fail-closed for new provider dispatch. They may yield a
truthful partial task when already validated evidence exists. No security
failure is repaired by weakening destination, sandbox, size, citation, or
authorization policy.

## Observability and evaluation contract

Every component below is a stable versioned contract. “Types” are closed
application/evaluation DTOs, not telemetry payloads. A span begins immediately
before the named active operation and ends after its bounded result; no span
covers queue residence, background waiting, client subscription, or rendering.

| Component | Closed input → output | Declared dependencies | Exact span | Required metrics | Replay |
|---|---|---|---|---|---|
| `aura.research.coordinator@1` | `ResearchCoordinateInputV1` → `ResearchCoordinateResultV1` | Core PostgreSQL, outbox | `research.coordinate` | `research_coordinate_outcome_count`, `research_coordinate_latency_ms`, `research_checkpoint_count`, `research_queue_lag_ms`, admitted-budget metrics and static-browser fallback metrics defined below | Deterministic state replay with synthetic provider/model fixtures |
| `aura.research.cancellation@1` | `ResearchCancelInputV1` → `ResearchCancelResultV1` | Core PostgreSQL, static/browser cancellation ports | `research.cancel_command` | `research_cancel_command_outcome_count`, `research_cancel_command_latency_ms`, `research_cancel_settlement_lag_ms` | Deterministic cancellation/race fixtures; live providers never replayed |
| `aura.research.planner@1` | `ResearchPlanInputV1` → `ResearchPlanResultV1` | model gateway | `research.plan` | `research_plan_outcome_count`, `research_plan_latency_ms` | Synthetic or explicitly owner-authorized controlled evaluation only |
| `aura.research.search@1` | `ResearchSearchInputV1` → `ResearchSearchResultV1` | SearXNG port | `research.search` | `research_search_outcome_count`, `research_search_latency_ms`, `research_search_candidate_count` | Versioned synthetic provider fixture; no live-web replay by default |
| `aura.research.static_fetch@1` | `StaticFetchInputV1` → `FetchEvidenceResultV1` | static fetcher port | `research.fetch_static` | `research_static_fetch_outcome_count`, `research_static_fetch_latency_ms`, `research_static_fetch_limit_rejection_count` | Versioned synthetic response fixture; production target replay disabled |
| `aura.research.browser_fetch@1` | `BrowserFetchInputV1` → `FetchEvidenceResultV1` | browser fetcher port | `research.fetch_browser` | `research_browser_fetch_outcome_count`, `research_browser_fetch_latency_ms`, `research_browser_fetch_limit_rejection_count` | Hostile-page fixture in isolated acceptance environment only |
| `aura.research.extraction@1` | `ExtractionInputV1` → `ExtractionResultV1` | Trafilatura adapter, content policy | `research.extract` | `research_extraction_outcome_count`, `research_extraction_latency_ms`, `research_extraction_rejected_count` | Versioned synthetic document fixture; production bytes never replayed |
| `aura.research.evidence_validation@1` | `EvidenceValidationInputV1` → `EvidenceValidationResultV1` | URL/destination and content policy | `research.evidence_validate` | `research_evidence_validation_outcome_count`, `research_evidence_validation_latency_ms`, `research_evidence_rejected_count` | Deterministic synthetic/authorized fixture replay |
| `aura.research.citation_validation@1` | `CitationValidationInputV1` → `CitationValidationResultV1` | admitted task evidence repository | `research.citation_validate` | `research_citation_validation_outcome_count`, `research_citation_validation_latency_ms`, `research_citation_rejected_count` | Deterministic fixture replay |
| `aura.research.synthesis@1` | `ResearchSynthesisInputV1` → `ResearchSynthesisResultV1` | model gateway, citation validator | `research.synthesize` | `research_synthesis_outcome_count`, `research_synthesis_latency_ms`, `research_unsupported_claim_count` | Synthetic or explicitly owner-authorized controlled evaluation only |
| `aura.artifacts.persistence@1` | `ArtifactRevisionWriteInputV1` → `ArtifactRevisionWriteResultV1` | Core PostgreSQL, Garage port | `artifact.revision_write` | `artifact_write_outcome_count`, `artifact_write_latency_ms` | Deterministic adapter fixtures; production bytes never replayed |
| `aura.artifacts.deletion@1` | `ArtifactDeleteInputV1` → `ArtifactDeleteResultV1` | Core PostgreSQL, Garage port | `artifact.delete` | `artifact_deletion_outcome_count`, `artifact_deletion_latency_ms` | Fault-injection fixtures only |
| `aura.research.deletion@1` | `ResearchDeleteInputV1` → `ResearchDeleteResultV1` | Core PostgreSQL, artifacts public API | `research.delete` | `research_deletion_outcome_count`, `research_deletion_latency_ms` | Synthetic lifecycle fixtures only |
| `aura.execution.task_projection@1` | `TaskProjectionInvalidationV1` or `TaskProjectionRebuildPageV1` → `TaskProjectionUpdateResultV1` | source public snapshot API, Core PostgreSQL | `task_projection.reconcile` | `task_projection_outcome_count`, `task_projection_reconcile_latency_ms`, `task_projection_lag_ms` | Deterministic snapshot/version fixtures |
| `aura.interaction.research_completion_delivery@1` | `ResearchCompletionDeliveryInputV1` → `ResearchCompletionDeliveryResultV1` | conversation public API, notification public API, Core PostgreSQL | `research_completion.deliver` | `research_completion_outcome_count`, `research_completion_latency_ms` | Synthetic idempotency/failure fixtures only |

The coordinator's exact admitted-budget metrics are
`research_budget_wall_time_consumed_ms` (`milliseconds`),
`research_budget_sources_consumed_count`,
`research_budget_searches_consumed_count`,
`research_budget_browser_pages_consumed_count`,
`research_budget_model_calls_consumed_count`, and
`research_budget_exhaustion_count` (all `count`). It emits one bounded sample
for each consumption metric at the first durable terminal transition using the
admitted-at timestamp and durable operation ledger; retries and fallbacks are
included, and interrupted work uses the last durable ledger. No admitted limit
or task identity becomes a label.

`research_queue_lag_ms` is a `milliseconds` histogram observed at worker claim
as `claimed_at - queued_at`; the coordinator span begins only after claim and
does not cover this interval. Cancellation command latency is the active time
from entry into the authorized cancellation handler through durable cancel
intent commit and bounded response. `research_cancel_settlement_lag_ms` is a
separate histogram observed once at terminal settlement as
`terminal_at - cancel_intent_at`; no span remains open across that wait.

Static-to-browser fallback uses
`research_static_fetch_settled_count` (one `count` for every terminal static
attempt), `research_static_to_browser_fallback_count` (one `count` only when
Core accepts an `inadequate_extraction` transition and dispatches browser
retrieval), and `research_static_to_browser_fallback_ratio` (`ratio`). The
ratio is the bounded quotient of those two counters for the same five-minute
deployment/component cohort, or zero when the denominator is zero; rejected or
disabled browser requests do not increment the numerator.

For these added measurements the dimension and outcome subsets are closed:

- `research_queue_lag_ms` uses only `operation`, `outcome`, `status`,
  `dependency`, and `errorClass`; an observed lag has outcome `success`, while
  inability to establish a trustworthy claim timestamp is recorded only by the
  coordinator outcome counter as `unknown` with `queue_unavailable`.
- Cancellation command metrics use only those same five dimensions and
  outcomes `success`, `rejected`, `timeout`, `failed`, or `unknown`; their only
  applicable error classes are `authorization_denied`, `conflict`,
  `cancel_dispatch_failed`, `provider_unavailable`, `timeout`, and
  `settlement_unknown`. Settlement lag is emitted only with `success` for a
  proven terminal transition; unresolved settlement emits no lag sample and is
  represented by the cancellation outcome counter as `unknown` with
  `settlement_unknown`.
- Budget metrics use only `operation`, `outcome`, `status`, and `errorClass`;
  outcome is `success` for a durable ledger sample and `unknown` with
  `settlement_unknown` when no trustworthy ledger exists. Exhaustion uses
  status `budget_exhausted` and error class `budget_exhausted`.
- Fallback measurements use only `operation`, `outcome`, `status`, `provider`,
  `degradation`, and `errorClass`; emitted count/ratio samples have outcome
  `success`, provider `static_fetcher`, and error class absent. Rejected
  transitions remain on the coordinator outcome counter with the applicable
  `budget_exhausted`, `provider_disabled`, `provider_unavailable`, or
  `validation_failed` class.

`count` metrics use unit `count`; latency and lag use `milliseconds`; the
fallback ratio uses `ratio`. Exact operation values are the span names above
plus metric-only `research.queue_claim`, `research.cancel_settlement`,
`research.budget_settle`, and `research.browser_fallback`. Metrics
and spans may use only metadata-observation-v2 dimensions `operation`,
`outcome`, `status`, `dependency`, `errorClass`, `provider`,
`retrievalStage`, and `degradation`, and only when applicable. Provider values
are the finite set `searxng`, `static_fetcher`, `browser_fetcher`, `ollama`,
`postgresql`, `garage`, `nats`, and `none`. Outcome values are `success`,
`rejected`, `partial`, `canceled`, `timeout`, `failed`, and `unknown`. Status
values are the closed lifecycle/stage values defined in this document.
Dependency values are exactly `core_postgresql`, `transactional_outbox`,
`model_gateway`, `searxng_port`, `static_fetcher_port`,
`browser_fetcher_port`, `static_fetcher_cancel_port`,
`browser_fetcher_cancel_port`, `url_policy`, `content_policy`, `evidence_repository`,
`citation_validator`, `garage_port`, `artifacts_public_api`,
`source_snapshot_api`, `conversation_public_api`, `notification_public_api`,
and `none`. Retrieval-stage values are exactly the research stages; degradation
values are `none`, `provider_disabled`, `provider_unavailable`, `draining`, and
`reconciling`.

The complete error-class vocabulary is `validation_failed`,
`authorization_denied`, `conflict`, `budget_exhausted`, `provider_disabled`,
`provider_unavailable`, `destination_blocked`, `redirect_blocked`,
`type_rejected`, `size_exceeded`, `resource_exhausted`,
`prompt_injection_detected`, `citation_invalid`, `storage_unavailable`,
`deletion_failed`, `cleanup_unknown`, `snapshot_unavailable`, `version_gap`,
`delivery_failed`, `queue_unavailable`, `cancel_dispatch_failed`,
`settlement_unknown`, and `timeout`. Exception text and content-derived values are
never dimensions. Cardinality tests must prove that no other dimension key or
value is emitted.

Production capture is always `metadata_only`. Only spans may carry
metadata-observation-v2 trace lineage (`traceId`, `spanId`, `parentSpanId`, and
bounded `linkedSpanIds`) and, when already admitted by the surrounding run,
optional `runId` or `conversationId`; metrics carry no correlation identifiers.
Task, research, artifact, source, evidence, event, delivery, owner, workspace,
household, request, provider-request, object, digest, URL/domain, query, title,
excerpt, prompt, response, citation, filename, and idempotency identifiers or
values are prohibited from spans, metrics, logs, and evaluator diagnostics.
Component ID/version are structural fields, not labels.

Ordinary metadata observations follow the existing deployment telemetry
retention policy and introduce no content retention. Controlled evaluation
uses only versioned synthetic fixtures or explicitly owner-authorized datasets;
its artifacts retain the existing manual-only evaluation policy. Production
research content, fetched pages, evidence excerpts, and artifact bytes are not
captured for replay. Observatory outage, capture failure, or evaluator failure
cannot affect research, deletion, projection, or delivery outcomes.

Instrumentation tests must register every component and exact metric above,
validate units and the closed operation/outcome/error/dimension vocabularies,
and reject identifiers or content in observations. A fake-clock boundary test
must independently prove queue lag, cancellation command latency, and
cancel-intent-to-terminal lag. It must fail if
`research_coordinate_latency_ms`, an end-to-end task duration, a run duration,
or an open span is substituted for any of those three measurements. Budget
tests prove one terminal sample per ledger field, inclusion of retries and
fallbacks, and values no greater than the admitted envelope. Fallback tests
prove the numerator/denominator rules and the five-minute bounded ratio. Trace
tests prove correlation appears only on spans; capture, replay, retention, and
privacy tests apply unchanged to cancellation, queue, budget, and fallback
observations.

| Evaluator | Target and typed result | Required pass rule |
|---|---|---|
| `research_source_relevance@1` | ranked fixture sources → `RetrievalScoreV1` | recall@k ≥ 0.80 on the approved fixture set |
| `research_claim_support@1` | claims plus admitted evidence → `ClaimSupportScoreV1` | every material retained claim supported; score 1.00 |
| `research_citation_precision@1` | citations plus evidence registry → `CitationPrecisionScoreV1` | precision 1.00; invented/cross-task/stale citations 0 |
| `research_freshness@1` | time-sensitive fixture and source dates → `FreshnessScoreV1` | all fixture freshness expectations pass |
| `research_source_diversity@1` | accepted source set → `SourceDiversityScoreV1` | ≥2 independent domains when fixture declares two available; otherwise explicit limitation |
| `research_unsupported_claims@1` | final result plus evidence → `UnsupportedClaimScoreV1` | unsupported material claim rate 0 |
| `research_budget_compliance@1` | admitted envelope plus operation ledger → `BudgetComplianceScoreV1` | no counter/deadline exceeds admitted envelope |
| `research_evidence_sufficiency@1` | labeled sufficient/insufficient cases → `SufficiencyScoreV1` | accuracy ≥0.90 and no exhausted case mislabeled complete |
| `research_prompt_injection_resistance@1` | hostile evidence fixture → `InjectionResistanceScoreV1` | 100% preserve policy, grants, budgets, tools, and transition authority |
| `research_destination_policy@1` | SSRF/DNS/redirect corpus → `DestinationPolicyScoreV1` | 100% prohibited targets blocked at Core and provider boundary |
| `research_failure_isolation@1` | provider/queue/store/Observatory faults → `FailureIsolationScoreV1` | truthful bounded outcome; Core remains available; no false success |
| `research_privacy@1` | telemetry/event/log/browser-state scan → `PrivacyScoreV1` | zero prohibited fields or content values |
| `research_deletion_truth@1` | deletion and citation fixture → `DeletionTruthScoreV1` | immediate unreadability, no resurrection, and truthful pending/failed/unknown/deleted state |
| `task_projection_consistency@1` | invalidation/snapshot/rebuild histories → `ProjectionConsistencyScoreV1` | live and rebuild snapshots converge exactly; no stale regression or cross-owner row |

These thresholds are release gates created by AURA-0148. They may be raised by
an approved baseline promotion but never lowered or bypassed to make a change
pass.

## Delivery decomposition

Each work item must be schema-valid before implementation, stay inside its
paths, produce structured handoffs, and receive the listed blocking reviews.
Contract changes precede all consumers. Multiple writing zones within a ticket
use isolated workers and Integration Maintainer reconciliation.

| Work item | Primary ownership and deliverable | Dependencies | Required validation and review |
|---|---|---|---|
| `AURA-0141` | Contract Steward: additive OpenAPI, event schemas, web citations, research/artifact/task resources, regenerated clients | `AURA-0140` | Compatibility and generated-sync tests; General, Architecture, Security/Privacy |
| `AURA-0142` | Core Worker: artifacts domain, PostgreSQL migration, Garage port/adapter, preview/download/delete use cases | `AURA-0141` | Unit/integration/migration/storage-fault tests; General, Architecture, Security/Privacy, Observability |
| `AURA-0143` | Core Worker: execution-owned Task Center projection, source public snapshot adapters, rebuild/backfill, routed commands and API | `AURA-0141` | Projection/rebuild/race/architecture tests; General, Architecture, Security/Privacy, Observability |
| `AURA-0144` | Core and Platform Workers: SearXNG, static and browser provider clients/services, sandboxed deployment, capability switches and health | `AURA-0140`, `AURA-0141` | Network/sandbox/SSRF/resource tests; General, Architecture, Security/Privacy, Observability; Integration Maintainer |
| `AURA-0145` | Core Worker: durable research coordinator, checkpoints, budgets, evidence/citations, cancellation, research/evidence deletion, artifact production | `AURA-0142`–`AURA-0144` | Lifecycle/recovery/citation/budget/deletion tests, exact queue/cancellation/budget/fallback instrumentation boundary tests, and module evaluations; General, Architecture, Security/Privacy, Observability, Test/Evaluation |
| `AURA-0146` | Core Worker: admission-only `web.research@1`, stable activity reference, client-subscription semantics, exactly-once completion append and notification | `AURA-0145` | Tool ceiling/run-lifecycle, Quick/Auto boundary, conversation race, delivery and privacy tests; General, Architecture, Security/Privacy, Observability |
| `AURA-0147` | Frontend Worker: Knowledge/Research, Knowledge/Artifacts, Activity/Tasks routes, inline Advanced settings, stable transcript activity, responsive recovery/deletion UX | `AURA-0141`–`AURA-0146` | Lint/unit/type/generated-client/E2E/accessibility/live-region/draft-clearing/theme/zoom tests; General, Security/Privacy, Aura UX/UI |
| `AURA-0148` | Test/Evaluation Engineer and Integration Maintainer: independent security, resilience, quality, privacy and full-stack acceptance; aggregate integration | `AURA-0142`–`AURA-0147` | All work-item checks, evaluation thresholds, repository validators; every applicable blocking reviewer |

`AURA-0142`, `AURA-0143`, and `AURA-0144` may run in parallel only after
`AURA-0141` is reviewed and integrated. `AURA-0145`, `AURA-0146`, and
`AURA-0147` follow dependency order. A reviewer reports findings and never
repairs the reviewed implementation; the same reviewer verifies corrections.

## Rollout and rollback

1. Deploy provider services and configuration with research admission, static
   fetch, and browser fetch switches disabled.
2. Apply additive contracts and Core migrations; deploy artifact and task
   read paths. Run artifact storage reconciliation and idempotent task-source
   backfill before exposing navigation.
3. Enable static-provider health checks and synthetic security probes, then
   enable static research for an owner-only cohort. Browser remains disabled.
4. Enable browser retrieval separately only after sandbox and hostile-page
   acceptance passes. Enable conversational tool admission after dedicated
   research completion and delivery are stable.
5. Expose Aura Web routes and Task Center navigation after authoritative REST,
   SSE reconciliation, recovery, accessibility, and privacy acceptance pass.

Rollback disables admission first, then browser and static dispatch. Existing
tasks are allowed to settle without new provider work or are canceled
truthfully; history and artifacts remain inspectable and deletable. Contract
fields remain additive and readable. Migrations are not destructively rolled
back after data exists. Projection rows may be rebuilt from source public APIs.
Provider services can be removed after all in-flight requests settle because
they own no business truth.

## Acceptance summary

The initiative is complete only when Quick, Auto background-transition, Deep, and
Agent-led journeys pass from both conversation and `/research`; citations are
claim-supported and task-scoped; provider isolation and SSRF suites pass;
artifact revisions, integrity, preview, download, and deletion are truthful;
Task Center rebuild and routed commands preserve source authority; telemetry
and browser-state scans remain content-free; and Core completes research with
Observatory unavailable.
