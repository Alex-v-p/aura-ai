---
name: aura-plan-work
description: Plan a nontrivial Aura repository change as a validated work item with bounded ownership and required reviews.
---

# Plan Aura work

1. Read the request, `docs/architecture/aura-ai-architecture-handover.md`, applicable ADRs/specifications, and existing contracts.
2. Use `repository_mapper` when existing implementation is involved; ground ownership and impact in files and symbols.
3. Classify the change and record ownership zones, allowed and forbidden paths, contracts, migrations, observability, privacy, security, evaluation, and baseline impact.
4. Create `work-items/active/<id>.yaml` from the template and validate it with `python3 tooling/architecture/validate-work-item.py`. When no item is active, make this a single-file `apply_patch`; the bootstrap guard permits no unrelated path in that patch.
5. Select the minimum implementation, review, and integration roles required by the activation matrix in `docs/development/codex-agent-system.md`.
6. Stop with `ADR_REQUIRED` when the architecture or an unresolved decision does not authorize a material choice.

Do not treat feature-catalogue entries as implementation approval or priority.
