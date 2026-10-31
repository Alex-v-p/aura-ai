"""Deterministic full-stack governed-research acceptance fixture.

The fixture models the public boundaries exercised by AURA-0142 through
AURA-0147.  It deliberately stores no question, URL, excerpt, title, artifact
bytes, or owner content.  The output is a metadata-only release gate.
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

MODES = ("quick", "auto", "deep", "agent_led")
ORIGINS = ("conversation", "research-route")
DEPENDENCIES = {
    "postgres": "isolated",
    "nats": "isolated",
    "valkey": "isolated",
    "garage": "isolated",
    "static_fetcher": "synthetic-disabled",
    "browser_fetcher": "synthetic-disabled",
    "frontend": "http",
}


@dataclass(frozen=True)
class Budget:
    wall_seconds: int
    sources: int


BUDGETS = {
    "quick": Budget(90, 6),
    "auto": Budget(600, 15),
    "deep": Budget(1800, 30),
    "agent_led": Budget(3600, 60),
}


def _check(name: str, condition: bool, reason: str) -> dict[str, str]:
    return {
        "name": name,
        "status": "passed" if condition else "failed",
        "reason": reason,
    }


PYTHON_RESEARCH_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_research_baseline_and_hostile_regressions_are_executable"
)
PYTHON_ARTIFACT_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_artifact_state_machine_observes_revision_preview_delete_and_unreadability"
)
PYTHON_TASK_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_task_center_state_machine_covers_every_authoritative_source_and_recovery"
)
PYTHON_PUBLIC_JOURNEY_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_core_public_boundary_executes_eight_research_journeys_and_recovery"
)
PYTHON_PUBLIC_CANCEL_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_core_public_boundary_fences_extension_and_cancellation"
)
PYTHON_RESILIENCE_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_resilience_fixture_drives_production_projection_state_machine"
)
PYTHON_TIMEOUT_BEFORE_DISPATCH_TEST_ID = (
    "python::services.aura-core.tests.test_research_budget_evaluation::"
    "test_wall_clock_deadline_stops_execution_before_provider_dispatch"
)
PYTHON_TIMEOUT_LABEL_TEST_ID = (
    "python::services.aura-core.tests.test_research_budget_evaluation::"
    "test_wall_clock_expiry_has_exact_terminal_failure_labels"
)
PYTHON_TELEMETRY_TEST_ID = (
    "python::services.aura-observatory.tests.test_aura0148_evaluation_privacy::"
    "test_telemetry_fixture_declares_only_bounded_metadata_and_all_sinks"
)
PLAYWRIGHT_RESEARCH_ADMISSION_TEST_ID = (
    "desktop::governed-research-acceptance.spec.ts::"
    "Aura governed research synthetic acceptance › admits every bounded mode with "
    "server-owned budgets from /research"
)
PLAYWRIGHT_RESEARCH_JOURNEY_TEST_ID = (
    "desktop::governed-research-acceptance.spec.ts::"
    "Aura governed research synthetic acceptance › executes all four dedicated "
    "research journeys through cited completion and delivery"
)
PLAYWRIGHT_CHAT_TEST_ID = (
    "desktop::governed-research-acceptance.spec.ts::"
    "Aura governed research synthetic acceptance › executes all four chat-origin "
    "journeys through cited completion and delivery"
)
PLAYWRIGHT_ACCESSIBILITY_TEST_ID = (
    "desktop::governed-research-accessibility.spec.ts::"
    "Aura governed research accessibility › new routes have no axe violations with "
    "reduced motion enabled"
)


def _evidence_map() -> dict[str, tuple[str, ...]]:
    """Map every fixture assertion to concrete, executed metadata evidence."""

    result: dict[str, tuple[str, ...]] = {}
    for origin in ORIGINS:
        test_id = (
            PLAYWRIGHT_CHAT_TEST_ID
            if origin == "conversation"
            else PLAYWRIGHT_RESEARCH_JOURNEY_TEST_ID
        )
        for mode in MODES:
            prefix = f"{origin}.{mode}"
            result[f"{prefix}.durable-admission"] = (
                test_id,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:frontend",
            )
            result[f"{prefix}.terminal-settlement"] = (
                test_id,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:frontend",
            )
            result[f"{prefix}.citation-and-artifact-lineage"] = (
                test_id,
                PYTHON_ARTIFACT_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
            )
    result.update(
        {
            "task-center.projection-rebuild": (
                PYTHON_TASK_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:postgres",
                "service:nats",
            ),
            "task-center.duplicate-and-out-of-order-events": (
                PYTHON_TASK_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:nats",
            ),
            "task-center.owner-scope": (
                PYTHON_TASK_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                PYTHON_ARTIFACT_TEST_ID,
            ),
            "artifact.revision-integrity": (
                PYTHON_ARTIFACT_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:postgres",
                "service:garage",
            ),
            "artifact.delete-immediate-unreadability": (
                PYTHON_ARTIFACT_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:garage",
            ),
            "research.one-time-extension-cap": (
                PYTHON_RESEARCH_TEST_ID,
                PYTHON_PUBLIC_CANCEL_TEST_ID,
                "service:postgres",
            ),
            "research.cancel-and-retry-truth": (
                PYTHON_RESEARCH_TEST_ID,
                PYTHON_PUBLIC_CANCEL_TEST_ID,
                "service:nats",
            ),
            "research.hostile-evidence-preserves-policy": (
                PYTHON_RESEARCH_TEST_ID,
                PYTHON_PUBLIC_JOURNEY_TEST_ID,
                "service:static_fetcher",
                "service:browser_fetcher",
            ),
            "research.timeout-deadline-convergence": (
                PYTHON_TIMEOUT_BEFORE_DISPATCH_TEST_ID,
                PYTHON_TIMEOUT_LABEL_TEST_ID,
                PYTHON_RESILIENCE_TEST_ID,
            ),
            "telemetry.metadata-only": (PYTHON_TELEMETRY_TEST_ID,),
            "http-deployment.no-tls-assumption": (
                PLAYWRIGHT_ACCESSIBILITY_TEST_ID,
                "service:frontend",
            ),
            "provider-profiles.remain-disabled": (
                "service:static_fetcher",
                "service:browser_fetcher",
            ),
        }
    )
    return result


CHECK_EVIDENCE = _evidence_map()
_ALLOWED_OBSERVATION_PREFIXES = ("python::", "desktop::", "tablet::", "mobile::", "service:")


def _metadata_mapping(evidence_ids: tuple[str, ...]) -> dict[str, object]:
    """Return an explicit, content-free observation binding for one check."""

    if not evidence_ids or any(
        not evidence_id.startswith(_ALLOWED_OBSERVATION_PREFIXES)
        for evidence_id in evidence_ids
    ):
        raise ValueError("synthetic evidence mapping is not a metadata observation")
    return {"capturePolicy": "metadata_only", "observations": list(evidence_ids)}


def _load_acceptance(path: Path) -> dict[str, bool]:
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("observed acceptance evidence is unavailable") from error
    if not isinstance(document, dict) or document.get("capturePolicy") != "metadata_only":
        raise ValueError("observed acceptance evidence is malformed")
    outcomes: dict[str, bool] = {}
    for surface in ("python", "playwright"):
        section = document.get(surface)
        tests = section.get("tests") if isinstance(section, dict) else None
        if not isinstance(tests, list) or not tests:
            raise ValueError("observed acceptance test evidence is missing")
        for item in tests:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                raise ValueError("observed acceptance test identity is malformed")
            test_id = item["id"]
            if test_id in outcomes:
                raise ValueError(f"duplicate observed acceptance test: {test_id}")
            if item.get("outcome") != "passed":
                outcomes[test_id] = False
            else:
                outcomes[test_id] = True
    return outcomes


def run(observations: Mapping[str, bool], acceptance: Mapping[str, bool]) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    available = {**{f"service:{name}": value for name, value in observations.items()}, **acceptance}
    for name, evidence_ids in CHECK_EVIDENCE.items():
        if any(evidence_id not in available for evidence_id in evidence_ids):
            raise ValueError(f"synthetic evidence mapping is missing: {name}")
        condition = all(available[evidence_id] for evidence_id in evidence_ids)
        checks.append(_check(name, condition, "executed_metadata_evidence"))
    return {
        "schemaVersion": 1,
        "runner": "aura-governed-research-synthetic@1",
        "capturePolicy": "metadata-only",
        "fixtureRevision": "governed-research-synthetic-v1",
        "dependencies": DEPENDENCIES,
        "observations": {
            name: "passed" if observations.get(name, False) else "failed"
            for name in DEPENDENCIES
        },
        "checks": checks,
        "evidenceMappings": {
            item["name"]: _metadata_mapping(CHECK_EVIDENCE[item["name"]]) for item in checks
        },
    }


def validate(report: dict[str, Any]) -> None:
    if report.get("capturePolicy") != "metadata-only":
        raise AssertionError("fixture must remain metadata-only")
    if report.get("fixtureRevision") != "governed-research-synthetic-v1":
        raise AssertionError("unexpected fixture revision")
    dependencies = report.get("dependencies")
    if dependencies != DEPENDENCIES:
        raise AssertionError("synthetic dependency boundary changed")
    checks = report.get("checks")
    if not isinstance(checks, list) or not checks:
        raise AssertionError("mandatory fixture evidence is missing")
    raw_checks = cast(list[Any], checks)
    if any(not isinstance(item, dict) for item in raw_checks):
        raise AssertionError("fixture evidence item is not an object")
    typed_checks = cast(list[dict[str, Any]], raw_checks)
    if any(item.get("status") != "passed" for item in typed_checks):
        raise AssertionError("mandatory fixture evidence did not pass")
    if len(typed_checks) != len({item.get("name") for item in typed_checks}):
        raise AssertionError("duplicate fixture evidence was accepted")
    expected_names = set(CHECK_EVIDENCE)
    observed_names = {item.get("name") for item in typed_checks}
    if observed_names != expected_names:
        raise AssertionError("fixture evidence inventory is incomplete")
    mappings = report.get("evidenceMappings")
    if not isinstance(mappings, dict) or set(mappings) != expected_names:
        raise AssertionError("fixture evidence mappings are incomplete")
    for name in expected_names:
        mapping = mappings.get(name)
        observations = mapping.get("observations") if isinstance(mapping, dict) else None
        if (
            not isinstance(mapping, dict)
            or set(mapping) != {"capturePolicy", "observations"}
            or mapping.get("capturePolicy") != "metadata_only"
            or not isinstance(observations, list)
            or observations != list(CHECK_EVIDENCE[name])
            or any(
                not isinstance(observation, str)
                or not observation.startswith(_ALLOWED_OBSERVATION_PREFIXES)
                for observation in observations or []
            )
        ):
            raise AssertionError(f"fixture evidence mapping changed: {name}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--observations", type=Path, required=True)
    parser.add_argument("--acceptance", type=Path, required=True)
    args = parser.parse_args()
    observations = json.loads(args.observations.read_text(encoding="utf-8"))
    raw_observations = cast(dict[str, Any], observations)
    if not isinstance(observations, dict) or any(
        not isinstance(value, bool) for value in raw_observations.values()
    ):
        raise SystemExit("dependency observations are malformed")
    report = run(cast(Mapping[str, bool], raw_observations), _load_acceptance(args.acceptance))
    validate(report)
    args.report.write_text(json.dumps(report, separators=(",", ":")) + "\n", encoding="utf-8")
    print(f"synthetic fixture evidence passed ({len(report['checks'])} checks)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
