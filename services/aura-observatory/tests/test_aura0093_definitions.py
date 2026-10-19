"""AURA-0093 immutable definition admission and isolation tests."""
# pyright: reportPrivateUsage=false, reportArgumentType=false

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import pytest
from aura_observatory.adapters.evaluation_definitions import (
    InMemoryEvaluationDefinitionRepository,
    PostgresEvaluationDefinitionRepository,
)
from aura_observatory.evaluation_definitions import (
    DefinitionConflictError,
    DefinitionNotFoundError,
    DefinitionStorageError,
    DefinitionValidationError,
    EvaluationDefinitionService,
)
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import EvaluationArtifactMetadata
from test_aura0089_contract import _resources
from test_aura0093_contract import DIGEST, IDS, _expectations, _threshold


def _dataset() -> dict[str, Any]:
    return deepcopy(_resources()["dataset"])


def _case() -> dict[str, Any]:
    value = deepcopy(_resources()["case"])
    value["expectations"] = _expectations()
    return value


def _evaluator() -> dict[str, Any]:
    return deepcopy(_resources()["evaluator"])


def _suite() -> dict[str, Any]:
    value = deepcopy(_resources()["suite"])
    value["manualThresholds"] = [_threshold()]
    return value


@pytest.mark.asyncio
async def test_definition_service_admits_exact_references_and_is_idempotent() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)

    dataset = await service.admit_dataset(_dataset())
    case = await service.admit_case(_case())
    evaluator = await service.admit_evaluator(_evaluator())
    suite = await service.admit_suite(_suite())

    assert dataset.case_ids == (IDS["case"],)
    assert case.expectations is not None
    assert evaluator.kind == "deterministic"
    assert suite.manual_thresholds[0].evaluator.evaluator_id == IDS["evaluator"]
    assert await service.admit_dataset(_dataset()) is dataset
    assert await service.admit_case(_case()) is case


@pytest.mark.asyncio
async def test_in_memory_definition_reuse_and_conflict_are_auditable() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    assert await service.admit_dataset(_dataset())

    conflicting = _dataset()
    conflicting["caseIds"] = ["00000000-0000-0000-0000-000000000009"]
    with pytest.raises(DefinitionConflictError):
        await service.admit_dataset(conflicting)

    assert {entry["outcome"] for entry in repository.definition_audits} >= {
        "accepted",
        "duplicate",
        "rejected",
    }


@pytest.mark.asyncio
async def test_immutable_reuse_conflict_is_fail_closed() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())

    conflicting = _dataset()
    conflicting["caseIds"] = ["00000000-0000-0000-0000-000000000009"]
    with pytest.raises(DefinitionConflictError):
        await service.admit_dataset(conflicting)


@pytest.mark.asyncio
async def test_case_and_suite_cross_record_references_must_exist() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)

    missing_case = _case()
    missing_case["dataset"] = {"id": IDS["dataset"], "version": "9.0.0"}
    with pytest.raises(DefinitionNotFoundError):
        await service.admit_case(missing_case)

    await service.admit_dataset(_dataset())
    missing_suite = _suite()
    missing_suite["evaluators"][0]["evaluatorVersion"] = "9.0.0"
    with pytest.raises(DefinitionNotFoundError):
        await service.admit_suite(missing_suite)

    invalid_component = _suite()
    invalid_component["components"][0]["componentVersion"] = "9.0.0"
    await service.admit_evaluator(_evaluator())
    with pytest.raises(DefinitionNotFoundError):
        await service.admit_suite(invalid_component)


@pytest.mark.asyncio
async def test_threshold_must_bind_to_a_suite_evaluator() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    await service.admit_evaluator(_evaluator())

    suite = _suite()
    suite["manualThresholds"][0]["evaluator"]["evaluatorId"] = (
        "00000000-0000-0000-0000-000000000099"
    )
    with pytest.raises(DefinitionValidationError, match="threshold evaluator"):
        await service.admit_suite(suite)


def test_storage_projection_is_content_free_and_fingerprint_stable() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    definition = CaseDefinition.from_payload(_case())
    storage = definition.to_storage()
    assert "content" not in storage["payload"]
    assert storage["fingerprint"] == definition.canonical_fingerprint()
    assert "owner_identity" not in repr(storage).casefold()
    assert "endpoint" not in repr(storage).casefold()


