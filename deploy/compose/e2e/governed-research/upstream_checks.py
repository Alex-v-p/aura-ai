"""Execute the archived AURA-0142..0147 required checks as hard gates."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

WORK_ITEMS = tuple(f"AURA-{number:04d}" for number in range(142, 148))
CHECK_LINE = re.compile(r"^  - (.+)$")
SENSITIVE_MARKER = "work-items/active/"
EVIDENCE_MANIFEST = "deploy/compose/e2e/governed-research/integration-evidence-v1.json"
LEGACY_SKIP_ALLOWLIST = "deploy/compose/e2e/governed-research/legacy-conditional-skips-v1.json"
DATABASE_ASSIGNMENT = re.compile(r"\b(AURA_[A-Z0-9_]*DATABASE_URL)=")
SKIP_SUMMARY = re.compile(r"^SKIPPED \[(\d+)\] ([^:]+):(\d+): (.+)$")
GOVERNED_SKIP_MARKERS = (
    "test_aura0142",
    "test_aura0143",
    "test_aura0144",
    "test_aura0145",
    "test_aura0146",
    "test_aura0147",
    "test_aura0148",
    "test_generated_artifact",
    "test_task_projection",
    "test_research_delivery",
    "test_document_sql",
    "test_notifications",
    "test_workflow_run_recovery",
)
DATABASE_VARIABLES = (
    "AURA_0023_MIGRATION_FRESH_DATABASE_URL",
    "AURA_0023_MIGRATION_UPGRADE_DATABASE_URL",
    "AURA_0023_PERSONA_DATABASE_URL",
    "AURA_0028_MIGRATION_FRESH_DATABASE_URL",
    "AURA_0028_MIGRATION_UPGRADE_DATABASE_URL",
    "AURA_0032_MIGRATION_FRESH_DATABASE_URL",
    "AURA_0032_MIGRATION_UPGRADE_DATABASE_URL",
    "AURA_0037_MIGRATION_FRESH_DATABASE_URL",
    "AURA_0037_MIGRATION_UPGRADE_DATABASE_URL",
    "AURA_0038_LIVE_ATTACH_DATABASE_URL",
    "AURA_0038_MIGRATION_DATABASE_URL",
    "AURA_0038_MIGRATION_FRESH_DATABASE_URL",
    "AURA_0039_MIGRATION_DATABASE_URL",
    "AURA_0039_MIGRATION_FRESH_DATABASE_URL",
    "AURA_0047_MIGRATION_DATABASE_URL",
    "AURA_0048_MIGRATION_DATABASE_URL",
    "AURA_0053_MIGRATION_DATABASE_URL",
    "AURA_0054_FRESH_DATABASE_URL",
    "AURA_0054_MIGRATION_DATABASE_URL",
    "AURA_0055_DATABASE_URL",
    "AURA_0056_DATABASE_URL",
    "AURA_0056_FRESH_DATABASE_URL",
    "AURA_0056_MIGRATION_DATABASE_URL",
    "AURA_0064_DATABASE_URL",
    "AURA_0064_MIGRATION_DATABASE_URL",
    "AURA_0066_MIGRATION_DATABASE_URL",
    "AURA_0068_DATABASE_URL",
    "AURA_0076_DATABASE_URL",
    "AURA_0077_DATABASE_URL",
    "AURA_0130_MIGRATION_DATABASE_URL",
    "AURA_0133_DATABASE_URL",
    "AURA_0137_DATABASE_URL",
    "AURA_0143_MIGRATION_DATABASE_URL",
    "AURA_0149_MIGRATION_DATABASE_URL",
    "AURA_MIGRATION_FRESH_DATABASE_URL",
    "AURA_MIGRATION_UPGRADE_DATABASE_URL",
    "AURA_TEST_DATABASE_URL",
)


def _command_timeout_seconds(command: str) -> int:
    """Bound every current-state check without timing out frontend builds."""

    long_running_markers = ("playwright", "storybook", "nx run", "build")
    return 900 if any(marker in command.lower() for marker in long_running_markers) else 300


def _database_url(base_url: str, database: str) -> str:
    parsed = urlsplit(base_url)
    if not parsed.hostname or not parsed.username:
        raise RuntimeError("isolated PostgreSQL URL is malformed")
    user = quote(parsed.username, safe="")
    password = quote(parsed.password or "", safe="")
    authority = f"{user}:{password}@{parsed.hostname}"
    if parsed.port is not None:
        authority += f":{parsed.port}"
    return urlunsplit((parsed.scheme, authority, f"/{database}", parsed.query, ""))


def _compose_exec(root: Path, args: list[str], *, timeout_seconds: int = 60) -> None:
    compose_file = os.environ.get("AURA_GOVERNED_RESEARCH_COMPOSE_FILE")
    project = os.environ.get("AURA_GOVERNED_RESEARCH_PROJECT")
    if not compose_file or not project:
        raise RuntimeError("isolated Compose coordinates are unavailable")
    try:
        completed = subprocess.run(
            ["docker", "compose", "-p", project, "-f", compose_file, "exec", "-T", *args],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"isolated dependency operation timed out: {args[0]}") from error
    if completed.returncode != 0:
        raise RuntimeError(f"isolated dependency preparation failed: {args[0]}")


def _prepare_test_environment(
    root: Path, command: str, ordinal: int
) -> tuple[dict[str, str], list[str]]:
    """Provide clean dependency coordinates without changing the recorded command."""

    environment = os.environ.copy()
    if "pytest" not in command:
        return environment, []
    is_broad_core = "services/aura-core/tests -q" in command
    if is_broad_core:
        # The archived broad command intentionally exercises its original
        # conditional semantics.  The runner must not turn every historical
        # optional SQL test live by inheriting the disposable coordinates that
        # dedicated checks receive.
        for variable in (*DATABASE_VARIABLES, "AURA_TEST_NATS_URL", "AURA_TEST_VALKEY_URL"):
            environment.pop(variable, None)
        environment.pop("AURA_TEST_DEPENDENCIES_ISOLATED", None)
        report_root = environment.get("AURA_GOVERNED_RESEARCH_REPORT_ROOT")
        if report_root:
            junit_path = Path(report_root) / f"upstream-{ordinal:03d}.xml"
            junit_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            temporary_root = Path(tempfile.mkdtemp(prefix="aura-0148-upstream-"))
            junit_path = temporary_root / f"upstream-{ordinal:03d}.xml"
        existing_options = environment.get("PYTEST_ADDOPTS", "").strip()
        environment["PYTEST_ADDOPTS"] = (
            f"{existing_options} --junitxml={junit_path}".strip()
        )
        environment["AURA_UPSTREAM_JUNIT_PATH"] = str(junit_path)
        return environment, []
    base_url = environment.get("AURA_TEST_DATABASE_URL")
    if not base_url:
        raise RuntimeError("isolated PostgreSQL URL is unavailable")
    requested = set(DATABASE_ASSIGNMENT.findall(command))
    if not requested and "services/aura-core/tests" not in command:
        return environment, []
    requested.add("AURA_TEST_DATABASE_URL")
    # The document SQL command reads the historical AURA-0068 coordinate even
    # though it is not written in that archived command.  Keep that compatibility
    # input scoped to the dedicated command; broad Core must retain its original
    # conditional-test environment semantics.
    if "test_document_sql.py" in command:
        requested.add("AURA_0068_DATABASE_URL")
    # The notification coverage advertises a separate PostgreSQL coordinate
    # which is not present in its archived command.  Give it the same fresh,
    # isolated database treatment as every other database-backed check.
    if "test_notifications.py" in command:
        requested.add("AURA_0055_DATABASE_URL")
    if not requested:
        return environment, []
    requested = {variable for variable in requested if variable in DATABASE_VARIABLES}
    databases = [
        f"aura_upstream_{ordinal:03d}_{position:02d}"
        for position in range(len(requested))
    ]
    for database in databases:
        _compose_exec(
            root,
            [
                "postgres",
                "psql",
                "-U",
                "aura_fixture",
                "-d",
                "postgres",
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                f"CREATE DATABASE {database}",
            ],
        )
        # The disposable image provides pgvector, but a fresh database does
        # not inherit extensions from template1.  Provision this dependency
        # only; each exact suite remains responsible for its own migrations
        # and schema setup.
        _compose_exec(
            root,
            [
                "postgres",
                "psql",
                "-U",
                "aura_fixture",
                "-d",
                database,
                "-v",
                "ON_ERROR_STOP=1",
                "-c",
                "CREATE EXTENSION IF NOT EXISTS vector",
            ],
        )
    variable_databases = dict(zip(sorted(requested), databases, strict=True))
    for variable, database in variable_databases.items():
        environment[variable] = _database_url(base_url, database)
    environment["AURA_TEST_DEPENDENCIES_ISOLATED"] = "1"
    # The runner declares Valkey as an isolated state dependency.  Clear its
    # disposable logical database between exact commands without exposing any
    # application content in the upstream report.
    if environment.get("AURA_TEST_VALKEY_URL"):
        _compose_exec(root, ["valkey", "valkey-cli", "FLUSHALL"])
    return environment, databases


def _dispose_test_environment(root: Path, databases: list[str]) -> None:
    # The runner gives every command fresh database names, so no command can
    # observe another command's state.  PostgreSQL can hold a DROP DATABASE
    # operation in a checkpoint wait for an unbounded period on Docker
    # Desktop; issuing one drop per variable here would make the gate itself
    # non-deterministic.  The runner's exit trap removes the disposable
    # PostgreSQL volume, which is the authoritative cleanup boundary.
    del root, databases


def _load_legacy_skip_allowlist(root: Path) -> dict[tuple[str, str], dict[str, Any]]:
    path = root / LEGACY_SKIP_ALLOWLIST
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("legacy conditional skip allowlist is unavailable") from error
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise RuntimeError("unsupported legacy conditional skip allowlist")
    entries = document.get("entries")
    if not isinstance(entries, list) or not entries:
        raise RuntimeError("legacy conditional skip allowlist is empty")
    result: dict[tuple[str, str], dict[str, Any]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise RuntimeError("malformed legacy conditional skip allowlist entry")
        node_ids = entry.get("nodeIds")
        source = entry.get("source")
        reason = entry.get("reason")
        evidence = entry.get("evidence")
        coverage = entry.get("coverage")
        if (
            not isinstance(node_ids, list)
            or not node_ids
            or not all(isinstance(node_id, str) for node_id in node_ids)
            or not isinstance(source, str)
            or not isinstance(reason, str)
            or not isinstance(evidence, list)
            or not evidence
            or not all(isinstance(item, str) for item in evidence)
            or not isinstance(coverage, dict)
            or coverage.get("status") not in {"passed", "integrated"}
            or coverage.get("kind") not in {"dedicated-command", "immutable-integration"}
        ):
            raise RuntimeError("malformed legacy conditional skip allowlist entry")
        if any(any(marker in node_id for marker in GOVERNED_SKIP_MARKERS) for node_id in node_ids):
            is_aura0150_immutable = (
                any("test_workflow_run_recovery" in node_id for node_id in node_ids)
                and coverage.get("kind") == "immutable-integration"
                and coverage.get("status") == "integrated"
                and "work-items/integration/AURA-0150.yaml"
                in coverage.get("reports", [])
            )
            if (
                not is_aura0150_immutable
                and (
                    coverage.get("kind") != "dedicated-command"
                    or coverage.get("status") != "passed"
                )
            ):
                raise RuntimeError("governed research skip lacks green dedicated coverage")
        key = (source, reason)
        if key in result:
            raise RuntimeError("duplicate legacy conditional skip allowlist entry")
        result[key] = entry
    return result


def _skip_records(output: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in output.splitlines():
        match = SKIP_SUMMARY.match(line.strip())
        if match:
            records.append(
                {
                    "count": int(match.group(1)),
                    "source": f"{match.group(2)}:{match.group(3)}",
                    "reason": match.group(4),
                }
            )
    return records


def _junit_skip_nodes(path: Path) -> dict[tuple[str, str], set[str]]:
    try:
        document = ET.parse(path)
    except (OSError, ET.ParseError) as error:
        raise RuntimeError("broad Core skip JUnit evidence is unavailable") from error
    observed: dict[tuple[str, str], set[str]] = {}
    for testcase in document.getroot().iter("testcase"):
        skipped = testcase.find("skipped")
        if skipped is None:
            continue
        classname = testcase.attrib.get("classname", "")
        parts = classname.split(".")
        if len(parts) < 4 or parts[:3] != ["services", "aura-core", "tests"]:
            raise RuntimeError("broad Core JUnit evidence has an invalid test module")
        source = "services/aura-core/tests/" + "/".join(parts[3:]) + ".py"
        reason = skipped.attrib.get("message", "")
        node_id = source + "::" + testcase.attrib.get("name", "")
        observed.setdefault((source, reason), set()).add(node_id)
    return observed


def _validate_skips(
    root: Path,
    output: str,
    *,
    allow_legacy_conditional: bool,
    junit_path: Path | None = None,
) -> list[dict[str, Any]]:
    records = _skip_records(output)
    has_skip_summary = bool(re.search(r"\b\d+ skipped\b", output))
    if not records and not has_skip_summary:
        return records
    if not allow_legacy_conditional:
        raise RuntimeError("dedicated live check produced a skip")
    allowlist = _load_legacy_skip_allowlist(root)
    if not records:
        if junit_path is None:
            raise RuntimeError("broad Core skip JUnit evidence is unavailable")
        observed = _junit_skip_nodes(junit_path)
        records = [
            {
                "count": len(entry["nodeIds"]),
                "source": str(entry["source"]),
                "reason": str(entry["reason"]),
            }
            for entry in allowlist.values()
            if (
                str(entry["source"]).rsplit(":", 1)[0],
                str(entry["reason"]),
            ) in observed
        ]
    failures: list[str] = []
    for record in records:
        entry = allowlist.get((record["source"], record["reason"]))
        if entry is not None and record["count"] == len(entry["nodeIds"]):
            continue
        # Pytest can aggregate skips raised by an imported fixture under the
        # fixture's source line (for example notification-stream coverage is
        # reported under test_notifications.py:95).  Keep the exact summary
        # count check, then rely on the JUnit node-ID comparison below for the
        # authoritative per-module mapping.
        grouped_count = sum(
            len(candidate["nodeIds"])
            for (source, reason), candidate in allowlist.items()
            if reason == record["reason"]
        )
        if entry is None and grouped_count == record["count"]:
            continue
        if entry is None or grouped_count != record["count"]:
            failures.append(f"{record['source']}: {record['reason']}")
    if failures:
        raise RuntimeError("unallowlisted or changed skip: " + "; ".join(failures))
    if junit_path is None:
        raise RuntimeError("broad Core skip JUnit evidence is unavailable")
    observed = _junit_skip_nodes(junit_path)
    expected: dict[tuple[str, str], set[str]] = {}
    for entry in allowlist.values():
        key = (str(entry["source"]).rsplit(":", 1)[0], str(entry["reason"]))
        expected.setdefault(key, set()).update(str(node) for node in entry["nodeIds"])
    for key, nodes in observed.items():
        if expected.get(key) != nodes:
            failures.append(f"JUnit node IDs changed for {key[0]}: {key[1]}")
    if failures:
        raise RuntimeError("unallowlisted or changed skip: " + "; ".join(failures))
    return records


def required_checks(root: Path) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []
    for work_item in WORK_ITEMS:
        path = root / "work-items" / "archived" / f"{work_item}.yaml"
        if not path.is_file():
            raise RuntimeError(f"mandatory archived work item is missing: {work_item}")
        in_checks = False
        found: list[str] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line == "required_checks:":
                in_checks = True
                continue
            if in_checks and line and not line.startswith(" "):
                break
            if in_checks:
                match = CHECK_LINE.match(line)
                if match:
                    found.append(match.group(1))
        if not found:
            raise RuntimeError(f"mandatory check inventory is empty: {work_item}")
        for command in found:
            # Keep the recorded command byte-for-byte.  Commands that name an
            # active work item are repository-state-sensitive and are handled
            # by immutable integration evidence below rather than rewritten.
            checks.append((f"{work_item}:{command}", command))
    return checks


def _sensitive_commands(root: Path, work_item: str) -> tuple[str, ...]:
    return tuple(
        command
        for name, command in required_checks(root)
        if name.startswith(f"{work_item}:")
        and (SENSITIVE_MARKER in command or command == "git diff --check")
    )


def _load_evidence_manifest(root: Path) -> dict[str, dict[str, Any]]:
    path = root / EVIDENCE_MANIFEST
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("immutable integration evidence manifest is unavailable") from error
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise RuntimeError("unsupported integration evidence manifest")
    entries = document.get("items")
    manifest_items = {
        item.get("workItem") for item in entries if isinstance(item, dict)
    } if isinstance(entries, list) else set()
    if not isinstance(entries, list) or manifest_items != set(WORK_ITEMS):
        raise RuntimeError("integration evidence manifest does not cover every work item")
    result: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict) or not isinstance(item.get("workItem"), str):
            raise RuntimeError("malformed integration evidence entry")
        work_item = item["workItem"]
        if work_item in result:
            raise RuntimeError("duplicate integration evidence entry")
        result[work_item] = item
    return result


def _resolve_commit(root: Path, commit: str) -> str:
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", commit):
        raise RuntimeError("integration report contains an invalid commit identity")
    completed = subprocess.run(
        ["git", "rev-parse", "--verify", f"{commit}^{{commit}}"],
        cwd=root,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    resolved = completed.stdout.strip()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", resolved):
        raise RuntimeError(f"integration commit is not present: {commit}")
    return resolved


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    completed = subprocess.run(
        ["git", "merge-base", "--is-ancestor", ancestor, descendant],
        cwd=root,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return completed.returncode == 0


def _normalized_evidence(value: str) -> str:
    """Match folded YAML prose without changing the recorded command."""

    return " ".join(value.split())


def _exact_or_recorded_evidence(
    command: str,
    work_item: str,
    passed: list[Any],
    bindings: list[Any] | None = None,
) -> list[str]:
    """Bind a requested check to one exact or explicitly recorded PASS item.

    Older integrated reports record the reviewed validator with its archived
    path or a concise validator description.  Those reports are immutable, so
    the compatibility terms below identify the same command family and work
    item without rewriting the active command or treating an unrelated PASS as
    equivalent.  New reports still use the exact normalized command match.
    """

    normalized_command = _normalized_evidence(command)
    explicit = [
        binding
        for binding in (bindings or [])
        if isinstance(binding, dict)
        and binding.get("requestedCommand") == command
    ]
    if explicit:
        if len(explicit) != 1 or not isinstance(explicit[0].get("evidenceText"), str):
            raise RuntimeError("immutable evidence binding is malformed")
        evidence_text = _normalized_evidence(explicit[0]["evidenceText"])
        return [
            item
            for item in passed
            if isinstance(item, str) and _normalized_evidence(item) == evidence_text
        ]
    exact = [
        item
        for item in passed
        if isinstance(item, str)
        and normalized_command in _normalized_evidence(item)
    ]
    if exact:
        return exact
    terms = {
        "validate-work-item.py": "validate-work-item",
        "validate-changed-paths.py": "changed-path",
        "validate-contract-changes.py": "contract",
        "validate-observability.py": "observability",
        "validate-baseline-protection.py": "baseline",
    }
    command_term = next(
        (term for marker, term in terms.items() if marker in command), None
    )
    if command_term is None:
        return []
    if work_item not in " ".join(item for item in passed if isinstance(item, str)):
        return []
    candidates = [
        item
        for item in passed
        if isinstance(item, str)
        and command_term in _normalized_evidence(item).lower()
    ]
    scoped = [item for item in candidates if work_item in item]
    return scoped if scoped else candidates


def _immutable_integration_evidence(
    root: Path,
    work_item: str,
    command: str,
    manifest: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Validate a pinned integrated report without pretending to execute it."""

    entry = manifest.get(work_item)
    if entry is None:
        raise RuntimeError(f"integration evidence entry is missing: {work_item}")
    report_name = entry.get("report")
    report_commit = entry.get("reportCommit")
    checkpoints = entry.get("checkpointCommits")
    declared = entry.get("sensitiveCommands")
    bindings = entry.get("evidenceBindings")
    if (
        report_name != f"work-items/integration/{work_item}.yaml"
        or not isinstance(report_commit, str)
        or not isinstance(checkpoints, list)
        or not checkpoints
        or not isinstance(declared, list)
        or tuple(declared) != _sensitive_commands(root, work_item)
        or command not in declared
        or not isinstance(bindings, list)
        or len(bindings) != len(declared)
    ):
        raise RuntimeError(f"integration evidence inventory mismatch: {work_item}")
    # Every sensitive command must be bound explicitly to one immutable report
    # line and the report commit named by the manifest.  Do not fall back to
    # fuzzy validator-family matching: a nearby PASS is not proof for the
    # requested command.
    binding = next(
        (
            candidate
            for candidate in bindings
            if isinstance(candidate, dict)
            and candidate.get("requestedCommand") == command
        ),
        None,
    )
    if (
        not isinstance(binding, dict)
        or set(binding) != {"requestedCommand", "reportCommit", "evidenceText"}
        or binding.get("reportCommit") != report_commit
        or not isinstance(binding.get("evidenceText"), str)
        or not binding["evidenceText"].strip()
        or sum(
            1
            for candidate in bindings
            if isinstance(candidate, dict)
            and candidate.get("requestedCommand") == command
        )
        != 1
    ):
        raise RuntimeError(f"exact integration command binding is missing: {work_item}")
    resolved_report_commit = _resolve_commit(root, report_commit)
    resolved_checkpoints = tuple(_resolve_commit(root, str(commit)) for commit in checkpoints)
    if any(
        not _is_ancestor(root, checkpoint, resolved_report_commit)
        for checkpoint in resolved_checkpoints
    ):
        raise RuntimeError(f"integration checkpoint is outside report ancestry: {work_item}")
    report_path = root / str(report_name)
    try:
        encoded = subprocess.run(
            ["git", "show", f"{resolved_report_commit}:{report_name}"],
            cwd=root,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
        if encoded.returncode != 0:
            raise RuntimeError(f"pinned integration report is absent: {work_item}")
        document = yaml.safe_load(encoded.stdout)
    except OSError as error:
        raise RuntimeError(f"pinned integration report cannot be read: {work_item}") from error
    if not isinstance(document, dict) or document.get("status") != "integrated":
        raise RuntimeError(f"integration report is not integrated: {work_item}")
    checks = document.get("checks")
    passed = checks.get("passed") if isinstance(checks, dict) else None
    if not isinstance(passed, list) or not passed:
        raise RuntimeError(f"integration check evidence is missing: {work_item}")
    exact_matches = [
        item
        for item in passed
        if isinstance(item, str)
        and _normalized_evidence(item) == _normalized_evidence(binding["evidenceText"])
    ]
    if len(exact_matches) != 1:
        raise RuntimeError(f"exact integration check evidence is missing or ambiguous: {work_item}")
    integrated_commits = document.get("integrated_commits")
    if not isinstance(integrated_commits, list):
        raise RuntimeError(f"integration commit inventory is malformed: {work_item}")
    resolved_integrated = tuple(
        _resolve_commit(root, str(commit)) for commit in integrated_commits
    )
    if any(commit not in resolved_checkpoints for commit in resolved_integrated):
        raise RuntimeError(f"integration commit is absent from manifest checkpoints: {work_item}")
    return {
        "status": "passed",
        "exitCode": 0,
        "evidence": "integration-report",
        "integrationReport": str(report_path.relative_to(root)),
        "reportCommit": resolved_report_commit,
        "checkpointCommitCount": len(resolved_checkpoints),
    }


def run(root: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    checks = required_checks(root)
    manifest = _load_evidence_manifest(root)
    for ordinal, (name, command) in enumerate(checks):
        work_item = name.split(":", 1)[0]
        if SENSITIVE_MARKER in command or command == "git diff --check":
            started = time.monotonic()
            try:
                evidence = _immutable_integration_evidence(root, work_item, command, manifest)
                result = {
                    "name": name,
                    "command": command,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    **evidence,
                }
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                result = {
                    "name": name,
                    "command": command,
                    "status": "failed",
                    "exitCode": 1,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "failureClass": "missing_integration_evidence",
                    "reason": type(error).__name__,
                }
            results.append(result)
            continue
        started = time.monotonic()
        databases: list[str] = []
        try:
            command_environment, databases = _prepare_test_environment(root, command, ordinal)
            completed = subprocess.run(
                ["bash", "-lc", command],
                cwd=root,
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=command_environment,
                timeout=_command_timeout_seconds(command),
            )
        except RuntimeError as error:
            results.append(
                {
                    "name": name,
                    "command": command,
                    "status": "failed",
                    "exitCode": 1,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "failureClass": "dependency_setup_failed",
                    "reason": type(error).__name__,
                }
            )
            continue
        except subprocess.TimeoutExpired:
            results.append(
                {
                    "name": name,
                    "command": command,
                    "status": "failed",
                    "exitCode": 124,
                    "durationMs": int((time.monotonic() - started) * 1000),
                    "failureClass": "command_timeout",
                }
            )
            continue
        finally:
            _dispose_test_environment(root, databases)
        output = completed.stdout or ""
        allow_legacy_conditional = "services/aura-core/tests -q" in command
        skip_error: RuntimeError | None = None
        try:
            skip_records = _validate_skips(
                root,
                output,
                allow_legacy_conditional=allow_legacy_conditional,
                junit_path=(
                    Path(command_environment["AURA_UPSTREAM_JUNIT_PATH"])
                    if allow_legacy_conditional
                    and command_environment.get("AURA_UPSTREAM_JUNIT_PATH")
                    else None
                ),
            )
            if re.search(r"\b\d+ skipped\b", output) and not skip_records:
                raise RuntimeError("skip summary could not be mapped to node IDs")
        except RuntimeError as error:
            skip_records = []
            skip_error = error
        results.append(
            {
                "name": name,
                "command": command,
                "status": "failed"
                if completed.returncode != 0 or skip_error is not None
                else "passed",
                "exitCode": completed.returncode,
                "durationMs": int((time.monotonic() - started) * 1000),
                **(
                    {
                        "failureClass": "mandatory_skip",
                        "reason": str(skip_error),
                        "skipCount": len(skip_records),
                    }
                    if skip_error is not None
                    else {}
                ),
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    try:
        results = run(args.root)
    except (OSError, RuntimeError) as error:
        print(f"mandatory upstream inventory failed closed: {error}", file=sys.stderr)
        return 2
    args.report.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "runner": "aura-governed-research-upstream@1",
                "capturePolicy": "metadata-only",
                "checks": results,
            },
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    failed = sum(1 for item in results if item["status"] != "passed")
    print(f"upstream checks: {len(results)} executed, {failed} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
