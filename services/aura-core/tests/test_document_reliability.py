"""Reliability and cleanup evaluations for the AURA-0068 document boundary."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.knowledge.documents.parser import DocumentParseError
from aura_core.domains.knowledge.documents.public import (
    PROCESSING_LEASE,
    UPLOAD_SESSION_TTL,
    Document,
    DocumentCommandSink,
    DocumentService,
    DocumentStatus,
    DocumentVersionConflict,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.entrypoints.worker.documents import DocumentCommand, DocumentWorker

OWNER = ("https://issuer.example", "reliability-owner")


class TrackingStore(MemoryDocumentObjectStore):
    def __init__(self) -> None:
        super().__init__()
        self.put_keys: list[str] = []
        self.delete_keys: list[str] = []

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        self.put_keys.append(key)
        return await super().put_stream(key, stream, content_type=content_type)

    async def delete(self, key: str) -> None:
        self.delete_keys.append(key)
        await super().delete(key)


class FailingCreateRepository(MemoryDocumentRepository):
    async def create_upload(
        self,
        document: Document,
        *,
        idempotency_key: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> Document:
        del document, idempotency_key, receipt_fingerprint
        raise RuntimeError("database commit failed")


class BarrierDuplicateRepository(MemoryDocumentRepository):
    def __init__(self) -> None:
        super().__init__()
        self.calls = 0
        self.reached = asyncio.Event()

    async def find_duplicate(self, owner: tuple[str, str], digest: str):  # type: ignore[no-untyped-def]
        self.calls += 1
        if self.calls == 2:
            self.reached.set()
        await self.reached.wait()
        return await super().find_duplicate(owner, digest)


class CrashAfterChunkRepository(MemoryDocumentRepository):
    async def settle_ready(self, document, chunks, *, expected_version):  # type: ignore[no-untyped-def]
        del expected_version
        self.chunk_rows[document.id] = list(chunks)
        raise RuntimeError("worker crashed after chunk write")


class FlakyDeleteStore(TrackingStore):
    def __init__(self) -> None:
        super().__init__()
        self.fail_delete = True

    async def delete(self, key: str) -> None:
        self.delete_keys.append(key)
        if self.fail_delete:
            raise RuntimeError("Garage unavailable")
        await MemoryDocumentObjectStore.delete(self, key)


class InterruptedPutStore(TrackingStore):
    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        self.put_keys.append(key)
        pieces: list[bytes] = []
        async for piece in stream:
            pieces.append(piece)
            self.objects[key] = b"".join(pieces)
        raise RuntimeError("client stream interrupted")


class RetainedInterruptedStore(FlakyDeleteStore):
    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        self.put_keys.append(key)
        pieces: list[bytes] = []
        async for piece in stream:
            pieces.append(piece)
            self.objects[key] = b"".join(pieces)
            raise RuntimeError("process interrupted")
        raise AssertionError("the interruption boundary was not reached")


class SessionMetadataRepository(MemoryDocumentRepository):
    """Memory adapter with the SQL repository's upload-session placeholder row."""

    async def create_upload_session(self, value) -> None:  # type: ignore[no-untyped-def]
        await super().create_upload_session(value)
        now = value.created_at
        self.documents[value.document_id] = Document(
            value.document_id,
            value.issuer,
            value.subject,
            value.filename,
            value.media_type,
            value.byte_count,
            "",
            "",
            DocumentStatus.UPLOADING,
            value.version,
            now,
            now,
        )


class FailingCleanupRepository(SessionMetadataRepository):
    def __init__(self) -> None:
        super().__init__()
        self.fail_cleanup_staging = True

    async def mark_upload_cleanup(
        self,
        document_id: UUID,
        object_key: str,
        *,
        command_sink: DocumentCommandSink | None,
    ) -> None:
        if self.fail_cleanup_staging:
            self.fail_cleanup_staging = False
            raise RuntimeError("cleanup staging interrupted")
        await super().mark_upload_cleanup(
            document_id, object_key, command_sink=command_sink
        )


class DurableDocumentCommandSink:
    """Identifier-only outbox fake; object keys and content never enter commands."""

    def __init__(self) -> None:
        self.commands: list[tuple[UUID, str]] = []

    async def publish(self, document_id: UUID, *, operation: str = "process") -> None:
        self.commands.append((document_id, operation))

    async def stage(self, *_: object, **__: object) -> None:
        return None

    async def stage_event(self, *_: object, **__: object) -> None:
        return None


