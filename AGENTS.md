# Aura AI repository guidance

- Read `docs/architecture/aura-ai-architecture-handover.md`, applicable ADRs, and the nearest nested `AGENTS.md` before changing product code.
- Treat the primary thread as Delivery Lead: scope and coordinate work, but delegate production implementation to the appropriate custom worker.
- Require a valid `AURA_WORK_ITEM` for nontrivial writes. Stay inside its `allowed_paths`, outside `forbidden_paths`, and produce the required structured handoff.
- Use public module APIs and versioned contracts. Preserve Core/Observatory isolation, service-owned data, provider direction, thin entrypoints, and frontend product boundaries.
- Do not add top-level directories, deployable services, packages, frameworks, cross-boundary dependencies, or architectural exceptions without an accepted ADR or explicit owner decision. Return `ADR_REQUIRED` when authorization is missing.
- Treat the envisioned feature catalogue as context, not implementation scope or priority.
- Do not weaken governance, tests, evaluation thresholds, golden results, or protected baselines to make checks pass.
- Run every check listed by the active work item. Use the plan → implement → review → integrate lifecycle described in `docs/development/codex-agent-system.md`.

## Code review rules

- Block correctness, architecture, contract, security, privacy, observability, and test failures with file-and-symbol evidence.
- Reviewers report findings and never repair the code they review. The owning worker corrects findings and the same reviewer verifies them.
