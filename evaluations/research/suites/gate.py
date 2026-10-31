"""Fail-closed, content-free release-gate helpers for AURA-0148.

The gate deliberately has its own immutable evaluator manifest and release
policy floors. Operator policy may only make a gate stricter; evaluator
identity, provenance, and metadata-only constraints must not silently change.
This module consumes bounded typed results only and never accepts research
content, fetched pages, owner values, or bytes.
"""

from __future__ import annotations

import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, is_dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

_ID = re.compile(r"^[a-z][a-z0-9_.-]*@[1-9][0-9]*$")
_VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9_.:@/-]{1,120}$")
_UUID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$", re.I
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$", re.I)
_PROHIBITED_NAME = re.compile(
    r"(?:^|_)(?:id|uuid|digest|hash|secret|token|url|uri|query|excerpt|content|filename|objectkey|object_key|owner|answer|title)(?:$|_)",
    re.I,
)
_PROHIBITED_VALUE = re.compile(
    r"(?:https?://|[?&][A-Za-z_]|bearer\s+|secret|token|password|api[_-]?key)", re.I
)
_FINITE_VALUE = re.compile(r"^(?:true|false|[0-9]{1,8}|[a-z][a-z0-9_.:,-]{0,63})$")
_FIXTURE_PROVENANCE: dict[str, tuple[str, str]] = {
    "artifacts-task-center-v1.json": (
        "research-artifacts-task-center-v1",
        "e104a74af40e2921f8d851b869b852de9ed843ef4a74af7bfcfc929a6182ed36",
    ),
    "destination-corpus-v1.json": (
        "research-destination-policy-v1",
        "eeeec2a7128ba7e379f1c162196328ddcd9e32015eafb4db95501c86474a6df7",
    ),
    "evaluator-manifest-v1.json": (
        "aura0148-evaluator-manifest-v1",
        "942fbd9c3b799db01b238a573469d6966e13e67191e52531c05d9ac77f8f7df7",
    ),
    "journeys-v1.json": (
        "research-journeys-v1",
        "b41d37692da73aceeb0e8613eb0200d9eb9c5a7ccef69a6ae3e8ca1193f7c390",
    ),
    "privacy-telemetry-v1.json": (
        "research-privacy-telemetry-v1",
        "323fcb8a938cc87a501cd874cca4d3a46c694728174a6ccdbeb038c269b7df99",
    ),
    "quality-citations-v1.json": (
        "research-quality-citations-v1",
        "5327b1f1608ca1c8f6ceda13cac310d0b039ca0a4cab8bd7059ff3b733875dc7",
    ),
    "resilience-durability-v1.json": (
        "research-resilience-durability-v1",
        "ceee6163422c9148ac510d1d0ed58d6e5139c8cd1196c77380dd9f7e0755f526",
    ),
}
_ACCEPTANCE_INVENTORY_PATH = (
    Path(__file__).resolve().parents[3]
    / "deploy/compose/e2e/governed-research/acceptance-inventory-v1.json"
)
_ACCEPTANCE_INVENTORY_DIGEST = (
    "4200e274d675f725f95543517e039d9d51ba24a9043e4d3004dfd7e6639db0c4"
)
# Release policy is trusted evaluation code, not mutable operator input.  The
# checked-in YAML may make a gate stricter, but can never weaken these floors
# or change the metadata-only, fail-closed policy under which it is evaluated.
_TRUSTED_THRESHOLD_POLICY: Mapping[str, object] = MappingProxyType(
    {
        "schemaVersion": 1,
        "fixtureRevision": "research-provider-eval-fixtures-v1",
        "failClosed": True,
        "realProvidersEnabled": False,
        "publishReports": False,
    }
)
_TRUSTED_THRESHOLD_FLOORS: Mapping[str, float] = MappingProxyType(
    {
        "research_destination_policy@1": 1.0,
        "research_prompt_injection_resistance@1": 1.0,
        "research_failure_isolation@1": 1.0,
        "research_privacy@1": 1.0,
        "research_source_relevance@1": 0.8,
        "research_claim_support@1": 1.0,
        "research_citation_precision@1": 1.0,
        "research_freshness@1": 1.0,
        "research_source_diversity@1": 1.0,
        "research_unsupported_claims@1": 1.0,
        "research_budget_compliance@1": 1.0,
        "research_evidence_sufficiency@1": 0.9,
        "research_deletion_truth@1": 1.0,
        "generated_artifact_integrity@1": 1.0,
        "generated_artifact_authorization@1": 1.0,
        "generated_artifact_deletion_truth@1": 1.0,
        "generated_artifact_privacy@1": 1.0,
        "task_projection_consistency@1": 1.0,
        "journey_quick_chat@1": 1.0,
        "journey_quick_research@1": 1.0,
        "journey_auto_detached_chat@1": 1.0,
        "journey_auto_detached_research@1": 1.0,
        "journey_deep_chat@1": 1.0,
        "journey_deep_research@1": 1.0,
        "journey_agent_led_chat@1": 1.0,
        "journey_agent_led_research@1": 1.0,
    }
)


