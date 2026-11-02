# ruff: noqa: E501

"""SQLAlchemy mappings for the Core-owned research aggregate."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.platform.database.base import Base


class ResearchTaskRow(Base):
    __tablename__ = "research_tasks"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    synthesized_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    delivery_claim_token: Mapped[str | None] = mapped_column(String(64), nullable=True)
    delivery_claim_phase: Mapped[str | None] = mapped_column(String(24), nullable=True)
    delivery_claim_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    outcome: Mapped[str | None] = mapped_column(String(24), nullable=True, index=True)
    content_state: Mapped[str] = mapped_column(String(24), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    admitted_budget: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    consumed_budget: Mapped[dict[str, int]] = mapped_column(JSONB, nullable=False)
    extension_accepted: Mapped[bool] = mapped_column(nullable=False, default=False)
    failure_class: Mapped[str | None] = mapped_column(String(32))
    seed_urls: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    domains: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    artifact_refs: Mapped[list[dict[str, object]]] = mapped_column(JSONB, nullable=False, default=list)
    capability_snapshot: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    origin: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    policy_revision: Mapped[str] = mapped_column(String(64), nullable=False, default="research.v1")
    deadline_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancellation_requested: Mapped[bool] = mapped_column(nullable=False, default=False)
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deletion_audit_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ResearchSettingsRow(Base):
    __tablename__ = "research_settings"

    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    defaults: Mapped[dict[str, dict[str, int]]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchCheckpointRow(Base):
    __tablename__ = "research_checkpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(32), nullable=False)
    operation: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchEvidenceRow(Base):
    __tablename__ = "research_evidence"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    publisher: Mapped[str | None] = mapped_column(String(300))
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    media_type: Mapped[str] = mapped_column(String(64), nullable=False)
    extraction_method: Mapped[str] = mapped_column(String(16), nullable=False)
    freshness_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excerpts: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    locator: Mapped[str | None] = mapped_column(String(255))


class ResearchCitationRow(Base):
    __tablename__ = "research_citations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    retrieved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locator: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("research_id", "source_id", name="uq_research_citation_source"),)


class ResearchIdempotencyRow(Base):
    __tablename__ = "research_idempotency"

    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResearchCommandIdempotencyRow(Base):
    __tablename__ = "research_command_idempotency"

    principal_issuer: Mapped[str] = mapped_column(String(1024), primary_key=True)
    principal_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    command: Mapped[str] = mapped_column(String(48), nullable=False)
    research_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    source_version: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class ResearchOperationRow(Base):
    __tablename__ = "research_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    operation: Mapped[str] = mapped_column(String(48), nullable=False)
    request_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    admitted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    response: Mapped[dict[str, object]] = mapped_column(JSONB, nullable=False, default=dict)
    __table_args__ = (UniqueConstraint("research_id", "operation", "request_id"),)


class ResearchFallbackCohortRow(Base):
    __tablename__ = "research_fallback_cohorts"

    component: Mapped[str] = mapped_column(String(96), primary_key=True)
    cohort_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    static_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    browser_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ResearchDeletionCleanupRow(Base):
    __tablename__ = "research_deletion_cleanup"

    research_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    audit_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    delete_linked_artifacts: Mapped[bool] = mapped_column(nullable=False, default=False)
    evidence_status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    artifact_status: Mapped[str] = mapped_column(String(24), nullable=False, default="not_requested")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending")
    last_error: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
