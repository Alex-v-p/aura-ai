"""Independent contract, safety, and branch-isolation coverage for AURA-0076."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import os
import re
from collections.abc import AsyncIterable, AsyncIterator, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.domains.artifacts.attachments.evaluation import (
    evaluate_attached_document_grounding_and_citation,
    evaluate_attachment_capture_redaction,
    evaluate_conversation_attachment_branch_isolation,
    evaluate_production_fixture,
    evaluate_vision_capability_and_image_preservation,
)
from aura_core.domains.artifacts.attachments.persistence import (
    AttachmentLifecycleEventRow,
    AttachmentRow,
    AttachmentUploadSessionRow,
)
from aura_core.domains.artifacts.attachments.ports import InspectedImage
from aura_core.domains.artifacts.attachments.public import (
    MAX_IMAGE_BYTES,
    Attachment,
    AttachmentAdmissionError,
    AttachmentConflict,
    AttachmentDuplicate,
    AttachmentError,
    AttachmentLifecycleEvent,
    AttachmentNotFound,
    AttachmentService,
    AttachmentStatus,
    BasicImageInspector,
    ImageValidationError,
)
from aura_core.domains.artifacts.attachments.repository import (
    MemoryAttachmentRepository,
    SqlAttachmentRepository,
)
from aura_core.domains.execution.runs.events import RunEvent, new_event
from aura_core.domains.execution.runs.persistence import RunRow
from aura_core.domains.execution.runs.public import (
    Run,
    RunCoordinator,
    RunRepository,
    message_payload,
    run_payload,
)
from aura_core.domains.interaction.agents.adapters import SqlAgentSeeder, SqlAgentStore
from aura_core.domains.interaction.agents.public import AgentCatalog
from aura_core.domains.interaction.conversations.context import (
    search_attached_documents,
    select_attachment_context,
)
from aura_core.domains.interaction.conversations.dto import (
    AttachmentSummary,
    Conversation,
    ConversationAttachmentReference,
    ConversationBranch,
    ConversationBranchOrigin,
    Message,
    MessageRole,
)
from aura_core.domains.interaction.conversations.persistence import (
    ConversationRow,
    IdempotencyRow,
    MessageAttachmentRow,
    MessageRow,
)
from aura_core.domains.interaction.conversations.repository import (
    SqlConversationRepository,
)
from aura_core.domains.interaction.conversations.store import ConversationStore
from aura_core.domains.knowledge.documents.public import (
    Document,
    DocumentStatus,
)
from aura_core.platform.outbox import (
    ATTACHMENT_EVENT_TOPIC,
    InMemoryOutbox,
    SqlOutboxRepository,
)
from aura_core.platform.outbox.persistence import OutboxRow
from aura_core.platform.outbox.service import RUN_COMMAND_TOPIC, OutboxCommand
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.runtime.models.ports import (
    ChatMessage,
    ModelDescriptor,
    ModelStreamCompleted,
    ModelTextDelta,
    ModelToolCall,
    ProviderTraceContext,
)
from aura_core.runtime.streaming.publisher import EventPublisher, PersistentEventPublisher

ROOT = Path(__file__).parents[3]
OWNER = ("https://issuer.example", "attachment-owner")
FOREIGN = ("https://issuer.example", "other-owner")


class MemoryObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        value = b"".join([piece async for piece in stream])
        self.objects[key] = value
        return len(value)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            if key not in self.objects:
                raise FileNotFoundError(key)
            yield self.objects[key]

        return stream()

    async def delete(self, key: str) -> None:
        self.deleted.append(key)
        self.objects.pop(key, None)


class FailingObjectStore(MemoryObjectStore):
    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del key, stream, content_type
        raise OSError("object store unavailable")


class DeleteFailingObjectStore(MemoryObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_deletes = True

    async def delete(self, key: str) -> None:
        if self.fail_deletes:
            raise OSError("delete unavailable")
        await super().delete(key)


class BlockingObjectStore(MemoryObjectStore):
    """Pause the first original-object write to expose an in-progress row."""

    def __init__(self) -> None:
        super().__init__()
        self.original_started = asyncio.Event()
        self.release_original = asyncio.Event()

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        if key.endswith("/original") and not self.original_started.is_set():
            self.original_started.set()
            await self.release_original.wait()
        return await super().put_stream(key, stream, content_type=content_type)


class StubInspector:
    def __init__(self, *, animated: bool = False, width: int = 4, height: int = 3) -> None:
        self.animated = animated
        self.width = width
        self.height = height

    def inspect(self, data: bytes, *, declared_media_type: str) -> InspectedImage:
        return InspectedImage(
            declared_media_type,
            self.width,
            self.height,
            b"normalized-derivative",
            self.animated,
        )


async def _upload(
    service: AttachmentService,
    *,
    owner: tuple[str, str] = OWNER,
    filename: str = "private.png",
    data: bytes = b"source-bytes",
    idempotency_key: str | None = None,
) -> Attachment:
    return await service.upload(
        *owner,
        filename=filename,
        media_type="image/png",
        stream=data,
        idempotency_key=idempotency_key,
    )


@pytest.mark.asyncio
async def test_image_attachment_is_owner_scoped_idempotent_and_tombstoned() -> None:
    objects = MemoryObjectStore()
    service = AttachmentService(
        MemoryAttachmentRepository(), objects, inspector=StubInspector()
    )

    first = await _upload(service, idempotency_key="upload-1")
    replay = await _upload(service, idempotency_key="upload-1")
    assert replay == first
    with pytest.raises(ImageValidationError, match="idempotency key"):
        await _upload(service, filename="different.png", idempotency_key="upload-1")

    assert await service.content(*OWNER, first.id) == b"source-bytes"
    assert await service.preview(*OWNER, first.id) == b"normalized-derivative"
    with pytest.raises(AttachmentNotFound):
        await service.get(*FOREIGN, first.id)

    deleted = await service.delete(*OWNER, first.id)
    assert deleted.status is AttachmentStatus.UNAVAILABLE
    assert deleted.original_object_key == ""
    assert deleted.derivative_object_key == ""
    assert not objects.objects
    assert await service.delete(*OWNER, first.id) == deleted
    with pytest.raises(AttachmentNotFound):
        await service.preview(*OWNER, first.id)


@pytest.mark.asyncio
async def test_same_owner_duplicate_content_reuses_ready_attachment_without_side_effects() -> None:
    objects = MemoryObjectStore()
    metrics = MetadataMetrics()
    service = AttachmentService(
        MemoryAttachmentRepository(),
        objects,
        inspector=StubInspector(),
        metrics=metrics,
    )

    first = await _upload(
        service,
        filename="first-name.png",
        data=b"same-content",
        idempotency_key=str(uuid4()),
    )
    lifecycle_count = len(service.lifecycle_events)
    object_snapshot = dict(objects.objects)
    second = await _upload(
        service,
        filename="different-name.png",
        data=b"same-content",
        idempotency_key=str(uuid4()),
    )

    assert second == first
    assert len(service.lifecycle_events) == lifecycle_count
    assert objects.objects == object_snapshot
    upload_outcomes = {
        dict(item.dimensions).get("outcome")
        for item in metrics.snapshot()
        if item.metric == "attachment_upload_outcome"
    }
    assert "duplicate" in upload_outcomes
    encoded = json.dumps(
        [
            dict(item.dimensions)
            for item in metrics.snapshot()
            if item.metric == "attachment_upload_outcome"
        ]
    ).casefold()
    assert "first-name.png" not in encoded
    assert "different-name.png" not in encoded
    assert first.digest not in encoded


@pytest.mark.asyncio
async def test_duplicate_completion_replays_existing_attachment_after_service_recreation() -> None:
    repository = MemoryAttachmentRepository()
    objects = MemoryObjectStore()
    first_service = AttachmentService(repository, objects, inspector=StubInspector())
    original = await _upload(
        first_service,
        filename="already-stored.png",
        data=b"replay-content",
        idempotency_key=str(uuid4()),
    )
    lifecycle_count = len(first_service.lifecycle_events)
    object_snapshot = dict(objects.objects)
    upload_key = str(uuid4())
    session = await first_service.create_upload_session(
        *OWNER,
        filename="new-session.png",
        media_type="image/png",
        byte_count=len(b"replay-content"),
        idempotency_key=upload_key,
    )
    completed = await first_service.complete_upload(
        *OWNER,
        cast(UUID, session["id"]),
        stream=b"replay-content",
        idempotency_key=upload_key,
        media_type="image/png",
    )
    assert completed.id == original.id
    assert len(first_service.lifecycle_events) == lifecycle_count
    assert objects.objects == object_snapshot

    recreated = AttachmentService(repository, objects, inspector=StubInspector())
    replay = await recreated.complete_upload(
        *OWNER,
        cast(UUID, session["id"]),
        stream=b"replay-content",
        idempotency_key=upload_key,
        media_type="image/png",
        expected_version=1,
    )
    assert replay == original
    assert recreated.lifecycle_events == []
    assert objects.objects == object_snapshot


@pytest.mark.asyncio
async def test_owner_digest_unique_race_is_translated_to_duplicate_reuse() -> None:
    class UniqueRaceRepository(MemoryAttachmentRepository):
        def __init__(self) -> None:
            super().__init__()
            self.force_race = False
            self._missed_lookup = False

        async def get_by_digest(
            self,
            owner: tuple[str, str],
            digest: str,
            *,
            exclude_attachment_id: UUID | None = None,
        ) -> Attachment | None:
            if self.force_race and not self._missed_lookup:
                self._missed_lookup = True
                return None
            return await super().get_by_digest(
                owner, digest, exclude_attachment_id=exclude_attachment_id
            )

        async def save_with_event(
            self, attachment: Attachment, event: AttachmentLifecycleEvent
        ) -> Attachment:
            if self.force_race and event.status is AttachmentStatus.UPLOADING:
                raise AttachmentDuplicate("duplicate attachment")
            return await super().save_with_event(attachment, event)

    repository = UniqueRaceRepository()
    objects = MemoryObjectStore()
    service = AttachmentService(repository, objects, inspector=StubInspector())
    first = await _upload(service, data=b"unique-race", idempotency_key=str(uuid4()))
    lifecycle_count = len(service.lifecycle_events)
    object_snapshot = dict(objects.objects)
    repository.force_race = True
    second = await _upload(
        service,
        filename="race-loser.png",
        data=b"unique-race",
        idempotency_key=str(uuid4()),
    )
    assert second == first
    assert len(service.lifecycle_events) == lifecycle_count
    assert objects.objects == object_snapshot

@pytest.mark.asyncio
async def test_duplicate_content_is_owner_scoped_and_unavailable_records_are_not_resurrected(
) -> None:
    repository = MemoryAttachmentRepository()
    owner_objects = MemoryObjectStore()
    owner_service = AttachmentService(repository, owner_objects, inspector=StubInspector())
    foreign_service = AttachmentService(
        repository, MemoryObjectStore(), inspector=StubInspector()
    )

    first = await _upload(owner_service, data=b"owner-scoped", idempotency_key=str(uuid4()))
    foreign = await _upload(
        foreign_service,
        owner=FOREIGN,
        data=b"owner-scoped",
        idempotency_key=str(uuid4()),
    )
    assert foreign.id != first.id
    assert foreign.owner == FOREIGN

    deleted = await owner_service.delete(
        *OWNER,
        first.id,
        expected_version=first.version,
        idempotency_key=str(uuid4()),
    )
    assert deleted.status is AttachmentStatus.UNAVAILABLE
    with pytest.raises(AttachmentConflict):
        await _upload(
            owner_service,
            filename="after-delete.png",
            data=b"owner-scoped",
            idempotency_key=str(uuid4()),
        )
    assert await repository.get(OWNER, first.id) == deleted
    assert await repository.get(FOREIGN, foreign.id) == foreign


@pytest.mark.asyncio
async def test_duplicate_content_observes_in_progress_attachment_and_does_not_write_again() -> None:
    repository = MemoryAttachmentRepository()
    objects = BlockingObjectStore()
    service = AttachmentService(repository, objects, inspector=StubInspector())

    first_task = asyncio.create_task(
        _upload(service, filename="in-progress.png", data=b"in-progress", idempotency_key="one")
    )
    await asyncio.wait_for(objects.original_started.wait(), timeout=1)
    second = await _upload(
        service,
        filename="same-bytes-later.png",
        data=b"in-progress",
        idempotency_key="two",
    )
    assert second.id == (await repository.list(OWNER))[0].id
    assert second.status is AttachmentStatus.PROCESSING
    assert len(objects.objects) == 0

    objects.release_original.set()
    first = await asyncio.wait_for(first_task, timeout=1)
    assert first.id == second.id
    assert first.status is AttachmentStatus.READY
    assert len(objects.objects) == 2


@pytest.mark.asyncio
async def test_attachment_storage_failure_is_failed_and_does_not_leak_objects() -> None:
    repository = MemoryAttachmentRepository()
    service = AttachmentService(repository, FailingObjectStore(), inspector=StubInspector())
    attachment_id = uuid4()
    with pytest.raises(AttachmentError, match="storage unavailable"):
        await service.upload(
            *OWNER,
            filename="failed.png",
            media_type="image/png",
            stream=b"source",
            attachment_id=attachment_id,
        )
    failed = await repository.get(OWNER, attachment_id)
    assert failed is not None
    assert failed.status is AttachmentStatus.FAILED
    assert failed.failure_code == "storage"


@pytest.mark.asyncio
async def test_delete_storage_failure_emits_retryable_failure_then_tombstones_on_retry() -> None:
    objects = DeleteFailingObjectStore()
    service = AttachmentService(
        MemoryAttachmentRepository(), objects, inspector=StubInspector()
    )
    item = await _upload(service, idempotency_key=str(uuid4()))
    failed = await service.delete(
        *OWNER, item.id, expected_version=1, idempotency_key=str(uuid4())
    )
    assert failed.status is AttachmentStatus.FAILED
    assert failed.failure_code == "storage_unavailable"
    assert [event.event_type for event in service.lifecycle_events[-2:]] == [
        "attachment.deleting",
        "attachment.failed",
    ]
    objects.fail_deletes = False
    deleted = await service.delete(
        *OWNER, item.id, expected_version=failed.version, idempotency_key=str(uuid4())
    )
    assert deleted.status is AttachmentStatus.UNAVAILABLE
    assert deleted.original_object_key == ""
    with pytest.raises(AttachmentNotFound):
        await service.get(*OWNER, item.id)
    assert await service.list(*OWNER) == []


@pytest.mark.asyncio
async def test_upload_session_replay_expiry_and_delete_version_are_safe_to_retry() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    service = AttachmentService(
        MemoryAttachmentRepository(),
        MemoryObjectStore(),
        inspector=StubInspector(),
        clock=lambda: now,
    )
    key = str(uuid4())
    session = await service.create_upload_session(
        *OWNER,
        filename="replay.png",
        media_type="image/png",
        byte_count=6,
        idempotency_key=key,
    )
    assert await service.create_upload_session(
        *OWNER,
        filename="replay.png",
        media_type="image/png",
        byte_count=6,
        idempotency_key=key,
    ) == session
    attachment = await service.complete_upload(
        *OWNER,
        cast(UUID, session["id"]),
        stream=b"source",
        idempotency_key=key,
        media_type="image/png",
    )
    # A network replay of the same completion must be idempotent, rather than
    # turning a committed upload into a misleading 404.
    replay = await service.complete_upload(
        *OWNER,
        cast(UUID, session["id"]),
        stream=b"source",
        idempotency_key=key,
        media_type="image/png",
    )
    assert replay == attachment

    expired = await service.create_upload_session(
        *OWNER,
        filename="expired.png",
        media_type="image/png",
        byte_count=6,
        idempotency_key=str(uuid4()),
    )
    now = datetime(2026, 1, 1, 0, 16, tzinfo=UTC)
    with pytest.raises(AttachmentNotFound):
        await service.complete_upload(
            *OWNER,
            cast(UUID, expired["id"]),
            stream=b"source",
            idempotency_key=cast(str, expired["idempotency_key"]),
            media_type="image/png",
        )

    delete_key = str(uuid4())
    deleted = await service.delete(
        *OWNER, attachment.id, expected_version=1, idempotency_key=delete_key
    )
    assert await service.delete(
        *OWNER, attachment.id, expected_version=1, idempotency_key=delete_key
    ) == deleted
    with pytest.raises(AttachmentConflict, match="version"):
        await service.delete(
            *OWNER, attachment.id, expected_version=1, idempotency_key=str(uuid4())
        )


@pytest.mark.asyncio
async def test_upload_replay_survives_service_recreation_and_delete_receipt_is_bound() -> None:
    repository = MemoryAttachmentRepository()
    first = AttachmentService(repository, MemoryObjectStore(), inspector=StubInspector())
    key = str(uuid4())
    session = await first.create_upload_session(
        *OWNER,
        filename="restart.png",
        media_type="image/png",
        byte_count=6,
        idempotency_key=key,
    )
    second = AttachmentService(repository, MemoryObjectStore(), inspector=StubInspector())
    assert await second.create_upload_session(
        *OWNER,
        filename="restart.png",
        media_type="image/png",
        byte_count=6,
        idempotency_key=key,
    ) == session
    with pytest.raises(AttachmentConflict, match="idempotency"):
        await second.create_upload_session(
            *OWNER,
            filename="changed.png",
            media_type="image/png",
            byte_count=6,
            idempotency_key=key,
        )
    item = await second.complete_upload(
        *OWNER,
        cast(UUID, session["id"]),
        stream=b"source",
        idempotency_key=key,
        media_type="image/png",
        expected_version=1,
    )
    delete_key = str(uuid4())
    deleted = await second.delete(*OWNER, item.id, expected_version=1, idempotency_key=delete_key)
    third = AttachmentService(repository, MemoryObjectStore(), inspector=StubInspector())
    assert (
        await third.delete(*OWNER, item.id, expected_version=1, idempotency_key=delete_key)
        == deleted
    )
    with pytest.raises(AttachmentConflict, match="idempotency"):
        await third.delete(
            *OWNER, item.id, expected_version=deleted.version, idempotency_key=delete_key
        )


@pytest.mark.asyncio
async def test_regenerate_and_retry_re_admit_deleted_sources_before_new_runs() -> None:
    repository = MemoryAttachmentRepository()
    attachment_service = AttachmentService(
        repository, MemoryObjectStore(), inspector=StubInspector()
    )
    item = await _upload(attachment_service, idempotency_key=str(uuid4()))
    store = ConversationStore(attachment_service=attachment_service)
    model = ModelDescriptor(
        "fake",
        "fake",
        "fake",
        ("chat", "vision"),
        model_revision="revision-1",
        model_digest="a" * 64,
    )
    conversation, _message, run = await store.create(
        *OWNER,
        "inspect this",
        "fake",
        (model,),
        str(uuid4()),
        attachments=(ConversationAttachmentReference("image", item.id),),
    )
    deleted = await attachment_service.delete(
        *OWNER, item.id, expected_version=item.version, idempotency_key=str(uuid4())
    )
    assert deleted.status is AttachmentStatus.UNAVAILABLE
    with pytest.raises(AttachmentAdmissionError):
        await store.regenerate(
            run.id, conversation.version, str(uuid4()), OWNER[1], OWNER[0]
        )
    with pytest.raises(AttachmentAdmissionError):
        await store.retry(run.id, OWNER[1], str(uuid4()), OWNER[0])
    assert len(conversation.runs) == 1


@pytest.mark.asyncio
async def test_attachment_lifecycle_metrics_have_no_rejected_records() -> None:
    metrics = MetadataMetrics()
    service = AttachmentService(
        MemoryAttachmentRepository(),
        MemoryObjectStore(),
        inspector=StubInspector(),
        metrics=metrics,
    )
    item = await _upload(service, idempotency_key=str(uuid4()))
    await service.delete(*OWNER, item.id, expected_version=1, idempotency_key=str(uuid4()))
    assert metrics.stats().rejected == 0
    spans = [record for record in metrics.snapshot() if record.kind == "span"]
    upload = next(
        record
        for record in spans
        if dict(record.trace_attributes).get("operation") == "attachment.upload"
    )
    process = next(
        record
        for record in spans
        if dict(record.trace_attributes).get("operation") == "attachment.process"
    )
    assert upload.trace_id == process.trace_id
    assert process.parent_span_id == upload.span_id
    assert process.value > 0
    assert dict(process.dimensions)["dependency"] == "attachment_store"
    assert dict(process.dimensions)["outcome"] == "ok"
    assert dict(process.trace_attributes)["attachment_id"] == str(item.id)
    prohibited = (
        "bytes",
        "filename",
        "mediatype",
        "bytecount",
        "width",
        "height",
        "exif",
        "prompt",
        "payload",
        "content",
        "private.png",
        "image/png",
    )
    for record in metrics.snapshot():
        encoded = json.dumps(
            {
                "dimensions": dict(record.dimensions),
                "trace_attributes": dict(record.trace_attributes),
            }
        ).casefold()
        assert all(token not in encoded for token in prohibited)
    for event in service.lifecycle_events:
        encoded = json.dumps(event.payload()).casefold()
        assert all(token not in encoded for token in prohibited)


def test_basic_inspector_rejects_mime_spoofing_malformed_and_pixel_bombs() -> None:
    inspector = BasicImageInspector()
    with pytest.raises(ImageValidationError):
        inspector.inspect(b"not-an-image", declared_media_type="image/png")
    with pytest.raises(ImageValidationError):
        inspector.inspect(b"not-an-image", declared_media_type="image/jpeg")

    # A valid PNG signature/header with dimensions above 40 megapixels is
    # rejected before any decoded bytes could be admitted.
    bomb = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\x0dIHDR"
        + (10_000).to_bytes(4, "big")
        + (10_000).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )
    with pytest.raises(ImageValidationError, match="safety limit"):
        inspector.inspect(bomb, declared_media_type="image/png")


@pytest.mark.asyncio
async def test_attachment_safety_rejects_animation_and_oversized_decoded_images() -> None:
    animated = AttachmentService(
        MemoryAttachmentRepository(), MemoryObjectStore(), inspector=StubInspector(animated=True)
    )
    with pytest.raises(ImageValidationError, match="safety inspection"):
        await _upload(animated)

    oversized = AttachmentService(
        MemoryAttachmentRepository(),
        MemoryObjectStore(),
        inspector=StubInspector(width=10_000, height=10_000),
    )
    with pytest.raises(ImageValidationError, match="safety inspection"):
        await _upload(oversized)


@pytest.mark.asyncio
async def test_attachment_upload_enforces_the_25_mb_byte_limit_before_inspection() -> None:
    inspected = False

    class NeverInspect:
        def inspect(self, data: bytes, *, declared_media_type: str) -> InspectedImage:
            nonlocal inspected
            del data, declared_media_type
            inspected = True
            raise AssertionError("oversized bytes must be rejected before decoding")

    async def stream() -> AsyncIterator[bytes]:
        yield b"x" * MAX_IMAGE_BYTES
        yield b"x"

    service = AttachmentService(
        MemoryAttachmentRepository(), MemoryObjectStore(), inspector=NeverInspect()
    )
    with pytest.raises(ImageValidationError, match="25 MB"):
        await service.upload(
            *OWNER,
            filename="oversized.png",
            media_type="image/png",
            stream=stream(),
        )
    assert inspected is False


def test_attachment_lifecycle_event_is_closed_and_content_free() -> None:
    event = AttachmentLifecycleEvent(
        uuid4(), AttachmentStatus.READY, 2, datetime.now(UTC), "attachment.ready"
    )
    payload = event.payload()
    assert set(payload) == {
        "schemaVersion",
        "eventId",
        "eventType",
        "attachmentId",
        "uploadId",
        "status",
        "attachmentVersion",
        "storedObjectCount",
        "occurredAt",
        "correlationId",
        "causationId",
    }
    encoded = json.dumps(payload)
    prohibited = {"filename", "mediaType", "byteCount", "width", "height", "exif"}
    assert not prohibited.intersection(payload)
    assert all(token not in encoded for token in prohibited)


def _summary(
    kind: str, source_id: UUID, *, available: bool = True, order: int = 0
) -> AttachmentSummary:
    return AttachmentSummary(
        uuid5_for_test(kind, source_id, order),
        kind,
        source_id,
        f"{kind}-{order}.bin",
        "image/png" if kind == "image" else "application/pdf",
        10,
        "available" if available else "unavailable",
        10 if kind == "image" else None,
        10 if kind == "image" else None,
    )


def uuid5_for_test(kind: str, source_id: UUID, order: int) -> UUID:
    # Deterministic link IDs make this fixture replayable without persisting
    # any attachment content.
    from uuid import uuid5

    return uuid5(UUID("f6c7b6c0-0fd6-40d0-a518-4c6ce2c02e5b"), f"{kind}:{source_id}:{order}")


def _conversation_with_sibling_attachments() -> tuple[Conversation, UUID, UUID, UUID]:
    conversation_id = uuid4()
    root = ConversationBranch(conversation_id)
    child = ConversationBranch(
        conversation_id,
        parent_branch_id=root.id,
        forked_from_message_id=None,
        origin=ConversationBranchOrigin.EDITED_MESSAGE,
    )
    sibling = ConversationBranch(
        conversation_id,
        parent_branch_id=root.id,
        forked_from_message_id=None,
        origin=ConversationBranchOrigin.EDITED_MESSAGE,
    )
    root_message = Message(
        conversation_id,
        MessageRole.USER,
        "root",
        branch_id=root.id,
        attachments=(_summary("image", uuid4(), order=1), _summary("document", uuid4(), order=1)),
    )
    child_message = Message(
        conversation_id,
        MessageRole.USER,
        "child",
        branch_id=child.id,
        parent_message_id=root_message.id,
        attachments=(_summary("image", uuid4(), order=2), _summary("document", uuid4(), order=2)),
    )
    sibling_message = Message(
        conversation_id,
        MessageRole.USER,
        "sibling",
        branch_id=sibling.id,
        parent_message_id=root_message.id,
        attachments=(_summary("image", uuid4(), order=3),),
    )
    child.forked_from_message_id = root_message.id
    sibling.forked_from_message_id = root_message.id
    conversation = Conversation(
        OWNER[0], OWNER[1], "attachments", uuid4(), uuid4(), "fake",
        messages=[root_message, child_message, sibling_message],
        branches=[root, child, sibling],
        active_branch_id=child.id,
    )
    return conversation, child.id, child_message.id, sibling_message.id


def test_attachment_context_is_current_first_ancestor_only_and_bounded() -> None:
    conversation, branch_id, current_id, sibling_id = _conversation_with_sibling_attachments()
    del sibling_id
    images, documents = select_attachment_context(
        conversation, branch_id, current_message_id=current_id, max_images=4, max_documents=8
    )
    assert images and images[0].source_id == conversation.messages[1].attachments[0].source_id
    assert documents and documents[0].source_id == conversation.messages[1].attachments[1].source_id
    assert all(
        item.source_id != conversation.messages[2].attachments[0].source_id for item in images
    )

    # The function must count unique available sources and never exceed the
    # model-input image bound even when the history contains duplicates.
    current = conversation.messages[1]
    current.attachments = tuple(
        _summary("image", uuid4(), order=index) for index in range(6)
    )
    images, _ = select_attachment_context(conversation, branch_id, current_message_id=current_id)
    assert len(images) == 4


@pytest.mark.asyncio
async def test_attached_document_search_passes_only_authorized_ids() -> None:
    allowed = uuid4()
    foreign = uuid4()
    observed: list[tuple[tuple[UUID, ...], str]] = []

    class SearchService:
        async def search(
            self,
            issuer: str,
            subject: str,
            query: str,
            *,
            limit: int,
            allowed_document_ids: tuple[UUID, ...] | None = None,
            context: ProviderTraceContext | None = None,
        ) -> object:
            del context
            assert (issuer, subject) == OWNER
            assert limit == 8
            observed.append((tuple(allowed_document_ids or ()), query))
            return SimpleNamespace(
                hits=(
                    SimpleNamespace(document_id=foreign),
                    SimpleNamespace(document_id=allowed),
                )
            )

    hits = await search_attached_documents(
        SearchService(), OWNER, "private question", [allowed, allowed]
    )
    typed_hits = cast(Sequence[Any], hits)
    assert [item.document_id for item in typed_hits] == [allowed]
    assert observed == [((allowed,), "private question")]


class ReadyImageService:
    def __init__(self, values: dict[UUID, Attachment]) -> None:
        self.values = values

    async def get(self, issuer: str, subject: str, attachment_id: UUID) -> Attachment:
        if (issuer, subject) != OWNER or attachment_id not in self.values:
            raise AttachmentNotFound
        return self.values[attachment_id]

    async def content(self, issuer: str, subject: str, attachment_id: UUID) -> bytes:
        if (issuer, subject) != OWNER or attachment_id not in self.values:
            raise AttachmentNotFound
        return b"original-image"

    async def preview(self, issuer: str, subject: str, attachment_id: UUID) -> bytes:
        if (issuer, subject) != OWNER or attachment_id not in self.values:
            raise AttachmentNotFound
        return b"normalized-provider-image"


class ReadyDocumentService:
    def __init__(self, values: dict[UUID, Document]) -> None:
        self.values = values

    async def get(self, issuer: str, subject: str, document_id: UUID) -> Document | None:
        if (issuer, subject) != OWNER:
            return None
        return self.values.get(document_id)

    async def search(
        self,
        issuer: str,
        subject: str,
        query: str,
        *,
        limit: int,
        allowed_document_ids: tuple[UUID, ...] | None = None,
        context: ProviderTraceContext | None = None,
    ) -> object:
        del query, context
        if (issuer, subject) != OWNER:
            return SimpleNamespace(hits=())
        ids = tuple(allowed_document_ids or self.values.keys())[:limit]
        return SimpleNamespace(
            hits=tuple(
                SimpleNamespace(
                    document_id=document_id,
                    filename="policy.pdf",
                    excerpt="owner policy evidence",
                    locator="page-1",
                    score=1.0,
                )
                for document_id in ids
            )
        )


def _ready_image() -> Attachment:
    return cast(Attachment, SimpleNamespace(
        status=AttachmentStatus.READY,
        filename="photo.png",
        media_type="image/png",
        byte_count=12,
        width=4,
        height=3,
    ))


def _ready_document() -> Document:
    now = datetime.now(UTC)
    return Document(
        uuid4(), OWNER[0], OWNER[1], "policy.pdf", "application/pdf", 12,
        "a" * 64, "documents/policy", DocumentStatus.READY, 1, now, now,
    )


class CountingImageService(ReadyImageService):
    def __init__(self, values: dict[UUID, Attachment]) -> None:
        super().__init__(values)
        self.preview_calls: list[tuple[str, str, UUID]] = []

    async def preview(self, issuer: str, subject: str, attachment_id: UUID) -> bytes:
        if (issuer, subject) != OWNER or attachment_id not in self.values:
            raise AttachmentNotFound
        self.preview_calls.append((issuer, subject, attachment_id))
        return b"normalized-provider-image"


VISION_DESCRIPTOR = ModelDescriptor(
    "vision-model",
    "Vision model",
    "fake",
    ("chat", "tool_calling", "vision"),
    model_revision="revision-1",
    model_digest="a" * 64,
)


class VisionRunProvider:
    def __init__(self, *, native: bool = False, synthesis: bool = False) -> None:
        self.native = native
        self.synthesis = synthesis
        self.calls: list[tuple[str, list[ChatMessage]]] = []

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (VISION_DESCRIPTOR,)

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[object]:
        del model_id
        self.calls.append(("synthesis" if self.synthesis else "direct", list(messages)))
        yield ModelTextDelta("grounded image answer")
        yield ModelStreamCompleted()

    async def stream_chat_with_tools(
        self, model_id: str, messages: Sequence[ChatMessage], tools: Sequence[object]
    ) -> AsyncIterator[object]:
        del model_id, tools
        self.calls.append(("native", list(messages)))
        if self.synthesis:
            yield ModelToolCall("document.search", {"query": "policy"})
        else:
            yield ModelTextDelta("native image answer")
        yield ModelStreamCompleted()


class DirectVisionRunProvider(VisionRunProvider):
    stream_chat_with_tools = None  # type: ignore[assignment]


class GroundedCitationProvider(VisionRunProvider):
    async def stream_chat_with_tools(
        self, model_id: str, messages: Sequence[ChatMessage], tools: Sequence[object]
    ) -> AsyncIterator[object]:
        del model_id, tools
        self.calls.append(("native", list(messages)))
        evidence = next(
            item.content
            for item in messages
            if item.role == "user" and "BEGIN UNTRUSTED ATTACHED DOCUMENT EVIDENCE" in item.content
        )
        match = re.search(r"evidenceId=([0-9a-f-]{36})", evidence)
        assert match is not None
        yield ModelTextDelta(f"grounded answer [[evidence:{match.group(1)}]]")
        yield ModelStreamCompleted()


class ProductionEvaluatorProvider(VisionRunProvider):
    """Provider fixture that exercises native retrieval and synthesis seams."""

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[object]:
        del model_id
        self.calls.append(("synthesis", list(messages)))
        evidence_ids = [
            match.group(1)
            for item in messages
            if item.role == "user"
            for match in re.finditer(r"evidenceId=([0-9a-f-]{36})", item.content)
        ]
        evidence = evidence_ids[-1] if evidence_ids else None
        suffix = f" [[evidence:{evidence}]]" if evidence else ""
        yield ModelTextDelta(f"synthesized attachment answer{suffix}")
        yield ModelStreamCompleted()


class VerifiedVisionSelection:
    async def list_models(
        self, *, context: ProviderTraceContext | None = None
    ) -> tuple[ModelDescriptor, ...]:
        del context
        return (VISION_DESCRIPTOR,)

    async def refresh_models(
        self, *, context: ProviderTraceContext | None = None
    ) -> tuple[ModelDescriptor, ...]:
        del context
        return (VISION_DESCRIPTOR,)

    async def verify_model(
        self,
        model_id: str,
        capability: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> ModelDescriptor | None:
        del context
        return VISION_DESCRIPTOR if (model_id, capability) == ("vision-model", "vision") else None


async def _run_with_image_provider(
    provider: VisionRunProvider,
    *,
    with_document: bool = False,
) -> tuple[VisionRunProvider, CountingImageService, Run]:
    image_id = uuid4()
    image_service = CountingImageService({image_id: _ready_image()})
    document = _ready_document()
    document_service = ReadyDocumentService({document.id: document})
    store = ConversationStore(
        attachment_service=image_service,
        document_service=document_service,
    )
    store.set_model_selection(VerifiedVisionSelection())
    refs = [ConversationAttachmentReference("image", image_id)]
    if with_document:
        refs.append(ConversationAttachmentReference("document", document.id))
    _conversation, _message, run = await store.create(
        *OWNER,
        "inspect the attached policy image",
        "vision-model",
        (VISION_DESCRIPTOR,),
        str(uuid4()),
        attachments=tuple(refs),
    )
    coordinator = RunCoordinator(
        cast(RunRepository, store),
        EventPublisher(),
        InMemoryOutbox(),
        document_service=document_service,
        attachment_service=image_service,
    )
    await coordinator.execute(run.id, provider)
    _, completed = await store.find_run_any(run.id)
    return provider, image_service, completed


@pytest.mark.asyncio
@pytest.mark.parametrize("native", [False, True])
async def test_run_worker_propagates_images_to_direct_and_native_initial_inference(
    native: bool,
) -> None:
    provider: VisionRunProvider = VisionRunProvider(native=native)
    if not native:
        provider = DirectVisionRunProvider()
    provider, image_service, completed = await _run_with_image_provider(provider)
    completed_run = completed
    assert completed_run.status.value == "completed"
    assert provider.calls
    assert provider.calls[0][0] in {"direct", "native"}
    messages = cast(Sequence[ChatMessage], provider.calls[0][1])
    user = next(
        item
        for item in messages
        if item.role == "user" and item.content == "inspect the attached policy image"
    )
    assert len(user.image_parts) == 1
    assert user.image_parts[0].data == b"normalized-provider-image"
    assert len(image_service.preview_calls) == 1


@pytest.mark.asyncio
async def test_run_worker_reloads_images_for_document_grounding_and_synthesis() -> None:
    provider = VisionRunProvider(native=True, synthesis=True)
    provider, image_service, completed = await _run_with_image_provider(
        provider, with_document=True
    )
    completed_run = completed
    assert completed_run.status.value == "completed"
    assert [kind for kind, _ in provider.calls] == ["native", "synthesis"]
    assert len(image_service.preview_calls) == 2
    for _kind, messages in provider.calls:
        typed_messages = cast(Sequence[ChatMessage], messages)
        user = next(
            item
            for item in typed_messages
            if item.role == "user" and item.content == "inspect the attached policy image"
        )
        assert len(user.image_parts) == 1
        assert user.image_parts[0].data == b"normalized-provider-image"


@pytest.mark.asyncio
async def test_deleted_image_and_document_project_as_tombstones_in_run_and_branch_reads() -> None:
    image_id = uuid4()
    document = _ready_document()
    image_service = CountingImageService({image_id: _ready_image()})
    document_service = ReadyDocumentService({document.id: document})
    store = ConversationStore(
        attachment_service=image_service,
        document_service=document_service,
    )
    store.set_model_selection(VerifiedVisionSelection())
    conversation, message, run = await store.create(
        *OWNER,
        "inspect these files",
        "vision-model",
        (VISION_DESCRIPTOR,),
        str(uuid4()),
        attachments=(
            ConversationAttachmentReference("image", image_id),
            ConversationAttachmentReference("document", document.id),
        ),
    )
    image_service.values.clear()
    document_service.values.clear()

    _projected, projected_run = await store.find_run_any(run.id)
    assert projected_run.id == run.id
    assert all(item.availability == "unavailable" for item in projected_run.attachments)
    assert all(
        item.display_filename == "Attachment unavailable" for item in projected_run.attachments
    )
    assert all(item.media_type == "application/octet-stream" for item in projected_run.attachments)
    assert message.branch_id is not None
    assert run.branch_id is not None
    path = await store.branch_path(conversation.id, message.branch_id, OWNER[1], OWNER[0])
    assert all(item.availability == "unavailable" for item in path[0].attachments)
    branch_runs = await store.branch_runs(
        conversation.id, run.branch_id, OWNER[1], OWNER[0]
    )
    assert all(item.availability == "unavailable" for item in branch_runs[0].attachments)


@pytest.mark.asyncio
async def test_sse_snapshot_replay_strips_durable_attachment_presentation() -> None:
    image_id = uuid4()
    document = _ready_document()
    image_service = CountingImageService({image_id: _ready_image()})
    document_service = ReadyDocumentService({document.id: document})
    store = ConversationStore(
        attachment_service=image_service,
        document_service=document_service,
    )
    store.set_model_selection(VerifiedVisionSelection())
    conversation, _message, run = await store.create(
        *OWNER,
        "inspect these files",
        "vision-model",
        (VISION_DESCRIPTOR,),
        str(uuid4()),
        attachments=(
            ConversationAttachmentReference("image", image_id),
            ConversationAttachmentReference("document", document.id),
        ),
    )
    image_service.values.clear()
    document_service.values.clear()
    _projected, projected_run = await store.find_run_any(run.id)
    fresh = run_payload(projected_run, include_attachments=True)
    assert all(
        item["availability"] == "unavailable"
        and item["displayFilename"] == "Attachment unavailable"
        and item["mediaType"] == "application/octet-stream"
        and item["byteCount"] == 0
        and "width" not in item
        and "height" not in item
        for item in cast(list[dict[str, object]], fresh["attachments"])
    )
    event = new_event(
        "run.snapshot",
        run.id,
        conversation.id,
        0,
        {"run": run_payload(run, include_attachments=True)},
    )
    publisher = EventPublisher()
    await publisher.publish(event)
    durable = (await publisher.history(run.id))[0]
    assert "attachments" not in cast(dict[str, object], durable.data["run"])
    replay_publisher = EventPublisher()
    await replay_publisher.hydrate([event])
    replayed = (await replay_publisher.history(run.id))[0]
    assert "attachments" not in cast(dict[str, object], replayed.data["run"])


@pytest.mark.asyncio
async def test_persistent_publisher_sanitizes_before_persistence_and_replay() -> None:
    run_id = uuid4()
    conversation_id = uuid4()
    event = new_event(
        "run.snapshot",
        run_id,
        conversation_id,
        0,
        {
            "run": {
                "id": str(run_id),
                "status": "completed",
                "attachments": [
                    {
                        "linkId": str(uuid4()),
                        "kind": "image",
                        "sourceId": str(uuid4()),
                        "displayFilename": "private.png",
                        "mediaType": "image/png",
                        "byteCount": 12,
                    }
                ],
            }
        },
    )
    persisted: list[RunEvent] = []

    async def save(value: RunEvent) -> RunEvent:
        persisted.append(value)
        return value

    publisher = PersistentEventPublisher(save)
    await publisher.publish(event)
    assert persisted
    assert "attachments" not in cast(dict[str, object], persisted[0].data["run"])
    replayed = (await publisher.history(run_id))[0]
    assert "attachments" not in cast(dict[str, object], replayed.data["run"])


@pytest.mark.asyncio
async def test_sql_store_projects_deleted_attachments_for_branch_run_and_sse() -> None:
    image_id = uuid4()
    document = _ready_document()
    image_service = CountingImageService({image_id: _ready_image()})
    document_service = ReadyDocumentService({document.id: document})
    memory_store = ConversationStore(
        attachment_service=image_service,
        document_service=document_service,
    )
    memory_store.set_model_selection(VerifiedVisionSelection())
    conversation, message, run = await memory_store.create(
        *OWNER,
        "inspect these files",
        "vision-model",
        (VISION_DESCRIPTOR,),
        str(uuid4()),
        attachments=(
            ConversationAttachmentReference("image", image_id),
            ConversationAttachmentReference("document", document.id),
        ),
    )
    image_service.values.clear()
    document_service.values.clear()

    class SessionScope:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *_: object) -> None:
            return None

    sql_store = SqlConversationStore(
        cast(Any, lambda: SessionScope()),
        attachment_service=image_service,
        document_service=document_service,
    )
    sql_store._row = AsyncMock(return_value=object())  # type: ignore[method-assign]
    sql_store._load = AsyncMock(return_value=conversation)  # type: ignore[method-assign]
    sql_store.runs.get = AsyncMock(return_value=run)  # type: ignore[method-assign]
    sql_store.conversations.get = AsyncMock(return_value=object())  # type: ignore[method-assign]

    detail = await sql_store.get(conversation.id, OWNER[1], OWNER[0])
    path = await sql_store.branch_path(
        conversation.id, cast(UUID, message.branch_id), OWNER[1], OWNER[0]
    )
    branch_runs = await sql_store.branch_runs(
        conversation.id, cast(UUID, run.branch_id), OWNER[1], OWNER[0]
    )
    found, found_run = await sql_store.find_run(run.id, OWNER[1], OWNER[0])
    found_any, found_any_run = await sql_store.find_run_any(run.id)
    for item in (*path[0].attachments, *branch_runs[0].attachments, *found_run.attachments):
        assert item.availability == "unavailable"
        assert item.display_filename == "Attachment unavailable"
        assert item.media_type == "application/octet-stream"
        assert item.byte_count == 0
        assert item.width is None and item.height is None
    for item in (*detail.messages[0].attachments, *detail.runs[0].attachments):
        assert item.availability == "unavailable"
        assert item.display_filename == "Attachment unavailable"
        assert item.media_type == "application/octet-stream"
        assert item.byte_count == 0
        assert item.width is None and item.height is None
    assert found.id == found_any.id == conversation.id
    assert found_any_run.attachments == found_run.attachments
    fresh = run_payload(found_any_run, include_attachments=True)
    assert all(
        item["availability"] == "unavailable"
        for item in cast(list[dict[str, object]], fresh["attachments"])
    )


@pytest.mark.asyncio
async def test_attached_documents_are_searched_before_inference_and_citations_are_same_run(
) -> None:
    provider = GroundedCitationProvider(native=True)
    provider, _image_service, completed = await _run_with_image_provider(
        provider, with_document=True
    )
    completed_run = completed
    assert completed_run.status.value == "completed"
    assert completed_run.document_search_used is True
    assert len(completed_run.citations) == 1
    assert completed_run.citations[0].locator == "page-1"
    initial_messages = provider.calls[0][1]
    evidence_messages = [
        item
        for item in initial_messages
        if item.role == "user" and "BEGIN UNTRUSTED ATTACHED DOCUMENT EVIDENCE" in item.content
    ]
    assert len(evidence_messages) == 1
    assert "owner policy evidence" in evidence_messages[0].content


@pytest.mark.asyncio
async def test_admission_is_atomic_and_branch_edit_and_regeneration_sets_are_explicit() -> None:
    image_id = uuid4()
    document = _ready_document()
    store = ConversationStore(
        attachment_service=ReadyImageService({image_id: _ready_image()}),
        document_service=ReadyDocumentService({document.id: document}),
    )
    models = (
        ModelDescriptor(
            "fake",
            "fake",
            "fake",
            ("chat", "vision"),
            model_revision="revision-1",
            model_digest="a" * 64,
        ),
    )
    class VerifiedVision:
        async def list_models(
            self, *, context: ProviderTraceContext | None = None
        ) -> Sequence[ModelDescriptor]:
            del context
            return models

        async def refresh_models(
            self, *, context: ProviderTraceContext | None = None
        ) -> Sequence[ModelDescriptor]:
            del context
            return models

        async def verify_model(
            self,
            model_id: str,
            capability: str,
            *,
            context: ProviderTraceContext | None = None,
        ) -> ModelDescriptor | None:
            del context
            assert (model_id, capability) == ("fake", "vision")
            return models[0]

    store.set_model_selection(VerifiedVision())
    refs = (
        ConversationAttachmentReference("image", image_id),
        ConversationAttachmentReference("document", document.id),
    )
    conversation, source, run = await store.create(
        *OWNER,
        "inspect these",
        "fake",
        models,
        "create-1",
        attachments=refs,
    )
    assert source.attachments == run.attachments
    assert {item.kind for item in source.attachments} == {"image", "document"}

    # A bad resource fails before the conversation is inserted.
    fresh = ConversationStore(
        attachment_service=ReadyImageService({image_id: _ready_image()}),
        document_service=ReadyDocumentService({}),
    )
    with pytest.raises(AttachmentAdmissionError):
        await fresh.create(
            *OWNER,
            "must not persist",
            "fake",
            models,
            "create-2",
            attachments=refs,
        )
    assert not fresh._conversations  # pyright: ignore[reportPrivateUsage] -- atomicity assertion

    branch_result = await store.create_branch(
        conversation.id,
        source.id,
        "edited",
        conversation.version,
        "branch-1",
        OWNER[1],
        OWNER[0],
        attachments=(),
    )
    assert branch_result[1].attachments == ()
    regenerated = await store.regenerate(
        run.id, branch_result[0].version, "regen-1", OWNER[1], OWNER[0]
    )
    assert regenerated[2].attachments == source.attachments


@pytest.mark.asyncio
async def test_attachment_count_limits_reject_duplicates_and_too_many_images() -> None:
    ids = [uuid4() for _ in range(9)]
    service = ReadyImageService({identifier: _ready_image() for identifier in ids})
    store = ConversationStore(attachment_service=service)
    models = (
        ModelDescriptor(
            "fake",
            "fake",
            "fake",
            ("chat", "vision"),
            model_revision="revision-1",
            model_digest="a" * 64,
        ),
    )
    with pytest.raises(AttachmentAdmissionError, match="attachment limit"):
        await store.create(
            *OWNER,
            "too many",
            "fake",
            models,
            "limit-1",
            attachments=tuple(
                ConversationAttachmentReference("image", identifier) for identifier in ids
            ),
        )
    with pytest.raises(AttachmentAdmissionError, match="image attachment limit"):
        await store.create(
            *OWNER,
            "too many images",
            "fake",
            models,
            "limit-2",
            attachments=tuple(
                ConversationAttachmentReference("image", identifier) for identifier in ids[:5]
            ),
        )
    with pytest.raises(AttachmentAdmissionError, match="duplicate"):
        await store.create(
            *OWNER,
            "duplicate",
            "fake",
            models,
            "limit-3",
            attachments=(
                ConversationAttachmentReference("image", ids[0]),
                ConversationAttachmentReference("image", ids[0]),
            ),
        )


def test_historical_message_rows_and_migration_are_additive() -> None:
    message_id = uuid4()
    row = SimpleNamespace(
        id=message_id,
        conversation_id=uuid4(),
        role="user",
        content="historical",
        state="complete",
        run_id=None,
        created_at=None,
        updated_at=None,
        citations=None,
        branch_id=None,
        parent_message_id=None,
        attachments=None,
    )
    message = SqlConversationRepository._message(row)  # type: ignore[arg-type]
    assert message.id == message_id
    assert message.attachments == ()

    migration_path = (
        ROOT / "services/aura-core/migrations/versions/0022_conversation_attachments.py"
    )
    spec = importlib.util.spec_from_file_location("aura_migration_0022", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0022_conversation_attachments"
    assert module.down_revision == "0021_conversation_branching"
    source = migration_path.read_text()
    assert "CREATE TABLE IF NOT EXISTS artifact_attachments" in source
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS attachments" in source
    assert "REFERENCES documents" not in source
    assert "REFERENCES artifact_attachments" not in source


def test_branch_message_hydration_falls_back_to_normalized_attachment_links() -> None:
    link = SimpleNamespace(
        id=uuid4(),
        kind="image",
        source_id=uuid4(),
        display_filename="branch-image.png",
        media_type="image/png",
        byte_count=256,
        availability="available",
        width=32,
        height=16,
    )
    row = SimpleNamespace(
        id=uuid4(),
        conversation_id=uuid4(),
        role="user",
        content="edited branch",
        state="complete",
        run_id=uuid4(),
        created_at=None,
        updated_at=None,
        citations=None,
        branch_id=uuid4(),
        parent_message_id=None,
        # Some branch rows can have durable normalized links even when the
        # additive JSON snapshot is absent; reload must retain the cards.
        attachments=None,
        attachment_links=[link],
    )

    message = SqlConversationRepository._message(row)  # type: ignore[arg-type]
    payload = message_payload(message)

    assert message.attachments[0].source_id == link.source_id
    attachments = cast(list[dict[str, object]], payload["attachments"])
    assert attachments[0]["displayFilename"] == "branch-image.png"


def test_attachment_sql_links_are_unique_and_upload_sessions_are_durable() -> None:
    attachment_table: Any = AttachmentRow.__table__
    attachment_constraints = {
        constraint.name
        for constraint in attachment_table.constraints
        if constraint.name is not None
    }
    assert "uq_attachment_owner_digest" in attachment_constraints
    upload_table: Any = AttachmentUploadSessionRow.__table__
    upload_constraints = {
        constraint.name
        for constraint in upload_table.constraints
        if constraint.name is not None
    }
    assert "uq_attachment_upload_owner_key" in upload_constraints
    assert {"expires_at", "idempotency_key", "attachment_id"} <= set(
        upload_table.columns.keys()
    )

    link_columns: Any = MessageAttachmentRow.__table__.columns
    assert "source_id" in link_columns
    assert not any(
        getattr(foreign_key.column.table, "name", "") in {
            "artifact_attachments",
            "documents",
        }
        for foreign_key in link_columns["source_id"].foreign_keys
    )
    migration = (
        ROOT / "services/aura-core/migrations/versions/0022_conversation_attachments.py"
    ).read_text()
    assert "UNIQUE (message_id, kind, source_id)" in migration
    assert "artifact_attachment_upload_sessions" in migration
    assert "expires_at" in migration


@pytest.mark.asyncio
async def test_sql_attachment_lifecycle_and_topic_outbox_are_atomic() -> None:
    database_url = os.environ.get("AURA_0076_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0076_DATABASE_URL is required for SQL attachment coverage")
    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    repository = SqlAttachmentRepository(sessions, outbox=SqlOutboxRepository())
    item = await _upload(
        AttachmentService(
            MemoryAttachmentRepository(),
            MemoryObjectStore(),
            inspector=StubInspector(),
        ),
        idempotency_key=str(uuid4()),
    )
    event = AttachmentLifecycleEvent(
        item.id,
        item.status,
        item.version,
        item.updated_at,
        "attachment.ready",
        correlation_id=item.id,
        causation_id=item.id,
        stored_object_count=2,
    )
    try:
        assert await repository.save_with_event(item, event) == item
        assert await repository.get(OWNER, item.id) == item
        async with sessions() as session:
            lifecycle = await session.get(AttachmentLifecycleEventRow, event.event_id)
            outbox = (
                await session.scalars(
                    select(OutboxRow).where(OutboxRow.topic == ATTACHMENT_EVENT_TOPIC)
                )
            ).all()
            assert lifecycle is not None
            assert outbox
            replay = OutboxCommand.from_record(
                outbox[-1].id,
                outbox[-1].topic,
                outbox[-1].payload,
                outbox[-1].created_at or item.updated_at,
            )
            assert replay.topic == ATTACHMENT_EVENT_TOPIC

        deleted = item.deleted()
        deleted_event = AttachmentLifecycleEvent(
            deleted.id,
            deleted.status,
            deleted.version,
            deleted.updated_at,
            "attachment.deleted",
            correlation_id=item.id,
            causation_id=item.id,
        )
        assert await repository.delete_with_event(OWNER, deleted, deleted_event) == deleted

        class FailingOutbox:
            async def stage_command(self, *_: object, **__: object) -> None:
                raise RuntimeError("forced attachment topic failure")

        failed = replace(item, id=uuid4(), version=item.version + 1)
        failed_event = AttachmentLifecycleEvent(
            failed.id,
            failed.status,
            failed.version,
            failed.updated_at,
            "attachment.ready",
            correlation_id=failed.id,
            causation_id=failed.id,
            stored_object_count=2,
        )
        before_outbox_ids = {row.id for row in outbox}
        failing = SqlAttachmentRepository(sessions, outbox=FailingOutbox())  # type: ignore[arg-type]
        with pytest.raises(RuntimeError, match="forced attachment topic failure"):
            await failing.save_with_event(failed, failed_event)
        assert await failing.get(OWNER, failed.id) is None
        assert await repository.get(OWNER, deleted.id) == deleted
        async with sessions() as session:
            assert await session.get(AttachmentLifecycleEventRow, failed_event.event_id) is None
            after_outbox = (
                await session.scalars(
                    select(OutboxRow).where(OutboxRow.topic == ATTACHMENT_EVENT_TOPIC)
                )
            ).all()
            assert {row.id for row in after_outbox} == before_outbox_ids
        malformed = dict(outbox[-1].payload)
        malformed["status"] = "not-a-status"
        with pytest.raises(ValueError, match="invalid status"):
            OutboxCommand.from_record(uuid4(), ATTACHMENT_EVENT_TOPIC, malformed, item.updated_at)
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_message_links_flush_after_parent_and_rollback_together() -> None:
    """A real FK flush catches the original attachment admission regression."""

    database_url = os.environ.get("AURA_0077_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0077_DATABASE_URL is required for SQL attachment coverage")
    from alembic import command
    from aura_core.domains.governance.identity.persistence import PrincipalRow
    from aura_core.domains.interaction.agents.adapters.postgres import SqlAgentSeeder
    from aura_core.domains.interaction.conversations.public import GENERAL_AGENT
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise

    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    conversation_id = uuid4()
    message_id = uuid4()
    link_id = uuid4()
    source_id = uuid4()
    principal_id = uuid4()
    now = datetime.now(UTC)
    summary = _summary("image", source_id)
    message = Message(
        conversation_id,
        MessageRole.USER,
        "persist attached image",
        id=message_id,
        created_at=now,
        updated_at=now,
        attachments=(replace(summary, link_id=link_id),),
    )
    repository = SqlConversationRepository()
    try:
        await SqlAgentSeeder(sessions).seed()
        async with sessions() as session:
            session.add(
                PrincipalRow(
                    id=principal_id,
                    issuer="https://aura0077.example",
                    subject=f"message-ordering-{principal_id}",
                )
            )
            session.add(
                ConversationRow(
                    id=conversation_id,
                    principal_id=principal_id,
                    title="attachment-ordering",
                    agent_profile_id=GENERAL_AGENT.profile_id,
                    agent_revision_id=GENERAL_AGENT.revision_id,
                    model_id="chat",
                )
            )
            await session.flush()
            repository.stage_message(session, message)
            await session.flush()
            stored_message = await session.get(MessageRow, message_id)
            stored_links = (
                await session.scalars(
                    select(MessageAttachmentRow).where(
                        MessageAttachmentRow.message_id == message_id
                    )
                )
            ).all()
            assert stored_message is not None
            assert stored_message.attachments is not None
            assert len(stored_links) == 1
            assert stored_links[0].message_id == stored_message.id
            assert stored_links[0].source_id == source_id
            assert stored_message.attachments[0]["linkId"] == str(link_id)
            await session.rollback()

        async with sessions() as session:
            assert await session.get(MessageRow, message_id) is None
            assert not (
                await session.scalars(
                    select(MessageAttachmentRow).where(
                        MessageAttachmentRow.message_id == message_id
                    )
                )
            ).all()
            assert await session.get(ConversationRow, conversation_id) is None
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_conversation_create_with_attachments_is_fully_atomic(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise production ConversationStore admission and its transaction boundary."""

    database_url = os.environ.get("AURA_0077_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0077_DATABASE_URL is required for SQL attachment coverage")
    from alembic import command
    from aura_core.domains.interaction.personas.adapters.postgres import SqlPersonaSeeder
    from aura_core.entrypoints.cli import alembic_config
    from aura_core.platform.auth import Settings
    from sqlalchemy import select, text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise

    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    issuer = "https://aura0077.example"
    subject = f"conversation-{uuid4().hex}"
    owner = (issuer, subject)
    create_key = str(uuid4())
    failed_key = str(uuid4())
    conversation_id: UUID | None = None
    run_id: UUID | None = None
    message_id: UUID | None = None
    attachment: Attachment | None = None
    try:
        await SqlPersonaSeeder(sessions).seed()
        await SqlAgentSeeder(sessions).seed()
        catalog = AgentCatalog()
        agent_store = SqlAgentStore(sessions, catalog)
        await agent_store.refresh()

        attachment_repository = SqlAttachmentRepository(sessions, SqlOutboxRepository())
        attachment_service = AttachmentService(
            attachment_repository,
            MemoryObjectStore(),
            inspector=StubInspector(),
        )
        attachment = await _upload(
            attachment_service,
            owner=owner,
            filename="conversation-image.png",
            data=b"conversation-image",
            idempotency_key=str(uuid4()),
        )
        model = ModelDescriptor(
            "chat",
            "Chat with vision",
            "ollama",
            ("chat", "vision"),
            model_revision="revision-1",
            model_digest="d" * 64,
        )
        store = SqlConversationStore(sessions, catalog)
        store.agent_store = agent_store
        store.set_attachment_services(attachment_service, None)
        conversation, user_message, run = await store.create(
            issuer,
            subject,
            "inspect this image",
            "chat",
            (model,),
            create_key,
            attachments=(ConversationAttachmentReference("image", attachment.id),),
        )
        conversation_id, message_id, run_id = conversation.id, user_message.id, run.id

        async with sessions() as session:
            persisted_conversation = await session.get(ConversationRow, conversation.id)
            persisted_message = await session.get(MessageRow, user_message.id)
            persisted_run = await session.get(RunRow, run.id)
            persisted_links = (
                await session.scalars(
                    select(MessageAttachmentRow).where(
                        MessageAttachmentRow.message_id == user_message.id
                    )
                )
            ).all()
            persisted_idempotency = await session.get(
                IdempotencyRow, (issuer, subject, create_key)
            )
            persisted_outbox = await session.get(OutboxRow, run.id)
            assert persisted_conversation is not None
            assert persisted_message is not None
            assert persisted_message.attachments
            assert persisted_run is not None
            assert len(persisted_links) == 1
            assert persisted_links[0].source_id == attachment.id
            assert persisted_links[0].message_id == persisted_message.id
            assert persisted_message.attachments[0]["sourceId"] == str(attachment.id)
            assert persisted_run.attachments
            assert persisted_idempotency is not None
            assert persisted_idempotency.response["runId"] == str(run.id)
            assert persisted_outbox is not None
            assert persisted_outbox.topic == RUN_COMMAND_TOPIC
            assert persisted_outbox.aggregate_id == run.id

            before_conversation_ids = set(
                (await session.scalars(select(ConversationRow.id))).all()
            )
            before_message_ids = set((await session.scalars(select(MessageRow.id))).all())
            before_link_ids = set(
                (await session.scalars(select(MessageAttachmentRow.id))).all()
            )
            before_run_ids = set((await session.scalars(select(RunRow.id))).all())
            before_run_outbox_ids = set(
                (
                    await session.scalars(
                        select(OutboxRow.id).where(OutboxRow.topic == RUN_COMMAND_TOPIC)
                    )
                ).all()
            )
            before_idempotency_keys = set(
                (
                    await session.execute(
                        select(
                            IdempotencyRow.principal_issuer,
                            IdempotencyRow.principal_subject,
                            IdempotencyRow.idempotency_key,
                        )
                    )
                ).all()
            )

        failed_conversation_id = UUID(
            bytes=hashlib.sha256(f"{subject}:{failed_key}".encode()).digest()[:16]
        )
        failed_message_id = UUID(
            bytes=hashlib.sha256(f"{failed_conversation_id}:message".encode()).digest()[:16]
        )
        failed_run_id = UUID(
            bytes=hashlib.sha256(f"{failed_conversation_id}:run".encode()).digest()[:16]
        )

        def fail_late(*args: object, **kwargs: object) -> None:
            del args, kwargs
            raise RuntimeError("late conversation transaction failure")

        monkeypatch.setattr(store, "_audit", fail_late)
        with pytest.raises(RuntimeError, match="late conversation transaction failure"):
            await store.create(
                issuer,
                subject,
                "must roll back",
                "chat",
                (model,),
                failed_key,
                attachments=(ConversationAttachmentReference("image", attachment.id),),
            )

        async with sessions() as session:
            after_conversation_ids = set(
                (await session.scalars(select(ConversationRow.id))).all()
            )
            after_message_ids = set((await session.scalars(select(MessageRow.id))).all())
            after_link_ids = set(
                (await session.scalars(select(MessageAttachmentRow.id))).all()
            )
            after_run_ids = set((await session.scalars(select(RunRow.id))).all())
            after_run_outbox_ids = set(
                (
                    await session.scalars(
                        select(OutboxRow.id).where(OutboxRow.topic == RUN_COMMAND_TOPIC)
                    )
                ).all()
            )
            after_idempotency_keys = set(
                (
                    await session.execute(
                        select(
                            IdempotencyRow.principal_issuer,
                            IdempotencyRow.principal_subject,
                            IdempotencyRow.idempotency_key,
                        )
                    )
                ).all()
            )
            assert after_conversation_ids == before_conversation_ids
            assert after_message_ids == before_message_ids
            assert after_link_ids == before_link_ids
            assert after_run_ids == before_run_ids
            assert after_run_outbox_ids == before_run_outbox_ids
            assert after_idempotency_keys == before_idempotency_keys
            assert await session.get(ConversationRow, failed_conversation_id) is None
            assert await session.get(MessageRow, failed_message_id) is None
            assert await session.get(RunRow, failed_run_id) is None
            assert await session.get(IdempotencyRow, (issuer, subject, failed_key)) is None
            assert await session.get(OutboxRow, failed_run_id) is None
            assert not (
                await session.scalars(
                    select(MessageAttachmentRow).where(
                        MessageAttachmentRow.message_id == failed_message_id
                    )
                )
            ).all()
    finally:
        async with sessions() as session, session.begin():
            if message_id is not None:
                await session.execute(
                    text("DELETE FROM message_attachments WHERE message_id = :message_id"),
                    {"message_id": message_id},
                )
                if run_id is not None:
                    await session.execute(
                        text("DELETE FROM run_events WHERE run_id = :run_id"),
                        {"run_id": run_id},
                    )
                    await session.execute(
                        text("DELETE FROM runs WHERE id = :run_id"),
                        {"run_id": run_id},
                    )
                await session.execute(
                    text("DELETE FROM messages WHERE id = :message_id"),
                    {"message_id": message_id},
                )
            if conversation_id is not None:
                for table in (
                    "conversation_agent_assignments",
                    "conversation_persona_assignments",
                    "conversation_branches",
                ):
                    await session.execute(
                        text(f"DELETE FROM {table} WHERE conversation_id = :conversation_id"),
                        {"conversation_id": conversation_id},
                    )
                await session.execute(
                    text("DELETE FROM conversations WHERE id = :conversation_id"),
                    {"conversation_id": conversation_id},
                )
            if run_id is not None:
                await session.execute(
                    text("DELETE FROM outbox WHERE id = :run_id"), {"run_id": run_id}
                )
            await session.execute(
                text(
                    "DELETE FROM command_idempotency "
                    "WHERE principal_issuer = :issuer AND principal_subject = :subject"
                ),
                {"issuer": issuer, "subject": subject},
            )
            if attachment is not None:
                await session.execute(
                    text(
                        "DELETE FROM artifact_attachment_lifecycle_events "
                        "WHERE attachment_id = :attachment_id"
                    ),
                    {"attachment_id": attachment.id},
                )
                await session.execute(
                    text("DELETE FROM outbox WHERE aggregate_id = :attachment_id"),
                    {"attachment_id": attachment.id},
                )
                await session.execute(
                    text("DELETE FROM artifact_attachments WHERE id = :attachment_id"),
                    {"attachment_id": attachment.id},
                )
        await engine.dispose()