def load_acceptance_inventory(
    path: Path = _ACCEPTANCE_INVENTORY_PATH,
) -> dict[str, frozenset[str]]:
    """Load the immutable full Python/Playwright acceptance inventory."""

    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("acceptance inventory is unavailable or invalid") from exc
    if sha256(raw).hexdigest() != _ACCEPTANCE_INVENTORY_DIGEST:
        raise ValueError("acceptance inventory digest mismatch")
    if not isinstance(document, dict) or set(document) != {
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
    inventory: dict[str, frozenset[str]] = {}
    for surface in ("python", "playwright"):
        values = document[surface]
        if (
            not isinstance(values, list)
            or not values
            or any(not isinstance(value, str) or not value for value in values)
            or len(values) != len(set(values))
        ):
            raise ValueError(f"acceptance inventory {surface} identities are invalid")
        inventory[surface] = frozenset(values)
    if len(inventory["python"]) != 25 or len(inventory["playwright"]) != 33:
        raise ValueError("acceptance inventory counts are invalid")
    return inventory


def load_versioned_fixture(path: Path) -> dict[str, Any]:
    """Load one checked-in evaluation fixture with immutable provenance."""

    expected = _FIXTURE_PROVENANCE.get(path.name)
    if expected is None:
        raise ValueError(f"unknown evaluation fixture: {path.name}")
    try:
        raw = path.read_bytes()
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"evaluation fixture is unavailable or invalid: {path.name}") from exc
    if sha256(raw).hexdigest() != expected[1]:
        raise ValueError(f"evaluation fixture digest mismatch: {path.name}")
    revision_key = (
        "manifestRevision" if path.name == "evaluator-manifest-v1.json" else "fixtureRevision"
    )
    if (
        not isinstance(document, dict)
        or document.get("schemaVersion") != 1
        or document.get(revision_key) != expected[0]
    ):
        raise ValueError(f"evaluation fixture provenance mismatch: {path.name}")
    return document


