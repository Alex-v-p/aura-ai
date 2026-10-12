# Aura Web sidebar and management surfaces

> Status: approved page-specific design direction for AURA-0058
>
> Scope: Aura Web shell and the existing management destinations for Agents,
> Personas, Memory, Reminders, Approvals, and Notifications. This document is
> implementation guidance; it does not create frontend components, runtime
> APIs, wire values, observability identifiers, or product capabilities.

This document extends the [Aura Web design foundation](README.md) for the
management surfaces that share the application shell. It makes the selected
information architecture and responsive behavior concrete while preserving the
foundation's calm, warm, refined, and legible direction.

## Authority and precedence

Use this order when guidance conflicts:

1. Owner decisions and approved product requirements.
2. Accepted ADRs.
3. Approved specifications, including the Aura Web design foundation.
4. The architecture handover and module boundaries, including Core/Observatory
   isolation and frontend product boundaries.
5. Versioned API and event contracts.
6. This page-specific direction and local implementation guidance.

An implementation must record a conflict rather than silently inventing an
exception. This document cannot authorize a new package, framework, service,
runtime contract, retention policy, provider, permission, or cross-boundary
dependency. Existing routes and deep links remain compatible unless a later
owner-approved work item explicitly says otherwise.

The source documents are:

- [Design index](README.md) for boundaries, settled direction, and document
  routing.
- [Visual foundations](foundations.md) for semantic tokens, typography,
  spacing, shape, motion, themes, responsive density, accessibility, and
  privacy-sensitive presentation.
- [Components and interactions](components-and-interactions.md) for the shell,
  conversation context, controls, forms, drawers, dialogs, notifications, and
  interaction states.
- [Failure and recovery states](failure-and-recovery-states.md) for preserving
  drafts and partial work, authentication and permission states, recovery copy,
  approvals, side effects, and sanitized diagnostics.
- [Reference research](reference-research.md) for adopted and explicitly
  rejected lessons from the supplied visual references. References are
  inspiration only and do not add requirements.

## Direction at a glance

Aura uses a grouped workspace with one clear home for each concept:

```text
Conversations

Personalize
  Agents
  Personas
  Memory

Automate
  Reminders

Activity
  Approvals
  Notifications
```

The sidebar is global navigation. Conversation history, search, and filters are
contextual conversation controls and never compete with global destinations.
The default experience is breathable and task-focused; technical metadata,
diagnostics, thresholds, identifiers, and provenance are available in detail
views or explicit Advanced disclosures.

Use the existing semantic roles from `foundations.md`: `canvas`, `surface`,
`surface-subtle`, `surface-raised`, `text`, `text-muted`, `border`,
`control-border`, `accent`, and separate success/warning/danger/info status
roles. Dusty rose indicates interaction and selection; it never means failure.
Use the existing system-sans/Inter intent, 4px spacing grid, 8/12/16/24px
radius tiers, restrained borders, soft elevation, 120–200ms ordinary motion,
and reduced-motion behavior. Do not introduce a parallel palette, decorative
gradient system, or copied reference branding.

## Shell anatomy

### Wide desktop

- Keep a readable labeled sidebar at approximately 280px wide with the Aura
  identity, grouped navigation, optional pending/unread counts, and an explicit
  collapse control.
- Use labels of at least 14px with preferred 44×44px targets. The current route
  has a surface treatment, text and icon state, and a visible marker; color is
  never the only signal.
- Keep the focused page canvas primary. Contextual detail may open beside it as
  a drawer; a fixed three-pane layout is not required.
- Keep appearance selection in a Preferences menu or compact Preferences control,
  not as a competing navigation destination.
- Preserve the current heading and landmark order: navigation, page heading,
  content, then contextual detail or status.

### Tablet

- Use a compact rail around 72–80px when space requires it, but expose every
  route name through an accessible label and tooltip/on-demand label.
- Never squeeze conversation search, filter, or history controls into the rail.
  Open those controls as a contextual overlay from the Conversations context.
- Keep the canvas usable at text zoom and reflow. Drawers overlay content rather
  than shrinking the page into an unusable strip.
- Pending and unread badges remain reachable and are not removed merely because
  labels are collapsed.

### Mobile

- Use an explicit menu trigger and a grouped overlay drawer with a scrim.
- The drawer traps focus while open, closes on Escape or its close control,
  returns focus to the trigger, and exposes the same grouped hierarchy as
  desktop.
- Use full-height detail pages or drawers for selected records. Do not force a
  desktop split pane into a narrow viewport.
- Keep primary page actions reachable without relying on hover, tiny icons, or
  horizontal scrolling.

### Shared state patterns

Every surface uses the same page-level state vocabulary and structure:

- **Loading:** structural placeholder matching the eventual content; preserve
  route context and user-authored drafts.