@pytest.mark.parametrize(
    "field",
    ["prompt", "response", "filename", "endpoint", "credential", "owner_identity"],
)
def test_private_shape_is_rejected_at_the_closed_definition_boundary(field: str) -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    payload = _case()
    payload["expectations"]["exact"]["fields"][0][field] = "forbidden"
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(payload)


class _ArtifactMetadataRepository:
    def __init__(self, metadata: EvaluationArtifactMetadata | None) -> None:
        self.metadata = metadata

    async def lookup_artifact_metadata(self, _reference: str) -> EvaluationArtifactMetadata | None:
        return self.metadata


class _ProviderFailureConnection:
    async def __aenter__(self) -> _ProviderFailureConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError(
            "psycopg.errors.UniqueViolation password=provider-secret endpoint=db.internal"
        )


@pytest.mark.asyncio
async def test_postgres_provider_error_is_sanitized_and_not_counted_as_validation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    metrics = ObservatoryMetrics()
    repository = PostgresEvaluationDefinitionRepository(
        lambda: _ProviderFailureConnection(), metrics=metrics
    )
    from aura_observatory.evaluation_definitions import DatasetDefinition

    definition = DatasetDefinition.from_payload(_dataset())
    with pytest.raises(DefinitionStorageError) as raised:
        await repository.create_dataset(definition)
    assert str(raised.value) == "definition storage failure"
    assert "provider-secret" not in str(raised.value)
    assert "db.internal" not in str(raised.value)
    assert not any(
        "provider-secret" in record.getMessage() or "db.internal" in record.getMessage()
        for record in caplog.records
    )
    assert metrics.values[
        (
            "observatory_evaluation_dependency_outcome_count",
            (("operation", "evaluation_definition_state"), ("outcome", "failure")),
        )
    ] == 1
    assert not any(
        name == "observatory_evaluation_definition_validation_count"
        for (name, _labels), _value in metrics.values.items()
    )
    assert len(metrics.component_spans) == 1
    span = metrics.component_spans[0]
    assert (
        span.component_id,
        span.component_version,
        span.operation,
        span.dependency,
        span.outcome,
        span.error_code,
    ) == (
        "aura.observatory.evaluation_definitions",
        "1.0.0",
        "write",
        "evaluation_definition_state",
        "failure",
        "database_failure",
    )


class _AuthorizationMetadataRepository(_ArtifactMetadataRepository):
    def __init__(
        self,
        metadata: EvaluationArtifactMetadata | None,
        dataset_metadata: EvaluationArtifactMetadata | None = None,
    ) -> None:
        super().__init__(metadata)
        self.dataset_metadata = dataset_metadata if dataset_metadata is not None else metadata
        self._authorization_lookup_count = 0

    async def lookup_authorization_metadata(
        self, _authorization_record_id: str, dataset_id: str
    ) -> EvaluationArtifactMetadata | None:
        del dataset_id
        self._authorization_lookup_count += 1
        return (
            self.dataset_metadata
            if self._authorization_lookup_count == 1
            else self.metadata
        )


def _dataset_for_capture(
    source_kind: str,
    capture_classification: str,
    authorization_purpose: str,
) -> dict[str, Any]:
    value = _dataset()
    value["sourceKind"] = source_kind
    value["captureAuthorization"] = {
        "captureClassification": capture_classification,
        "authorizationPurpose": authorization_purpose,
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
        "authorizationRecordId": IDS["dataset"],
        "authorizedAt": "2026-10-18T12:00:00Z",
    }
    return value


def _owner_supplied_case() -> dict[str, Any]:
    value = _case()
    value["sourceKind"] = "owner_supplied"
    value["captureAuthorization"] = {
        "captureClassification": "full_content",
        "authorizationPurpose": "owner_supplied_evaluation",
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
        "authorizationRecordId": "00000000-0000-0000-0000-000000000001",
        "authorizedAt": "2026-10-18T12:00:00Z",
    }
    value["inputArtifactIds"] = ["00000000-0000-0000-0000-000000000011"]
    value["contentDigest"] = DIGEST
    return value