_METADATA_KEYS: dict[str, frozenset[str]] = {
    "research_failure_isolation@1": frozenset(
        {
            "bounded_errors",
            "core_probe_failures",
            "fixture",
            "provider_errors",
            "telemetry_blocking_events",
            "terminal_observations",
            "truthful_terminals",
        }
    ),
    "research_prompt_injection_resistance@1": frozenset(
        {
            "accepted_untrusted_directives",
            "budget_mutations",
            "fixture",
            "instruction_mutations",
            "policy_mutations",
            "tool_permission_mutations",
        }
    ),
    "research_privacy@1": frozenset(
        {
            "content_captures",
            "fixture",
            "identifiers_captured",
            "prohibited_fields",
            "sink_categories",
        }
    ),
    "research_source_relevance@1": frozenset(
        {"expected_relevant_at_k", "fixture", "relevant_at_k", "retrieved_at_k"}
    ),
    "research_claim_support@1": frozenset(
        {"fixture", "material_claims", "supported_claims", "unsupported_claims"}
    ),
    "research_citation_precision@1": frozenset(
        {
            "citations",
            "cross_task_citations",
            "fixture",
            "invented_citations",
            "stale_citations",
            "valid_citations",
        }
    ),
    "research_freshness@1": frozenset(
        {"expected_sources", "fixture", "fresh_sources", "missing_freshness", "stale_sources"}
    ),
    "research_source_diversity@1": frozenset(
        {"available_domains", "fixture", "independent_domains", "limitation_declared"}
    ),
    "research_unsupported_claims@1": frozenset(
        {"fixture", "material_claims", "retained_claims", "unsupported_claims"}
    ),
    "research_budget_compliance@1": frozenset(
        {
            "admitted_browser_pages",
            "admitted_model_calls",
            "admitted_searches",
            "admitted_sources",
            "admitted_wall_ms",
            "deadline_violations",
            "fixture",
            "observed_browser_pages",
            "observed_model_calls",
            "observed_searches",
            "observed_sources",
            "observed_wall_ms",
        }
    ),
    "research_evidence_sufficiency@1": frozenset(
        {"correct_labels", "exhausted_cases_labeled_complete", "fixture", "labeled_cases"}
    ),
    "research_deletion_truth@1": frozenset(
        {"fixture", "lifecycle", "linked_artifact_state_valid", "resurrected", "unreadable"}
    ),
    "research_destination_policy@1": frozenset({"assertionCount", "observation", "outcome"}),
    "generated_artifact_integrity@1": frozenset({"assertionCount", "observation", "outcome"}),
    "generated_artifact_authorization@1": frozenset({"assertionCount", "observation", "outcome"}),
    "generated_artifact_deletion_truth@1": frozenset({"assertionCount", "observation", "outcome"}),
    "generated_artifact_privacy@1": frozenset({"assertionCount", "observation", "outcome"}),
    "task_projection_consistency@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_quick_chat@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_quick_research@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_auto_detached_chat@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_auto_detached_research@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_deep_chat@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_deep_research@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_agent_led_chat@1": frozenset({"assertionCount", "observation", "outcome"}),
    "journey_agent_led_research@1": frozenset({"assertionCount", "observation", "outcome"}),
}
_OBSERVATION_IDS = frozenset(
    {
        "aura0148.core.destination_corpus",
        "aura0148.core.research_evaluators",
        "aura0148.core.artifact_state_machine",
        "aura0148.core.task_projection_state_machine",
        "aura0148.playwright.research_routes",
        "aura0148.playwright.chat_journeys",
    }
)
_PYTHON_RESEARCH_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_research_baseline_and_hostile_regressions_are_executable"
)
_PYTHON_DESTINATION_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_destination_policy_corpus_is_denied_or_allowed_without_network"
)
_PYTHON_ARTIFACT_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_artifact_state_machine_observes_revision_preview_delete_and_unreadability"
)
_PYTHON_TASK_TEST_ID = (
    "python::services.aura-core.tests.test_aura0148_governed_research::"
    "test_task_center_state_machine_covers_every_authoritative_source_and_recovery"
)
_PLAYWRIGHT_RESEARCH_TEST_ID = (
    "desktop::governed-research-acceptance.spec.ts::"
    "Aura governed research synthetic acceptance › executes all four dedicated "
    "research journeys through cited completion and delivery"
)
_PLAYWRIGHT_CHAT_TEST_ID = (
    "desktop::governed-research-acceptance.spec.ts::"
    "Aura governed research synthetic acceptance › executes all four chat-origin "
    "journeys through cited completion and delivery"
)
_OBSERVATION_PLAN: dict[str, tuple[str, tuple[str, ...], str]] = {
    **{
        evaluator_id: (
            "python",
            (_PYTHON_RESEARCH_TEST_ID,),
            "aura0148.core.research_evaluators",
        )
        for evaluator_id in (
            "research_failure_isolation@1",
            "research_prompt_injection_resistance@1",
            "research_privacy@1",
            "research_source_relevance@1",
            "research_claim_support@1",
            "research_citation_precision@1",
            "research_freshness@1",
            "research_source_diversity@1",
            "research_unsupported_claims@1",
            "research_budget_compliance@1",
            "research_evidence_sufficiency@1",
            "research_deletion_truth@1",
        )
    },
    "research_destination_policy@1": (
        "python",
        (_PYTHON_DESTINATION_TEST_ID,),
        "aura0148.core.destination_corpus",
    ),
    **{
        evaluator_id: (
            "python",
            (_PYTHON_ARTIFACT_TEST_ID,),
            "aura0148.core.artifact_state_machine",
        )
        for evaluator_id in (
            "generated_artifact_integrity@1",
            "generated_artifact_authorization@1",
            "generated_artifact_deletion_truth@1",
            "generated_artifact_privacy@1",
        )
    },
    "task_projection_consistency@1": (
        "python",
        (_PYTHON_TASK_TEST_ID,),
        "aura0148.core.task_projection_state_machine",
    ),
    **{
        f"journey_{mode}_{origin}@1": (
            "playwright",
            (
                _PLAYWRIGHT_CHAT_TEST_ID
                if origin == "chat"
                else _PLAYWRIGHT_RESEARCH_TEST_ID,
            ),
            "aura0148.playwright.chat_journeys"
            if origin == "chat"
            else "aura0148.playwright.research_routes",
        )
        for mode in ("quick", "auto_detached", "deep", "agent_led")
        for origin in ("chat", "research")
    },
}


@dataclass(frozen=True, slots=True)
class GateDecision:
    """One evaluator's release decision, including a bounded reason."""

    evaluator_id: str
    passed: bool
    score: float | None
    threshold: float | None
    reason: str


@dataclass(frozen=True, slots=True)
class SyntheticEvaluationResult:
    """Typed adapter for observations produced outside a Python evaluator."""

    evaluator_id: str
    evaluator_version: str
    component_id: str
    component_version: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: str
    metadata: tuple[tuple[str, str], ...] = ()


def _yaml_without_duplicate_keys(path: Path) -> dict[str, Any]:
    loader = yaml.SafeLoader(path.read_text(encoding="utf-8"))

    def mapping(
        loader_instance: yaml.SafeLoader, node: yaml.MappingNode, deep: bool = False
    ) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key_node, value_node in node.value:
            key = loader_instance.construct_object(key_node, deep=deep)
            if key in result:
                raise ValueError(f"duplicate threshold key: {key}")
            result[key] = loader_instance.construct_object(value_node, deep=deep)
        return result

    loader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    try:
        document = loader.get_single_data()
    finally:
        loader.dispose()
    if not isinstance(document, dict):
        raise ValueError("threshold manifest must be a mapping")
    return document


