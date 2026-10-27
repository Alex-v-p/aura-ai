"""Calendar-owned PostgreSQL mappings.

No mapping in this module contains event content.  Provider identifiers and
credential ciphertext are server-side integration state, never API values.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class CalendarConnectionRow(Base):
    __tablename__ = "calendar_connections"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="google")
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="disconnected")
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_error_category: Mapped[str | None] = mapped_column(String(48))
    last_error_retryable: Mapped[bool | None] = mapped_column(Boolean)
    last_error_recovery_action: Mapped[str | None] = mapped_column(String(32))
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    revocation_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_revocation_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revocation_lease_attempt: Mapped[int | None] = mapped_column(Integer)
    revocation_lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "principal_issuer",
            "principal_subject",
            "provider",
            name="uq_calendar_connection_owner_provider",
        ),
        CheckConstraint("version > 0", name="ck_calendar_connection_version_positive"),
        CheckConstraint(
            "revocation_attempts >= 0 AND revocation_attempts <= 6",
            name="ck_calendar_revocation_attempts_bound",
        ),
        CheckConstraint(
            "status IN ("
            "'disconnected','connecting','connected',"
            "'reauthorization_required','revocation_uncertain',"
            "'disabled','unavailable')",
            name="ck_calendar_connection_status",
        ),
        Index("ix_calendar_connections_owner", "principal_issuer", "principal_subject"),
    )


class CalendarCredentialRow(Base):
    __tablename__ = "calendar_credentials"
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    key_version: Mapped[str] = mapped_column(String(64), nullable=False)
    associated_data_digest: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint(
            "octet_length(ciphertext) BETWEEN 16 AND 32768",
            name="ck_calendar_credential_ciphertext_bound",
        ),
        CheckConstraint("octet_length(nonce) = 12", name="ck_calendar_credential_nonce_length"),
        CheckConstraint(
            "char_length(key_version) BETWEEN 1 AND 64",
            name="ck_calendar_credential_key_version_bound",
        ),
    )


class CalendarMetadataRow(Base):
    __tablename__ = "calendar_discovered_calendars"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_connections.id", ondelete="CASCADE"),
        nullable=False,
    )
    provider_calendar_id: Mapped[str] = mapped_column(String(1024), nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    time_zone: Mapped[str] = mapped_column(String(128), nullable=False)
    readable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "provider_calendar_id", name="uq_calendar_provider_calendar"
        ),
        Index("ix_calendar_metadata_connection_readable", "connection_id", "readable", "id"),
        CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 255", name="ck_calendar_display_name_bound"
        ),
        CheckConstraint(
            "char_length(time_zone) BETWEEN 1 AND 128", name="ck_calendar_timezone_bound"
        ),
    )


class CalendarSelectionRow(Base):
    __tablename__ = "calendar_selected_calendars"
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    calendar_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_discovered_calendars.id", ondelete="CASCADE"),
        primary_key=True,
    )
    selected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class CalendarSelectionVersionRow(Base):
    __tablename__ = "calendar_selection_versions"
    connection_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("calendar_connections.id", ondelete="CASCADE"),
        primary_key=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint("version > 0", name="ck_calendar_selection_version_positive"),
    )


class CalendarIdempotencyRow(Base):
    __tablename__ = "calendar_command_idempotency"
    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    operation: Mapped[str] = mapped_column(String(64), primary_key=True)
    idempotency_key: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        CheckConstraint(
            "NOT (response ? 'authorizationPath') "
            "AND NOT (response ? 'authorization_path') "
            "AND NOT (response ? 'credential') "
            "AND NOT (response ? 'provider_id')",
            name="ck_calendar_idempotency_content_free",
        ),
    )


class CalendarAuditRow(Base):
    __tablename__ = "calendar_audit_entries"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    action: Mapped[str] = mapped_column(String(48), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    category: Mapped[str | None] = mapped_column(String(48))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    __table_args__ = (
        CheckConstraint(
            "char_length(action) BETWEEN 1 AND 48", name="ck_calendar_audit_action_bound"
        ),
        CheckConstraint(
            "char_length(outcome) BETWEEN 1 AND 32", name="ck_calendar_audit_outcome_bound"
        ),
        Index("ix_calendar_audit_time", "occurred_at"),
    )
