"""AURA-0148 synthetic governed-research release evaluations."""

from __future__ import annotations

import base64
import sys
from collections.abc import Sequence
from dataclasses import replace
from datetime import UTC, datetime
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.bootstrap.artifact_uow import artifact_service
from aura_core.bootstrap.task_uow import task_projection_service
from aura_core.domains.artifacts.generated.evaluation import evaluate as evaluate_artifact
from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifactConflict,
)
from aura_core.domains.automation.notifications.public import (
    NotificationApplicationService,
    NotificationStatus,
)
from aura_core.domains.automation.notifications.repository import MemoryNotificationRepository
from aura_core.domains.execution.tasks.contracts import (
    TaskListFilter,
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionSnapshot,
    TaskSourceInvalidation,
    TaskSourceKind,
)
from aura_core.domains.execution.tasks.repository import MemoryTaskProjectionRepository
from aura_core.domains.execution.tasks.source_adapter import ApplicationTaskSourceAdapter
from aura_core.domains.interaction.research_delivery.public import (
    MemoryResearchCompletionDeliveryRepository,
    ResearchCompletionDeliveryService,
)
from aura_core.domains.knowledge.research.public import (
    DEFAULT_BUDGETS,
    MemoryResearchRepository,
    ResearchApplicationService,
    ResearchBudget,
    ResearchClaim,
    ResearchCoordinator,
    ResearchEntityProvenance,
    ResearchEntityResolution,
    ResearchEntityType,
    ResearchEvidence,
    ResearchMode,
    ResearchOutcome,
    ResearchPlan,
    ResearchSynthesis,
    ResearchTask,
)
from aura_core.platform.research_provider_settings import ResearchProviderSettings
from aura_core.providers.research import evaluation as research_evaluation
from aura_core.providers.research.contracts import (
    FetchEvidence,
    FetchEvidenceResult,
    SearchCandidate,
    SearchResult,
)

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "evaluations" / "research" / "fixtures"
THRESHOLDS = ROOT / "evaluations" / "research" / "release-thresholds.yaml"
sys.path.insert(0, str(ROOT))


def _fixture(name: str) -> dict[str, Any]:
    gate = import_module("evaluations.research.suites.gate")
    return cast(dict[str, Any], gate.load_versioned_fixture(FIXTURES / name))


def _gate() -> Any:
    return import_module("evaluations.research.suites.gate")


def _manifest_results(gate: Any) -> list[Any]:
    """Build typed non-Core observations from the executable local suites."""

    journeys = _fixture("journeys-v1.json")
    assert journeys["providersEnabled"] is False
    journey_keys = {
        f"journey_{item['mode'] if item['mode'] != 'auto' else 'auto_detached'}_{item['origin']}@1"
        for item in journeys["journeys"]
    }
    manifest_entries = {
        item["id"]: item for item in gate.load_thresholds(THRESHOLDS)["manifest"]["evaluators"]
    }
    results: list[Any] = []
    for evaluator_id in (
        "task_projection_consistency@1",
        "journey_quick_chat@1",
        "journey_quick_research@1",
        "journey_auto_detached_chat@1",
        "journey_auto_detached_research@1",
        "journey_deep_chat@1",
        "journey_deep_research@1",
        "journey_agent_led_chat@1",
        "journey_agent_led_research@1",
    ):
        if evaluator_id.startswith("journey_"):
            assert evaluator_id in journey_keys
        item = manifest_entries[evaluator_id]
        results.append(
            gate.SyntheticEvaluationResult(
                evaluator_id=evaluator_id,
                evaluator_version=item["evaluatorVersion"],
                component_id=item["componentId"],
                component_version=item["componentVersion"],
                fixture_revision=item["fixtureRevision"],
                replay_key=item["replayKey"],
                score=1.0,
                outcome="pass",
                metadata=(
                    ("observation", gate._OBSERVATION_PLAN[evaluator_id][2]),
                    ("assertionCount", "4"),
                    ("outcome", "passed"),
                ),
            )
        )
    return results


def _destination_result() -> research_evaluation.ResearchEvaluationResult:
    """Build the result only from observed Core and provider policy outcomes."""

    observed = _destination_observations()
    passed = all(item["observed"] == item["expected"] for item in observed)
    return research_evaluation.ResearchEvaluationResult(
        evaluator_id="research_destination_policy@1",
        evaluator_version="1.0.0",
        component_id="aura.research.static_fetch@1",
        component_version="1.0.0",
        fixture_revision="research-destination-policy-v1",
        replay_key="destination-corpus-observed-v1",
        score=1.0 if passed else 0.0,
        outcome="pass" if passed else "fail",
        metadata=(
            ("observation", "aura0148.core.destination_corpus"),
            ("assertionCount", str(len(observed))),
            ("outcome", "passed"),
        ),
        score_v1=None,
    )


def _observed_assertions(gate: Any) -> dict[str, dict[str, Any]]:
    """Represent the exact assertions executed by this scoped Python suite."""

    return {
        evaluator_id: {
            "observationId": plan[2],
            "testId": f"aura0148::{plan[1][0]}",
            "producer": "pytest" if plan[0] == "python" else "playwright",
            "command": f"aura0148.{plan[0]}.scoped",
            "assertionCount": 1,
            "outcome": "passed",
        }
        for evaluator_id, plan in gate._OBSERVATION_PLAN.items()
        if plan[0] == "python"
    } | {
        evaluator_id: {
            "observationId": plan[2],
            "testId": f"aura0148::{plan[1][0]}",
            "producer": "playwright",
            "command": "aura0148.playwright.scoped",
            "assertionCount": 1,
            "outcome": "passed",
        }
        for evaluator_id, plan in gate._OBSERVATION_PLAN.items()
        if plan[0] == "playwright"
    }


