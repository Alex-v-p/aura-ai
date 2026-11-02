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

#### Owner-approved first defaults

The owner closed this gate on 2026-10-27 for the first bounded slice. These
defaults are design authority for the three follow-on Stage 4 work items; they
are not contracts or implementation by themselves.

The catalogue is closed to exactly four step kinds. Every object described
below rejects unknown properties, and every enum rejects unknown values. JSON
size limits count the UTF-8 bytes of RFC 8785 canonical JSON; text limits count
the UTF-8 bytes of the normalized string rather than characters or code units.

The value algebra is closed to:

- `bounded_text`: an NFC-normalized JSON string containing valid UTF-8, at
  most 8 KiB after normalization;
- `instant`: an offset-aware RFC 3339 JSON string normalized to UTC;
- `opaque_identifier`: an ASCII JSON string of 1–128 characters matching
  `^[A-Za-z0-9_-]+$` and carrying no user-authored meaning;
- `agent_result`: `{ "status": "completed", "output": bounded_text }`;
- `approval_result`: `{ "decision": "approved" | "rejected" | "expired",
  "decided_at": instant }`;
- `notification_receipt`: `{ "notification_id": opaque_identifier,
  "status": "inbox_available" }`; and
- `wait_result`: `{ "resumed_at": instant }`.

The four exact step shapes and mapping slots are:

1. `agent_run` is exactly `{ "kind": "agent_run",
   "agent_revision_id": opaque_identifier, "instructions": bounded_text,
   "input": selector<bounded_text> }`. On success it returns `agent_result`.
   It admits at most one Aura run for the entire instance, has a five-minute
   deadline, exposes zero tools, reads and writes no memory, creates no
   delegated runs, and cannot be replayed.
2. `owner_approval` is exactly `{ "kind": "owner_approval" }`; it has no
   mappable input and returns `approval_result`. Its proposal is system-derived
   from immutable workflow state as specified below; author text and prior-step
   output cannot become approval presentation. It expires no later than seven
   days after creation or the instance deadline, whichever is earlier.
3. `owner_notification` is exactly `{ "kind": "owner_notification",
   "content": selector<bounded_text> }` and returns `notification_receipt`.
   It invokes the Notifications public application API to create one
   owner-private inbox item and cannot select a provider or external delivery
   channel.
4. `wait_until` is exactly `{ "kind": "wait_until", "due_at":
   selector<instant> }` and returns `wait_result`. The instant may be no more
   than 30 days after the step begins and no later than the instance deadline.

Instance input is a closed object of at most 16 publish-time-declared
`bounded_text` or `instant` fields and at most 32 KiB. Each field name matches
`^[a-z][a-z0-9_]{0,63}$`. Each mappable slot has
exactly one selector with one of these closed shapes:
`{ "source": "instance_input", "field": declared_field }` or
`{ "source": "step_output", "step_ordinal": earlier_ordinal,
"field": declared_output_field }`. The referenced type must exactly equal the
slot type. A selector cannot reference the current or a later step. There are
no expressions, interpolation, templates, literals, coercion, dynamic paths,
fallbacks, model-selected mappings, or additional mapping slots. Static agent
instructions are immutable revision configuration, not data-plane values.

A complete published revision contains one through eight steps in a fixed
order and is at most 64 KiB. Agent instructions are at most 8 KiB. The
owner-visible workflow name is at most 512 UTF-8 bytes and 160 Unicode scalar
values. No other free-form static value is permitted. One step is active at a
time; there are no loops, branches, parallelism, recursion, dynamic dispatch,
skip edges, or compensation edges. The first implementation slice admits only
an explicit owner-requested manual start. It has no schedule or other trigger
configuration.

Core validates every closed object, selector, type edge, resource bound, and
the complete acyclic mapping graph before publication and again at admission.
Runtime values cannot select a step kind, change step order or a transition,
modify instructions, policy, owner or workspace, select an agent revision,
widen a grant, enable a tool or memory, suppress a confirmation, choose a
notification channel, or create a nested workflow. Prior-step output is
delimited untrusted evidence wherever it is presented to the one agent run.

An instance permits one logical execution of each declared step, no more than
eight step settlements, and a total lifetime of 30 days from admission.
Persisted private workflow values are bounded to 32 KiB per step record and
64 KiB across instance input and all step outputs. A checkpoint contains only
the closed coordinator state needed to resume—revision and instance
references, current ordinal, bounded statuses, versions, deadlines, and
idempotency references—and is at most 8 KiB. Its `checkpoint_status` is exactly
`current` or `settled`. Core keeps at most one `current` checkpoint plus one
`settled` checkpoint per declared step, for a maximum of nine checkpoints and
72 KiB per instance. Checkpoints do not duplicate prompts, workflow inputs,
step outputs, approval detail, or notification content. Publication and
admission reject an over-limit definition or input; execution fails closed
before persisting an over-limit output or checkpoint.