class DeleteCommand:
    def __init__(self, document_id: UUID) -> None:
        self.id = uuid4()
        self.aggregate_id = document_id
        self.topic = "aura.knowledge.documents.delete.v1"
        self.correlation_id = None
        self.causation_id = None


@pytest.mark.asyncio
async def test_upload_session_survives_service_recreation_and_has_stable_identity() -> None:
    repository = MemoryDocumentRepository()
    store = TrackingStore()
    first = DocumentService(repository, store)
    session = await first.create_upload_session(
        *OWNER,
        filename="persistent.txt",
        media_type="text/plain",
        byte_count=8,
        idempotency_key="session-command",
    )

    recreated = DocumentService(repository, store)
    document = await recreated.complete_upload(
        session.id,
        b"persist!",
        issuer=OWNER[0],
        subject=OWNER[1],
        content_type="text/plain",
        expected_version=1,
        idempotency_key="content-command",
    )
    assert document.id == session.document_id
    assert document.byte_count == 8


@pytest.mark.asyncio
async def test_create_upload_session_replay_is_idempotent() -> None:
    service = DocumentService(MemoryDocumentRepository(), TrackingStore())
    first = await service.create_upload_session(
        *OWNER,
        filename="same.txt",
        media_type="text/plain",
        byte_count=4,
        idempotency_key="same-session",
    )
    replay = await service.create_upload_session(
        *OWNER,
        filename="same.txt",
        media_type="text/plain",
        byte_count=4,
        idempotency_key="same-session",
    )
    assert replay.id == first.id
    assert replay.document_id == first.document_id


@pytest.mark.asyncio
async def test_completed_upload_session_replay_after_recreation_preserves_ids() -> None:
    """A retried create request must remain idempotent after completion and restart."""

    repository = MemoryDocumentRepository()
    store = TrackingStore()
    first = DocumentService(repository, store)
    session = await first.create_upload_session(
        *OWNER,
        filename="completed.txt",
        media_type="text/plain",
        byte_count=9,
        idempotency_key="completed-session",
    )
    uploaded = await first.complete_upload(
        session.id,
        b"completed",
        issuer=OWNER[0],
        subject=OWNER[1],
        content_type="text/plain",
        expected_version=session.version,
        idempotency_key="completed-content",
    )

    recreated = DocumentService(repository, store)
    replay = await recreated.create_upload_session(
        *OWNER,
        filename="completed.txt",
        media_type="text/plain",
        byte_count=9,
        idempotency_key="completed-session",
    )
    assert replay.id == session.id
    assert replay.document_id == uploaded.id == session.document_id


@pytest.mark.asyncio
async def test_upload_content_idempotency_key_cannot_cross_upload_sessions() -> None:
    repository = MemoryDocumentRepository()
    store = TrackingStore()
    first = DocumentService(repository, store)
    session_a = await first.create_upload_session(
        *OWNER,
        filename="first.txt",
        media_type="text/plain",
        byte_count=5,
        idempotency_key="session-a",
    )
    document_a = await first.complete_upload(
        session_a.id,
        b"first",
        issuer=OWNER[0],
        subject=OWNER[1],
        content_type="text/plain",
        expected_version=session_a.version,
        idempotency_key="content-key",
    )

    recreated = DocumentService(repository, store)
    session_b = await recreated.create_upload_session(
        *OWNER,
        filename="second.txt",
        media_type="text/plain",
        byte_count=6,
        idempotency_key="session-b",
    )
    with pytest.raises(DocumentVersionConflict, match="idempotency"):
        await recreated.complete_upload(
            session_b.id,
            b"second",
            issuer=OWNER[0],
            subject=OWNER[1],
            content_type="text/plain",
            expected_version=session_b.version,
            idempotency_key="content-key",
        )

    replay = await recreated.complete_upload(
        session_a.id,
        b"first",
        issuer=OWNER[0],
        subject=OWNER[1],
        content_type="text/plain",
        expected_version=session_a.version,
        idempotency_key="content-key",
    )
    assert replay.id == document_a.id
    assert replay.duplicate is True


