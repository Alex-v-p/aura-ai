#!/usr/bin/env python3
from __future__ import annotations

import argparse

from common import (
    ROOT,
    emit,
    git_changed_paths,
    path_matches,
    read_yaml,
    resolve_work_item,
    semantic_work_item_errors,
    validate_document,
)

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-item")
    args = parser.parse_args()
    protected = [
        entry["pattern"]
        for entry in read_yaml(ROOT / "tooling/architecture/critical-paths.yaml")["critical_paths"]
        if entry.get("baseline_permission")
    ]
    paths = [
        path
        for path in git_changed_paths()
        if any(path_matches(path, pattern) for pattern in protected)
    ]
    if not paths:
        raise SystemExit(
            emit(
                "skipped",
                "baseline-protection",
                ["No protected baseline or threshold changes detected."],
            )
        )
    work_item = resolve_work_item(args.work_item)
    if work_item is None or not work_item.is_file():
        raise SystemExit(
            emit("failed", "baseline-protection", ["Protected changes require a valid work item."])
        )
    item, errors = validate_document(work_item, "work-item.schema.json")
    if isinstance(item, dict):
        errors.extend(semantic_work_item_errors(item))
    if not errors and not item.get("baseline_changes_allowed"):
        errors.append("Work item does not authorize baseline changes.")
    if not errors and not item.get("human_approval_required"):
        errors.append("Baseline changes require human approval.")
    if not errors and item.get("change_class") not in {"evaluation", "repository_governance"}:
        errors.append(
            "Protected baseline changes require change_class evaluation or repository_governance."
        )
    assigned = (
        item.get("required_agents", {}).get("implementation", []) if isinstance(item, dict) else []
    )
    if not errors and "test_evaluation_engineer" not in assigned:
        errors.append(
            "Protected baseline changes require test_evaluation_engineer as an "
            "implementation agent."
        )
    raise SystemExit(
        emit(
            "failed" if errors else "passed",
            "baseline-protection",
            errors or ["Protected baseline changes are authorized."],
            paths=paths,
        )
    )
