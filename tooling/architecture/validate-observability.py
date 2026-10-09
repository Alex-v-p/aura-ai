#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from common import ROOT, emit, read_yaml, resolve_work_item, validate_document

ManifestEntry = tuple[Path, Any]


def component_manifests() -> list[ManifestEntry]:
    manifests: list[ManifestEntry] = []
    patterns = [
        "services/*/resources/component-manifests/*.yaml",
        "services/*/resources/component-manifests/*.yml",
    ]
    for pattern in patterns:
        for path in ROOT.glob(pattern):
            document = read_yaml(path)
            manifests.append((path, document))
    return manifests


def component_catalog(
    manifests: list[ManifestEntry] | None = None,
) -> dict[str, ManifestEntry]:
    """Build the valid-ID lookup used by work-item-specific checks."""

    if manifests is None:
        manifests = component_manifests()
    catalog: dict[str, ManifestEntry] = {}
    for path, manifest in manifests:
        if not isinstance(manifest, dict):
            continue
        component = manifest.get("component")
        if not isinstance(component, dict):
            continue
        identifier = component.get("id")
        if isinstance(identifier, str) and identifier.strip():
            catalog[identifier] = (path, manifest)
    return catalog


def catalogue_errors(
    manifests: list[ManifestEntry] | dict[str, ManifestEntry],
) -> list[str]:
    """Check the complete manifest catalogue independently of a work item."""

    entries = (
        manifests
        if isinstance(manifests, list)
        else [(path, manifest) for path, manifest in manifests.values()]
    )
    errors: list[str] = []
    seen: set[str] = set()
    for path, manifest in entries:
        location = str(path.relative_to(ROOT))
        if not isinstance(manifest, dict):
            errors.append(f"{location}: manifest must be a mapping")
            continue
        component = manifest.get("component")
        if not isinstance(component, dict):
            errors.append(f"{location}: component metadata is required")
            continue
        identifier = component.get("id")
        if not isinstance(identifier, str) or not identifier.strip():
            errors.append(f"{location}: component.id is required")
        elif identifier in seen:
            errors.append(f"{location}: duplicate component id {identifier}")
        else:
            seen.add(identifier)
        for field in ("version", "owner", "category"):
            if not isinstance(component.get(field), str) or not component[field].strip():
                errors.append(f"{location}: component.{field} is required")
        for section in ("input", "output"):
            if not isinstance(manifest.get(section), dict):
                errors.append(f"{location}: {section} metadata is required")
        metrics = manifest.get("metrics")
        if (
            not isinstance(metrics, list)
            or not metrics
            or not all(isinstance(metric, str) and metric.strip() for metric in metrics)
        ):
            errors.append(f"{location}: at least one non-empty metric is required")
        if (
            not isinstance(manifest.get("capture_policy"), str)
            or not manifest["capture_policy"].strip()
        ):
            errors.append(f"{location}: capture_policy is required")
    if not entries:
        errors.append("No observable component manifests exist in the component catalogue.")
    return sorted(set(errors))


def work_item_errors(path: Path) -> list[str]:
    item, errors = validate_document(path, "work-item.schema.json")
    if errors:
        return errors
    config = item.get("observability", {}) if isinstance(item, dict) else {}
    if not config.get("applicable"):
        return []
    manifests = component_manifests()
    catalog = component_catalog(manifests)
    errors.extend(catalogue_errors(manifests))
    if not config.get("affected_components"):
        errors.append("Affected observable components are required.")
    if not config.get("required_metrics") and not config.get("required_evaluators"):
        errors.append("Each affected runtime component needs required metrics or evaluators.")
    if config.get("capture_policy") is None:
        errors.append("An applicable observability change requires an explicit capture_policy.")
    metrics: set[str] = set()
    evaluators: set[str] = set()
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
    return sorted(set(errors))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    args = parser.parse_args()
    if args.work_item:
        path = resolve_work_item(args.work_item)
        if path is None or not path.is_file():
            raise SystemExit(emit("failed", "observability", ["A valid work item is required."]))
        item, item_errors = validate_document(path, "work-item.schema.json")
        config = item.get("observability", {}) if isinstance(item, dict) else {}
        if item_errors:
            raise SystemExit(emit("failed", "observability", item_errors))
        if not config.get("applicable"):
            raise SystemExit(
                emit(
                    "skipped", "observability", ["Work item declares observability not applicable."]
                )
            )
        errors = work_item_errors(path)
        raise SystemExit(
            emit(
                "failed" if errors else "passed",
                "observability",
                errors or ["Observability obligations are declared."],
            )
        )
    errors = catalogue_errors(component_manifests())
    raise SystemExit(
        emit(
            "failed" if errors else "passed",
            "observability",
            errors or ["Observable component catalogue is complete."],
        )
    )
