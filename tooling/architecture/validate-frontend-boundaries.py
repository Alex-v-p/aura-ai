#!/usr/bin/env python3
from __future__ import annotations

from common import ROOT, emit

FRONTEND = ROOT / "frontend"


def scan(root, forbidden: tuple[str, ...]) -> list[str]:
    errors = []
    if not root.is_dir():
        return errors
    for suffix in ("*.ts", "*.tsx", "*.js", "*.mjs"):
        for path in root.rglob(suffix):
            text = path.read_text(encoding="utf-8")
            for marker in forbidden:
                if marker in text:
                    errors.append(
                        f"{path.relative_to(ROOT)} references forbidden product library {marker}"
                    )
    return errors


if __name__ == "__main__":
    if not FRONTEND.is_dir():
        raise SystemExit(
            emit("skipped", "frontend-boundaries", ["Frontend workspace does not exist yet."])
        )
    problems = []
    problems += scan(FRONTEND / "libs" / "aura", ("@aura/observatory", "libs/observatory"))
    problems += scan(FRONTEND / "libs" / "observatory", ("@aura/aura", "libs/aura"))
    problems += scan(
        FRONTEND / "libs" / "shared",
        ("@aura/aura", "@aura/observatory", "libs/aura", "libs/observatory"),
    )
    raise SystemExit(
        emit(
            "failed" if problems else "passed",
            "frontend-boundaries",
            problems or ["Frontend product boundaries hold."],
        )
    )
