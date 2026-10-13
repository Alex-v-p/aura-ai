"""Public application API for owner-private documents.

The module deliberately exposes value objects and use cases only.  SQL and
object-store adapters are composed by ``bootstrap.document_uow``.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
from collections.abc import AsyncIterable, AsyncIterator, Callable, Iterable
from dataclasses import InitVar, dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any, ClassVar, Protocol, cast
from uuid import UUID, uuid4, uuid5

from aura_core.domains.knowledge.documents.chunking import chunk_text
from aura_core.domains.knowledge.documents.parser import DocumentParseError, parse_document
from aura_core.domains.knowledge.documents.ports import (
    DocumentEmbedder,
    DocumentObjectStore,
    DocumentParser,
    DocumentRepository,
)
from aura_core.runtime.models.ports import ProviderTraceContext

MAX_UPLOAD_BYTES = 100_000_000
UPLOAD_SESSION_TTL = timedelta(minutes=15)
PROCESSING_LEASE = timedelta(minutes=5)
DOCUMENT_PROCESSING_TOPIC = "aura.knowledge.documents.process.v1"
DOCUMENT_ID_NAMESPACE = UUID("b5dd8d2f-1d29-4f24-8c5c-3957ad79d6b3")
_OWNER_RE = re.compile(r"^[^\x00\n\r]{1,1024}$")


class DocumentStatus(StrEnum):
    UPLOADING = "uploading"
    QUEUED = "queued"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    DELETING = "deleting"


class DocumentError(RuntimeError):
    pass


class DocumentValidationError(DocumentError, ValueError):
    pass


class DocumentNotFound(DocumentError):
    pass


class DocumentVersionConflict(DocumentError):
    pass


@dataclass(frozen=True, slots=True)
class Document:
    id: UUID
    issuer: str
    subject: str
    filename: str
    media_type: str
    byte_count: int
    digest: str
    object_key: str
    status: DocumentStatus
    version: int
    created_at: datetime
    updated_at: datetime
    failure_code: str | None = None
    extracted_text_length: int = 0
    chunk_count: int = 0
    duplicate: bool = False

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject

    @property
    def searchable(self) -> bool:
        return self.status is DocumentStatus.READY


@dataclass(frozen=True, slots=True)
class DocumentChunk:
    id: UUID
    document_id: UUID
    ordinal: int
    text: str
    locator: str
    digest: str
    embedding: tuple[float, ...] | None = None
    model_id: str | None = None
    model_revision: str | None = None
    model_digest: str | None = None


@dataclass(frozen=True, slots=True)
class DocumentSearchHit:
    evidence_id: UUID
    document_id: UUID
    filename: str
    excerpt: str
    locator: str
    score: float


@dataclass
class DocumentLifecycleEvent:
    """Identifier-only lifecycle payload for outbox and telemetry consumers."""

    document_id: UUID
    status: DocumentStatus
    version: int
    occurred_at: datetime
    event_type_input: InitVar[str | None] = None
    _registry: ClassVar[dict[UUID, UUID]] = {}

    def __post_init__(self, event_type_input: str | None) -> None:
        if event_type_input is not None:
            self.__dict__["_event_type"] = event_type_input
        # Keep only opaque event/document identifiers for in-process replay;
        # content and owner metadata never enter the registry.
        self._registry[self.event_id] = self.document_id

    @classmethod
    def document_for_event(cls, event_id: UUID) -> UUID | None:
        return cls._registry.get(event_id)

    @property
    def schema_version(self) -> int:
        return 1

    @property
    def event_id(self) -> UUID:
        return uuid5(
            DOCUMENT_ID_NAMESPACE,
            f"event:{self.document_id}:{self.version}:{self.event_type}",
        )

    @property
    def event_type(self) -> str:
        return str(getattr(self, "_event_type", f"document.{self.status.value}"))

    @property
    def correlation_id(self) -> UUID:
        return uuid5(DOCUMENT_ID_NAMESPACE, f"correlation:{self.document_id}")

    @property
    def causation_id(self) -> UUID:
        return uuid5(DOCUMENT_ID_NAMESPACE, f"causation:{self.event_id}")

    def payload(self) -> dict[str, object]:
        """Return the closed v1 wire shape without document content."""

        return {
            "schemaVersion": self.schema_version,
            "eventId": str(self.event_id),
            "eventType": self.event_type,
            "documentId": str(self.document_id),
            "status": (
                "deleted" if self.event_type == "document.deleted" else self.status.value
            ),
            "documentVersion": self.version,
            "occurredAt": self.occurred_at.isoformat(),
            "correlationId": str(self.correlation_id),
            "causationId": str(self.causation_id),
        }


@dataclass(frozen=True, slots=True)
class DocumentUploadSession:
    id: UUID
    document_id: UUID
    issuer: str
    subject: str
    filename: str
    media_type: str
    byte_count: int
    idempotency_key: str | None
    created_at: datetime
    expires_at: datetime
    version: int = 1

    @property
    def owner(self) -> tuple[str, str]:
        return self.issuer, self.subject


class DocumentEventSink(Protocol):
    async def emit(self, event: str, *, document: DocumentLifecycleEvent) -> None: ...


def _lifecycle_event(
    event_type: str, document_id: UUID, status: DocumentStatus, version: int, occurred_at: datetime
) -> DocumentLifecycleEvent:
    return DocumentLifecycleEvent(document_id, status, version, occurred_at, event_type)


class DocumentCommandSink(Protocol):
    async def publish(self, document_id: UUID, *, operation: str = "process") -> None: ...

    async def stage(
        self,
        session: object,
        *,
        document_id: UUID,
        operation: str,
        version: int | None = None,
    ) -> None: ...

    async def stage_event(
        self, session: object, *, event_type: str, document: Document
    ) -> None: ...


class DocumentMetrics(Protocol):
    def record_span(
        self, component: str, operation: str, duration_ms: float, **attributes: object
    ) -> None: ...

    def observe(
        self,
        component: str,
        name: str,
        value: float,
        *,
        trace_id: str | None = None,
        **attributes: str,
    ) -> None: ...

    def increment(
        self,
        component: str,
        name: str,
        *,
        trace_id: str | None = None,
        **attributes: str,
    ) -> None: ...


class MemoryDocumentRepository:
    """Deterministic adapter used for local composition and pure tests."""

    def __init__(self) -> None:
        self.documents: dict[UUID, Document] = {}
        self.chunk_rows: dict[UUID, list[DocumentChunk]] = {}
        self.idempotency: dict[tuple[str, str, str], UUID] = {}
        self.upload_sessions: dict[UUID, DocumentUploadSession] = {}
        self.claimed_upload_sessions: set[UUID] = set()
        self.completed_upload_sessions: set[UUID] = set()
        self.processing_claims: set[UUID] = set()
        self.tombstones: dict[UUID, tuple[datetime, int]] = {}
        self.command_receipts: dict[tuple[tuple[str, str], str, str], Document] = {}
        self.command_receipt_fingerprints: dict[tuple[tuple[str, str], str, str], str] = {}
        self._lock = asyncio.Lock()

    async def create_upload(
        self,
        document: Document,
        *,
        idempotency_key: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> Document:
        existing = self.documents.get(document.id)
        if existing is not None and (
            existing.status is not DocumentStatus.UPLOADING
            or existing.version != document.version
        ):
            raise DocumentVersionConflict("upload placeholder changed")
        if idempotency_key:
            self.idempotency[(document.issuer, document.subject, idempotency_key)] = document.id
        self.documents[document.id] = document
        if idempotency_key:
            self.command_receipts[(document.owner, "upload", idempotency_key)] = document
            self.command_receipt_fingerprints[(document.owner, "upload", idempotency_key)] = (
                receipt_fingerprint if receipt_fingerprint is not None else str(document.id)
            )
        return document

    async def create_upload_session(self, value: DocumentUploadSession) -> None:
        self.upload_sessions[value.id] = value
        self.documents.setdefault(
            value.document_id,
            Document(
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
                value.created_at,
                value.created_at,
            ),
        )

    async def get_upload_session(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> DocumentUploadSession | None:
        session = self.upload_sessions.get(upload_id)
        return (
            session
            if session is not None
            and upload_id not in self.completed_upload_sessions
            and session.owner == owner
            else None
        )

    async def get_upload_session_any(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> DocumentUploadSession | None:
        session = self.upload_sessions.get(upload_id)
        return session if session is not None and session.owner == owner else None

    async def find_upload_session(
        self, owner: tuple[str, str], idempotency_key: str
    ) -> DocumentUploadSession | None:
        return next(
            (
                session
                for session in self.upload_sessions.values()
                if session.owner == owner and session.idempotency_key == idempotency_key
            ),
            None,
        )

    async def consume_upload_session(
        self, owner: tuple[str, str], upload_id: UUID, *, expected_version: int
    ) -> DocumentUploadSession | None:
        async with self._lock:
            session = await self.get_upload_session(owner, upload_id)
            if (
                session is None
                or session.version != expected_version
                or upload_id in self.claimed_upload_sessions
                or upload_id in self.completed_upload_sessions
            ):
                return None
            self.claimed_upload_sessions.add(upload_id)
            return session

    async def release_upload_session(self, upload_id: UUID, *, completed: bool) -> None:
        self.claimed_upload_sessions.discard(upload_id)
        if completed:
            self.completed_upload_sessions.add(upload_id)
        else:
            self.completed_upload_sessions.discard(upload_id)

    async def mark_upload_cleanup(
        self, document_id: UUID, object_key: str, *, command_sink: DocumentCommandSink | None
    ) -> None:
        async with self._lock:
            current = self.documents.get(document_id)
            if current is None or current.status is not DocumentStatus.UPLOADING:
                return
            updated = replace(
                current,
                object_key=object_key,
                status=DocumentStatus.DELETING,
                version=current.version + 1,
                updated_at=datetime.now(UTC),
                failure_code="upload_cleanup_retryable",
            )
            self.documents[document_id] = updated
            for upload_id, session in tuple(self.upload_sessions.items()):
                if session.document_id == document_id:
                    self.upload_sessions.pop(upload_id, None)
                    self.completed_upload_sessions.discard(upload_id)
            if command_sink is not None:
                await command_sink.publish(document_id, operation="delete")

    async def claim_stale_upload(
        self, document_id: UUID, before: object, *, command_sink: DocumentCommandSink | None
    ) -> bool:
        if not isinstance(before, datetime):
            return False
        async with self._lock:
            current = self.documents.get(document_id)
            if current is None or current.status is not DocumentStatus.UPLOADING:
                return False
            sessions = [
                session
                for session in self.upload_sessions.values()
                if session.document_id == document_id
                and session.id not in self.claimed_upload_sessions
                and session.expires_at < before
            ]
            if not sessions:
                return False
            updated = replace(
                current,
                status=DocumentStatus.DELETING,
                version=current.version + 1,
                updated_at=datetime.now(UTC),
                failure_code="upload_cleanup_retryable",
            )
            self.documents[document_id] = updated
            for session in sessions:
                self.upload_sessions.pop(session.id, None)
                self.completed_upload_sessions.discard(session.id)
            if command_sink is not None:
                await command_sink.publish(document_id, operation="delete")
            return True

    async def reserve_upload_object(self, document_id: UUID, object_key: str) -> None:
        async with self._lock:
            current = self.documents.get(document_id)
            if current is not None and current.status is DocumentStatus.UPLOADING:
                self.documents[document_id] = replace(
                    current, object_key=object_key, updated_at=datetime.now(UTC)
                )
            elif current is not None:
                raise DocumentVersionConflict("upload cleanup already claimed")

    async def stale_uploads(self, before: object, *, limit: int = 100) -> list[Document]:
        if not isinstance(before, datetime):
            return []
        return [
            item
            for item in sorted(
                self.documents.values(), key=lambda value: (value.updated_at, value.id)
            )
            if item.status is DocumentStatus.UPLOADING and item.updated_at < before
            and any(
                session.document_id == item.id
                and session.id not in self.claimed_upload_sessions
                and session.expires_at < before
                for session in self.upload_sessions.values()
            )
        ][:limit]

    async def get(self, owner: tuple[str, str], document_id: UUID) -> Document | None:
        item = self.documents.get(document_id)
        if item is not None:
            return item if item.owner == owner else None
        if document_id in self.tombstones:
            deleted_at, final_version = self.tombstones[document_id]
            return Document(
                document_id,
                owner[0],
                owner[1],
                "",
                "application/octet-stream",
                0,
                "",
                "",
                DocumentStatus.DELETING,
                final_version,
                deleted_at,
                deleted_at,
            )
        return None

    async def get_any(self, document_id: UUID) -> Document | None:
        item = self.documents.get(document_id)
        if item is not None:
            return item
        tombstone = self.tombstones.get(document_id)
        if tombstone is None:
            return None
        deleted_at, final_version = tombstone
        return Document(
            document_id,
            "",
            "",
            "",
            "application/octet-stream",
            0,
            "",
            "",
            DocumentStatus.DELETING,
            final_version,
            deleted_at,
            deleted_at,
        )

    async def list(
        self,
        owner: tuple[str, str],
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        media_type: str | None = None,
    ) -> list[Document]:
        return sorted(
            (
                item
                for item in self.documents.values()
                if item.owner == owner
                and (status is None or item.status.value == status)
                and (media_type is None or item.media_type == media_type)
            ),
            key=lambda item: (item.created_at, item.id),
            reverse=True,
        )[offset : offset + limit]

    async def find_duplicate(self, owner: tuple[str, str], digest: str) -> Document | None:
        return next(
            (
                item
                for item in self.documents.values()
                if item.owner == owner
                and item.digest == digest
                and item.status is not DocumentStatus.DELETING
            ),
            None,
        )

    async def find_idempotency(self, owner: tuple[str, str], key: str) -> Document | None:
        document_id = self.idempotency.get((owner[0], owner[1], key))
        return self.documents.get(document_id) if document_id is not None else None

    async def find_command_receipt(
        self, owner: tuple[str, str], operation: str, idempotency_key: str
    ) -> Document | None:
        return self.command_receipts.get((owner, operation, idempotency_key))

    async def record_command_receipt(
        self,
        owner: tuple[str, str],
        operation: str,
        idempotency_key: str,
        document: Document,
        *,
        fingerprint: str,
    ) -> None:
        self.command_receipts[(owner, operation, idempotency_key)] = document
        self.command_receipt_fingerprints[(owner, operation, idempotency_key)] = fingerprint

    async def find_command_receipt_fingerprint(
        self, owner: tuple[str, str], operation: str, idempotency_key: str
    ) -> str | None:
        return self.command_receipt_fingerprints.get((owner, operation, idempotency_key))

    async def resolve_lifecycle_event(self, event_id: UUID) -> tuple[UUID, int, str] | None:
        document_id = DocumentLifecycleEvent.document_for_event(event_id)
        return (document_id, 0, "") if document_id is not None else None

    async def save(self, document: Document, *, expected_version: int | None = None) -> Document:
        old = self.documents.get(document.id)
        if old is None:
            raise DocumentNotFound("document not found")
        if expected_version is not None and old.version != expected_version:
            raise DocumentVersionConflict("document version conflict")
        self.documents[document.id] = document
        return document

    async def replace_chunks(self, document_id: UUID, chunks: list[DocumentChunk]) -> None:
        self.chunk_rows[document_id] = list(chunks)

    async def chunks(self, document_id: UUID) -> list[DocumentChunk]:
        return list(self.chunk_rows.get(document_id, ()))

    async def delete_chunks(self, document_id: UUID) -> None:
        self.chunk_rows.pop(document_id, None)

    async def claim_processing(
        self, document_id: UUID, *, command_sink: DocumentCommandSink | None = None
    ) -> Document | None:
        del command_sink
        async with self._lock:
            document = self.documents.get(document_id)
            if document is None:
                return None
            now = datetime.now(UTC)
            reclaimable = (
                document.status is DocumentStatus.PROCESSING
                and now - document.updated_at >= PROCESSING_LEASE
            )
            if document.status is not DocumentStatus.QUEUED and not reclaimable:
                return None
            if document_id in self.processing_claims and not reclaimable:
                return None
            self.processing_claims.add(document_id)
            claimed = replace(
                document,
                status=DocumentStatus.PROCESSING,
                version=document.version + 1,
                updated_at=now,
            )
            self.documents[document_id] = claimed
            return claimed

    async def settle_ready(
        self,
        document: Document,
        chunks: list[DocumentChunk],
        *,
        expected_version: int,
    ) -> Document | None:
        async with self._lock:
            current = self.documents.get(document.id)
            if (
                current is None
                or current.status is not DocumentStatus.PROCESSING
                or current.version != expected_version
            ):
                self.processing_claims.discard(document.id)
                return None
            self.chunk_rows[document.id] = list(chunks)
            settled = replace(document, status=DocumentStatus.READY, version=expected_version + 1)
            self.documents[document.id] = settled
            self.processing_claims.discard(document.id)
            return settled

    async def settle_failed(
        self,
        document: Document,
        *,
        expected_version: int,
        failure_code: str,
        command_sink: DocumentCommandSink | None = None,
    ) -> Document | None:
        del command_sink
        async with self._lock:
            current = self.documents.get(document.id)
            if (
                current is None
                or current.status is not DocumentStatus.PROCESSING
                or current.version != expected_version
            ):
                self.processing_claims.discard(document.id)
                return None
            self.chunk_rows.pop(document.id, None)
            failed = replace(
                current,
                status=DocumentStatus.FAILED,
                version=expected_version + 1,
                updated_at=document.updated_at,
                failure_code=failure_code,
            )
            self.documents[document.id] = failed
            self.processing_claims.discard(document.id)
            return failed

    async def settle_deleted(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink | None = None,
    ) -> bool:
        del command_sink
        async with self._lock:
            current = self.documents.get(document.id)
            if current is None or current.version != expected_version:
                return False
            self.chunk_rows.pop(document.id, None)
            self.documents.pop(document.id, None)
            for key, receipt in tuple(self.command_receipts.items()):
                if receipt.id == document.id:
                    self.command_receipts.pop(key, None)
                    self.command_receipt_fingerprints.pop(key, None)
            for key, value in tuple(self.idempotency.items()):
                if value == document.id:
                    self.idempotency.pop(key, None)
            for upload_id, session in tuple(self.upload_sessions.items()):
                if session.document_id == document.id:
                    self.upload_sessions.pop(upload_id, None)
                    self.completed_upload_sessions.discard(upload_id)
            self.tombstones[document.id] = (datetime.now(UTC), expected_version)
            self.processing_claims.discard(document.id)
            return True

    async def transition_deleting(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> Document:
        del command_sink
        async with self._lock:
            current = self.documents.get(document.id)
            if (
                current is None
                or current.version != expected_version
                or current.status is DocumentStatus.DELETING
            ):
                raise DocumentVersionConflict("document version conflict")
            transitioned = replace(
                current,
                status=DocumentStatus.DELETING,
                version=current.version + 1,
                updated_at=datetime.now(UTC),
            )
            self.documents[document.id] = transitioned
            if idempotency_key:
                self.command_receipts[
                    (document.owner, "delete", idempotency_key)
                ] = transitioned
                self.command_receipt_fingerprints[
                    (document.owner, "delete", idempotency_key)
                ] = fingerprint or _mutation_fingerprint(document.id, expected_version)
            return transitioned

    async def transition_retry(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> Document:
        del command_sink
        async with self._lock:
            current = self.documents.get(document.id)
            if (
                current is None
                or current.version != expected_version
                or current.status is not DocumentStatus.FAILED
            ):
                raise DocumentVersionConflict("document version conflict")
            queued = replace(
                current,
                status=DocumentStatus.QUEUED,
                version=current.version + 1,
                updated_at=datetime.now(UTC),
                failure_code=None,
            )
            self.documents[document.id] = queued
            if idempotency_key:
                self.command_receipts[(document.owner, "retry", idempotency_key)] = queued
                self.command_receipt_fingerprints[
                    (document.owner, "retry", idempotency_key)
                ] = fingerprint or _mutation_fingerprint(document.id, expected_version)
            return queued

    async def search(
        self,
        owner: tuple[str, str],
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        media_type: str | None = None,
        query_embedding: tuple[float, ...] | None = None,
    ) -> list[DocumentSearchHit]:
        terms = set(query.casefold().split())
        hits: list[DocumentSearchHit] = []
        for document in await self.list(owner, limit=100_000, media_type=media_type):
            if not document.searchable:
                continue
            for chunk in self.chunk_rows.get(document.id, ()):
                words = set(chunk.text.casefold().split())
                overlap = len(terms & words)
                lexical = overlap / max(1, len(terms))
                vector = _cosine(query_embedding, chunk.embedding)
                if overlap or (query_embedding is not None and chunk.embedding is not None):
                    vector = max(vector, 0.001) if query_embedding is not None else vector
                    hits.append(
                        DocumentSearchHit(
                            chunk.id,
                            document.id,
                            document.filename,
                            chunk.text[:2_000],
                            chunk.locator,
                            0.65 * lexical + 0.35 * vector,
                        )
                    )
        return sorted(hits, key=lambda item: (-item.score, str(item.document_id)))[
            offset : offset + limit
        ]


class MemoryDocumentObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_stream(self, key: str, stream: AsyncIterable[bytes], *, content_type: str) -> int:
        del content_type
        pieces: list[bytes] = []
        total = 0
        async for piece in stream:
            total += len(piece)
            pieces.append(piece)
        self.objects[key] = b"".join(pieces)
        return total

    async def get(self, key: str) -> AsyncIterator[bytes]:
        data = self.objects.get(key)
        if data is None:
            raise DocumentNotFound("object not found")
        yield data

    async def delete(self, key: str) -> None:
        self.objects.pop(key, None)


async def _aiter_bytes(
    source: AsyncIterable[bytes] | Iterable[bytes] | bytes,
) -> AsyncIterator[bytes]:
    if isinstance(source, bytes):
        yield source
    elif hasattr(source, "__aiter__"):
        async for item in source:  # type: ignore[union-attr]
            yield item
    else:
        for item in source:  # type: ignore[union-attr]
            yield item


class DocumentService:
    """Owner-scoped document commands and queries."""

    def __init__(
        self,
        repository: DocumentRepository,
        object_store: DocumentObjectStore,
        *,
        embedder: DocumentEmbedder | None = None,
        embedding_model: str = "nomic-embed-text",
        events: DocumentEventSink | None = None,
        commands: DocumentCommandSink | None = None,
        parser: DocumentParser | None = None,
        metrics: DocumentMetrics | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.object_store = object_store
        self.embedder = embedder
        self.embedding_model = embedding_model
        self.events = events
        self.commands = commands
        self.parser = parser
        self.metrics = metrics
        self.clock = clock or (lambda: datetime.now(UTC))
        self.upload_sessions: dict[UUID, DocumentUploadSession] = {}

    def _record_metric(
        self,
        component: str,
        operation: str,
        started: float,
        *,
        outcome: str,
        dependency: str,
        document_id: UUID | None = None,
        error_class: str | None = None,
        chunks: int | None = None,
        candidates: int | None = None,
        retry: bool = False,
        context: ProviderTraceContext | None = None,
        degradation: str | None = None,
    ) -> None:
        metrics = self.metrics
        if metrics is None:
            return
        try:
            lineage = context or ProviderTraceContext()
            stable_seed = f"{component}:{operation}:{document_id or 'library'}"
            trace_id = lineage.trace_id or uuid5(DOCUMENT_ID_NAMESPACE, stable_seed).hex
            # The command context identifies the parent operation.  Derive a
            # stable child span for this use case so repeated deliveries retain
            # the same lineage without generating random identifiers.
            span_id = uuid5(
                DOCUMENT_ID_NAMESPACE, f"span:{trace_id}:{stable_seed}"
            ).hex[:16]
            span_kwargs: dict[str, object] = {
                "trace_id": trace_id,
                "span_id": span_id,
                "dependency": dependency,
                "outcome": outcome,
            }
            parent_span_id = lineage.span_id if lineage.span_id != span_id else None
            # MetadataMetrics requires this structural field even for a root
            # span; unlike lineage attributes, None is meaningful here.
            span_kwargs["parent_span_id"] = parent_span_id
            if error_class is not None:
                span_kwargs["error_class"] = error_class
            for key, value in (
                ("correlation_id", lineage.correlation_id),
                ("causation_id", lineage.causation_id),
                ("command_id", lineage.command_id),
            ):
                if value is not None:
                    span_kwargs[key] = value
            if degradation is not None:
                span_kwargs["degradation"] = degradation
            if document_id is not None:
                span_kwargs["document_id"] = str(document_id)
            metrics.record_span(
                component,
                operation,
                (monotonic() - started) * 1000,
                **span_kwargs,
            )
            metrics.observe(
                component,
                {
                    "aura.knowledge.document_upload": "document_upload_duration_ms",
                    "aura.knowledge.document_processing": "document_processing_duration_ms",
                    "aura.knowledge.document_retrieval": "document_retrieval_duration_ms",
                    "aura.knowledge.document_deletion": "document_deletion_duration_ms",
                }[component],
                (monotonic() - started) * 1000,
                trace_id=trace_id,
                outcome=outcome,
                dependency=dependency,
            )
            outcome_metric = {
                "aura.knowledge.document_upload": "document_upload_outcome",
                "aura.knowledge.document_processing": "document_processing_outcome",
                "aura.knowledge.document_deletion": "document_deletion_outcome",
            }.get(component)
            if outcome_metric is not None:
                metrics.increment(
                    component,
                    outcome_metric,
                    trace_id=trace_id,
                    outcome=outcome,
                    dependency=dependency,
                )
            if chunks is not None:
                metrics.observe(component, "document_chunk_count", chunks, trace_id=trace_id)
            if candidates is not None:
                metrics.observe(
                    component,
                    "document_retrieval_candidate_count",
                    candidates,
                    trace_id=trace_id,
                )
            if retry:
                metrics.increment(
                    component, "document_retry_count", trace_id=trace_id, outcome=outcome
                )
        except Exception:
            # Telemetry is explicitly non-blocking and must never alter a
            # document state transition.
            return

    def _owner(self, issuer: str, subject: str) -> tuple[str, str]:
        if not _OWNER_RE.fullmatch(issuer) or not _OWNER_RE.fullmatch(subject):
            raise DocumentValidationError("invalid owner identity")
        return issuer, subject

    async def upload(
        self,
        issuer: str,
        subject: str,
        *,
        filename: str,
        media_type: str,
        stream: AsyncIterable[bytes] | Iterable[bytes] | bytes,
        idempotency_key: str | None = None,
        expected_bytes: int | None = None,
        document_id: UUID | None = None,
        object_key: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> Document:
        owner = self._owner(issuer, subject)
        if (
            not filename
            or len(filename) > 255
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        ):
            raise DocumentValidationError("invalid filename")
        digest = hashlib.sha256()
        total = 0
        first = bytearray()

        async def bounded() -> AsyncIterator[bytes]:
            nonlocal total
            async for piece in _aiter_bytes(stream):
                total += len(piece)
                if total > MAX_UPLOAD_BYTES:
                    raise DocumentValidationError("upload exceeds 100000000 byte limit")
                if len(first) < 16:
                    first.extend(piece[: 16 - len(first)])
                digest.update(piece)
                yield piece

        # The object store receives the stream directly; Core retains only the
        # digest and a bounded signature prefix.  Every failure after the PUT
        # is paired with best-effort object cleanup so rejected uploads cannot
        # become unreachable private bytes.
        object_key = object_key or f"documents/{issuer}/{subject}/{uuid4().hex}"
        retained = False
        started = monotonic()
        outcome = "failed"
        try:
            await self.object_store.put_stream(object_key, bounded(), content_type=media_type)
            if expected_bytes is not None and total != expected_bytes:
                raise DocumentValidationError("content length does not match upload session")
            from aura_core.domains.knowledge.documents.parser import validate_format

            kind = validate_format(filename, media_type, bytes(first))
            content_digest = digest.hexdigest()
            if idempotency_key:
                prior = await self.repository.find_idempotency(owner, idempotency_key)
                if prior is not None:
                    if prior.digest != content_digest:
                        raise DocumentValidationError("idempotency key was already used")
                    outcome = "duplicate"
                    return replace(prior, duplicate=True)
            duplicate = await self.repository.find_duplicate(owner, content_digest)
            if duplicate is not None:
                outcome = "duplicate"
                return replace(duplicate, duplicate=True)
            now = self.clock()
            document = Document(
                document_id or uuid4(),
                issuer,
                subject,
                filename,
                kind,
                total,
                content_digest,
                object_key,
                DocumentStatus.QUEUED,
                1,
                now,
                now,
            )
            try:
                create_kwargs: dict[str, object] = {"idempotency_key": idempotency_key}
                if receipt_fingerprint is not None and getattr(
                    self.repository, "transactional_outbox", False
                ):
                    create_kwargs["receipt_fingerprint"] = receipt_fingerprint
                await cast(Any, self.repository).create_upload(document, **create_kwargs)
            except Exception:
                prior = await self.repository.find_idempotency(owner, idempotency_key or "")
                if prior is None:
                    prior = await self.repository.find_duplicate(owner, content_digest)
                if prior is not None:
                    return replace(prior, duplicate=True)
                raise
            retained = True
            if self.commands is not None and not getattr(
                self.repository, "transactional_outbox", False
            ):
                await self.commands.publish(document.id)
            if self.events is not None and not getattr(
                self.repository, "transactional_outbox", False
            ):
                await self.events.emit(
                    "document.queued",
                    document=_lifecycle_event(
                        "document.queued", document.id, document.status, document.version, now
                    ),
                )
            outcome = "accepted"
            return document
        finally:
            # The retained document is the only object that keeps this key.
            # Duplicate, validation, and persistence failures must remove it.
            if not retained:
                try:
                    await self.object_store.delete(object_key)
                except Exception:
                    if document_id is not None:
                        try:
                            await self.repository.mark_upload_cleanup(
                                document_id, object_key, command_sink=self.commands
                            )
                        except Exception:
                            # A failed durable cleanup handoff is itself a
                            # dependency failure and must remain visible.
                            raise
            self._record_metric(
                "aura.knowledge.document_upload",
                "document.upload",
                started,
                outcome=outcome,
                dependency="object_storage",
                document_id=document_id,
            )

    async def create_upload_session(
        self,
        issuer: str,
        subject: str,
        *,
        filename: str,
        media_type: str,
        # The transport contract requires this field.  A one-byte default is
        # retained for callers of the public service API that predate the
        # upload-session contract; zero is never accepted.
        byte_count: int = 1,
        idempotency_key: str | None = None,
    ) -> DocumentUploadSession:
        self._owner(issuer, subject)
        if (
            not filename
            or len(filename) > 255
            or "/" in filename
            or "\\" in filename
            or any(ord(character) < 32 or ord(character) == 127 for character in filename)
        ):
            raise DocumentValidationError("invalid filename")
        if byte_count < 1 or byte_count > MAX_UPLOAD_BYTES:
            raise DocumentValidationError("invalid upload size")
        from aura_core.domains.knowledge.documents.parser import validate_format

        signature = (
            b"%PDF-"
            if filename.casefold().endswith(".pdf")
            else b"PK"
            if filename.casefold().endswith(".docx")
            else b""
        )
        validate_format(filename, media_type, signature)
        if idempotency_key:
            existing = await self.repository.find_upload_session((issuer, subject), idempotency_key)
            if existing is not None:
                if self.clock() >= existing.expires_at:
                    active = await self.repository.get_upload_session(
                        (issuer, subject), existing.id
                    )
                    if active is not None:
                        placeholder = await self.repository.get_any(existing.document_id)
                        await self.repository.mark_upload_cleanup(
                            existing.document_id,
                            placeholder.object_key if placeholder is not None else "",
                            command_sink=self.commands,
                        )
                        existing = await self.repository.find_upload_session(
                            (issuer, subject), idempotency_key
                        )
                if existing is not None and (
                    existing.filename != filename
                    or existing.media_type != media_type
                    or existing.byte_count != byte_count
                ):
                    raise DocumentValidationError("idempotency key was already used")
                if existing is not None:
                    self.upload_sessions[existing.id] = existing
                    return existing
        created_at = self.clock()
        session = DocumentUploadSession(
            uuid4(),
            uuid4(),
            issuer,
            subject,
            filename,
            media_type,
            byte_count,
            idempotency_key,
            created_at,
            created_at + UPLOAD_SESSION_TTL,
        )
        self.upload_sessions[session.id] = session
        try:
            if getattr(self.repository, "transactional_outbox", False):
                await cast(Any, self.repository).create_upload_session(
                    session, command_sink=self.commands
                )
            else:
                await self.repository.create_upload_session(session)
            if self.events is not None and not getattr(
                self.repository, "transactional_outbox", False
            ):
                await self.events.emit(
                    "document.upload.created",
                    document=_lifecycle_event(
                        "document.upload.created",
                        session.document_id,
                        DocumentStatus.UPLOADING,
                        session.version,
                        session.created_at,
                    ),
                )
        except Exception:
            if idempotency_key:
                existing = await self.repository.find_upload_session(
                    (issuer, subject), idempotency_key
                )
                if existing is not None:
                    if (
                        existing.filename == filename
                        and existing.media_type == media_type
                        and existing.byte_count == byte_count
                    ):
                        self.upload_sessions[existing.id] = existing
                        return existing
            self.upload_sessions.pop(session.id, None)
            raise
        return session

    async def complete_upload(
        self,
        upload_id: UUID,
        stream: AsyncIterable[bytes] | Iterable[bytes] | bytes,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        content_type: str | None = None,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Document:
        owner = self._owner(issuer, subject) if issuer is not None and subject is not None else None
        replay_session = None
        if owner is not None:
            replay_session = self.upload_sessions.get(upload_id)
            if replay_session is None:
                replay_session = await cast(Any, self.repository).get_upload_session_any(
                    owner, upload_id
                )
        if owner is not None and idempotency_key:
            prior = await self.repository.find_command_receipt(owner, "upload", idempotency_key)
            if prior is not None:
                fingerprint = await self.repository.find_command_receipt_fingerprint(
                    owner, "upload", idempotency_key
                )
                if (
                    fingerprint is None
                    or replay_session is None
                    or replay_session.id != upload_id
                    or expected_version is None
                    or fingerprint != f"{upload_id}:{expected_version}"
                    or replay_session.document_id != prior.id
                ):
                    raise DocumentVersionConflict("idempotency fingerprint conflict")
                return replace(prior, duplicate=True)
        session = self.upload_sessions.get(upload_id)
        if session is None and issuer is not None and subject is not None:
            session = await self.repository.get_upload_session((issuer, subject), upload_id)
        if session is None:
            raise DocumentNotFound("upload session not found")
        if self.clock() >= session.expires_at:
            self.upload_sessions.pop(upload_id, None)
            raise DocumentNotFound("upload session not found")
        if issuer is not None and subject is not None and session.owner != (issuer, subject):
            raise DocumentNotFound("upload session not found")
        if (
            content_type is not None
            and content_type.split(";", 1)[0].strip().casefold()
            != session.media_type.split(";", 1)[0].strip().casefold()
        ):
            raise DocumentValidationError("content type does not match upload session")
        if expected_version is not None and expected_version != session.version:
            raise DocumentVersionConflict("upload session version conflict")
        if isinstance(stream, bytes) and len(stream) != session.byte_count:
            raise DocumentValidationError("content length does not match upload session")
        claimed = await self.repository.consume_upload_session(
            session.owner, upload_id, expected_version=expected_version or session.version
        )
        if claimed is None:
            raise DocumentVersionConflict("upload session is already being completed")
        object_key = f"documents/{session.issuer}/{session.subject}/{uuid4().hex}"
        try:
            await self.repository.reserve_upload_object(session.document_id, object_key)
            document = await self.upload(
                session.issuer,
                session.subject,
                filename=session.filename,
                media_type=session.media_type,
                stream=stream,
                idempotency_key=idempotency_key or session.idempotency_key,
                expected_bytes=session.byte_count,
                document_id=session.document_id,
                object_key=object_key,
                receipt_fingerprint=f"{upload_id}:{expected_version or session.version}",
            )
        except Exception:
            pending = await self.repository.get_any(session.document_id)
            await self.repository.release_upload_session(
                upload_id,
                completed=bool(pending and pending.status is DocumentStatus.DELETING),
            )
            raise
        self.upload_sessions.pop(upload_id, None)
        await self.repository.release_upload_session(upload_id, completed=True)
        if issuer is not None and subject is not None and idempotency_key:
            if not getattr(self.repository, "transactional_outbox", False):
                await self.repository.record_command_receipt(
                    (issuer, subject),
                    "upload",
                    idempotency_key,
                    document,
                    fingerprint=(
                        f"{upload_id}:{expected_version or session.version}"
                    ),
                )
        return document

    async def recover_stale_uploads(self, *, limit: int = 100) -> int:
        """Turn crashed upload placeholders into durable cleanup commands."""

        before = self.clock()
        stale = await cast(Any, self.repository).stale_uploads(before, limit=limit)
        recovered = 0
        for document in stale:
            claim = getattr(self.repository, "claim_stale_upload", None)
            if callable(claim):
                recovered += int(
                    await cast(Callable[..., Any], claim)(
                        document.id, before, command_sink=self.commands
                    )
                )
            else:
                # Adapters predating the atomic claim seam still get the
                # conservative cleanup path; production adapters implement
                # claim_stale_upload and re-check the session in one tx.
                await self.repository.mark_upload_cleanup(
                    document.id, document.object_key, command_sink=self.commands
                )
                recovered += 1
        return recovered

    async def resolve_lifecycle_event(self, event_id: UUID) -> UUID | None:
        resolver = getattr(self.repository, "resolve_lifecycle_event", None)
        if callable(resolver):
            typed_resolver = cast(
                Callable[[UUID], Any], resolver
            )
            resolved = await typed_resolver(event_id)
            if resolved is not None:
                return resolved[0]
        return DocumentLifecycleEvent.document_for_event(event_id)

    async def get(self, issuer: str, subject: str, document_id: UUID) -> Document:
        item = await self.repository.get(self._owner(issuer, subject), document_id)
        if item is None:
            raise DocumentNotFound("document not found")
        chunks = await self.repository.chunks(document_id)
        return replace(item, chunk_count=len(chunks))  # type: ignore[arg-type]

    async def list(
        self,
        issuer: str,
        subject: str,
        *,
        limit: int = 100,
        offset: int = 0,
        status: str | None = None,
        media_type: str | None = None,
    ) -> list[Document]:
        repository = cast(Any, self.repository)
        kwargs: dict[str, object] = {
            "limit": limit,
            "status": status,
            "media_type": media_type,
        }
        if offset:
            kwargs["offset"] = offset
        return [
            item
            for item in await repository.list(self._owner(issuer, subject), **kwargs)
        ]  # type: ignore[misc]

    async def search(
        self,
        issuer: str,
        subject: str,
        query: str,
        *,
        limit: int = 20,
        offset: int = 0,
        media_type: str | None = None,
        query_embedding: tuple[float, ...] | None = None,
        context: ProviderTraceContext | None = None,
    ) -> list[DocumentSearchHit]:
        started = monotonic()
        if not query.strip() or len(query) > 500:
            raise DocumentValidationError("invalid search query")
        effective_embedding = query_embedding
        embedding_degraded = False
        if self.embedder is not None:
            try:
                result = await self.embedder.embed(
                    self.embedding_model,
                    query,
                    context=context or _library_context("document-search"),
                )
                effective_embedding = result.vector
            except Exception:
                # Lexical retrieval remains available during embedding-provider
                # outage; the failure is represented only by bounded telemetry.
                effective_embedding = None
                embedding_degraded = True
        repository = cast(Any, self.repository)
        kwargs: dict[str, object] = {
            "limit": limit,
            "media_type": media_type,
            "query_embedding": effective_embedding,
        }
        if offset:
            kwargs["offset"] = offset
        result = [
            item
            for item in await repository.search(self._owner(issuer, subject), query, **kwargs)
        ]  # type: ignore[misc]
        self._record_metric(
            "aura.knowledge.document_retrieval",
            "document.search",
            started,
            outcome="degraded" if embedding_degraded else "ok",
            dependency="embedding_provider" if embedding_degraded else "postgresql",
            error_class="embedding_unavailable" if embedding_degraded else None,
            degradation="embedding_unavailable" if embedding_degraded else None,
            candidates=len(result),
            context=context or _library_context("document-search"),
        )
        return result

    async def process(
        self, document_id: UUID, *, context: ProviderTraceContext | None = None
    ) -> Document:
        # Claiming is an atomic status transition.  A redelivered command can
        # never run a second parser concurrently or settle content after a
        # delete has claimed the document.
        started = monotonic()
        current = await self._get_by_id(document_id)
        if current.status in {DocumentStatus.READY, DocumentStatus.DELETING}:
            self._record_metric(
                "aura.knowledge.document_processing",
                "document.process",
                started,
                outcome="duplicate",
                dependency="postgresql",
                document_id=document_id,
                context=context,
            )
            return current
        if getattr(self.repository, "transactional_outbox", False):
            document = await cast(Any, self.repository).claim_processing(
                document_id, command_sink=self.commands
            )
        else:
            document = await self.repository.claim_processing(document_id)
        if document is None:
            return await self._get_by_id(document_id)
        processing_version = document.version
        try:
            data = bytearray()
            async for piece in self.object_store.get(document.object_key):
                data.extend(piece)
                if len(data) > MAX_UPLOAD_BYTES:
                    raise DocumentValidationError("stored object exceeds upload limit")
            if self.parser is None:
                parsed = parse_document(
                    bytes(data), filename=document.filename, media_type=document.media_type
                )
            else:
                try:
                    parsed = await self.parser.parse(
                        bytes(data), filename=document.filename, media_type=document.media_type
                    )
                except DocumentParseError:
                    raise
                except Exception as exc:
                    raise DocumentParseError("document parser failed") from exc
            text_chunks = chunk_text(parsed.text)
            rows: list[DocumentChunk] = []
            for item in text_chunks:
                embedding = (
                    await self.embedder.embed(
                        self.embedding_model,
                        item.text,
                        context=_document_context(context, document.id),
                    )
                    if self.embedder
                    else None
                )
                rows.append(
                    DocumentChunk(
                        uuid5(document.id, f"chunk:{item.ordinal}"),
                        document.id,
                        item.ordinal,
                        item.text,
                        item.locator,
                        hashlib.sha256(item.text.encode()).hexdigest(),
                        embedding.vector if embedding else None,
                        embedding.model_id if embedding else None,
                        embedding.model_revision if embedding else None,
                        embedding.model_digest if embedding else None,
                    )
                )
            ready_document = replace(
                document,
                updated_at=self.clock(),
                extracted_text_length=len(parsed.text),
                failure_code=None,
            )
            if getattr(self.repository, "transactional_outbox", False):
                settled = await cast(Any, self.repository).settle_ready(
                    ready_document,
                    rows,
                    expected_version=processing_version,
                    command_sink=self.commands,
                )
            else:
                settled = await self.repository.settle_ready(
                    ready_document, rows, expected_version=processing_version
                )
            if settled is None:
                return await self._get_by_id(document.id)
            document = settled
        except Exception as exc:
            # A parser or embedding failure must never leave a partially
            # written chunk set visible to a later retry.
            try:
                await self.repository.delete_chunks(document.id)
            except Exception:
                pass
            try:
                if hasattr(self.repository, "settle_failed"):
                    if getattr(self.repository, "transactional_outbox", False):
                        failed = await cast(Any, self.repository).settle_failed(
                            document,
                            expected_version=processing_version,
                            failure_code=_error_code(exc),
                            command_sink=self.commands,
                        )
                    else:
                        failed = await cast(Any, self.repository).settle_failed(
                            document,
                            expected_version=processing_version,
                            failure_code=_error_code(exc),
                        )
                else:
                    failed = await self.repository.save(
                        replace(
                            document,
                            status=DocumentStatus.FAILED,
                            version=processing_version + 1,
                            updated_at=self.clock(),
                            failure_code=_error_code(exc),
                        ),
                        expected_version=processing_version,
                    )
                if failed is None:
                    return await self._get_by_id(document.id)
                document = failed
            except DocumentVersionConflict:
                return await self._get_by_id(document.id)
        if self.events is not None and not getattr(
            self.repository, "transactional_outbox", False
        ):
            await self.events.emit(
                "document.ready" if document.status is DocumentStatus.READY else "document.failed",
                document=_lifecycle_event(
                    (
                        "document.ready"
                        if document.status is DocumentStatus.READY
                        else "document.failed"
                    ),
                    document.id,
                    document.status,
                    document.version,
                    document.updated_at,
                ),
            )
        self._record_metric(
            "aura.knowledge.document_processing",
            "document.process",
            started,
            outcome="ok" if document.status is DocumentStatus.READY else "failed",
            dependency=_processing_dependency(document.failure_code),
            error_class=(
                None
                if document.status is DocumentStatus.READY
                else document.failure_code
            ),
            document_id=document.id,
            context=context,
            chunks=len(await self.repository.chunks(document.id))
            if document.status is DocumentStatus.READY
            else None,
        )
        return document

    async def delete(
        self,
        issuer: str,
        subject: str,
        document_id: UUID,
        *,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Document:
        started = monotonic()
        owner = self._owner(issuer, subject)
        if idempotency_key:
            prior = await self.repository.find_command_receipt(owner, "delete", idempotency_key)
            if prior is not None:
                fingerprint = await self.repository.find_command_receipt_fingerprint(
                    owner, "delete", idempotency_key
                )
                if not _receipt_matches(fingerprint, document_id, expected_version):
                    raise DocumentVersionConflict("idempotency fingerprint conflict")
                return prior
        document = await self.get(issuer, subject, document_id)
        staged = False
        if self.commands is not None:
            document = await self.repository.transition_deleting(
                document,
                expected_version=expected_version or document.version,
                command_sink=self.commands,
                idempotency_key=idempotency_key,
                fingerprint=_mutation_fingerprint(document.id, expected_version, document.version),
            )
            staged = True
        else:
            document = await self.repository.save(
                replace(
                    document,
                    status=DocumentStatus.DELETING,
                    version=document.version + 1,
                    updated_at=self.clock(),
                ),
                expected_version=expected_version,
            )
        if self.commands is not None and not staged:
            await self.commands.publish(document.id, operation="delete")
        if self.events is not None and not staged:
            await self.events.emit(
                "document.deleting",
                document=_lifecycle_event(
                    "document.deleting",
                    document.id,
                    document.status,
                    document.version,
                    document.updated_at,
                ),
            )
        self._record_metric(
            "aura.knowledge.document_deletion",
            "document.delete",
            started,
            outcome="accepted",
            dependency="postgresql",
            document_id=document.id,
        )
        if idempotency_key and not getattr(self.repository, "transactional_outbox", False):
            await self.repository.record_command_receipt(
                owner,
                "delete",
                idempotency_key,
                document,
                fingerprint=_mutation_fingerprint(
                    document.id, expected_version, max(1, document.version - 1)
                ),
            )
        return document

    async def download(self, issuer: str, subject: str, document_id: UUID) -> AsyncIterator[bytes]:
        document = await self.get(issuer, subject, document_id)
        if document.status is DocumentStatus.DELETING:
            raise DocumentNotFound("document not found")
        return self.object_store.get(document.object_key)

    async def delete_by_id(
        self, document_id: UUID, *, context: ProviderTraceContext | None = None
    ) -> Document:
        """Worker-only deletion settlement for an already authorized tombstone."""

        started = monotonic()
        document = await self._get_by_id(document_id)
        if (
            document.status is DocumentStatus.DELETING
            and not document.object_key
            and not document.issuer
            and not document.subject
        ):
            # A successfully settled tombstone is an idempotent worker ACK.
            self._record_metric(
                "aura.knowledge.document_deletion",
                "document.delete",
                started,
                outcome="duplicate",
                dependency="postgresql",
                document_id=document.id,
                context=context,
            )
            return document
        if document.status is not DocumentStatus.DELETING:
            document = await self.repository.save(
                replace(
                    document,
                    status=DocumentStatus.DELETING,
                    version=document.version + 1,
                    updated_at=self.clock(),
                ),
                expected_version=document.version,
            )
        try:
            if document.object_key:
                await self.object_store.delete(document.object_key)
            if getattr(self.repository, "transactional_outbox", False):
                settled = await cast(Any, self.repository).settle_deleted(
                    document,
                    expected_version=document.version,
                    command_sink=self.commands,
                )
            else:
                settled = await self.repository.settle_deleted(
                    document, expected_version=document.version
                )
            if settled:
                if self.events is not None and not getattr(
                    self.repository, "transactional_outbox", False
                ):
                    await self.events.emit(
                        "document.deleted",
                        document=_lifecycle_event(
                            "document.deleted",
                            document.id,
                            document.status,
                            document.version,
                            document.updated_at,
                        ),
                    )
                tombstone = await self.repository.get_any(document.id)
                if tombstone is not None:
                    document = tombstone
        except Exception as exc:
            failed = await self.repository.save(
                replace(
                    document,
                    failure_code="delete_retryable",
                    version=document.version + 1,
                    updated_at=self.clock(),
                ),
                expected_version=document.version,
            )
            self._record_metric(
                "aura.knowledge.document_deletion",
                "document.delete",
                started,
                outcome="failed",
                dependency="object_storage",
                error_class=_error_code(exc),
                document_id=document.id,
                context=context,
            )
            return failed
        self._record_metric(
            "aura.knowledge.document_deletion",
            "document.delete",
            started,
            outcome="ok",
            dependency="object_storage",
            document_id=document.id,
            context=context,
        )
        return document

    async def retry(
        self,
        issuer: str,
        subject: str,
        document_id: UUID,
        *,
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> Document:
        started = monotonic()
        owner = self._owner(issuer, subject)
        if idempotency_key:
            prior = await self.repository.find_command_receipt(owner, "retry", idempotency_key)
            if prior is not None:
                fingerprint = await self.repository.find_command_receipt_fingerprint(
                    owner, "retry", idempotency_key
                )
                if not _receipt_matches(fingerprint, document_id, expected_version):
                    raise DocumentVersionConflict("idempotency fingerprint conflict")
                return prior
        document = await self.get(issuer, subject, document_id)
        if document.status is not DocumentStatus.FAILED:
            raise DocumentValidationError("only failed documents can be retried")
        if self.commands is not None and getattr(self.repository, "transactional_outbox", False):
            queued = await self.repository.transition_retry(
                document,
                expected_version=expected_version or document.version,
                command_sink=self.commands,
                idempotency_key=idempotency_key,
                fingerprint=_mutation_fingerprint(document.id, expected_version, document.version),
            )
        else:
            queued = await self.repository.save(
                replace(
                    document,
                    status=DocumentStatus.QUEUED,
                    version=document.version + 1,
                    updated_at=self.clock(),
                    failure_code=None,
                ),
                expected_version=expected_version,
            )
        if self.commands is not None and not getattr(
            self.repository, "transactional_outbox", False
        ):
            await self.commands.publish(queued.id, operation="process")
        self._record_metric(
            "aura.knowledge.document_processing",
            "document.retry",
            started,
            outcome="accepted",
            dependency="postgresql",
            document_id=queued.id,
            retry=True,
        )
        if idempotency_key and not getattr(self.repository, "transactional_outbox", False):
            await self.repository.record_command_receipt(
                owner,
                "retry",
                idempotency_key,
                queued,
                fingerprint=_mutation_fingerprint(
                    queued.id, expected_version, max(1, queued.version - 1)
                ),
            )
        return queued

    async def _get_by_id(self, document_id: UUID) -> Document:
        item = await self.repository.get_any(document_id)
        if item is not None:
            return item
        raise DocumentNotFound("document not found")


async def awaitable_iterator(value: AsyncIterator[bytes]) -> AsyncIterator[bytes]:
    async for item in value:
        yield item


def _error_code(error: BaseException) -> str:
    if isinstance(error, DocumentParseError):
        return "parse_failed"
    if isinstance(error, DocumentValidationError):
        return "validation_failed"
    if error.__class__.__name__.casefold().endswith("objectstoreerror"):
        return "object_storage_failed"
    if "timeout" in type(error).__name__.casefold():
        return "timeout"
    return "processing_failed"


def _mutation_fingerprint(
    document_id: UUID, expected_version: int | None, fallback_version: int | None = None
) -> str:
    version = expected_version if expected_version is not None else fallback_version
    return f"{document_id}:{version if version is not None else 'auto'}"


def _receipt_matches(
    fingerprint: str | None, document_id: UUID, expected_version: int | None
) -> bool:
    if fingerprint is None:
        return False
    prefix = f"{document_id}:"
    if not fingerprint.startswith(prefix):
        return False
    return expected_version is None or fingerprint == f"{prefix}{expected_version}"


def _processing_dependency(failure_code: str | None) -> str:
    if failure_code == "parse_failed":
        return "parser"
    if failure_code == "object_storage_failed":
        return "object_storage"
    return "embedding_provider"


def _document_context(
    context: ProviderTraceContext | None, document_id: UUID
) -> ProviderTraceContext:
    return replace(context or ProviderTraceContext(), job_id=str(document_id))


def _library_context(operation: str) -> ProviderTraceContext:
    """Create stable UUID lineage for API operations without a command envelope."""

    command_id = str(uuid5(DOCUMENT_ID_NAMESPACE, f"command:{operation}"))
    return ProviderTraceContext(
        trace_id=command_id.replace("-", ""),
        span_id=command_id.replace("-", "")[:16],
        command_id=command_id,
        correlation_id=command_id,
        causation_id=command_id,
    )


def _cosine(
    left: tuple[float, ...] | None, right: tuple[float, ...] | None
) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    denominator = math.sqrt(sum(item * item for item in left)) * math.sqrt(
        sum(item * item for item in right)
    )
    if not denominator:
        return 0.0
    return max(
        0.0,
        min(1.0, sum(a * b for a, b in zip(left, right, strict=False)) / denominator),
    )