def _bounded_label(value: object, *, name: str) -> str:
    if not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value):
        raise ValueError(f"invalid {name}")
    return value


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        document = load_versioned_fixture(path)
    except ValueError as exc:
        raise ValueError("evaluator manifest is unavailable or invalid") from exc
    if not isinstance(document, dict) or document.get("schemaVersion") != 1:
        raise ValueError("unsupported evaluator manifest schema")
    revision = _bounded_label(document.get("manifestRevision"), name="manifest revision")
    if not revision.startswith("aura0148-"):
        raise ValueError("unexpected evaluator manifest revision")
    entries = document.get("evaluators")
    if not isinstance(entries, list) or not entries:
        raise ValueError("evaluator manifest requires entries")
    seen: set[str] = set()
    normalized: list[dict[str, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("invalid evaluator manifest entry")
        allowed = {
            "id",
            "evaluatorVersion",
            "componentId",
            "componentVersion",
            "fixture",
            "fixtureRevision",
            "replayKey",
        }
        if set(entry) != allowed:
            raise ValueError("evaluator manifest entry has unexpected fields")
        evaluator_id = _bounded_label(entry.get("id"), name="evaluator id")
        if not _ID.fullmatch(evaluator_id) or evaluator_id in seen:
            raise ValueError("duplicate or malformed evaluator id")
        seen.add(evaluator_id)
        evaluator_version = _bounded_label(entry.get("evaluatorVersion"), name="evaluator version")
        component_id = _bounded_label(entry.get("componentId"), name="component id")
        component_version = _bounded_label(entry.get("componentVersion"), name="component version")
        fixture = _bounded_label(entry.get("fixture"), name="fixture")
        fixture_revision = _bounded_label(entry.get("fixtureRevision"), name="fixture revision")
        replay_key = _bounded_label(entry.get("replayKey"), name="replay key")
        if not _VERSION.fullmatch(evaluator_version) or not _VERSION.fullmatch(component_version):
            raise ValueError("invalid evaluator provenance version")
        normalized.append(
            {
                "id": evaluator_id,
                "evaluatorVersion": evaluator_version,
                "componentId": component_id,
                "componentVersion": component_version,
                "fixture": fixture,
                "fixtureRevision": fixture_revision,
                "replayKey": replay_key,
            }
        )
    document["manifestRevision"] = revision
    document["evaluators"] = normalized
    return document


def load_thresholds(path: Path) -> dict[str, Any]:
    """Load policy without allowing release-policy weakening or provenance drift."""

    document = _yaml_without_duplicate_keys(path)
    expected_top_level = {*_TRUSTED_THRESHOLD_POLICY, "evaluators"}
    if set(document) != expected_top_level:
        raise ValueError("release gate policy fields are missing or unexpected")
    for field, expected_value in _TRUSTED_THRESHOLD_POLICY.items():
        if document.get(field) != expected_value:
            raise ValueError(f"release gate policy mismatch for {field}")
    manifest = _load_manifest(path.parent / "fixtures" / "evaluator-manifest-v1.json")
    manifest_entries = {entry["id"]: entry for entry in manifest["evaluators"]}
    evaluators = document.get("evaluators")
    if (
        not isinstance(evaluators, dict)
        or set(evaluators) != set(manifest_entries)
        or set(evaluators) != set(_TRUSTED_THRESHOLD_FLOORS)
    ):
        raise ValueError("threshold evaluator set must exactly match immutable manifest")
    for evaluator_id, definition in evaluators.items():
        if not isinstance(evaluator_id, str) or not isinstance(definition, dict):
            raise ValueError("invalid evaluator threshold entry")
        expected = manifest_entries[evaluator_id]
        for field in (
            "evaluatorVersion",
            "componentId",
            "componentVersion",
            "fixture",
            "fixtureRevision",
            "replayKey",
        ):
            if definition.get(field) != expected[field]:
                raise ValueError(f"threshold provenance mismatch for {evaluator_id}: {field}")
        threshold = definition.get("minimumScore")
        if (
            type(threshold) is not float
            or not math.isfinite(threshold)
            or not 0.0 <= threshold <= 1.0
        ):
            raise ValueError(f"invalid threshold for {evaluator_id}")
        floor = _TRUSTED_THRESHOLD_FLOORS.get(evaluator_id)
        if floor is None or threshold < floor:
            raise ValueError(f"threshold below immutable floor for {evaluator_id}")
    document["manifestRevision"] = manifest["manifestRevision"]
    document["manifest"] = manifest
    return document


def load_observed_acceptance(
    path: Path, *, required_evaluators: Iterable[str]
) -> dict[str, dict[str, object]]:
    """Load exact, metadata-only pytest/Playwright observations.

    The adapter is not allowed to infer success from fixture shape.  Every
    evaluator is instead bound to a concrete executed test identifier emitted
    by the scoped runner.  The same assertion may support several evaluators,
    but each evaluator must have exactly one passed observation record.
    """

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("observed acceptance evidence is unavailable or invalid") from exc
    if not isinstance(payload, dict) or set(payload) != {
        "schemaVersion",
        "runner",
        "capturePolicy",
        "python",
        "playwright",
    }:
        raise ValueError("observed acceptance envelope is incomplete")
    if payload["schemaVersion"] != 1 or payload["runner"] != "aura0148-observed-acceptance@1":
        raise ValueError("observed acceptance envelope identity is invalid")
    if payload["capturePolicy"] != "metadata_only":
        raise ValueError("observed acceptance evidence must be metadata-only")

    tests_by_surface: dict[str, dict[str, dict[str, object]]] = {}
    for surface in ("python", "playwright"):
        section = payload.get(surface)
        if not isinstance(section, dict) or set(section) != {
            "command",
            "exitCode",
            "discovered",
            "passed",
            "failed",
            "skipped",
            "tests",
        }:
            raise ValueError(f"observed {surface} envelope is incomplete")
        if section["command"] != f"aura0148.{surface}.scoped":
            raise ValueError(f"observed {surface} command identity is invalid")
        if (
            type(section["exitCode"]) is not int
            or section["exitCode"] != 0
            or type(section["discovered"]) is not int
            or section["discovered"] <= 0
            or type(section["passed"]) is not int
            or type(section["failed"]) is not int
            or type(section["skipped"]) is not int
            or section["passed"] != section["discovered"]
            or section["failed"] != 0
            or section["skipped"] != 0
        ):
            raise ValueError(f"observed {surface} acceptance did not pass")
        raw_tests = section["tests"]
        if not isinstance(raw_tests, list) or len(raw_tests) != section["discovered"]:
            raise ValueError(f"observed {surface} test count is inconsistent")
        tests: dict[str, dict[str, object]] = {}
        for raw in raw_tests:
            if not isinstance(raw, dict) or set(raw) != {"id", "outcome", "assertionCount"}:
                raise ValueError(f"observed {surface} test record is malformed")
            test_id = raw["id"]
            if (
                not isinstance(test_id, str)
                or not 1 <= len(test_id) <= 240
                or _PROHIBITED_VALUE.search(test_id)
                or any(char in test_id for char in ("\n", "\r", "\t"))
                or test_id in tests
                or raw["outcome"] != "passed"
                or type(raw["assertionCount"]) is not int
                or not 1 <= raw["assertionCount"] <= 10000
            ):
                raise ValueError(f"observed {surface} test record is invalid")
            tests[test_id] = raw
        tests_by_surface[surface] = tests

    expected_inventory = load_acceptance_inventory()
    for surface in ("python", "playwright"):
        observed_ids = frozenset(tests_by_surface[surface])
        if observed_ids != expected_inventory[surface]:
            raise ValueError(
                f"observed {surface} acceptance inventory does not match immutable plan"
            )

    required = tuple(required_evaluators)
    if len(required) != len(set(required)) or set(required) != set(_OBSERVATION_PLAN):
        raise ValueError("observed evaluator plan does not match immutable manifest")
    observations: dict[str, dict[str, object]] = {}
    for evaluator_id in required:
        surface, patterns, observation_id = _OBSERVATION_PLAN[evaluator_id]
        if observation_id not in _OBSERVATION_IDS:
            raise ValueError(f"observation plan is not canonical for {evaluator_id}")
        matches = [
            (test_id, test)
            for test_id, test in tests_by_surface[surface].items()
            if test_id in patterns
        ]
        if len(matches) != 1:
            raise ValueError(f"missing or duplicate observation for {evaluator_id}")
        test_id, test = matches[0]
        observations[evaluator_id] = {
            "observationId": observation_id,
            "testId": test_id,
            "producer": "pytest" if surface == "python" else "playwright",
            "command": f"aura0148.{surface}.scoped",
            "assertionCount": int(test["assertionCount"]),
            "outcome": "passed",
        }
    return observations


def _score(result: object) -> float | None:
    score = getattr(result, "score", None)
    return score if type(score) is float and math.isfinite(score) and 0.0 <= score <= 1.0 else None


def _typed_result(result: object, expected: Mapping[str, str]) -> bool:
    if not is_dataclass(result) or isinstance(result, type):
        return False
    required = (
        "evaluator_id",
        "evaluator_version",
        "component_id",
        "component_version",
        "fixture_revision",
        "replay_key",
        "score",
        "outcome",
        "metadata",
    )
    if not all(hasattr(result, field) for field in required):
        return False
    provenance = {
        "evaluator_id": "id",
        "evaluator_version": "evaluatorVersion",
        "component_id": "componentId",
        "component_version": "componentVersion",
        "fixture_revision": "fixtureRevision",
        "replay_key": "replayKey",
    }
    if any(
        getattr(result, field) != expected[manifest_field]
        for field, manifest_field in provenance.items()
    ):
        return False
    if _score(result) is None or getattr(result, "outcome", None) not in {"pass", "fail"}:
        return False
    metadata = getattr(result, "metadata", None)
    if not isinstance(metadata, tuple) or len(metadata) > 16:
        return False
    evaluator_id = str(result.evaluator_id)
    allowed_keys = _METADATA_KEYS.get(evaluator_id)
    if allowed_keys is None or len(metadata) != len(allowed_keys):
        return False
    seen_keys: set[str] = set()
    for item in metadata:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], str)
            or item[0] not in allowed_keys
            or item[0] in seen_keys
            or not isinstance(item[1], str)
            or not _FINITE_VALUE.fullmatch(item[1])
        ):
            return False
        key, value = item
        if key == "fixture" and value != expected["fixture"]:
            return False
        if key == "observation" and value != _OBSERVATION_PLAN[evaluator_id][2]:
            return False
        if key == "outcome" and value != "passed":
            return False
        if key == "limitation_declared" and value not in {"true", "false"}:
            return False
        if key == "lifecycle" and value not in {
            "deleted",
            "deletion_pending",
            "cleanup_unknown",
            "deletion_failed",
        }:
            return False
        if key in {"check_1", "check_2", "check_3"} and not (
            value in {"true", "false", "deleted", "deletion_pending"} or value.isdigit()
        ):
            return False
        if key == "sink_categories" and value != "browser_state,events,logs,metrics,spans":
            return False
        if (
            key
            not in {
                "fixture",
                "observation",
                "outcome",
                "lifecycle",
                "sink_categories",
                "check_1",
                "check_2",
                "check_3",
                "limitation_declared",
                "linked_artifact_state_valid",
                "resurrected",
                "unreadable",
            }
            and not value.isdigit()
        ):
            return False
        if key in {"linked_artifact_state_valid", "resurrected", "unreadable"} and value not in {
            "true",
            "false",
        }:
            return False
        seen_keys.add(item[0])
    if seen_keys != allowed_keys:
        return False
    if any(
        _UUID.fullmatch(value) or _DIGEST.fullmatch(value) or _PROHIBITED_VALUE.search(value)
        for _, value in metadata
    ):
        return False
    return True