@pytest.mark.asyncio
async def test_upload_retry_and_delete_idempotency_survive_service_recreation() -> None:
    repository = MemoryDocumentRepository()
    store = TrackingStore()
    first = DocumentService(repository, store)
    uploaded = await first.upload(
        *OWNER,
        filename="recreated.txt",
        media_type="text/plain",
        stream=b"recreated",
        idempotency_key="upload-replay",
    )

    second = DocumentService(repository, store)
    replayed_upload = await second.upload(
        *OWNER,
        filename="recreated.txt",
        media_type="text/plain",
        stream=b"recreated",
        idempotency_key="upload-replay",
    )
    assert replayed_upload.id == uploaded.id
    assert replayed_upload.duplicate is True

    failed = await repository.save(
        replace(uploaded, status=DocumentStatus.FAILED, version=2, failure_code="parse_failed")
    )
    retried = await second.retry(
        *OWNER,
        failed.id,
        expected_version=failed.version,
        idempotency_key="retry-replay",
    )
    third = DocumentService(repository, store)
    replayed_retry = await third.retry(
        *OWNER,
        failed.id,
        expected_version=failed.version,
        idempotency_key="retry-replay",
    )
    assert replayed_retry.id == retried.id
    assert replayed_retry.version == retried.version

    deleted = await third.delete(
        *OWNER,
        failed.id,
        expected_version=replayed_retry.version,
        idempotency_key="delete-replay",
    )
    fourth = DocumentService(repository, store)
    replayed_delete = await fourth.delete(
        *OWNER,
        failed.id,
        expected_version=replayed_retry.version,
        idempotency_key="delete-replay",
    )
    assert replayed_delete.id == deleted.id
    assert replayed_delete.version == deleted.version


@pytest.mark.asyncio
async def test_expired_processing_lease_is_reclaimed_after_worker_crash() -> None:
    repository = MemoryDocumentRepository()
    service = DocumentService(repository, TrackingStore())
    document = await service.upload(
        *OWNER,
        filename="lease.txt",
        media_type="text/plain",
        stream=b"lease recovery",
    )
    repository.documents[document.id] = replace(
        document,
        status=DocumentStatus.PROCESSING,
        version=2,
        updated_at=datetime.now(UTC) - PROCESSING_LEASE - timedelta(seconds=1),
    )

    recovered = await service.process(document.id)
    assert recovered.status is DocumentStatus.READY
    assert len(await repository.chunks(document.id)) == 1


@pytest.mark.asyncio
async def test_malformed_upload_and_database_failure_leave_no_orphan_object() -> None:
    malformed_store = TrackingStore()
    malformed_service = DocumentService(MemoryDocumentRepository(), malformed_store)
    with pytest.raises(DocumentParseError):
        await malformed_service.upload(
            *OWNER,
            filename="bad.pdf",
            media_type="application/pdf",
            stream=b"not a PDF",
        )
    assert malformed_store.objects == {}

    database_store = TrackingStore()
    database_service = DocumentService(FailingCreateRepository(), database_store)
    with pytest.raises(RuntimeError, match="database"):
        await database_service.upload(
            *OWNER,
            filename="database.txt",
            media_type="text/plain",
            stream=b"database failure",
        )
    assert database_store.objects == {}


@pytest.mark.asyncio
async def test_rejected_upload_cleanup_is_durable_identifier_only_and_worker_retryable() -> None:
    repository = SessionMetadataRepository()
    store = FlakyDeleteStore()
    commands = DurableDocumentCommandSink()
    service = DocumentService(repository, store, commands=commands)
    session = await service.create_upload_session(
        *OWNER,
        filename="rejected.pdf",
        media_type="application/pdf",
        byte_count=16,
        idempotency_key="rejected-session",
    )
    # The production SQL adapter creates this row transactionally with the
    # session; the deterministic fake mirrors that metadata placeholder.
    document_id = session.document_id

    with pytest.raises(DocumentParseError):
        await service.complete_upload(
            session.id,
            b"not a valid PDF!",
            issuer=OWNER[0],
            subject=OWNER[1],
            content_type="application/pdf",
            expected_version=session.version,
            idempotency_key="rejected-content",
        )

    pending = repository.documents[document_id]
    assert pending.status is DocumentStatus.DELETING
    assert pending.failure_code == "upload_cleanup_retryable"
    assert await repository.chunks(document_id) == []
    assert len(store.objects) == 1
    object_key = next(iter(store.objects))
    assert pending.object_key == object_key
    assert commands.commands == [(document_id, "delete")]
    assert object_key not in repr(commands.commands)
    assert b"not a valid PDF" not in repr(commands.commands).encode()

    store.fail_delete = False
    worker = DocumentWorker(service)
    command = cast(DocumentCommand, DeleteCommand(document_id))
    await worker.delete_command(command)
    replay = cast(Document, await worker.delete_command(command))

    assert replay.id == document_id
    assert store.objects == {}
    assert document_id not in repository.documents
    assert document_id not in repository.chunk_rows
    assert document_id in repository.tombstones


