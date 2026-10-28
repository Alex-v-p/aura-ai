# ruff: noqa: E501

from __future__ import annotations

from typing import NoReturn

import pytest
from aura_core.bootstrap.artifact_uow import MemoryGeneratedArtifactObjectStore, artifact_service
from aura_core.domains.artifacts.generated.ports import GeneratedArtifactCleanupUnknown
from aura_core.domains.artifacts.generated.public import (
    GeneratedArtifactConflict,
    GeneratedArtifactLifecycle,
    GeneratedArtifactValidationError,
)
from aura_core.domains.artifacts.generated.repository import MemoryGeneratedArtifactRepository
from aura_core.platform.telemetry import MetadataMetrics


class UnknownDeleteStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key: str, stream: object, *, content_type: str) -> int:
        del content_type
        self.objects[key] = b"".join([piece async for piece in stream])  # type: ignore[union-attr]
        return len(self.objects[key])

    async def put_if_absent(self, key: str, stream: object, *, content_type: str, sha256: str, byte_count: int) -> int:
        del sha256, byte_count
        return await self.put_stream(key, stream, content_type=content_type)

    def get(self, key: str):
        async def stream():
            yield self.objects[key]
        return stream()

    async def delete(self, key: str) -> None:
        del key
        raise GeneratedArtifactCleanupUnknown("ambiguous")


class TransientDeleteStore(UnknownDeleteStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail = True

    async def delete(self, key: str) -> None:
        if self.fail:
            self.fail = False
            raise OSError("transient")
        self.objects.pop(key, None)


@pytest.mark.asyncio
async def test_delete_makes_content_unavailable_before_unknown_cleanup() -> None:
    store = UnknownDeleteStore()
    service = artifact_service(repository=MemoryGeneratedArtifactRepository(), object_store=store)
    item = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="x",
        creator_component="aura.research.coordinator@1",
    )
    admitted = await service.admit_delete("issuer", "subject", item.id, expected_version=1, idempotency_key="delete")
    assert admitted.lifecycle is GeneratedArtifactLifecycle.DELETION_PENDING
    results = await service.resume_pending_cleanup("issuer", "subject")
    deleted = results[0]
    assert deleted.lifecycle is GeneratedArtifactLifecycle.CLEANUP_UNKNOWN
    with pytest.raises(GeneratedArtifactConflict):
        await service.preview("issuer", "subject", item.id)


@pytest.mark.asyncio
async def test_admitted_retry_receipt_is_not_a_settlement_replay() -> None:
    store = TransientDeleteStore()
    service = artifact_service(repository=MemoryGeneratedArtifactRepository(), object_store=store)
    item = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="x",
        creator_component="aura.research.coordinator@1",
    )
    pending = await service.admit_delete("issuer", "subject", item.id, expected_version=1, idempotency_key="delete")
    assert (await service.resume_pending_cleanup("issuer", "subject"))[0].lifecycle is GeneratedArtifactLifecycle.DELETION_FAILED
    retry = await service.admit_retry_cleanup("issuer", "subject", item.id, expected_version=pending.version + 1, idempotency_key="retry")
    assert retry.lifecycle is GeneratedArtifactLifecycle.DELETION_FAILED
    assert (await service.resume_pending_cleanup("issuer", "subject"))[0].lifecycle is GeneratedArtifactLifecycle.DELETED


