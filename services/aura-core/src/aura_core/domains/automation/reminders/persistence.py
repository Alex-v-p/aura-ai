"""Reminder-owned SQLAlchemy mappings."""

from __future__ import annotations

import uuid
from datetime import date, datetime

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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class ReminderRow(Base):
    __tablename__ = "reminders"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="scheduled")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("id", "principal_issuer", "principal_subject", name="uq_reminder_owner"),
        CheckConstraint(
            "char_length(content) BETWEEN 1 AND 2000",
            name="ck_reminders_content_length",
        ),
        CheckConstraint(
            "status IN ('scheduled', 'fired', 'completed', 'canceled')",
            name="ck_reminders_status",
        ),
        CheckConstraint("version > 0", name="ck_reminders_version_positive"),
    )


class ReminderIdempotencyRow(Base):
    __tablename__ = "reminder_command_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    reminder_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReminderSeriesRow(Base):
    __tablename__ = "reminder_series"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    frequency: Mapped[str] = mapped_column(String(32), nullable=False)
    interval_value: Mapped[int] = mapped_column(Integer, nullable=False)
    weekdays: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    month_day: Mapped[int | None] = mapped_column(Integer)
    count_limit: Mapped[int | None] = mapped_column(Integer)
    end_date: Mapped[date | None] = mapped_column(Date)
    timezone: Mapped[str] = mapped_column(String(128), nullable=False)
    local_start: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    next_due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    next_reminder_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reminders.id")
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    missed_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "id", "principal_issuer", "principal_subject", name="uq_reminder_series_owner"
        ),
        CheckConstraint("interval_value > 0", name="ck_reminder_series_interval_positive"),
        CheckConstraint("version > 0", name="ck_reminder_series_version_positive"),
        CheckConstraint(
            "status IN ('active', 'paused', 'completed', 'canceled')",
            name="ck_reminder_series_status",
        ),
    )


class ReminderOccurrenceRow(Base):
    __tablename__ = "reminder_occurrences"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    series_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reminder_series.id"), nullable=False, index=True
    )
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("reminders.id"), nullable=False, unique=True
    )
    occurrence_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="scheduled")
    local_due_at: Mapped[datetime] = mapped_column(DateTime(timezone=False), nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    skipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "series_id", "occurrence_sequence", name="uq_reminder_occurrence_sequence"
        ),
        CheckConstraint(
            "occurrence_sequence > 0",
            name="ck_reminder_occurrence_sequence_positive",
        ),
        CheckConstraint(
            "status IN ("
            "'scheduled', 'fired', 'completed', 'skipped', 'replaced', "
            "'paused-window', 'series-canceled')",
            name="ck_reminder_occurrence_status",
        ),
        Index(
            "uq_reminder_scheduled_occurrence_per_series",
            "series_id",
            unique=True,
            postgresql_where=text("status = 'scheduled'"),
        ),
    )


class ReminderSeriesIdempotencyRow(Base):
    __tablename__ = "reminder_series_command_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    series_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    operation: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="series.create"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
