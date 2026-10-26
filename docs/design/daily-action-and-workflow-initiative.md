# Daily action and workflow initiative

> Status: implementation plan for AURA-0126
>
> Scope: four ordered Aura Core and Aura Web stages covering a daily action
> center, read-only calendar planning, scheduled bounded briefings, and a
> narrow first durable workflow capability.

This plan translates the owner's selected product sequence into independently
activatable delivery slices. It does not authorize product code, contracts,
migrations, provider access, dependencies, deployment changes, or protected
baseline changes. Each slice requires its own validated work item and the
reviews named below.

## Outcome and sequencing

The initiative should make Aura useful as a daily operating surface before it
adds a general automation engine:

```text
existing reminders + approvals + notifications + conversations
                              |
                              v
                 Stage 1: Today / Action Center
                              |
                              v
              Stage 2: read-only calendar planning
                              |
                              v
              Stage 3: scheduled bounded briefings
                              |
                              v
           Stage 4: typed sequential durable workflows
```

Stages are ordered dependencies, not one release or one work item. Stage 1 can
start immediately. Stages 2 through 4 stop at their decision gates until the
owner records the required choices. A later stage must not broaden an earlier
stage's permissions, data retention, provider access, or execution budgets by
implication.

## Initiative-wide invariants

- PostgreSQL remains authoritative for Core-owned schedule and workflow state.
  NATS JetStream transports identifier-only durable work and never becomes
  business truth.
- HTTP entrypoints, schedulers, workers, model-facing tools, and future voice
  intents invoke shared application use cases. No entrypoint reimplements
  calendar, briefing, or workflow rules.
- Core domains depend on provider-neutral ports. Google Calendar is the first
  calendar adapter, not a provider dependency in domain code.
- Mutations use owner authorization, origin-aware CSRF protection, UUID
  idempotency, optimistic versions or transactional locking, and explicit
  confirmation where consequential.
- Calendar content, briefing inputs and outputs, workflow inputs, credentials,
  and owner identity stay out of ordinary events, telemetry, URLs, logs, and
  browser persistence. Runtime telemetry remains metadata-only.
- Background workers carry no owner authority. Every scheduled occurrence and
  workflow instance binds the immutable owner principal, household, workspace,
  source grants, and an explicit subset of the pinned agent's tool and memory
  permissions. Admission and every resume fail closed after disconnect,
  revocation, privilege change, or loss of resource access.
- Provider content, retrieved content, and prior-step output are schema-bounded,
  separately delimited untrusted evidence. They cannot alter system
  instructions, source selection, mappings, permissions, tool grants,
  confirmations, memory writes, or workflow transitions.
- Aura Web uses the generated Aura client and public feature APIs. It never
  imports Core internals, Observatory code, the Observatory client, or governed
  run-evidence contracts.
- Runs pin the exact agent revision and applicable policies. Deadlines, step,
  model, tool, retry, and resource budgets are explicit and enforceable.
- User-facing failures distinguish proposed, authorized, attempted, completed,
  canceled, failed, and unknown outcomes. Retry is never offered when it could
  duplicate an uncertain side effect.
- Ordinary OTLP, events, and SSE remain metadata-only. Owner-enabled full-content
  run evidence follows ADR-0017 on its separate one-way channel, manual-only
  retention, owner-only Observatory access, and permanent secret exclusions.
  Each new execution boundary requires a closed serializer and privacy tests;
  Aura Web never reads, links to, caches, or displays that evidence.
- Frontend stages reuse the established semantic tokens and system, light, and
  dark themes; respect reduced motion; preserve focus and drafts; use non-color
  state signals; and do not introduce provider-branded or hard-coded styling.
- Compact navigation, notification, briefing, and workflow summaries default
  to source, status, time, and neutral labels such as “Daily briefing ready”
  or “Workflow needs attention.” Substantive private content appears only on
  the authenticated detail route unless a separately approved owner preference
  defines rich previews. The authenticated `/today` page may show only the
  owner-authorized reminder content and existing conversation title/status
  already returned by current APIs; it does not expose calendar descriptions,
  briefing output, workflow input/output, or approval detail in compact cards.
  Badges and collapsed navigation never contain private content.
- No stage introduces arbitrary scripting, unrestricted network access, shell
  execution, Home Assistant actions, calendar writes, parallel workflow steps,
  graph concepts in public contracts, or automatic Observatory dependency.

## Stage 1 — Today / Action Center

### Product outcome

Add `/today` as Aura Web's authenticated landing page. It should answer “What
needs my attention?” without forcing the owner to inspect several management
destinations.

The first surface contains:

- **Needs attention:** pending approvals and unacknowledged delivered reminder
  notifications, ordered by urgency and expiry only when the existing
  contracts expose those values.
- **Today:** due, fired, and overdue reminders plus the next occurrence of an
  active series where already available.
- **Continue:** a bounded set of recent conversations with their existing
  titles and status.
- **Later:** a small preview of the next upcoming reminders rather than a full
  agenda duplicate.
