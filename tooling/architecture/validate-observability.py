#!/usr/bin/env python3
from __future__ import annotations

import argparse

from common import ROOT, emit, read_yaml, resolve_work_item, validate_document


def component_catalog():
    manifests = {}
    patterns = [
        "services/*/resources/component-manifests/*.yaml",
        "services/*/resources/component-manifests/*.yml",
    ]
    for pattern in patterns:
        for path in ROOT.glob(pattern):
            document = read_yaml(path)
            component = document.get("component", {}) if isinstance(document, dict) else {}
            identifier = component.get("id")
            if identifier:
                manifests[identifier] = (path, document)
    return manifests

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    args = parser.parse_args()
    path = resolve_work_item(args.work_item)
    if path is None or not path.is_file():
        raise SystemExit(emit("failed", "observability", ["A valid work item is required."]))
    item, errors = validate_document(path, "work-item.schema.json")
    config = item.get("observability", {}) if isinstance(item, dict) else {}
    if errors:
        raise SystemExit(emit("failed", "observability", errors))
    if not config.get("applicable"):
        raise SystemExit(emit("skipped", "observability", ["Work item declares observability not applicable."]))
    if not config.get("affected_components"):
        errors.append("Affected observable components are required.")
    if not config.get("required_metrics") and not config.get("required_evaluators"):
        errors.append("Each affected runtime component needs required metrics or evaluators.")
    if config.get("capture_policy") is None:
        errors.append("An applicable observability change requires an explicit capture_policy.")
    catalog = component_catalog()
    if not catalog:
        errors.append("No observable component manifests exist for an applicable runtime change.")
    else:
        metrics = set()
        evaluators = set()
        for identifier in config.get("affected_components", []):
            if identifier not in catalog:
                errors.append(f"Affected component has no manifest: {identifier}")
                continue
            path, manifest = catalog[identifier]
            component = manifest.get("component", {})
            if not component.get("version"):
                errors.append(f"{path.relative_to(ROOT)}: component version is required")
            metrics.update(manifest.get("metrics", []))
            evaluators.update(manifest.get("evaluators", []))
        missing_metrics = set(config.get("required_metrics", [])) - metrics
        missing_evaluators = set(config.get("required_evaluators", [])) - evaluators
        if missing_metrics:
            errors.append(f"Component manifests lack required metrics: {sorted(missing_metrics)}")
        if missing_evaluators:
            errors.append(f"Component manifests lack required evaluators: {sorted(missing_evaluators)}")
    raise SystemExit(emit("failed" if errors else "passed", "observability", errors or ["Observability obligations are declared."]))