The instance state and outcome vocabularies are separate. `instance_status` is
exactly `queued`, `running`, `waiting_for_time`, `waiting_for_approval`,
`waiting_for_resolution`, `resolution_required`, `cancel_requested`, or
`terminal`. Only a `terminal` instance has `terminal_outcome`, whose exact enum
is `completed`, `canceled`, or `failed`. `step_status` is exactly `pending`,
`running`, `waiting_for_time`, `waiting_for_approval`, `failed_replayable`,
`resolution_required`, `completed`, `rejected`, `expired`, `canceled`, or
`failed`. Each step also carries `effect_outcome`, exactly `not_attempted`,
`completed`, `definitively_failed`, or `unknown`, and zero or more applicable
`recovery_capabilities` from the closed set `request_replay`, `settle_failed`,
`reconcile`, and `cancel`.

The instance representation separately returns a content-free ordered
`completed_effects` summary of `{ step_ordinal, step_kind }` entries. This lets
clients present completed effects beside current recovery without treating a
terminal outcome as a statement that earlier effects were reversed. `partial`
is derived presentation only when an instance has completed effects but did
not terminalize as `completed`, or when an unknown effect remains in step
history. It is never an instance status, terminal outcome, or step outcome.

There are no automatic semantic retries. At-least-once transport redelivery
re-enters the same command with the same step-execution idempotency identity;
it is duplicate suppression, not a new execution. A definitive non-replayable
failure sets the step to `failed` and immediately terminalizes the instance as
`failed`. A definitive replay-safe failure sets the step to
`failed_replayable`, the instance to `waiting_for_resolution`, and exposes
exactly `request_replay`, `settle_failed`, and `cancel`. Before terminal
settlement the owner may request exactly one replay, using the same revision,
input, narrowed grants, step-execution idempotency identity, and remaining
deadline. A second replay request is rejected idempotently. The owner choosing
`settle_failed`, declining to retry, or reaching the 30-day instance deadline
terminalizes the instance as `failed`. The replay succeeding continues the
fixed sequence; the replay failing terminalizes it as `failed`. The
`agent_run` is never replayable because the instance permits only one agent-run
admission. No command can reopen a terminal instance.

An ambiguous effect sets the step to `resolution_required` with
`effect_outcome: unknown`, sets the instance to the durable nonterminal
`resolution_required` state, exposes only `reconcile` and `cancel`, and blocks
all later steps. Authoritative reconciliation against the owning application
may confirm that the effect completed, set the step to `completed`, and
continue the fixed sequence; or it may establish a definitive failure, set the
step to `failed`, and terminalize the instance as `failed`. Unknown never
offers replay. Owner cancellation, or the 30-day instance deadline while still
unresolved, terminalizes the instance as `canceled` while preserving the
step's unknown effect in immutable history.

All resolution races use an expected instance version and a PostgreSQL lock.
The first terminal settlement wins. If reconciliation commits before
cancellation or deadline settlement, its confirmed result governs and the
normal sequence may continue. If cancellation or deadline settlement commits
first, later authoritative evidence may append bounded reconciliation metadata
to the unknown step history but can never reopen the instance, change its
`canceled` terminal outcome, or admit another step. A normal cancellation sets
`cancel_requested`, prevents future admission, and requests cancellation of an
active Aura run. Confirmed stop settles `canceled`; an ambiguous stop enters
`resolution_required` and follows the same resolution rules. Cancellation
never claims to reverse a sent notification, elapsed wait, approval decision,
generated output, or any completed effect.

Approval rejection or expiry sets the approval step to that exact status with
`effect_outcome: definitively_failed` and terminalizes the instance as
`failed`; it never authorizes a later step. Approval presentation is an
immutable system-derived object containing only: workflow identifier, revision
identifier, the pinned bounded `workflow_display_name`, exact step ordinal and
step-execution identifier, `next_step_kind` (`agent_run`, `owner_approval`,
`owner_notification`, `wait_until`, or `complete`), `consequence`, opaque owner
and workspace scope identifiers, origin `manual_workflow_instance`, and expiry.
The permitted `(next_step_kind, consequence, copy)` triples are exactly:
`(agent_run, continue_to_agent_run, "Approve this step to continue to the agent
run.")`, `(owner_approval, continue_to_owner_approval, "Approve this step to
continue to the next approval.")`, `(owner_notification,
continue_to_owner_notification, "Approve this step to continue to the private
notification.")`, `(wait_until, continue_to_wait_until, "Approve this step to
continue to the wait.")`, or `(complete, complete_workflow, "Approve this step
to complete the workflow.")`. Core selects the one triple from the next fixed
step; no caller supplies it. The display name is rendered as a separately
delimited inert label and is never interpolated into consequence copy. No other
author text, instance input, or prior output enters the presentation. A stale,
expired, canceled, differently bound, or version-mismatched decision is
rejected idempotently. Approval authorizes only this workflow transition; any
nested application confirmation remains mandatory.