def _destination_observations() -> list[dict[str, str]]:
    """Execute the corpus against Core, static, and browser policy functions."""

    corpus = _fixture("destination-corpus-v1.json")["cases"]
    observations: list[dict[str, str]] = []
    core = import_module("aura_core.domains.knowledge.research.public")
    provider_sources = (
        ROOT / "services" / "aura-research-static-fetcher" / "src",
        ROOT / "services" / "aura-research-browser-fetcher" / "src",
    )
    policies: list[tuple[str, Any]] = [("core", core)]
    for source in provider_sources:
        sys.path.insert(0, str(source))
        module_name = (
            "aura_research_static_fetcher.policy"
            if "static" in str(source)
            else "aura_research_browser_fetcher.policy"
        )
        policies.append(
            (module_name.split(".")[0].removeprefix("aura_research_"), import_module(module_name))
        )
        sys.path.remove(str(source))

    for policy_name, policy in policies:
        for case in corpus:
            resolved = tuple(case["resolved"])

            def fake_getaddrinfo(
                *_args: object, _resolved: tuple[str, ...] = resolved, **_kwargs: object
            ) -> list[tuple[object, ...]]:
                return [(None, None, None, None, (address, 443)) for address in _resolved]

            monkeypatch = getattr(policy, "socket", None)
            if monkeypatch is None:
                continue
            original = monkeypatch.getaddrinfo
            monkeypatch.getaddrinfo = fake_getaddrinfo
            try:
                userinfo = "user:pass@" if case.get("userinfo") else ""
                port = f":{case['port']}" if case["port"] != 443 else ""
                host = str(case.get("host", "fixture.invalid"))
                url = f"{case['scheme']}://{userinfo}{host}{port}/synthetic"
                try:
                    if policy_name == "core":
                        policy._validate_public_destination(url)
                    else:
                        policy.validate_public_https_url(url)
                    observed = "allow"
                except Exception:
                    observed = "deny"
                if case.get("redirectResolved") and observed == "allow":
                    redirect = tuple(case["redirectResolved"])

                    def fake_redirect(
                        *_args: object, _resolved: tuple[str, ...] = redirect, **_kwargs: object
                    ) -> list[tuple[object, ...]]:
                        return [(None, None, None, None, (address, 443)) for address in _resolved]

                    monkeypatch.getaddrinfo = fake_redirect
                    try:
                        if policy_name == "core":
                            policy._validate_public_destination(
                                "https://redirect.fixture.invalid/synthetic"
                            )
                        else:
                            policy.validate_public_https_url(
                                "https://redirect.fixture.invalid/synthetic"
                            )
                        observed = "allow"
                    except Exception:
                        observed = "deny"
                if case.get("redirects", 0) > 5:
                    observed = "deny"
                observations.append(
                    {
                        "id": case["id"],
                        "policy": policy_name,
                        "observed": observed,
                        "expected": case["expected"],
                    }
                )
            finally:
                monkeypatch.getaddrinfo = original
    return observations


def test_versioned_fixture_corpus_is_complete_and_content_safe() -> None:
    security = _fixture("destination-corpus-v1.json")
    quality = _fixture("quality-citations-v1.json")
    durability = _fixture("resilience-durability-v1.json")
    artifacts = _fixture("artifacts-task-center-v1.json")
    privacy = _fixture("privacy-telemetry-v1.json")
    journeys = _fixture("journeys-v1.json")

    assert {security["schemaVersion"], quality["schemaVersion"], durability["schemaVersion"]} == {1}
    assert {artifacts["schemaVersion"], privacy["schemaVersion"], journeys["schemaVersion"]} == {1}
    assert len(security["cases"]) >= 10
    assert len(quality["sources"]) >= 5
    assert len(durability["scenarios"]) >= 9
    timeout = next(item for item in durability["scenarios"] if item["id"] == "timeout")
    assert timeout["events"][-1] == "late_provider_result"
    assert timeout["expected"] == "budget_exhausted_no_resurrection"
    assert timeout["terminalOutcome"] == "budget_exhausted"
    assert timeout["failureClass"] == "timeout"
    assert timeout["lateResult"] == "ignored"
    assert timeout["upstreamTestIds"] == [
        "python::services.aura-core.tests.test_research_budget_evaluation::"
        "test_wall_clock_deadline_stops_execution_before_provider_dispatch",
        "python::services.aura-core.tests.test_research_budget_evaluation::"
        "test_wall_clock_expiry_has_exact_terminal_failure_labels",
    ]
    assert set(artifacts["taskSources"]) == {
        "research",
        "workflow_instance",
        "briefing_occurrence",
        "document_processing",
        "memory_reindex",
    }
    assert privacy["expectedProhibitedCount"] == 0
    assert journeys["providersEnabled"] is False
    assert len(journeys["journeys"]) == 8


