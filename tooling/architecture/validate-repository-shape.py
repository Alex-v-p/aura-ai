#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from common import ROOT, emit

ALLOWED_TOP_LEVEL = {
    ".agents", ".aura-work-item", ".codex", ".devcontainer", ".github", ".gitignore", ".editorconfig", ".env.example",
    ".pre-commit-config.yaml", ".python-version", "AGENTS.md", "ARCHITECTURE.md", "CODEOWNERS", "CONTRIBUTING.md",
    "LICENSE", "README.md", "SECURITY.md", "benchmarks", "compose.yaml", "contracts", "deploy", "docs", "evaluations",
    "extensions", "frontend", "infra", "justfile", "packages", "pyproject.toml", "satellites", "scripts", "services",
    "tests", "tooling", "uv.lock", "work-items",
}
DUMPING_GROUNDS = {"helpers", "managers", "models", "utils"}


def errors_for(root: Path = ROOT) -> list[str]:
    errors = []
    for child in root.iterdir():
        if child.name == ".git":
            continue
        if child.name not in ALLOWED_TOP_LEVEL:
            errors.append(f"Unauthorized top-level path: {child.name}")
        if child.is_dir() and child.name in DUMPING_GROUNDS:
            errors.append(f"Global dumping-ground directory is prohibited: {child.name}")
    return errors


if __name__ == "__main__":
    problems = errors_for()
    raise SystemExit(emit("failed" if problems else "passed", "repository-shape", problems or ["Repository shape is authorized."]))