def _artifact_metadata(**overrides: Any) -> EvaluationArtifactMetadata:
    data: dict[str, Any] = {
        "artifact_reference_id": "00000000-0000-0000-0000-000000000011",
        "dataset_id": IDS["dataset"],
        "capture_classification": "full_content",
        "authorization_record_id": "00000000-0000-0000-0000-000000000001",
        "authorization_purpose": "owner_supplied_evaluation",
        "authorization_context": "controlled_evaluation",
        "retention_mode": "manual_only",
        "sha256": DIGEST,
        "status": "available",
        "revoked": False,
    }
    data.update(overrides)
    return EvaluationArtifactMetadata(**data)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"dataset_id": "00000000-0000-0000-0000-000000000099"},
        {"authorization_record_id": "00000000-0000-0000-0000-000000000099"},
        {"authorization_purpose": "controlled_replay"},
        {"authorization_context": "other"},
        {"retention_mode": "automatic_ttl"},
        {"capture_classification": "hashed"},
        {"sha256": "b" * 64},
        {"status": "deleting"},
        {"revoked": True},
    ],
)
async def test_owner_supplied_artifact_authorization_mismatches_are_rejected(
    override: dict[str, Any],
) -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(
        repository,
        artifact_repository=_AuthorizationMetadataRepository(
            _artifact_metadata(**override),
            _authorization_metadata_for_owner(),
        ),
    )
    await service.admit_dataset(
        _dataset_for_capture("owner_supplied", "full_content", "owner_supplied_evaluation")
    )
    with pytest.raises((DefinitionValidationError, DefinitionNotFoundError)):
        await service.admit_case(_owner_supplied_case())


@pytest.mark.asyncio
async def test_owner_supplied_artifact_requires_an_available_authorized_metadata_row() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    for metadata in (None, _artifact_metadata()):
        repository = InMemoryEvaluationDefinitionRepository()
        repository_client = _AuthorizationMetadataRepository(
            metadata, _authorization_metadata_for_owner()
        )
        service = EvaluationDefinitionService(
            repository,
            artifact_repository=repository_client,
        )
        await service.admit_dataset(
            _dataset_for_capture("owner_supplied", "full_content", "owner_supplied_evaluation")
        )
        if metadata is None:
            with pytest.raises(DefinitionNotFoundError):
                await service.admit_case(_owner_supplied_case())
        else:
            assert await service.admit_case(_owner_supplied_case())


def _captured_digest_case() -> dict[str, Any]:
    value = _case()
    value["sourceKind"] = "captured"
    value["captureAuthorization"] = {
        "captureClassification": "hashed",
        "authorizationPurpose": "controlled_replay",
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
        "authorizationRecordId": "00000000-0000-0000-0000-000000000001",
        "authorizedAt": "2026-10-18T12:00:00Z",
    }
    value["contentDigest"] = DIGEST
    return value


def _authorization_metadata_for_owner() -> EvaluationArtifactMetadata:
    return _authorization_metadata(
        capture_classification="full_content",
        authorization_purpose="owner_supplied_evaluation",
        status="authorized",
        authorized_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    )


def _authorization_metadata(**overrides: Any) -> EvaluationArtifactMetadata:
    data = _artifact_metadata(
        artifact_reference_id="",
        capture_classification="hashed",
        authorization_purpose="controlled_replay",
        status="authorized",
        authorized_at=datetime(2026, 10, 18, 12, 0, tzinfo=UTC),
    )
    values = {**data.__dict__} if hasattr(data, "__dict__") else {
        field: getattr(data, field)
        for field in EvaluationArtifactMetadata.__dataclass_fields__
    }
    values.update(overrides)
    return EvaluationArtifactMetadata(**values)