- **Quick actions:** navigate to Ask Aura, create a reminder, view all
  approvals, or open the full reminder and notification workspaces.

Action Center cards are navigation-only. Approval decisions, reminder
completion, and notification acknowledgement remain on their canonical detail
routes with their existing confirmation, concurrency, and failure semantics.
The Action Center must not provide list-level, bulk, inline, or one-click
mutations.

### Delivery boundary

This is one frontend-owned work item using existing Aura API operations and
SSE stores. It requires no Core contract, database migration, provider,
deployment, or runtime telemetry change.

The owning frontend worker should establish one deliberate feature-library
boundary for the cross-feature presentation rather than placing business
behavior in the application shell. The work item must identify and register
the exact library dependency direction before implementation. The shell owns
only route, navigation, and provider composition.

Expected ownership zones:

- `frontend.aura-web.shell`
- `frontend.aura-web.daily-action`
- `evaluation.aura-web.daily-action`

Expected path families:

- `frontend/apps/aura-web/**`
- the selected new or existing Aura daily-action feature library
- focused Storybook and Aura Web end-to-end specifications
- `tooling/architecture/modules.yaml` only if registration is required
- work-item delivery records

### Interaction and recovery requirements

- Each section loads and fails independently. A failed notification request
  must not erase available reminders or conversations.
- Loading placeholders retain the page hierarchy. The all-clear state is calm
  and offers Ask Aura as the primary next action.
- Live updates are coalesced and section-scoped; the page must not introduce a
  request loop or duplicate existing SSE subscriptions.
- Desktop uses a readable two-column composition where useful. Mobile keeps a
  single logical reading order: needs attention, today, continue, later.
- Focus returns to the triggering card after a detail drawer or route-backed
  overlay closes. Counts and urgency never rely on color alone.
- Private content is rendered only from the current owner-authorized response
  and is not cached by the service worker or copied into URLs.
- Today is the first item in the expanded, rail, and mobile navigation, above
  Conversations. The wordmark and every “Aura home” link resolve to `/today`;
  Conversations keeps an explicit label and route. Today exposes `aria-current`,
  an accessible rail label/tooltip, and the same mobile reading order.
- When mobile navigation is closed it is absent from the accessibility tree.
  When open it has modal semantics or makes the background inert. Escape,
  explicit close, route activation, and viewport changes return focus to a
  predictable trigger or destination.

### Acceptance and evaluation

- `/` redirects to `/today`; `/conversation` and every existing deep link
  remain compatible.
- Existing reminder, notification, approval, and conversation actions retain
  their canonical detail routes and stores.
- Empty, partial failure, offline/reconnecting, permission-denied, and stale
  snapshot states have focused component and end-to-end coverage.
- Keyboard order, headings, landmarks, dynamic status announcements, reflow,
  touch targets, and light/dark contrast meet the current accessibility
  baseline.
- Existing Aura Web unit, Storybook accessibility, Playwright, generated-client
  sync, frontend-boundary, and repository architecture checks remain green.
- Browser coverage includes accessibility-tree assertions for closed/open
  mobile navigation plus 1440×900, 900×1000, 390×844, and 200% reflow checks.

### Rollout and rollback

Ship the route and redirect together after full-stack acceptance. Rollback is
a frontend-only route/navigation reversal; it must not delete or mutate any
Core data. Existing management routes remain the recovery path throughout.

### Minimum roles

- Implementation: Frontend Worker and Test and Evaluation Engineer.
- Review: General Reviewer, Aura UX/UI Reviewer, and Architecture Guardian.
- Integration: not required if one frontend writer owns the slice; otherwise
  use the Integration Maintainer.

## Stage 2 — Read-only calendar-backed daily planning

### Decision gate

Implementation is blocked until the owner approves all of the following:

1. Google OAuth credential retention, encryption, rotation, revocation,
   disconnect, expiry, and reauthorization behavior.
2. Whether Aura durably stores any event content. The recommended initial
   policy is on-demand retrieval with only the minimum encrypted connection
   metadata needed for operation and no durable event-content cache.
3. Which Google calendars are selectable by default and whether newly
   discovered calendars require explicit opt-in.

If these choices cannot fit the existing ADR-0015 secrets and credential
boundary, the planning work item returns `ADR_REQUIRED` before contract or
provider implementation.

### Product outcome

Extend `/today` with a read-only day agenda and deterministic conflict
awareness. Add an Integration management surface for connecting, inspecting,
reauthorizing, and disconnecting Google Calendar without exposing provider
tokens to the browser.

Initial capability is deliberately narrow:

- connect one owner-scoped Google account through a server-held OAuth flow;
- explicitly select readable calendars;
- retrieve a bounded date range in the owner's selected IANA timezone;
- normalize all-day and timed events without losing provider identity;
- identify overlaps deterministically and explain them in plain language;
- merge calendar presentation with existing reminders in Aura Web; and
- show disconnected, expired, rate-limited, partial-calendar, and stale states.

