#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re

from common import SCHEMA_DIR, git_changed_paths, hook_input, read_json, resolve_work_item, scope_errors, semantic_work_item_errors, validate_document, validate_schema, yaml

COMPLETE = re.compile(r"\b(complete|completed|implemented|finished|integrated)\b", re.I)


def parse_handoff(message: str, work_item: str) -> tuple[dict | None, list[str]]:
    blocks = re.findall(r"```(?:yaml|yml)?\s*\n(.*?)```", message, re.DOTALL | re.IGNORECASE)
    candidates = blocks or [message]
    candidate_errors: list[str] = []
    for candidate in candidates:
        try:
            document = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if not isinstance(document, dict) or document.get("work_item") != work_item:
            continue
        schema_name = "integration-report.schema.json" if document.get("agent") == "integration_maintainer" else "implementation-handoff.schema.json"
        errors = validate_schema(document, read_json(SCHEMA_DIR / schema_name))
        if not errors:
            return document, []
        candidate_errors.extend(errors)
    return None, candidate_errors or ["Completion report must include a schema-valid implementation handoff or integration report."]


def required_check_errors(item: dict, handoff: dict) -> list[str]:
    results = handoff.get("checks", handoff.get("verification", {}))
    reported = "\n".join(str(value) for key in ("passed", "failed", "not_run") for value in results.get(key, [])) if isinstance(results, dict) else ""
    return [f"Required check was not reported: {check}" for check in item.get("required_checks", []) if check not in reported]


def main() -> int:
    try:
        data = hook_input()
    except ValueError as exc:
        print(json.dumps({"decision": "block", "reason": str(exc)}))
        return 0
    if data.get("stop_hook_active"):
        print("{}")
        return 0
    path = resolve_work_item()
    if path is None or not path.is_file():
        print("{}")
        return 0
    message = str(data.get("last_assistant_message", ""))
    if not COMPLETE.search(message):
        print("{}")
        return 0
    item, errors = validate_document(path, "work-item.schema.json")
    if isinstance(item, dict):
        errors.extend(semantic_work_item_errors(item))
        try:
            errors.extend(scope_errors(git_changed_paths(), item, os.environ.get("AURA_AGENT_ROLE") or None))
        except RuntimeError as exc:
            errors.append(str(exc))
        handoff, handoff_errors = parse_handoff(message, str(item.get("id", "")))
        errors.extend(handoff_errors)
        if handoff is not None:
            errors.extend(required_check_errors(item, handoff))
    if re.search(r"\bBLOCK\b", message) and not re.search(r"\bresolved\b", message, re.I):
        errors.append("Completion report contains an unresolved blocking verdict.")
    if errors:
        print(json.dumps({"decision": "block", "reason": "Aura completion requirements are missing: " + "; ".join(sorted(set(errors)))}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