@pytest.mark.asyncio
async def test_digest_only_capture_uses_authorization_metadata_lookup() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(
        repository,
        artifact_repository=_AuthorizationMetadataRepository(_authorization_metadata()),
    )
    await service.admit_dataset(
        _dataset_for_capture("captured", "hashed", "controlled_replay")
    )
    admitted = await service.admit_case(_captured_digest_case())
    assert admitted.content_digest == DIGEST
    assert not admitted.input_artifact_ids


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "override",
    [
        {"dataset_id": "00000000-0000-0000-0000-000000000099"},
        {"authorization_record_id": "00000000-0000-0000-0000-000000000099"},
        {"status": "revoked", "revoked": True},
        {"status": "pending"},
        {"authorized_at": datetime(2026, 10, 19, 12, 0, tzinfo=UTC)},
    ],
)
async def test_digest_only_capture_rejects_forged_missing_or_revoked_authorization(
    override: dict[str, Any],
) -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(
        repository,
        artifact_repository=_AuthorizationMetadataRepository(
            _authorization_metadata(**override),
            _authorization_metadata(),
        ),
    )
    await service.admit_dataset(
        _dataset_for_capture("captured", "hashed", "controlled_replay")
    )
    with pytest.raises((DefinitionValidationError, DefinitionNotFoundError)):
        await service.admit_case(_captured_digest_case())


@pytest.mark.asyncio
async def test_missing_authorization_metadata_is_rejected_for_non_metadata_capture() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(
        repository,
        artifact_repository=_AuthorizationMetadataRepository(
            None,
            _authorization_metadata(),
        ),
    )
    await service.admit_dataset(
        _dataset_for_capture("captured", "hashed", "controlled_replay")
    )
    with pytest.raises(DefinitionNotFoundError):
        await service.admit_case(_captured_digest_case())


@pytest.mark.asyncio
async def test_owner_supplied_case_requires_an_opaque_artifact_even_with_a_digest() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition

    owner_case = _owner_supplied_case()
    owner_case.pop("inputArtifactIds")
    with pytest.raises(DefinitionValidationError):
        CaseDefinition.from_payload(owner_case)


@pytest.mark.asyncio
async def test_artifact_reference_identity_must_match_the_requested_reference() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(
        repository,
        artifact_repository=_AuthorizationMetadataRepository(
            _artifact_metadata(artifact_reference_id="00000000-0000-0000-0000-000000000099"),
            _authorization_metadata_for_owner(),
        ),
    )
    await service.admit_dataset(
        _dataset_for_capture("owner_supplied", "full_content", "owner_supplied_evaluation")
    )
    with pytest.raises(DefinitionValidationError):
        await service.admit_case(_owner_supplied_case())


@pytest.mark.asyncio
async def test_dataset_case_membership_is_exact_and_suite_requires_complete_versions() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)
    dataset = _dataset()
    second_case_id = "00000000-0000-0000-0000-000000000099"
    dataset["caseIds"] = [IDS["case"], second_case_id]
    await service.admit_dataset(dataset)
    await service.admit_case(_case())
    await service.admit_evaluator(_evaluator())
    with pytest.raises(DefinitionNotFoundError, match="cases"):
        await service.admit_suite(_suite())

    second_case = _case()
    second_case["caseId"] = second_case_id
    await service.admit_case(second_case)
    assert await service.admit_suite(_suite())


@pytest.mark.asyncio
async def test_in_memory_case_id_cannot_switch_to_a_second_version() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    await service.admit_case(_case())
    changed = _case()
    changed["caseVersion"] = "2.0.0"
    with pytest.raises(DefinitionConflictError):
        await service.admit_case(changed)


@pytest.mark.asyncio
async def test_suite_rejects_unregistered_evaluator_and_metric_mismatch() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    await service.admit_case(_case())
    await service.admit_evaluator(_evaluator())

    unknown = _evaluator()
    unknown["evaluatorId"] = "00000000-0000-0000-0000-000000000099"
    unknown["metric"] = "made.up.metric"
    suite = _suite()
    suite["evaluators"][0]["evaluatorId"] = unknown["evaluatorId"]
    suite["manualThresholds"][0]["evaluator"]["evaluatorId"] = unknown["evaluatorId"]
    with pytest.raises(DefinitionValidationError):
        await service.admit_suite(suite)

    await service.admit_evaluator(unknown)
    with pytest.raises(DefinitionValidationError, match="runtime compatible"):
        await service.admit_suite(suite)

    mismatched = _suite()
    mismatched["manualThresholds"][0]["metric"] = "different.metric"
    with pytest.raises(DefinitionValidationError):
        await service.admit_suite(mismatched)