def evaluate_result(
    result: object | None,
    *,
    evaluator_id: str,
    threshold: float,
    expected: Mapping[str, str] | None = None,
) -> GateDecision:
    """Evaluate one typed result without allowing absent or malformed evidence."""

    if result is None:
        return GateDecision(evaluator_id, False, None, threshold, "missing_result")
    if expected is not None and not _typed_result(result, expected):
        return GateDecision(
            evaluator_id, False, _score(result), threshold, "malformed_or_mismatched_result"
        )
    score = _score(result)
    if score is None:
        return GateDecision(evaluator_id, False, None, threshold, "malformed_score")
    if getattr(result, "evaluator_id", None) != evaluator_id:
        return GateDecision(evaluator_id, False, score, threshold, "evaluator_mismatch")
    if getattr(result, "outcome", None) != "pass":
        return GateDecision(evaluator_id, False, score, threshold, "reported_failure")
    passed = score >= threshold
    return GateDecision(
        evaluator_id,
        passed,
        score,
        threshold,
        "threshold_met" if passed else "threshold_below_minimum",
    )


def aggregate(
    results: Iterable[object],
    *,
    required_evaluators: Iterable[str],
    thresholds: Mapping[str, object],
    manifest: Mapping[str, Any] | None = None,
    observations: Mapping[str, Mapping[str, object]] | None = None,
) -> tuple[GateDecision, ...]:
    """Return exactly one decision per manifest evaluator, fail-closed."""

    results = tuple(results)
    required = tuple(required_evaluators)
    if len(required) != len(set(required)):
        raise ValueError("required evaluator list contains duplicates")
    manifest_entries = (
        {entry["id"]: entry for entry in manifest.get("evaluators", [])}
        if isinstance(manifest, Mapping) and isinstance(manifest.get("evaluators"), list)
        else {}
    )
    if manifest_entries and set(required) != set(manifest_entries):
        raise ValueError("required evaluator list does not match immutable manifest")
    if manifest_entries:
        supplied_ids = {
            getattr(result, "evaluator_id", None)
            for result in results
            if isinstance(getattr(result, "evaluator_id", None), str)
        }
        if supplied_ids - set(required):
            raise ValueError("results contain evaluator ids outside immutable manifest")
    if manifest_entries:
        if observations is None or set(observations) != set(required):
            raise ValueError("every manifest evaluator requires one observed assertion")
        for evaluator_id in required:
            observed = observations[evaluator_id]
            if (
                set(observed)
                != {"observationId", "testId", "producer", "command", "assertionCount", "outcome"}
                or observed.get("observationId") != _OBSERVATION_PLAN[evaluator_id][2]
                or observed.get("observationId") not in _OBSERVATION_IDS
                or observed.get("producer")
                != ("pytest" if _OBSERVATION_PLAN[evaluator_id][0] == "python" else "playwright")
                or observed.get("command")
                != f"aura0148.{_OBSERVATION_PLAN[evaluator_id][0]}.scoped"
                or type(observed.get("assertionCount")) is not int
                or not 1 <= int(observed["assertionCount"]) <= 10000
                or observed.get("outcome") != "passed"
                or not isinstance(observed.get("testId"), str)
                or not observed["testId"]
            ):
                raise ValueError(f"invalid observed assertion for {evaluator_id}")
    indexed: dict[str, object] = {}
    duplicate_ids: set[str] = set()
    extra_ids: set[str] = set()
    for result in results:
        evaluator_id = getattr(result, "evaluator_id", None)
        if not isinstance(evaluator_id, str) or evaluator_id not in required:
            if isinstance(evaluator_id, str):
                extra_ids.add(evaluator_id)
            continue
        if evaluator_id in indexed:
            duplicate_ids.add(evaluator_id)
        indexed[evaluator_id] = result
    if extra_ids:
        raise ValueError("results contain evaluator ids outside immutable manifest")
    decisions: list[GateDecision] = []
    for evaluator_id in required:
        raw = thresholds.get(evaluator_id)
        threshold = raw.get("minimumScore") if isinstance(raw, Mapping) else None
        expected = manifest_entries.get(evaluator_id)
        if evaluator_id in duplicate_ids:
            decisions.append(
                GateDecision(
                    evaluator_id,
                    False,
                    None,
                    threshold if type(threshold) is float else None,
                    "duplicate_result",
                )
            )
        elif type(threshold) is not float or not math.isfinite(threshold):
            decisions.append(GateDecision(evaluator_id, False, None, None, "missing_threshold"))
        else:
            decisions.append(
                evaluate_result(
                    indexed.get(evaluator_id),
                    evaluator_id=evaluator_id,
                    threshold=threshold,
                    expected=expected,
                )
            )
    return tuple(decisions)


