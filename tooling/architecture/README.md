# Architecture enforcement

These manifests and Python entrypoints are the machine-readable boundary for Aura repository governance. Every command emits JSON and exits nonzero on a violation. Checks whose product structure does not exist return `status: skipped` with a reason; that is not a substantive pass.

Common commands:

```bash
python3 tooling/architecture/validate-work-item.py work-items/templates/work-item.yaml
python3 tooling/architecture/validate-changed-paths.py --work-item work-items/archived/AURA-0001.yaml
python3 tooling/architecture/validate-repository-shape.py
python3 tooling/architecture/validate-import-boundaries.py
python3 tooling/architecture/validate-service-isolation.py
python3 tooling/architecture/validate-frontend-boundaries.py
python3 tooling/architecture/validate-contract-changes.py --work-item work-items/archived/AURA-0001.yaml
python3 tooling/architecture/validate-generated-sync.py
python3 tooling/architecture/validate-observability.py --work-item work-items/archived/AURA-0001.yaml
python3 tooling/architecture/validate-baseline-protection.py --work-item work-items/archived/AURA-0001.yaml
python3 -m unittest discover -s tooling/architecture/tests -v
```

PyYAML is required to parse work items and manifests. No ordinary check requires network access.