Restart reads PostgreSQL truth, revalidates the pinned context and grants, and
resumes only the same current step; transport or executor state cannot advance
it. Grant, connection, owner, workspace, or resource revocation fails closed
before admission or resume and terminalizes as a definitive authorization
failure unless an already-started operation has an unknown effect, which must
enter `resolution_required`.

Definitions, drafts, published revisions, instances, instance history,
results, and checkpoints are retained until an owner makes an explicit,
scope-specific deletion request. The manual scopes are: one draft; one retired
definition; one published revision; one terminal instance identity and state;
the history of one terminal instance; one terminal instance result; or the
checkpoints of one terminal instance. Deleting any one scope never cascades to
another. Active instance state and its pinned revision cannot be deleted.

Deleting instance identity/state retains a content-free instance tombstone
with instance, owner-scope, workflow, revision, and terminal-outcome lineage
for as long as any history, result, or checkpoint survives. Deleting history,
result, or checkpoints leaves that tombstone and every other retained scope
unchanged. Deleting a referenced revision or definition likewise retains the
content-free identity and lineage required by surviving scopes while removing
only the selected private content. Tests must exercise every deletion order
and prove surviving scopes remain authorized, attributable, inspectable, and
unchanged. Each deletion is owner-authorized, CSRF-protected, idempotent, and
leaves content-free audit truth. There is no expiry, cascading deletion,
background purge, or executor-driven retention.

Published revisions are immutable. Editing a published workflow clones its
configuration into a new draft with fresh identity and optimistic version;
publishing that draft creates another immutable revision. An admitted instance
pins one exact workflow revision, agent revision, owner, household, workspace,
grants, mappings, limits, and deadline for its lifetime. Later edits,
publication, retirement, or manual-start availability changes do not alter or
migrate it.

#### Stage 4 threat model

| Abuse or failure case | Required mitigation and testable proof |
| --- | --- |
| Cross-owner or cross-workspace access | Bind owner, household, and workspace at admission; authorize every read, command, deletion, and resume against that immutable context. Tests attempt identifier substitution, list leakage, manual admission under another owner, and resume after privilege change. |
| Worker or executor as a confused deputy | Service identities carry no owner authority. Reconstruct only the pinned narrowed context from PostgreSQL and revalidate it before each step. Tests prove a worker credential alone cannot admit, resume, read private values, approve, notify, or delete. |
| Injection through instance input or prior results | Validate the closed types and publish-time mappings, delimit values as untrusted evidence, and prohibit values from instructions, transitions, policy, grants, confirmation, memory, notification routing, or nested invocation. Hostile strings and mapping/path substitution tests must change neither control flow nor authority. |
| Duplicate command, outbox, or worker delivery | Give each manual admission, instance, step execution, approval, notification, and terminal settlement a stable idempotency identity; serialize transitions with PostgreSQL locks or expected versions. Repeated and reordered delivery tests produce one instance, one logical step effect, and one terminal settlement. |
| Stale, replayed, or misbound approval | Bind an approval to owner, instance, revision, exact step ordinal and execution identity, immutable system-derived presentation, expected version, and expiry. Race tests prove only one current decision wins and that approval cannot authorize a later or changed step, interpolate private output, or bypass a nested confirmation. |
| Grant, connection, or resource revocation | Revalidate immutable owner context and the pinned narrowed grant subset at admission and every resume. Revocation tests fail closed before the next step and never inherit worker, executor, or replacement-revision authority. |
| Resource exhaustion or indefinite suspension | Enforce the eight-step, one-active-step, one-agent-run, five-minute run, seven-day approval, 30-day wait and instance, byte, checkpoint, and replay limits above at publication, admission, persistence, and resume. Boundary and over-limit tests reject whole values and reach a durable bounded outcome. |
| Private-data leakage | Keep inputs, outputs, mappings, instructions, approval context, notification content, checkpoints, and owner identity out of URLs, ordinary events, outbox payloads, SSE, telemetry, logs, browser persistence, service-worker caches, and screenshots. Contract/privacy tests use canary secrets and assert absence at every prohibited sink. |
| Executor-state or LangGraph leakage | Treat any executor as a replaceable private adapter with no policy authority. Public APIs, events, generated clients, audit, telemetry, and Aura Web expose only Aura instance, step, status, and history terms. Boundary tests reject imports or schemas containing executor nodes, graphs, or checkpoint payloads. |
| Cancellation race or false reversal claim | Commit cancellation, reconciliation, deadline, and step settlement with expected versions; preserve committed and unknown effects; and block future admission. Race tests cover cancellation before admission, during the agent run, beside approval, and against reconciliation or terminal commit, including late evidence that must not reopen a terminal instance. |
| Retention or deletion scope confusion | Require an explicit owner-only scope, terminal-state preconditions where applicable, no cascading defaults, and content-free tombstones for retained references. Tests exercise every scope and deletion order, prove surviving history/results/checkpoints retain lineage, prove active state cannot be removed, and prove retrying deletion is idempotent. |

