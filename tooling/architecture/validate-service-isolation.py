#!/usr/bin/env python3
from __future__ import annotations

import re

from common import ROOT, emit


def scan(root, forbidden: str) -> list[str]:
    errors = []
    if not root.is_dir():
        return errors
    expression = re.compile(
        rf"^\s*(?:from|import)\s+{re.escape(forbidden)}(?:\.|\s|$)", re.MULTILINE
    )
    for path in root.rglob("*.py"):
        if expression.search(path.read_text(encoding="utf-8")):
            errors.append(
                f"{path.relative_to(ROOT)} imports forbidden service implementation {forbidden}"
            )
    return errors


if __name__ == "__main__":
    core = ROOT / "services" / "aura-core"
    observatory = ROOT / "services" / "aura-observatory"
    if not core.exists() and not observatory.exists():
        raise SystemExit(
            emit(
                "skipped",
                "service-isolation",
                ["Core and Observatory service trees do not exist yet."],
            )
        )
    problems = scan(core, "aura_observatory") + scan(observatory, "aura_core")
    raise SystemExit(
        emit(
            "failed" if problems else "passed",
            "service-isolation",
            problems or ["Service implementation imports remain isolated."],
        )
    )