def test_model_vision_required_is_a_typed_non_retryable_409_contract() -> None:
    contract = cast(
        dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text())
    )
    responses = cast(dict[str, Any], contract["components"]["responses"])
    vision_response = responses["RunAdmissionConflict"]
    schema = vision_response["content"]["application/problem+json"]["schema"]
    refs = {item["$ref"] for item in schema["oneOf"]}
    assert "#/components/schemas/ModelVisionRequiredError" in refs
    vision = contract["components"]["schemas"]["ModelVisionRequiredError"]
    assert vision["properties"]["status"] == {"type": "integer", "const": 409}
    assert vision["properties"]["code"] == {
        "type": "string",
        "const": "model_vision_required",
    }
    assert vision["properties"]["retryable"] == {"type": "boolean", "const": False}


def test_attachment_production_seam_evaluators_are_declared_and_content_free() -> None:
    import yaml

    manifest_root = ROOT / "services/aura-core/resources/component-manifests"
    expected = {
        "attachment_capture_redaction": (
            "attachment-processing.yaml",
            {"bytes", "filename", "prompt", "payload", "content"},
        ),
        "vision_capability_and_image_preservation": (
            "attachment-vision-inference.yaml",
            {"bytes", "prompt", "payload", "filename"},
        ),
        "conversation_attachment_branch_isolation": (
            "conversation-persistence.yaml",
            {"prompt", "response", "payload"},
        ),
        "attached_document_grounding_and_citation": (
            "document-retrieval.yaml",
            {"content", "excerpt", "filename", "owner"},
        ),
    }
    for evaluator, (filename, prohibited_subset) in expected.items():
        manifest = cast(dict[str, Any], yaml.safe_load((manifest_root / filename).read_text()))
        evaluators = manifest.get("evaluators", [])
        assert evaluator in evaluators
        assert manifest["capture_policy"] == "metadata_only"
        prohibited = set(manifest["cardinality"]["prohibited"])
        assert prohibited_subset <= prohibited