The canonical connection route is `/integrations/calendar`; `/integrations`
is the collection route and appears under a new **Connect** navigation group.
OAuth return restores focus to the connection-status heading or the exact
actionable error. `/today` uses one chronological agenda with an all-day region
followed by timed rows; every row carries a text-and-icon source label, and
conflicts use an explicit “Conflicts with …” relationship rather than color.
On mobile the order is timezone/date context, all-day items, timed items, then
undated reminder attention. Cross-timezone events show both event and display
timezone when they differ.

Calendar create, update, delete, RSVP, attendee changes, automatic rescheduling,
background content synchronization, calendar-to-memory writes, and model-facing
calendar tools are non-goals.

### Work-item decomposition

Use four sequential work items:

1. **Calendar decisions and contract:** record the owner policy, define the
   provider-neutral application API and additive OpenAPI resources, specify
   private/public fields, threat cases, limits, and contract tests. The
   Contract Steward owns this item with Core and frontend consumer review.
2. **Calendar platform and OAuth boundary:** configure runtime-injected OAuth
   client credentials, exact HTTPS callback allowlists, least-privilege
   read-only scopes, fixed Google endpoints and provider egress, readiness,
   secret delivery, and revocation-aware disablement. Provider base URLs are
   deployment-owned and never user-controlled.
3. **Core calendar read foundation:** implement the calendar domain, connection
   metadata, server-side credential reference, Google adapter, authorization,
   bounded retrieval, timezone normalization, deterministic conflict query,
   audit, and metadata-safe telemetry. Any persistent connection metadata uses
   a Core-owned forward migration.
4. **Aura Web daily planner:** regenerate the transport-only client, add the
   integration lifecycle UI and agenda presentation, and compose calendar
   events with reminders without duplicating server conflict rules.

Expected ownership zones:

- `contracts.aura-api.calendar`
- `services.aura-core.calendar`
- `services.aura-core.google-calendar-adapter`
- `platform.google-calendar-oauth`
- `deploy.google-calendar-secrets-and-egress`
- `frontend.aura-web.integrations-calendar`
- `frontend.aura-web.daily-planning`
- `evaluation.calendar-planning`

### Contract and runtime requirements

- Additive REST resources cover connection status and lifecycle, selected
  calendars, bounded event reads, and deterministic conflict results.
- The browser receives no access token, refresh token, client secret, provider
  authorization code, or internal credential reference.
- Provider failures map to stable Aura-owned error categories. Raw Google
  payloads and identifiers are not copied into logs or telemetry.
- Retrieval enforces owner scope, selected-calendar allowlists, range and page
  limits, deadlines, cancellation, and bounded retries with backoff.
- Conflict calculation is deterministic Core logic over normalized intervals;
  the model and browser are not conflict authorities.
- All timestamps retain explicit timezone provenance. DST gaps, overlaps,
  all-day events, multi-day events, and cross-timezone display receive focused
  tests.
- The callback validates exact state and nonce, accepts only the fixed provider
  origin, exposes no token or secret to Aura Web, and requests only approved
  read-only calendar scopes. Disconnect and rollback revoke provider access
  before local credential removal when the provider is reachable and record an
  explicit recoverable state when revocation is uncertain.

### Acceptance and evaluation

- Contract tests prove additive compatibility and generated-client sync.
- Security tests cover state/nonce, CSRF, owner isolation, token expiry,
  revocation, disconnect, reauthorization, and redaction.
- Provider tests use a controlled fake and cover pagination, rate limiting,
  partial failures, cancellation, and malformed upstream responses.
- Deterministic evaluation covers normalization and overlap classification at
  timezone and DST boundaries.
- Full-stack acceptance proves that a disconnected calendar cannot break the
  existing Action Center and that no calendar content enters ordinary
  telemetry, SSE invalidations, browser persistence, or service-worker caches.
- UX acceptance covers connect, callback, expired, reauthorize, disconnect,
  partial-calendar, stale, all-day, cross-timezone, and conflict states across
  desktop, tablet, mobile, keyboard-only operation, and 200% reflow.

### Observability

Plan stable components for connection commands, calendar retrieval, provider
calls, and conflict calculation. Measure bounded duration, outcome, retry
class, returned event count bucket, rate-limit state, and connection health.
Never record event titles, descriptions, attendees, locations, calendar names,
provider tokens, owner identity, or raw provider errors.

### Rollout and rollback

The integration is disabled until explicitly connected. Rollback first disables
new reads, then revokes and removes the owner-authorized credential according
to the approved lifecycle while retaining only the minimum content-free audit
required by policy. `/today` continues with existing Aura-owned sections.

### Minimum roles

- Implementation: Contract Steward, Core Worker, Platform Worker, Frontend
  Worker, and Test and Evaluation Engineer.
- Review: General Reviewer, Architecture Guardian, Security and Privacy
  Reviewer, Observability Steward, and Aura UX/UI Reviewer.
- Integration: Integration Maintainer because the stage has multiple writers.

## Stage 3 — Scheduled bounded briefings

### Decision gate

Implementation is blocked until the owner approves:

- the closed initial source set and whether memory or documents are excluded;
- supported cadence and timezone behavior;
- the delivery representation and retention policy; and
- whether results append to an existing conversation, use a dedicated
  conversation, or become a new durable briefing resource.

