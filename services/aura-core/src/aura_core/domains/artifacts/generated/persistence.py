# ruff: noqa: E501

"""Database rows for Core-owned generated text artifacts.

The generated-artifact boundary deliberately has its own rows.  No foreign
keys point at research, task, conversation, run, or workspace tables: those
identifiers are opaque lineage supplied by a public API.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class GeneratedArtifactRow(Base):
    __tablename__ = "generated_artifacts"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str | None] = mapped_column(String(300), nullable=True)
    lifecycle: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    current_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    task_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    deletion_audit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    integrity_revision: Mapped[int | None] = mapped_column(Integer)
    integrity_sha256: Mapped[str | None] = mapped_column(String(64))
    integrity_byte_count: Mapped[int | None] = mapped_column(Integer)
    integrity_created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeneratedArtifactRevisionRow(Base):
    __tablename__ = "generated_artifact_revisions"

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, primary_key=True)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    creator_component: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("artifact_id", "sha256", name="uq_generated_artifact_revision_digest"),
    )


class GeneratedArtifactIdempotencyRow(Base):
    __tablename__ = "generated_artifact_idempotency"

    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    result_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result_lifecycle: Mapped[str] = mapped_column(String(24), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeneratedArtifactLifecycleEventRow(Base):
    __tablename__ = "generated_artifact_lifecycle_events"

    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    artifact_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    lifecycle: Mapped[str] = mapped_column(String(24), nullable=False)
    current_revision: Mapped[int | None] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    causation_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)


class GeneratedArtifactTombstoneRow(Base):
    __tablename__ = "generated_artifact_tombstones"

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    final_version: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    integrity_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    integrity_byte_count: Mapped[int] = mapped_column(Integer, nullable=False)
    integrity_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class GeneratedArtifactCleanupWorkRow(Base):
    """Durable cleanup admission; the provider outcome is resolved separately."""

    __tablename__ = "generated_artifact_cleanup_work"

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    operation: Mapped[str] = mapped_column(String(32), primary_key=True)
    expected_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    claim_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeneratedArtifactAppendReservationRow(Base):
    """One immutable append command may reserve an artifact source version."""

    __tablename__ = "generated_artifact_append_reservations"

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    creator_component: Mapped[str] = mapped_column(String(128), nullable=False)
    previous_revision: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GeneratedArtifactCreateReservationRow(Base):
    """Durable admission fence for an artifact identity before publication."""

    __tablename__ = "generated_artifact_create_reservations"

    artifact_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    fingerprint: Mapped[str] = mapped_column(String(128), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
