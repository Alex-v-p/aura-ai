"""SQLAlchemy mappings owned by the scheduled briefing domain."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class BriefingScheduleRow(Base):
    __tablename__ = "briefing_schedules"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    local_time: Mapped[time] = mapped_column(String(8), nullable=False)
    time_zone: Mapped[str] = mapped_column(String(128), nullable=False)
    include_reminders: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    calendar_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    document_ids: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    agent_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    execution_policies: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_briefing_schedule_owner"
        ),
        CheckConstraint("version > 0", name="ck_briefing_schedule_version_positive"),
        CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 120", name="ck_briefing_schedule_name"
        ),
        CheckConstraint(
            "status IN ('active','paused','deleted')", name="ck_briefing_schedule_status"
        ),
        CheckConstraint("char_length(local_time) = 8", name="ck_briefing_schedule_local_time"),
    )


class BriefingOccurrenceRow(Base):
    __tablename__ = "briefing_occurrences"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("briefing_schedules.id"), nullable=False
    )
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    occurrence_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="scheduled")
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    due_local_date: Mapped[date] = mapped_column(Date, nullable=False)
    due_local_time: Mapped[time] = mapped_column(String(8), nullable=False)
    time_zone: Mapped[str] = mapped_column(String(128), nullable=False)
    source_completeness: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="unknown"
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("schedule_id", "due_local_date", name="uq_briefing_occurrence_local_date"),
        UniqueConstraint(
            "schedule_id", "occurrence_sequence", name="uq_briefing_occurrence_sequence"
        ),
        CheckConstraint("occurrence_sequence > 0", name="ck_briefing_occurrence_sequence_positive"),
        CheckConstraint("version > 0", name="ck_briefing_occurrence_version_positive"),
        CheckConstraint(
            "status IN ('scheduled','admitted','running','completed','partial','failed',"
            "'canceled','skipped','unknown')",
            name="ck_briefing_occurrence_status",
        ),
        CheckConstraint(
            "source_completeness IN ('complete','partial','unavailable','unknown')",
            name="ck_briefing_occurrence_source_completeness",
        ),
        Index("ix_briefing_occurrence_due_claim", "status", "due_at"),
    )


class BriefingResultRow(Base):
    __tablename__ = "briefing_results"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("briefing_schedules.id"), nullable=False
    )
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("briefing_occurrences.id"), nullable=False, unique=True
    )
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    run_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    original_status: Mapped[str | None] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    source_completeness: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_briefing_result_owner"
        ),
        CheckConstraint("version > 0", name="ck_briefing_result_version_positive"),
        CheckConstraint(
            "status IN ('completed','partial','failed','canceled','unknown','deleted')",
            name="ck_briefing_result_status",
        ),
        CheckConstraint(
            "source_completeness IN ('complete','partial','unavailable','unknown')",
            name="ck_briefing_result_source_completeness",
        ),
    )


class BriefingCommandIdempotencyRow(Base):
    __tablename__ = "briefing_command_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class BriefingRunLineageRow(Base):
    __tablename__ = "briefing_run_lineage"
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("briefing_occurrences.id"), primary_key=True
    )
    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("briefing_schedules.id"), nullable=False
    )
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, unique=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
