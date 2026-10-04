---
name: aura-integrate-change
description: Integrate reviewed Aura worker commits, run aggregate checks, and produce a structured integration report.
---

# Integrate Aura work

1. Validate the work item, implementation handoffs, required reviews, and approved commits.
2. Reject unresolved blocking findings and return semantic conflicts to the owning worker.
3. Combine reviewed commits, resolve only mechanical conflicts, and regenerate only authorized outputs.
4. Run aggregate required checks and changed-path validation in the integration worktree.
5. Produce a schema-valid report from `work-items/templates/integration-report.yaml`.

## Checkpoint policy

The Integration Maintainer may create an integration checkpoint only when the
current user or task explicitly authorizes commits and the combined change is
a coherent, reviewed, validated slice. Appropriate checkpoints include a
completed work item, an ownership-zone handoff, the point before or after a
risky integration transition, and prolonged integration once a coherent
validated slice exists. Do not commit broken, unreviewed, unrelated,
secret-bearing, temporary, generated-only, or noisy micro-changes. Workers do
not commit unreviewed implementations; semantic review remains before commit
and integration.

When the current user or task explicitly authorizes pushing as well, push each
intentional checkpoint normally to the configured upstream or named
destination after validation. Authorization is specific to the current task,
not standing authority for later work. Never force-push or rewrite history.

Do not add product behavior, weaken checks, or update protected baselines during integration.
