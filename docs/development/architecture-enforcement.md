# Architecture enforcement

Aura applies four layers:

1. `AGENTS.md`, custom-agent profiles, and skills guide behavior.
2. Hooks give immediate scope and completion feedback during a Codex turn.
3. Machine-readable manifests and Python validators reject invalid repository state.
4. Branch protection, required checks, and CODEOWNERS provide the final merge boundary when configured.

The manifests under `tooling/architecture/` define modules that actually exist, path ownership, critical paths, and dependency rules. A work item may narrow these rules. Governance, architecture, contracts, golden results, baselines, security policies, and CI are protected from ordinary workers.

Run the full current suite with:

```bash
python3 -m unittest discover -s tooling/architecture/tests -v
python3 tooling/architecture/validate-repository-shape.py
python3 tooling/architecture/validate-import-boundaries.py
python3 tooling/architecture/validate-service-isolation.py
python3 tooling/architecture/validate-frontend-boundaries.py
python3 tooling/architecture/validate-contract-changes.py
python3 tooling/architecture/validate-generated-sync.py
python3 tooling/architecture/validate-observability.py
python3 tooling/architecture/validate-baseline-protection.py
```

A missing subsystem is reported as `skipped` with a reason. This is intentionally distinct from passing an architecture check. Import, frontend, generated-client, and instrumentation enforcement becomes substantive as their source roots and manifests appear.

Hooks cannot see every hosted or specialized tool and may be untrusted or time out. Command rules govern commands outside the sandbox, not arbitrary file edits. Neither mechanism replaces validators or protected-branch settings.
