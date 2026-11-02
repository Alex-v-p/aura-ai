#!/usr/bin/env python3
from __future__ import annotations

import argparse

from common import (
    emit,
    git_changed_paths,
    path_matches,
    resolve_work_item,
    semantic_work_item_errors,
    validate_document,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    args = parser.parse_args()
    paths = [path for path in git_changed_paths() if path_matches(path, "contracts/**")]
    if not paths:
        raise SystemExit(emit("skipped", "contract-changes", ["No contract changes detected."]))
    work_item = resolve_work_item(args.work_item)
    if work_item is None or not work_item.is_file():
        raise SystemExit(
            emit("failed", "contract-changes", ["Contract changes require a valid work item."])
        )
    item, errors = validate_document(work_item, "work-item.schema.json")
    if isinstance(item, dict):
        errors.extend(semantic_work_item_errors(item))
    if not errors and not item.get("contracts", {}).get("changes_allowed"):
        errors.append("Work item does not authorize contract changes.")
    if not errors and item.get("change_class") not in {"contract", "repository_governance"}:
        errors.append("Contract edits require change_class contract or repository_governance.")
    assigned = (
        item.get("required_agents", {}).get("implementation", []) if isinstance(item, dict) else []
    )
    if not errors and "contract_steward" not in assigned:
        errors.append("Contract changes require contract_steward as an implementation agent.")
    raise SystemExit(
        emit(
            "failed" if errors else "passed",
            "contract-changes",
            errors or ["Contract changes are explicitly classified."],
            paths=paths,
        )
    )