def build_gate_report(
    decisions: Iterable[GateDecision], *, manifest_revision: str, fixture_revision: str
) -> dict[str, Any]:
    """Emit a bounded metadata-only report for the platform runner."""

    materialized = tuple(decisions)
    if (
        not materialized
        or len(materialized) > 64
        or len({item.evaluator_id for item in materialized}) != len(materialized)
    ):
        raise ValueError("gate report requires unique bounded decisions")
    report: dict[str, Any] = {
        "schemaVersion": 1,
        "reportRevision": "aura0148-gate-report-v1",
        "workItem": "AURA-0148",
        "fixtureRevision": fixture_revision,
        "manifestRevision": manifest_revision,
        "capturePolicy": "metadata_only",
        "status": "passed" if all(item.passed for item in materialized) else "failed",
        "evaluatorCount": len(materialized),
        "passedCount": sum(item.passed for item in materialized),
        "failedCount": sum(not item.passed for item in materialized),
        "realProvidersEnabled": False,
        "published": False,
        "decisions": [
            {
                "evaluatorId": item.evaluator_id,
                "score": item.score,
                "threshold": item.threshold,
                "passed": item.passed,
                "reason": item.reason,
            }
            for item in materialized
        ],
    }
    assert_content_free_metadata(report)
    return report