@pytest.mark.asyncio
async def test_settlement_exception_has_one_observation_set_per_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = MetadataMetrics(capacity=64)
    repository = MemoryGeneratedArtifactRepository()
    service = artifact_service(
        repository=repository, object_store=MemoryGeneratedArtifactObjectStore(), metrics=metrics
    )
    item = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="x",
        creator_component="aura.research.coordinator@1",
    )
    await service.admit_delete(
        "issuer", "subject", item.id, expected_version=item.version,
        idempotency_key="delete",
    )

    async def fail_settlement(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise RuntimeError("settlement failure")

    monkeypatch.setattr(repository, "set_cleanup_state", fail_settlement)
    baseline = len(metrics.measurements)
    with pytest.raises(RuntimeError, match="settlement failure"):
        await service.resume_pending_cleanup("issuer", "subject")

    delta = metrics.measurements[baseline:]
    assert len(delta) == 6
    assert sum(item.metric == "artifact_deletion_outcome_count" for item in delta) == 2
    assert sum(item.kind == "metric" and item.metric == "artifact_deletion_latency_ms" for item in delta) == 2
    assert sum(item.kind == "span" and item.metric == "artifact_deletion_latency_ms" for item in delta) == 2
    assert {dict(item.dimensions)["outcome"] for item in delta} == {"unknown"}
    assert {dict(item.dimensions)["error_class"] for item in delta} == {"settlement_unknown"}


@pytest.mark.asyncio
async def test_admission_and_claim_timeouts_are_timeout_observations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = MetadataMetrics(capacity=64)
    repository = MemoryGeneratedArtifactRepository()
    service = artifact_service(
        repository=repository, object_store=MemoryGeneratedArtifactObjectStore(), metrics=metrics
    )
    item = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="x",
        creator_component="aura.research.coordinator@1",
    )

    async def fail_admission(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise TimeoutError("database timeout")

    monkeypatch.setattr(repository, "begin_delete", fail_admission)
    baseline = len(metrics.measurements)
    with pytest.raises(TimeoutError, match="database timeout"):
        await service.admit_delete(
            "issuer", "subject", item.id, expected_version=item.version,
            idempotency_key="admission-timeout",
        )
    admission_delta = metrics.measurements[baseline:]
    assert len(admission_delta) == 3
    assert {dict(observation.dimensions)["outcome"] for observation in admission_delta} == {"timeout"}
    assert {dict(observation.dimensions)["error_class"] for observation in admission_delta} == {"timeout"}
    assert {dict(observation.dimensions)["dependency"] for observation in admission_delta} == {"core_postgresql"}

    repository = MemoryGeneratedArtifactRepository()
    service = artifact_service(
        repository=repository, object_store=MemoryGeneratedArtifactObjectStore(), metrics=metrics
    )
    item = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="x",
        creator_component="aura.research.coordinator@1",
    )
    await service.admit_delete(
        "issuer", "subject", item.id, expected_version=item.version,
        idempotency_key="claim-timeout",
    )

    async def fail_claim(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise TimeoutError("claim timeout")

    monkeypatch.setattr(repository, "claim_pending_cleanup", fail_claim)
    baseline = len(metrics.measurements)
    with pytest.raises(TimeoutError, match="claim timeout"):
        await service.resume_pending_cleanup("issuer", "subject")
    claim_delta = metrics.measurements[baseline:]
    assert len(claim_delta) == 3
    assert {dict(observation.dimensions)["outcome"] for observation in claim_delta} == {"timeout"}
    assert {dict(observation.dimensions)["error_class"] for observation in claim_delta} == {"timeout"}
    assert {dict(observation.dimensions)["dependency"] for observation in claim_delta} == {"core_postgresql"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("service_method", "repository_method"),
    [
        ("admit_delete", "begin_delete"),
        ("admit_retry_cleanup", "admit_cleanup"),
        ("admit_reconcile_cleanup", "admit_cleanup"),
    ],
)
@pytest.mark.parametrize(
    ("error_type", "expected_outcome", "expected_error"),
    [
        (GeneratedArtifactConflict, "rejected", "conflict"),
        (GeneratedArtifactValidationError, "rejected", "validation_failed"),
        (RuntimeError, "unknown", "settlement_unknown"),
    ],
)
async def test_admission_failures_have_one_typed_production_observation_set(
    monkeypatch: pytest.MonkeyPatch,
    service_method: str,
    repository_method: str,
    error_type: type[Exception],
    expected_outcome: str,
    expected_error: str,
) -> None:
    metrics = MetadataMetrics(capacity=64)
    repository = MemoryGeneratedArtifactRepository()
    service = artifact_service(
        repository=repository, object_store=MemoryGeneratedArtifactObjectStore(), metrics=metrics
    )
    item = await service.create_generated_artifact(
        "issuer", "subject", kind="generated_text", title="x",
        media_type="text/plain; charset=utf-8", content="x",
        creator_component="aura.research.coordinator@1",
    )

    async def fail_admission(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise error_type("admission failure")

    monkeypatch.setattr(repository, repository_method, fail_admission)
    baseline = len(metrics.measurements)
    with pytest.raises(error_type, match="admission failure"):
        await getattr(service, service_method)(
            "issuer", "subject", item.id, expected_version=item.version,
            idempotency_key=f"{service_method}-{expected_error}",
        )

    delta = metrics.measurements[baseline:]
    assert len(delta) == 3
    assert {dict(observation.dimensions)["outcome"] for observation in delta} == {expected_outcome}
    assert {dict(observation.dimensions)["error_class"] for observation in delta} == {expected_error}
    assert {dict(observation.dimensions)["dependency"] for observation in delta} == {"core_postgresql"}
    assert sum(item.kind == "metric" and item.metric == "artifact_deletion_outcome_count" for item in delta) == 1
    assert sum(item.kind == "metric" and item.metric == "artifact_deletion_latency_ms" for item in delta) == 1
    assert sum(item.kind == "span" and item.metric == "artifact_deletion_latency_ms" for item in delta) == 1


@pytest.mark.asyncio
async def test_ambiguous_claim_failure_is_unknown_in_production_sink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = MetadataMetrics(capacity=64)
    repository = MemoryGeneratedArtifactRepository()
    service = artifact_service(
        repository=repository, object_store=MemoryGeneratedArtifactObjectStore(), metrics=metrics
    )

    async def fail_claim(*args: object, **kwargs: object) -> NoReturn:
        del args, kwargs
        raise RuntimeError("claim failure")

    monkeypatch.setattr(repository, "claim_pending_cleanup", fail_claim)
    baseline = len(metrics.measurements)
    with pytest.raises(RuntimeError, match="claim failure"):
        await service.resume_pending_cleanup("issuer", "subject")

    delta = metrics.measurements[baseline:]
    assert len(delta) == 3
    assert {dict(observation.dimensions)["outcome"] for observation in delta} == {"unknown"}
    assert {dict(observation.dimensions)["error_class"] for observation in delta} == {"settlement_unknown"}
    assert {dict(observation.dimensions)["dependency"] for observation in delta} == {"core_postgresql"}
