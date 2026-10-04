#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re

from common import extract_patch_paths, hook_block, hook_input, likely_write_command, norm_path, resolve_work_item, scope_errors, semantic_work_item_errors, validate_document

DESTRUCTIVE = re.compile(r"\b(git\s+reset\s+--hard|git\s+clean\b|git\s+push\s+(?:--force|-f)|rm\s+-(?:rf|fr)\s+(?:/|\.\.)(?:\s|$))", re.I)
READ_ONLY_ROLES = {"repository_mapper", "general_reviewer", "architecture_guardian", "observability_steward", "security_privacy_reviewer"}
ACTIVE_WORK_ITEM = re.compile(r"work-items/active/AURA-[0-9]{4,}\.ya?ml")


def is_work_item_bootstrap(command: str, paths: list[str]) -> bool:
    added = re.findall(r"^\*\*\* Add File: (.+)$", command, re.MULTILINE)
    normalized = [norm_path(path.strip()) for path in added]
    return len(paths) == 1 and normalized == paths and ACTIVE_WORK_ITEM.fullmatch(paths[0]) is not None


def main() -> int:
    try:
        data = hook_input()
    except ValueError as exc:
        hook_block("PreToolUse", str(exc))
        return 0
    tool = str(data.get("tool_name", ""))
    tool_input = data.get("tool_input", {})
    command = str(tool_input.get("command", "")) if isinstance(tool_input, dict) else ""
    is_patch = tool == "apply_patch" or tool in {"Edit", "Write"} or "*** Begin Patch" in command
    writes = is_patch or (tool == "Bash" and likely_write_command(command))
    if not writes:
        print("{}")
        return 0
    if DESTRUCTIVE.search(command):
        hook_block("PreToolUse", "Destructive Git or broad filesystem operation denied by Aura governance.")
        return 0
    role = os.environ.get("AURA_AGENT_ROLE", "")
    if role in READ_ONLY_ROLES:
        hook_block("PreToolUse", f"Read-only role {role} cannot perform repository writes.")
        return 0
    work_item = resolve_work_item()
    if work_item is None or not work_item.is_file():
        paths = extract_patch_paths(command) if is_patch else []
        if is_patch and is_work_item_bootstrap(command, paths):
            print("{}")
            return 0
        hook_block("PreToolUse", "Nontrivial repository writes require a valid AURA_WORK_ITEM.")
        return 0
    item, validation_errors = validate_document(work_item, "work-item.schema.json")
    if isinstance(item, dict):
        validation_errors.extend(semantic_work_item_errors(item))
    if validation_errors:
        hook_block("PreToolUse", "Active work item is invalid: " + "; ".join(validation_errors))
        return 0
    paths = extract_patch_paths(command) if is_patch else []
    if paths:
        errors = scope_errors(paths, item, role or None)
        if errors:
            hook_block("PreToolUse", "; ".join(errors))
            return 0
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