The recommended first slice reads only calendar events and reminders, supports
one daily local-time cadence, and delivers an owner-private notification that
links to a dedicated durable briefing result. This recommendation is not an
implementation authorization.

The current notification contract is reminder-specific. The decision and
contract slice must therefore add a backward-compatible typed briefing source
to the notification domain, OpenAPI schema, generated clients, persistence,
and frontend notification feature before briefing delivery may use the inbox.
Briefings own schedule, occurrence, and result records; Notifications owns
inbox records, delivery state, and acknowledgement. The briefing application
invokes a deliberate public notification command and never reads notification
tables or adapters.

### Product outcome

Allow the owner to configure, pause, resume, inspect, and cancel a recurring
daily briefing. At each due time, deterministic schedule logic admits exactly
one bounded Aura run using a pinned agent revision and immutable execution
policy. The result is delivered through the approved owner-private Aura Web
representation and remains inspectable with its schedule and run lineage.

### Work-item decomposition

Use three sequential work items:

1. **Briefing decisions, contracts, notification evolution, and state model:** define schedule,
   occurrence, result, lineage, cancellation, retry, privacy, and event
   contracts without exposing executor concepts. Extend the notification
   source union and migration additively with notification-domain and frontend
   consumers assigned explicitly.
2. **Core schedule and execution:** implement PostgreSQL-owned schedules and
   occurrences, scheduler claiming, idempotent run admission, bounded context
   assembly, worker execution, cancellation/recovery, result persistence, and
   notification handoff through shared application use cases.
3. **Aura Web briefing management:** regenerate the client and add create,
   inspect, pause, resume, cancel, history, and result experiences to the daily
   planning surface.

Expected ownership zones:

- `contracts.aura-api.briefings`
- `contracts.events.automation.briefings`
- `services.aura-core.briefing-schedules`
- `services.aura-core.execution-admission`
- `services.aura-core.notification-delivery`
- `services.aura-core.domains.automation.notifications`
- `frontend.aura-web.briefings`
- `frontend.aura-web.notifications`
- `evaluation.scheduled-briefings`

### Runtime requirements

- Schedule definitions are owner-scoped, versioned, timezone-explicit, and
  separate from conversation history.
- Each occurrence has a stable identity and admits at most one run despite
  scheduler restarts, duplicate transport delivery, or worker retry.
- Every admitted run pins agent revision, model/tool/memory policy, source
  snapshot provenance, deadlines, and all resource budgets.
- Every occurrence pins the immutable owner principal, household, workspace,
  selected source grants, and a reviewed subset of agent tool and memory
  grants. Admission and resume revalidate current authorization; the scheduler
  and worker identities provide no authority. A source disconnect or revoked
  grant fails closed or produces the explicitly contracted partial result.
- Missed-occurrence policy is deterministic and bounded. The initial
  recommendation is to run at most one overdue occurrence and skip older ones
  with explicit history, matching the conservative reminder-series pattern.
- Provider or source failure produces a partial or failed briefing according
  to the approved contract; it cannot silently omit required sources.
- Pausing prevents future admission without canceling a running occurrence.
  Canceling a running occurrence uses the existing Aura run cancellation
  boundary and records an unambiguous terminal outcome.
- The scheduler polls and claims work only. It does not contain briefing domain
  rules or call a model directly.
- Calendar and reminder values are delimited untrusted evidence. They cannot
  modify the compiled instructions, selected sources, permissions, tools,
  confirmation policy, memory writes, or execution flow. Hostile calendar
  titles and indirect prompt injection receive dedicated security tests.
- If owner-enabled run evidence is active, briefing admission, source assembly,
  execution, and delivery use closed ADR-0017 serializers. Credentials,
  secret-bearing provider fields, and arbitrary provider payloads are rejected
  whole; capture failure cannot change briefing execution.

### Acceptance and evaluation

- State-machine and property tests cover timezone/DST recurrence, claims,
  duplicate delivery, restarts, stale versions, missed occurrences, pause,
  resume, cancellation, deadline, and terminal delivery.
- Runtime evaluation proves the configured budget is enforced and that source
  provenance, required-source completeness, and output delivery are recorded
  without private content in metrics.
- Full-stack acceptance covers setup through firing, live status, result
  inspection, cancellation, source degradation, and reconnect replay.
- Authorization acceptance covers privilege change, calendar disconnect,
  source revocation, agent-policy supersession, admission, and resume. No
  background identity may widen the pinned owner-authorized subset.
- Failure injection proves that NATS, model, calendar, notification, and
  browser outages do not corrupt authoritative schedule or occurrence state.
- Security acceptance includes hostile provider content, indirect prompt
  injection, closed evidence serialization, secret rejection, and proof that
  ordinary telemetry and Aura Web never receive governed evidence.

### Aura Web routes and states

