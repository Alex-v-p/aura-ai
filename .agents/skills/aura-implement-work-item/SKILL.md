---
name: aura-implement-work-item
description: Implement an approved Aura work item within one assigned ownership zone and produce a validated handoff.
---

# Implement an Aura work item

1. Resolve `AURA_WORK_ITEM`, validate it, and confirm your role, ownership zone, allowed paths, forbidden paths, and acceptance criteria.
2. Read the nearest `AGENTS.md` and only the authoritative sources relevant to the assignment.
3. Make the smallest complete change; do not perform adjacent cleanup or widen contracts, migrations, baselines, or governance scope.
4. Run every assigned check and validate changed paths with `python3 tooling/architecture/validate-changed-paths.py`.
5. Produce a schema-valid implementation handoff from `work-items/templates/implementation-handoff.yaml`, recording the actual model, effort, tier, files, checks, decisions, and unresolved risks.

Stop when scope is contradictory or requires an unapproved architecture decision.
