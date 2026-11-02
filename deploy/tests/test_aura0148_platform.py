"""AURA-0148 synthetic governed-research runner and topology gates."""

from __future__ import annotations

import importlib
import json
import os
import re
import subprocess
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from pathlib import Path
from typing import IO, Any, cast

import yaml

ROOT = Path(__file__).parents[2]
E2E = ROOT / "deploy/compose/e2e/governed-research"
RUNNER = ROOT / "deploy/compose/run-governed-research-e2e.sh"


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((E2E / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _observed_test(surface: str, module: str, name: str) -> str:
    return f"{surface}::{module}::{name}"


def test_synthetic_topology_has_isolated_dependencies_and_no_public_ports() -> None:
    document = _compose()
    services = cast(dict[str, Any], document["services"])
    expected = {
        "postgres",
        "nats",
        "valkey",
        "garage",
        "synthetic-static-fetcher",
        "synthetic-browser-fetcher",
        "synthetic-frontend",
    }
    assert set(services) == expected
    for name, service in services.items():
        if name == "postgres":
            assert service["ports"][0]["target"] == 5432
            assert service["ports"][0]["host_ip"] == "127.0.0.1"
        elif name == "nats":
            assert service["ports"][0]["target"] == 4222
            assert service["ports"][0]["host_ip"] == "127.0.0.1"
        elif name == "valkey":
            assert service["ports"][0]["target"] == 6379
            assert service["ports"][0]["host_ip"] == "127.0.0.1"
        else:
            assert "ports" not in service
        assert "build" not in service
        assert "privileged" not in service
    networks = cast(dict[str, Any], document["networks"])
    assert set(networks) == {"state", "static-provider", "browser-provider", "frontend"}
    assert networks["state"]["internal"] is False
    assert all(
        networks[name].get("internal") is True
        for name in ("static-provider", "browser-provider", "frontend")
    )


def test_state_endpoints_bind_only_to_loopback() -> None:
    services = cast(dict[str, Any], _compose()["services"])
    assert services["postgres"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["nats"]["ports"][0]["host_ip"] == "127.0.0.1"
    assert services["valkey"]["ports"][0]["host_ip"] == "127.0.0.1"


def test_fetchers_cannot_reach_state_or_bus_networks() -> None:
    services = cast(dict[str, Any], _compose()["services"])
    assert services["postgres"]["networks"] == ["state"]
    assert services["nats"]["networks"] == ["state"]
    assert services["valkey"]["networks"] == ["state"]
    assert services["garage"]["networks"] == ["state"]
    assert services["synthetic-static-fetcher"]["networks"] == ["static-provider"]
    assert services["synthetic-browser-fetcher"]["networks"] == ["browser-provider"]
    assert services["synthetic-frontend"]["networks"] == ["frontend"]
    for service in services.values():
        assert len(service["networks"]) == 1


def test_immutable_evidence_manifest_covers_exact_sensitive_inventory() -> None:
    manifest = json.loads(
        (E2E / "integration-evidence-v1.json").read_text(encoding="utf-8")
    )
    assert manifest["manifestRevision"] == "aura0148-integration-evidence-v1"
    assert {item["workItem"] for item in manifest["items"]} == {
        f"AURA-{number:04d}" for number in range(142, 148)
    }
    for item in manifest["items"]:
        assert item["report"] == f"work-items/integration/{item['workItem']}.yaml"
        assert len(item["reportCommit"]) == 40
        assert item["checkpointCommits"]
        assert item["sensitiveCommands"]
        assert all(
            "work-items/active/" in command or command == "git diff --check"
            for command in item["sensitiveCommands"]
        )


def test_research_provider_profiles_are_disabled_and_frontend_is_http() -> None:
    services = cast(dict[str, Any], _compose()["services"])
    static = cast(dict[str, Any], services["synthetic-static-fetcher"])
    browser = cast(dict[str, Any], services["synthetic-browser-fetcher"])
    assert static["environment"]["AURA_RESEARCH_STATIC_ENABLED"] == "false"
    assert browser["environment"]["AURA_RESEARCH_BROWSER_ENABLED"] == "false"
    assert services["synthetic-frontend"]["environment"]["BASE_URL"].startswith("http://")
    source = RUNNER.read_text(encoding="utf-8")
    assert "docker push" not in source
    assert "--push" not in source
    assert "cloudflare" not in source.lower()
    assert "nginx" not in source.lower()


def test_acceptance_inventory_rejects_missing_extra_and_replaced_tests() -> None:
    import sys

    sys.path.insert(0, str(E2E))
    acceptance = importlib.import_module("acceptance_results")
    inventory = acceptance._load_expected_inventory()

    def write_junit(path: Path, test_ids: list[str]) -> None:
        suites = ET.Element("testsuites")
        suite_by_surface: dict[str, ET.Element] = {}
        for test_id in test_ids:
            surface, classname, name = test_id.split("::", 2)
            suite = suite_by_surface.setdefault(
                surface,
                ET.SubElement(suites, "testsuite", hostname=surface),
            )
            ET.SubElement(suite, "testcase", classname=classname, name=name)
        ET.ElementTree(suites).write(path, encoding="utf-8", xml_declaration=True)

    def assert_rejected(test_ids: list[str], label: str) -> None:
        with tempfile.TemporaryDirectory(prefix=f"aura0148-{label}-") as directory:
            report = Path(directory) / "playwright.xml"
            write_junit(report, test_ids)
            try:
                acceptance._parse(
                    report,
                    "aura0148.playwright.scoped",
                    0,
                    "playwright",
                    inventory["playwright"],
                )
            except ValueError as error:
                assert "inventory" in str(error)
            else:
                raise AssertionError(f"{label} acceptance inventory mutation was accepted")

    accessibility_id = next(
        item for item in inventory["playwright"] if "session loss" in item
    )
    assert_rejected(
        [item for item in inventory["playwright"] if item != accessibility_id],
        "missing-accessibility-recovery",
    )
    extra_id = "desktop::governed-research-accessibility.spec.ts::extra noncanonical test"
    assert_rejected([*inventory["playwright"], extra_id], "extra-test")
    replacement_id = "desktop::governed-research-accessibility.spec.ts::replacement test"
    assert_rejected(
        [item for item in inventory["playwright"] if item != accessibility_id]
        + [replacement_id],
        "replacement-test",
    )


def test_synthetic_runner_passes_and_emits_metadata_only_evidence() -> None:
    result = subprocess.run(
        ["bash", str(RUNNER), "--synthetic-only"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "metadata-only" not in result.stdout
    assert "synthetic full-stack acceptance passed" in result.stdout


def test_runner_fails_when_a_declared_service_observation_is_broken() -> None:
    environment = os.environ.copy()
    environment["AURA_GOVERNED_RESEARCH_INJECT_FAILURE"] = "frontend"
    result = subprocess.run(
        ["bash", str(RUNNER), "--synthetic-only"],
        cwd=ROOT,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_fixture_fails_when_observation_evidence_is_false() -> None:
    fixture = E2E / "fixture.py"
    with tempfile.TemporaryDirectory(prefix="aura0148-observation-") as directory:
        root = Path(directory)
        observations = root / "observations.json"
        report = root / "report.json"
        observations.write_text(
            json.dumps(
                {
                    "postgres": False,
                    "nats": True,
                    "valkey": True,
                    "garage": True,
                    "static_fetcher": True,
                    "browser_fetcher": True,
                    "frontend": True,
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["python3", str(fixture), "--observations", str(observations), "--report", str(report)],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0
        assert not report.exists()


def test_upstream_fails_when_immutable_integration_evidence_is_missing() -> None:
    script = (
        "from pathlib import Path; import sys; "
        f"sys.path.insert(0, {str(E2E)!r}); "
        "from upstream_checks import _immutable_integration_evidence, _load_evidence_manifest; "
        "_immutable_integration_evidence("
        "Path.cwd(), 'AURA-0999', 'python3 tooling/architecture/validate-work-item.py', {})"
    )
    result = subprocess.run(
        ["python3", "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_immutable_evidence_binds_exact_command_and_commit_ancestry() -> None:
    import copy
    import sys

    sys.path.insert(0, str(E2E))
    upstream = importlib.import_module("upstream_checks")
    load_evidence_manifest = cast(
        Callable[[Path], dict[str, dict[str, Any]]],
        upstream._load_evidence_manifest,
    )
    immutable_integration_evidence = cast(
        Callable[..., dict[str, Any]],
        upstream._immutable_integration_evidence,
    )

    manifest = load_evidence_manifest(ROOT)
    work_item = "AURA-0145"
    command = manifest[work_item]["sensitiveCommands"][0]
    valid = immutable_integration_evidence(ROOT, work_item, command, manifest)
    assert valid["status"] == "passed"

    unrelated = copy.deepcopy(manifest)
    unrelated[work_item]["sensitiveCommands"] = [
        "python3 tooling/architecture/validate-work-item.py "
        "work-items/active/AURA-0999.yaml"
    ]
    try:
        immutable_integration_evidence(
            ROOT, work_item, unrelated[work_item]["sensitiveCommands"][0], unrelated
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("unrelated PASS evidence was accepted")

    mismatched_commit = copy.deepcopy(manifest)
    mismatched_commit[work_item]["reportCommit"] = "0" * 40
    try:
        immutable_integration_evidence(ROOT, work_item, command, mismatched_commit)
    except RuntimeError:
        pass
    else:
        raise AssertionError("mismatched report commit was accepted")

    non_ancestor = copy.deepcopy(manifest)
    non_ancestor[work_item]["checkpointCommits"] = [
        manifest["AURA-0147"]["reportCommit"]
    ]
    try:
        immutable_integration_evidence(ROOT, work_item, command, non_ancestor)
    except RuntimeError:
        pass
    else:
        raise AssertionError("non-ancestor checkpoint was accepted")

    explicit_bindings = {
        "AURA-0143": (
            "python3 tooling/architecture/validate-baseline-protection.py "
            "--work-item work-items/active/AURA-0143.yaml"
        ),
        "AURA-0144": (
            "python3 tooling/architecture/validate-work-item.py "
            "work-items/active/AURA-0144.yaml"
        ),
        "AURA-0146": (
            "python3 tooling/architecture/validate-contract-changes.py "
            "--work-item work-items/active/AURA-0146.yaml"
        ),
    }
    for item, bound_command in explicit_bindings.items():
        assert (
            immutable_integration_evidence(ROOT, item, bound_command, manifest)["status"]
            == "passed"
        )
        broken = copy.deepcopy(manifest)
        matching_binding = next(
            binding
            for binding in broken[item]["evidenceBindings"]
            if binding["requestedCommand"] == bound_command
        )
        matching_binding["evidenceText"] = "unrelated PASS evidence"
        try:
            immutable_integration_evidence(ROOT, item, bound_command, broken)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unrelated explicit evidence was accepted")

    # Every one of the 35 repository-state-sensitive mappings is explicit.
    # This also exercises report lookup and checkpoint ancestry for each
    # work-item command, rather than sampling one command family.
    for item, item_entry in manifest.items():
        for bound_command in item_entry["sensitiveCommands"]:
            evidence = immutable_integration_evidence(ROOT, item, bound_command, manifest)
            assert evidence["status"] == "passed"
            broken = copy.deepcopy(manifest)
            broken[item]["evidenceBindings"] = [
                binding
                for binding in broken[item]["evidenceBindings"]
                if binding["requestedCommand"] != bound_command
            ]
            try:
                immutable_integration_evidence(ROOT, item, bound_command, broken)
            except RuntimeError:
                pass
            else:
                raise AssertionError("missing exact evidence binding was accepted")


def test_release_gate_fails_when_a_typed_decision_is_changed() -> None:
    script = (
        "from pathlib import Path; import sys; "
        f"sys.path.insert(0, {str(E2E)!r}); "
        "from evaluation_adapter import validate_report; "
        "validate_report({'capturePolicy':'metadata_only', 'realProvidersEnabled':False, "
        "'published':False, 'status':'failed', 'evaluatorCount':1, "
        "'decisions':[{'evaluatorId':'research_privacy@1','score':0.0, "
        "'threshold':1.0,'passed':False,'reason':'reported_failure'}]})"
    )
    result = subprocess.run(
        ["python3", "-c", script],
        cwd=ROOT,
        env={
            **os.environ,
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONPATH": f"{ROOT / 'services/aura-core/src'}:{ROOT}",
        },
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0


def test_upstream_inventory_covers_every_archived_required_check() -> None:
    script = E2E / "upstream_checks.py"
    result = subprocess.run(
        [
            "python3",
            "-c",
            (
                "from pathlib import Path; import sys; "
                f"sys.path.insert(0, {str(E2E)!r}); "
                "from upstream_checks import required_checks; "
                "print(len(required_checks(Path.cwd())))"
            ),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    expected = 0
    for number in range(142, 148):
        document = yaml.safe_load(
            (ROOT / "work-items/archived" / f"AURA-{number:04d}.yaml").read_text(
                encoding="utf-8"
            )
        )
        expected += len(document["required_checks"])
    assert int(result.stdout.strip()) == expected
    assert script.is_file()


def test_upstream_runner_uses_bounded_long_frontend_timeouts() -> None:
    source = (E2E / "upstream_checks.py").read_text(encoding="utf-8")
    assert "timeout=_command_timeout_seconds(command)" in source
    assert "return 900" in source


def test_allowlisted_legacy_conditional_skip_is_accepted() -> None:
    document = cast(
        dict[str, Any],
        json.loads(
            (E2E / "legacy-conditional-skips-v1.json").read_text(encoding="utf-8")
        ),
    )
    entries = cast(list[dict[str, Any]], document["entries"])
    grouped_sources: dict[tuple[str, str], int] = {}
    for candidate in entries:
        key = (
            cast(str, candidate["source"]).rsplit(":", 1)[0],
            cast(str, candidate["reason"]),
        )
        grouped_sources[key] = grouped_sources.get(key, 0) + 1
    entry = next(
        candidate
        for candidate in entries
        if grouped_sources[
            (
                cast(str, candidate["source"]).rsplit(":", 1)[0],
                cast(str, candidate["reason"]),
            )
        ]
        == 1
    )
    source_path = cast(str, entry["source"])
    reason = cast(str, entry["reason"])
    node_id = cast(list[str], entry["nodeIds"])[0]
    module = node_id.split("::", 1)[0].removesuffix(".py").replace("/", ".")
    name = node_id.split("::", 1)[1]
    output = f"SKIPPED [{len(cast(list[str], entry['nodeIds']))}] {source_path}: {reason}"
    with tempfile.TemporaryDirectory(prefix="aura0148-junit-") as directory:
        junit = Path(directory) / "broad.xml"
        junit.write_text(
            f'<testsuite><testcase classname="{module}" name="{name}">'
            f'<skipped message="{reason}"/></testcase></testsuite>',
            encoding="utf-8",
        )
        script = (
            "from pathlib import Path; import sys; "
            f"sys.path.insert(0, {str(E2E)!r}); "
            "from upstream_checks import _validate_skips; "
            f"_validate_skips(Path({str(ROOT)!r}), {output!r}, "
            f"allow_legacy_conditional=True, junit_path=Path({str(junit)!r}))"
        )
        result = subprocess.run(
            ["python3", "-c", script],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr


def test_unallowlisted_or_governed_skip_fails_closed() -> None:
    script = (
        "from pathlib import Path; import sys; "
        f"sys.path.insert(0, {str(E2E)!r}); "
        "from upstream_checks import _validate_skips; "
        "_validate_skips(Path.cwd(), "
        "'SKIPPED [1] services/aura-core/tests/test_aura0145_governed_research.py:1: unknown', "
        "allow_legacy_conditional=True)"
    )
    result = subprocess.run(
        ["python3", "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "unallowlisted" in result.stderr or "governed" in result.stderr


def test_dedicated_live_skip_fails_closed_even_when_legacy_entry_exists() -> None:
    script = (
        "from pathlib import Path; import sys; "
        f"sys.path.insert(0, {str(E2E)!r}); "
        "from upstream_checks import _load_legacy_skip_allowlist, _validate_skips; "
        "source, entry = next(iter(_load_legacy_skip_allowlist(Path.cwd()).items())); "
        "output = f'SKIPPED [{len(entry[\"nodeIds\"])}] {source[0]}: {source[1]}'; "
        "_validate_skips(Path.cwd(), output, allow_legacy_conditional=False)"
    )
    result = subprocess.run(
        ["python3", "-c", script],
        cwd=ROOT,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "dedicated live check produced a skip" in result.stderr


def test_skip_allowlist_has_green_coverage_for_every_allowlisted_node() -> None:
    document = cast(
        dict[str, Any],
        json.loads(
            (E2E / "legacy-conditional-skips-v1.json").read_text(encoding="utf-8")
        ),
    )
    entries = cast(list[dict[str, Any]], document["entries"])
    assert sum(len(cast(list[str], entry["nodeIds"])) for entry in entries) == 127
    dedicated_governed_markers = (
        "test_generated_artifact",
        "test_task_projection",
        "test_document_sql",
        "test_notifications",
        "test_notification_stream",
    )
    workflow_marker = "test_workflow_run_recovery"
    corrective_markers = ("test_aura0149_research_delivery_migration",)
    for entry in entries:
        coverage = cast(dict[str, Any], entry["coverage"])
        assert coverage["status"] in {"passed", "integrated"}
        assert coverage["kind"] in {"dedicated-command", "immutable-integration"}
        if any(
            marker in node_id
            for node_id in cast(list[str], entry["nodeIds"])
            for marker in dedicated_governed_markers
        ):
            assert coverage["kind"] == "dedicated-command"
            assert coverage["status"] == "passed"
        if any(
            workflow_marker in node_id
            for node_id in cast(list[str], entry["nodeIds"])
        ):
            assert coverage["kind"] == "immutable-integration"
            assert coverage["status"] == "integrated"
            assert "work-items/integration/AURA-0150.yaml" in coverage["reports"]
        if any(
            marker in node_id
            for node_id in cast(list[str], entry["nodeIds"])
            for marker in corrective_markers
        ):
            assert coverage["kind"] == "immutable-integration"
            assert coverage["status"] == "integrated"


def test_runner_rejects_unknown_mode_and_never_treats_missing_evidence_as_success() -> None:
    unknown = subprocess.run(
        ["bash", str(RUNNER), "--skip"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert unknown.returncode == 64
    source = RUNNER.read_text(encoding="utf-8")
    assert "mandatory evidence is missing" in source
    assert "skipped evidence cannot be promoted to success" in source


def test_fixture_report_is_content_free_and_has_both_origins_and_all_modes() -> None:
    fixture = E2E / "fixture.py"
    with tempfile.TemporaryDirectory(prefix="aura0148-fixture-") as directory:
        root = Path(directory)
        report_path = root / "report.json"
        observations_path = root / "observations.json"
        desktop_research_admission_id = _observed_test(
            "desktop",
            "governed-research-acceptance.spec.ts",
            "Aura governed research synthetic acceptance › admits every bounded mode "
            "with server-owned budgets from /research",
        )
        desktop_research_journey_id = _observed_test(
            "desktop",
            "governed-research-acceptance.spec.ts",
            "Aura governed research synthetic acceptance › executes all four dedicated "
            "research journeys through cited completion and delivery",
        )
        desktop_chat_id = _observed_test(
            "desktop",
            "governed-research-acceptance.spec.ts",
            "Aura governed research synthetic acceptance › executes all four chat-origin "
            "journeys through cited completion and delivery",
        )
        desktop_accessibility_id = _observed_test(
            "desktop",
            "governed-research-accessibility.spec.ts",
            "Aura governed research accessibility › new routes have no axe violations with "
            "reduced motion enabled",
        )
        observations_path.write_text(
            json.dumps(
                {
                    name: True
                    for name in (
                        "postgres",
                        "nats",
                        "valkey",
                        "garage",
                        "static_fetcher",
                        "browser_fetcher",
                        "frontend",
                    )
                }
            ),
            encoding="utf-8",
        )
        acceptance_path = root / "acceptance.json"
        acceptance_path.write_text(
            json.dumps(
                {
                    "capturePolicy": "metadata_only",
                    "python": {
                        "tests": [
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_aura0148_governed_research",
                                    "test_research_baseline_and_hostile_regressions_are_executable",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_aura0148_governed_research",
                                    "test_artifact_state_machine_observes_revision_preview_delete_and_unreadability",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_aura0148_governed_research",
                                    "test_task_center_state_machine_covers_every_authoritative_source_and_recovery",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-observatory.tests.test_aura0148_evaluation_privacy",
                                    "test_telemetry_fixture_declares_only_bounded_metadata_and_all_sinks",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_aura0148_governed_research",
                                    "test_core_public_boundary_executes_eight_research_journeys_and_recovery",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_aura0148_governed_research",
                                    "test_core_public_boundary_fences_extension_and_cancellation",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_research_budget_evaluation",
                                    "test_wall_clock_deadline_stops_execution_before_provider_dispatch",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_research_budget_evaluation",
                                    "test_wall_clock_expiry_has_exact_terminal_failure_labels",
                                ),
                                "outcome": "passed",
                            },
                            {
                                "id": _observed_test(
                                    "python",
                                    "services.aura-core.tests.test_aura0148_governed_research",
                                    "test_resilience_fixture_drives_production_projection_state_machine",
                                ),
                                "outcome": "passed",
                            },
                        ]
                    },
                    "playwright": {
                        "tests": [
                            {
                                "id": desktop_research_admission_id,
                                "outcome": "passed",
                            },
                            {
                                "id": desktop_research_journey_id,
                                "outcome": "passed",
                            },
                            {
                                "id": desktop_chat_id,
                                "outcome": "passed",
                            },
                            {
                                "id": desktop_accessibility_id,
                                "outcome": "passed",
                            },
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                "python3",
                str(fixture),
                "--observations",
                str(observations_path),
                "--acceptance",
                str(acceptance_path),
                "--report",
                str(report_path),
            ],
            cwd=ROOT,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        names = {str(item["name"]) for item in report["checks"]}
        for origin in ("conversation", "research-route"):
            for mode in ("quick", "auto", "deep", "agent_led"):
                assert f"{origin}.{mode}.durable-admission" in names
        assert report["capturePolicy"] == "metadata-only"
        mappings = report["evidenceMappings"]
        assert len(mappings) == 36
        assert all(
            mapping["capturePolicy"] == "metadata_only"
            and isinstance(mapping["observations"], list)
            for mapping in mappings.values()
        )
        assert any(
            "test_core_public_boundary_executes_eight_research_journeys_and_recovery"
            in observation
            for mapping in mappings.values()
            for observation in mapping["observations"]
        )
        assert any(
            "test_core_public_boundary_fences_extension_and_cancellation" in observation
            for mapping in mappings.values()
            for observation in mapping["observations"]
        )
        timeout_observations = mappings["research.timeout-deadline-convergence"]["observations"]
        assert any(
            "test_wall_clock_deadline_stops_execution_before_provider_dispatch" in observation
            for observation in timeout_observations
        )
        assert any(
            "test_wall_clock_expiry_has_exact_terminal_failure_labels" in observation
            for observation in timeout_observations
        )
        serialized = json.dumps(report)
        assert "question" not in serialized
        assert "artifactBytes" not in serialized
    assert "https://" not in serialized


def test_research_route_checks_require_the_complete_dedicated_journey_evidence() -> None:
    """Admission evidence cannot stand in for the settled /research journey."""

    import sys

    sys.path.insert(0, str(E2E))
    fixture = importlib.import_module("fixture")
    route_names = [
        name for name in fixture.CHECK_EVIDENCE if name.startswith("research-route.")
    ]
    assert len(route_names) == 12
    journey_id = fixture.PLAYWRIGHT_RESEARCH_JOURNEY_TEST_ID
    admission_id = fixture.PLAYWRIGHT_RESEARCH_ADMISSION_TEST_ID
    assert all(
        journey_id in fixture.CHECK_EVIDENCE[name]
        and admission_id not in fixture.CHECK_EVIDENCE[name]
        for name in route_names
    )

    observations = {name: True for name in fixture.DEPENDENCIES}
    acceptance_ids = {
        observation
        for evidence in fixture.CHECK_EVIDENCE.values()
        for observation in evidence
        if observation.startswith(("python::", "desktop::", "tablet::", "mobile::"))
    }
    complete_acceptance = {observation: True for observation in acceptance_ids}
    complete_acceptance[admission_id] = True

    missing_journey = dict(complete_acceptance)
    del missing_journey[journey_id]
    try:
        fixture.run(observations, missing_journey)
    except ValueError as error:
        assert "research-route.quick.durable-admission" in str(error)
    else:
        raise AssertionError("admission-only evidence satisfied a research route")

    failed_journey = dict(complete_acceptance)
    failed_journey[journey_id] = False
    failed_report = fixture.run(observations, failed_journey)
    try:
        fixture.validate(failed_report)
    except AssertionError:
        pass
    else:
        raise AssertionError("failed dedicated research journey was accepted")

    with tempfile.TemporaryDirectory(prefix="aura0148-duplicate-journey-") as directory:
        acceptance_path = Path(directory) / "acceptance.json"
        acceptance_path.write_text(
            json.dumps(
                {
                    "capturePolicy": "metadata_only",
                    "python": {
                        "tests": [
                            {"id": "python::synthetic::placeholder", "outcome": "passed"}
                        ]
                    },
                    "playwright": {
                        "tests": [
                            {"id": journey_id, "outcome": "passed"},
                            {"id": journey_id, "outcome": "passed"},
                        ]
                    },
                }
            ),
            encoding="utf-8",
        )
        try:
            fixture._load_acceptance(acceptance_path)
        except ValueError as error:
            assert "duplicate observed acceptance test" in str(error)
        else:
            raise AssertionError("duplicate dedicated journey evidence was accepted")


def test_overlapping_synthetic_runners_have_independent_compose_ownership() -> None:
    """Concurrent runs must not share or clean up each other's topology."""

    projects: list[str] = []
    processes: list[subprocess.Popen[bytes]] = []
    logs: list[IO[bytes]] = []
    hold_directory = tempfile.TemporaryDirectory(prefix="aura0148-overlap-")
    hold_root = Path(hold_directory.name)
    hold_ready = hold_root / "ready"
    hold_release = hold_root / "release"

    def resource_ids(project: str) -> dict[str, set[str]]:
        compose = [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            str(E2E / "compose.yaml"),
        ]
        containers = subprocess.run(
            [*compose, "ps", "-q"],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.split()
        networks = subprocess.run(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--quiet",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.split()
        volumes = subprocess.run(
            [
                "docker",
                "volume",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--quiet",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.split()
        return {
            "containers": set(containers),
            "networks": set(networks),
            "volumes": set(volumes),
        }

    try:
        for index in range(2):
            log = tempfile.TemporaryFile()
            logs.append(log)
            environment = {
                **os.environ,
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            environment.pop("AURA_GOVERNED_RESEARCH_PROJECT", None)
            if index == 0:
                environment["AURA_GOVERNED_RESEARCH_HOLD_READY_FILE"] = str(hold_ready)
                environment["AURA_GOVERNED_RESEARCH_HOLD_RELEASE_FILE"] = str(hold_release)
            processes.append(
                subprocess.Popen(
                    ["bash", str(RUNNER), "--synthetic-only"],
                    cwd=ROOT,
                    env=environment,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
            )

        deadline = time.monotonic() + 90
        both_observed = False
        while time.monotonic() < deadline:
            discovered: list[str] = []
            for log in logs:
                log.flush()
                log.seek(0)
                output = log.read().decode("utf-8", errors="replace")
                log.seek(0, os.SEEK_END)
                match = re.search(r"governed-research: project=([a-z0-9][a-z0-9_-]+)", output)
                if match:
                    discovered.append(match.group(1))
            if len(discovered) == 2 and len(set(discovered)) == 2:
                projects = discovered
            if (
                len(projects) == 2
                and hold_ready.is_file()
                and all(process.poll() is None for process in processes)
            ):
                ownership = [resource_ids(project) for project in projects]
                disjoint = all(
                    ownership[0][resource].isdisjoint(ownership[1][resource])
                    for resource in ("containers", "networks", "volumes")
                )
                live_projects = all(
                    ownership_item["containers"] and ownership_item["networks"]
                    for ownership_item in ownership
                )
                if live_projects and disjoint:
                    both_observed = True
                    break
            time.sleep(1)
        assert both_observed, "both runner-owned Compose projects were not observed"

        # Runner 1 is intentionally held after Compose health. Let runner 2
        # complete and clean up, then prove runner 1 remains healthy before
        # releasing its bounded synchronization point.
        peer_result = processes[1].wait(timeout=900)
        logs[1].flush()
        logs[1].seek(0)
        peer_output = logs[1].read().decode("utf-8", errors="replace")
        assert peer_result == 0, peer_output
        assert processes[0].poll() is None, "held runner exited before release"
        held_resources = resource_ids(projects[0])
        assert held_resources["containers"] and held_resources["networks"]
        postgres = subprocess.run(
            [
                "docker",
                "compose",
                "-p",
                projects[0],
                "-f",
                str(E2E / "compose.yaml"),
                "ps",
                "-q",
                "postgres",
            ],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert postgres
        health = subprocess.run(
            ["docker", "inspect", "--format", "{{.State.Health.Status}}", postgres],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert health.returncode == 0
        assert health.stdout.strip() == "healthy"

        hold_release.touch()
        held_result = processes[0].wait(timeout=900)
        logs[0].flush()
        logs[0].seek(0)
        held_output = logs[0].read().decode("utf-8", errors="replace")
        assert held_result == 0, held_output
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=30)
        for project in projects:
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "-p",
                    project,
                    "-f",
                    str(E2E / "compose.yaml"),
                    "down",
                    "--volumes",
                    "--remove-orphans",
                ],
                cwd=ROOT,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        for log in logs:
            log.close()
        hold_directory.cleanup()
