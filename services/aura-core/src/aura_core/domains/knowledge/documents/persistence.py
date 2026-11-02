"""SQLAlchemy mappings owned by ``knowledge.documents``."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Index, Integer, String, Text, UniqueConstraint, func, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

from aura_core.platform.database.base import Base


class VectorType(UserDefinedType[object]):
    """pgvector column type local to the document persistence boundary."""

    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        del kw
        # v1 uses the fixed 768-dimensional nomic embedding contract.  A
        # dimensionless pgvector column cannot support a valid HNSW index.
        return "vector(768)"


class DocumentRow(Base):
    __tablename__ = "documents"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    object_key: Mapped[str | None] = mapped_column(String(512), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    extracted_text_length: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "principal_issuer", "principal_subject", "digest", name="uq_documents_owner_digest"
        ),
    )


class DocumentChunkRow(Base):
    __tablename__ = "document_chunks"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[str] = mapped_column(String(255), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    embedding: Mapped[list[float] | None] = mapped_column(VectorType(), nullable=True)
    model_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_revision: Mapped[str | None] = mapped_column(String(255), nullable=True)
    model_digest: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("document_id", "ordinal", name="uq_document_chunk_ordinal"),)


class DocumentIdempotencyRow(Base):
    __tablename__ = "document_upload_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentUploadSessionRow(Base):
    __tablename__ = "document_upload_sessions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, unique=True
    )
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    media_type: Mapped[str] = mapped_column(String(255), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="uploading")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index(
            "uq_document_upload_session_idempotency",
            "principal_issuer",
            "principal_subject",
            "idempotency_key",
            unique=True,
            postgresql_where=text("idempotency_key IS NOT NULL"),
        ),
    )


class DocumentTombstoneRow(Base):
    __tablename__ = "document_tombstones"
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    final_version: Mapped[int] = mapped_column(Integer, nullable=False)


class DocumentCommandReceiptRow(Base):
    __tablename__ = "document_command_receipts"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class DocumentLifecycleEventRow(Base):
    __tablename__ = "document_lifecycle_events"
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    document_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    causation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