@pytest.mark.asyncio
async def test_stale_interrupted_upload_recovers_reserved_object_after_cleanup_crash() -> None:
    repository = FailingCleanupRepository()
    store = RetainedInterruptedStore()
    commands = DurableDocumentCommandSink()
    service = DocumentService(repository, store, commands=commands)
    private_marker = b"private interrupted bytes"
    session = await service.create_upload_session(
        *OWNER,
        filename="interrupted.txt",
        media_type="text/plain",
        byte_count=len(private_marker),
        idempotency_key="interrupted-session",
    )

    with pytest.raises(RuntimeError, match="cleanup staging interrupted"):
        await service.complete_upload(
            session.id,
            private_marker,
            issuer=OWNER[0],
            subject=OWNER[1],
            content_type="text/plain",
            expected_version=session.version,
            idempotency_key="interrupted-content",
        )

    crashed = repository.documents[session.document_id]
    assert crashed.status is DocumentStatus.UPLOADING
    assert crashed.object_key
    assert store.objects[crashed.object_key] == private_marker
    assert commands.commands == []

    repository.documents[session.document_id] = replace(
        crashed,
        updated_at=datetime.now(UTC) - UPLOAD_SESSION_TTL - timedelta(seconds=1),
    )
    repository.upload_sessions[session.id] = replace(
        session,
        expires_at=datetime.now(UTC) - timedelta(seconds=1),
    )
    recreated = DocumentService(repository, store, commands=commands)
    assert await recreated.recover_stale_uploads() == 1
    pending = repository.documents[session.document_id]
    assert pending.status is DocumentStatus.DELETING
    assert commands.commands == [(session.document_id, "delete")]
    assert private_marker.decode() not in repr(commands.commands)
    assert crashed.object_key not in repr(commands.commands)

    store.fail_delete = False
    worker = DocumentWorker(recreated)
    command = cast(DocumentCommand, DeleteCommand(session.document_id))
    await worker.delete_command(command)
    await worker.delete_command(command)
    assert store.objects == {}
    assert session.document_id not in repository.documents
    assert session.document_id not in repository.chunk_rows
    assert set(repository.tombstones) == {session.document_id}
    assert private_marker.decode() not in repr(repository.tombstones)


@pytest.mark.asyncio
async def test_interrupted_upload_stream_removes_partial_object() -> None:
    store = InterruptedPutStore()
    service = DocumentService(MemoryDocumentRepository(), store)

    async def interrupted() -> AsyncIterable[bytes]:
        yield b"partial"
        raise RuntimeError("client disconnected")

    with pytest.raises(RuntimeError, match="client disconnected"):
        await service.upload(
            *OWNER,
            filename="interrupted.txt",
            media_type="text/plain",
            stream=interrupted(),
        )
    assert store.objects == {}
    assert store.delete_keys == store.put_keys


@pytest.mark.asyncio
async def test_concurrent_duplicate_uploads_create_one_document_and_one_object() -> None:
    repository = BarrierDuplicateRepository()
    store = TrackingStore()
    service = DocumentService(repository, store)
    results = await asyncio.gather(
        service.upload(
            *OWNER,
            filename="one.txt",
            media_type="text/plain",
            stream=b"same bytes",
        ),
        service.upload(
            *OWNER,
            filename="two.txt",
            media_type="text/plain",
            stream=b"same bytes",
        ),
    )
    assert results[0].id == results[1].id
    assert len(repository.documents) == 1
    assert len(store.objects) == 1


@pytest.mark.asyncio
async def test_deletion_outage_is_search_excluded_then_retryable_to_tombstone() -> None:
    store = FlakyDeleteStore()
    service = DocumentService(MemoryDocumentRepository(), store)
    document = await service.upload(
        *OWNER,
        filename="outage.txt",
        media_type="text/plain",
        stream=b"delete outage",
    )
    await service.process(document.id)
    deleting = await service.delete(*OWNER, document.id, expected_version=3)
    assert deleting.status is DocumentStatus.DELETING
    assert await service.search(*OWNER, "outage") == []
    store.fail_delete = False
    completed = await service.delete(*OWNER, document.id, expected_version=deleting.version)
    assert completed.status is DocumentStatus.DELETING
    assert await service.get(*OWNER, document.id)
    assert await service.search(*OWNER, "outage") == []