Use `/briefings`, `/briefings/new`, `/briefings/:id`, and
`/briefings/:id/occurrences/:occurrenceId` under **Automate**. Wide screens may
open occurrence detail in a route-backed drawer; mobile uses the canonical
full-screen route. Creation preserves valid fields and the unsaved draft after
validation, provider, or reconnect failure. Pause, resume, cancel, and delete
or retention-related actions name their scope and require a focused review when
consequential. Running, partial, canceled, failed, and unknown outcomes remain
visually and semantically distinct. Keyboard-only, reduced-motion, all-theme,
mobile, tablet, 200% reflow, stale-version, and reconnect journeys are required.
Meaningful running, awaiting-approval, partial, completed, canceled, failed,
and unknown transitions are announced through a restrained status/live region
without moving focus; low-level streaming or step updates are not individually
announced.

### Observability

Use stable Aura-owned components for schedule claim, briefing admission,
source assembly, execution, persistence, and delivery. Measure occurrence lag,
admission/result outcome, duration, retry class, budget exhaustion, source
completeness flags, and delivery status. Content and owner identity remain
excluded. Core exports asynchronously and never reads from or waits for
Observatory.

### Rollout and rollback

Roll out schedule creation behind an explicit product capability after real
full-stack recovery tests. Rollback disables new schedule admission, allows
the owner to cancel or inspect running work, and preserves authoritative
history according to the approved retention policy. It never deletes runs or
results merely to downgrade.

### Minimum roles

- Implementation: Contract Steward, Core Worker, Frontend Worker, Platform
  Worker where scheduler/deployment wiring is required, and Test and Evaluation
  Engineer.
- Review: General Reviewer, Architecture Guardian, Security and Privacy
  Reviewer, Observability Steward, and Aura UX/UI Reviewer.
- Integration: Integration Maintainer.

## Stage 4 — Typed sequential durable workflows

### Decision gate

Do not authorize a general “workflow engine.” Before the first vertical slice,
the owner must approve:

1. the closed initial step catalogue and typed data passed between steps;
2. per-step and whole-workflow time, run, tool, retry, and resource limits;
3. retry, compensation, unknown-outcome, and cancellation semantics;
4. definition/result/checkpoint retention and deletion policy; and
5. immutable definition revision and running-instance edit behavior.

If public contracts would expose LangGraph nodes/checkpoints, permit arbitrary
code or expressions, add an unbounded provider boundary, or require a new
trust model, return `ADR_REQUIRED`.

### Recommended first slice

Support owner-authored, versioned workflows with schedule or manual triggers,
strictly sequential execution, and a small typed step catalogue:

- admit one bounded agent run using a pinned agent revision and fixed input
  mapping;
- request an owner approval and pause durably;
- create an owner-private notification from an approved bounded result; and
- wait until an explicit instant when needed for a later step.

The final catalogue is an owner decision. The first slice excludes loops,
branches, parallel fan-out, arbitrary templates or expressions, provider calls,
calendar writes, Home Assistant, shell/filesystem access, user-supplied code,
dynamic tool grants, and recursive workflow invocation.

The current approval contract is closed over reminder proposals, and the
current notification source is reminder-specific until Stage 3 evolves it.
The workflow contract slice must add a backward-compatible typed workflow-step
approval proposal and workflow notification source, including migrations,
OpenAPI unions, generated clients, run-event compatibility, and both frontend
consumers. Workflows own definitions, instances, steps, checkpoints, and
results; Approvals owns approval records and decisions; Notifications owns
inbox records and acknowledgement. Cross-domain orchestration uses deliberate
public application commands and never reaches into another domain's tables or
adapters.

### Product outcome

Aura Web can create a draft workflow, review its effective steps and limits,
publish an immutable revision, manually start or schedule an instance, inspect
progress and history, decide approval pauses through the canonical approval
inbox, and cancel active work. Core owns the definition, instance, step,
checkpoint, and outcome truth independently of any executor backend.

### Work-item decomposition

Use four sequential work items:

1. **Workflow decisions and threat model:** close the gates above, define abuse
   cases, bounded resource policy, retention, and executor independence.
2. **Workflow, approval, and notification contracts plus deterministic state machine:** define immutable
   definitions, instances, typed steps, statuses, commands, history, events,
   idempotency, optimistic concurrency, typed workflow approval/notification
   unions, migration compatibility, consumer generation, and contract tests.
3. **Core workflow execution:** implement PostgreSQL state, scheduler/worker
   admission, shared step handlers, approval pause/resume, checkpoints,
   cancellation, recovery, metadata-safe audit/telemetry, and optional private
   executor adapter. LangGraph is not required for the sequential slice.
4. **Aura Web workflow management:** regenerate the client and add definition,
   review/publish, instance, approval handoff, history, recovery, and cancel
   experiences.

Expected ownership zones:

- `contracts.aura-api.workflows`
- `contracts.events.automation.workflows`
- `services.aura-core.workflow-domain`
- `services.aura-core.workflow-execution`
- `services.aura-core.approval-integration`
- `services.aura-core.domains.governance.approvals`
- `services.aura-core.domains.automation.notifications`
- `frontend.aura-web.workflows`
- `frontend.aura-web.approvals`
- `frontend.aura-web.notifications`
- `evaluation.durable-workflows`

