"""Composition of the public document use cases and their adapters."""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid5

from aura_core.domains.knowledge.documents.persistence import DocumentLifecycleEventRow
from aura_core.domains.knowledge.documents.public import (
    DOCUMENT_ID_NAMESPACE,
    DOCUMENT_PROCESSING_TOPIC,
    DocumentCommandSink,
    DocumentService,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.domains.knowledge.documents.repository import SqlDocumentRepository
from aura_core.platform.outbox import (
    OutboxCommand,
    SqlOutboxRepository,
    make_aggregate_command,
    make_event_command,
)
from aura_core.providers.document_parser import HttpDocumentParser
from aura_core.providers.object_storage.garage import GarageS3ObjectStore, UnavailableObjectStore


def document_service(
    sessions: Any = None,
    *,
    settings: Any = None,
    metrics: Any = None,
    testing: bool = False,
    embedder: Any = None,
    commands: Any = None,
    parser: Any = None,
) -> DocumentService:
    if testing or sessions is None:
        return DocumentService(
            MemoryDocumentRepository(),
            MemoryDocumentObjectStore(),
            embedder=embedder,
            commands=commands,
            parser=parser,
            metrics=metrics,
        )
    effective_commands = commands or SqlDocumentCommandSink(sessions)
    repository = SqlDocumentRepository(sessions, outbox=effective_commands)
    endpoint = settings.object_storage_endpoint
    bucket = settings.object_storage_bucket
    access_key = settings.object_storage_access_key
    secret_key = settings.object_storage_secret_key
    store = (
        GarageS3ObjectStore(
            endpoint,
            bucket,
            access_key=access_key,
            secret_key=secret_key,
            region=settings.object_storage_region,
        )
        if access_key and secret_key
        else UnavailableObjectStore()
    )
    return DocumentService(
        repository,
        store,
        embedder=embedder,
        commands=effective_commands,
        parser=parser or HttpDocumentParser(settings.document_parser_url),
        metrics=metrics,
    )


def processing_command(document_id: Any) -> OutboxCommand:
    return make_aggregate_command(
        "aura.knowledge.documents.process.v1",
        document_id,
        "document",
        document_id,
        identifiers=(("documentId", document_id),),
    )


class SqlDocumentCommandSink(DocumentCommandSink):
    """Stage identifier-only document commands in Core's durable outbox."""

    def __init__(self, sessions: Any) -> None:
        del sessions
        self.outbox = SqlOutboxRepository()

    async def publish(self, document_id: UUID, *, operation: str = "process") -> None:
        del document_id, operation
        raise RuntimeError("SQL document commands must be staged in the owning transaction")

    async def stage(
        self,
        session: Any,
        *,
        document_id: UUID,
        operation: str,
        version: int | None = None,
    ) -> None:
        topic = (
            DOCUMENT_PROCESSING_TOPIC
            if operation == "process"
            else "aura.knowledge.documents.delete.v1"
        )
        command_id = uuid5(
            DOCUMENT_ID_NAMESPACE,
            f"command:{document_id}:{operation}:{version if version is not None else 'unknown'}",
        )
        command = make_aggregate_command(
            topic,
            command_id,
            "knowledge.document",
            document_id,
            identifiers=(("documentId", document_id),),
        )
        await self.outbox.stage_command(session, command=command)

    async def stage_event(self, session: Any, *, event_type: str, document: Any) -> None:
        event_id = uuid5(
            DOCUMENT_ID_NAMESPACE,
            f"event:{document.id}:{document.version}:{event_type}",
        )
        correlation_id = uuid5(DOCUMENT_ID_NAMESPACE, f"correlation:{document.id}")
        event_payload = {
            "schemaVersion": 1,
            "eventId": str(event_id),
            "eventType": event_type,
            "documentId": str(document.id),
            "status": "deleted" if event_type == "document.deleted" else document.status.value,
            "documentVersion": document.version,
            "occurredAt": document.updated_at.isoformat(),
            "correlationId": str(correlation_id),
            "causationId": str(event_id),
        }
        event_command = make_event_command(
            "aura.knowledge.documents.events.v1", event_id, document.id, event_payload
        )
        session.add(
            DocumentLifecycleEventRow(
                event_id=event_id,
                document_id=document.id,
                document_version=document.version,
                event_type=event_type,
                occurred_at=document.updated_at,
                correlation_id=correlation_id,
                causation_id=event_id,
            )
        )
        await self.outbox.stage_command(session, command=event_command)
