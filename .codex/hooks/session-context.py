#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess

from common import ROOT, hook_input, read_yaml, resolve_work_item


def git_value(*args: str) -> str:
    result = subprocess.run(["git", *args], cwd=ROOT, check=False, text=True, capture_output=True)
    return result.stdout.strip() if result.returncode == 0 else "unknown"


def main() -> int:
    try:
        hook_input()
    except ValueError as exc:
        print(json.dumps({"systemMessage": str(exc)}))
        return 0
    path = resolve_work_item()
    lines = [
        f"Aura repository: branch {git_value('branch', '--show-current') or '(detached)'}; worktree {git_value('rev-parse', '--show-toplevel')}",
        "Preserve Core/Observatory isolation, public module APIs, provider direction, service-owned data, and protected governance paths.",
    ]
    if path and path.is_file():
        item = read_yaml(path)
        lines.append(f"Active work item: {item.get('id')} at {path.relative_to(ROOT) if path.is_relative_to(ROOT) else path}.")
        lines.append(f"Allowed paths: {', '.join(item.get('allowed_paths', []))}.")
        lines.append(f"Required roles: {item.get('required_agents', {})}.")
    else:
        lines.append("No active work item is selected. Read-only exploration and planning are allowed; select a valid work item before nontrivial writes.")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "\n".join(lines)}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
