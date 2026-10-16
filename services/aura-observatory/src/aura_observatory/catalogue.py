"""Closed Observatory component-manifest application boundary.

Only packaged Observatory resources reach this module.  Parsing, validation,
cycle detection, canonical hashing, and synchronization orchestration are kept
out of entrypoints; persistence is supplied through the narrow
``ComponentCataloguePort``.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Protocol, cast

import yaml

_COMPONENT_ID = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_IDENTIFIER = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_MAX_MANIFESTS = 256
_MAX_ITEMS = 128
_MAX_DEPENDENCIES = 64
_MANIFEST_KEYS = frozenset(
    {
        "component", "input", "output", "dependencies", "metrics",
        "metric_definitions", "metric_semantics", "evaluators", "capture_policy",
        "retention", "cardinality", "export",
    }
)


class ComponentCataloguePort(Protocol):
    """Narrow persistence seam for service-owned catalogue synchronization."""

    def sync_component_definitions(self, definitions: Sequence[Mapping[str, object]]) -> int: ...


def load_component_manifests(resource_dir: Path | None = None) -> list[dict[str, object]]:
    directory = (
        resource_dir
        or Path(__file__).resolve().parents[2] / "resources" / "component-manifests"
    )
    paths = sorted(directory.glob("*.yaml")) + sorted(directory.glob("*.yml"))
    if len(paths) > _MAX_MANIFESTS:
        raise ValueError("component manifest count exceeds the bounded limit")
    definitions: list[dict[str, object]] = []
    identities: set[tuple[str, str]] = set()
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError("component manifest must be a regular packaged file")
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            raise ValueError("component manifest could not be parsed") from exc
        definition = _normalize_manifest(document)
        identity = (str(definition["componentId"]), str(definition["componentVersion"]))
        if identity in identities:
            raise ValueError("duplicate component definition")
        identities.add(identity)
        definitions.append(definition)
    _reject_component_cycles(definitions)
    return sorted(
        definitions,
        key=lambda item: (str(item["componentId"]), str(item["componentVersion"])),
    )


def manifest_hash(definition: Mapping[str, object]) -> str:
    canonical = json.dumps(definition, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def synchronize_component_manifests(
    store: ComponentCataloguePort, resource_dir: Path | None = None
) -> int:
    return store.sync_component_definitions(load_component_manifests(resource_dir))


def _normalize_manifest(document: object) -> dict[str, object]:
    if not isinstance(document, dict):
        raise ValueError("component manifest must be an object")
    raw = cast(dict[str, object], document)
    if set(raw) - _MANIFEST_KEYS:
        raise ValueError("component manifest contains unsupported fields")
    component = _mapping(raw.get("component"), "component")
    _closed_keys(component, {"id", "version", "owner", "category"}, "component")
    component_id = _identifier(component.get("id"), "component id")
    if _COMPONENT_ID.fullmatch(component_id) is None:
        raise ValueError("component id is invalid")
    return {
        "componentId": component_id,
        "componentVersion": _version(component.get("version"), "component version"),
        "owner": _identifier(component.get("owner"), "component owner"),
        "category": _identifier(component.get("category"), "component category"),
        "inputSchemaId": _schema_id(raw.get("input"), "input"),
        "outputSchemaId": _schema_id(raw.get("output"), "output"),
        "measurements": _identifiers(raw.get("metrics", []), "metrics", _MAX_ITEMS),
        "evaluators": _identifiers(raw.get("evaluators", []), "evaluators", _MAX_ITEMS),
        "dependencies": _dependencies(raw.get("dependencies", [])),
    }


def _mapping(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return cast(dict[str, object], value)


def _closed_keys(value: Mapping[str, object], allowed: set[str], label: str) -> None:
    if set(value) - allowed:
        raise ValueError(f"{label} contains unsupported fields")


def _identifier(value: object, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _version(value: object, label: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _schema_id(value: object, label: str) -> str:
    mapping = _mapping(value, label)
    _closed_keys(mapping, {"schema", "replayable"}, label)
    return _identifier(mapping.get("schema"), f"{label} schema")


def _identifiers(value: object, label: str, maximum: int) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a bounded array")
    values = cast(list[object], value)
    if len(values) > maximum:
        raise ValueError(f"{label} must be a bounded array")
    result = [_identifier(item, f"{label} identifier") for item in values]
    if len(set(result)) != len(result):
        raise ValueError(f"{label} contains duplicates")
    return sorted(result)


def _dependencies(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list):
        raise ValueError("dependencies must be a bounded array")
    values = cast(list[object], value)
    if len(values) > _MAX_DEPENDENCIES:
        raise ValueError("dependencies must be a bounded array")
    result: list[dict[str, object]] = []
    identities: set[tuple[str, str, str | None]] = set()
    for item in values:
        required_version: str | None = None
        if isinstance(item, str):
            dependency_id = _identifier(item, "dependency id")
            kind = "component" if dependency_id.startswith("aura.") else "provider"
        elif isinstance(item, dict):
            mapping = cast(dict[str, object], item)
            _closed_keys(
                mapping,
                {"id", "dependencyId", "kind", "version", "requiredVersion"},
                "dependency",
            )
            dependency_id = _identifier(
                mapping.get("dependencyId", mapping.get("id")), "dependency id"
            )
            raw_kind = mapping.get("kind")
            kind = str(raw_kind) if raw_kind is not None else (
                "component" if dependency_id.startswith("aura.") else "provider"
            )
            if kind not in {"component", "provider"}:
                raise ValueError("dependency kind is invalid")
            raw_version = mapping.get("requiredVersion", mapping.get("version"))
            if raw_version is not None:
                required_version = _version(raw_version, "dependency version")
        else:
            raise ValueError("dependency must be a string or object")
        identity = (dependency_id, kind, required_version)
        if identity in identities:
            raise ValueError("duplicate dependency")
        identities.add(identity)
        result.append(
            {"dependencyId": dependency_id, "kind": kind, "requiredVersion": required_version}
        )
    return sorted(
        result,
        key=lambda item: (
            str(item["kind"]),
            str(item["dependencyId"]),
            str(item["requiredVersion"] or ""),
        ),
    )


def _reject_component_cycles(definitions: Sequence[Mapping[str, object]]) -> None:
    known = {(str(item["componentId"]), str(item["componentVersion"])) for item in definitions}
    graph: dict[tuple[str, str], list[tuple[str, str]]] = {identity: [] for identity in known}
    for item in definitions:
        source = (str(item["componentId"]), str(item["componentVersion"]))
        for dependency in cast(list[dict[str, object]], item["dependencies"]):
            if dependency["kind"] != "component":
                continue
            target_id = str(dependency["dependencyId"])
            required = dependency.get("requiredVersion")
            graph[source].extend(
                identity for identity in known
                if identity[0] == target_id and (required is None or identity[1] == required)
            )
    visiting: set[tuple[str, str]] = set()
    visited: set[tuple[str, str]] = set()

    def visit(node: tuple[str, str]) -> None:
        if node in visiting:
            raise ValueError("component dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for target in graph[node]:
            visit(target)
        visiting.remove(node)
        visited.add(node)

    for node in graph:
        visit(node)


__all__ = [
    "ComponentCataloguePort",
    "load_component_manifests",
    "manifest_hash",
    "synchronize_component_manifests",
]