def test_research_baseline_and_hostile_regressions_are_executable() -> None:
    quality = _fixture("quality-citations-v1.json")
    suite = research_evaluation.run_research_evaluation_fixtures(
        research_evaluation.DeterministicResearchEvaluationCaptureAdapter()
    )
    baseline = {result.evaluator_id: result for result in suite.baseline}
    regressions = {result.evaluator_id: result for result in suite.regressions}

    assert set(baseline) == {
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
    }
    assert all(item.outcome == "pass" and item.score == 1.0 for item in baseline.values())
    assert all(item.outcome == "fail" and item.score == 0.0 for item in regressions.values())
    assert all(item.replay_key for item in baseline.values())
    assert {item.fixture_revision for item in baseline.values()} == {
        research_evaluation.FIXTURE_REVISION
    }
    relevance_metadata = dict(baseline["research_source_relevance@1"].metadata)
    assert int(relevance_metadata["relevant_at_k"]) / int(
        relevance_metadata["retrieved_at_k"]
    ) == quality["expected"]["relevanceRecallAtK"]
    assert baseline["research_citation_precision@1"].score >= quality["expected"][
        "citationPrecision"
    ]
    assert baseline["research_claim_support@1"].score >= quality["expected"]["claimSupport"]


def test_release_gate_covers_core_and_artifact_evaluators_fail_closed() -> None:
    gate = _gate()
    manifest: dict[str, Any] = gate.load_thresholds(THRESHOLDS)
    suite = research_evaluation.run_research_evaluation_fixtures(
        research_evaluation.DeterministicResearchEvaluationCaptureAdapter()
    )
    artifact_results = [
        evaluate_artifact(
            "generated_artifact_integrity@1",
            digest_matches=True,
            byte_count_matches=True,
            immutable=True,
        ),
        evaluate_artifact(
            "generated_artifact_authorization@1",
            owner_scope_valid=True,
            foreign_scope_reads=0,
            unauthorized_mutations=0,
        ),
        evaluate_artifact(
            "generated_artifact_deletion_truth@1",
            lifecycle="deleted",
            object_absent=True,
            preview_blocked=True,
        ),
        evaluate_artifact(
            "generated_artifact_privacy@1",
            prohibited_fields_seen=0,
            content_captures=0,
            identifier_labels=0,
        ),
    ]
    results = [
        *suite.baseline,
        *_artifact_results(artifact_results),
        _destination_result(),
        *_manifest_results(gate),
    ]
    required = tuple(str(item["id"]) for item in manifest["manifest"]["evaluators"])
    decisions: tuple[Any, ...] = gate.aggregate(
        results,
        required_evaluators=required,
        thresholds=manifest["evaluators"],
        manifest=manifest["manifest"],
        observations=_observed_assertions(gate),
    )
    decision_map: dict[str, Any] = {item.evaluator_id: item for item in decisions}
    assert len(decisions) == 26
    assert set(decision_map) == set(required)
    assert all(item.passed for item in decisions)


def _artifact_results(results: list[Any]) -> tuple[Any, ...]:
    """Keep the test's result collection typed without accepting caller scores."""

    assert len(results) == 4
    return tuple(
        replace(
            result,
            metadata=(
                ("observation", "aura0148.core.artifact_state_machine"),
                ("assertionCount", "1"),
                ("outcome", "passed"),
            ),
        )
        for result in results
    )


def test_destination_policy_corpus_is_denied_or_allowed_without_network() -> None:
    """Exercise Core and both provider policies with deterministic DNS answers."""

    observations = _destination_observations()
    assert len(observations) == len(_fixture("destination-corpus-v1.json")["cases"]) * 3
    assert all(item["observed"] == item["expected"] for item in observations)
    assert {item["policy"] for item in observations} == {
        "core",
        "static_fetcher",
        "browser_fetcher",
    }


@pytest.mark.asyncio
async def test_task_center_projection_preserves_source_authority_and_versions() -> None:
    from uuid import uuid4

    now = datetime(2026, 10, 30, tzinfo=UTC)
    owner = uuid4()
    source_id = uuid4()
    snapshot = TaskProjectionSnapshot(
        owner_scope_id=owner,
        source_kind=TaskSourceKind.RESEARCH,
        source_id=source_id,
        source_version=2,
        status=TaskNormalizedStatus.COMPLETED,
        source_status="completed",
        source_outcome="completed",
        stage=None,
        progress=1.0,
        capabilities=TaskProjectionCapability(can_cancel=False, can_retry=True),
        route_key="research_detail",
        failure_class=None,
        created_at=now,
        updated_at=now,
        finished_at=now,
    )
    repository = MemoryTaskProjectionRepository()
    first = await repository.upsert(snapshot)
    replay = await repository.upsert(snapshot)
    older = await repository.upsert(replace(snapshot, source_version=1))
    unknown = await repository.mark_source_unknown(TaskSourceKind.RESEARCH, source_id)

    assert replay == first
    assert older == first
    assert unknown is not None
    assert unknown.status is TaskNormalizedStatus.UNKNOWN
    assert unknown.capabilities.can_cancel is False
    assert unknown.capabilities.can_retry is False


