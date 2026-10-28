"""SQLAlchemy mappings owned by the workflow state domain.

Foreign keys are deliberately non-cascading.  Workflow retention has seven
independent deletion scopes and therefore cannot be represented by an ORM
cascade.
"""

# ruff: noqa: E501

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class WorkflowDefinitionRow(Base):
    __tablename__ = "workflow_definitions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_workflow_definition_owner"
        ),
        CheckConstraint("version > 0", name="ck_workflow_definition_version_positive"),
        CheckConstraint(
            "status IN ('active','retired','deleted')", name="ck_workflow_definition_status"
        ),
    )


class WorkflowDraftRow(Base):
    __tablename__ = "workflow_drafts"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_workflow_draft_owner"
        ),
        CheckConstraint("version > 0", name="ck_workflow_draft_version_positive"),
    )


class WorkflowRevisionRow(Base):
    __tablename__ = "workflow_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    draft_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(512), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="draft")
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    input_schema: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    configuration: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("workflow_id", "revision_number", name="uq_workflow_revision_number"),
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_workflow_revision_owner"
        ),
        CheckConstraint("revision_number > 0", name="ck_workflow_revision_number_positive"),
        CheckConstraint("version > 0", name="ck_workflow_revision_version_positive"),
        CheckConstraint(
            "status IN ('draft','published','retired')", name="ck_workflow_revision_status"
        ),
        Index("ix_workflow_revision_owner", "principal_issuer", "principal_subject", "created_at"),
    )


class WorkflowInstanceRow(Base):
    __tablename__ = "workflow_instances"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    household_id: Mapped[str | None] = mapped_column(String(128))
    workspace_id: Mapped[str | None] = mapped_column(String(128))
    current_ordinal: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="queued")
    terminal_outcome: Mapped[str | None] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deadline: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    input_values: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    replay_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_workflow_instance_owner"
        ),
        CheckConstraint("current_ordinal BETWEEN 1 AND 8", name="ck_workflow_instance_ordinal"),
        CheckConstraint("version > 0", name="ck_workflow_instance_version_positive"),
        CheckConstraint("replay_count BETWEEN 0 AND 1", name="ck_workflow_instance_replay_count"),
        CheckConstraint(
            "deadline > admitted_at AND deadline <= admitted_at + INTERVAL '30 days'",
            name="ck_workflow_instance_deadline",
        ),
        CheckConstraint(
            "status IN ('queued','running','waiting_for_time','waiting_for_approval','waiting_for_resolution','resolution_required','cancel_requested','terminal')",
            name="ck_workflow_instance_status",
        ),
        CheckConstraint(
            "terminal_outcome IS NULL OR terminal_outcome IN ('completed','canceled','failed')",
            name="ck_workflow_instance_outcome",
        ),
        Index("ix_workflow_instance_owner", "principal_issuer", "principal_subject", "updated_at"),
    )


class WorkflowInstanceStepRow(Base):
    __tablename__ = "workflow_instance_steps"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending")
    effect_outcome: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="not_attempted"
    )
    execution_id: Mapped[str | None] = mapped_column(String(128))
    output: Mapped[object | None] = mapped_column(JSONB)
    recovery_capabilities: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("instance_id", "ordinal", name="uq_workflow_instance_step_ordinal"),
        CheckConstraint("ordinal BETWEEN 1 AND 8", name="ck_workflow_step_ordinal"),
        CheckConstraint("version > 0", name="ck_workflow_step_version_positive"),
        CheckConstraint(
            "kind IN ('agent_run','owner_approval','owner_notification','wait_until')",
            name="ck_workflow_step_kind",
        ),
        CheckConstraint(
            "status IN ('pending','running','waiting_for_time','waiting_for_approval','failed_replayable','resolution_required','completed','rejected','expired','canceled','failed')",
            name="ck_workflow_step_status",
        ),
        CheckConstraint(
            "effect_outcome IN ('not_attempted','completed','definitively_failed','unknown')",
            name="ck_workflow_step_effect",
        ),
    )


class WorkflowCheckpointRow(Base):
    __tablename__ = "workflow_checkpoints"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    checkpoint_status: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "instance_id", "ordinal", "checkpoint_status", name="uq_workflow_checkpoint_status"
        ),
        CheckConstraint(
            "checkpoint_status IN ('current','settled')", name="ck_workflow_checkpoint_status"
        ),
    )


class WorkflowResultRow(Base):
    __tablename__ = "workflow_results"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    payload: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("instance_id", name="uq_workflow_result_instance"),
        CheckConstraint(
            "outcome IN ('completed','canceled','failed')", name="ck_workflow_result_outcome"
        ),
    )


class WorkflowHistoryRow(Base):
    __tablename__ = "workflow_instance_history"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        Index(
            "ix_workflow_history_owner",
            "principal_issuer",
            "principal_subject",
            "instance_id",
            "occurred_at",
        ),
    )


class WorkflowTombstoneRow(Base):
    __tablename__ = "workflow_tombstones"
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    terminal_outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint(
            "terminal_outcome IN ('completed','canceled','failed')",
            name="ck_workflow_tombstone_outcome",
        ),
        CheckConstraint("version > 0", name="ck_workflow_tombstone_version"),
        Index("ix_workflow_tombstone_owner", "principal_issuer", "principal_subject"),
    )


class WorkflowDefinitionTombstoneRow(Base):
    __tablename__ = "workflow_definition_tombstones"
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_workflow_definition_tombstone_version"),
        Index("ix_workflow_definition_tombstone_owner", "principal_issuer", "principal_subject"),
    )


class WorkflowRevisionTombstoneRow(Base):
    __tablename__ = "workflow_revision_tombstones"
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    workflow_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    deleted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("revision_number > 0", name="ck_workflow_revision_tombstone_number"),
        CheckConstraint("version > 0", name="ck_workflow_revision_tombstone_version"),
        Index("ix_workflow_revision_tombstone_owner", "principal_issuer", "principal_subject"),
    )


class WorkflowCommandIdempotencyRow(Base):
    __tablename__ = "workflow_command_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkflowLifecycleEventRow(Base):
    __tablename__ = "workflow_lifecycle_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    instance_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    ordinal: Mapped[int | None] = mapped_column(Integer)
    event_kind: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_metadata: Mapped[dict[str, object]] = mapped_column("metadata", JSONB, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        Index(
            "ix_workflow_lifecycle_owner", "principal_issuer", "principal_subject", "occurred_at"
        ),
    )


__all__ = [
    "WorkflowDefinitionRow", "WorkflowDraftRow", "WorkflowRevisionRow",
    "WorkflowInstanceRow", "WorkflowInstanceStepRow", "WorkflowCheckpointRow",
    "WorkflowResultRow", "WorkflowHistoryRow", "WorkflowTombstoneRow",
    "WorkflowDefinitionTombstoneRow", "WorkflowRevisionTombstoneRow",
    "WorkflowCommandIdempotencyRow", "WorkflowLifecycleEventRow",
]
