#!/usr/bin/env python3
from __future__ import annotations

import json
import os

from common import git_changed_paths, hook_input, resolve_work_item, scope_errors, semantic_work_item_errors, validate_document


def main() -> int:
    try:
        hook_input()
    except ValueError as exc:
        print(json.dumps({"decision": "block", "reason": str(exc)}))
        return 0
    work_item = resolve_work_item()
    if work_item is None or not work_item.is_file():
        print("{}")
        return 0
    try:
        paths = git_changed_paths()
    except RuntimeError as exc:
        print(json.dumps({"decision": "block", "reason": f"Cannot verify changed-path scope: {exc}"}))
        return 0
    item, errors = validate_document(work_item, "work-item.schema.json")
    if isinstance(item, dict):
        errors.extend(semantic_work_item_errors(item))
        errors.extend(scope_errors(paths, item, os.environ.get("AURA_AGENT_ROLE") or None))
    if errors:
        print(json.dumps({"decision": "block", "reason": "Aura scope drift detected: " + "; ".join(errors)}))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
