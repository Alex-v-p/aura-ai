#!/usr/bin/env python3
from __future__ import annotations

import json
import re

from common import continuation, hook_input, validate_schema, read_json, SCHEMA_DIR, yaml

REVIEWERS = {"general_reviewer", "architecture_guardian", "observability_steward", "security_privacy_reviewer"}
IMPLEMENTERS = {"contract_steward", "core_worker", "observatory_worker", "frontend_worker", "platform_worker", "test_evaluation_engineer"}
ALLOWED_VERDICTS = {
    "general_reviewer": {"PASS", "BLOCK"},
    "architecture_guardian": {"PASS", "BLOCK", "ADR_REQUIRED"},
    "observability_steward": {"PASS", "BLOCK", "NOT_APPLICABLE"},
    "security_privacy_reviewer": {"PASS", "BLOCK"},
}


def parse_message(message: str):
    blocks = re.findall(r"```(?:yaml|yml)?\s*\n(.*?)```", message, re.DOTALL | re.IGNORECASE)
    candidates = blocks or [message]
    for candidate in candidates:
        try:
            value = yaml.safe_load(candidate)
        except yaml.YAMLError:
            continue
        if isinstance(value, dict):
            return value
    return None


def main() -> int:
    try:
        data = hook_input()
    except ValueError as exc:
        print(json.dumps({"decision": "block", "reason": str(exc)}))
        return 0
    agent = str(data.get("agent_type", ""))
    if agent not in REVIEWERS | IMPLEMENTERS | {"integration_maintainer"}:
        print("{}")
        return 0
    if data.get("stop_hook_active"):
        print("{}")
        return 0
    document = parse_message(str(data.get("last_assistant_message", "")))
    if document is None:
        continuation("Return the required Aura YAML handoff or review verdict before stopping.")
        return 0
    schema_name = "review-verdict.schema.json" if agent in REVIEWERS else "integration-report.schema.json" if agent == "integration_maintainer" else "implementation-handoff.schema.json"
    errors = validate_schema(document, read_json(SCHEMA_DIR / schema_name))
    if agent in REVIEWERS and document.get("verdict") not in ALLOWED_VERDICTS[agent]:
        errors.append(f"{agent} cannot return verdict {document.get('verdict')}")
    if agent in REVIEWERS and document.get("verdict") in {"BLOCK", "ADR_REQUIRED"} and not document.get("findings"):
        errors.append(f"{document.get('verdict')} requires at least one finding")
    if agent in IMPLEMENTERS and document.get("status") == "complete":
        if not document.get("scope", {}).get("allowed_paths_respected"):
            errors.append("complete handoff must confirm allowed_paths_respected")
        if document.get("verification", {}).get("failed"):
            errors.append("complete handoff cannot contain failed verification")
        if document.get("unresolved"):
            errors.append("complete handoff cannot contain unresolved items")
    if agent == "integration_maintainer" and document.get("status") == "integrated":
        if document.get("checks", {}).get("failed"):
            errors.append("integrated report cannot contain failed checks")
        if not document.get("changed_paths_valid"):
            errors.append("integrated report must confirm changed_paths_valid")
    if errors:
        continuation("Correct the Aura structured output: " + "; ".join(errors))
    else:
        print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
