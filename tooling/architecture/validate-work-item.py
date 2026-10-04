#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

from common import emit, resolve_work_item, semantic_work_item_errors, validate_document


def validate(path: Path) -> tuple[dict | None, list[str]]:
    item, errors = validate_document(path, "work-item.schema.json")
    if isinstance(item, dict):
        errors.extend(semantic_work_item_errors(item))
    return item, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", nargs="?")
    args = parser.parse_args()
    path = resolve_work_item(args.path)
    if path is None:
        return emit("failed", "work-item", ["No work item selected; set AURA_WORK_ITEM or pass a path."])
    if not path.is_file():
        return emit("failed", "work-item", [f"Work item does not exist: {path}"])
    item, errors = validate(path)
    return emit("failed" if errors else "passed", "work-item", errors or [f"Validated {item.get('id')}"], path=str(path))


if __name__ == "__main__":
    raise SystemExit(main())
