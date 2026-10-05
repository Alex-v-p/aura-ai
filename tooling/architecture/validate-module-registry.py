#!/usr/bin/env python3
"""Validate the explicit Aura module registry and dependency declarations."""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Any

from common import ROOT, emit, read_yaml

MANIFEST = ROOT / "tooling" / "architecture" / "modules.yaml"
RULES = ROOT / "tooling" / "architecture" / "dependency-rules.yaml"
REQUIRED_FIELDS = ("id", "root", "kind", "public_api", "dependencies")
SUPPORTED_KINDS = {
    "service",
    "package",
    "python",
    "platform",
    "runtime",
    "domain",
    "provider",
    "bootstrap",
    "entrypoint",
    "frontend-shared",
    "frontend-feature",
    "generated-client",
    "openapi-contract",
    "event-contract",
}


def _relative_path(value: Any, label: str) -> tuple[Path | None, list[str]]:
    if not isinstance(value, str) or not value.strip():
        return None, [f"{label} must be a non-empty relative path"]
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None, [f"{label} must stay within the repository: {value!r}"]
    return candidate, []


def _resolved_path(repo_root: Path, value: Any, label: str) -> tuple[Path | None, list[str]]:
    relative, errors = _relative_path(value, label)
    if relative is None:
        return None, errors
    root = repo_root.resolve()
    resolved = (repo_root / relative).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return None, [f"{label} resolves outside the repository: {value!r}"]
    if not resolved.exists():
        errors.append(f"{label} does not exist: {value!r}")
    return resolved, errors


def _path_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _source_paths(module: dict[str, Any], repo_root: Path, root: Path) -> tuple[list[Path], list[str]]:
    errors: list[str] = []
    raw_sources = module.get("source_files")
    if raw_sources is None and not module.get("python_modules"):
        return [], errors
    if raw_sources is not None and not isinstance(raw_sources, list):
        return [], ["source_files must be a list"]
    values = raw_sources if raw_sources is not None else [module.get("root")]
    paths: list[Path] = []
    for index, value in enumerate(values):
        path, path_errors = _resolved_path(repo_root, value, f"source_files[{index}]")
        errors.extend(path_errors)
        if path is None:
            continue
        if not _path_within(path, root):
            errors.append(f"source_files[{index}] must be within the module root: {value!r}")
        elif path.is_dir():
            paths.extend(sorted(path.rglob("*.py")))
        elif path.suffix == ".py":
            paths.append(path)
        else:
            errors.append(f"source_files[{index}] must identify a Python file or directory: {value!r}")
    return paths, errors


def _import_targets(path: Path, module_name: str | None) -> tuple[list[str], list[str]]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        return [], [f"unable to parse Python source {path}: {exc}"]
    targets: list[str] = []
    errors: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            targets.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0:
                if node.module:
                    targets.append(node.module)
                    targets.extend(
                        f"{node.module}.{alias.name}"
                        for alias in node.names
                        if alias.name != "*"
                    )
                continue
            if module_name is None:
                errors.append(
                    f"unable to resolve relative import in {path}: "
                    "source module identity is missing"
                )
                continue
            package_parts = module_name.split(".")
            if path.name != "__init__.py":
                package_parts = package_parts[:-1]
            ascend = node.level - 1
            if ascend >= len(package_parts):
                errors.append(
                    f"unable to resolve relative import in {path}: "
                    f"level {node.level} escapes module package {module_name}"
                )
                continue
            base_parts = package_parts[: len(package_parts) - ascend]
            base = ".".join(base_parts)
            target_base = f"{base}.{node.module}" if node.module else base
            targets.append(target_base)
            targets.extend(
                f"{target_base}.{alias.name}"
                for alias in node.names
                if alias.name != "*"
            )
    return targets, errors


def _resolve_module(target: str, module_map: dict[str, str]) -> str | None:
    parts = target.split(".")
    for index in range(len(parts), 0, -1):
        candidate = ".".join(parts[:index])
        if candidate in module_map:
            return module_map[candidate]
    return None


def _source_module_name(
    module: dict[str, Any], source_path: Path, root_path: Path
) -> str | None:
    python_modules = module.get("python_modules", [])
    if not isinstance(python_modules, list) or len(python_modules) != 1:
        return None
    declared = python_modules[0]
    if not isinstance(declared, str) or not declared.strip():
        return None
    if root_path.is_file():
        return declared
    try:
        relative = source_path.relative_to(root_path)
    except ValueError:
        return None
    if relative.name == "__init__.py":
        return declared
    suffix = ".".join(relative.with_suffix("").parts)
    return f"{declared}.{suffix}"