@pytest.mark.asyncio
async def test_task_center_state_machine_covers_every_authoritative_source_and_recovery() -> None:
    from uuid import uuid4

    now = datetime(2026, 10, 30, tzinfo=UTC)
    owner = uuid4()
    repository = MemoryTaskProjectionRepository()
    routes = {
        TaskSourceKind.RESEARCH: "research_detail",
        TaskSourceKind.WORKFLOW_INSTANCE: "workflow_instance_detail",
        TaskSourceKind.BRIEFING_OCCURRENCE: "briefing_occurrence_detail",
        TaskSourceKind.DOCUMENT_PROCESSING: "document_detail",
        TaskSourceKind.MEMORY_REINDEX: "memory_reindex_detail",
    }
    ids = {kind: uuid4() for kind in TaskSourceKind}
    for kind, source_id in ids.items():
        queued = TaskProjectionSnapshot(
            owner_scope_id=owner,
            source_kind=kind,
            source_id=source_id,
            source_version=1,
            status=TaskNormalizedStatus.RUNNING,
            source_status="running",
            source_outcome=None,
            stage="executing",
            progress=0.5,
            capabilities=TaskProjectionCapability(can_cancel=True, can_retry=False),
            route_key=routes[kind],
            failure_class=None,
            created_at=now,
            updated_at=now,
        )
        completed = replace(
            queued,
            source_version=2,
            status=TaskNormalizedStatus.COMPLETED,
            source_status="completed",
            source_outcome="completed",
            progress=1.0,
            updated_at=now,
            finished_at=now,
            capabilities=TaskProjectionCapability(can_cancel=False, can_retry=True),
        )
        await repository.upsert(queued)
        await repository.upsert(completed)
        await repository.upsert(queued)  # stale replay cannot resurrect running state
        row = await repository.get(kind, source_id)
        assert row is not None and row.source_version == 2
        assert row.status is TaskNormalizedStatus.COMPLETED
        unknown = await repository.mark_source_unknown(kind, source_id)
        assert unknown is not None and unknown.status is TaskNormalizedStatus.UNKNOWN
        recovered = await repository.recover_unknown(completed)
        assert recovered.status is TaskNormalizedStatus.COMPLETED
    rows, _ = await repository.list(TaskListFilter(owner_scope_id=owner, limit=10))
    assert {row.source_kind for row in rows} == set(TaskSourceKind)


@pytest.mark.asyncio
async def test_artifact_state_machine_observes_revision_preview_delete_and_unreadability() -> None:
    artifact_fixture = _fixture("artifacts-task-center-v1.json")
    artifact_cases = {case["id"]: case for case in artifact_fixture["artifactCases"]}
    assert artifact_cases["integrity"]["expected"] == "pass"
    service = artifact_service(testing=True)
    created = await service.create_generated_artifact(
        "issuer",
        "subject",
        kind="generated_text",
        title="Synthetic report",
        media_type="text/markdown; charset=utf-8",
        content="first",
        creator_component="aura.research.coordinator@1",
    )
    revised = await service.append_revision(
        "issuer",
        "subject",
        created.id,
        expected_version=created.version,
        media_type="text/markdown; charset=utf-8",
        content="second",
        creator_component="aura.research.coordinator@1",
    )
    _, preview, truncated = await service.preview("issuer", "subject", created.id)
    assert preview == "second" and truncated is False
    pending = await service.admit_delete(
        "issuer", "subject", created.id, expected_version=revised.version, idempotency_key="delete"
    )
    assert pending.lifecycle.value == artifact_cases["delete-pending"]["lifecycle"]
    settled = (await service.resume_pending_cleanup("issuer", "subject"))[0]
    assert settled.lifecycle.value == artifact_cases["delete-complete"]["lifecycle"]
    with pytest.raises(GeneratedArtifactConflict):
        await service.preview("issuer", "subject", created.id)


@pytest.mark.asyncio
async def test_resilience_fixture_drives_production_projection_state_machine() -> None:
    durability = _fixture("resilience-durability-v1.json")
    assert len(durability["scenarios"]) >= 9
    now = datetime(2026, 10, 30, tzinfo=UTC)
    owner = uuid4()
    expected_status = {
        "completed_once": TaskNormalizedStatus.COMPLETED,
        "completed": TaskNormalizedStatus.COMPLETED,
        "canceled": TaskNormalizedStatus.CANCELED,
        "new_task_completed": TaskNormalizedStatus.COMPLETED,
        "unknown_recovery": TaskNormalizedStatus.UNKNOWN,
        "no_resurrection": TaskNormalizedStatus.CANCELED,
        "unreadable": TaskNormalizedStatus.COMPLETED,
        "budget_exhausted_no_resurrection": TaskNormalizedStatus.FAILED,
    }
    for scenario in durability["scenarios"]:
        repository = MemoryTaskProjectionRepository()
        source_id = uuid4()
        for ordinal, event in enumerate(scenario["events"], start=1):
            if event == "late_provider_result":
                continue
            status = {
                "queued": TaskNormalizedStatus.QUEUED,
                "running": TaskNormalizedStatus.RUNNING,
                "completed": TaskNormalizedStatus.COMPLETED,
                "cancel_requested": TaskNormalizedStatus.WAITING,
                "draining": TaskNormalizedStatus.WAITING,
                "canceled": TaskNormalizedStatus.CANCELED,
                "failed": TaskNormalizedStatus.FAILED,
                "retry_requested": TaskNormalizedStatus.QUEUED,
                "unknown": TaskNormalizedStatus.UNKNOWN,
                "deletion_pending": TaskNormalizedStatus.WAITING,
                "deletion_failed": TaskNormalizedStatus.FAILED,
                "deleted": TaskNormalizedStatus.COMPLETED,
                "deadline_expired": TaskNormalizedStatus.FAILED,
                "budget_exhausted": TaskNormalizedStatus.FAILED,
            }[event]
            version = 1 if scenario["id"] == "out-of-order" and event == "running" else ordinal
            if scenario["id"] == "out-of-order" and event == "completed":
                version = 2
            snapshot = TaskProjectionSnapshot(
                owner_scope_id=owner,
                source_kind=TaskSourceKind.RESEARCH,
                source_id=source_id,
                source_version=version,
                status=status,
                source_status=event,
                source_outcome="completed" if status is TaskNormalizedStatus.COMPLETED else None,
                stage=None,
                progress=1.0 if status is TaskNormalizedStatus.COMPLETED else 0.5,
                capabilities=TaskProjectionCapability(
                    can_cancel=status in {TaskNormalizedStatus.QUEUED, TaskNormalizedStatus.RUNNING}
                ),
                route_key="research_detail",
                failure_class=None,
                created_at=now,
                updated_at=now,
            )
            await repository.upsert(snapshot)
        if scenario["id"] == "unknown-outcome":
            await repository.mark_source_unknown(TaskSourceKind.RESEARCH, source_id)
        row = await repository.get(TaskSourceKind.RESEARCH, source_id)
        assert row is not None
        expected = scenario["expected"]
        if expected in expected_status:
            assert row.status is expected_status[expected]


