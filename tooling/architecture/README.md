# Architecture enforcement

These manifests and Python entrypoints are the machine-readable boundary for Aura repository governance. Every command emits JSON and exits nonzero on a violation. Checks whose product structure does not exist return `status: skipped` with a reason; that is not a substantive pass.

`modules.yaml` is the integrated registry of real module roots. Each entry has a
repository-relative root, a supported non-empty kind, a public API marker, and
explicit module dependencies. Python entries also identify their import module
names and are checked against the actual Core import graph, including deliberate
public-surface cycles.
`validate-module-registry.py` fails closed when a root is missing, escapes the
repository, has an external public API, is duplicated, or names an unknown
dependency. Every Python file under the registered Core package namespace must
be assigned to exactly one source set, and relative imports are resolved from
the importing module identity. It also validates dependency-rule metadata and
emits JSON-safe diagnostics for malformed or missing CLI inputs. `dependency-rules.yaml`
contains the corresponding public-API, provider-direction, service-isolation,
and frontend product-boundary rules.

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
python3 tooling/architecture/validate-module-registry.py
python3 -m unittest discover -s tooling/architecture/tests -v
```

PyYAML is required to parse work items and manifests. No ordinary check requires network access.
