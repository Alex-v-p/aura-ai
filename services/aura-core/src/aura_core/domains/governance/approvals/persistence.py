"""SQLAlchemy mappings for durable owner-scoped approvals."""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, BigInteger, DateTime, Index, Integer, Sequence, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base

_APPROVAL_EVENT_REPLAY_POSITION = Sequence(
    "approval_events_replay_position_seq", metadata=Base.metadata
)


class ApprovalRow(Base):
    __tablename__ = "approvals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    tool_call_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="pending")
    execution_status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="not_started")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    origin: Mapped[str | None] = mapped_column(String(64))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    workflow_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), index=True)
    workflow_step_ordinal: Mapped[int | None] = mapped_column(Integer)
    workflow_step_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_display_name: Mapped[str | None] = mapped_column(String(512))
    next_step_kind: Mapped[str | None] = mapped_column(String(32))
    consequence: Mapped[str | None] = mapped_column(String(64))
    consequence_copy: Mapped[str | None] = mapped_column(String(128))
    owner_scope_id: Mapped[str | None] = mapped_column(String(128))
    workspace_scope_id: Mapped[str | None] = mapped_column(String(128))


class ApprovalEventRow(Base):
    __tablename__ = "approval_events"
    __table_args__ = (
        Index("uq_approval_events_replay_position", "replay_position", unique=True),
        Index(
            "ix_approval_events_owner_replay_position",
            "principal_issuer",
            "principal_subject",
            "replay_position",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    proposal_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    tool_id: Mapped[str] = mapped_column(String(128), nullable=False)
    tool_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    execution_status: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    replay_position: Mapped[int] = mapped_column(
        BigInteger,
        _APPROVAL_EVENT_REPLAY_POSITION,
        server_default=_APPROVAL_EVENT_REPLAY_POSITION.next_value(),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    origin: Mapped[str | None] = mapped_column(String(64))
    workflow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_instance_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    workflow_step_ordinal: Mapped[int | None] = mapped_column(Integer)
    workflow_step_execution_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))


class ApprovalIdempotencyRow(Base):
    __tablename__ = "approval_command_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    approval_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


__all__ = ["ApprovalEventRow", "ApprovalIdempotencyRow", "ApprovalRow"]