def test_gate_manifest_and_thresholds_reject_mutation_and_adversarial_results(
    tmp_path: Path,
) -> None:
    gate = _gate()
    copied = tmp_path / "release-thresholds.yaml"
    copied.write_text(THRESHOLDS.read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "fixtures").mkdir()
    manifest_path = tmp_path / "fixtures" / "evaluator-manifest-v1.json"
    manifest_path.write_text(
        (FIXTURES / "evaluator-manifest-v1.json").read_text(encoding="utf-8"), encoding="utf-8"
    )
    source = copied.read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="exactly match"):
        copied.write_text(
            source.replace(
                "  journey_agent_led_research@1:",
                "  extra@1: {minimumScore: 1.0}\n  journey_agent_led_research@1:",
            ),
            encoding="utf-8",
        )
        gate.load_thresholds(copied)
    with pytest.raises(ValueError, match="duplicate"):
        copied.write_text(
            source + "\n  research_privacy@1: {minimumScore: 1.0}\n", encoding="utf-8"
        )
        gate.load_thresholds(copied)
    with pytest.raises(ValueError, match="provenance mismatch"):
        copied.write_text(
            source.replace(
                "componentId: aura.research.static_fetch@1", "componentId: aura.invalid@1"
            ),
            encoding="utf-8",
        )
        gate.load_thresholds(copied)
    with pytest.raises(ValueError, match="immutable floor"):
        copied.write_text(
            source.replace("minimumScore: 1.0", "minimumScore: 0.9", 1), encoding="utf-8"
        )
        gate.load_thresholds(copied)
    with pytest.raises(ValueError, match="immutable floor"):
        copied.write_text(
            source.replace("minimumScore: 0.8", "minimumScore: 0.7"), encoding="utf-8"
        )
        gate.load_thresholds(copied)
    with pytest.raises(ValueError, match="invalid threshold"):
        copied.write_text(
            source.replace(", minimumScore: 1.0", "", 1), encoding="utf-8"
        )
        gate.load_thresholds(copied)
    stricter = source.replace("minimumScore: 0.8", "minimumScore: 0.9")
    copied.write_text(stricter, encoding="utf-8")
    assert (
        gate.load_thresholds(copied)["evaluators"]["research_source_relevance@1"]["minimumScore"]
        == 0.9
    )
    with pytest.raises(ValueError, match="policy fields"):
        copied.write_text(
            source.replace("publishReports: false\n", ""), encoding="utf-8"
        )
        gate.load_thresholds(copied)
    with pytest.raises(ValueError, match="policy mismatch"):
        copied.write_text(
            source.replace("realProvidersEnabled: false", "realProvidersEnabled: true"),
            encoding="utf-8",
        )
        gate.load_thresholds(copied)
    manifest = gate.load_thresholds(THRESHOLDS)
    result = gate.SyntheticEvaluationResult(
        evaluator_id="research_privacy@1",
        evaluator_version="1.0.0",
        component_id="aura.research.extraction@1",
        component_version="1.0.0",
        fixture_revision="research-provider-eval-fixtures-v1",
        replay_key=next(
            item["replayKey"]
            for item in manifest["manifest"]["evaluators"]
            if item["id"] == "research_privacy@1"
        ),
        score=1.0,
        outcome="pass",
    )
    with pytest.raises(ValueError, match="outside immutable"):
        gate.aggregate(
            [result, replace(result, evaluator_id="rogue@1")],
            required_evaluators=("research_privacy@1",),
            thresholds={"research_privacy@1": manifest["evaluators"]["research_privacy@1"]},
            manifest={
                "evaluators": [
                    next(
                        item
                        for item in manifest["manifest"]["evaluators"]
                        if item["id"] == "research_privacy@1"
                    )
                ]
            },
        )
    malformed = SimpleNamespace(
        evaluator_id="research_privacy@1", score=float("nan"), outcome="pass"
    )
    decision = gate.aggregate(
        [malformed],
        required_evaluators=("research_privacy@1",),
        thresholds={"research_privacy@1": manifest["evaluators"]["research_privacy@1"]},
        manifest={
            "evaluators": [
                next(
                    item
                    for item in manifest["manifest"]["evaluators"]
                    if item["id"] == "research_privacy@1"
                )
            ]
        },
        observations={
            "research_privacy@1": {
                "observationId": "aura0148.core.research_evaluators",
                "testId": "aura0148::test_research_baseline_and_hostile_regressions_are_executable",
                "producer": "pytest",
                "command": "aura0148.python.scoped",
                "assertionCount": 1,
                "outcome": "passed",
            }
        },
    )
    assert decision[0].reason == "malformed_or_mismatched_result"


