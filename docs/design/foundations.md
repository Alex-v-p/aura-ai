# Aura Web visual foundations

This document defines the semantic visual foundation for Aura Web. Values are
implementation-ready guidance, but remain library-neutral and do not define a
runtime theme API.

## Themes

Theme selection has three user-facing choices: `system`, `light`, and `dark`.
On first use, `system` follows the browser's `prefers-color-scheme` value.
Selecting light or dark is an explicit override and persists for the user.
While `system` is selected, a live operating-system theme change updates Aura;
while an explicit override is selected, operating-system changes do not.
Theme changes should not reload the page, lose a draft, or move keyboard focus.

Use semantic roles in components. A component consumes `surface` or `text`,
not a hard-coded hex value. The token names below are documentation names and
may be mapped to the eventual frontend styling system.

### Core semantic tokens

| Role | Light | Dark | Use and constraint |
| --- | --- | --- | --- |
| `canvas` | `#FCF9FA` | `#121012` | Main page background |
| `surface` | `#FFFFFF` | `#191619` | Cards, panels, and primary controls |
| `surface-subtle` | `#F7F1F3` | `#211D20` | Low-emphasis grouping and user-turn tint |
| `surface-raised` | `#FFFFFF` | `#292428` | Menus, dialogs, drawers, and raised controls |
| `text` | `#2A2225` | `#F7F0F3` | Primary readable text |
| `text-muted` | `#6F6267` | `#B9ABB1` | Supporting text; never the only copy for a required action |
| `border` | `#DED4D8` | `#40383D` | Decorative dividers and grouping only; never the required boundary of a control |
| `control-border` | `#8A747D` | `#7B6F76` | Required field, control, and meaningful non-text boundary |
| `accent` | `#A84F70` | `#E4A3B9` | Primary action and selected affordance |
| `accent-hover` | `#8E3F5E` | `#F0B6C8` | Hover or pressed emphasis |
| `accent-subtle` | `#F8DDE6` | `#432735` | Non-destructive accent background |
| `on-accent` | `#FFFFFF` | `#2A111B` | Text and icons placed directly on accent |

The palette is a starting semantic contract for design work. Text, controls,
focus indicators, and meaningful boundaries must be tested against the actual
adjacent surface when implemented. Do not use a muted token for essential
information solely because it looks quiet.

As a palette sanity check, the intended primary pairings currently measure
above WCAG AA thresholds: light `text` on `canvas` is approximately 14.8:1,
light `text-muted` on `canvas` 5.6:1, light `accent` on white 5.2:1, dark
`text` on `canvas` 16.9:1, dark `text-muted` on `canvas` 8.6:1, and dark
`accent` on `canvas` 9.2:1. The `control-border` pairings are at least 3.86:1
in light mode and 3.18:1 in dark mode across the defined canvas, surface,
subtle, and raised contexts. Required control boundaries use `control-border`;
`border` is reserved for decorative dividers and grouping. These are reference
calculations, not a waiver from testing borders, focus indicators, disabled
states, tinted surfaces, and actual text sizes in the implemented UI.

### Status roles

Status communicates meaning through color plus a text label, icon, or other
non-color signal. Pink is never the error role.

| Role | Light default | Dark default | Meaning |
| --- | --- | --- | --- |
| `success` | `#287A52` | `#78D6A2` | Completed or healthy |
| `warning` | `#956A16` | `#E4BE68` | Attention or a reversible limitation |
| `danger` | `#B33A4A` | `#F29AA5` | Failed, blocked, or destructive consequence |
| `info` | `#326FA8` | `#8FC4F2` | Neutral explanation or progress context |

Use a tinted status surface and an on-surface status text variant rather than
placing saturated status color behind long paragraphs. Status color must meet
the intended text and non-text contrast requirements in the final context.

## Typography

Use Inter Variable when the web implementation is ready, self-hosted under the
application's asset policy. Until then, use a system sans fallback such as
`Inter, ui-sans-serif, system-ui, sans-serif`; the fallback is part of the
design intent, not a requirement to add a font dependency in this work item.

Use a readable text measure (normally 60–75 characters for long prose), 1.5
line height for body copy, and visibly larger headings without relying on thin
weight or low contrast. Code, identifiers, and tabular values may use a
system monospace fallback. Avoid all-caps body copy and avoid using weight as
the only distinction between states.

## Spacing, shape, and elevation

- Use a 4 px base spacing grid. Common steps are 4, 8, 12, 16, 24, 32, and
  48 px; choose the smallest step that preserves a clear grouping.
- Use radius tiers of 8 px (fields and compact controls), 12 px (cards and
  menus), 16 px (composer and larger panels), and 24 px (prominent modal or
  welcome surfaces). Do not round every text block.
- Prefer restrained 1 px borders and very soft elevation. Elevation separates
  a drawer, menu, or dialog from its surface; it should not be the only signal
  of hierarchy.
- Avoid decorative gradients as primary UI. Any future illustration or
  celebration is supplemental and must not carry interaction meaning.

## Motion and interaction

Use 120–200 ms for ordinary hover, pressed, disclosure, and theme transitions.
Long-running work communicates through stable progress content rather than
indefinite animation alone. Respect `prefers-reduced-motion: reduce` by
removing nonessential movement, parallax, and animated decorative effects;
retain an instantaneous state change and an accessible text update.

Every interactive element has `default`, `hover`, `active`,
`focus-visible`, `selected`, `disabled`, `loading`, `success`, `warning`, and
`error` guidance where the state applies. Keyboard focus is visible, remains
unobscured, and is not represented by color alone.

## Responsive behavior and density

The primary shell is an adaptive two-pane composition:

| Viewport | Navigation behavior | Content behavior |
| --- | --- | --- |
| Mobile | Sidebar becomes an overlay opened by an explicit control | Conversation fills the viewport; contextual detail is a full-height drawer |
| Tablet | Sidebar becomes a compact rail or collapses to icons with labels available on demand | Conversation remains primary; drawers overlay content |
| Wide desktop | Full collapsible sidebar stays available | Focused conversation canvas with optional contextual drawer |

The default density is calm and breathable. Dense lists, tool traces, and
diagnostic detail belong in optional detail views. Do not require a fixed
three-pane layout to use core conversation tasks. Support zoom and text
reflow without clipping primary actions.

## Accessibility baseline

- Target WCAG 2.2 AA in the implemented UI: 4.5:1 for normal text, 3:1 for
  large text, and 3:1 for meaningful non-text UI boundaries or indicators.
- Keep interactive targets at least 24 × 24 CSS px or provide sufficient
  spacing; prefer larger targets on touch surfaces.
- Preserve a logical heading, landmark, and reading order. Drawers and dialogs
  manage focus, announce their title, and return focus to the invoking control
  on close.
- Use labels and text explanations for errors and status. Do not make hue,
  saturation, or position the sole indication of meaning.
- Announce dynamic progress, reconnecting, and completion as status messages
  without stealing focus from the user's current task.
- Provide keyboard operation for navigation, composer actions, menus, dialogs,
  code copying, retry, cancellation, and theme selection.

## Privacy-sensitive presentation

The visual foundation does not change Aura's privacy or permission policy.
Future UI must redact secrets, tokens, private payloads, hidden reasoning, and
raw provider diagnostics by default. A disclosure control may expose useful,
sanitized operational context only when the user is authorized and the
surface's privacy expectations are clear.
