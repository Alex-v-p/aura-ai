from __future__ import annotations

import fnmatch
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Iterable

try:
    import yaml
except ImportError as exc:  # pragma: no cover - environment failure
    raise SystemExit("PyYAML is required for Aura governance checks") from exc

ROOT = Path(__file__).resolve().parents[2]
SCHEMA_DIR = ROOT / "work-items" / "schema"


def read_yaml(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        value = yaml.safe_load(handle)
    return {} if value is None else value


def read_json(path: Path) -> Any:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def emit(status: str, check: str, messages: Iterable[str] = (), **details: Any) -> int:
    payload = {"status": status, "check": check, "messages": list(messages), **details}
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if status in {"passed", "skipped"} else 1


def norm_path(value: str | Path) -> str:
    text = str(value).replace("\\", "/")
    while text.startswith("./"):
        text = text[2:]
    candidate = Path(text)
    if candidate.is_absolute():
        try:
            text = candidate.resolve().relative_to(ROOT.resolve()).as_posix()
        except ValueError:
            pass
    return text.rstrip("/")


def path_matches(path: str | Path, pattern: str) -> bool:
    candidate = norm_path(path)
    rule = norm_path(pattern)
    if rule.endswith("/**"):
        root = rule[:-3].rstrip("/")
        return candidate == root or candidate.startswith(root + "/")
    if not any(char in rule for char in "*?["):
        return candidate == rule
    return fnmatch.fnmatchcase(candidate, rule)


def matching_patterns(path: str, patterns: Iterable[str]) -> list[str]:
    return [pattern for pattern in patterns if path_matches(path, pattern)]


def patterns_overlap(left: str, right: str) -> bool:
    left_root = norm_path(left).split("*", 1)[0].rstrip("/")
    right_root = norm_path(right).split("*", 1)[0].rstrip("/")
    if not left_root or not right_root:
        return True
    return left_root == right_root or left_root.startswith(right_root + "/") or right_root.startswith(left_root + "/")


def git_changed_paths(root: Path = ROOT) -> list[str]:
    commands = [
        ["git", "diff", "--name-only", "HEAD"],
        ["git", "ls-files", "--others", "--exclude-standard"],
    ]
    paths: set[str] = set()
    for command in commands:
        result = subprocess.run(command, cwd=root, check=False, text=True, capture_output=True)
        if result.returncode:
            raise RuntimeError(result.stderr.strip() or "git path discovery failed")
        paths.update(norm_path(line) for line in result.stdout.splitlines() if line.strip())
    return sorted(paths)


def resolve_work_item(explicit: str | None = None, cwd: Path = ROOT) -> Path | None:
    if explicit:
        path = Path(explicit)
        return path if path.is_absolute() else cwd / path
    identifier = os.environ.get("AURA_WORK_ITEM", "").strip()
    if not identifier:
        pointer = cwd / ".aura-work-item"
        if pointer.is_file():
            identifier = pointer.read_text(encoding="utf-8").strip()
    if not identifier:
        return None
    if identifier.endswith((".yaml", ".yml")) or "/" in identifier:
        candidate = Path(identifier)
        candidate = candidate if candidate.is_absolute() else cwd / candidate
        active = (cwd / "work-items" / "active").resolve()
        try:
            candidate.resolve().relative_to(active)
        except ValueError:
            return None
        return candidate
    directory = cwd / "work-items" / "active"
    for suffix in (".yaml", ".yml"):
        candidate = directory / f"{identifier}{suffix}"
        if candidate.is_file():
            return candidate
    return cwd / "work-items" / "active" / f"{identifier}.yaml"


def _json_types(schema_type: Any) -> tuple[str, ...]:
    return tuple(schema_type) if isinstance(schema_type, list) else (schema_type,)


def _is_type(value: Any, expected: str) -> bool:
    return {
        "object": isinstance(value, dict), "array": isinstance(value, list),
        "string": isinstance(value, str), "boolean": isinstance(value, bool),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_schema(value: Any, schema: dict[str, Any], root_schema: dict[str, Any] | None = None, location: str = "$") -> list[str]:
    root_schema = root_schema or schema
    if "$ref" in schema:
        ref = schema["$ref"]
        if not ref.startswith("#/"):
            return [f"{location}: unsupported schema reference {ref}"]
        target: Any = root_schema
        for part in ref[2:].split("/"):
            target = target[part.replace("~1", "/").replace("~0", "~")]
        return validate_schema(value, target, root_schema, location)
    errors: list[str] = []
    if "const" in schema and value != schema["const"]:
        errors.append(f"{location}: must equal {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        errors.append(f"{location}: must be one of {schema['enum']}")
    if "type" in schema and not any(_is_type(value, item) for item in _json_types(schema["type"])):
        return [f"{location}: expected type {schema['type']}"]
    if isinstance(value, dict):
        required = schema.get("required", [])
        errors.extend(f"{location}: missing required key {key}" for key in required if key not in value)
        properties = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            errors.extend(f"{location}: unexpected key {key}" for key in value if key not in properties)
        for key, child in properties.items():
            if key in value:
                errors.extend(validate_schema(value[key], child, root_schema, f"{location}.{key}"))
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            errors.append(f"{location}: requires at least {schema['minItems']} items")
        if schema.get("uniqueItems"):
            fingerprints = [json.dumps(item, sort_keys=True) for item in value]
            if len(set(fingerprints)) != len(fingerprints):
                errors.append(f"{location}: items must be unique")
        if "items" in schema:
            for index, item in enumerate(value):
                errors.extend(validate_schema(item, schema["items"], root_schema, f"{location}[{index}]"))
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            errors.append(f"{location}: must contain at least {schema['minLength']} characters")
        if "pattern" in schema and re.search(schema["pattern"], value) is None:
            errors.append(f"{location}: does not match {schema['pattern']}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errors.append(f"{location}: must be >= {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errors.append(f"{location}: must be <= {schema['maximum']}")
    return errors


def validate_document(path: Path, schema_name: str) -> tuple[Any, list[str]]:
    try:
        document = read_yaml(path)
    except (OSError, yaml.YAMLError) as exc:
        return None, [f"{path}: {exc}"]
    schema = read_json(SCHEMA_DIR / schema_name)
    return document, validate_schema(document, schema)


def semantic_work_item_errors(item: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = item.get("allowed_paths", [])
    forbidden = item.get("forbidden_paths", [])
    for left in allowed:
        for right in forbidden:
            if patterns_overlap(left, right):
                errors.append(f"allowed path {left!r} overlaps forbidden path {right!r}")
    execution = item.get("execution", {})
    if execution.get("max_parallel_writers", 1) > execution.get("max_parallel_agents", 1):
        errors.append("max_parallel_writers cannot exceed max_parallel_agents")
    if execution.get("parallelism_class") == "local" and execution.get("max_parallel_writers", 1) != 1:
        errors.append("local parallelism requires exactly one writer")
    if execution.get("parallelism_class") == "fanout" and not item.get("required_agents", {}).get("integration"):
        errors.append("fanout work requires an Integration Maintainer")
    if item.get("contracts", {}).get("compatibility_requirement") != "none" and not item.get("contracts", {}).get("affected"):
        errors.append("contract compatibility requirements need at least one affected contract")
    if item.get("data", {}).get("migration_required") and not item.get("data", {}).get("owning_service"):
        errors.append("migrations require an owning_service")
    if item.get("observability", {}).get("applicable") and not item.get("observability", {}).get("affected_components"):
        errors.append("applicable observability requires affected_components")
    if item.get("security_privacy", {}).get("applicable") and not item.get("security_privacy", {}).get("concerns"):
        errors.append("applicable security/privacy review requires concerns")
    if item.get("change_class") == "repository_governance" and not item.get("human_approval_required"):
        errors.append("repository governance changes require human approval")
    if item.get("baseline_changes_allowed") and not item.get("human_approval_required"):
        errors.append("baseline changes require human approval")
    return errors


def critical_path_errors(paths: Iterable[str], item: dict[str, Any]) -> list[str]:
    manifest = read_yaml(ROOT / "tooling" / "architecture" / "critical-paths.yaml")
    errors: list[str] = []
    for path in paths:
        for rule in manifest.get("critical_paths", []):
            if not path_matches(path, rule["pattern"]):
                continue
            if not item.get("human_approval_required"):
                errors.append(f"{path}: critical path requires human_approval_required")
            if item.get("change_class") not in rule.get("change_classes", []):
                errors.append(f"{path}: change class {item.get('change_class')} is not authorized for critical pattern {rule['pattern']}")
            if rule.get("baseline_permission") and not item.get("baseline_changes_allowed"):
                errors.append(f"{path}: protected baseline change is not authorized")
    return errors


def scope_errors(paths: Iterable[str], item: dict[str, Any], role: str | None = None) -> list[str]:
    allowed = item.get("allowed_paths", [])
    forbidden = item.get("forbidden_paths", [])
    errors: list[str] = []
    ownership = read_yaml(ROOT / "tooling" / "architecture" / "path-ownership.yaml").get("ownership", [])
    for raw_path in paths:
        path = norm_path(raw_path)
        if matching_patterns(path, forbidden):
            errors.append(f"{path}: matches forbidden path")
        if not matching_patterns(path, allowed):
            errors.append(f"{path}: outside allowed_paths")
        matches = [entry for entry in ownership if path_matches(path, entry["pattern"])]
        if not matches:
            errors.append(f"{path}: no architectural owner is defined")
        if role and matches:
            most_specific = max(matches, key=lambda entry: len(entry["pattern"].replace("*", "")))
            if role not in most_specific.get("roles", []):
                errors.append(f"{path}: role {role} is not an owner; expected one of {most_specific.get('roles', [])}")
    errors.extend(critical_path_errors(paths, item))
    return sorted(set(errors))


def extract_patch_paths(command: str) -> list[str]:
    paths = []
    for match in re.finditer(r"^\*\*\* (?:Add|Update|Delete) File: (.+)$", command, re.MULTILINE):
        paths.append(norm_path(match.group(1).strip()))
    return paths


def likely_write_command(command: str) -> bool:
    patterns = [
        r"(^|[;&|]\s*)(rm|mv|cp|install|mkdir|touch|truncate)\b",
        r"(^|\s)(>|>>)\s*\S", r"\b(git\s+(add|commit|reset|clean|checkout|restore)|sed\s+-i|perl\s+-i)\b",
        r"\b(python|python3|node|ruby)\b.*\b(write_text|write_bytes|open\([^)]*['\"]w)",
    ]
    return any(re.search(pattern, command, re.MULTILINE) for pattern in patterns)


def hook_input() -> dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError(f"malformed hook input: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("hook input must be a JSON object")
    return value


def hook_block(event: str, reason: str) -> None:
    print(json.dumps({"hookSpecificOutput": {"hookEventName": event, "permissionDecision": "deny", "permissionDecisionReason": reason}}))


def continuation(reason: str) -> None:
    print(json.dumps({"decision": "block", "reason": reason}))


def changed_paths_or_skip(check: str) -> tuple[list[str] | None, int | None]:
    try:
        paths = git_changed_paths()
    except RuntimeError as exc:
        return None, emit("failed", check, [str(exc)])
    if not paths:
        return None, emit("skipped", check, ["No changed paths to validate."])
    return paths, None