- **Empty:** explain what belongs here and provide one clear next action.
- **Authentication required:** explain that sign-in is needed, preserve local
  work where policy allows, and offer the existing sign-in path.
- **Permission denied:** name the affected action or resource and do not offer an
  impossible retry loop.
- **Validation:** associate plain-language error text with the field and retain
  valid inputs.
- **Offline/reconnecting/provider failure:** identify the affected task,
  preserve drafts or partial results, and make retry/cancel semantics explicit.
- **Success/warning/danger/info:** combine text and an accessible semantic signal;
  never rely on color alone.

Forms use a consistent heading, short explanation, grouped fields, inline help,
review/publish or save action, and a safe cancel/back path. Destructive or
consequential actions use a focused confirmation dialog or review step. Drawers
announce their heading, manage focus, provide a close action, and return focus
to their opener.

## Route matrix

The matrix names every current Aura Web management route and the detail routes
selected for the next implementation work. Existing routes remain compatible;
the three explicit detail routes are additive deep links and canonical mobile
representations. Desktop drawers open the same route-backed detail without
creating a second state model.

| Route | Purpose | Regions and actions | Desktop / tablet / mobile | Drawer and subpage relationship | Compatibility |
| --- | --- | --- | --- | --- | --- |
| `/agents` | Agent mission-control library | Header, search/status filters when useful, Agent list/cards, Create agent | Labeled cards; compact list with overlay filters; stacked cards | Opens `/agents/:id`; no route change for collection filters | Preserve existing list URL and create action |
| `/agents/new` | Progressive Agent builder | Identity, Persona, Behavior, Capabilities, Memory, Review and Publish | Grouped sections; one focused step; one focused step | Full-page builder; dialogs only for consequential confirmation | Preserve create deep link and draft fields |
| `/agents/:id` | Individual Agent workspace | Overview, Behavior, Capabilities, Memory summary, Versions, lifecycle action | Tabs plus detail; tabs with overlay; focused subpages | Canonical Agent detail; policy routes are related subpages | Preserve detail URL and immutable revision behavior |
| `/agents/:id/tools` | Agent capability/tool policy | Grants, limitations, Advanced metadata, save/revise | Detail panel; overlay; full-screen subpage | Child policy subpage of `/agents/:id` | Preserve tool-policy deep link |
| `/agents/:id/memory` | Agent memory policy | Read/write scope, recall behavior, plain-language policy, Advanced limits | Detail panel; overlay; full-screen subpage | Child policy subpage, distinct from global records | Preserve memory-policy deep link |
| `/personas` | Persona creative library | Cards, search when needed, status, assigned-Agent count, Create persona | Calm cards; compact cards with overlay filters; stacked cards | Opens `/personas/:id` | Preserve list URL and create action |
| `/personas/new` | Persona editor | Identity, guidance, ephemeral Preview, Review and Save | Split editor/preview; stacked sections; consecutive full-screen steps | Full-page editor; Preview stays in draft context | Preserve create deep link and unsaved inputs |
| `/personas/:id` | Persona detail and assignments | Overview, Preview, Used by, Versions, enable/disable | Detail drawer; overlay; full-screen detail | Canonical Persona detail and preview context | Preserve detail URL and historical references |
| `/memory` | “What Aura knows” records | Search, readable list, filters, selected inspector, Create memory | List plus inspector; rail plus overlay; list then `/memory/:id` | Inspector selection resolves to `/memory/:id` | Preserve records URL and authorization rules |
| `/memory/:id` | Memory record detail | Content, scope, provenance, evidence, lifecycle, Correct, secondary actions | Inspector/drawer; overlay; full-screen page | Canonical record subpage opened from `/memory` | Preserve record deep link |
| `/memory/candidates` | Memory review queue | Pending count, candidate list, source/scope detail, Approve, Edit and approve, Reject | List/detail; overlay detail; stacked review then detail | Candidate detail is contextual and review-first | Preserve review-queue URL |
| `/memory/settings` | Memory setup and model health | Setup, active model health, recovery guidance, Advanced provider inventory | Sections; grouped disclosures; grouped disclosures | Full page; technical disclosures stay local | Preserve settings URL |
| `/reminders` | Upcoming agenda and History entry | New reminder, Today/Tomorrow/This week/Later groups, History | Agenda with drawer launch; overlay detail; stacked agenda | Selecting an item opens `/reminders/:id`; desktop presents it in a drawer | Preserve collection URL |
| `/reminders/:id` | Reminder detail and lifecycle | Message, local due date/time/timezone, status, Complete, Cancel, recovery | Contextual drawer; overlay; full-screen page | Canonical detail route; drawer is its desktop presentation | Additive deep link; collection remains compatible |
| `/approvals` | Approval decision inbox | Pending first, terminal history, intent/origin/scope/expiry, open for review | List plus drawer; overlay; stacked list | Selecting an item opens `/approvals/:id`; no list-level approval | Preserve collection URL and review-before-action |
| `/approvals/:id` | Approval review and decision | Consequence, scope, origin, expiry, execution state, disclosed metadata, Approve, Reject | Focus-managed drawer; overlay; full-screen page with sticky footer | Canonical detail route; actions only after opening | Additive deep link; unknown outcome remains explicit |
| `/notifications` | Lightweight activity inbox | Unread, History, summaries, unread count, open detail | List plus drawer; overlay; stacked list | Selecting an item opens `/notifications/:id` | Preserve collection URL |
| `/notifications/:id` | Notification detail and reminder handoff | Summary, Acknowledge, Mark reminder complete, Open reminder, source status | Drawer; overlay; full-screen page | Canonical detail route; dismissal never erases source state | Additive deep link; source failure remains visible |