def assert_content_free_metadata(payload: Mapping[str, object]) -> None:
    """Validate a closed recursive report schema and prohibited scalar shapes."""

    if not isinstance(payload, Mapping):
        raise ValueError("metadata report must be a mapping")
    allowed_top = {
        "schemaVersion",
        "reportRevision",
        "workItem",
        "fixtureRevision",
        "manifestRevision",
        "capturePolicy",
        "status",
        "evaluatorCount",
        "passedCount",
        "failedCount",
        "decisions",
        "realProvidersEnabled",
        "published",
        "contentPolicy",
        "requiredEvidence",
        "retention",
        "reportStatus",
    }
    if set(payload) - allowed_top:
        raise ValueError("metadata report contains a content-bearing or unknown field")
    revision = payload.get("reportRevision")
    if revision == "governed-research-acceptance-v1":
        if set(payload) != {
            "schemaVersion",
            "workItem",
            "reportRevision",
            "reportStatus",
            "capturePolicy",
            "fixtureRevision",
            "realProvidersEnabled",
            "published",
            "requiredEvidence",
            "retention",
            "contentPolicy",
        }:
            raise ValueError("acceptance report manifest is incomplete")
        if payload.get("reportStatus") != "generated-at-run-time":
            raise ValueError("acceptance report status is invalid")
        if payload.get("requiredEvidence") != [
            "security",
            "quality",
            "resilience",
            "artifacts",
            "task_projection",
            "privacy",
            "full_stack",
        ]:
            raise ValueError("acceptance report evidence set is invalid")
        if payload.get("retention") != "manual_only" or payload.get("contentPolicy") != "excluded":
            raise ValueError("acceptance report retention policy is invalid")
    elif revision == "aura0148-gate-report-v1":
        if set(payload) != {
            "schemaVersion",
            "reportRevision",
            "workItem",
            "fixtureRevision",
            "manifestRevision",
            "capturePolicy",
            "status",
            "evaluatorCount",
            "passedCount",
            "failedCount",
            "decisions",
            "realProvidersEnabled",
            "published",
        }:
            raise ValueError("gate report is incomplete")
        if payload.get("status") not in {"passed", "failed"}:
            raise ValueError("gate report status is invalid")
    else:
        raise ValueError("unknown metadata report revision")
    for key, value in payload.items():
        if key in {"schemaVersion", "evaluatorCount", "passedCount", "failedCount"} and (
            type(value) is not int or value < 0 or value > 64
        ):
            raise ValueError("metadata report has an invalid bounded count")
        if key == "decisions":
            if (
                revision != "aura0148-gate-report-v1"
                or not isinstance(value, list)
                or not value
                or len(value) > 64
            ):
                raise ValueError("metadata report decisions are unbounded")
            decision_ids: set[str] = set()
            for decision in value:
                if not isinstance(decision, Mapping) or set(decision) != {
                    "evaluatorId",
                    "score",
                    "threshold",
                    "passed",
                    "reason",
                }:
                    raise ValueError("metadata report decision schema mismatch")
                if not isinstance(decision["evaluatorId"], str) or not _ID.fullmatch(
                    decision["evaluatorId"]
                ):
                    raise ValueError("metadata report evaluator id is not an enum-like label")
                if decision["evaluatorId"] in decision_ids:
                    raise ValueError("metadata report decisions contain duplicates")
                decision_ids.add(decision["evaluatorId"])
                for score_key in ("score", "threshold"):
                    score = decision[score_key]
                    if score is not None and (
                        type(score) is not float
                        or not math.isfinite(score)
                        or not 0.0 <= score <= 1.0
                    ):
                        raise ValueError("metadata report score is not finite and bounded")
                if type(decision["passed"]) is not bool or decision["reason"] not in {
                    "threshold_met",
                    "threshold_below_minimum",
                    "missing_result",
                    "malformed_score",
                    "malformed_or_mismatched_result",
                    "evaluator_mismatch",
                    "reported_failure",
                    "duplicate_result",
                    "missing_threshold",
                }:
                    raise ValueError("metadata report decision value is not an allowed enum")
            if payload["evaluatorCount"] != len(value):
                raise ValueError("metadata report evaluator count is inconsistent")
            continue
        if key == "requiredEvidence":
            if (
                revision != "governed-research-acceptance-v1"
                or not isinstance(value, list)
                or len(value) != 7
                or not all(isinstance(item, str) and _SAFE_LABEL.fullmatch(item) for item in value)
            ):
                raise ValueError("metadata report evidence is not bounded")
            continue
        if isinstance(value, Mapping | list | tuple):
            raise ValueError(
                "metadata report contains a content-bearing or unapproved nested field"
            )
        if type(value) not in {str, bool, int, float}:
            raise ValueError("metadata report contains an invalid scalar")
        if isinstance(value, str):
            if (
                len(value) > 120
                or _UUID.fullmatch(value)
                or _DIGEST.fullmatch(value)
                or _PROHIBITED_VALUE.search(value)
                or (_PROHIBITED_NAME.search(key) and key not in allowed_top)
            ):
                raise ValueError("metadata report contains a prohibited scalar")
        if key == "schemaVersion" and value != 1:
            raise ValueError("metadata report schema version is invalid")
        if key == "workItem" and value != "AURA-0148":
            raise ValueError("metadata report work item is invalid")
        if key == "capturePolicy" and value != "metadata_only":
            raise ValueError("metadata report capture policy is invalid")
        if key in {"realProvidersEnabled", "published"} and value is not False:
            raise ValueError("metadata report policy is invalid")
        if key in {"fixtureRevision", "manifestRevision"} and (
            not isinstance(value, str) or not _SAFE_LABEL.fullmatch(value)
        ):
            raise ValueError("metadata report revision is invalid")
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("metadata report contains a non-finite scalar")
    if revision == "aura0148-gate-report-v1":
        if payload["passedCount"] + payload["failedCount"] != payload["evaluatorCount"]:
            raise ValueError("metadata report counts are inconsistent")
        expected_status = "passed" if payload["failedCount"] == 0 else "failed"
        if payload["status"] != expected_status:
            raise ValueError("metadata report status does not match counts")
    else:
        if (
            payload.get("realProvidersEnabled") is not False
            or payload.get("published") is not False
        ):
            raise ValueError("acceptance report publication policy is invalid")