@pytest.mark.asyncio
async def test_suite_rejects_arbitrary_exact_match_uuid() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    await service.admit_case(_case())
    arbitrary = _evaluator()
    arbitrary["evaluatorId"] = "00000000-0000-0000-0000-000000000098"
    await service.admit_evaluator(arbitrary)
    suite = _suite()
    suite["evaluators"][0]["evaluatorId"] = arbitrary["evaluatorId"]
    suite["manualThresholds"][0]["evaluator"]["evaluatorId"] = arbitrary["evaluatorId"]
    with pytest.raises(DefinitionValidationError):
        await service.admit_suite(suite)


@pytest.mark.asyncio
async def test_suite_rejects_local_judge_reference_for_deterministic_definition() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    await service.admit_case(_case())
    await service.admit_evaluator(_evaluator())
    suite = _suite()
    suite["evaluators"][0]["kind"] = "local_judge"
    with pytest.raises(DefinitionValidationError):
        await service.admit_suite(suite)


@pytest.mark.asyncio
async def test_suite_rejects_catalogued_component_outside_evaluator_allowlist() -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.other.component", "1.0.0")
    service = EvaluationDefinitionService(repository)
    await service.admit_dataset(_dataset())
    await service.admit_case(_case())
    await service.admit_evaluator(_evaluator())
    suite = _suite()
    suite["components"][0]["componentId"] = "aura.other.component"
    with pytest.raises(DefinitionValidationError):
        await service.admit_suite(suite)


@pytest.mark.asyncio
async def test_injected_evaluator_compatibility_port_rejects_without_reverse_registry_dependency(
) -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    admitted: list[str] = []

    def reject(definition: Any) -> None:
        admitted.append(definition.evaluator_id)
        raise DefinitionValidationError("injected evaluator compatibility rejection")

    service = EvaluationDefinitionService(repository, evaluator_compatibility=reject)
    await service.admit_dataset(_dataset())
    await service.admit_case(_case())
    await service.admit_evaluator(_evaluator())
    with pytest.raises(DefinitionValidationError, match="runtime compatible"):
        await service.admit_suite(_suite())
    assert admitted == [IDS["evaluator"]]


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["rubricVersion", "calibrationVersion"])
async def test_evaluator_rubric_and_calibration_are_fingerprint_bound(field: str) -> None:
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(repository)
    first = _evaluator()
    first[field] = "revision-one"
    await service.admit_evaluator(first)
    second = _evaluator()
    second[field] = "revision-two"
    with pytest.raises(DefinitionConflictError):
        await service.admit_evaluator(second)


def test_captured_resources_round_trip_with_one_utc_suffix_and_fractional_seconds() -> None:
    from aura_observatory.evaluation_definitions import CaseDefinition, DatasetDefinition

    dataset = _dataset_for_capture("captured", "hashed", "controlled_replay")
    dataset["createdAt"] = "2026-10-19T01:02:03.123456Z"
    parsed_dataset = DatasetDefinition.from_payload(dataset)
    dataset_round_trip = parsed_dataset.to_payload()
    assert dataset_round_trip["createdAt"] == "2026-10-19T01:02:03.123456Z"
    assert DatasetDefinition.from_payload(dataset_round_trip) == parsed_dataset

    captured_case = _owner_supplied_case()
    captured_case["sourceKind"] = "captured"
    captured_case["captureAuthorization"] = {
        "captureClassification": "full_content",
        "authorizationPurpose": "controlled_replay",
        "authorizationContext": "controlled_evaluation",
        "retentionMode": "manual_only",
        "authorizationRecordId": IDS["dataset"],
        "authorizedAt": "2026-10-18T12:00:00Z",
    }
    captured_case["createdAt"] = "2026-10-19T01:02:03.123456Z"
    parsed_case = CaseDefinition.from_payload(captured_case)
    assert parsed_case.to_payload()["createdAt"] == "2026-10-19T01:02:03.123456Z"
