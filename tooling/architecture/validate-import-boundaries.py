#!/usr/bin/env python3
from __future__ import annotations

import ast
from pathlib import Path

from common import ROOT, emit

CORE = ROOT / "services" / "aura-core" / "src" / "aura_core"


def imported_names(path: Path) -> list[str]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except OSError, SyntaxError:
        return []
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


def check() -> list[str]:
    errors = []
    domains = CORE / "domains"
    if not domains.is_dir():
        return errors
    for path in domains.rglob("*.py"):
        relative = path.relative_to(domains)
        owner = relative.parts[0] if relative.parts else ""
        for name in imported_names(path):
            if name.startswith("aura_core.providers"):
                errors.append(f"{path.relative_to(ROOT)} imports provider implementation {name}")
            match = name.split("aura_core.domains.", 1)
            if len(match) == 2:
                parts = match[1].split(".")
                if (
                    parts
                    and parts[0] != owner
                    and any(part in {"adapters", "persistence"} for part in parts)
                ):
                    errors.append(
                        f"{path.relative_to(ROOT)} imports another domain's internals: {name}"
                    )
    return errors


if __name__ == "__main__":
    if not CORE.is_dir():
        raise SystemExit(
            emit(
                "skipped", "python-import-boundaries", ["Aura Core source tree does not exist yet."]
            )
        )
    problems = check()
    raise SystemExit(
        emit(
            "failed" if problems else "passed",
            "python-import-boundaries",
            problems or ["Python import boundaries hold."],
        )
    )
