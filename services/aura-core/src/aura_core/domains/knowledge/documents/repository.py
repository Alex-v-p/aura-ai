"""Owner-scoped SQL repository for documents."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import cast as sql_cast
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.knowledge.documents.persistence import (
    DocumentChunkRow,
    DocumentCommandReceiptRow,
    DocumentIdempotencyRow,
    DocumentLifecycleEventRow,
    DocumentRow,
    DocumentTombstoneRow,
    DocumentUploadSessionRow,
    VectorType,
)
from aura_core.domains.knowledge.documents.ports import DocumentRepositoryUnavailable
from aura_core.domains.knowledge.documents.public import (
    Document,
    DocumentChunk,
    DocumentCommandSink,
    DocumentSearchHit,
    DocumentStatus,
    DocumentUploadSession,
    DocumentVersionConflict,
)


def _decode_embedding(value: object) -> tuple[float, ...] | None:
    """Normalize psycopg's textual pgvector result and test doubles.

    ``VectorType`` is intentionally a small Core-owned SQL type rather than a
    dependency on a provider-specific pgvector package. Psycopg therefore
    returns a vector column as its canonical ``[0.1,0.2]`` text form unless a
    result codec is registered. Writes still use the same pgvector literal
    adaptation, while reads must accept both that text form and list/tuple
    values supplied by in-memory SQL test doubles.
    """

    if value is None:
        return None
    raw_values: tuple[object, ...]
    if isinstance(value, str):
        encoded = value.strip()
        if len(encoded) < 2 or encoded[0] != "[" or encoded[-1] != "]":
            raise ValueError("invalid pgvector value")
        body = encoded[1:-1].strip()
        raw_values = () if not body else tuple(body.split(","))
    elif isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray)):
        raw_values = tuple(cast(Sequence[object], value))
    else:
        raise ValueError("invalid pgvector value")
    return tuple(float(cast(Any, item)) for item in raw_values)


class SqlDocumentRepository:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        outbox: DocumentCommandSink | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = sessions
        self.outbox = outbox
        self.transactional_outbox = outbox is not None
        self.clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _document(row: DocumentRow) -> Document:
        return Document(
            row.id,
            row.principal_issuer,
            row.principal_subject,
            row.filename,
            row.media_type,
            row.byte_count,
            row.digest or "",
            row.object_key or "",
            DocumentStatus(row.status),
            row.version,
            row.created_at,
            row.updated_at,
            row.failure_code,
            row.extracted_text_length,
        )

    @staticmethod
    def _chunk(row: DocumentChunkRow) -> DocumentChunk:
        vector = _decode_embedding(row.embedding)
        return DocumentChunk(
            row.id,
            row.document_id,
            row.ordinal,
            row.text,
            row.locator,
            row.digest,
            vector,
            row.model_id,
            row.model_revision,
            row.model_digest,
        )

    @staticmethod
    def _session(row: DocumentUploadSessionRow) -> DocumentUploadSession:
        return DocumentUploadSession(
            row.id,
            row.document_id,
            row.principal_issuer,
            row.principal_subject,
            row.filename,
            row.media_type,
            row.byte_count,
            row.idempotency_key,
            row.created_at,
            row.expires_at,
            row.version,
        )

    async def create_upload_session(
        self, value: DocumentUploadSession, *, command_sink: DocumentCommandSink | None = None
    ) -> None:
        async with self.sessions() as session:
            session.add(
                DocumentRow(
                    id=value.document_id,
                    principal_issuer=value.issuer,
                    principal_subject=value.subject,
                    filename=value.filename,
                    media_type=value.media_type,
                    byte_count=value.byte_count,
                    digest=None,
                    object_key=None,
                    status=DocumentStatus.UPLOADING.value,
                    version=1,
                    failure_code=None,
                    extracted_text_length=0,
                    created_at=value.created_at,
                    updated_at=value.created_at,
                )
            )
            session.add(
                DocumentUploadSessionRow(
                    id=value.id,
                    document_id=value.document_id,
                    principal_issuer=value.issuer,
                    principal_subject=value.subject,
                    filename=value.filename,
                    media_type=value.media_type,
                    byte_count=value.byte_count,
                    idempotency_key=value.idempotency_key,
                    version=value.version,
                    created_at=value.created_at,
                    expires_at=value.expires_at,
                )
            )
            if command_sink is not None:
                await command_sink.stage_event(
                    session,
                    event_type="document.upload.created",
                    document=Document(
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
            await session.commit()

    async def get_upload_session(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> DocumentUploadSession | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DocumentUploadSessionRow).where(
                    DocumentUploadSessionRow.id == upload_id,
                    DocumentUploadSessionRow.principal_issuer == owner[0],
                    DocumentUploadSessionRow.principal_subject == owner[1],
                    DocumentUploadSessionRow.status == DocumentStatus.UPLOADING.value,
                )
            )
            return self._session(row) if row else None

    async def get_upload_session_any(
        self, owner: tuple[str, str], upload_id: UUID
    ) -> DocumentUploadSession | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DocumentUploadSessionRow).where(
                    DocumentUploadSessionRow.id == upload_id,
                    DocumentUploadSessionRow.principal_issuer == owner[0],
                    DocumentUploadSessionRow.principal_subject == owner[1],
                    DocumentUploadSessionRow.status.in_(
                        (DocumentStatus.UPLOADING.value, "consumed", "completed")
                    ),
                )
            )
            return self._session(row) if row else None

    async def find_upload_session(
        self, owner: tuple[str, str], idempotency_key: str
    ) -> DocumentUploadSession | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DocumentUploadSessionRow).where(
                    DocumentUploadSessionRow.principal_issuer == owner[0],
                    DocumentUploadSessionRow.principal_subject == owner[1],
                    DocumentUploadSessionRow.idempotency_key == idempotency_key,
                    DocumentUploadSessionRow.status.in_((
                        DocumentStatus.UPLOADING.value,
                        "consumed",
                        "completed",
                    )),
                )
            )
            return self._session(row) if row else None

    async def consume_upload_session(
        self, owner: tuple[str, str], upload_id: UUID, *, expected_version: int
    ) -> DocumentUploadSession | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DocumentUploadSessionRow).where(
                    DocumentUploadSessionRow.id == upload_id,
                    DocumentUploadSessionRow.principal_issuer == owner[0],
                    DocumentUploadSessionRow.principal_subject == owner[1],
                    DocumentUploadSessionRow.status == DocumentStatus.UPLOADING.value,
                    DocumentUploadSessionRow.version == expected_version,
                )
            )
            if row is None:
                return None
            result = await session.execute(
                update(DocumentUploadSessionRow)
                .where(
                    DocumentUploadSessionRow.id == upload_id,
                    DocumentUploadSessionRow.status == DocumentStatus.UPLOADING.value,
                    DocumentUploadSessionRow.version == expected_version,
                )
                .values(status="consumed", version=expected_version + 1)
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return self._session(row)

    async def release_upload_session(self, upload_id: UUID, *, completed: bool) -> None:
        async with self.sessions() as session:
            if completed:
                await session.execute(
                    update(DocumentUploadSessionRow)
                    .where(DocumentUploadSessionRow.id == upload_id)
                    .values(status="completed")
                )
            else:
                await session.execute(
                    update(DocumentUploadSessionRow)
                    .where(DocumentUploadSessionRow.id == upload_id)
                    .values(status=DocumentStatus.UPLOADING.value, version=1)
                )
            await session.commit()

    async def mark_upload_cleanup(
        self, document_id: UUID, object_key: str, *, command_sink: DocumentCommandSink | None
    ) -> None:
        async with self.sessions() as session:
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document_id,
                    DocumentRow.status == DocumentStatus.UPLOADING.value,
                )
                .values(
                    object_key=object_key,
                    status=DocumentStatus.DELETING.value,
                    version=DocumentRow.version + 1,
                    failure_code="upload_cleanup_retryable",
                    updated_at=self.clock(),
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                return
            row = await session.get(DocumentRow, document_id)
            if row is None:
                await session.rollback()
                return
            await session.execute(
                delete(DocumentUploadSessionRow).where(
                    DocumentUploadSessionRow.document_id == document_id
                )
            )
            if command_sink is not None:
                await command_sink.stage(
                    session,
                    document_id=document_id,
                    operation="delete",
                    version=row.version,
                )
            await session.commit()

    async def claim_stale_upload(
        self, document_id: UUID, before: object, *, command_sink: DocumentCommandSink | None
    ) -> bool:
        if not isinstance(before, datetime):
            return False
        async with self.sessions() as session:
            row = await session.scalar(
                select(DocumentRow)
                .join(
                    DocumentUploadSessionRow,
                    DocumentUploadSessionRow.document_id == DocumentRow.id,
                )
                .where(
                    DocumentRow.id == document_id,
                    DocumentRow.status == DocumentStatus.UPLOADING.value,
                    DocumentRow.updated_at < before,
                    DocumentUploadSessionRow.status == DocumentStatus.UPLOADING.value,
                    DocumentUploadSessionRow.expires_at < before,
                )
                .with_for_update()
            )
            if row is None:
                await session.rollback()
                return False
            row.object_key = row.object_key or ""
            row.status = DocumentStatus.DELETING.value
            row.version += 1
            row.failure_code = "upload_cleanup_retryable"
            row.updated_at = self.clock()
            await session.execute(
                delete(DocumentUploadSessionRow).where(
                    DocumentUploadSessionRow.document_id == document_id,
                    DocumentUploadSessionRow.status == DocumentStatus.UPLOADING.value,
                    DocumentUploadSessionRow.expires_at < before,
                )
            )
            if command_sink is not None:
                await command_sink.stage(
                    session,
                    document_id=document_id,
                    operation="delete",
                    version=row.version,
                )
            await session.commit()
            return True

    async def reserve_upload_object(self, document_id: UUID, object_key: str) -> None:
        async with self.sessions() as session:
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document_id,
                    DocumentRow.status == DocumentStatus.UPLOADING.value,
                )
                .values(object_key=object_key, updated_at=self.clock())
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                raise DocumentVersionConflict("upload cleanup already claimed")
            await session.commit()

    async def stale_uploads(self, before: object, *, limit: int = 100) -> list[Document]:
        if not isinstance(before, datetime):
            return []
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DocumentRow)
                    .where(
                        DocumentRow.status == DocumentStatus.UPLOADING.value,
                        DocumentUploadSessionRow.status == DocumentStatus.UPLOADING.value,
                        DocumentUploadSessionRow.expires_at < before,
                        DocumentRow.updated_at < before,
                    )
                    .join(
                        DocumentUploadSessionRow,
                        DocumentUploadSessionRow.document_id == DocumentRow.id,
                    )
                    .order_by(DocumentRow.updated_at, DocumentRow.id)
                    .limit(limit)
                )
            ).all()
            return [self._document(row) for row in rows]

    async def create_upload(
        self,
        document: Document,
        *,
        idempotency_key: str | None = None,
        receipt_fingerprint: str | None = None,
    ) -> Document:
        async with self.sessions() as session:
            try:
                existing = await session.get(DocumentRow, document.id, with_for_update=True)
            except TypeError:
                # Small in-memory session doubles do not expose SQLAlchemy's
                # row-lock keyword; production AsyncSession does.
                existing = await session.get(DocumentRow, document.id)
            if existing is not None and (
                existing.status != DocumentStatus.UPLOADING.value
                or existing.version != document.version
            ):
                await session.rollback()
                raise DocumentVersionConflict("upload placeholder changed")
            queue_upload = existing is None or existing.status == DocumentStatus.UPLOADING.value
            if existing is None:
                session.add(
                    DocumentRow(
                        id=document.id,
                        principal_issuer=document.issuer,
                        principal_subject=document.subject,
                        filename=document.filename,
                        media_type=document.media_type,
                        byte_count=document.byte_count,
                        digest=document.digest,
                        object_key=document.object_key,
                        status=document.status.value,
                        version=document.version,
                        failure_code=document.failure_code,
                        extracted_text_length=document.extracted_text_length,
                        created_at=document.created_at,
                        updated_at=document.updated_at,
                    )
                )
            else:
                existing.filename = document.filename
                existing.media_type = document.media_type
                existing.byte_count = document.byte_count
                existing.digest = document.digest
                existing.object_key = document.object_key
                existing.status = document.status.value
                existing.version = document.version
                existing.failure_code = document.failure_code
                existing.updated_at = document.updated_at
            if queue_upload and self.outbox is not None:
                await self.outbox.stage(
                    session,
                    document_id=document.id,
                    operation="process",
                )
                await self.outbox.stage_event(
                    session,
                    event_type="document.queued",
                    document=document,
                )
            if idempotency_key:
                session.add(
                    DocumentIdempotencyRow(
                        principal_issuer=document.issuer,
                        principal_subject=document.subject,
                        idempotency_key=idempotency_key,
                        fingerprint=str(document.id),
                        document_id=document.id,
                    )
                )
                if queue_upload:
                    session.add(
                        DocumentCommandReceiptRow(
                            principal_issuer=document.issuer,
                            principal_subject=document.subject,
                            operation="upload",
                            idempotency_key=idempotency_key,
                            fingerprint=(
                                receipt_fingerprint
                                if receipt_fingerprint is not None
                                else str(document.id)
                            ),
                            document_id=document.id,
                            version=document.version,
                            status=document.status.value,
                        )
                    )
            await session.commit()
        return document

    async def get(self, owner: tuple[str, str], document_id: UUID) -> Document | None:
        try:
            async with self.sessions() as session:
                row = await session.scalar(
                    select(DocumentRow).where(
                        DocumentRow.id == document_id,
                        DocumentRow.principal_issuer == owner[0],
                        DocumentRow.principal_subject == owner[1],
                    )
                )
                return self._document(row) if row else None
        except (DBAPIError, TimeoutError) as exc:
            raise DocumentRepositoryUnavailable("document repository unavailable") from exc

    async def get_any(self, document_id: UUID) -> Document | None:
        async with self.sessions() as session:
            row = await session.get(DocumentRow, document_id)
            if row is not None:
                return self._document(row)
            tombstone = await session.get(DocumentTombstoneRow, document_id)
            if tombstone is None:
                return None
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
                tombstone.final_version,
                tombstone.deleted_at,
                tombstone.deleted_at,
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
        filters = [
            DocumentRow.principal_issuer == owner[0],
            DocumentRow.principal_subject == owner[1],
        ]
        if status is not None:
            filters.append(DocumentRow.status == status)
        if media_type is not None:
            filters.append(DocumentRow.media_type == media_type)
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DocumentRow)
                    .where(*filters)
                    .order_by(DocumentRow.updated_at.desc(), DocumentRow.id.desc())
                    .offset(offset)
                    .limit(limit)
                )
            ).all()
            return [self._document(row) for row in rows]

    async def find_duplicate(self, owner: tuple[str, str], digest: str) -> Document | None:
        async with self.sessions() as session:
            row = await session.scalar(
                select(DocumentRow).where(
                    DocumentRow.principal_issuer == owner[0],
                    DocumentRow.principal_subject == owner[1],
                    DocumentRow.digest == digest,
                    DocumentRow.status != DocumentStatus.DELETING.value,
                )
            )
            return self._document(row) if row else None

    async def find_idempotency(self, owner: tuple[str, str], key: str) -> Document | None:
        async with self.sessions() as session:
            receipt = await session.scalar(
                select(DocumentIdempotencyRow).where(
                    DocumentIdempotencyRow.principal_issuer == owner[0],
                    DocumentIdempotencyRow.principal_subject == owner[1],
                    DocumentIdempotencyRow.idempotency_key == key,
                )
            )
            if receipt is None:
                return None
            row = await session.get(DocumentRow, receipt.document_id)
            return self._document(row) if row else None

    async def find_command_receipt(
        self, owner: tuple[str, str], operation: str, idempotency_key: str
    ) -> Document | None:
        async with self.sessions() as session:
            receipt = await session.scalar(
                select(DocumentCommandReceiptRow).where(
                    DocumentCommandReceiptRow.principal_issuer == owner[0],
                    DocumentCommandReceiptRow.principal_subject == owner[1],
                    DocumentCommandReceiptRow.operation == operation,
                    DocumentCommandReceiptRow.idempotency_key == idempotency_key,
                )
            )
            if receipt is None:
                return None
            row = await session.get(DocumentRow, receipt.document_id)
            if row is None:
                return Document(
                    receipt.document_id,
                    owner[0],
                    owner[1],
                    "",
                    "application/octet-stream",
                    0,
                    "",
                    "",
                    DocumentStatus(receipt.status),
                    receipt.version,
                    receipt.created_at,
                    receipt.created_at,
                )
            return self._document(row)

    async def find_command_receipt_fingerprint(
        self, owner: tuple[str, str], operation: str, idempotency_key: str
    ) -> str | None:
        async with self.sessions() as session:
            receipt = await session.scalar(
                select(DocumentCommandReceiptRow.fingerprint).where(
                    DocumentCommandReceiptRow.principal_issuer == owner[0],
                    DocumentCommandReceiptRow.principal_subject == owner[1],
                    DocumentCommandReceiptRow.operation == operation,
                    DocumentCommandReceiptRow.idempotency_key == idempotency_key,
                )
            )
            return str(receipt) if receipt is not None else None

    async def resolve_lifecycle_event(
        self, event_id: UUID
    ) -> tuple[UUID, int, str] | None:
        async with self.sessions() as session:
            row = await session.get(DocumentLifecycleEventRow, event_id)
            if row is None:
                return None
            return row.document_id, row.document_version, row.event_type

    async def record_command_receipt(
        self,
        owner: tuple[str, str],
        operation: str,
        idempotency_key: str,
        document: Document,
        *,
        fingerprint: str,
    ) -> None:
        async with self.sessions() as session:
            existing = await session.get(
                DocumentCommandReceiptRow,
                (owner[0], owner[1], operation, idempotency_key),
            )
            if existing is None:
                session.add(
                    DocumentCommandReceiptRow(
                        principal_issuer=owner[0],
                        principal_subject=owner[1],
                        operation=operation,
                        idempotency_key=idempotency_key,
                        fingerprint=fingerprint,
                        document_id=document.id,
                        version=document.version,
                        status=document.status.value,
                    )
                )
            await session.commit()

    async def _stage_command_receipt(
        self,
        session: AsyncSession,
        owner: tuple[str, str],
        operation: str,
        idempotency_key: str,
        document: Document,
        fingerprint: str,
    ) -> None:
        existing = await session.get(
            DocumentCommandReceiptRow,
            (owner[0], owner[1], operation, idempotency_key),
        )
        if existing is None:
            session.add(
                DocumentCommandReceiptRow(
                    principal_issuer=owner[0],
                    principal_subject=owner[1],
                    operation=operation,
                    idempotency_key=idempotency_key,
                    fingerprint=fingerprint,
                    document_id=document.id,
                    version=document.version,
                    status=document.status.value,
                )
            )
        else:
            existing.fingerprint = fingerprint
            existing.document_id = document.id
            existing.version = document.version
            existing.status = document.status.value

    async def save(self, document: Document, *, expected_version: int | None = None) -> Document:
        async with self.sessions() as session:
            query = update(DocumentRow).where(DocumentRow.id == document.id)
            if expected_version is not None:
                query = query.where(DocumentRow.version == expected_version)
            result = await session.execute(
                query.values(
                    status=document.status.value,
                    version=document.version,
                    failure_code=document.failure_code,
                    extracted_text_length=document.extracted_text_length,
                    updated_at=document.updated_at,
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                raise DocumentVersionConflict("document version conflict")
            await session.commit()
        return document

    async def replace_chunks(self, document_id: UUID, chunks: list[DocumentChunk]) -> None:
        async with self.sessions() as session:
            await session.execute(
                delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
            )
            for item in chunks:
                session.add(
                    DocumentChunkRow(
                        id=item.id,
                        document_id=item.document_id,
                        ordinal=item.ordinal,
                        text=item.text,
                        locator=item.locator,
                        digest=item.digest,
                        embedding=list(item.embedding) if item.embedding is not None else None,
                        model_id=item.model_id,
                        model_revision=item.model_revision,
                        model_digest=item.model_digest,
                    )
                )
            await session.commit()

    async def chunks(self, document_id: UUID) -> list[DocumentChunk]:
        async with self.sessions() as session:
            rows = (
                await session.scalars(
                    select(DocumentChunkRow)
                    .where(DocumentChunkRow.document_id == document_id)
                    .order_by(DocumentChunkRow.ordinal)
                )
            ).all()
            return [self._chunk(row) for row in rows]

    async def preview_chunks(
        self, owner: tuple[str, str], document_id: UUID
    ) -> list[DocumentChunk]:
        """Read ready chunks through the owner-scoped document boundary."""

        try:
            async with self.sessions() as session:
                rows = (
                    await session.scalars(
                        select(DocumentChunkRow)
                        .join(DocumentRow, DocumentRow.id == DocumentChunkRow.document_id)
                        .where(
                            DocumentChunkRow.document_id == document_id,
                            DocumentRow.principal_issuer == owner[0],
                            DocumentRow.principal_subject == owner[1],
                            DocumentRow.status == DocumentStatus.READY.value,
                        )
                        .order_by(DocumentChunkRow.ordinal, DocumentChunkRow.id)
                    )
                ).all()
                return [self._chunk(row) for row in rows]
        except (DBAPIError, TimeoutError) as exc:
            raise DocumentRepositoryUnavailable("document repository unavailable") from exc

    async def delete_chunks(self, document_id: UUID) -> None:
        async with self.sessions() as session:
            await session.execute(
                delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document_id)
            )
            await session.commit()

    async def claim_processing(
        self, document_id: UUID, *, command_sink: DocumentCommandSink | None = None
    ) -> Document | None:
        async with self.sessions() as session:
            lease_cutoff = self.clock() - timedelta(minutes=5)
            row = await session.scalar(
                select(DocumentRow).where(
                    DocumentRow.id == document_id,
                    (
                        (DocumentRow.status == DocumentStatus.QUEUED.value)
                        | (
                            (DocumentRow.status == DocumentStatus.PROCESSING.value)
                            & (DocumentRow.updated_at <= lease_cutoff)
                        )
                    ),
                )
            )
            if row is None:
                return None
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document_id,
                    DocumentRow.status == row.status,
                    DocumentRow.version == row.version,
                )
                .values(
                    status=DocumentStatus.PROCESSING.value,
                    version=row.version + 1,
                    updated_at=self.clock(),
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                return None
            row.status = DocumentStatus.PROCESSING.value
            row.version += 1
            claimed = self._document(row)
            if command_sink is not None:
                await command_sink.stage_event(
                    session,
                    event_type="document.processing",
                    document=claimed,
                )
            await session.commit()
            return claimed

    async def settle_ready(
        self,
        document: Document,
        chunks: list[DocumentChunk],
        *,
        expected_version: int,
        command_sink: DocumentCommandSink | None = None,
    ) -> Document | None:
        async with self.sessions() as session:
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document.id,
                    DocumentRow.status == DocumentStatus.PROCESSING.value,
                    DocumentRow.version == expected_version,
                )
                .values(
                    status=DocumentStatus.READY.value,
                    version=expected_version + 1,
                    extracted_text_length=document.extracted_text_length,
                    failure_code=None,
                    updated_at=document.updated_at,
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                return None
            await session.execute(
                delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document.id)
            )
            for item in chunks:
                session.add(
                    DocumentChunkRow(
                        id=item.id,
                        document_id=item.document_id,
                        ordinal=item.ordinal,
                        text=item.text,
                        locator=item.locator,
                        digest=item.digest,
                        embedding=list(item.embedding) if item.embedding is not None else None,
                        model_id=item.model_id,
                        model_revision=item.model_revision,
                        model_digest=item.model_digest,
                    )
                )
            settled = replace(
                document,
                status=DocumentStatus.READY,
                version=expected_version + 1,
                failure_code=None,
            )
            if command_sink is not None:
                await command_sink.stage_event(
                    session,
                    event_type="document.ready",
                    document=settled,
                )
            await session.commit()
            return settled

    async def settle_failed(
        self,
        document: Document,
        *,
        expected_version: int,
        failure_code: str,
        command_sink: DocumentCommandSink | None = None,
    ) -> Document | None:
        async with self.sessions() as session:
            failed = replace(
                document,
                status=DocumentStatus.FAILED,
                version=expected_version + 1,
                updated_at=self.clock(),
                failure_code=failure_code,
            )
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document.id,
                    DocumentRow.status == DocumentStatus.PROCESSING.value,
                    DocumentRow.version == expected_version,
                )
                .values(
                    status=failed.status.value,
                    version=failed.version,
                    failure_code=failure_code,
                    updated_at=failed.updated_at,
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                return None
            await session.execute(
                delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document.id)
            )
            if command_sink is not None:
                await command_sink.stage_event(
                    session,
                    event_type="document.failed",
                    document=failed,
                )
            await session.commit()
            return failed

    async def settle_deleted(
        self,
        document: Document,
        *,
        expected_version: int,
        command_sink: DocumentCommandSink | None = None,
    ) -> bool:
        async with self.sessions() as session:
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document.id,
                    DocumentRow.status == DocumentStatus.DELETING.value,
                    DocumentRow.version == expected_version,
                )
                .values(updated_at=self.clock())
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                return False
            await session.execute(
                delete(DocumentChunkRow).where(DocumentChunkRow.document_id == document.id)
            )
            await session.execute(
                delete(DocumentCommandReceiptRow).where(
                    DocumentCommandReceiptRow.document_id == document.id
                )
            )
            await session.execute(
                delete(DocumentIdempotencyRow).where(
                    DocumentIdempotencyRow.document_id == document.id
                )
            )
            session.add(
                DocumentTombstoneRow(
                    document_id=document.id,
                    deleted_at=self.clock(),
                    final_version=expected_version,
                )
            )
            await session.execute(delete(DocumentRow).where(DocumentRow.id == document.id))
            if command_sink is not None:
                await command_sink.stage_event(
                    session,
                    event_type="document.deleted",
                    document=document,
                )
            await session.commit()
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
        async with self.sessions() as session:
            transitioned = replace(
                document,
                status=DocumentStatus.DELETING,
                version=document.version + 1,
                updated_at=self.clock(),
            )
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document.id,
                    DocumentRow.version == expected_version,
                    DocumentRow.status != DocumentStatus.DELETING.value,
                )
                .values(
                    status=DocumentStatus.DELETING.value,
                    version=transitioned.version,
                    updated_at=transitioned.updated_at,
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                raise DocumentVersionConflict("document version conflict")
            await command_sink.stage(
                session,
                document_id=document.id,
                operation="delete",
                version=transitioned.version,
            )
            await command_sink.stage_event(
                session,
                event_type="document.deleting",
                document=transitioned,
            )
            if idempotency_key:
                await self._stage_command_receipt(
                    session,
                    document.owner,
                    "delete",
                    idempotency_key,
                    transitioned,
                    fingerprint or str(expected_version),
                )
            await session.commit()
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
        async with self.sessions() as session:
            queued = replace(
                document,
                status=DocumentStatus.QUEUED,
                version=document.version + 1,
                updated_at=self.clock(),
                failure_code=None,
            )
            result = await session.execute(
                update(DocumentRow)
                .where(
                    DocumentRow.id == document.id,
                    DocumentRow.status == DocumentStatus.FAILED.value,
                    DocumentRow.version == expected_version,
                )
                .values(
                    status=queued.status.value,
                    version=queued.version,
                    failure_code=None,
                    updated_at=queued.updated_at,
                )
            )
            if cast(Any, result).rowcount != 1:
                await session.rollback()
                raise DocumentVersionConflict("document version conflict")
            await command_sink.stage(
                session,
                document_id=document.id,
                operation="process",
                version=queued.version,
            )
            await command_sink.stage_event(
                session,
                event_type="document.queued",
                document=queued,
            )
            if idempotency_key:
                await self._stage_command_receipt(
                    session,
                    document.owner,
                    "retry",
                    idempotency_key,
                    queued,
                    fingerprint or str(expected_version),
                )
            await session.commit()
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
        async with self.sessions() as session:
            filters = [
                DocumentRow.principal_issuer == owner[0],
                DocumentRow.principal_subject == owner[1],
                DocumentRow.status == DocumentStatus.READY.value,
            ]
            if media_type is not None:
                filters.append(DocumentRow.media_type == media_type)
            # The API uses an opaque offset cursor.  Fetch enough ranked SQL
            # candidates to materialize the requested page without scanning
            # arbitrary rows in Python, while retaining a hard abuse bound.
            candidate_limit = min(1000, max(limit * 8, limit))
            query_vector = (
                "[" + ",".join(format(value, ".17g") for value in query_embedding) + "]"
                if query_embedding is not None
                else None
            )
            lexical_rows = (
                await session.execute(
                    select(DocumentChunkRow, DocumentRow)
                    .join(DocumentRow, DocumentRow.id == DocumentChunkRow.document_id)
                    .where(
                        *filters,
                        func.to_tsvector("simple", DocumentChunkRow.text).op("@@")(
                            func.plainto_tsquery("simple", query)
                        ),
                    )
                    .order_by(
                        func.ts_rank(
                            func.to_tsvector("simple", DocumentChunkRow.text),
                            func.plainto_tsquery("simple", query),
                        ).desc(),
                        DocumentChunkRow.id,
                    )
                    .limit(candidate_limit)
                )
            ).all()
            rows: list[tuple[DocumentChunkRow, DocumentRow]] = [
                (chunk, document) for chunk, document in lexical_rows
            ]
            if query_embedding is not None:
                vector_rows = (
                    await session.execute(
                        select(DocumentChunkRow, DocumentRow)
                        .join(DocumentRow, DocumentRow.id == DocumentChunkRow.document_id)
                        .where(*filters, DocumentChunkRow.embedding.is_not(None))
                        .order_by(
                            DocumentChunkRow.embedding.op("<=>")(
                                sql_cast(query_vector, VectorType())
                            )
                        )
                        .limit(candidate_limit)
                    )
                ).all()
                seen = {chunk.id for chunk, _document in rows}
                rows.extend(
                    (chunk, document)
                    for chunk, document in vector_rows
                    if chunk.id not in seen
                )
            terms = set(query.casefold().split())
            hits: list[DocumentSearchHit] = []
            for chunk, document in rows:
                overlap = len(terms & set(chunk.text.casefold().split()))
                lexical = overlap / max(1, len(terms))
                vector = _cosine(query_embedding, self._chunk(chunk).embedding)
                if overlap or (query_embedding is not None and chunk.embedding is not None):
                    vector = max(vector, 0.001) if query_embedding is not None else vector
                    hits.append(
                        DocumentSearchHit(
                            chunk.id,
                            document.id,
                            document.filename,
                            chunk.text[:2000],
                            chunk.locator,
                            0.65 * lexical + 0.35 * vector,
                        )
                    )
            return sorted(hits, key=lambda item: (-item.score, str(item.evidence_id)))[
                offset : offset + limit
            ]


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
