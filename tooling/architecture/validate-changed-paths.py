#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os

from common import emit, git_changed_paths, resolve_work_item, scope_errors, semantic_work_item_errors, validate_document


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    parser.add_argument("--role", default=os.environ.get("AURA_AGENT_ROLE"))
    parser.add_argument("--path", action="append", dest="paths")
    args = parser.parse_args()
    work_item_path = resolve_work_item(args.work_item)
    if work_item_path is None or not work_item_path.is_file():
        return emit("failed", "changed-paths", ["A valid active work item is required."])
    item, schema_errors = validate_document(work_item_path, "work-item.schema.json")
    if isinstance(item, dict):
        schema_errors.extend(semantic_work_item_errors(item))
    if schema_errors:
        return emit("failed", "changed-paths", schema_errors)
    paths = sorted(set(args.paths or git_changed_paths()))
    if not paths:
        return emit("skipped", "changed-paths", ["No changed paths to validate."])
    errors = scope_errors(paths, item, args.role)
    return emit("failed" if errors else "passed", "changed-paths", errors or [f"Validated {len(paths)} changed paths."], paths=paths)


if __name__ == "__main__":
    raise SystemExit(main())