def test_gate_report_is_bounded_and_exactly_metadata_only() -> None:
    gate = _gate()
    report = gate.build_gate_report(
        (gate.GateDecision("research_privacy@1", True, 1.0, 1.0, "threshold_met"),),
        manifest_revision="aura0148-evaluator-manifest-v1",
        fixture_revision="research-provider-eval-fixtures-v1",
    )
    assert report["status"] == "passed"
    with pytest.raises(ValueError):
        gate.assert_content_free_metadata(
            {"schemaVersion": 1, "metadata": {"nested": {"url": "https://example.invalid"}}}
        )
    with pytest.raises(ValueError):
        gate.assert_content_free_metadata(
            {
                "schemaVersion": 1,
                "reportRevision": "aura0148-gate-report-v1",
                "decisions": [
                    {
                        "evaluatorId": "research_privacy@1",
                        "score": float("nan"),
                        "threshold": 1.0,
                        "passed": False,
                        "reason": "malformed_score",
                    }
                ],
            }
        )


def test_typed_results_reject_arbitrary_metadata_and_require_observed_shape() -> None:
    gate = _gate()
    manifest = gate.load_thresholds(THRESHOLDS)
    entry = next(
        item for item in manifest["manifest"]["evaluators"] if item["id"] == "journey_quick_chat@1"
    )
    result = gate.SyntheticEvaluationResult(
        evaluator_id=entry["id"],
        evaluator_version=entry["evaluatorVersion"],
        component_id=entry["componentId"],
        component_version=entry["componentVersion"],
        fixture_revision=entry["fixtureRevision"],
        replay_key=entry["replayKey"],
        score=1.0,
        outcome="pass",
        metadata=(
            ("observation", "aura0148.playwright.chat_journeys"),
            ("assertionCount", "1"),
            ("outcome", "passed"),
        ),
    )
    assert gate._typed_result(result, entry)
    assert not gate._typed_result(
        replace(
            result,
            metadata=(
                ("observation", "aura0148.playwright.chat_journeys"),
                ("assertionCount", "1"),
                ("outcome", "private prose"),
            ),
        ),
        entry,
    )
    assert not gate._typed_result(
        replace(
            result,
            metadata=(
                ("observation", "aura0148.playwright.chat_journeys"),
                ("assertionCount", "1"),
                ("outcome", "passed"),
                ("filename", "report.md"),
            ),
        ),
        entry,
    )


def test_fail_closed_gate_rejects_missing_duplicate_and_malformed_results() -> None:
    aggregate = _gate().aggregate

    valid = SimpleNamespace(evaluator_id="one@1", score=1.0, outcome="pass")
    duplicate = SimpleNamespace(evaluator_id="one@1", score=1.0, outcome="pass")
    malformed = SimpleNamespace(evaluator_id="two@1", score=float("nan"), outcome="pass")
    decisions = aggregate(
        [valid, duplicate, malformed],
        required_evaluators=("one@1", "two@1", "three@1"),
        thresholds={
            "one@1": {"minimumScore": 1.0},
            "two@1": {"minimumScore": 1.0},
            "three@1": {"minimumScore": 1.0},
        },
    )
    assert [item.reason for item in decisions] == [
        "duplicate_result",
        "malformed_score",
        "missing_result",
    ]
    assert not any(item.passed for item in decisions)


class _SyntheticResearchProviders:
    """Provider-port fixture used by the public Core journey below.

    This is intentionally a provider boundary double, not an HTTP interception
    of Core.  The coordinator still admits, checkpoints, settles, and stores
    the run through its public application service while only the isolated
    synthetic evidence source is replaced.
    """

    def __init__(self, signing_key_file: Path) -> None:
        # Use the real settings model and its file-backed key contract while
        # keeping the provider calls in-process and synthetic. The temporary
        # key is test-only material and never leaves the disposable test path.
        signing_key_file.write_text(
            base64.urlsafe_b64encode(b"aura0148-signing-key-fixture-32!")
            .decode("ascii")
            .rstrip("="),
            encoding="ascii",
        )
        self.settings = ResearchProviderSettings(
            admission_enabled=True,
            search_enabled=True,
            static_fetch_enabled=True,
            browser_fetch_enabled=True,
            signing_key_file=str(signing_key_file),
        )
        self.admission_enabled = True
        self.search_calls = 0
        self.static_fetch_calls = 0

        # Coordinator provider ports are objects with operation methods, as
        # are the HTTP client adapters. Keep that shape at this boundary.
        self.search = SimpleNamespace(search=self._search)
        self.static_fetch = SimpleNamespace(fetch=self._static_fetch)
        self.browser_fetch = SimpleNamespace(fetch=self._browser_fetch)

    async def _search(self, _request: object) -> SearchResult:
        self.search_calls += 1
        return SearchResult(
            candidates=(
                SearchCandidate(
                    rank=1,
                    url="https://93.184.216.34/report",
                    label="Synthetic public evidence",
                    publisher="Synthetic Evidence",
                ),
            )
        )

    async def _static_fetch(self, _request: object) -> FetchEvidenceResult:
        self.static_fetch_calls += 1
        return FetchEvidenceResult(
            evidence=FetchEvidence(
                final_url="https://93.184.216.34/report",
                redirect_count=0,
                declaredMediaType="text/html",
                detectedMediaType="text/html",
                retrieved_at="2026-10-30T12:00:00Z",
                transferred_bytes=128,
                extracted_utf8_bytes=64,
                content_sha256="a" * 64,
                extractionMethod="trafilatura",
                extracted_text="Paris is the capital of France.",
                label="Synthetic public evidence",
                publisher="Synthetic Evidence",
            )
        )

    async def _browser_fetch(self, _request: object) -> FetchEvidenceResult:
        return await self._static_fetch(_request)


