"""AURA-0098 result-to-score projection coverage."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from aura_observatory.adapters.evaluation_definitions import InMemoryEvaluationDefinitionRepository
from aura_observatory.evaluation_definitions import (
    DefinitionStorageError,
    DefinitionValidationError,
    EvaluationDefinitionService,
)
from aura_observatory.evaluation_evaluators import EvaluationApplicationService
from aura_observatory.evaluation_orchestration import (
    EvaluationAdmission,
    EvaluationOrchestrationService,
    InMemoryEvaluationOrchestrationRepository,
    OrchestrationDependencyError,
)
from aura_observatory.evaluation_storage import DeletionSelector, EvaluationScore

IDS = {
    "dataset": "00000000-0000-0000-0000-000000000001",
    "case": "00000000-0000-0000-0000-000000000002",
    "suite": "00000000-0000-0000-0000-000000000003",
    "evaluator": "00000000-0000-0000-0000-000000000004",
}
DIGEST = "a" * 64


def _resource(kind: str, resource_id: str) -> dict[str, Any]:
    del resource_id
    return {
        "schemaVersion": 1,
        "resourceType": kind,
        "retentionMode": "manual_only",
        "provenance": {
            "createdByComponent": "aura.observatory.tests",
            "createdByVersion": "1.0.0",
            "sourceDigest": DIGEST,
        },
        "createdAt": "2026-10-20T12:00:00Z",
    }


def _authorization() -> dict[str, str]:
    return {
        "captureClassification": "metadata_only",
        "authorizationPurpose": "synthetic_benchmark",
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
    }


def _component() -> dict[str, str]:
    return {
        "componentId": "aura.core.runner",
        "componentVersion": "1.0.0",
        "codeRevision": "0000000",
        "imageDigest": DIGEST,
        "configurationRevision": "local",
    }


def _dataset(version: str = "1.0.0") -> dict[str, Any]:
    return _resource("dataset", IDS["dataset"]) | {
        "datasetId": IDS["dataset"],
        "datasetVersion": version,
        "sourceKind": "synthetic",
        "scopes": ["public_project"],
        "captureAuthorization": _authorization(),
        "caseIds": [IDS["case"]],
    }


def _case(version: str = "1.0.0") -> dict[str, Any]:
    return _resource("case", IDS["case"]) | {
        "caseId": IDS["case"],
        "caseVersion": version,
        "dataset": {"id": IDS["dataset"], "version": version},
        "sourceKind": "synthetic",
        "scopes": ["public_project"],
        "captureAuthorization": _authorization(),
        "expectations": {
            "exact": {
                "fields": [{"fieldId": "answer", "expectedDigest": DIGEST, "normalization": "none"}]
            }
        },
    }


def _evaluator() -> dict[str, Any]:
    return _resource("evaluator", IDS["evaluator"]) | {
        "evaluatorId": IDS["evaluator"],
        "evaluatorVersion": "1.0.0",
        "kind": "deterministic",
        "metric": "exact_match",
        "componentIds": ["aura.core.runner"],
    }


def _suite(version: str = "1.0.0") -> dict[str, Any]:
    return _resource("suite", IDS["suite"]) | {
        "suiteId": IDS["suite"],
        "suiteVersion": version,
        "dataset": {"id": IDS["dataset"], "version": version},
        "evaluators": [
            {"evaluatorId": IDS["evaluator"], "evaluatorVersion": "1.0.0", "kind": "deterministic"}
        ],
        "components": [_component()],
        "limits": {
            "timeoutMs": 1000,
            "maxConcurrency": 1,
            "maxAttempts": 2,
            "maxCases": 10,
            "maxInputBytes": 1000,
            "maxOutputBytes": 1000,
            "maxTokens": 100,
        },
    }


class _DefinitionRepository(InMemoryEvaluationDefinitionRepository):
    async def renew_definition_command(
        self,
        operation: str,
        idempotency_key: str,
        request_fingerprint: str,
        claim_token: str,
    ) -> bool:
        del operation, idempotency_key, request_fingerprint, claim_token
        return True


class _MembershipStorageFailureRepository(_DefinitionRepository):
    async def case_versions_for_selection(
        self, dataset_id: str, version: str, case_ids: tuple[str, ...]
    ) -> dict[str, str]:
        del dataset_id, version, case_ids
        raise DefinitionStorageError("membership storage failure")


class _ScoreStore:
    def __init__(self) -> None:
        self.scores: list[EvaluationScore] = []

    async def insert_score(self, score: EvaluationScore) -> None:
        score.validate()
        if any(existing.score_id == score.score_id for existing in self.scores):
            return
        self.scores.append(score)

    async def delete_scores_for_artifact(self, artifact_reference_id: str) -> None:
        del artifact_reference_id

    async def delete_scores_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime | None = None
    ) -> None:
        del selector, snapshot_at

    async def list_score_ids_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime, limit: int
    ) -> list[str]:
        del selector, snapshot_at, limit
        return []

    async def delete_scores_for_ids(self, score_ids: Sequence[str]) -> None:
        del score_ids


def _bindings(version: str = "1.0.0") -> tuple[Mapping[str, object], Mapping[str, object]]:
    component = _component()
    provenance = {
        "dataset": {"id": IDS["dataset"], "version": version},
        "suite": {"id": IDS["suite"], "version": version},
        "evaluators": [
            {"evaluatorId": IDS["evaluator"], "evaluatorVersion": "1.0.0", "kind": "deterministic"}
        ],
        "components": [component],
        "model": {"modelId": "synthetic", "modelVersion": "1.0.0", "modelDigest": "a" * 64},
        "deploymentRevision": "aura0098-test",
        "reproducibility": {
            "seed": 7,
            "clockMode": "fixed",
            "timeZone": "UTC",
            "locale": "en-US",
            "fixtureRevision": "synthetic-v1",
            "promptRevision": "prompt-v1",
            "policyRevision": "policy-v1",
        },
    }
    request = {
        "caseIds": [IDS["case"]],
        "targetComponent": component,
        "provenance": provenance,
    }
    result = {
        "status": "completed",
        "resultId": IDS["case"],
        "completedAt": "2026-10-20T12:00:00Z",
        "runtimeEvidence": {"fixtureRevision": {"status": "missing"}},
    }
    return request, result


async def _application(version: str = "1.0.0") -> tuple[EvaluationApplicationService, _ScoreStore]:
    repository = _DefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    definitions = EvaluationDefinitionService(repository)
    await definitions.admit_dataset(_dataset(version))
    await definitions.admit_case(_case(version))
    await definitions.admit_evaluator(_evaluator())
    await definitions.admit_suite(_suite(version))
    scores = _ScoreStore()
    return EvaluationApplicationService(repository, scores), scores


@pytest.mark.asyncio
async def test_completed_result_persists_job_correlated_coverage_score() -> None:
    application, scores = await _application()
    request, result = _bindings()

    persisted = await application.evaluate_result(
        job_id=IDS["dataset"],
        result_id=IDS["case"],
        request_data=request,
        result_data=result,
    )

    assert persisted == tuple(scores.scores)
    assert len(persisted) == 1
    score = persisted[0]
    assert score.job_id == IDS["dataset"]
    assert score.result_id == IDS["case"]
    assert score.case_id == IDS["case"]
    assert score.evidence_status == "not_observed"
    assert score.reproducibility == "deterministic-v1"
    assert score.execution_provenance is not None
    assert "content" not in str(score.execution_provenance).casefold()


@pytest.mark.asyncio
async def test_completed_result_resolves_non_initial_case_version_from_membership() -> None:
    application, scores = await _application("2.0.0")
    request, result = _bindings("2.0.0")

    persisted = await application.evaluate_result(
        job_id=IDS["dataset"],
        result_id=IDS["case"],
        request_data=request,
        result_data=result,
    )

    assert len(persisted) == 1
    assert persisted[0].case_id == IDS["case"]
    provenance = persisted[0].execution_provenance
    assert isinstance(provenance, Mapping)
    dataset_provenance = provenance["dataset"]
    assert isinstance(dataset_provenance, Mapping)
    assert dataset_provenance["resourceVersion"] == "2.0.0"
    assert len(scores.scores) == 1


@pytest.mark.asyncio
async def test_completed_result_with_mismatched_definition_version_is_permanent() -> None:
    application, scores = await _application("2.0.0")
    request, result = _bindings("1.0.0")

    with pytest.raises(DefinitionValidationError, match="case membership"):
        await application.evaluate_result(
            job_id=IDS["dataset"],
            result_id=IDS["case"],
            request_data=request,
            result_data=result,
        )
    assert scores.scores == []


@pytest.mark.asyncio
async def test_case_membership_storage_failure_remains_retryable() -> None:
    repository = _MembershipStorageFailureRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    definitions = EvaluationDefinitionService(repository)
    await definitions.admit_dataset(_dataset("2.0.0"))
    await definitions.admit_case(_case("2.0.0"))
    await definitions.admit_evaluator(_evaluator())
    await definitions.admit_suite(_suite("2.0.0"))
    application = EvaluationApplicationService(repository, _ScoreStore())
    request, result = _bindings("2.0.0")

    with pytest.raises(DefinitionStorageError, match="membership storage"):
        await application.evaluate_result(
            job_id=IDS["dataset"],
            result_id=IDS["case"],
            request_data=request,
            result_data=result,
        )


@pytest.mark.asyncio
async def test_versioned_result_redelivery_reuses_stable_score_identity() -> None:
    application, scores = await _application("2.0.0")
    request, result = _bindings("2.0.0")

    first = await application.evaluate_result(
        job_id=IDS["dataset"],
        result_id=IDS["case"],
        request_data=request,
        result_data=result,
    )
    second = await application.evaluate_result(
        job_id=IDS["dataset"],
        result_id=IDS["case"],
        request_data=request,
        result_data=result,
    )

    assert [score.score_id for score in first] == [score.score_id for score in second]
    assert len(scores.scores) == 1


@pytest.mark.asyncio
async def test_non_completed_result_does_not_write_score() -> None:
    application, scores = await _application()
    request, result = _bindings()
    failed = dict(result)
    failed["status"] = "failed"

    assert await application.evaluate_result(
        job_id=IDS["dataset"], result_id=IDS["case"], request_data=request, result_data=failed
    ) == ()
    assert scores.scores == []


@pytest.mark.asyncio
async def test_invalid_result_binding_fails_closed_without_storage_write() -> None:
    application, scores = await _application()
    request, result = _bindings()
    request = dict(request)
    request["targetComponent"] = {"componentId": "aura.private.content"}

    with pytest.raises(DefinitionValidationError):
        await application.evaluate_result(
            job_id=IDS["dataset"], result_id=IDS["case"], request_data=request, result_data=result
        )
    assert scores.scores == []


def _orchestration_admission() -> EvaluationAdmission:
    request, _result = _bindings()
    now = datetime.now(UTC)
    request_data = dict(request)
    request_data.update(
        {
            "executionKind": "synthetic_end_to_end",
            "requestVersion": "1.0.0",
            "captureAuthorization": _authorization(),
            "limits": {
                "execution": {
                    "timeoutMs": 1000,
                    "maxConcurrency": 1,
                    "maxAttempts": 2,
                    "maxCases": 1,
                    "maxInputBytes": 0,
                    "maxOutputBytes": 1000,
                    "maxTokens": 0,
                },
                "maxQueueTimeMs": 1000,
                "maxModelCalls": 1,
                "maxArtifactOperations": 0,
            },
            "deadlineAt": (now + timedelta(minutes=5)).isoformat(timespec="milliseconds").replace(
                "+00:00", "Z"
            ),
            "inputArtifactReferences": [],
        }
    )
    return EvaluationAdmission(
        request_id=str(uuid4()),
        idempotency_key=str(uuid4()),
        request_version="1.0.0",
        dataset_id=IDS["dataset"],
        dataset_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        capture_classification="metadata_only",
        deadline_at=now + timedelta(minutes=5),
        max_attempts=2,
        envelope={"data": request_data},
        created_at=now,
    )


def _completed_envelope(request: Mapping[str, object]) -> dict[str, object]:
    request_data = request["data"]
    assert isinstance(request_data, Mapping)
    occurred_at = datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    runtime_evidence = {
        key: {"status": "missing"}
        for key in (
            "componentVersion",
            "codeRevision",
            "imageDigest",
            "configurationRevision",
            "modelId",
            "modelVersion",
            "modelDigest",
            "deploymentRevision",
            "fixtureRevision",
            "promptRevision",
            "policyRevision",
            "seed",
        )
    }
    result = dict(request)
    result["messageId"] = str(uuid4())
    result["messageType"] = "evaluation.runner.completed"
    result["occurredAt"] = occurred_at
    result["causationId"] = request["messageId"]
    result["data"] = {
        "resultId": IDS["case"],
        "resultDigest": DIGEST,
        "status": "completed",
        "startedAt": occurred_at,
        "completedAt": occurred_at,
        "deadlineAt": request_data["deadlineAt"],
        "captureAuthorization": request_data["captureAuthorization"],
        "effectiveLimits": request_data["limits"],
        "provenance": request_data["provenance"],
        "runtimeEvidence": runtime_evidence,
        "outputArtifactReferences": [],
    }
    return result


class _Metrics:
    def __init__(self) -> None:
        self.values: list[tuple[str, dict[str, str], float]] = []
        self.spans: list[dict[str, object]] = []

    def record(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        self.values.append((name, dict(labels or {}), value))

    def set(
        self, name: str, value: float = 1.0, labels: Mapping[str, str] | None = None
    ) -> None:
        self.record(name, value, labels)

    def record_component_span(self, **span: object) -> None:
        self.spans.append(span)


@pytest.mark.asyncio
async def test_projection_failure_is_repaired_by_redelivery_and_restart() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()

    async def failing_scorer(
        _job_id: str,
        _result_id: str,
        _request_data: Mapping[str, object],
        _result_data: Mapping[str, object],
    ) -> object:
        raise RuntimeError("clickhouse unavailable")

    first_service = EvaluationOrchestrationService(
        repository, result_scorer=failing_scorer
    )
    job = await first_service.admit(_orchestration_admission())
    request = next(
        entry.envelope for entry in repository.outbox.values() if entry.job_id == job.job_id
    )
    result = _completed_envelope(request)
    acknowledgements: list[str] = []

    async def acknowledge() -> None:
        acknowledgements.append("ack")

    with pytest.raises(OrchestrationDependencyError, match="score_projection_failure"):
        await first_service.consume_result(result, acknowledge=acknowledge)
    assert acknowledgements == []

    repair_calls = 0

    async def repair_scorer(
        _job_id: str,
        _result_id: str,
        _request_data: Mapping[str, object],
        _result_data: Mapping[str, object],
    ) -> object:
        nonlocal repair_calls
        repair_calls += 1
        return None

    replacement = EvaluationOrchestrationService(
        repository, result_scorer=repair_scorer
    )
    assert await replacement.consume_result(result, acknowledge=acknowledge) == "duplicate"
    assert acknowledgements == ["ack"]
    assert repair_calls == 1

    restarted = EvaluationOrchestrationService(repository, result_scorer=repair_scorer)
    assert await restarted.consume_result(result, acknowledge=acknowledge) == "duplicate"
    assert acknowledgements == ["ack", "ack"]
    assert repair_calls == 2


@pytest.mark.asyncio
async def test_projection_dependency_failure_is_attributed_once() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()
    metrics = _Metrics()

    async def scorer(
        _job_id: str,
        _result_id: str,
        _request_data: Mapping[str, object],
        _result_data: Mapping[str, object],
    ) -> object:
        raise RuntimeError("clickhouse unavailable")

    service = EvaluationOrchestrationService(
        repository, metrics=metrics, result_scorer=scorer
    )
    job = await service.admit(_orchestration_admission())
    request = next(
        entry.envelope for entry in repository.outbox.values() if entry.job_id == job.job_id
    )
    with pytest.raises(OrchestrationDependencyError):
        await service.consume_result(_completed_envelope(request))

    failures = [
        item
        for item in metrics.values
        if item[0] == "observatory_evaluation_dependency_outcome_count"
        and item[1] == {"operation": "clickhouse", "outcome": "failed"}
    ]
    assert len(failures) == 1
    assert len([span for span in metrics.spans if span.get("operation") == "score_projection"]) == 1


@pytest.mark.asyncio
async def test_permanent_projection_validation_is_acknowledged() -> None:
    repository = InMemoryEvaluationOrchestrationRepository()

    async def scorer(
        _job_id: str,
        _result_id: str,
        _request_data: Mapping[str, object],
        _result_data: Mapping[str, object],
    ) -> object:
        raise DefinitionValidationError("definition is incompatible")

    service = EvaluationOrchestrationService(repository, result_scorer=scorer)
    job = await service.admit(_orchestration_admission())
    request = next(
        entry.envelope for entry in repository.outbox.values() if entry.job_id == job.job_id
    )
    acknowledgements: list[str] = []

    async def acknowledge() -> None:
        acknowledgements.append("ack")

    assert await service.consume_result(
        _completed_envelope(request), acknowledge=acknowledge
    ) == "applied"
    assert acknowledgements == ["ack"]