The `/conversation` and `/conversation/:conversationId` routes remain the
focused conversation context. They own conversation history, search, filters,
and draft preservation; those controls do not move into the global management
route matrix or collapsed rail.

## Route-level direction

### Agents: mission-control library and individual Agent workspace

The Agents collection is a calm mission-control library. A card or row exposes
the Agent name, purpose, status, selected Persona, and current revision. Search
and status filtering are progressive enhancements and must not crowd the empty
or first-use state. The primary collection action is **Create agent**.

Creation is a progressive builder with these conceptual steps:

1. Identity and purpose.
2. Persona selection or creation.
3. Behavior and instructions.
4. Capabilities and tool grants.
5. Memory behavior and scope.
6. Review and Publish.

Inputs remain available when validation or a provider request fails. Nothing
becomes active before the final Review and Publish action. Publication must keep
the existing immutable revision and authorization semantics; this design does
not define an API shape.

An individual Agent uses the following information architecture, represented as
tabs on wide screens and focused subpages on mobile:

- **Overview:** purpose, Persona, effective capabilities, memory summary,
  current status, and the primary lifecycle action.
- **Behavior:** readable instructions and creation of a new immutable revision.
- **Capabilities:** tool grants and limitations in plain language; identifiers
  and technical policy detail are disclosed under Advanced.
- **Memory:** read/write scope, recall behavior, and policy explanation first;
  exact thresholds and budgets under Advanced.
- **Versions:** immutable revision history and an explicit two-version compare
  path.

Enable/disable actions name the lifecycle consequence and preserve historical
configuration. Tool and memory policy routes remain deep-link compatible but
visually belong to this Agent workspace.

### Personas: creative library and ephemeral preview

Personas are reusable expression profiles and should feel distinct from the
more operational Agent library without creating a new visual system. Cards show
name, description, active status, current revision, and assigned-Agent count.
Use typography, spacing, and `accent-subtle` for gentle personality; do not use
arbitrary gradients, illustrations, or third-party branding as interaction
signals.

The Persona editor separates:

- identity and description;
- behavioral guidance;
- a private test-prompt preview;
- review and immutable save.

Preview is ephemeral: it does not create a conversation, invoke tools, write
memory, or durably retain prompt/output content. A Persona detail view exposes
**Overview**, **Preview**, **Used by**, and **Versions**. Disabling a Persona
explains affected current Agents while preserving historical Agent references.
On mobile, editor and preview are consecutive full-screen steps with a clear
back path.

### Memory: human-readable knowledge workspace

Frame Memory as **What Aura knows**. The records view prioritizes readable
content, scope, lifecycle state, and recency. Search is prominent; scope,
status, kind, and advanced filters are progressively disclosed. On wide screens
use list plus contextual inspector. On mobile, selecting a record opens the
full-screen `/memory/:id` detail page.

Record detail groups:

- current memory and scope;
- why/how it was learned and supporting evidence;
- lifecycle and revision history;
- **Correct** as the primary corrective action;
- dispute, supersede, archive, or disable in a secondary action menu.

Manual creation begins with content and scope. Confidence, importance,
retention, and similar expert fields are Advanced controls. The review queue
remains inside Memory with a visible pending count and the actions **Approve**,
**Edit and approve**, and **Reject**. Candidate detail explains source and
proposed scope before any decision.

Memory settings prioritize first-use setup, active model health, and recovery
guidance. Provider inventory, model digests, and technical diagnostics are
secondary disclosures. Agent memory policy uses plain language first and exact
limits under Advanced.

### Reminders: agenda and history

Present upcoming reminders as an agenda grouped into **Today**, **Tomorrow**,
**This week**, and **Later**, with explicit local date, time, timezone, message,
and status. The primary action is **New reminder**. Completed and canceled
items belong in a separate History view rather than competing with upcoming
work in equal-weight columns.