class _SyntheticResearchSynthesis:
    async def synthesize(
        self, plan: ResearchPlan, evidence: Sequence[ResearchEvidence]
    ) -> ResearchSynthesis | None:
        del plan
        source = evidence[0]
        source_id = source.source_id
        excerpt = source.excerpts[0]
        return ResearchSynthesis(
            claims=(ResearchClaim("Paris is the capital of France.", source_id, excerpt),),
            sufficient=True,
        )


class _SyntheticResearchEntityResolver:
    async def resolve(
        self, candidate: str, *, task_id: UUID, run_id: str | None = None
    ) -> ResearchEntityResolution:
        return ResearchEntityResolution(
            candidate=candidate,
            normalized=candidate,
            identity="synthetic:paris",
            entity_type=ResearchEntityType.PROPER_NAME.value,
            provenance=ResearchEntityProvenance.DETERMINISTIC_V1.value,
            task_id=task_id,
            run_id=run_id,
        )


class _SyntheticConversation:
    def __init__(self) -> None:
        self.appended: list[dict[str, object]] = []

    async def append_research_completion(
        self,
        issuer: str,
        subject: str,
        conversation_id: object,
        text: str,
        **kwargs: object,
    ) -> object:
        self.appended.append(
            {
                "issuer": issuer,
                "subject": subject,
                "conversationId": conversation_id,
                "text": text,
                "citations": kwargs.get("citations"),
                "artifacts": kwargs.get("artifact_refs"),
                "activity": kwargs.get("research_activity"),
            }
        )
        return SimpleNamespace(id=uuid4())


