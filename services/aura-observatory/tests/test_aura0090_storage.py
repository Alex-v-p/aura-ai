"""AURA-0090 storage-boundary and deletion service tests.

The fakes in this module model the durable boundaries, but intentionally keep
artifact bytes in memory only for the duration of a test.  Assertions inspect
bounded outcomes and metadata; they never print or log the synthetic payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
import os
from collections.abc import AsyncIterable, Mapping
from dataclasses import fields, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from aura_observatory.adapters.evaluation_storage import (
    ClickHouseEvaluationScoreStore,
    PostgresEvaluationArtifactRepository,
)
from aura_observatory.adapters.object_storage import (
    GarageEvaluationObjectStore,
    object_key_for_artifact,
)
from aura_observatory.adapters.postgres import PostgresCatalog
from aura_observatory.evaluation_storage import (
    ArtifactRecord,
    ArtifactStorageError,
    ArtifactStorageService,
    ArtifactUpload,
    DeletionCommand,
    DeletionFailure,
    DeletionKind,
    DeletionSelector,
    EvaluationDeletionService,
    EvaluationMetrics,
    EvaluationScore,
)
from aura_observatory.metrics import ObservatoryMetrics


def _id() -> str:
    return str(uuid4())


def _upload(
    payload: bytes = b"synthetic evaluation artifact",
    *,
    capture: str = "full_content",
    max_bytes: int | None = None,
    digest: str | None = None,
    idempotency_key: str | None = None,
    artifact_reference_id: str | None = None,
) -> tuple[ArtifactUpload, bytes]:
    return (
        ArtifactUpload(
            artifact_id=_id(),
            artifact_reference_id=artifact_reference_id or _id(),
            request_id=_id(),
            result_id=_id(),
            idempotency_key=idempotency_key or _id(),
            authorization_record_id=_id(),
            capture_classification=capture,
            media_classification="evaluation_output",
            media_type="application/octet-stream",
            expected_sha256=digest or hashlib.sha256(payload).hexdigest(),
            max_bytes=max_bytes if max_bytes is not None else len(payload),
            job_id=_id(),
            dataset_id=_id(),
            suite_id=_id(),
        ),
        payload,
    )


async def _chunks(payload: bytes, size: int = 3) -> AsyncIterable[bytes]:
    for offset in range(0, len(payload), size):
        yield payload[offset : offset + size]


class MemoryArtifactRepository:
    def __init__(self) -> None:
        self.authorized = True
        self.records_by_key: dict[str, ArtifactRecord] = {}
        self.pending: dict[str, ArtifactUpload] = {}
        self.failures: dict[str, str] = {}
        self.targets: dict[str, list[str]] = {}
        self.completed: dict[str, int] = {}
        self.failed_commands: dict[str, str] = {}
        self.claimed: set[str] = set()
        self.phases: dict[str, str] = {}
        self.lock = asyncio.Lock()

    async def reserve_artifact(self, request: ArtifactUpload) -> ArtifactRecord | None | bool:
        existing = self.records_by_key.get(request.idempotency_key)
        if existing is not None:
            if existing.sha256 != request.expected_sha256:
                raise ArtifactStorageError("idempotency_conflict")
            return existing
        if not self.authorized:
            return False
        self.pending[request.artifact_reference_id] = request
        return None

    async def publish_artifact(self, artifact: ArtifactRecord) -> None:
        self.records_by_key[artifact.idempotency_key] = artifact
        self.pending.pop(artifact.artifact_reference_id, None)

    async def mark_artifact_failed(self, reference: str, failure_code: str) -> None:
        self.failures[reference] = failure_code

    async def claim_deletion_command(self, command_id: str) -> str | None:
        async with self.lock:
            if command_id in self.claimed or command_id in self.completed:
                return None
            self.claimed.add(command_id)
            return command_id

    async def deletion_targets(self, command_id: str, *, limit: int) -> list[dict[str, str]]:
        return [
            {"artifact_reference_id": reference}
            for reference in self.targets.get(command_id, [])[:limit]
        ]

    async def mark_deletion_target(self, command_id: str, reference: str) -> None:
        remaining = self.targets.get(command_id, [])
        if reference in remaining:
            remaining.remove(reference)

    async def mark_deletion_phase(self, command_id: str, phase: str) -> bool:
        self.phases[command_id] = phase
        return True

    async def delete_artifact_metadata(self, reference: str) -> None:
        for key, record in list(self.records_by_key.items()):
            if record.artifact_reference_id == reference:
                del self.records_by_key[key]

    async def complete_deletion_command(self, command_id: str, deleted_count: int) -> None:
        self.completed[command_id] = deleted_count

    async def fail_deletion_command(self, command_id: str, failure_code: str) -> None:
        self.failed_commands[command_id] = failure_code


class GlobalTargetOwnershipRepository(MemoryArtifactRepository):
    """Model the partial unique live-target index across command snapshots."""

    def __init__(self) -> None:
        super().__init__()
        self.live_owner: dict[str, str] = {}
        self.completed_references: set[str] = set()

    async def deletion_targets(self, command_id: str, *, limit: int) -> list[dict[str, str]]:
        owned: list[dict[str, str]] = []
        for reference in self.targets.get(command_id, [])[:limit]:
            if reference in self.completed_references:
                continue
            owner = self.live_owner.setdefault(reference, command_id)
            if owner == command_id:
                owned.append({"artifact_reference_id": reference})
        return owned

    async def mark_deletion_target(self, command_id: str, reference: str) -> None:
        await super().mark_deletion_target(command_id, reference)
        if self.live_owner.get(reference) == command_id:
            self.completed_references.add(reference)


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.fail_reference: str | None = None

    async def put_stream(
        self, reference: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
        self.objects[reference] = b"".join(chunks)
        return len(self.objects[reference])

    async def delete(self, reference: str) -> None:
        if reference == self.fail_reference:
            raise RuntimeError("provider response must not cross the boundary")
        self.deleted.append(reference)
        self.objects.pop(reference, None)


class BlockingUploadObjectStore(MemoryObjectStore):
    """Pause after the first streamed chunk so cancellation is deterministic."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def put_stream(
        self, reference: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        chunks: list[bytes] = []
        async for chunk in stream:
            chunks.append(chunk)
            self.started.set()
            await self.release.wait()
        self.objects[reference] = b"".join(chunks)
        return len(self.objects[reference])