def validate_manifest(document: Any, repo_root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    if not isinstance(document, dict):
        return ["manifest must be a mapping"]
    if document.get("version") != 1:
        errors.append("manifest version must be 1")
    modules = document.get("modules")
    if not isinstance(modules, list) or not modules:
        return errors + ["modules must be a non-empty list"]

    ids: set[str] = set()
    roots: dict[str, str] = {}
    declared_dependencies: list[tuple[str, Any]] = []
    for index, module in enumerate(modules):
        location = f"modules[{index}]"
        if not isinstance(module, dict):
            errors.append(f"{location} must be a mapping")
            continue
        missing = [field for field in REQUIRED_FIELDS if field not in module]
        errors.extend(f"{location} missing required field {field}" for field in missing)
        module_id = module.get("id")
        if not isinstance(module_id, str) or not module_id.strip():
            errors.append(f"{location}.id must be a non-empty string")
            module_id = f"<invalid-{index}>"
        elif module_id in ids:
            errors.append(f"duplicate module id: {module_id}")
        else:
            ids.add(module_id)
        kind = module.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            errors.append(f"{location}.kind must be a non-empty string")
        elif kind not in SUPPORTED_KINDS:
            errors.append(f"{location}.kind is unsupported: {kind!r}")
        for field in ("root", "public_api"):
            resolved, path_errors = _resolved_path(repo_root, module.get(field), f"{location}.{field}")
            errors.extend(path_errors)
            if field == "root" and resolved is not None:
                normalized = resolved.relative_to(repo_root.resolve()).as_posix()
                previous = roots.get(normalized)
                if previous is not None:
                    errors.append(f"duplicate module root {normalized!r}: {previous} and {module_id}")
                else:
                    roots[normalized] = str(module_id)
        root_path, root_errors = _resolved_path(repo_root, module.get("root"), f"{location}.root")
        errors.extend(root_errors)
        public_path, public_errors = _resolved_path(repo_root, module.get("public_api"), f"{location}.public_api")
        errors.extend(public_errors)
        if root_path is not None and public_path is not None and not _path_within(public_path, root_path):
            errors.append(f"{location}.public_api must be within its module root")
        python_modules = module.get("python_modules", [])
        if not isinstance(python_modules, list) or any(
            not isinstance(value, str) or not value.strip() for value in python_modules
        ):
            errors.append(f"{location}.python_modules must be a list of non-empty strings")
        dependencies = module.get("dependencies")
        if not isinstance(dependencies, list):
            errors.append(f"{location}.dependencies must be a list")
        else:
            declared_dependencies.extend((str(module_id), dependency) for dependency in dependencies)

    for module_id, dependency in declared_dependencies:
        if not isinstance(dependency, str) or dependency not in ids:
            errors.append(f"module {module_id} declares unknown dependency {dependency!r}")
        elif dependency == module_id:
            errors.append(f"module {module_id} cannot depend on itself")

    module_map: dict[str, str] = {}
    for module in modules:
        if not isinstance(module, dict):
            continue
        module_id = module.get("id")
        for python_module in module.get("python_modules", []):
            if python_module in module_map and module_map[python_module] != module_id:
                errors.append(
                    f"duplicate Python module {python_module!r}: "
                    f"{module_map[python_module]} and {module_id}"
                )
            else:
                module_map[python_module] = str(module_id)

    source_owners: dict[Path, list[str]] = {}
    package_roots: list[Path] = []
    for index, module in enumerate(modules):
        if not isinstance(module, dict):
            continue
        module_id = str(module.get("id", f"<invalid-{index}>"))
        root_path, root_errors = _resolved_path(repo_root, module.get("root"), f"modules[{index}].root")
        errors.extend(root_errors)
        if root_path is None:
            continue
        source_paths, source_errors = _source_paths(module, repo_root, root_path)
        errors.extend(f"modules[{index}]: {error}" for error in source_errors)
        if module.get("kind") == "package":
            package_roots.append(root_path)
        for source_path in source_paths:
            source_owners.setdefault(source_path, []).append(module_id)
        declared = set(module.get("dependencies", [])) if isinstance(module.get("dependencies"), list) else set()
        for source_path in source_paths:
            module_name = _source_module_name(module, source_path, root_path)
            targets, parse_errors = _import_targets(source_path, module_name)
            errors.extend(parse_errors)
            for target in targets:
                if not target.startswith("aura_core"):
                    continue
                dependency = _resolve_module(target, module_map)
                if dependency is None:
                    errors.append(f"module {module_id} imports unregistered Python module {target}")
                elif dependency != module_id and dependency not in declared:
                    errors.append(
                        f"module {module_id} imports {target} ({dependency}) "
                        "without declaring the dependency"
                    )

    core_root = (repo_root / "services" / "aura-core" / "src" / "aura_core").resolve()
    covered_core_files: set[Path] = set()
    for package_root in package_roots:
        if not _path_within(package_root, core_root):
            continue
        covered_core_files.update(package_root.rglob("*.py"))
    for source_path, owners in source_owners.items():
        if source_path in covered_core_files and len(owners) != 1:
            errors.append(
                f"Core Python source {source_path.relative_to(repo_root.resolve())} "
                f"must have exactly one registry owner; found {owners}"
            )
    for source_path in sorted(covered_core_files):
        owners = source_owners.get(source_path, [])
        if len(owners) != 1:
            try:
                display = source_path.relative_to(repo_root.resolve())
            except ValueError:
                display = source_path
            errors.append(
                f"Core Python source {display} must have exactly one registry owner; "
                f"found {owners or 'none'}"
            )
    return sorted(set(errors))


def validate_dependency_rules(document: Any) -> list[str]:
    if not isinstance(document, dict):
        return ["dependency rules must be a mapping"]
    rules = document.get("rules")
    if not isinstance(rules, list) or not rules:
        return ["dependency rules must contain a non-empty rules list"]
    errors: list[str] = []
    ids: set[str] = set()
    for index, rule in enumerate(rules):
        location = f"rules[{index}]"
        if not isinstance(rule, dict):
            errors.append(f"{location} must be a mapping")
            continue
        rule_id = rule.get("id")
        source = rule.get("source")
        if not isinstance(rule_id, str) or not rule_id.strip():
            errors.append(f"{location}.id must be a non-empty string")
        elif rule_id in ids:
            errors.append(f"duplicate dependency rule id: {rule_id}")
        else:
            ids.add(rule_id)
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{location}.source must be a non-empty path pattern")
        elif Path(source).is_absolute() or ".." in Path(source).parts:
            errors.append(f"{location}.source must be repository-relative and nonescaping")
        restrictions = ("policy", "forbidden_imports", "forbidden_import_prefixes", "forbidden_segments", "require_cross_domain_public_api")
        if not any(key in rule for key in restrictions):
            errors.append(f"{location} must declare a policy or restriction")
        if "policy" in rule and (not isinstance(rule["policy"], str) or not rule["policy"].strip()):
            errors.append(f"{location}.policy must be a non-empty string")
        for key in ("forbidden_imports", "forbidden_import_prefixes", "forbidden_segments"):
            if key in rule and (
                not isinstance(rule[key], list)
                or not rule[key]
                or any(not isinstance(value, str) or not value.strip() for value in rule[key])
            ):
                errors.append(f"{location}.{key} must be a non-empty list of strings")
        if "require_cross_domain_public_api" in rule and not isinstance(rule["require_cross_domain_public_api"], bool):
            errors.append(f"{location}.require_cross_domain_public_api must be boolean")
    return sorted(set(errors))


def check(manifest_path: Path = MANIFEST, rules_path: Path = RULES, repo_root: Path = ROOT) -> list[str]:
    errors: list[str] = []
    try:
        manifest = read_yaml(manifest_path)
    except Exception as exc:
        errors.append(f"unable to read module registry: {exc}")
        manifest = None
    try:
        rules = read_yaml(rules_path)
    except Exception as exc:
        errors.append(f"unable to read dependency rules: {exc}")
        rules = None
    errors.extend(validate_manifest(manifest, repo_root))
    errors.extend(validate_dependency_rules(rules))
    return sorted(set(errors))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=MANIFEST)
    parser.add_argument("--rules", type=Path, default=RULES)
    args = parser.parse_args()
    errors = check(args.manifest, args.rules, ROOT)
    try:
        manifest_display = args.manifest.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        manifest_display = str(args.manifest)
    return emit(
        "failed" if errors else "passed",
        "module-registry",
        errors or ["Module roots and dependency rules are valid."],
        module_registry=manifest_display,
    )


if __name__ == "__main__":
    raise SystemExit(main())