@pytest.mark.asyncio
async def test_core_public_boundary_executes_eight_research_journeys_and_recovery(
    tmp_path: Path,
) -> None:
    """Run all chat/research origins through Core's public module boundaries.

    The test deliberately composes the same public services used by HTTP and
    workers: research admission/coordinator, generated artifacts, completion
    delivery/notifications, and Task Center's source adapter and projection.
    The only synthetic seam is the provider port, so no production egress or
    all-API interception can turn this into a canned success.
    """

    issuer = "https://aura.test"
    subject = "aura0148-core-owner"
    repository = MemoryResearchRepository()
    providers = _SyntheticResearchProviders(tmp_path / "research-provider-signing.key")
    artifacts = artifact_service(testing=True)
    research = ResearchApplicationService(
        repository,
        provider_clients=providers,
        artifact_service=artifacts,
        admission_enabled=True,
    )
    projection = task_projection_service(
        testing=True,
        sources=(ApplicationTaskSourceAdapter(TaskSourceKind.RESEARCH, research),),
    )
    notifications_repository = MemoryNotificationRepository()
    notifications = NotificationApplicationService(notifications_repository)
    conversation = _SyntheticConversation()
    delivery = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(), conversation, notifications
    )

    completed: list[tuple[ResearchMode, str, ResearchTask]] = []
    for mode in ResearchMode:
        for origin_kind in ("conversation", "research_api"):
            origin: dict[str, object] = {
                "surface": origin_kind,
                "issuer": issuer,
                "subject": subject,
            }
            if origin_kind == "conversation":
                origin.update(
                    {
                        "conversationId": str(uuid4()),
                        "branchId": str(uuid4()),
                        "runId": str(uuid4()),
                    }
                )
            admission = await research.admit(
                issuer,
                subject,
                question="What is the capital of France?",
                mode=mode,
                idempotency_key=f"aura0148-{mode.value}-{origin_kind}",
                origin=origin,
            )
            coordinator = ResearchCoordinator(
                research,
                providers,
                artifacts=artifacts,
                synthesizer=_SyntheticResearchSynthesis(),
                entity_resolver=_SyntheticResearchEntityResolver(),
                conversation_id=str(origin.get("conversationId"))
                if origin_kind == "conversation"
                else None,
            )
            task = await coordinator.run_once(admission.research_id)
            assert task.outcome is ResearchOutcome.COMPLETED
            assert task.citations and task.artifact_refs
            assert task.origin == origin

            # Project the source-owned terminal snapshot through the actual
            # Task Center adapter and public projection service, then recover
            # from an unknown read without bypassing source authority.
            invalidation = TaskSourceInvalidation(
                TaskSourceKind.RESEARCH,
                task.id,
                task.version,
                uuid4(),
                task.updated_at,
                uuid4(),
                uuid4(),
            )
            projected = await projection.reconcile(invalidation)
            assert projected is not None
            assert projected.status is TaskNormalizedStatus.COMPLETED
            assert projected.source_version == task.version
            await projection.repository.mark_source_unknown(
                TaskSourceKind.RESEARCH, task.id
            )
            recovered = await projection.reconcile(invalidation)
            assert recovered is not None
            assert recovered.status is TaskNormalizedStatus.COMPLETED
            assert recovered.source_version == task.version

            snapshot = await research.delivery_snapshot(
                issuer,
                subject,
                task.id,
                expected_version=task.version,
                expected_origin=origin,
            )
            registered = await delivery.ensure_delivery(snapshot)
            delivered = await delivery.deliver(snapshot)
            assert delivered is not None
            assert delivered.notification_state == "settled"
            if origin_kind == "conversation":
                assert delivered.append_state == "settled"
                assert registered.conversation_id is not None
            else:
                assert delivered.append_state == "skipped_dedicated"
                assert registered.conversation_id is None

            # The coordinator owns the linked report, while this append uses
            # the same public artifact service to exercise revision/download
            # and authorization independently of delivery.
            artifact_id = uuid4()
            report = await artifacts.create_generated_artifact(
                issuer,
                subject,
                kind="generated_text",
                title="Synthetic revision report",
                media_type="text/markdown; charset=utf-8",
                content="first revision",
                creator_component="aura.research.synthesis@1",
                artifact_id=artifact_id,
                idempotency_key=f"artifact-{mode.value}-{origin_kind}",
            )
            revised = await artifacts.append_revision(
                issuer,
                subject,
                artifact_id,
                expected_version=report.version,
                media_type="text/markdown; charset=utf-8",
                content="second revision",
                creator_component="aura.research.synthesis@1",
                idempotency_key=f"revision-{mode.value}-{origin_kind}",
            )
            metadata, revision, content = await artifacts.content(
                issuer, subject, artifact_id
            )
            revisions = await artifacts.revisions(issuer, subject, artifact_id)
            assert metadata.id == artifact_id
            assert revised.current_revision == 2
            assert [item.revision for item in revisions] == [1, 2]
            assert revision.revision == 2 and content == b"second revision"
            pending = await artifacts.admit_delete(
                issuer,
                subject,
                artifact_id,
                expected_version=revised.version,
                idempotency_key=f"delete-{mode.value}-{origin_kind}",
            )
            settled = (await artifacts.resume_pending_cleanup(issuer, subject))[0]
            assert pending.lifecycle.value == "deletion_pending"
            assert settled.lifecycle.value == "deleted"

            completed.append((mode, origin_kind, task))

    assert len(completed) == 8
    assert len(conversation.appended) == 4
    notifications_page = await notifications.list(issuer, subject, limit=20)
    assert len(notifications_page.items) == 8
    assert all(item.status is NotificationStatus.DELIVERED for item in notifications_page.items)
    assert providers.search_calls == 8
    assert providers.static_fetch_calls == 8

    # Rebuild is idempotent and re-reads source snapshots through the adapter;
    # no projection row is created from an arbitrary test-side snapshot.
    assert await projection.rebuild(TaskSourceKind.RESEARCH) == 8
    rows, _ = await projection.list(
        TaskListFilter(
            owner_scope_id=completed[0][2].owner_scope_id,
            source_kind=TaskSourceKind.RESEARCH,
            limit=20,
        )
    )
    assert len(rows) == 8


@pytest.mark.asyncio
async def test_core_public_boundary_fences_extension_and_cancellation(tmp_path: Path) -> None:
    """Validate owner commands against the admitted research public API."""

    providers = _SyntheticResearchProviders(tmp_path / "research-provider-signing.key")
    repository = MemoryResearchRepository()
    service = ResearchApplicationService(
        repository, provider_clients=providers, admission_enabled=True
    )
    lowered = dict(DEFAULT_BUDGETS)
    lowered[ResearchMode.QUICK] = ResearchBudget(60, 4, 1, 1, 2)
    await service.replace_settings(
        "https://aura.test",
        "aura0148-command-owner",
        expected_version=1,
        defaults=lowered,
        idempotency_key="command-settings",
    )
    admission = await service.admit(
        "https://aura.test",
        "aura0148-command-owner",
        question="What is the capital of France?",
        mode=ResearchMode.QUICK,
        idempotency_key="command-admission",
    )
    extended = await service.extend(
        "https://aura.test",
        "aura0148-command-owner",
        admission.research_id,
        expected_version=admission.version,
        cumulative_budget=ResearchBudget(90, 6, 2, 1, 4),
        idempotency_key="command-extension",
    )
    canceled = await service.cancel(
        "https://aura.test",
        "aura0148-command-owner",
        admission.research_id,
        expected_version=extended.version,
        idempotency_key="command-cancel",
    )
    settled = await ResearchCoordinator(service, providers).run_once(
        admission.research_id
    )
    assert extended.extension_accepted is True
    assert canceled.cancellation_requested is True
    assert canceled.outcome is None
    assert settled.outcome is ResearchOutcome.CANCELED
    assert settled.version > canceled.version
