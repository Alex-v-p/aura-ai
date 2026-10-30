"""SQL mapping for conversation-owned research completion delivery."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class ResearchCompletionDeliveryRow(Base):
    __tablename__ = "research_completion_deliveries"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    origin_kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="conversation"
    )
    origin: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, server_default="{}")
    branch_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    originating_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    terminal_version: Mapped[int] = mapped_column(Integer, nullable=False)
    append_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    notification_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    append_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    append_message_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    notification_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint(
            "research_id", "conversation_id", name="uq_research_completion_destination"
        ),
    )