### Domain and execution requirements

- Published workflow revisions are immutable. Running instances pin one exact
  revision and never change when a later revision is published.
- Core validates every step and mapping before publication. Step handlers call
  public application use cases and cannot access another domain's tables or
  adapters.
- The state machine defines queued, running, waiting-for-time,
  waiting-for-approval, completed, canceled, failed, and unknown outcomes in
  Aura-owned terms. Executor-specific state remains private.
- Checkpoint persistence is bounded and references the Aura instance and step.
  PostgreSQL workflow state remains authoritative after a restart.
- At-least-once delivery cannot duplicate a step. Every mutating handler has a
  stable step-execution idempotency identity and an explicit unknown-outcome
  path.
- Approval can narrow or authorize only the exact pending step. It cannot
  alter the definition, expand grants, or approve later steps.
- Workflow approval never replaces a nested application's native permission or
  confirmation rule. A step that calls another use case must still satisfy that
  use case's current authorization and confirmation policy.
- Every instance and resume binds and revalidates its immutable owner principal,
  household, workspace, source grants, and explicit subset of agent tool and
  memory grants. Scheduler and worker identities provide no authority, and any
  lost grant fails closed before the next step.
- Definition inputs and prior-step results are typed, bounded, separately
  delimited untrusted evidence. They cannot select step kinds, alter mappings,
  change instructions, widen grants, suppress confirmations, write memory, or
  choose transitions. Hostile and indirect prompt-injection fixtures are part
  of security acceptance.
- Owner-enabled run evidence uses closed ADR-0017 serializers for workflow
  admission, step execution, approval, and settlement. Secret-bearing values
  and arbitrary prior-step/provider payloads are rejected whole, and evidence
  export remains nonblocking and isolated from Aura Web.
- Cancellation stops future admission, propagates to a running Aura run, and
  records cleanup without claiming reversal of completed side effects.

### Acceptance and evaluation

- Model-based state-machine tests cover every valid and invalid transition,
  version conflict, duplicate command, approval race, restart, checkpoint,
  deadline, retry, cancellation, and unknown outcome.
- Security tests prove owner isolation, grant non-escalation, exact approval
  binding, input bounds, and rejection of unknown step kinds or mappings.
- Deterministic evaluation measures completion, step latency, retry and
  duplicate-prevention behavior, approval correctness, cancellation latency,
  recovery, and resource-budget enforcement.
- Full-stack acceptance covers draft through immutable publication, manual and
  scheduled start, live progress, approval pause/resume, failure recovery,
  history, and cancellation.
- Authorization tests cover revoked owners, workspaces, sources, tools, memory
  scopes, approval resume, and nested confirmation. Security tests cover
  hostile step output, mapping injection, evidence serialization, and secret
  rejection.

### Aura Web routes and states

Use `/workflows`, `/workflows/new`, `/workflows/:id`, and
`/workflows/:id/instances/:instanceId` under **Automate**. Desktop authoring
uses a labelled ordered list with explicit add, move up, move down, edit, and
remove controls; drag may be supplemental but never required. Mobile edits one
step at a time and returns to the ordered review. Drafts and valid fields
survive validation, version conflict, reconnect, and provider failure. Publish
shows the immutable revision, effective step permissions, confirmation points,
and all budgets before the final action. Running instances are visually
separate from editable drafts; a running revision cannot be edited, but may be
cloned into a new draft. Attempted, waiting, completed, partial, canceled,
failed, and unknown outcomes have explicit recovery paths without unsafe retry.
Keyboard-only, focus return, themes, reduced motion, narrow viewport, and 200%
reflow coverage are required.
Meaningful running, awaiting-approval, partial, completed, canceled, failed,
and unknown transitions are announced through a restrained status/live region
without moving focus or announcing every internal step update.

### Observability

Plan stable components for definition validation, instance admission, each
closed step kind, checkpoint, approval pause/resume, cancellation, and result
settlement. Record identifiers, bounded statuses, versions, durations, retry
classes, and budget usage only. Never place workflow inputs, outputs, mappings,
approval summaries, or owner identity in ordinary telemetry or event payloads.

### Rollout and rollback

Start with manual admission and one controlled owner before enabling schedules.
Rollback disables new definition publication and instance admission, preserves
inspection and cancellation, and lets already completed history remain
truthful. It does not reinterpret or automatically rerun incomplete instances.

### Minimum roles

- Implementation: Contract Steward, Core Worker, Frontend Worker, Platform
  Worker, and Test and Evaluation Engineer.
- Review: General Reviewer, Architecture Guardian, Security and Privacy
  Reviewer, Observability Steward, and Aura UX/UI Reviewer.
- Integration: Integration Maintainer.

## Future observability and evaluation contracts

AURA-0126 changes documentation only, so it affects no runtime observable
component. Future runtime work items must either adopt the following proposed
Aura-owned identities and register their exact manifests or replace them with
an Architecture Guardian- and Observability Steward-reviewed equivalent before
implementation. Stage 1 adds no runtime telemetry; its behavior is evaluated
through frontend unit, accessibility, browser, and request-amplification tests.