@pytest.mark.asyncio
async def test_attachment_evaluators_are_deterministic_and_reject_negative_cases() -> None:
    passing = (
        evaluate_attachment_capture_redaction(prohibited_fields_seen=0),
        evaluate_vision_capability_and_image_preservation(
            verified=True, image_parts=2, expected_parts=2
        ),
        evaluate_conversation_attachment_branch_isolation(
            authorized_links=2, foreign_links=0
        ),
        evaluate_attached_document_grounding_and_citation(
            retrieved_documents=2, cited_documents=2
        ),
    )
    assert all(result.outcome == "pass" and result.score == 1.0 for result in passing)
    replayed = (
        evaluate_attachment_capture_redaction(prohibited_fields_seen=0),
        evaluate_vision_capability_and_image_preservation(
            verified=True, image_parts=2, expected_parts=2
        ),
        evaluate_conversation_attachment_branch_isolation(
            authorized_links=2, foreign_links=0
        ),
        evaluate_attached_document_grounding_and_citation(
            retrieved_documents=2, cited_documents=2
        ),
    )
    assert replayed == passing
    assert all(
        set(result.payload())
        == {
            "evaluatorId",
            "evaluatorVersion",
            "componentId",
            "componentVersion",
            "fixtureRevision",
            "replayKey",
            "score",
            "outcome",
            "metadata",
        }
        for result in passing
    )

    negative = (
        evaluate_attachment_capture_redaction(prohibited_fields_seen=1),
        evaluate_vision_capability_and_image_preservation(
            verified=False, image_parts=2, expected_parts=2
        ),
        evaluate_conversation_attachment_branch_isolation(
            authorized_links=2, foreign_links=1
        ),
        evaluate_attached_document_grounding_and_citation(
            retrieved_documents=2, cited_documents=1, invalid_citations=1
        ),
    )
    assert all(result.outcome == "fail" and result.score == 0.0 for result in negative)

    image_id = uuid4()
    document = _ready_document()
    image_service = CountingImageService({image_id: _ready_image()})
    document_service = ReadyDocumentService({document.id: document})
    store = ConversationStore(
        attachment_service=image_service,
        document_service=document_service,
    )
    store.set_model_selection(VerifiedVisionSelection())
    conversation, message, run = await store.create(
        *OWNER,
        "inspect these files",
        "vision-model",
        (VISION_DESCRIPTOR,),
        str(uuid4()),
        attachments=(
            ConversationAttachmentReference("image", image_id),
            ConversationAttachmentReference("document", document.id),
        ),
    )
    selected_images, selected_documents = select_attachment_context(
        conversation,
        message.branch_id,
        current_message_id=message.id,
    )
    retrieved = await search_attached_documents(
        document_service,
        OWNER,
        message.content,
        tuple(item.source_id for item in selected_documents),
    )
    provider = ProductionEvaluatorProvider(native=True, synthesis=True)
    coordinator = RunCoordinator(
        cast(RunRepository, store),
        EventPublisher(),
        InMemoryOutbox(),
        document_service=document_service,
        attachment_service=image_service,
    )
    await coordinator.execute(run.id, provider)
    _projected, completed = await store.find_run_any(run.id)
    completed_run = completed
    assert [kind for kind, _ in provider.calls] == ["native", "synthesis"]
    assert completed_run.citations

    capture_metrics = MetadataMetrics()
    await _upload(
        AttachmentService(
            MemoryAttachmentRepository(),
            MemoryObjectStore(),
            inspector=StubInspector(),
            metrics=capture_metrics,
        )
    )
    provider_messages = tuple(
        item for _kind, messages in provider.calls for item in messages
    )
    branch_links = (*selected_images, *selected_documents)
    production = evaluate_production_fixture(
        branch_attachment_links=branch_links,
        authorized_attachment_links=branch_links,
        selected_images=selected_images,
        provider_messages=provider_messages,
        vision_descriptor=(await provider.list_models())[0],
        retrieval_hits=cast(Sequence[Any], retrieved),
        citations=completed_run.citations,
        capture_records=capture_metrics.snapshot(),
    )
    assert all(result.outcome == "pass" for result in production)
    empty_retrieval = await search_attached_documents(
        document_service, OWNER, message.content, ()
    )
    production_negative = evaluate_production_fixture(
        branch_attachment_links=branch_links,
        authorized_attachment_links=selected_images,
        selected_images=selected_images,
        provider_messages=provider_messages,
        vision_descriptor=(await provider.list_models())[0],
        retrieval_hits=cast(Sequence[Any], empty_retrieval),
        citations=completed_run.citations,
        capture_records=capture_metrics.snapshot(),
    )
    assert [result.outcome for result in production_negative] == [
        "pass",
        "pass",
        "fail",
        "fail",
    ]