@pytest.mark.asyncio
async def test_successful_deletion_purges_private_receipts_and_leaves_only_tombstone_metadata(
) -> None:
    repository = MemoryDocumentRepository()
    store = TrackingStore()
    commands = DurableDocumentCommandSink()
    service = DocumentService(repository, store, commands=commands)
    uploaded = await service.upload(
        *OWNER,
        filename="receipt-cleanup.txt",
        media_type="text/plain",
        stream=b"receipt cleanup private bytes",
        idempotency_key="upload-receipt",
    )
    upload_fingerprint = repository.command_receipt_fingerprints[
        (OWNER, "upload", "upload-receipt")
    ]
    assert uploaded.digest not in upload_fingerprint
    await service.process(uploaded.id)
    deleting = await service.delete(
        *OWNER,
        uploaded.id,
        expected_version=3,
        idempotency_key="delete-receipt",
    )
    delete_fingerprint = repository.command_receipt_fingerprints[
        (OWNER, "delete", "delete-receipt")
    ]
    assert uploaded.digest not in delete_fingerprint
    assert repository.command_receipts
    assert repository.idempotency

    settled = await service.delete_by_id(uploaded.id)
    assert settled.id == uploaded.id
    assert settled.version == deleting.version
    assert repository.command_receipts == {}
    assert repository.command_receipt_fingerprints == {}
    assert repository.idempotency == {}
    assert repository.upload_sessions == {}
    assert store.objects == {}

    tombstone = await repository.get_any(uploaded.id)
    assert tombstone is not None
    assert tombstone.id == uploaded.id
    assert tombstone.version == deleting.version
    assert tombstone.issuer == ""
    assert tombstone.subject == ""
    assert tombstone.filename == ""
    assert tombstone.digest == ""
    assert tombstone.object_key == ""
    assert tombstone.byte_count == 0
    assert repr(repository.tombstones).count(uploaded.filename) == 0
    assert b"receipt cleanup private bytes" not in repr(repository.tombstones).encode()
@pytest.mark.asyncio
async def test_concurrent_processing_deliveries_commit_one_stable_chunk_set() -> None:
    repository = MemoryDocumentRepository()
    store = TrackingStore()
    service = DocumentService(repository, store)
    document = await service.upload(
        *OWNER,
        filename="concurrent.txt",
        media_type="text/plain",
        stream=b"concurrent processing",
    )
    results = await asyncio.gather(service.process(document.id), service.process(document.id))
    assert all(result.status is DocumentStatus.READY for result in results)
    assert len(await repository.chunks(document.id)) == 1
    assert await service.search(*OWNER, "concurrent")


@pytest.mark.asyncio
async def test_processing_crash_boundary_does_not_expose_partial_searchable_chunks() -> None:
    repository = CrashAfterChunkRepository()
    service = DocumentService(repository, TrackingStore())
    document = await service.upload(
        *OWNER,
        filename="crash.txt",
        media_type="text/plain",
        stream=b"crash boundary",
    )
    failed = await service.process(document.id)
    assert failed.status is DocumentStatus.FAILED
    assert await service.search(*OWNER, "crash") == []
    assert await repository.chunks(document.id) == []


@pytest.mark.asyncio
async def test_process_and_delete_race_cannot_restore_searchable_chunks_or_object() -> None:
    repository = MemoryDocumentRepository()
    store = TrackingStore()
    service = DocumentService(repository, store)
    document = await service.upload(
        *OWNER,
        filename="race.txt",
        media_type="text/plain",
        stream=b"process delete race",
    )
    outcomes = await asyncio.gather(
        service.process(document.id),
        service.delete(*OWNER, document.id, expected_version=1),
        return_exceptions=True,
    )
    assert any(isinstance(outcome, BaseException) for outcome in outcomes) or any(
        getattr(outcome, "status", None) is DocumentStatus.DELETING for outcome in outcomes
    )
    current = repository.documents[document.id]
    if current.status is DocumentStatus.READY:
        current = await service.delete(*OWNER, document.id, expected_version=current.version)
    assert current.status is DocumentStatus.DELETING
    await service.delete_by_id(document.id)
    assert await service.search(*OWNER, "race") == []
    assert document.object_key not in store.objects
