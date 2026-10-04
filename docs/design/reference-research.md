# Aura Web reference research

This document records the visual references and external guidance used for the
Aura Web design foundation. References inform patterns; they do not override
Aura's architecture, privacy policy, frontend boundaries, or approved scope.

Research access date: 2026-10-04.

## Supplied screenshots

The three user-supplied screenshots were inspected in the conversation and are
not copied into this repository.

### ChatGPT reference

The dark ChatGPT Work screen demonstrates a focused central composer, a
restrained left navigation, prominent work-mode context, and a clear primary
action. Aura adopts the focused composition and the idea that navigation can
collapse around the task. Aura does not copy the branding, labels, model
selector, logo, or exact layout. The screenshot's dense navigation and
product-specific integrations are not Aura requirements.

### Claude reference

The light Claude screen demonstrates warm off-white surfaces, generous
whitespace, a calm welcome state, a simple composer, and a navigation rail
that remains legible without visual noise. Aura adopts the warmth, whitespace,
and approachable tone. Aura does not copy the Claude wordmark, iconography,
copy, typography treatment, or branded color.

### LobeChat reference

The LobeChat screen demonstrates a dark, information-dense workspace with an
assistant list, topic history, tool/knowledge controls, and visible detail
surfaces. Aura reserves this density for optional detail views such as run
activity, citations, artifacts, and tool context. The default Aura Web shell
does not require a fixed three-pane composition. Aura does not copy its
illustrations, gradients, icons, labels, or application chrome.

### Explicit non-adoptions

- No copied third-party image assets or branding.
- No fixed three-pane default that makes conversation narrow.
- No decorative gradient as the primary interaction surface.
- No screenshot-derived feature requirement or assumption that a referenced
  integration exists in Aura.

## Authoritative accessibility and platform guidance

These sources are cited for principles and implementation review; the design
documents do not claim that citing them is a substitute for testing the final
UI.

| Source | Relevant guidance | URL |
| --- | --- | --- |
| W3C WCAG 2.2, 1.4.3 Contrast (Minimum) | 4.5:1 normal text; 3:1 large text | https://www.w3.org/TR/WCAG22/#contrast-minimum |
| W3C WCAG 2.2, 1.4.11 Non-text Contrast | 3:1 for meaningful controls and graphics | https://www.w3.org/TR/WCAG22/#non-text-contrast |
| W3C WCAG 2.2, 3.3.1 Error Identification | Identify the affected item and describe the error in text | https://www.w3.org/TR/WCAG22/#error-identification |
| W3C WCAG 2.2, 4.1.3 Status Messages | Announce status without unnecessary focus movement | https://www.w3.org/TR/WCAG22/#status-messages |
| W3C WCAG 2.2, 2.5.8 Target Size (Minimum) | 24 × 24 CSS px target or adequate spacing | https://www.w3.org/TR/WCAG22/#target-size-minimum |
| W3C WCAG 2.2, 2.4.11 Focus Not Obscured | Keep focused controls visibly available | https://www.w3.org/TR/WCAG22/#focus-not-obscured-minimum |
| W3C WCAG 2.2, 1.4.1 Use of Color | Do not use color as the sole meaning signal | https://www.w3.org/TR/WCAG22/#use-of-color |
| MDN, `prefers-color-scheme` | Detect the user's system light/dark preference | https://developer.mozilla.org/en-US/docs/Web/CSS/@media/prefers-color-scheme |
| Microsoft, Guidelines for Human-AI Interaction | Set expectations, support correction, feedback, and recovery | https://www.microsoft.com/en-us/research/project/guidelines-for-human-ai-interaction/ |
| Angular Material, Theming | Reference for a future implementation's theme composition; not a library mandate | https://material.angular.dev/guide/theming |

The palette and state guidance in [foundations](foundations.md) and [failure
and recovery states](failure-and-recovery-states.md) apply these principles:
contrast is verified in context, errors have textual identification, live
updates do not steal focus, targets remain operable, and colors do not carry
meaning alone.

## Future research guardrails

When adding references, record the URL, access date, and the specific pattern
being evaluated. Treat commercial product screenshots as inspiration rather
than requirements. Do not add external assets, embedded tracking, or network
dependencies to satisfy a documentation reference.