Keep creation focused on message and due date/time. The structure may leave
room for future recurrence, but recurrence is not part of this direction. On
desktop, selecting an item opens `/reminders/:id` as a contextual drawer. On
mobile, `/reminders/:id` is a full-screen detail page. Completion and
cancellation remain explicit, reversible where possible, and visibly confirmed.

### Approvals: decision inbox

Approvals is a safety-first decision inbox. Pending requests appear first;
terminal history is separated. A list item explains the proposed action in
plain language, its origin, scope, expiry, and status. Do not offer approval at
the list row before review.

Detail explains:

- what Aura wants to do;
- the user-visible consequence and affected scope;
- the originating conversation or task;
- expiry and execution state;
- sanitized tool/version detail under disclosure.

Keep **Approve** and **Reject** in a sticky detail footer after `/approvals/:id`
is opened. Use a focus-managed desktop drawer and a full-screen mobile detail
page. Do not add bulk approval or list-level quick approval. Proposed,
authorized, attempted, completed, rejected, and unknown outcomes are distinct
in text. Unknown is never presented as success and is never retried silently.

### Notifications: lightweight activity inbox

Notifications are concise, durable-enough summaries, not the only place an
error is explained. Keep **Unread** and **History** views, preserve unread
counts in every sidebar mode, and route each item to relevant detail.

Reminder delivery detail at `/notifications/:id` supports **Acknowledge**,
**Mark reminder complete**, and **Open reminder**. Use a desktop drawer and a
full-screen mobile detail page. A blocking reminder failure is also shown on the source reminder;
notification dismissal must not erase the underlying state.

## Chosen and rejected alternatives

| Decision | Chosen direction | Rejected alternative | Reason |
| --- | --- | --- | --- |
| Global navigation | Grouped sidebar with Personalize, Automate, and Activity | Separate Manage/Activity hubs | Keeps destinations discoverable and matches the existing shell without adding an extra navigation layer. |
| Conversation controls | Contextual library within Conversations | Global history/search controls in the rail | Prevents tablet clipping and keeps global navigation stable. |
| Mobile navigation | Grouped overlay drawer | Tabs plus a hidden “More” menu | Preserves one mental model across viewports and keeps every destination reachable. |
| Desktop detail | Contextual drawer beside the focused canvas | Permanent three-pane layout | Retains readable content width and follows the approved two-pane foundation. |
| Agent creation | Progressive builder with Review and Publish | One long undifferentiated form | Makes policy and consequential configuration inspectable before activation. |
| Persona preview | Ephemeral private preview | Preview as a normal conversation | Prevents unintended memory, tools, history, and retention side effects. |
| Memory browsing | Human-readable records with optional inspector detail | Default audit table full of metadata | Keeps everyday correction approachable while retaining inspectability. |
| Reminder organization | Temporal agenda plus History | Equal-weight Upcoming/History columns | Gives due work a clear temporal hierarchy. |
| Approval action | Review-first decision inbox | Bulk or list-level quick approval | Preserves consequence visibility and reduces accidental authorization. |
| Visual tone | Warm, calm, refined semantic surfaces | Decorative gradients or copied reference branding | Extends the approved Aura foundation and keeps interaction meaning legible. |

## Traceability to approved guidance

| Direction in this document | Foundation source |
| --- | --- |
| Semantic roles, warm calm tone, typography, spacing, radii, elevation, motion, themes, responsive density, WCAG, and privacy disclosure | [Visual foundations](foundations.md) |
| Grouped shell, collapse/expand, current-location signal, tablet labels/tooltips, mobile scrim/focus return, controls, forms, drawers, dialogs, and notifications | [Components and interactions](components-and-interactions.md) |
| Loading, empty, authentication, permission, offline, retry, draft preservation, approval states, unknown outcomes, and sanitized diagnostics | [Failure and recovery states](failure-and-recovery-states.md) |
| Warm whitespace, focused composition, optional density, and rejection of copied branding, fixed three-pane defaults, and primary gradients | [Reference research](reference-research.md) |
| Document boundaries, source precedence, catalogue non-roadmap status, and Core/Observatory separation | [Design index](README.md) and repository architecture guidance |

## Implementation and review checklist

Implementers and reviewers should verify:

- all global destinations and pending/unread indicators remain reachable in
  expanded, rail, tablet, and mobile states;
- labels remain readable at normal and 200% zoom, and every icon has an
  accessible name and on-demand label;
- selected, disabled, loading, success, warning, danger, and info states have
  text or structural signals in addition to color;
- drawers and mobile detail surfaces manage focus and return it to their opener;
- drafts and partial results survive route changes and recoverable failures;
- loading, empty, authentication, permission, validation, offline, provider,
  conflict, and unknown-outcome states offer the safest available next action;
- technical, sensitive, and provider details are sanitized and disclosed only
  when appropriate;
- desktop, tablet, and mobile implementations preserve the route-level intent
  above without creating new contracts or cross-boundary dependencies.
