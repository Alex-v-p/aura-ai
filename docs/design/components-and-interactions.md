# Aura Web components and interactions

This is a behavior guide for the future Aura Web client. It describes visual
responsibilities and user-facing states without creating component names,
framework code, API payloads, or stable observability identifiers.

## Shell and navigation

The shell has a collapsible sidebar, a focused content canvas, and optional
contextual drawers. The sidebar provides the primary route groups (for
example conversations, agents, artifacts, memory, automations, integrations,
and settings) only when those product capabilities are available. Navigation
must not imply that an envisioned feature is already implemented.

The sidebar has a visible collapse/expand control, a compact mobile trigger,
current-location text, and keyboard support. On mobile, it is an overlay with a
scrim; closing it returns focus to the trigger. On tablet, a compact rail may
show icons, but tooltips or an accessible label must expose the route name.
The content canvas never becomes unusably narrow just to keep navigation
visible.

## Conversation list

Conversation lists expose a clear new-conversation action, search when the
collection is large, and stable titles with a useful secondary timestamp or
status. Loading uses a structural placeholder; empty states explain what will
appear and offer the next action; failed loading preserves navigation and
offers retry. Destructive actions require an explicit confirmation and never
silently discard a draft.

Selection is communicated by surface and text/icon state, not color alone.
Keyboard users can move through the list, open an item, and reach item actions
without needing a pointer-only overflow menu.

## Transcript

Use an open transcript. Assistant content sits directly on the canvas with
clear role and time/context metadata. User turns may use `surface-subtle` as a
restrained tinted surface; avoid nesting every turn in a heavy bubble.

Long Markdown, headings, lists, links, citations, tables, code, artifacts,
tool activity, approvals, and partial results keep their own readable rhythm.
Code blocks provide language context and a copy action with success feedback.
Links identify their destination; downloads identify file type and size where
known. Do not expose hidden model reasoning or unsanitized provider payloads.

Rich content is an explicit trust boundary. Sanitize and escape user-, model-,
tool-, provider-, and externally sourced content; never execute HTML or
scripts from transcript content. Allowlist URL schemes, isolate previews and
downloads, and visually and semantically separate untrusted content from
trusted system and approval chrome. Content itself must never trigger tools,
change permissions, or confirm an action; those transitions require an
explicit authorized interaction through trusted UI.

Streaming content has a stable working indicator and a stop action. When work
ends, the transcript exposes completion or interruption in text. The user's
composer remains available unless a policy or workflow explicitly disables it.

## Composer

The composer is the primary focused action. It provides an always-visible
multiline input, an accessible label or prompt, attachment affordances when
supported, model or agent selection when available, and a send/stop control
whose label changes with the run state. A draft survives route changes,
reconnects, validation failures, and interrupted generation where technically
possible.

The composer must make capability boundaries legible: unsupported file types,
permission limitations, provider availability, and consequential actions are
explained before the user loses work. Keyboard submit behavior is discoverable
and does not prevent an intentional newline. Voice input, if later provided,
has visible recording, permission, and cancellation states.

## Controls and rich content

Use native or equivalently accessible controls with visible labels for actions
that are not universally recognizable. Menus close on Escape and retain a
logical focus path. Selectors show the current choice and any relevant
limitation before opening. Forms associate labels, descriptions, and errors
with fields; validation does not rely on submission-time color changes alone.

Cards group a small, coherent action or result. They do not become a second
navigation system or hide critical copy in hover-only affordances. A card's
action target is keyboard reachable and its selected/disabled states are
explicit.

## Drawers, dialogs, and notifications

Drawers hold contextual detail such as citations, artifact metadata, run
activity, or settings. They overlay the canvas on narrow widths and may sit
beside it on wide screens. They announce their heading, trap focus while
modal, provide a close action, and return focus to the opener.

Dialogs are reserved for decisions that need attention, such as confirmation,
permission, or unsaved-work risk. Use inline guidance for ordinary validation.
Destructive dialog actions name the consequence and provide a safe cancel
path.

Notifications are concise, non-blocking summaries with an accessible live
region and a route to relevant detail. They must not be the only place an
error is described, and they should not disappear before a keyboard or screen
reader user can understand the next action.

## Component state matrix

| Surface | Default | Hover / active | Focus-visible | Selected | Disabled | Loading | Success / warning / error |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Navigation item | Label and icon | Surface emphasis | Unobscured focus ring plus label | Current route marker and text | Explain unavailable capability | Preserve label; show local progress | Inline status where route needs attention |
| Conversation row | Title and metadata | Row emphasis and actions | Row focus ring | Surface plus current marker | Explain access or archive state | Skeleton matching row shape | Status text and retry where needed |
| Composer | Empty or drafted input | Action emphasis | Input and controls have distinct focus | N/A | Explain why sending is unavailable | Stop action and stable progress text | Inline validation, interruption, or sent confirmation |
| Primary button | Text and icon if useful | Accent hover | Visible focus ring | Pressed state | Label remains readable | Progress label; prevent duplicate submit | Confirmation or actionable error |
| Selector / menu | Current value | Item emphasis | Menu and item focus | Check plus text | Explain unavailable option | Preserve current value | Warning or validation adjacent to control |
| Message action | Explicit accessible label | Icon and label emphasis | Visible focus | Toggle state with text/tooltip | Explain permission or context | Disable only the affected action | Copy/saved confirmation or retry |
| Card / artifact | Title, summary, action | Surface emphasis | Whole action path visible | Surface marker plus text | Explain access | Placeholder preserving layout | Result metadata or recovery action |
| Drawer / dialog | Heading and purpose | Action emphasis | Managed focus | N/A | N/A | Keep heading; show progress | Clear decision, recovery, or cancellation |
| Status / notification | Text and semantic icon | Optional dismissal emphasis | Dismiss control focus | N/A | N/A | Live progress update | Text, icon, and recovery—not color alone |

## Run-oriented display states

These labels describe what a user sees during a run; they are not backend
contracts or required enum values:

`idle` → `preparing` → `working` → `completed`

Recoverable branches include `waiting-for-input`, `awaiting-approval`,
`reconnecting`, `interrupted`, `canceled`, `timed-out`, and `failed`. Every
branch preserves the conversation context and identifies the safest available
next action. The UI must distinguish a provider failure from a user validation
error, even if both offer retry.
