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

Do not add product behavior, weaken checks, or update protected baselines during integration.
