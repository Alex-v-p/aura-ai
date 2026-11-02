# Work-item process

Use one YAML work item for every nontrivial write. Start from `work-items/templates/work-item.yaml`, assign a stable `AURA-NNNN` id, describe the outcome, name authoritative sources and ownership zones, and explicitly bound allowed and forbidden paths. Record contract, migration, observability, privacy, evaluation, model-tier, parallelism, baseline, review, and human-approval requirements.

When no work item is active, the scope hook permits one bootstrap operation: an `apply_patch` call that only adds a single `work-items/active/AURA-NNNN.yaml` file. It does not permit unrelated files in the same patch. Validate and select that record before any implementation write.

Select a work item with:

```bash
export AURA_WORK_ITEM=AURA-0127
```

When environment injection is unavailable, an untracked repository-root `.aura-work-item` may contain the id. Never commit that developer-specific pointer.

Validate before implementation:

```bash
python3 tooling/architecture/validate-work-item.py
```

Workers return an implementation handoff, reviewers return a verdict, and the Integration Maintainer returns an integration report. Templates and schemas live under `work-items/`. A writing turn may report completion only with a schema-valid handoff or integration report whose check results name every command in `required_checks`. Work progresses through `draft`, `approved`, `in_progress`, `review`, and `integrated`; blocked work records the unresolved decision. Move integrated records to `archived/`.

## Authorized checkpoints

Per-commit user authorization is not required for a focused local checkpoint.
Create it only after a coherent reviewed and validated slice,
such as work-item completion, an ownership-zone handoff, a pre-integration or
other risky transition, or prolonged work with a coherent validated slice. Do
not commit broken, unrelated, secret-bearing, temporary, generated-only, or
noisy micro-changes. Workers do not commit unreviewed implementations. When
push is separately explicitly authorized, push each checkpoint normally to the
configured upstream or named destination. Never force-push or rewrite history
without separate explicit authorization.

Allowed paths narrow machine-readable path ownership. They never widen it. Critical paths require `human_approval_required: true` and an authorized change class. Contract and protected baseline changes also require their explicit permission fields.