| Proposed component | Owner and typed boundary | Dependencies | Active-operation span and local metric | Replay and evaluator |
| --- | --- | --- | --- | --- |
| `aura.integration.calendar_connection@1.0.0` | Calendar application; typed connect, reauthorize, disconnect command to connection result | identity, audit, credential port, Google adapter | command admission through committed result; `calendar_connection_command_duration_ms` | external OAuth effects are not replayable; contract-fake evaluation uses `calendar.provider-boundary.v1` |
| `aura.provider.google_calendar@1.0.0` | Google adapter; bounded Aura provider request to normalized provider response/error | credential port, fixed Google endpoint, HTTP transport | one outbound request from dispatch through complete body/error and close; `google_calendar_provider_request_duration_ms` | real provider calls are not replayable; versioned HTTP contract fixtures use `calendar.provider-boundary.v1` |
| `aura.integration.calendar_retrieval@1.0.0` | Calendar application; owner-scoped range query to normalized bounded event page | authorization, credential port, Google adapter | authorized query through normalized page or terminal failure; `calendar_retrieval_duration_ms` | provider calls are not replayed; normalized synthetic response replay uses `calendar.normalization.v1` |
| `aura.automation.calendar_conflict@1.0.0` | Calendar domain; normalized interval set to deterministic conflict groups | calendar normalization only | conflict invocation through complete groups; `calendar_conflict_evaluation_duration_ms` | replayable with versioned synthetic intervals; `calendar.conflict.v1` |
| `aura.automation.briefing_scheduler@1.0.0` | Briefing schedule application; due occurrence claim to admission command or terminal skip | PostgreSQL schedule state, outbox | due claim through durable admission/skip settlement; `briefing_schedule_lag_ms` and `briefing_schedule_claim_duration_ms` | replayable with synthetic schedule/clock fixtures; `briefing.schedule-admission.v1` |
| `aura.execution.briefing_admission@1.0.0` | Run coordinator; authorized occurrence plus pinned policy to admitted run identity | identity, agent policy, run coordinator | admission request through durable admitted/rejected result; `briefing_admission_duration_ms` | replayable with synthetic policies; `briefing.authorization.v1` |
| `aura.execution.briefing_context@1.0.0` | Briefing application; authorized source snapshot references to bounded model-visible context | calendar/reminder public queries, prompt compiler | source assembly start through validated context result; `briefing_context_assembly_duration_ms` | replayable only with controlled synthetic snapshots; `briefing.source-completeness.v1` |
| existing `aura.execution.run_coordinator@1.9.0` | Existing Run Coordinator; admitted briefing/workflow run to durable terminal run state | model, tools, checkpoints, run repository | existing operation spans including execution, `checkpoint.persist`, and `run.cancel`; existing `operation_duration_ms` | existing controlled Core evaluation runner; future briefing/workflow fixtures extend `briefing.execution.v1` and `workflow.agent-run.v1` without changing ownership |
| `aura.automation.briefing_persistence@1.0.0` | Briefing result application; terminal run result to committed briefing result/occurrence lineage | run public result, briefing repository | persistence start through committed result or terminal conflict; `briefing_result_persistence_duration_ms` | replayable with synthetic terminal results; `briefing.result-persistence.v1` |
| `aura.automation.briefing_cancellation@1.0.0` | Briefing application; owner cancel command to schedule/occurrence settlement and optional public run cancel | authorization, briefing repository, Run Coordinator | authorized cancel through durable local settlement; `briefing_cancellation_duration_ms` | replayable with synthetic schedule/run states; `briefing.cancellation.v1` |
| `aura.automation.briefing_delivery@1.0.0` | Briefing application to public notification command; settled result to inbox delivery result | briefing result store, notification public API | delivery command through durable inbox result; `briefing_delivery_duration_ms` | replayable with synthetic result metadata and notification fake; `briefing.delivery.v1` |
| `aura.automation.workflow_definition@1.0.0` | Workflow application; draft revision to validation/publication result | authorization, closed step registry | validation/publication start through committed or rejected revision; `workflow_definition_validation_duration_ms` | replayable with versioned synthetic definitions; `workflow.definition.v1` |
| `aura.automation.workflow_scheduler@1.0.0` | Workflow application; due trigger claim to instance admission or terminal skip | PostgreSQL workflow state, outbox | due claim through durable admission/skip; `workflow_schedule_lag_ms` and `workflow_schedule_claim_duration_ms` | replayable with synthetic trigger/clock fixtures; `workflow.admission.v1` |
| `aura.automation.workflow_step.agent_run@1.0.0` | Workflow agent-run handler; typed pinned run request to durable step settlement | Run Coordinator public API | handler admission through durable step settlement; `workflow_agent_run_step_duration_ms` | replayable with synthetic run outcomes; `workflow.agent-run.v1` |
| `aura.automation.workflow_approval@1.0.0` | Workflow coordinator to public approval command; exact paused step to authorized/rejected/expired resume | approval public API, workflow state | approval proposal creation and each resume command are separate spans; `workflow_approval_command_duration_ms` | replayable with synthetic races; `workflow.authorization.v1` |
| `aura.automation.workflow_step.notification@1.0.0` | Workflow notification handler; typed neutral notification request to durable step settlement | notification public API | handler admission through durable inbox/step settlement; `workflow_notification_step_duration_ms` | replayable with notification fake; `workflow.notification-step.v1` |
| `aura.automation.workflow_step.wait_until@1.0.0` | Workflow wait handler; typed instant to durable waiting/resumed step state | clock, workflow repository, scheduler | each enter-wait and due-resume operation has a separate span; `workflow_wait_command_duration_ms` plus explicit `workflow_wait_lag_ms` | replayable with synthetic clock/state; `workflow.wait-step.v1` |
| `aura.automation.workflow_checkpoint@1.0.0` | Workflow coordinator; authoritative instance/step transition to bounded durable checkpoint reference | workflow repository, optional private executor adapter | checkpoint request through verified committed reference; `workflow_checkpoint_persistence_duration_ms` | replayable with synthetic bounded state; `workflow.checkpoint.v1` |
| `aura.automation.workflow_cancellation@1.0.0` | Workflow application; owner cancel command to durable instance settlement and downstream public cancel | authorization, workflow repository, Run Coordinator | authorized cancel through durable local settlement; `workflow_cancellation_duration_ms` | replayable with synthetic instance/run states; `workflow.cancellation.v1` |
| `aura.automation.workflow_settlement@1.0.0` | Workflow application; terminal step result to durable instance outcome and optional notification | workflow state, notification public API | terminal settlement start through committed instance outcome; `workflow_settlement_duration_ms` | replayable with synthetic histories; `workflow.state-machine.v1` |