PostgreSQL is the authority for definitions, revisions, manual admissions, instances,
step state, checkpoints, idempotency, and settlement. The transactional outbox
publishes identifier-only durable reactions to NATS under at-least-once
delivery; neither NATS, Valkey, nor an executor owns workflow truth. Step
handlers invoke public application APIs for Runs, Approvals, and Notifications;
they never import another domain's adapters or tables. HTTP and worker
entrypoints use those same workflow application commands. Future browser
clients consume only generated OpenAPI clients.

The first slice needs no LangGraph integration. If a later implementation uses
an executor adapter, it receives only bounded Aura commands, has no independent
owner authority, and reconciles recovery through Aura transitions. Its private
state is neither a workflow checkpoint contract nor a source of truth.

Ordinary workflow events, audit, logs, metrics, traces, outbox messages, and
SSE remain metadata-only. Core never reads from or waits for Observatory.
Owner-enabled full-content evidence, if configured, uses separate closed
ADR-0017 serializers, rejects prohibited or oversized items whole, exports
asynchronously on `run-evidence-v1`, and stays inaccessible to Aura Web.
Observatory never reads Core workflow tables or executor state, and executor
state never enters ordinary telemetry or governed evidence as an arbitrary
object dump.

### Approved first slice

Support owner-authored, versioned workflows with owner-requested manual start,
strictly sequential execution, and the closed typed step catalogue:

- admit one bounded agent run using a pinned agent revision and fixed input
  mapping;
- request an owner approval and pause durably;
- create an owner-private notification from an approved bounded result; and
- wait until an explicit instant when needed for a later step.

The owner-approved catalogue above is final for this slice. It excludes loops,
branches, parallel fan-out, arbitrary templates or expressions, provider calls,
calendar writes, Home Assistant, shell/filesystem access, user-supplied code,
dynamic tool grants, and recursive workflow invocation.

Schedules are deliberately deferred. A later owner decision must close cadence,
timezone, daylight-saving gap and overlap behavior, missed-run policy,
pause/resume/cancel lifecycle, and schedule deletion semantics before any
schedule contract, Core scheduler behavior, or Aura Web schedule control is
authorized. The first contract, Core, and Web slices contain no schedule
fields, routes, commands, workers, or acceptance promise.

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
publish an immutable revision, manually start an instance, inspect progress
and history, decide approval pauses through the canonical approval inbox,
cancel active work, and explicitly delete one authorized retention scope at a
time. Core owns the definition, instance, step, checkpoint, and outcome truth
independently of any executor backend.

### Ordered Stage 4 checkpoints

Use four sequential checkpoints. Each checkpoint requires its own validated
work item, required handoff and reviews, and passing checks before the next one
begins:

1. **Workflow decisions and threat model:** close the gates above, define abuse
   cases, bounded resource policy, retention, and executor independence. The
   owner-approved defaults and threat model in this section complete the design
   input for this checkpoint; repository integration still follows the normal
   review lifecycle.
2. **Workflow, approval, and notification contracts plus deterministic state machine:** define immutable
   definitions, instances, typed steps, statuses, commands, history, events,
   idempotency, optimistic concurrency, typed workflow approval/notification
   unions, manual-admission compatibility, consumer generation, and contract
   tests. It does not define schedules.
3. **Core workflow execution:** implement PostgreSQL state, manual/worker
   admission, shared step handlers, approval pause/resume, checkpoints,
   cancellation, recovery, metadata-safe audit/telemetry, and optional private
   executor adapter. LangGraph and scheduling are not required or authorized
   for the sequential slice.