def test_attachment_contract_and_generated_client_are_additive_and_redacted() -> None:
    contract = cast(
        dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text())
    )
    assert contract["info"]["version"] == "1.18.0"
    paths = cast(dict[str, Any], contract["paths"])
    assert {
        "/api/v1/attachments/uploads",
        "/api/v1/attachments/uploads/{upload_id}/content",
        "/api/v1/attachments",
        "/api/v1/attachments/{attachment_id}",
        "/api/v1/attachments/{attachment_id}/content",
        "/api/v1/attachments/{attachment_id}/preview",
    } <= paths.keys()
    schemas = cast(dict[str, Any], contract["components"]["schemas"])
    assert schemas["MessageAttachmentInput"]["maxItems"] == 8
    summary = schemas["MessageAttachmentSummary"]
    if "oneOf" in summary:
        summary_variants = cast(list[dict[str, Any]], summary["oneOf"])
        assert {
            variant["$ref"] for variant in summary_variants
        } == {
            "#/components/schemas/AvailableMessageAttachmentSummary",
            "#/components/schemas/UnavailableMessageAttachmentSummary",
        }
        assert {
            schemas[variant["$ref"].rsplit("/", 1)[-1]]["properties"][
                "availability"
            ]["const"]
            for variant in summary_variants
        } == {"available", "unavailable"}
    else:
        assert summary["properties"]["availability"]["$ref"].endswith(
            "/AttachmentAvailability"
        )
    assert schemas["AttachmentAvailability"]["enum"] == [
        "available",
        "unavailable",
    ]

    event = cast(
        dict[str, Any],
        json.loads(
            (ROOT / "contracts/events/artifacts/v1/attachment-events.schema.json").read_text()
        ),
    )
    event_properties = set(event["$defs"]["AttachmentEvent"]["properties"])
    assert not {
        "filename",
        "mediaType",
        "byteCount",
        "width",
        "height",
        "bytes",
        "exif",
    } & event_properties
    assert event["$defs"]["AttachmentEvent"]["additionalProperties"] is False

    generated = (
        ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"
    ).read_text()
    for operation in (
        "createAttachmentUpload",
        "uploadAttachmentContent",
        "listAttachments",
        "getAttachmentPreview",
        "deleteAttachment",
    ):
        assert operation in generated