For every component above:

- Allowed metric dimensions are closed low-cardinality enums: `operation`,
  `outcome`, `error_class`, and a fixed provider or step-kind value only where
  declared by that component's future manifest. Identifiers, timestamps,
  owner/household/workspace values, calendar names, content-derived values,
  URLs, model names, and raw provider errors are prohibited metric labels.
- Correlation, causation, run, occurrence, workflow, instance, step, approval,
  and notification identifiers are trace-only bounded attributes where the
  established telemetry contract permits them; they never become metric
  dimensions.
- The closed outcome set is `ok`, `partial`, `canceled`, `rejected`, `timeout`,
  and `error`, narrowed when a component cannot produce a value. The closed
  error classes are `authorization`, `validation`, `conflict`, `provider_auth`,
  `provider_rate_limit`, `provider_unavailable`, `dependency_unavailable`,
  `deadline`, `budget`, and `internal`, again narrowed per component.
- Component spans measure active local operation time only. Schedule lag and
  approval wait are explicit measurements, not inflated active spans. Each
  future work item must test span start/end semantics, error mapping, and that
  prohibited or identifier-derived values cannot enter metric dimensions.
- Ordinary diagnostics and replay fixtures are metadata-only and synthetic by
  default. Any explicitly authorized evaluation artifact follows the existing
  owner-only manual-deletion retention policy. Ordinary telemetry uses the
  existing deployment-owned retention configuration; a future stage must not
  introduce a new retention class or silently extend it.
- Product reconnect/restart recovery is not component replay. A component
  marked replayable above uses versioned synthetic inputs and deterministic
  expected outputs; external provider side effects are never replayed.
- Governed full-content evidence is separate from these evaluators and follows
  ADR-0017. Evaluator artifacts, diagnostics, logs, metrics, and SSE must never
  receive credentials, secret-bearing fields, calendar or workflow content,
  owner identity, or arbitrary provider payloads.

## Activation order and branch checkpoints

Activate only one bounded work item at a time unless its validated parallelism
plan proves disjoint ownership. The intended order is:

1. Stage 1 Action Center.
2. Stage 2 owner decision and contract.
3. Stage 2 Calendar platform and OAuth boundary.
4. Stage 2 Core foundation.
5. Stage 2 Aura Web planner.
6. Stage 3 owner decision, contract, notification evolution, and state model.
7. Stage 3 Core execution.
8. Stage 3 Aura Web management.
9. Stage 4 owner decisions and threat model.
10. Stage 4 workflow, approval, and notification contracts and deterministic state machine.
11. Stage 4 Core execution.
12. Stage 4 Aura Web management.

Create a focused local Git checkpoint only after each work item has an
implementation handoff, all required checks, blocking review clearance, and an
integration report where multiple writers were used. Push only with explicit
task-specific owner authorization. Never weaken tests, architecture checks,
evaluation thresholds, privacy rules, or protected baselines to advance a
stage.

## Planning completion criteria

A future Delivery Lead may activate Stage 1 directly from this plan. For every
later stage it must first confirm the decision gate, create a fresh repository
map against the then-current branch, allocate a stable work-item identifier,
and validate exact allowed and forbidden paths. This plan supplies sequence and
constraints; it is not standing implementation authority.
