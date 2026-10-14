"""Worker delivery and recovery evaluations for AURA-0068."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.knowledge.documents.public import (
    Document,
    DocumentService,
    DocumentStatus,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.entrypoints.worker import app as worker_app
from aura_core.entrypoints.worker.documents import DocumentCommand, DocumentWorker

OWNER = ("https://issuer.example", "worker-owner")


@dataclass(frozen=True)
class Command:
    topic: str
    aggregate_id: UUID | None
    id: UUID = field(default_factory=uuid4)
    correlation_id: UUID | None = None
    causation_id: UUID | None = None


class Consumer:
    def __init__(self, command: Command) -> None:
        self.command = command
        self.acks = 0
        self.nacks = 0

    async def receive(self) -> Command:
        return self.command

    async def ack(self) -> None:
        self.acks += 1

    async def nack(self, **kwargs: object) -> None:
        del kwargs
        self.nacks += 1

    async def reject(self, **kwargs: object) -> None:
        del kwargs
        self.nacks += 1


@pytest.mark.asyncio
async def test_duplicate_processing_delivery_is_idempotent_and_does_not_duplicate_chunks() -> None:
    repository = MemoryDocumentRepository()
    service = DocumentService(repository, MemoryDocumentObjectStore())
    document = await service.upload(
        *OWNER,
        filename="worker.txt",
        media_type="text/plain",
        stream=b"worker replay evidence",
    )
    worker = DocumentWorker(service)

    first = await worker.process_command(
        cast(DocumentCommand, Command("aura.knowledge.documents.process.v1", document.id))
    )
    second = await worker.process_command(
        cast(DocumentCommand, Command("aura.knowledge.documents.process.v1", document.id))
    )
    first_document = first if isinstance(first, Document) else None
    second_document = second if isinstance(second, Document) else None
    assert first_document is not None and second_document is not None
    assert first_document.status is second_document.status is DocumentStatus.READY
    assert first_document.version == second_document.version
    assert len(await repository.chunks(document.id)) == 1


@pytest.mark.asyncio
async def test_malformed_or_foreign_worker_commands_are_rejected_before_processing() -> None:
    service = DocumentService(MemoryDocumentRepository(), MemoryDocumentObjectStore())
    worker = DocumentWorker(service)
    with pytest.raises(ValueError, match="invalid"):
        await worker.process_command(cast(DocumentCommand, Command("wrong.topic", None)))
    with pytest.raises(ValueError, match="invalid"):
        await worker.process_command(
            cast(DocumentCommand, Command("aura.knowledge.documents.process.v1", None))
        )


@pytest.mark.asyncio
async def test_outbox_document_topic_has_a_real_nats_worker_dispatch_boundary() -> None:
    handler = cast(
        Callable[[DocumentWorker, Consumer], Awaitable[object]],
        getattr(worker_app, "run_document_once", None),
    )
    assert callable(handler), "document processing must be wired to the NATS worker loop"
    repository = MemoryDocumentRepository()
    service = DocumentService(repository, MemoryDocumentObjectStore())
    document = await service.upload(
        *OWNER,
        filename="dispatch.txt",
        media_type="text/plain",
        stream=b"dispatch",
    )
    consumer = Consumer(Command("aura.knowledge.documents.process.v1", document.id))
    await handler(DocumentWorker(service), consumer)
    assert consumer.acks == 1
    assert consumer.nacks == 0
    processed = await repository.get_any(document.id)
    assert processed is not None
    assert processed.status is DocumentStatus.READY


@pytest.mark.asyncio
async def test_worker_recovery_after_parse_failure_can_be_retried_without_partial_chunks() -> None:
    repository = MemoryDocumentRepository()
    store = MemoryDocumentObjectStore()
    service = DocumentService(repository, store)
    document = await service.upload(
        *OWNER,
        filename="invalid.pdf",
        media_type="application/pdf",
        stream=b"%PDF-1.7 malformed",
    )
    worker = DocumentWorker(service)
    failed = cast(Document, await worker.process(document.id))
    assert failed.status is DocumentStatus.FAILED
    assert await repository.chunks(document.id) == []
    queued = await service.retry(*OWNER, document.id, expected_version=failed.version)
    assert queued.status is DocumentStatus.QUEUED
