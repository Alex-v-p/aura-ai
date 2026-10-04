---
name: aura-review-change
description: Review an Aura change against its work item and return an evidence-backed structured verdict without editing it.
---

# Review an Aura change

1. Read the work item, authoritative sources, and actual diff; do not rely on the worker summary alone.
2. Trace affected behavior and perform the checks assigned to your reviewer role.
3. Report only evidence-backed findings with severity, rule, files, symbols, impact, and smallest acceptable correction.
4. Return a schema-valid verdict based on `work-items/templates/review-verdict.yaml` and your role's allowed verdict set.

Never modify the reviewed code. After a correction, verify the same finding again.
