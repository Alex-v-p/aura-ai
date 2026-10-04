# Aura Web design foundation

> Status: approved design guidance for documentation and planning
>
> Scope: Aura Web first; reusable visual guidance may also inform Aura Wall and
> Observatory Web where their product boundaries permit it.

This directory is the durable design reference for Aura's web experiences. It
turns the approved Aura Web UI/UX Design Foundation plan into guidance that
designers, frontend workers, reviewers, and agents can use without loading the
entire product catalogue. It does not create frontend code, runtime APIs,
contracts, component identifiers, packages, or an implementation schedule.

## How to use this directory

Read this file first, then select the smallest document relevant to the task:

| Need | Reference |
| --- | --- |
| Tokens, theme behavior, layout, type, motion, or accessibility | [Foundations](foundations.md) |
| Shell, conversation UI, controls, overlays, or interaction states | [Components and interactions](components-and-interactions.md) |
| Errors, degraded operation, recovery, or sensitive diagnostics | [Failure and recovery states](failure-and-recovery-states.md) |
| Inspiration analysis, research, or source attribution | [Reference research](reference-research.md) |

The documentation index and [context routing](../context-routing.md) are the
discovery paths for this directory. A work item may narrow the context further.
When these documents conflict with an accepted ADR, versioned contract,
architecture boundary, or explicit owner decision, follow the repository's
source-of-truth precedence and record the conflict rather than silently
inventing a design exception.

## Authority and boundaries

The settled guidance in this directory is the approved visual and interaction
direction, not a backend or API contract. Names such as `canvas`, `accent`,
`focus-visible`, and `interrupted` are documentation conventions for future
implementation. They are not wire values, database fields, observable
component IDs, or a requirement to adopt a particular UI library.

The feature catalogue remains an envisioned catalogue, not a delivery roadmap.
It can identify likely surfaces (for example conversation, artifacts,
approvals, memory, and settings), but it does not expand this design work into
those product capabilities. Aura Observatory remains a separate product and
must not be treated as an admin route inside Aura Web.

## Design direction at a glance

- Calm, warm, and legible rather than decorative or theatrical.
- Dusty-rose semantic accent with separate status colors; pink never means
  failure.
- `system`, `light`, and `dark` theme choices with a system-aware first use.
- Collapsible sidebar plus a focused conversation canvas; contextual detail is
  a drawer, not a permanently required third pane.
- Open transcript: assistant content is readable on the canvas and user turns
  use a restrained tinted surface rather than dense chat bubbles.
- Every important state has a text explanation, a visible recovery path, and
  an accessible non-color signal.
- User-authored drafts and partial work are treated as valuable data and are
  preserved through recoverable failures whenever technically possible.

## Relationship to supplied visual references

The three supplied screenshots are inspiration only. The conclusions extracted
from them and the reasons for adopting or rejecting patterns are recorded in
[reference research](reference-research.md). No screenshot asset is copied,
embedded, or treated as a product requirement.