4. **Aura Web workflow management:** regenerate the client and add definition,
   review/publish, manual start, instance, approval handoff, history, recovery,
   cancel, and scope-specific deletion experiences. It adds no schedule UI.

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
- The state machine uses the exact status, terminal-outcome, per-step outcome,
  effect summary, and recovery-capability vocabularies above. Unknown effects
  are nonterminal `resolution_required`, never a terminal instance outcome.
  Executor-specific state remains private.
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
  memory grants. Worker and executor identities provide no authority, and any
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
  deadline, replayable failure, unknown resolution, late reconciliation,
  cancellation, and terminal non-reopening.
- Security tests prove owner isolation, grant non-escalation, exact approval
  binding, input bounds, and rejection of unknown step kinds or mappings.
- Deterministic evaluation measures completion, step latency, replay and
  duplicate-prevention behavior, approval correctness, cancellation latency,
  recovery, and resource-budget enforcement.
- Full-stack acceptance covers draft through immutable publication, manual
  start, live progress, immutable system-derived approval review, pause/resume,
  replayable-failure choice, unknown reconciliation/cancellation, completed
  effect history, scope-specific deletion, and cancellation. Schedule creation
  and firing are explicitly absent from this slice.
- Authorization tests cover revoked owners, workspaces, sources, tools, memory
  scopes, approval resume, and nested confirmation. Security tests cover
  hostile step output, mapping injection, evidence serialization, and secret
  rejection.

### Aura Web routes and states

Use `/workflows`, `/workflows/new`, `/workflows/:id`, and
`/workflows/:id/instances/:instanceId` under **Automate**. Desktop authoring
uses a labelled ordered list with explicit add, move up, move down, edit, and
remove controls; drag may be supplemental but never required. Mobile edits one
step at a time and returns to the ordered review. Selector authoring is a typed
picker that lists only compatible instance fields and earlier-step output
fields; it never exposes an expression or free-form path editor. Every add,
move, remove, and clone revalidates the entire ordered mapping graph. Adding
moves focus to the new step heading or first invalid field; moving preserves
focus on that step and announces its new ordinal; removing uses a focused
confirmation and returns focus to the next step, previous step, or Add control;
cloning opens the new draft and focuses its heading. Reordering never remaps a
selector silently: a now-forward or incompatible reference blocks save and
publish, identifies the source and expected type, and focuses the first invalid
selector. Version conflict preserves the draft, focuses an error summary, and
offers reload or clone-to-new-draft without overwriting either revision.

Drafts and valid fields survive validation, version conflict, reconnect, and
provider failure. Publish shows the immutable revision, effective step
permissions, confirmation points, and all budgets before the final action.
Running instances are visually separate from editable drafts; a running
revision cannot be edited, but may be cloned into a new draft. The instance
view renders terminal outcome, per-step status/effect outcome, completed-effect
summary, and currently allowed recovery actions as separate concepts. A
canceled instance may therefore still show an unknown prior effect. `Partial`
is only a derived visible qualifier, never a wire status. No unsafe retry is
offered.

Every manual deletion scope appears beside its own contextual resource, never
as a bulk or ambiguous “delete workflow data” action. A focused confirmation
states exactly what private content will be removed, which content-free
tombstone or lineage remains, which other scopes will survive unchanged, and
why an active instance or pinned revision cannot be deleted. Cancel returns
focus to the invoking control; confirmed deletion returns focus to the nearest
surviving heading or collection action and announces the scoped result. Browser
acceptance covers all seven scopes, every surviving-scope relationship,
non-cascade behavior, active-state rejection, stale versions, idempotent repeat,
keyboard focus return, and sanitized failure recovery.

Keyboard-only, focus return, themes, reduced motion, narrow viewport, and 200%
reflow coverage are required.
Meaningful running, awaiting-approval, partial, completed, canceled, failed,
resolution-required, and unknown-effect transitions are announced through a
restrained status/live region without moving focus or announcing every internal
step update.

### Observability

Plan stable components for definition validation, instance admission, each
closed step kind, checkpoint, approval pause/resume, cancellation, and result
settlement. Record identifiers, bounded statuses, versions, durations, replay
classes, and budget usage only. Never place workflow inputs, outputs, mappings,
approval summaries, or owner identity in ordinary telemetry or event payloads.

### Rollout and rollback

This slice remains manual-only; schedules require the separate owner decision
defined above and are not a rollout flag. Rollback disables new definition
publication and instance admission, preserves inspection and cancellation, and
lets already completed history remain truthful. It does not reinterpret or
automatically rerun incomplete instances.

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