class BlockingDeleteObjectStore(MemoryObjectStore):
    """Pause before deletion so a worker stop cannot look like completion."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def delete(self, reference: str) -> None:
        self.started.set()
        await self.release.wait()
        await super().delete(reference)


class MemoryScoreStore:
    def __init__(self) -> None:
        self.deleted: list[str] = []
        self.available_ids: list[str] = []
        self.deleted_score_ids: list[str] = []

    async def delete_scores_for_artifact(self, reference: str) -> None:
        self.deleted.append(reference)

    async def list_score_ids_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime, limit: int
    ) -> list[str]:
        del selector, snapshot_at
        return self.available_ids[:limit]

    async def delete_scores_for_ids(self, score_ids: list[str]) -> None:
        self.deleted_score_ids.extend(score_ids)
        self.available_ids = [
            score_id for score_id in self.available_ids if score_id not in score_ids
        ]


class ScoreLedgerRepository(MemoryArtifactRepository):
    def __init__(self) -> None:
        super().__init__()
        self.score_targets: dict[str, list[str]] = {}
        self.completed_score_targets: set[tuple[str, str]] = set()

    async def create_deletion_command(
        self, command: DeletionCommand, *, score_ids: tuple[str, ...] = ()
    ) -> DeletionCommand:
        self.score_targets[command.command_id] = list(score_ids)
        return command

    async def deletion_score_targets(self, command_id: str, *, limit: int) -> list[dict[str, str]]:
        return [
            {"score_id": score_id}
            for score_id in self.score_targets.get(command_id, [])
            if (command_id, score_id) not in self.completed_score_targets
        ][:limit]

    async def mark_deletion_score_target(self, command_id: str, score_id: str) -> bool:
        self.completed_score_targets.add((command_id, score_id))
        return True


class RetryableScoreStore(MemoryScoreStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_once = True

    async def delete_scores_for_ids(self, score_ids: list[str]) -> None:
        if self.fail_once:
            self.fail_once = False
            raise BoundedFailure("storage_timeout")
        await super().delete_scores_for_ids(score_ids)


class BlockingScoreStore(MemoryScoreStore):
    """Pause score mutation so lease cleanup is tested on cancellation."""

    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def delete_scores_for_ids(self, score_ids: list[str]) -> None:
        self.started.set()
        await self.release.wait()
        await super().delete_scores_for_ids(score_ids)


class LeaseTrackingScoreRepository(ScoreLedgerRepository):
    def __init__(self) -> None:
        super().__init__()
        self.renewal_calls = 0

    async def renew_deletion_lease(self, command_id: str, lease_token: str) -> bool:
        del command_id, lease_token
        self.renewal_calls += 1
        return True


class BoundedFailure(RuntimeError):
    """Adapter-shaped error whose diagnostic text must not cross the service."""

    def __init__(self, failure_code: str) -> None:
        self.failure_code = failure_code
        super().__init__("private provider diagnostic")


class FailingReserveRepository(MemoryArtifactRepository):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    async def reserve_artifact(self, request: ArtifactUpload) -> ArtifactRecord | None | bool:
        del request
        raise self.error


class FailingPublishRepository(MemoryArtifactRepository):
    async def publish_artifact(self, artifact: ArtifactRecord) -> None:
        del artifact
        raise RuntimeError("private database diagnostic")


class FailingObjectStore(MemoryObjectStore):
    def __init__(self, failure_code: str) -> None:
        super().__init__()
        self.error = BoundedFailure(failure_code)

    async def put_stream(
        self, reference: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del reference, stream, content_type
        raise self.error


class LeaseLostRepository(MemoryArtifactRepository):
    async def renew_deletion_lease(self, command_id: str, lease_token: str) -> bool:
        del command_id, lease_token
        return False


@pytest.mark.asyncio
async def test_authorized_upload_streams_digest_and_publishes_only_metadata() -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, payload = _upload()

    record = await service.store(request, _chunks(payload))

    assert record.artifact_reference_id == request.artifact_reference_id
    assert record.byte_count == len(payload)
    assert record.sha256 == hashlib.sha256(payload).hexdigest()
    assert object_store.objects[request.artifact_reference_id] == payload
    assert not hasattr(record, "content")
    record_values = [getattr(record, field.name) for field in fields(record)]
    assert all("private" not in str(value).lower() for value in record_values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (RuntimeError("private database diagnostic"), "database_failure"),
        (BoundedFailure("storage_timeout"), "storage_timeout"),
    ],
)
async def test_reservation_failures_are_bounded_and_classified(
    error: Exception, expected: str
) -> None:
    request, payload = _upload()
    metrics = EvaluationMetrics()
    service = ArtifactStorageService(
        FailingReserveRepository(error), MemoryObjectStore(), max_bytes=1024, metrics=metrics
    )

    with pytest.raises(ArtifactStorageError) as raised:
        await service.store(request, _chunks(payload))

    assert raised.value.failure_code == expected
    assert "private" not in str(raised.value)
    assert "provider" not in str(raised.value)
    assert any(
        name == "observatory_evaluation_artifact_operation_count"
        and dict(labels).get("outcome") == "failed"
        for (name, labels), _value in metrics.values.items()
    )


@pytest.mark.asyncio
async def test_publish_database_failure_is_bounded_cleans_object_and_persists_code() -> None:
    repository = FailingPublishRepository()
    object_store = MemoryObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, payload = _upload()

    with pytest.raises(ArtifactStorageError) as raised:
        await service.store(request, _chunks(payload))

    assert raised.value.failure_code == "database_failure"
    assert "private" not in str(raised.value)
    assert request.artifact_reference_id not in object_store.objects
    assert repository.failures[request.artifact_reference_id] == "database_failure"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code", ["storage_authentication", "storage_integrity", "storage_timeout"]
)
async def test_provider_upload_failures_remain_bounded_and_persisted(
    failure_code: str,
) -> None:
    repository = MemoryArtifactRepository()
    object_store = FailingObjectStore(failure_code)
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, payload = _upload()

    with pytest.raises(ArtifactStorageError) as raised:
        await service.store(request, _chunks(payload))

    assert raised.value.failure_code == failure_code
    assert "private" not in str(raised.value)
    assert "provider" not in str(raised.value)
    assert repository.failures[request.artifact_reference_id] == failure_code


@pytest.mark.asyncio
async def test_unauthorized_and_metadata_only_uploads_fail_closed_without_object() -> None:
    repository = MemoryArtifactRepository()
    repository.authorized = False
    object_store = MemoryObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, payload = _upload(capture="metadata_only")

    with pytest.raises(ArtifactStorageError) as error:
        await service.store(request, _chunks(payload))

    assert error.value.failure_code == "authorization_required"
    assert object_store.objects == {}
    assert repository.pending == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("payload", "max_bytes", "digest", "expected"),
    [
        (b"too large", 3, None, "artifact_limit_exceeded"),
        (b"digest mismatch", 100, "a" * 64, "integrity_mismatch"),
    ],
)
async def test_stream_limit_and_digest_mismatch_cleanup_visible_state(
    payload: bytes, max_bytes: int, digest: str | None, expected: str
) -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, _ = _upload(payload, max_bytes=max_bytes, digest=digest)

    with pytest.raises(ArtifactStorageError) as error:
        await service.store(request, _chunks(payload))

    assert error.value.failure_code == expected
    assert request.artifact_reference_id not in object_store.objects
    assert repository.failures[request.artifact_reference_id] == expected


@pytest.mark.asyncio
async def test_malformed_stream_is_removed_and_not_published() -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, _ = _upload()

    async def malformed() -> AsyncIterable[bytes]:
        yield b"valid-prefix"
        yield cast_bytes("not bytes")

    with pytest.raises(ArtifactStorageError) as error:
        await service.store(request, malformed())

    assert error.value.failure_code == "malformed_stream"
    assert request.artifact_reference_id not in object_store.objects


def cast_bytes(value: Any) -> bytes:
    """Keep the malformed-stream fixture explicit without leaking its value."""

    return value  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_same_idempotency_key_and_fingerprint_returns_same_record() -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, payload = _upload()

    first = await service.store(request, _chunks(payload))
    second = await service.store(request, _chunks(b"different bytes are not read"))

    assert second == first
    assert object_store.objects[request.artifact_reference_id] == payload


@pytest.mark.asyncio
async def test_cancelled_streamed_upload_is_not_published_and_retries_cleanly() -> None:
    """A cancelled producer leaves no visible partial object or metadata row."""

    repository = MemoryArtifactRepository()
    object_store = BlockingUploadObjectStore()
    service = ArtifactStorageService(repository, object_store, max_bytes=1024)
    request, payload = _upload(payload=b"cancelled upload payload")
    task = asyncio.create_task(service.store(request, _chunks(payload, size=len(payload))))

    await asyncio.wait_for(object_store.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert request.artifact_reference_id not in object_store.objects
    assert repository.records_by_key == {}
    assert request.artifact_reference_id in repository.pending

    # The persisted pending reservation is recoverable by a new worker.  It
    # must not be mistaken for an available artifact on the cancelled path.
    object_store.release.set()
    record = await service.store(request, _chunks(payload))
    assert record.artifact_reference_id == request.artifact_reference_id
    assert repository.records_by_key[request.idempotency_key] == record


def test_object_key_is_canonical_uuid_only_and_never_a_caller_path() -> None:
    reference = "123e4567-e89b-12d3-a456-426614174000"
    assert object_key_for_artifact(reference) == "artifacts/123e4567-e89b-12d3-a456-426614174000"
    for invalid in ("../../secret", "https://example.invalid/object", "not-a-uuid"):
        with pytest.raises(ValueError):
            object_key_for_artifact(invalid)
    assert object_key_for_artifact(reference.upper()).endswith(reference)

    assert object_key_for_artifact(reference).count("/") == 1


def _score(**overrides: Any) -> EvaluationScore:
    values: dict[str, Any] = {
        "score_id": _id(),
        "job_id": _id(),
        "result_id": _id(),
        "dataset_id": _id(),
        "suite_id": _id(),
        "evaluator_id": _id(),
        "evaluator_version": "1.0.0",
        "evaluator_kind": "deterministic",
        "component_id": "aura.core.evaluation",
        "component_version": "1.0.0",
        "capture_classification": "metadata_only",
        "evidence_status": "observed",
        "value": 0.0,
        "outcome": "pass",
        "observed_at": datetime.now(UTC),
    }
    values.update(overrides)
    return EvaluationScore(**values)


@pytest.mark.parametrize(
    "overrides",
    [
        {"value": math.nan},
        {"value": math.inf},
        {"evaluator_kind": "cloud_judge"},
        {"evidence_status": "not_observed", "value": 0.0, "outcome": None},
    ],
)
def test_scores_reject_nonfinite_unknown_or_inconsistent_evidence(
    overrides: dict[str, Any],
) -> None:
    with pytest.raises(ArtifactStorageError):
        _score(**overrides).validate()


def test_zero_score_is_observed_and_missing_evidence_has_no_value_or_outcome() -> None:
    zero = _score(value=0.0, outcome="fail")
    zero.validate()
    missing = _score(evidence_status="not_observed", value=None, outcome=None)
    missing.validate()


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("evaluator_version", "content"),
        ("evaluator_version", "prompt"),
        ("evaluator_version", "1/2"),
        ("evaluator_version", "v" + ("x" * 64)),
        ("component_version", "content"),
        ("component_version", "prompt"),
        ("component_version", "1/2"),
        ("component_version", "v" + ("x" * 64)),
        ("component_id", "content"),
        ("component_id", "prompt"),
        ("component_id", "aura."),
        ("component_id", "aura." + ("x" * 92)),
        ("component_id", "aura/component"),
    ],
)
def test_score_safe_identifiers_reject_content_like_invalid_and_oversized_values(
    field_name: str, value: str
) -> None:
    with pytest.raises(ArtifactStorageError) as error:
        _score(**{field_name: value}).validate()
    assert error.value.failure_code == "score_invalid"


def _command(
    kind: DeletionKind,
    value: str | datetime | None,
    *,
    command_id: str | None = None,
) -> DeletionCommand:
    return DeletionCommand(
        command_id=command_id or _id(),
        idempotency_key=_id(),
        selector=DeletionSelector(kind, value),
        confirmation="DELETE_EVALUATION_ARTIFACTS",
    )


@pytest.mark.parametrize(
    "selector",
    [
        DeletionSelector(DeletionKind.PURGE_ALL, _id()),
        DeletionSelector(DeletionKind.JOB, None),
        DeletionSelector(DeletionKind.DATASET, "not-a-uuid"),
        DeletionSelector(DeletionKind.CUTOFF, datetime(2026, 1, 1)),
        DeletionSelector(
            DeletionKind.CUTOFF, datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2)))
        ),
    ],
)
def test_deletion_selectors_are_exactly_bounded_and_timezone_aware(
    selector: DeletionSelector,
) -> None:
    with pytest.raises(ArtifactStorageError):
        selector.validate()


@pytest.mark.asyncio
async def test_deletion_is_idempotent_for_missing_objects_and_completed_retry() -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    score_store = MemoryScoreStore()
    metrics = EvaluationMetrics()
    reference = _id()
    command = _command(DeletionKind.JOB, _id())
    repository.targets[command.command_id] = [reference]
    service = EvaluationDeletionService(repository, object_store, score_store, metrics=metrics)

    assert await service.execute(command) == 1
    assert reference in object_store.deleted
    assert score_store.deleted == [reference]
    assert repository.completed[command.command_id] == 1
    assert await service.execute(command) == 0
    duration_series = [
        (name, labels)
        for (name, labels), _value in metrics.values.items()
        if name == "observatory_evaluation_deletion_duration_ms"
    ]
    assert len(duration_series) == 1
    assert dict(duration_series[0][1]) == {"outcome": "succeeded", "selector": "job"}


@pytest.mark.asyncio
async def test_lease_loss_is_bounded_not_persisted_as_stale_owner_and_has_duration() -> None:
    repository = LeaseLostRepository()
    command = _command(DeletionKind.PURGE_ALL, None)
    metrics = EvaluationMetrics()
    service = EvaluationDeletionService(
        repository, MemoryObjectStore(), MemoryScoreStore(), metrics=metrics
    )

    with pytest.raises(DeletionFailure) as raised:
        await service.execute(command)

    assert raised.value.failure_code == "lease_lost"
    assert repository.failed_commands == {}
    assert any(
        name == "observatory_evaluation_deletion_failure_count"
        and dict(labels).get("failure_code") == "lease_lost"
        for (name, labels), _value in metrics.values.items()
    )
    assert any(
        name == "observatory_evaluation_deletion_duration_ms"
        and dict(labels) == {"outcome": "failed", "selector": "purge_all"}
        for (name, labels), _value in metrics.values.items()
    )


@pytest.mark.asyncio
async def test_score_target_snapshot_is_immutable_across_late_historical_data() -> None:
    original_score = _id()
    late_score = _id()
    repository = ScoreLedgerRepository()
    score_store = MemoryScoreStore()
    score_store.available_ids = [original_score]
    service = EvaluationDeletionService(repository, MemoryObjectStore(), score_store)
    command = _command(DeletionKind.PURGE_ALL, None)

    await service.create_command(command)
    score_store.available_ids.append(late_score)

    assert await service.execute(command) == 0
    assert score_store.deleted_score_ids == [original_score]
    assert late_score in score_store.available_ids
    assert repository.score_targets[command.command_id] == [original_score]


@pytest.mark.asyncio
async def test_score_target_snapshot_retries_after_failure_without_retargeting() -> None:
    original_score = _id()
    late_score = _id()
    repository = ScoreLedgerRepository()
    score_store = RetryableScoreStore()
    score_store.available_ids = [original_score]
    service = EvaluationDeletionService(repository, MemoryObjectStore(), score_store)
    command = _command(DeletionKind.JOB, _id())
    await service.create_command(command)
    score_store.available_ids.append(late_score)

    with pytest.raises(DeletionFailure) as raised:
        await service.execute(command)
    assert raised.value.failure_code == "storage_timeout"
    assert repository.completed_score_targets == set()

    repository.claimed.remove(command.command_id)
    assert await service.execute(command) == 0
    assert score_store.deleted_score_ids == [original_score]
    assert late_score in score_store.available_ids
    assert repository.completed_score_targets == {(command.command_id, original_score)}


@pytest.mark.asyncio
async def test_concurrent_overlapping_commands_share_claim_without_double_delete() -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    score_store = MemoryScoreStore()
    command_id = _id()
    command = _command(DeletionKind.PURGE_ALL, None, command_id=command_id)
    repository.targets[command_id] = [_id()]
    service = EvaluationDeletionService(repository, object_store, score_store)

    results = await asyncio.gather(service.execute(command), service.execute(command))

    assert sorted(results) == [0, 1]
    assert len(object_store.deleted) == 1
    assert len(score_store.deleted) == 1


@pytest.mark.asyncio
async def test_distinct_overlapping_commands_have_one_live_owner_and_delete_once() -> None:
    repository = GlobalTargetOwnershipRepository()
    object_store = MemoryObjectStore()
    score_store = MemoryScoreStore()
    reference = _id()
    first = _command(DeletionKind.JOB, _id())
    second = _command(DeletionKind.JOB, first.selector.value)
    repository.targets[first.command_id] = [reference]
    repository.targets[second.command_id] = [reference]
    service = EvaluationDeletionService(repository, object_store, score_store)

    results = await asyncio.gather(service.execute(first), service.execute(second))

    assert first.command_id != second.command_id
    assert sorted(results) == [0, 1]
    assert sum(results) == 1
    owner = repository.live_owner[reference]
    assert owner in {first.command_id, second.command_id}
    assert repository.completed[owner] == 1
    loser = second.command_id if owner == first.command_id else first.command_id
    assert repository.completed[loser] == 0
    assert object_store.deleted == [reference]


@pytest.mark.asyncio
async def test_deletion_failure_is_bounded_and_restart_can_resume_pending_target() -> None:
    repository = MemoryArtifactRepository()
    object_store = MemoryObjectStore()
    score_store = MemoryScoreStore()
    reference = _id()
    command = _command(DeletionKind.SUITE, _id())
    repository.targets[command.command_id] = [reference]
    object_store.fail_reference = reference
    service = EvaluationDeletionService(repository, object_store, score_store)

    with pytest.raises(DeletionFailure) as error:
        await service.execute(command)
    assert error.value.failure_code == "storage_failure"
    assert repository.failed_commands[command.command_id] == "storage_failure"
    assert repository.completed == {}

    object_store.fail_reference = None
    repository.claimed.remove(command.command_id)
    assert await service.execute(command) == 1
    assert repository.completed[command.command_id] == 1


@pytest.mark.asyncio
async def test_cancelled_deletion_preserves_phase_targets_and_recovers_after_restart() -> None:
    """A worker stop cannot publish completion or hide a partially deleted target."""

    repository = MemoryArtifactRepository()
    object_store = BlockingDeleteObjectStore()
    score_store = MemoryScoreStore()
    reference = _id()
    object_store.objects[reference] = b"bounded fixture"
    command = _command(DeletionKind.JOB, _id())
    repository.targets[command.command_id] = [reference]
    service = EvaluationDeletionService(repository, object_store, score_store)

    task = asyncio.create_task(service.execute(command))
    await asyncio.wait_for(object_store.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert repository.phases[command.command_id] == "objects"
    assert repository.completed == {}
    assert repository.failed_commands == {}
    assert repository.targets[command.command_id] == [reference]
    assert reference in object_store.objects

    # Model the expired lease observed by a restarted worker.  The immutable
    # target is still the only target, so recovery cannot retarget new data.
    repository.claimed.remove(command.command_id)
    object_store.release.set()
    assert await service.execute(command) == 1
    assert repository.completed[command.command_id] == 1
    assert repository.targets[command.command_id] == []
    assert reference not in object_store.objects


@pytest.mark.asyncio
async def test_cancelled_score_deletion_closes_lease_and_recovers_after_restart() -> None:
    """Cancellation in the score phase must not orphan the renewal task."""

    repository = LeaseTrackingScoreRepository()
    score_store = BlockingScoreStore()
    score_id = _id()
    score_store.available_ids = [score_id]
    command = _command(DeletionKind.JOB, _id())
    await EvaluationDeletionService(repository, MemoryObjectStore(), score_store).create_command(
        command
    )
    service = EvaluationDeletionService(repository, MemoryObjectStore(), score_store)

    task = asyncio.create_task(service.execute(command))
    await asyncio.wait_for(score_store.started.wait(), timeout=1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert repository.completed == {}
    assert repository.score_targets[command.command_id] == [score_id]
    assert not any(
        getattr(active.get_coro(), "__qualname__", "") == "_DeletionLeaseGuard._renew_loop"
        and not active.done()
        for active in asyncio.all_tasks()
    )

    repository.claimed.remove(command.command_id)
    score_store.release.set()
    assert await service.execute(command) == 0
    assert score_store.deleted_score_ids == [score_id]
    assert repository.completed[command.command_id] == 0


def test_provider_endpoint_rejects_credentials_query_and_redirect_prone_configuration() -> None:
    with pytest.raises(ValueError):
        GarageEvaluationObjectStore(
            "http://user:pass@garage.internal:3900",
            "bucket",
            access_key="k",
            secret_key="s",
        )
    with pytest.raises(ValueError):
        GarageEvaluationObjectStore(
            "http://garage.internal:3900/?token=secret",
            "bucket",
            access_key="k",
            secret_key="s",
        )


class FakeResult:
    def __init__(
        self,
        row: Any = None,
        rows: list[Any] | None = None,
        *,
        rowcount: int = 1,
    ) -> None:
        self.row = row
        self.rows = rows if rows is not None else ([] if row is None else [row])
        self.rowcount = rowcount

    async def fetchone(self) -> Any:
        return self.row

    async def fetchall(self) -> list[Any]:
        return self.rows


class FakeConnection:
    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> FakeConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeResult:
        self.calls.append((query, params))
        return self.results.pop(0) if self.results else FakeResult()


class FakeConnectionFactory:
    def __init__(self, results: list[FakeResult] | None = None) -> None:
        self.connection = FakeConnection(results)
        self.called = False

    def __call__(self) -> FakeConnection:
        self.called = True
        return self.connection


class MetricDatabase:
    """Small durable-boundary fake shared by separate adapter instances."""

    def __init__(self) -> None:
        self.rows: dict[tuple[str, str, str, str, str, str], tuple[float, int, dict[str, int]]] = {}
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.upsert_lock = asyncio.Lock()

    def __call__(self) -> MetricConnection:
        return MetricConnection(self)


def _metric_key(values: tuple[Any, ...]) -> tuple[str, str, str, str, str, str]:
    if len(values) != 6:
        raise AssertionError("metric key must contain six fixed columns")
    return cast(tuple[str, str, str, str, str, str], tuple(str(value) for value in values))


class MetricConnection:
    def __init__(self, database: MetricDatabase) -> None:
        self.database = database

    async def __aenter__(self) -> MetricConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> FakeResult:
        self.database.calls.append((query, params))
        normalized = " ".join(query.split())
        if "INSERT INTO observatory_evaluation_metric_series" in normalized:
            name, operation, outcome, selector, phase, failure, value, count, buckets = params
            key = _metric_key((name, operation, outcome, selector, phase, failure))
            incoming_buckets = cast(dict[str, int], json.loads(str(buckets)))
            # Model PostgreSQL's one-statement ON CONFLICT upsert.  The
            # yield makes concurrent first writes exercise the same durable
            # boundary instead of relying on task scheduling luck.
            await asyncio.sleep(0)
            async with self.database.upsert_lock:
                current = self.database.rows.get(key)
                if current is None:
                    self.database.rows[key] = (
                        float(value),
                        int(count),
                        incoming_buckets,
                    )
                else:
                    old_value, old_count, old_buckets = current
                    self.database.rows[key] = (
                        old_value + float(value),
                        old_count + int(count),
                        {
                            bucket: old_buckets.get(bucket, 0) + incoming_buckets.get(bucket, 0)
                            for bucket in set(old_buckets) | set(incoming_buckets)
                        },
                    )
            return FakeResult()
        if "SELECT metric_name, label_operation" in normalized:
            rows = [
                (
                    name,
                    operation,
                    outcome,
                    selector,
                    phase,
                    failure,
                    value,
                    count,
                    buckets,
                )
                for (name, operation, outcome, selector, phase, failure), (
                    value,
                    count,
                    buckets,
                ) in sorted(self.database.rows.items())
            ]
            return FakeResult(rows=rows)
        return FakeResult()


def _artifact_row(request: ArtifactUpload, payload: bytes, status: str) -> dict[str, Any]:
    return {
        "artifact_id": request.artifact_id,
        "artifact_reference_id": request.artifact_reference_id,
        "request_id": request.request_id,
        "result_id": request.result_id,
        "idempotency_key": request.idempotency_key,
        "authorization_record_id": request.authorization_record_id,
        "capture_classification": request.capture_classification,
        "media_classification": request.media_classification,
        "media_type": request.media_type,
        "byte_count": len(payload),
        "sha256": request.expected_sha256,
        "job_id": request.job_id,
        "dataset_id": request.dataset_id,
        "suite_id": request.suite_id,
        "created_at": request.created_at,
        "status": status,
        "declared_max_bytes": request.max_bytes,
    }


@pytest.mark.asyncio
async def test_postgres_existing_row_mapping_returns_exact_artifact_record() -> None:
    request, payload = _upload()
    factory = FakeConnectionFactory([FakeResult(_artifact_row(request, payload, "available"))])
    repository = PostgresEvaluationArtifactRepository(factory)

    result = await repository.reserve_artifact(request)

    assert isinstance(result, ArtifactRecord)
    assert result.artifact_id == request.artifact_id
    assert result.artifact_reference_id == request.artifact_reference_id
    assert result.request_id == request.request_id
    assert result.result_id == request.result_id
    assert result.idempotency_key == request.idempotency_key
    assert result.authorization_record_id == request.authorization_record_id
    assert result.job_id == request.job_id
    assert result.dataset_id == request.dataset_id
    assert result.suite_id == request.suite_id
    assert result.byte_count == len(payload)


@pytest.mark.asyncio
async def test_postgres_artifact_select_uses_alias_qualified_columns() -> None:
    request, _payload = _upload()
    factory = FakeConnectionFactory([FakeResult(None), FakeResult(None)])

    await PostgresEvaluationArtifactRepository(factory).reserve_artifact(request)

    select_sql = factory.connection.calls[0][0].split("FROM", 1)[0]
    for column in (
        "artifact_id",
        "artifact_reference_id",
        "request_id",
        "result_id",
        "idempotency_key",
        "authorization_record_id",
        "capture_classification",
        "media_classification",
        "media_type",
        "byte_count",
        "sha256",
        "job_id",
        "dataset_id",
        "suite_id",
        "created_at",
        "status",
        "declared_max_bytes",
    ):
        assert f"artifact.{column}" in select_sql


@pytest.mark.asyncio
async def test_postgres_retry_state_resets_failed_row_but_rejects_deleting_conflict() -> None:
    request, payload = _upload()
    retry_factory = FakeConnectionFactory(
        [FakeResult(_artifact_row(request, payload, "failed"))]
    )
    retry_result = await PostgresEvaluationArtifactRepository(
        retry_factory
    ).reserve_artifact(request)
    assert retry_result is None
    assert "SET status = 'pending'" in retry_factory.connection.calls[-1][0]

    deleting_factory = FakeConnectionFactory(
        [FakeResult(_artifact_row(request, payload, "deleting"))]
    )
    with pytest.raises(ArtifactStorageError, match="artifact_deleting"):
        await PostgresEvaluationArtifactRepository(deleting_factory).reserve_artifact(request)


@pytest.mark.asyncio
async def test_postgres_fingerprint_conflict_is_rejected_before_retry_update() -> None:
    request, payload = _upload()
    row = _artifact_row(request, payload, "available")
    row["sha256"] = "b" * 64
    factory = FakeConnectionFactory([FakeResult(row)])

    with pytest.raises(ArtifactStorageError, match="idempotency_conflict"):
        await PostgresEvaluationArtifactRepository(factory).reserve_artifact(request)
    assert len(factory.connection.calls) == 1


@pytest.mark.asyncio
async def test_postgres_authorization_requires_matching_scope_and_unrevoked_record() -> None:
    request, _payload = _upload()
    factory = FakeConnectionFactory([FakeResult(None), FakeResult(None)])

    reserved = await PostgresEvaluationArtifactRepository(factory).reserve_artifact(request)

    assert reserved is False
    authorization_query, params = factory.connection.calls[1]
    assert "request_id = %s AND dataset_id = %s" in authorization_query
    assert "revoked_at IS NULL" in authorization_query
    assert params[:3] == (
        request.authorization_record_id,
        request.request_id,
        request.dataset_id,
    )
    assert not any(request.authorization_record_id in str(value) for value in params[3:])


@pytest.mark.asyncio
async def test_postgres_revocation_between_reserve_and_publish_fails_closed() -> None:
    request, payload = _upload()
    reserve_factory = FakeConnectionFactory(
        [FakeResult(None), FakeResult((request.capture_classification,)), FakeResult()]
    )
    repository = PostgresEvaluationArtifactRepository(reserve_factory)
    assert await repository.reserve_artifact(request) is None

    record = ArtifactRecord(
        artifact_id=request.artifact_id,
        artifact_reference_id=request.artifact_reference_id,
        request_id=request.request_id,
        result_id=request.result_id,
        idempotency_key=request.idempotency_key,
        authorization_record_id=request.authorization_record_id,
        capture_classification=request.capture_classification,
        media_classification=request.media_classification,
        media_type=request.media_type,
        byte_count=len(payload),
        sha256=request.expected_sha256,
        job_id=request.job_id,
        dataset_id=request.dataset_id,
        suite_id=request.suite_id,
        created_at=request.created_at,
    )
    revoked_factory = FakeConnectionFactory([FakeResult(None, rowcount=0)])

    with pytest.raises(ArtifactStorageError, match="artifact_state_conflict"):
        await PostgresEvaluationArtifactRepository(revoked_factory).publish_artifact(record)
    publish_query, publish_params = revoked_factory.connection.calls[0]
    assert "revoked_at IS NULL" in publish_query
    assert publish_params[2] == request.artifact_reference_id


@pytest.mark.asyncio
async def test_optional_postgres_authorization_creation_revocation_and_scoped_replay() -> None:
    database_url = os.environ.get("AURA_AURA0086_POSTGRES_URL")
    if not database_url:
        pytest.skip("set AURA_AURA0086_POSTGRES_URL for real PostgreSQL coverage")

    import psycopg

    migration_root = Path(__file__).parents[1] / "migrations/postgresql/versions"
    schema = f"aura0090_{uuid4().hex[:12]}"
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    sync_connection: Any = psycopg.connect(database_url)
    async_connection: Any | None = None
    try:
        sync_connection.execute(f"CREATE SCHEMA {quoted_schema}")
        sync_connection.execute(f"SET search_path TO {quoted_schema}")
        for migration in sorted(migration_root.glob("*.sql")):
            sync_connection.execute(migration.read_text(encoding="utf-8"))
        sync_connection.commit()

        async_connection = await psycopg.AsyncConnection.connect(database_url)
        await async_connection.execute(f"SET search_path TO {quoted_schema}")
        await async_connection.commit()

        class ConnectionContext:
            async def __aenter__(self) -> Any:
                return async_connection

            async def __aexit__(self, exc_type: Any, _exc: Any, _traceback: Any) -> None:
                if exc_type is None:
                    await async_connection.commit()
                else:
                    await async_connection.rollback()

        def connection_factory() -> ConnectionContext:
            return ConnectionContext()

        request, _payload = _upload()
        await async_connection.execute(
            """INSERT INTO observatory_evaluation_authorizations(
            authorization_record_id, request_id, dataset_id, capture_classification,
            authorization_purpose, authorization_context, retention_mode, authorized_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, CURRENT_TIMESTAMP)""",
            (
                request.authorization_record_id,
                request.request_id,
                request.dataset_id,
                request.capture_classification,
                request.authorization_purpose,
                request.authorization_context,
                "manual_only",
            ),
        )
        await async_connection.commit()

        repository = PostgresEvaluationArtifactRepository(connection_factory)
        assert await repository.reserve_artifact(request) is None
        status_row = await async_connection.execute(
            "SELECT status FROM observatory_evaluation_artifacts WHERE artifact_reference_id = %s",
            (request.artifact_reference_id,),
        )
        assert await status_row.fetchone() == ("pending",)

        scoped_replay = replace(
            request,
            request_id=_id(),
            dataset_id=_id(),
            authorization_record_id=_id(),
        )
        assert await repository.reserve_artifact(scoped_replay) is False

        await async_connection.execute(
            "UPDATE observatory_evaluation_authorizations SET revoked_at = CURRENT_TIMESTAMP "
            "WHERE authorization_record_id = %s",
            (request.authorization_record_id,),
        )
        await async_connection.commit()
        assert await repository.reserve_artifact(request) is False
    finally:
        if async_connection is not None:
            await async_connection.close()
        sync_connection.rollback()
        sync_connection.execute("SET search_path TO public")
        sync_connection.execute(f"DROP SCHEMA {quoted_schema} CASCADE")
        sync_connection.commit()
        sync_connection.close()


@pytest.mark.asyncio
async def test_postgres_command_validation_precedes_snapshot_queries() -> None:
    factory = FakeConnectionFactory()
    invalid = DeletionCommand(
        command_id=_id(),
        idempotency_key=_id(),
        selector=DeletionSelector(DeletionKind.PURGE_ALL),
        confirmation="not-the-explicit-confirmation",
    )

    with pytest.raises(ArtifactStorageError, match="deletion_confirmation_required"):
        await PostgresEvaluationArtifactRepository(factory).create_deletion_command(invalid)
    assert not factory.called


@pytest.mark.parametrize(
    "requested_at",
    [
        datetime(2026, 1, 1),
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(hours=2))),
    ],
)
@pytest.mark.asyncio
async def test_postgres_command_rejects_non_utc_requested_at_before_db_call(
    requested_at: datetime,
) -> None:
    factory = FakeConnectionFactory()
    command = replace(_command(DeletionKind.PURGE_ALL, None), requested_at=requested_at)

    with pytest.raises(ArtifactStorageError, match="deletion_requested_at_invalid"):
        await PostgresEvaluationArtifactRepository(factory).create_deletion_command(command)
    assert not factory.called


@pytest.mark.asyncio
async def test_postgres_claim_is_leased_cas_and_stale_commands_are_recoverable() -> None:
    command_id = _id()
    factory = FakeConnectionFactory([FakeResult((command_id,)), FakeResult(None)])
    repository = PostgresEvaluationArtifactRepository(factory)

    lease = await repository.claim_deletion_command(command_id)
    second_lease = await repository.claim_deletion_command(command_id)

    assert lease is not None
    assert UUID(lease).version == 4
    assert second_lease is None
    claim_sql = factory.connection.calls[0][0]
    assert "lease_expires_at < CURRENT_TIMESTAMP" in claim_sql
    assert "RETURNING command_id" in claim_sql

    pending_factory = FakeConnectionFactory(
        [
            FakeResult(
                rows=[
                    (
                        command_id,
                        _id(),
                        "job",
                        _id(),
                        "DELETE_EVALUATION_ARTIFACTS",
                        datetime.now(UTC),
                    )
                ]
            )
        ]
    )
    pending = await PostgresEvaluationArtifactRepository(
        pending_factory
    ).pending_deletion_commands()
    assert len(pending) == 1
    assert "lease_expires_at < CURRENT_TIMESTAMP" in pending_factory.connection.calls[0][0]


@pytest.mark.asyncio
async def test_postgres_failure_and_success_write_content_free_audits_and_resume_state() -> None:
    command_id = _id()
    failure_factory = FakeConnectionFactory()
    repository = PostgresEvaluationArtifactRepository(failure_factory)
    await repository.fail_deletion_command(command_id, "storage_timeout")
    assert "SET status = 'queued'" in failure_factory.connection.calls[0][0]
    assert "'failed'" in failure_factory.connection.calls[1][0]
    assert "failure_code" in failure_factory.connection.calls[1][0]

    success_factory = FakeConnectionFactory()
    success_repository = PostgresEvaluationArtifactRepository(success_factory)
    await success_repository.complete_deletion_command(command_id, 2)
    assert "SET status = 'completed'" in success_factory.connection.calls[0][0]
    assert "'completed'" in success_factory.connection.calls[1][0]
    assert "deleted_count" in success_factory.connection.calls[1][0]
    for query, params in failure_factory.connection.calls + success_factory.connection.calls:
        assert "private-payload" not in query
        assert all("private-payload" not in str(value) for value in params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code", ["storage_authentication", "storage_integrity", "database_failure"]
)
async def test_postgres_artifact_failure_persistence_preserves_bounded_codes(
    failure_code: str,
) -> None:
    factory = FakeConnectionFactory()

    await PostgresEvaluationArtifactRepository(factory).mark_artifact_failed(
        _id(), failure_code
    )

    _query, params = factory.connection.calls[0]
    assert params[0] == failure_code


@pytest.mark.asyncio
async def test_persisted_command_replay_returns_original_id_and_snapshot_time() -> None:
    command = _command(DeletionKind.JOB, _id())
    persisted_id = _id()
    persisted_at = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)
    factory = FakeConnectionFactory(
        [
            FakeResult(
                {
                    "command_id": persisted_id,
                    "idempotency_key": command.idempotency_key,
                    "selector_kind": "job",
                    "selector_value": command.selector.value,
                    "confirmation": command.confirmation,
                    "requested_at": persisted_at,
                }
            )
        ]
    )

    replayed = await PostgresEvaluationArtifactRepository(factory).create_deletion_command(
        command
    )

    assert replayed.command_id == persisted_id
    assert replayed.idempotency_key == command.idempotency_key
    assert replayed.requested_at == persisted_at
    assert len(factory.connection.calls) == 1


@pytest.mark.asyncio
async def test_postgres_command_creation_persists_immutable_score_target_ids() -> None:
    command = _command(DeletionKind.PURGE_ALL, None)
    score_id = _id()
    factory = FakeConnectionFactory()

    await PostgresEvaluationArtifactRepository(factory).create_deletion_command(
        command, score_ids=(score_id,)
    )

    score_insert = next(
        (call for call in factory.connection.calls if "deletion_score_targets" in call[0]),
        None,
    )
    assert score_insert is not None
    assert score_insert[1] == (command.command_id, score_id)


@pytest.mark.asyncio
async def test_lease_renewal_and_stale_worker_fencing_use_compare_and_set() -> None:
    command_id = _id()
    lease_token = _id()
    success_factory = FakeConnectionFactory([FakeResult((command_id,))])
    renewed = await PostgresEvaluationArtifactRepository(
        success_factory
    ).renew_deletion_lease(command_id, lease_token)

    assert renewed is True
    renew_sql, renew_params = success_factory.connection.calls[0]
    assert "lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '60 seconds'" in renew_sql
    assert "AND lease_token = %s" in renew_sql
    assert renew_params == (command_id, lease_token)

    stale_factory = FakeConnectionFactory(
        [FakeResult(None, rowcount=0), FakeResult(None, rowcount=0)]
    )
    stale_repository = PostgresEvaluationArtifactRepository(stale_factory)
    assert await stale_repository.mark_deletion_phase(
        command_id, "objects", lease_token=lease_token
    ) is False
    assert await stale_repository.mark_deletion_target(
        command_id, _id(), lease_token=lease_token
    ) is False
    assert all("lease_token = %s" in query for query, _params in stale_factory.connection.calls)


@pytest.mark.asyncio
async def test_reclaimed_command_rejects_the_previous_worker_token() -> None:
    command_id = _id()
    reclaim_factory = FakeConnectionFactory(
        [FakeResult((command_id,)), FakeResult((command_id,)), FakeResult(None, rowcount=0)]
    )
    repository = PostgresEvaluationArtifactRepository(reclaim_factory)

    previous_token = await repository.claim_deletion_command(command_id)
    reclaimed_token = await repository.claim_deletion_command(command_id)

    assert previous_token is not None
    assert reclaimed_token is not None
    assert previous_token != reclaimed_token
    assert (
        await repository.mark_deletion_phase(
            command_id, "scores", lease_token=previous_token
        )
        is False
    )
    assert "lease_token = %s" in reclaim_factory.connection.calls[-1][0]


@pytest.mark.asyncio
async def test_restart_completion_uses_persisted_target_count_and_writes_audit() -> None:
    command_id = _id()
    factory = FakeConnectionFactory([FakeResult((3,)), FakeResult()])

    completed = await PostgresEvaluationArtifactRepository(factory).complete_deletion_command(
        command_id, deleted_count=1, lease_token=_id()
    )

    assert completed == 3
    completion_sql, completion_params = factory.connection.calls[0]
    assert "SELECT COUNT(*)::BIGINT" in completion_sql
    assert "RETURNING command.deleted_count" in completion_sql
    assert completion_params[0] == command_id
    assert "'completed'" in factory.connection.calls[1][0]


class RecordingHTTPClient:
    calls: list[dict[str, Any]] = []
    status_code = 200
    timeout_error = False

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> RecordingHTTPClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        if self.timeout_error:
            raise httpx.ReadTimeout("bounded provider timeout")
        self.calls.append({"endpoint": endpoint, **kwargs})
        return httpx.Response(
            self.status_code,
            request=httpx.Request("POST", endpoint),
        )


class ScoreListingHTTPClient(RecordingHTTPClient):
    score_ids: list[str] = []
    json_each_row = False

    async def post(self, endpoint: str, **kwargs: Any) -> httpx.Response:
        self.calls.append({"endpoint": endpoint, **kwargs})
        content = kwargs.get("content", "")
        if isinstance(content, str) and content.startswith("SELECT DISTINCT score_id"):
            if self.json_each_row:
                body = "".join(
                    json.dumps({"score_id": score_id}) + "\n" for score_id in self.score_ids
                )
                return httpx.Response(
                    200,
                    content=body.encode(),
                    request=httpx.Request("POST", endpoint),
                )
            return httpx.Response(
                200,
                json={"data": [[score_id] for score_id in self.score_ids]},
                request=httpx.Request("POST", endpoint),
            )
        return httpx.Response(200, request=httpx.Request("POST", endpoint))


class RecordingS3Client:
    requests: list[tuple[str, bytes, dict[str, str]]] = []
    response_content_length: str | None = None

    def __init__(self, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> RecordingS3Client:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def send(self, request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        self.requests.append((request.method, body, dict(request.headers)))
        if request.method == "PUT":
            headers: dict[str, str] = {}
            if self.response_content_length is not None:
                headers["content-length"] = self.response_content_length
            return httpx.Response(
                200,
                headers=headers,
                request=request,
            )
        return httpx.Response(204, request=request)


@pytest.mark.asyncio
@pytest.mark.parametrize("response_content_length", [None, "0", "999999"])
async def test_sigv4_put_counts_chunked_request_bytes_not_response_length(
    monkeypatch: pytest.MonkeyPatch,
    response_content_length: str | None,
) -> None:
    RecordingS3Client.requests = []
    RecordingS3Client.response_content_length = response_content_length
    monkeypatch.setattr(
        "aura_observatory.adapters.object_storage.httpx.AsyncClient", RecordingS3Client
    )
    payload = b"authorized synthetic artifact"
    request, _ = _upload(payload)
    repository = MemoryArtifactRepository()
    store = GarageEvaluationObjectStore(
        "http://garage.internal:3900",
        "observatory-evaluation",
        access_key="observatory-key",
        secret_key="s" * 64,
    )
    service = ArtifactStorageService(repository, store, max_bytes=1024)

    record = await service.store(request, _chunks(payload, size=3))

    put_method, put_body, put_headers = RecordingS3Client.requests[0]
    assert put_method == "PUT"
    assert put_body == payload
    assert record.byte_count == len(payload)
    assert put_headers["x-amz-content-sha256"] == request.expected_sha256
    assert "AWS4-HMAC-SHA256" in put_headers["authorization"]
    assert RecordingS3Client.requests[-1][0] == "PUT"
    assert repository.records_by_key[request.idempotency_key] == record


@pytest.mark.asyncio
@pytest.mark.parametrize("response_content_length", [None, "0", "999999"])
async def test_garage_put_stream_returns_bytes_consumed_from_chunked_upload(
    monkeypatch: pytest.MonkeyPatch,
    response_content_length: str | None,
) -> None:
    RecordingS3Client.requests = []
    RecordingS3Client.response_content_length = response_content_length
    monkeypatch.setattr(
        "aura_observatory.adapters.object_storage.httpx.AsyncClient", RecordingS3Client
    )
    payload = b"chunked synthetic object payload"
    store = GarageEvaluationObjectStore(
        "http://garage.internal:3900",
        "observatory-evaluation",
        access_key="observatory-key",
        secret_key="s" * 64,
    )

    consumed = await store.put_stream(
        _id(),
        _chunks(payload, size=2),
        content_type="application/octet-stream",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )

    assert consumed == len(payload)
    assert RecordingS3Client.requests[0][1] == payload


def test_sigv4_path_style_vector_preserves_garage_uri_and_signed_headers() -> None:
    """Pin the path-style Garage SigV4 vector against accidental re-encoding."""

    store = GarageEvaluationObjectStore(
        "http://aura-garage:3900",
        "aura-observatory-evaluation",
        access_key="GK111111111111111111111111",
        secret_key="2" * 64,
        region="garage",
        clock=lambda: datetime(2026, 10, 18, 12, 34, 56, tzinfo=UTC),
    )
    request = store._signed_request(  # pyright: ignore[reportPrivateUsage]
        "PUT",
        "artifacts/123e4567-e89b-12d3-a456-426614174000",
        headers={"content-type": "application/octet-stream"},
        payload_hash=hashlib.sha256(b"abc").hexdigest(),
    )

    assert str(request.url) == (
        "http://aura-garage:3900/aura-observatory-evaluation/"
        "artifacts/123e4567-e89b-12d3-a456-426614174000"
    )
    assert request.headers["host"] == "aura-garage:3900"
    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"abc").hexdigest()
    assert request.headers["authorization"] == (
        "AWS4-HMAC-SHA256 Credential=GK111111111111111111111111/20261018/garage/"
        "s3/aws4_request, SignedHeaders=content-type;host;x-amz-content-sha256;"
        "x-amz-date, Signature=86aac68479e0d73632c3d9ecd875b1270a32c90a1189547419e69ae6e6dc1cc3"
    )


def test_sigv4_delete_vector_uses_empty_payload_hash_and_same_path_style_scope() -> None:
    store = GarageEvaluationObjectStore(
        "http://aura-garage:3900",
        "aura-observatory-evaluation",
        access_key="GK111111111111111111111111",
        secret_key="2" * 64,
        region="garage",
        clock=lambda: datetime(2026, 10, 18, 12, 34, 56, tzinfo=UTC),
    )
    request = store._signed_request(  # pyright: ignore[reportPrivateUsage]
        "DELETE",
        "artifacts/123e4567-e89b-12d3-a456-426614174000",
        payload_hash=hashlib.sha256(b"").hexdigest(),
    )

    assert request.headers["x-amz-content-sha256"] == hashlib.sha256(b"").hexdigest()
    assert request.headers["host"] == "aura-garage:3900"
    assert request.url.raw_path == (
        b"/aura-observatory-evaluation/artifacts/123e4567-e89b-12d3-a456-426614174000"
    )


@pytest.mark.asyncio
async def test_clickhouse_selector_deletion_covers_nullable_reference_scores_and_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingHTTPClient.calls = []
    RecordingHTTPClient.status_code = 200
    RecordingHTTPClient.timeout_error = False
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        RecordingHTTPClient,
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")
    snapshot = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    await store.delete_scores_for_selector(
        DeletionSelector(DeletionKind.JOB, _id()), snapshot_at=snapshot
    )

    call = RecordingHTTPClient.calls[-1]
    assert call["params"]["mutations_sync"] == "2"
    assert "artifact_reference_id" not in call["content"]
    assert "observed_at <= toDateTime64('2026-10-18 12:00:00.000" in call["content"]
    assert "ALTER TABLE evaluation_scores DELETE WHERE job_id = toUUID" in call["content"]


@pytest.mark.asyncio
async def test_clickhouse_score_id_snapshot_and_deletion_are_bounded_and_synchronous(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score_id = _id()
    ScoreListingHTTPClient.calls = []
    ScoreListingHTTPClient.score_ids = [score_id]
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        ScoreListingHTTPClient,
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")
    snapshot = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    listed = await store.list_score_ids_for_selector(
        DeletionSelector(DeletionKind.PURGE_ALL), snapshot_at=snapshot, limit=100
    )
    await store.delete_scores_for_ids(listed)

    assert listed == [score_id]
    assert ScoreListingHTTPClient.calls[0]["content"].endswith("LIMIT 101 FORMAT JSON")
    delete_call = ScoreListingHTTPClient.calls[1]
    assert delete_call["params"]["mutations_sync"] == "2"
    assert score_id in delete_call["content"]
    assert "artifact_reference_id" not in delete_call["content"]


@pytest.mark.asyncio
async def test_clickhouse_score_id_snapshot_accepts_json_each_row_and_fails_closed_at_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    score_id = _id()
    ScoreListingHTTPClient.calls = []
    ScoreListingHTTPClient.score_ids = [score_id]
    ScoreListingHTTPClient.json_each_row = True
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        ScoreListingHTTPClient,
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")
    snapshot = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    listed = await store.list_score_ids_for_selector(
        DeletionSelector(DeletionKind.PURGE_ALL), snapshot_at=snapshot, limit=1
    )
    assert listed == [score_id]
    assert ScoreListingHTTPClient.calls[0]["content"].endswith("LIMIT 2 FORMAT JSON")

    ScoreListingHTTPClient.score_ids = [_id(), _id()]
    with pytest.raises(ArtifactStorageError, match="target_limit_exceeded"):
        await store.list_score_ids_for_selector(
            DeletionSelector(DeletionKind.PURGE_ALL), snapshot_at=snapshot, limit=1
        )
    ScoreListingHTTPClient.json_each_row = False


@pytest.mark.asyncio
async def test_clickhouse_score_id_snapshot_rejects_non_utc_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ScoreListingHTTPClient.calls = []
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        ScoreListingHTTPClient,
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")

    with pytest.raises(ArtifactStorageError, match="deletion_requested_at_invalid"):
        await store.list_score_ids_for_selector(
            DeletionSelector(DeletionKind.PURGE_ALL),
            snapshot_at=datetime(2026, 10, 18, 12, 0, tzinfo=timezone(timedelta(hours=1))),
            limit=100,
        )
    assert ScoreListingHTTPClient.calls == []


@pytest.mark.asyncio
async def test_clickhouse_cutoff_is_strict_and_mutation_failures_are_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingHTTPClient.calls = []
    RecordingHTTPClient.status_code = 500
    RecordingHTTPClient.timeout_error = False
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        RecordingHTTPClient,
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")
    cutoff = datetime(2026, 10, 18, 12, 0, tzinfo=UTC)

    with pytest.raises(ArtifactStorageError, match="database_failure"):
        await store.delete_scores_for_selector(DeletionSelector(DeletionKind.CUTOFF, cutoff))
    call = RecordingHTTPClient.calls[-1]
    assert call["params"]["mutations_sync"] == "2"
    assert "observed_at < toDateTime64('2026-10-18 12:00:00.000" in call["content"]


@pytest.mark.asyncio
async def test_clickhouse_score_rows_preserve_zero_and_nullable_outcomes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    RecordingHTTPClient.calls = []
    RecordingHTTPClient.status_code = 200
    RecordingHTTPClient.timeout_error = False
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        RecordingHTTPClient,
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")

    await store.insert_score(
        _score(
            value=0.0,
            outcome="fail",
            observed_at=datetime(2026, 10, 18, 12, 0, 0, 123456, tzinfo=UTC),
        )
    )
    await store.insert_score(
        _score(evidence_status="not_observed", value=None, outcome=None)
    )
    await store.insert_score(
        _score(evidence_status="not_applicable", value=None, outcome=None)
    )

    rows = [json.loads(call["content"].split("\n", 1)[1]) for call in RecordingHTTPClient.calls]
    assert rows[0]["score_value"] == 0.0
    assert rows[0]["outcome"] == "fail"
    assert rows[0]["observed_at"] == "2026-10-18 12:00:00.123"
    assert "T" not in rows[0]["observed_at"]
    assert not rows[0]["observed_at"].endswith("Z")
    assert rows[1]["score_value"] is None and rows[1]["outcome"] is None
    assert rows[2]["score_value"] is None and rows[2]["outcome"] is None


@pytest.mark.asyncio
async def test_runtime_metrics_are_emitted_with_bounded_labels_only(
    caplog: pytest.LogCaptureFixture,
) -> None:
    artifact_metrics = ObservatoryMetrics()
    artifact_repository = MemoryArtifactRepository()
    artifact_store = MemoryObjectStore()
    artifact_service = ArtifactStorageService(
        artifact_repository,
        artifact_store,
        max_bytes=1024,
        metrics=artifact_metrics,
    )
    request, payload = _upload()
    await artifact_service.store(request, _chunks(payload))
    artifact_prometheus = artifact_metrics.prometheus()
    assert any(
        metric in artifact_prometheus
        for metric in (
            "observatory_evaluation_artifact_operation_count",
            "observatory_evaluation_artifact_operation_duration_ms",
        )
    )
    assert request.artifact_reference_id not in artifact_prometheus

    deletion_metrics = ObservatoryMetrics()
    deletion_repository = MemoryArtifactRepository()
    reference = _id()
    command = _command(DeletionKind.PURGE_ALL, None)
    deletion_repository.targets[command.command_id] = [reference]
    await EvaluationDeletionService(
        deletion_repository,
        MemoryObjectStore(),
        MemoryScoreStore(),
        metrics=deletion_metrics,
    ).execute(command)
    deletion_prometheus = deletion_metrics.prometheus()
    assert any(
        metric in deletion_prometheus
        for metric in (
            "observatory_evaluation_deletion_action_count",
            "observatory_evaluation_deletion_deleted_count",
        )
    )
    assert reference not in deletion_prometheus

    worker_metrics = ObservatoryMetrics()
    service = EvaluationDeletionService(
        MemoryArtifactRepository(),
        MemoryObjectStore(),
        MemoryScoreStore(),
        metrics=worker_metrics,
    )
    with caplog.at_level(logging.ERROR, logger="aura_observatory.evaluation_storage"):
        service.record_worker_failure("private-payload-token")
    assert "private-payload-token" not in caplog.text
    assert "observatory_evaluation_deletion_failure_count" in worker_metrics.prometheus()


def test_central_metrics_export_and_rehydrated_prometheus_persistence(tmp_path: Any) -> None:
    metrics_path = tmp_path / "observatory-metrics.json"
    metrics = ObservatoryMetrics(metrics_path)
    metrics.record(
        "observatory_evaluation_deletion_failure_count",
        labels={"phase": "worker", "failure_code": "storage_failure"},
    )
    first_prometheus = metrics.prometheus()
    rehydrated = ObservatoryMetrics(metrics_path)
    persisted_prometheus = rehydrated.prometheus()

    assert "observatory_evaluation_deletion_failure_count" in first_prometheus
    assert "observatory_evaluation_deletion_failure_count" in persisted_prometheus
    assert 'failure_code="storage_failure"' in persisted_prometheus
    assert "private-payload" not in persisted_prometheus


@pytest.mark.asyncio
async def test_postgres_metric_aggregation_is_visible_to_a_new_api_process() -> None:
    database = MetricDatabase()
    worker_one = ObservatoryMetrics(persistence=PostgresCatalog(database))
    worker_one.record(
        "observatory_evaluation_artifact_operation_count",
        labels={"operation": "store", "outcome": "succeeded"},
    )
    worker_one.record(
        "observatory_evaluation_artifact_operation_duration_ms",
        12.5,
        labels={"operation": "store"},
    )
    worker_one.record(
        "observatory_evaluation_deletion_duration_ms",
        42.0,
        labels={"selector": "purge_all", "outcome": "succeeded"},
    )
    await worker_one.drain()

    worker_two = ObservatoryMetrics(persistence=PostgresCatalog(database))
    for _ in range(2):
        worker_two.record(
            "observatory_evaluation_artifact_operation_count",
            labels={"operation": "store", "outcome": "succeeded"},
        )
    await worker_two.drain()

    api_catalog = PostgresCatalog(database)
    await api_catalog.record_metric(
        "observatory_evaluation_artifact_operation_count",
        labels={"operation": "store", "artifact_reference_id": "private-content"},
        value=1.0,
    )
    series = await api_catalog.metric_series()
    rendered = ObservatoryMetrics.prometheus_for_series(
        cast(list[Mapping[str, object]], series)
    )

    count_series = next(
        item
        for item in series
        if item["name"] == "observatory_evaluation_artifact_operation_count"
    )
    duration_series = next(
        item
        for item in series
        if item["name"] == "observatory_evaluation_artifact_operation_duration_ms"
    )
    deletion_duration_series = next(
        item
        for item in series
        if item["name"] == "observatory_evaluation_deletion_duration_ms"
    )
    assert count_series["value"] == 3.0
    assert duration_series["value"] == 12.5
    assert duration_series["count"] == 1
    assert deletion_duration_series["value"] == 42.0
    assert deletion_duration_series["count"] == 1
    assert deletion_duration_series["labels"] == {
        "selector": "purge_all",
        "outcome": "succeeded",
    }
    assert 'operation="store",outcome="succeeded"} 3' in rendered
    assert "observatory_evaluation_artifact_operation_duration_ms_sum" in rendered
    assert "observatory_evaluation_deletion_duration_ms_sum" in rendered
    assert "private-content" not in rendered
    assert "private-content" not in json.dumps(series)
    assert all(
        set(cast(dict[str, str], item["labels"]))
        <= {"operation", "outcome", "selector", "phase", "failure_code"}
        for item in series
        if "labels" in item
    )
    metric_queries = [
        query
        for query, _ in database.calls
        if "INSERT INTO observatory_evaluation_metric_series" in query
    ]
    assert metric_queries
    assert all("ON CONFLICT" in query and "DO UPDATE SET" in query for query in metric_queries)
    assert not any("SELECT value, histogram_count" in query for query in metric_queries)
    assert not any(
        "UPDATE observatory_evaluation_metric_series" in query for query in metric_queries
    )


@pytest.mark.asyncio
async def test_concurrent_first_metric_writes_are_atomic_and_visible_after_drain() -> None:
    database = MetricDatabase()
    metrics = ObservatoryMetrics(persistence=PostgresCatalog(database))

    for _ in range(8):
        metrics.record(
            "observatory_evaluation_artifact_operation_count",
            labels={"operation": "store", "outcome": "succeeded"},
        )
    await metrics.drain()

    series = await PostgresCatalog(database).metric_series()
    count = next(
        item
        for item in series
        if item["name"] == "observatory_evaluation_artifact_operation_count"
    )
    assert count["value"] == 8.0
    metric_queries = [
        query
        for query, _ in database.calls
        if "INSERT INTO observatory_evaluation_metric_series" in query
    ]
    assert len(metric_queries) == 8
    assert all("ON CONFLICT" in query and "DO UPDATE SET" in query for query in metric_queries)


@pytest.mark.asyncio
async def test_metric_persistence_failure_is_typed_and_content_free() -> None:
    class PersistenceFailure(RuntimeError):
        failure_code = "storage_timeout"

    class FailingPersistence:
        async def record_metric(
            self,
            _name: str,
            _value: float,
            _labels: Mapping[str, str],
            *,
            histogram: bool,
        ) -> None:
            del histogram
            raise PersistenceFailure("private evaluation content must not escape")

    metrics = ObservatoryMetrics(persistence=FailingPersistence())
    metrics.record(
        "observatory_evaluation_artifact_operation_count",
        labels={"operation": "store", "outcome": "failed"},
    )
    await metrics.drain()

    assert metrics.persistence_failures == 1
    assert metrics.persistence_drops == 0
    assert metrics.persistence_diagnostics == ("failed:storage_timeout",)
    assert "private evaluation content" not in " ".join(metrics.persistence_diagnostics)


@pytest.mark.asyncio
async def test_metric_persistence_queue_is_bounded_and_drops_are_accounted() -> None:
    gate = asyncio.Event()

    class BlockingPersistence:
        async def record_metric(
            self,
            _name: str,
            _value: float,
            _labels: Mapping[str, str],
            *,
            histogram: bool,
        ) -> None:
            del histogram
            await gate.wait()

    metrics = ObservatoryMetrics(persistence=BlockingPersistence())
    for _ in range(257):
        metrics.record(
            "observatory_evaluation_artifact_operation_count",
            labels={"operation": "store", "outcome": "succeeded"},
        )

    assert metrics.persistence_drops == 1
    assert metrics.persistence_diagnostics == ("dropped:queue_full",)
    await metrics.drain(timeout_seconds=0.1)
    assert metrics.persistence_drops == 1
    assert len(metrics.persistence_diagnostics) <= 32


@pytest.mark.asyncio
async def test_metric_shutdown_flush_drains_pending_writes() -> None:
    completed: list[str] = []

    class RecordingPersistence:
        async def record_metric(
            self,
            name: str,
            _value: float,
            _labels: Mapping[str, str],
            *,
            histogram: bool,
        ) -> None:
            del histogram
            completed.append(name)

    metrics = ObservatoryMetrics(persistence=RecordingPersistence())
    metrics.record(
        "observatory_evaluation_artifact_operation_count",
        labels={"operation": "store", "outcome": "succeeded"},
    )
    assert completed == []
    await metrics.flush()

    assert completed == ["observatory_evaluation_artifact_operation_count"]
    assert metrics.persistence_failures == 0
    pending = metrics._persistence_tasks  # pyright: ignore[reportPrivateUsage]
    assert not pending
