"""Identity-owned SQLAlchemy mappings."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class PrincipalRow(Base):
    __tablename__ = "principals"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (UniqueConstraint("issuer", "subject", name="uq_principal_identity"),)
