"""Normalize AURA-0148 Python and Playwright JUnit observations.

Only bounded test identifiers, outcomes, counts, exit codes, and command labels
cross the platform/evaluation boundary.  Test output and application content
are deliberately excluded.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from hashlib import sha256
from pathlib import Path
from typing import Any, cast

_INVENTORY_PATH = Path(__file__).with_name("acceptance-inventory-v1.json")
_INVENTORY_DIGEST = "4200e274d675f725f95543517e039d9d51ba24a9043e4d3004dfd7e6639db0c4"


def _load_expected_inventory(path: Path = _INVENTORY_PATH) -> dict[str, frozenset[str]]:
    """Load the immutable full acceptance inventory for both producers."""

    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("acceptance inventory is unavailable or invalid") from error
    if sha256(raw).hexdigest() != _INVENTORY_DIGEST:
        raise ValueError("acceptance inventory digest mismatch")
    if not isinstance(document, dict):
        raise ValueError("acceptance inventory shape is invalid")
    document = cast(dict[str, Any], document)
    if set(cast(dict[str, object], document)) != {
        "schemaVersion",
        "inventoryRevision",
        "python",
        "playwright",
    }:
        raise ValueError("acceptance inventory shape is invalid")
    if (
        document["schemaVersion"] != 1
        or document["inventoryRevision"] != "aura0148-acceptance-inventory-v1"
    ):
        raise ValueError("acceptance inventory provenance is invalid")
    result: dict[str, frozenset[str]] = {}
    for producer in ("python", "playwright"):
        raw_values = cast(object, document[producer])
        if not isinstance(raw_values, list):
            raise ValueError(f"acceptance inventory {producer} identities are invalid")
        values = cast(list[object], raw_values)
        if not values or any(not isinstance(value, str) or not value for value in values):
            raise ValueError(f"acceptance inventory {producer} identities are invalid")
        string_values = cast(list[str], values)
        if len(string_values) != len(set(string_values)):
            raise ValueError(f"acceptance inventory {producer} identities are invalid")
        result[producer] = frozenset(string_values)
    if len(result["python"]) != 25 or len(result["playwright"]) != 33:
        raise ValueError("acceptance inventory counts are invalid")
    return result


def _parse(
    path: Path,
    command: str,
    exit_code: int,
    producer: str,
    expected_ids: frozenset[str],
) -> dict[str, Any]:
    try:
        root = ET.parse(path).getroot()
    except (OSError, ET.ParseError) as error:
        raise ValueError(f"acceptance result XML is unavailable: {path.name}") from error
    tests: list[dict[str, object]] = []
    for suite in root.iter("testsuite"):
        hostname = "python" if producer == "python" else suite.get("hostname", "")
        if not hostname or hostname not in {"python", "desktop", "tablet", "mobile"}:
            raise ValueError("acceptance test producer identity is missing")
        for case in suite.findall("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            if not classname or not name:
                raise ValueError("acceptance test identity is missing")
            outcome = "passed"
            if case.find("failure") is not None or case.find("error") is not None:
                outcome = "failed"
            elif case.find("skipped") is not None:
                outcome = "skipped"
            tests.append(
                {"id": f"{hostname}::{classname}::{name}", "outcome": outcome, "assertionCount": 1}
            )
    counts: dict[str, int] = {
        "discovered": len(tests),
        "passed": sum(item["outcome"] == "passed" for item in tests),
        "failed": sum(item["outcome"] == "failed" for item in tests),
        "skipped": sum(item["outcome"] == "skipped" for item in tests),
    }
    if (
        not tests
        or sum(counts[key] for key in ("passed", "failed", "skipped")) != counts["discovered"]
    ):
        raise ValueError("acceptance test discovery is empty or inconsistent")
    observed_ids = [str(item["id"]) for item in tests]
    if len(observed_ids) != len(set(observed_ids)) or frozenset(observed_ids) != expected_ids:
        raise ValueError(f"acceptance {producer} inventory does not match immutable plan")
    if exit_code != 0 or counts["failed"] or counts["skipped"]:
        failed_ids = [item["id"] for item in tests if item["outcome"] != "passed"]
        detail = ", ".join(str(test_id) for test_id in failed_ids[:8])
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"acceptance tests did not pass{suffix}")
    # Preserve only a stable command class; full shell commands can contain
    # paths, hostnames, or credentials and are not evaluation metadata.
    command_class = (
        "aura0148.playwright.scoped" if "playwright" in command else "aura0148.python.scoped"
    )
    return {"command": command_class, "exitCode": exit_code, **counts, "tests": tests}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python-xml", type=Path, required=True)
    parser.add_argument("--playwright-xml", type=Path, required=True)
    parser.add_argument("--python-command", required=True)
    parser.add_argument("--playwright-command", required=True)
    parser.add_argument("--python-exit-code", type=int, required=True)
    parser.add_argument("--playwright-exit-code", type=int, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        inventory = _load_expected_inventory()
        report = {
            "schemaVersion": 1,
            "runner": "aura0148-observed-acceptance@1",
            "capturePolicy": "metadata_only",
            "python": _parse(
                args.python_xml,
                args.python_command,
                args.python_exit_code,
                "python",
                inventory["python"],
            ),
            "playwright": _parse(
                args.playwright_xml,
                args.playwright_command,
                args.playwright_exit_code,
                "playwright",
                inventory["playwright"],
            ),
        }
        args.report.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    except (OSError, ValueError) as error:
        print(f"acceptance observations failed closed: {error}", file=sys.stderr)
        return 1
    report_data = cast(dict[str, Any], report)
    print(
        f"observed acceptance tests: {report_data['python']['passed']} Python, "
        f"{report_data['playwright']['passed']} Playwright"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
