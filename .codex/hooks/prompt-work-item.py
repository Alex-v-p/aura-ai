#!/usr/bin/env python3
from __future__ import annotations

import json
import re

from common import ROOT, hook_input, read_yaml, resolve_work_item

WRITE_WORDS = re.compile(r"\b(implement|change|edit|write|add|remove|delete|fix|refactor|build|create)\b", re.I)
READ_WORDS = re.compile(r"\b(plan|review|inspect|explain|analy[sz]e|map|research|read-only)\b", re.I)


def main() -> int:
    try:
        data = hook_input()
    except ValueError as exc:
        print(json.dumps({"systemMessage": str(exc)}))
        return 0
    prompt = str(data.get("prompt", ""))
    path = resolve_work_item()
    if path and path.is_file():
        item = read_yaml(path)
        context = (
            f"Active Aura work item {item.get('id')}: {item.get('intent')}\n"
            f"Ownership: {item.get('ownership_zones', [])}; allowed: {item.get('allowed_paths', [])}; "
            f"forbidden: {item.get('forbidden_paths', [])}; required agents: {item.get('required_agents', {})}."
        )
    elif WRITE_WORDS.search(prompt) and not READ_WORDS.search(prompt):
        context = "This prompt appears to request repository writes, but no valid AURA_WORK_ITEM is active. Plan and validate a work item before using writing tools."
    else:
        context = "No Aura work item is active. Keep this turn read-only or create and validate the work item before writing."
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": context}}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
