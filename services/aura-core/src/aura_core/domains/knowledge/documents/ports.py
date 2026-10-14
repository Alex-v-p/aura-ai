"""Inward ports for document storage and embedding providers."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol
from uuid import UUID

from aura_core.runtime.models.ports import EmbeddingResult, ProviderTraceContext

if TYPE_CHECKING:
    from aura_core.domains.knowledge.documents.parser import ParsedDocument
    from aura_core.domains.knowledge.documents.public import (
        Document,
        DocumentChunk,
        DocumentCommandSink,
        DocumentSearchHit,
        DocumentUploadSession,
    )


class DocumentObjectStoreError(RuntimeError):
    """Content-free dependency failure exposed by an object-store adapter."""


class DocumentRepositoryUnavailable(RuntimeError):
    """Content-free database dependency failure exposed by a repository adapter."""


class DocumentObjectStore(Protocol):
    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int: ...

    def get(self, key: str) -> AsyncIterator[bytes]: ...

    async def delete(self, key: str) -> None: ...


class DocumentEmbedder(Protocol):
    async def embed(
        self, model_id: str, text: str, *, context: ProviderTraceContext | None = None
    ) -> EmbeddingResult: ...


class DocumentParser(Protocol):
    async def parse(self, data: bytes, *, filename: str, media_type: str) -> ParsedDocument: ...


@dataclass(frozen=True, slots=True)
class DocumentEmbedding:
    vector: tuple[float, ...]
    model_id: str
    model_revision: str | None
    model_digest: str
    digest: str


@dataclass(frozen=True, slots=True)
class DocumentSearchResult:
    """Internal retrieval result with bounded, metadata-only degradation state.

    The application service unwraps this value before returning search hits to
    API callers.  ``identity_mismatch`` only says that at least one stored
    vector was excluded because it was produced by a different verified model
    generation; it deliberately carries no model identifiers, digests, or
    document content.
    """

    hits: list[DocumentSearchHit]
    identity_mismatch: bool = False


class DocumentSearchHitPort(Protocol):
    """Minimal identifier-bearing hit used by attached grounding."""

    @property
    def document_id(self) -> UUID: ...


class DocumentRepository(Protocol):
    async def create_upload(
        self,
        document: Document,
        *,
        idempotency_key: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> Document: ...

    async def create_upload_session(self, value: DocumentUploadSession) -> None: ...

    async def get_upload_session(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> DocumentUploadSession | None: ...

    async def get_upload_session_any(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> DocumentUploadSession | None: ...

    async def find_upload_session(
        self, owner: tuple[str, str], idempotency_key: str
    ) -> DocumentUploadSession | None: ...

    async def consume_upload_session(
        self, owner: tuple[str, str], upload_id: UUID, *, expected_version: int
    ) -> DocumentUploadSession | None: ...

    async def release_upload_session(self, upload_id: UUID, *, completed: bool) -> None: ...

    async def mark_upload_cleanup(
        self, document_id: UUID, object_key: str, *, command_sink: DocumentCommandSink | None
    ) -> None: ...

    async def claim_stale_upload(
        self, document_id: UUID, before: object, *, command_sink: DocumentCommandSink | None
    ) -> bool: ...

    async def reserve_upload_object(self, document_id: UUID, object_key: str) -> None: ...

    async def stale_uploads(self, before: object, *, limit: int = 100) -> list[Document]: ...

    async def get(self, owner: tuple[str, str], document_id: UUID) -> Document | None: ...

    async def get_any(self, document_id: UUID) -> Document | None: ...

    async def list(
        self,
        owner: tuple[str, str],
        *,
        limit: int = 100,
        status: str | None = None,
        media_type: str | None = None,
    ) -> list[Document]: ...

    async def find_duplicate(self, owner: tuple[str, str], digest: str) -> Document | None: ...

    async def find_idempotency(self, owner: tuple[str, str], key: str) -> Document | None: ...

    async def find_command_receipt(
        self, owner: tuple[str, str], operation: str, idempotency_key: str
    ) -> Document | None: ...

    async def find_command_receipt_fingerprint(
        self, owner: tuple[str, str], operation: str, idempotency_key: str
    ) -> str | None: ...

    async def resolve_lifecycle_event(
        self, event_id: UUID
    ) -> tuple[UUID, int, str] | None: ...

    async def record_command_receipt(
        self,
        owner: tuple[str, str],
        operation: str,
        idempotency_key: str,
        document: Document,
        *,
        fingerprint: str,
    ) -> None: ...

    async def save(
        self, document: Document, *, expected_version: int | None = None
    ) -> Document: ...

    async def replace_chunks(self, document_id: UUID, chunks: list[DocumentChunk]) -> None: ...

    async def chunks(self, document_id: UUID) -> list[DocumentChunk]: ...

    async def preview_chunks(
        self, owner: tuple[str, str], document_id: UUID
    ) -> list[DocumentChunk]: ...

    async def delete_chunks(self, document_id: UUID) -> None: ...

    async def settle_ready(
        self, document: Document, chunks: list[DocumentChunk], *, expected_version: int
    ) -> Document | None: ...


    async def claim_processing(self, document_id: UUID) -> Document | None: ...

    async def settle_deleted(self, document: Document, *, expected_version: int) -> bool: ...

    async def transition_deleting(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> Document: ...

    async def transition_retry(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> Document: ...

    async def search(
        self,
        owner: tuple[str, str],
        query: str,
        *,
        limit: int,
        media_type: str | None = None,
        query_embedding: tuple[float, ...] | None = None,
        query_model_id: str | None = None,
        query_model_revision: str | None = None,
        query_model_digest: str | None = None,
        allowed_document_ids: tuple[UUID, ...] | None = None,
    ) -> DocumentSearchResult: ...


class DocumentReadPort(Protocol):
    """Owner-scoped document metadata port for interaction boundaries."""

    async def get(self, issuer: str, subject: str, document_id: UUID) -> Document | None: ...


class DocumentSearchPort(Protocol):
    """Allowlisted retrieval port for attached-document grounding."""

    async def search(
        self,
        issuer: str,
        subject: str,
        query: str,
        *,
        limit: int,
        allowed_document_ids: tuple[UUID, ...] | None = None,
        context: ProviderTraceContext | None = None,
    ) -> object: ...
