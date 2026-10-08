"""Agent-owned SQLAlchemy mappings."""

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class AgentProfileRow(Base):
    __tablename__ = "agent_profiles"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    slug: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, server_default="active")
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    current_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class PromptComponentRevisionRow(Base):
    __tablename__ = "prompt_component_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    component: Mapped[str] = mapped_column(String(64), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("component", "revision", name="uq_prompt_component_revision"),
    )


class PromptBundleRevisionRow(Base):
    __tablename__ = "prompt_bundle_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    platform_component_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prompt_component_revisions.id"), nullable=False
    )
    governance_component_revision_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("prompt_component_revisions.id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AgentRevisionRow(Base):
    __tablename__ = "agent_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("agent_profiles.id"), nullable=False
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    system_prompt: Mapped[str] = mapped_column(Text, nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, default="")
    instructions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    persona_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    prompt_bundle_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model_policy_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    # This stays nullable in the declarative bootstrap metadata so the
    # pre-0010 foundation migration can create the legacy table and run its
    # historical seed inserts. Migration 0010 backfills every row and then
    # enforces NOT NULL plus the foreign key at the database boundary.
    memory_policy_revision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    __table_args__ = (
        UniqueConstraint("agent_profile_id", "revision", name="uq_agent_revision_number"),
    )


class MemoryPolicyRevisionRow(Base):
    __tablename__ = "memory_policy_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    agent_profile_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    shared_user_read: Mapped[bool] = mapped_column(default=True, nullable=False)
    current_agent_read: Mapped[bool] = mapped_column(default=True, nullable=False)
    fallback_relevance_threshold: Mapped[float] = mapped_column(nullable=False, default=0.5)
    max_memories: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    context_budget_fraction: Mapped[float] = mapped_column(nullable=False, default=0.2)
    allow_shared_user_promotion: Mapped[bool] = mapped_column(default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryPolicyFallbackGrantRow(Base):
    __tablename__ = "memory_policy_fallback_grants"
    policy_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    foreign_agent_profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True
    )


class ModelPolicyRevisionRow(Base):
    __tablename__ = "model_policy_revisions"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    policy: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
