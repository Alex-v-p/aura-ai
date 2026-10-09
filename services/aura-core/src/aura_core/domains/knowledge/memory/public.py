"""Provider-neutral durable memory commands, DTOs, and persistence ports.

This module is deliberately self-contained: extraction, retrieval, and provider
adapters can depend on these contracts without importing a storage adapter.
"""

# Dynamic command kwargs are intentionally normalized at this public seam.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownMemberType=false, reportAttributeAccessIssue=false

# Domain constructor signatures are kept compact and stable for application ports.
# ruff: noqa: E501

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import secrets
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4, uuid5

from aura_core.runtime.models.ports import (
    ModelSelectionPort,
    ProviderTraceContext,
    StructuredInferenceRequest,
)

MIN_HALF_LIFE_DAYS = 0.25
MAX_HALF_LIFE_DAYS = 3650.0
DEFAULT_MEMORY_IMPORTANCE = 0.5
DEFAULT_MEMORY_HALF_LIFE_DAYS = 30.0
DORMANT_THRESHOLD = 0.10
ARCHIVE_AFTER_DAYS = 30
PURGE_CONFIRMATION = "PURGE MEMORY"


def memory_activity_id(job_id: UUID) -> UUID:
    """Return the stable activity identity shared by SSE and reconciliation."""

    return uuid5(MEMORY_ID_NAMESPACE, f"activity:{job_id}")


MEMORY_PROVENANCE_TYPES = frozenset({"manual", "conversation_message", "run", "system", "import"})
MEMORY_RELATION_TYPES = frozenset({"supersedes", "superseded_by", "disputes", "disputed_by"})


class MemoryKind(StrEnum):
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"
    PREFERENCE = "preference"
    SYSTEM = "system"


class MemoryScopeType(StrEnum):
    USER = "user"
    AGENT = "agent"


class MemoryCollectionScopeType(StrEnum):
    USER = "user"
    AGENT = "agent"
    ALL = "all"


class MemoryLifecycleStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    ARCHIVED = "archived"
    DISABLED = "disabled"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


class MemoryAction(StrEnum):
    IGNORE = "ignore"
    CREATE = "create"
    REINFORCE = "reinforce"
    SUPERSEDE = "supersede"
    DISPUTE = "dispute"
    REVIEW = "review"


class MemorySensitivity(StrEnum):
    ORDINARY = "ordinary"
    HEALTH = "health"
    FINANCE = "finance"
    IDENTITY = "identity"
    INTIMATE = "intimate"
    PRECISE_LOCATION = "precise_location"
    SENSITIVE = "sensitive"
    CREDENTIAL = "credential"
    UNKNOWN_RISK = "unknown_risk"


class MemoryRetentionBasis(StrEnum):
    """Deterministic reason a turn is eligible for durable memory."""

    PERSONAL = "personal"
    EXPLICIT_REQUEST = "explicit_request"
    NONE = "none"


class CandidateState(StrEnum):
    PROPOSED = "proposed"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    REVIEW = "review"
    RETRYABLE = "retryable"


class ProcessingJobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    RETRYABLE = "retryable"
    FAILED = "failed"


MEMORY_PROCESSING_SCHEMA_VERSION = 1
MEMORY_PROCESSING_TOPIC = "aura.memory.process.v1"
MEMORY_ID_NAMESPACE = UUID("b7dc5f90-3db3-4d41-85a5-cd3f1d9d6f31")
_REINDEX_PENDING = object()
SENSITIVITY_POLICY_VERSION = "memory-sensitivity-v1"
MEMORY_EXTRACTION_POLICY_VERSION = "memory-extraction-policy-v2"

# Composition injects the platform's metadata-only recorder.  The domain does
# not import telemetry or retain provider payloads, and recorder failures are
# intentionally isolated from memory processing.
MemoryTelemetry = Callable[..., None]

# Domain-owned schema supplied to the generic structured-inference port.  The
# runtime and providers only validate/return JSON; action semantics remain here.
MEMORY_ACTION_SCHEMA: Mapping[str, object] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "action": {"type": "string", "enum": [item.value for item in MemoryAction]},
        "content": {"type": ["string", "null"]},
        "kind": {
            "type": ["string", "null"],
            "enum": [item.value for item in MemoryKind] + [None],
        },
        "scope_type": {
            "type": ["string", "null"],
            "enum": [item.value for item in MemoryScopeType] + [None],
        },
        "agent_profile_id": {"type": ["string", "null"]},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "importance": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "half_life_days": {
            "type": ["number", "null"],
            "minimum": MIN_HALF_LIFE_DAYS,
            "maximum": MAX_HALF_LIFE_DAYS,
        },
        "valid_to": {"type": ["string", "null"], "format": "date-time"},
        "sensitivity": {
            "type": "string",
            "enum": [item.value for item in MemorySensitivity],
        },
        "retention_basis": {
            "type": "string",
            "enum": [item.value for item in MemoryRetentionBasis],
        },
        "grounded_evidence_handles": {"type": "array", "items": {"type": "string"}},
        "related_memory_id": {"type": ["string", "null"]},
    },
    "required": [
        "action",
        "content",
        "kind",
        "scope_type",
        "agent_profile_id",
        "confidence",
        "importance",
        "half_life_days",
        "valid_to",
        "sensitivity",
        "retention_basis",
        "grounded_evidence_handles",
        "related_memory_id",
    ],
}


@dataclass(frozen=True, slots=True)
class MemoryProcessingCommand:
    """Identifier-only wakeup sent over the versioned worker subject."""

    command_id: UUID
    job_id: UUID
    run_id: UUID
    conversation_id: UUID
    correlation_id: UUID
    causation_id: UUID
    agent_revision_id: UUID
    user_message_id: UUID
    assistant_message_id: UUID
    created_at: datetime | None = None
    attempt_id: UUID | None = None
    generation_id: UUID | None = None
    schema_version: int = MEMORY_PROCESSING_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != MEMORY_PROCESSING_SCHEMA_VERSION:
            raise MemoryValidationError("unsupported memory processing command version")

    def payload(self) -> dict[str, object]:
        result: dict[str, object] = {
            "schemaVersion": self.schema_version,
            "commandId": str(self.command_id),
            "jobId": str(self.job_id),
            "runId": str(self.run_id),
            "conversationId": str(self.conversation_id),
            "correlationId": str(self.correlation_id),
            "causationId": str(self.causation_id),
            "agentRevisionId": str(self.agent_revision_id),
            "userMessageId": str(self.user_message_id),
            "assistantMessageId": str(self.assistant_message_id),
        }
        if self.created_at is not None:
            result["createdAt"] = self.created_at.isoformat()
        if self.attempt_id is not None:
            result["attemptId"] = str(self.attempt_id)
        if self.generation_id is not None:
            result["generationId"] = str(self.generation_id)
        return result

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> MemoryProcessingCommand:
        required = {
            "schemaVersion",
            "commandId",
            "jobId",
            "runId",
            "conversationId",
            "correlationId",
            "causationId",
            "agentRevisionId",
            "userMessageId",
            "assistantMessageId",
        }
        optional = {"createdAt", "attemptId", "generationId"}
        if (
            payload.get("schemaVersion") != MEMORY_PROCESSING_SCHEMA_VERSION
            or not required.issubset(payload)
            or set(payload) - required - optional
        ):
            raise MemoryValidationError("invalid memory processing command metadata")
        try:

            def required_uuid(key: str) -> UUID:
                value = payload.get(key)
                if type(value) is not str or not value:
                    raise ValueError(key)
                return UUID(value)

            created_at = payload.get("createdAt")
            return cls(
                required_uuid("commandId"),
                required_uuid("jobId"),
                required_uuid("runId"),
                required_uuid("conversationId"),
                required_uuid("correlationId"),
                required_uuid("causationId"),
                required_uuid("agentRevisionId"),
                required_uuid("userMessageId"),
                required_uuid("assistantMessageId"),
                datetime.fromisoformat(created_at) if isinstance(created_at, str) else None,
                required_uuid("attemptId") if "attemptId" in payload else None,
                required_uuid("generationId") if "generationId" in payload else None,
            )
        except (TypeError, ValueError) as exc:
            raise MemoryValidationError("invalid memory processing command metadata") from exc

    @classmethod
    def from_outbox(cls, command: object) -> MemoryProcessingCommand:
        """Parse the generic identifier envelope without importing its adapter."""

        if getattr(command, "topic", None) != MEMORY_PROCESSING_TOPIC:
            raise MemoryValidationError("unexpected memory processing topic")
        identifier = getattr(command, "identifier", None)
        if not callable(identifier):
            raise MemoryValidationError("memory command identifiers are unavailable")
        payload: dict[str, object] = {
            "schemaVersion": getattr(command, "schema_version", None),
            "commandId": str(getattr(command, "id", "")),
            "jobId": identifier("jobId"),
            "runId": str(getattr(command, "run_id", "")),
            "conversationId": str(getattr(command, "conversation_id", "")),
            "correlationId": str(getattr(command, "correlation_id", "") or ""),
            "causationId": str(getattr(command, "causation_id", "") or ""),
            "agentRevisionId": identifier("agentRevisionId"),
            "userMessageId": identifier("userMessageId"),
            "assistantMessageId": identifier("assistantMessageId"),
        }
        for name in ("created_at", "attempt_id", "generation_id"):
            value = getattr(command, name, None)
            if value is not None:
                payload[
                    {
                        "created_at": "createdAt",
                        "attempt_id": "attemptId",
                        "generation_id": "generationId",
                    }[name]
                ] = value.isoformat() if isinstance(value, datetime) else str(value)
        if any(
            key != "schemaVersion" and (type(value) is not str or not value)
            for key, value in payload.items()
        ):
            raise MemoryValidationError("memory command identifiers are incomplete")
        return cls.from_payload(payload)


@dataclass(frozen=True, slots=True)
class MemoryModelConfiguration:
    issuer: str
    subject: str
    extraction_model_id: str
    embedding_model_id: str
    extraction_model_revision: str | None = None
    embedding_model_revision: str | None = None
    embedding_generation: UUID | None = None
    version: int = 1

    def __post_init__(self) -> None:
        if not self.extraction_model_id or not self.embedding_model_id:
            raise MemoryValidationError("both memory model selections are required")
        if self.version < 1:
            raise MemoryValidationError("model configuration version must be positive")


@dataclass(frozen=True, slots=True)
class MemoryModelDescriptor:
    """Provider-neutral, verified model identity used by memory settings."""

    id: str
    display_name: str
    provider: str
    capabilities: tuple[str, ...]
    model_revision: str | None
    model_digest: str | None
    dimension: int | None
    availability: str = "available"
    selectable: bool = True
    disabled_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryReindexSnapshot:
    active_generation: MemoryEmbeddingGeneration | None
    replacement_generation: MemoryEmbeddingGeneration | None
    processed_revision_count: int
    total_revision_count: int


@dataclass(frozen=True, slots=True)
class MemoryModelConfigurationSnapshot:
    """Owner-scoped configuration and generation projection for the API."""

    configuration: MemoryModelConfiguration
    extraction_model: MemoryModelDescriptor | None
    embedding_model: MemoryModelDescriptor | None
    active_generation: MemoryEmbeddingGeneration | None
    building_generation: MemoryEmbeddingGeneration | None


class MemoryModelApplicationService:
    """Application boundary for verified model selection and scoped reindexing."""

    def __init__(
        self, repository: MemoryRepository, provider: ModelSelectionPort, worker: object | None
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.worker = worker

    async def inventory(self) -> tuple[MemoryModelDescriptor, ...]:
        return tuple(self._project(item) for item in await self.provider.list_models())

    async def save_configuration(
        self,
        issuer: str,
        subject: str,
        *,
        extraction_model_id: str,
        embedding_model_id: str,
        expected_version: int,
        idempotency_key: str,
        trace: ProviderTraceContext | None = None,
    ) -> MemoryModelConfiguration:
        models = await self._fresh_inventory(trace=trace)
        extraction = self._candidate(models, extraction_model_id, "structured_output")
        if extraction is not None:
            extraction = await self._verify_selected(extraction, "structured_output", trace=trace)
        embedding = self._candidate(models, embedding_model_id, "embedding")
        if embedding is not None and embedding.dimension is None:
            embedding = await self._verify_selected(embedding, "embedding", trace=trace)
        if extraction is None or embedding is None:
            raise MemoryValidationError("selected memory model is unavailable")
        if extraction.model_digest is None or embedding.model_digest is None:
            raise MemoryValidationError("provider model identity is unavailable")
        if extraction.model_revision is None or embedding.model_revision is None:
            raise MemoryValidationError("provider model revision is unavailable")
        if embedding.dimension is None or embedding.dimension < 1:
            raise MemoryValidationError("provider embedding dimension is unavailable")
        configuration = MemoryModelConfiguration(
            issuer,
            subject,
            extraction.id,
            embedding.id,
            extraction.model_revision,
            embedding.model_revision,
            version=expected_version,
        )
        return await self.repository.save_model_configuration(
            issuer,
            subject,
            configuration,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            dimension=embedding.dimension,
            model_digest=embedding.model_digest,
        )

    async def _fresh_inventory(
        self, *, trace: ProviderTraceContext | None = None
    ) -> tuple[MemoryModelDescriptor, ...]:
        return tuple(
            self._project(item)
            for item in await self.provider.refresh_models(context=trace)
        )

    async def _verify_selected(
        self,
        model: MemoryModelDescriptor,
        capability: str,
        *,
        trace: ProviderTraceContext | None = None,
    ) -> MemoryModelDescriptor | None:
        try:
            verified = await self.provider.verify_model(model.id, capability, context=trace)
        except Exception:
            return None
        if verified is None:
            return None
        normalized = self._normalize(verified)
        if (
            capability not in normalized.capabilities
            or normalized.id != model.id
            or normalized.provider != model.provider
            or normalized.model_digest != model.model_digest
            or normalized.model_revision != model.model_revision
        ):
            return None
        return normalized

    @classmethod
    def _project(cls, item: object) -> MemoryModelDescriptor:
        normalized = cls._normalize(item)
        capabilities = set(normalized.capabilities)
        if {"chat", "completion"} & capabilities:
            capabilities.add("structured_output")
        if capabilities == set(normalized.capabilities):
            return normalized
        return replace(normalized, capabilities=tuple(sorted(capabilities)))

    @staticmethod
    def _candidate(
        models: Sequence[MemoryModelDescriptor], model_id: str, capability: str
    ) -> MemoryModelDescriptor | None:
        return next(
            (
                item
                for item in models
                if item.id == model_id
                and item.availability == "available"
                and item.model_digest is not None
                and item.model_revision is not None
                and capability in item.capabilities
            ),
            None,
        )

    async def status(self, issuer: str, subject: str) -> MemoryReindexSnapshot:
        generations = await self._generations(issuer, subject)
        try:
            configuration = await self.repository.get_model_configuration(issuer, subject)
        except MemoryNotFound:
            configuration = None
        active = next(
            (
                item
                for item in generations
                if configuration is not None
                and item.id == configuration.embedding_generation
            ),
            None,
        )
        replacement = next((item for item in generations if item.status == "building"), None)
        records = await self.repository.list_memories(
            issuer,
            subject,
            MemoryFilters(
                scope_type=None, include_all_scopes=True, include_historical=True, limit=100000
            ),
        )
        total = sum(len(item.revisions) for item in records)
        processed = (
            sum(
                sum(
                    any(
                        embedding.revision_id == revision.id
                        and embedding.generation_id == replacement.id
                        for embedding in item.embeddings
                    )
                    for revision in item.revisions
                )
                for item in records
            )
            if replacement is not None
            else 0
        )
        return MemoryReindexSnapshot(active, replacement, processed, total)

    async def configuration_snapshot(
        self, issuer: str, subject: str
    ) -> MemoryModelConfigurationSnapshot:
        configuration = await self.repository.get_model_configuration(issuer, subject)
        models = await self.inventory()
        generations = await self._generations(issuer, subject)
        extraction = next(
            (item for item in models if item.id == configuration.extraction_model_id), None
        )
        embedding = next(
            (item for item in models if item.id == configuration.embedding_model_id), None
        )
        active = next(
            (
                item
                for item in generations
                if item.id == configuration.embedding_generation and item.status == "active"
            ),
            None,
        )
        building = next((item for item in generations if item.status == "building"), None)
        return MemoryModelConfigurationSnapshot(configuration, extraction, embedding, active, building)

    async def resume(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
    ) -> None:
        generation_loader = getattr(self.repository, "get_embedding_generation", None)
        if not callable(generation_loader):
            raise MemoryNotFound("embedding generation not found")
        generation = await cast(
            Callable[..., Awaitable[MemoryEmbeddingGeneration]], generation_loader
        )(issuer, subject, generation_id)
        if generation.status != "building":
            raise MemoryVersionConflict("embedding generation is not resumable")
        fingerprint = _fingerprint("reindex.resume", {"generationId": str(generation_id)})
        reserved = await self.repository.reserve_reindex_command(
            issuer, subject, generation_id, idempotency_key, fingerprint
        )
        if not reserved:
            return
        method = getattr(self.worker, "resume_reindex", None)
        if not callable(method):
            raise MemoryValidationError("memory reindex worker is unavailable")
        try:
            await cast(Callable[..., Awaitable[object]], method)(issuer, subject, generation_id)
            await self.repository.complete_reindex_command(
                issuer, subject, generation_id, idempotency_key, fingerprint
            )
        except Exception:
            # Keep the durable pending receipt.  A repeated identical command
            # must resume after a worker crash rather than losing the work
            # reservation between dispatch and settlement.
            raise

    async def _generations(self, issuer: str, subject: str) -> list[MemoryEmbeddingGeneration]:
        loader = getattr(self.repository, "list_embedding_generations", None)
        if not callable(loader):
            return []
        return await cast(Callable[..., Awaitable[list[MemoryEmbeddingGeneration]]], loader)(
            issuer, subject
        )

    @staticmethod
    def _normalize(item: object) -> MemoryModelDescriptor:
        model_id = str(getattr(item, "id", ""))
        capabilities = tuple(str(value) for value in getattr(item, "capabilities", ()))
        digest_value = getattr(item, "model_digest", None)
        candidate_digest = (
            str(digest_value).removeprefix("sha256:").lower() if digest_value else None
        )
        digest = (
            candidate_digest
            if candidate_digest is not None and re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
            else None
        )
        dimension_value = getattr(item, "dimension", None)
        dimension = int(dimension_value) if isinstance(dimension_value, int) else None
        revision_value = getattr(item, "model_revision", None)
        revision = (
            revision_value
            if isinstance(revision_value, str)
            and 1 <= len(revision_value) <= 255
            and not any(ord(char) < 32 or ord(char) == 127 for char in revision_value)
            else None
        )
        identifier_valid = 1 <= len(model_id) <= 255 and not any(
            ord(char) < 32 or ord(char) == 127 for char in model_id
        )
        identity_available = identifier_valid and digest is not None and revision is not None
        pending_embedding_verification = (
            identity_available and "embedding" in capabilities and dimension is None
        )
        missing = (
            not identifier_valid
            or digest is None
            or revision is None
            or ("embedding" in capabilities and dimension is None)
        )
        return MemoryModelDescriptor(
            model_id,
            str(getattr(item, "display_name", model_id)),
            str(getattr(item, "provider", "")),
            capabilities,
            revision,
            digest,
            dimension,
            str(getattr(item, "availability", "available")),
            (bool(getattr(item, "selectable", True)) and not missing)
            or pending_embedding_verification,
            (
                "provider identity unavailable"
                if not identity_available
                else getattr(item, "disabled_reason", None)
                if not missing
                else None
            ),
        )

    @staticmethod
    def _require(
        models: Sequence[MemoryModelDescriptor], model_id: str, capability: str
    ) -> MemoryModelDescriptor:
        selected = next(
            (
                item
                for item in models
                if item.id == model_id
                and item.availability == "available"
                and item.selectable
                and capability in item.capabilities
            ),
            None,
        )
        if selected is None:
            raise MemoryValidationError("selected memory model is unavailable")
        return selected


@dataclass(frozen=True, slots=True)
class MemoryProcessingJob:
    id: UUID
    issuer: str
    subject: str
    run_id: UUID
    conversation_id: UUID
    status: ProcessingJobStatus = ProcessingJobStatus.QUEUED
    attempt_count: int = 0
    available_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    correlation_id: UUID | None = None
    causation_id: UUID | None = None
    last_error_class: str | None = None
    agent_revision_id: UUID | None = None
    user_message_ids: tuple[UUID, ...] = ()
    assistant_message_ids: tuple[UUID, ...] = ()
    evidence_digest: str | None = None
    lease_id: UUID | None = None
    lease_until: datetime | None = None
    agent_profile_id: UUID | None = None
    memory_id: UUID | None = None
    # Captured from the run-pinned agent memory policy at admission.  It is
    # intentionally false when a legacy/unenriched job lacks that snapshot.
    allow_shared_user_promotion: bool = False
    memory_policy_revision_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class MemoryProcessingSettlement:
    """Durable result returned to the transport acknowledgement seam."""

    command_id: UUID
    job_id: UUID
    status: ProcessingJobStatus
    terminal: bool
    candidate: MemoryCandidate | None = None

    @property
    def settled(self) -> bool:
        return self.terminal


@dataclass(frozen=True, slots=True)
class MemoryEmbeddingJob:
    id: UUID
    issuer: str
    subject: str
    memory_id: UUID
    revision_id: UUID
    generation_id: UUID
    status: ProcessingJobStatus = ProcessingJobStatus.QUEUED
    attempt_count: int = 0
    available_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_error_class: str | None = None
    lease_id: UUID | None = None
    lease_until: datetime | None = None


@dataclass(frozen=True, slots=True)
class MemoryTurnEvidence:
    issuer: str
    subject: str
    run_id: UUID
    conversation_id: UUID
    agent_revision_id: UUID | None
    user_message_ids: tuple[UUID, ...]
    assistant_message_ids: tuple[UUID, ...]
    user_content: str
    assistant_content: str
    evidence_digest: str

    def validate_for(self, job: MemoryProcessingJob) -> None:
        if (self.issuer, self.subject, self.run_id, self.conversation_id) != (
            job.issuer,
            job.subject,
            job.run_id,
            job.conversation_id,
        ):
            raise MemoryValidationError("memory evidence owner or run mismatch")
        if job.agent_revision_id is not None and self.agent_revision_id != job.agent_revision_id:
            raise MemoryValidationError("memory evidence agent revision mismatch")
        if tuple(self.user_message_ids) != tuple(job.user_message_ids) or tuple(
            self.assistant_message_ids
        ) != tuple(job.assistant_message_ids):
            raise MemoryValidationError("memory evidence message set mismatch")
        # The evidence digest is deliberately over authenticated user-authored
        # evidence only.  Assistant text is retained as extraction context but
        # cannot qualify or alter the evidence identity.
        digest = hashlib.sha256(self.user_content.encode()).hexdigest()
        if digest != self.evidence_digest or (
            job.evidence_digest is not None and digest != job.evidence_digest
        ):
            raise MemoryValidationError("memory evidence digest mismatch")


@dataclass(frozen=True, slots=True)
class MemoryCandidate:
    id: UUID
    job_id: UUID
    issuer: str
    subject: str
    action: MemoryAction
    content: str | None
    kind: MemoryKind | None
    scope: MemoryScope | None
    confidence: float
    importance: float | None = None
    half_life_days: float | None = None
    valid_to: datetime | None = None
    sensitivity: MemorySensitivity = MemorySensitivity.ORDINARY
    grounded_message_ids: tuple[UUID, ...] = ()
    related_memory_id: UUID | None = None
    state: CandidateState = CandidateState.PROPOSED
    decision_reason: str | None = None
    memory_id: UUID | None = None
    version: int = 1
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    decided_at: datetime | None = None
    retention_basis: MemoryRetentionBasis = MemoryRetentionBasis.NONE

    def __post_init__(self) -> None:
        if not isinstance(self.action, MemoryAction):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise MemoryValidationError("unsupported memory candidate action")
        if not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise MemoryValidationError("candidate confidence must be between zero and one")
        if self.content is not None:
            if len(self.content.strip()) == 0 or len(self.content) > 32768:
                raise MemoryValidationError(
                    "candidate content must be between 1 and 32768 characters"
                )
            if self.importance is not None and not 0 <= self.importance <= 1:
                raise MemoryValidationError("candidate importance must be between zero and one")
            if (
                self.half_life_days is not None
                and not MIN_HALF_LIFE_DAYS <= self.half_life_days <= MAX_HALF_LIFE_DAYS
            ):
                raise MemoryValidationError("candidate half-life is outside the supported range")
        if (
            self.state in {CandidateState.PROPOSED, CandidateState.ACCEPTED}
            and self.action
            in {
                MemoryAction.CREATE,
                MemoryAction.REINFORCE,
                MemoryAction.SUPERSEDE,
                MemoryAction.DISPUTE,
            }
            and not self.content
        ):
            raise MemoryValidationError("memory actions require canonical content")
        if (
            self.scope is not None
            and self.scope.type is MemoryScopeType.AGENT
            and self.scope.agent_profile_id is None
        ):
            raise MemoryValidationError("agent candidate scope requires an agent profile")


@dataclass(frozen=True, slots=True)
class CandidateDecision:
    state: CandidateState
    reason: str


def _normalize_candidate_for_approval(candidate: MemoryCandidate) -> MemoryCandidate:
    """Make a content-bearing candidate owner-approvable.

    ``review`` is a provider disposition, not a durable memory mutation.  A
    few candidates were persisted before that distinction was enforced, so
    keep those rows compatible by translating their action at the approval
    boundary.  Missing decay metadata receives conservative, bounded defaults;
    validity remains open-ended unless the provider supplied an explicit end.
    """

    if candidate.content is None:
        return candidate
    action = MemoryAction.CREATE if candidate.action is MemoryAction.REVIEW else candidate.action
    return replace(
        candidate,
        action=action,
        importance=(
            candidate.importance
            if candidate.importance is not None
            else DEFAULT_MEMORY_IMPORTANCE
        ),
        half_life_days=(
            candidate.half_life_days
            if candidate.half_life_days is not None
            else DEFAULT_MEMORY_HALF_LIFE_DAYS
        ),
    )


def _content_free_candidate(candidate: MemoryCandidate) -> MemoryCandidate:
    """Strip private evidence from terminal/non-actionable candidate rows."""

    if candidate.state in {CandidateState.REJECTED, CandidateState.RETRYABLE}:
        return MemoryCandidate(
            candidate.id,
            candidate.job_id,
            candidate.issuer,
            candidate.subject,
            candidate.action,
            None,
            None,
            None,
            candidate.confidence,
            None,
            None,
            None,
            candidate.sensitivity,
            (),
            None,
            candidate.state,
            candidate.decision_reason,
            version=candidate.version,
            created_at=candidate.created_at,
            decided_at=candidate.decided_at,
            retention_basis=candidate.retention_basis,
        )
    return candidate


def decide_candidate(
    candidate: MemoryCandidate,
    *,
    user_message_ids: frozenset[UUID],
    run_agent_profile_id: UUID | None = None,
    existing_conflict: bool = False,
    user_content: str | None = None,
    allow_shared_user_promotion: bool = False,
) -> CandidateDecision:
    """Apply deterministic guardrails after untrusted structured inference."""

    if candidate.action is MemoryAction.IGNORE:
        return CandidateDecision(CandidateState.REJECTED, "ignored")
    if candidate.action is MemoryAction.REVIEW:
        return CandidateDecision(CandidateState.REVIEW, "provider_requested_review")
    classified = classify_sensitivity(candidate.content)
    if (
        classified is MemorySensitivity.CREDENTIAL
        or candidate.sensitivity is MemorySensitivity.CREDENTIAL
    ):
        return CandidateDecision(CandidateState.REJECTED, "credential")
    sensitive_categories = {
        MemorySensitivity.HEALTH,
        MemorySensitivity.FINANCE,
        MemorySensitivity.IDENTITY,
        MemorySensitivity.INTIMATE,
        MemorySensitivity.PRECISE_LOCATION,
        MemorySensitivity.SENSITIVE,
        MemorySensitivity.UNKNOWN_RISK,
    }
    if classified in sensitive_categories or candidate.sensitivity in sensitive_categories:
        return CandidateDecision(
            CandidateState.REVIEW,
            classified.value
            if classified is not MemorySensitivity.ORDINARY
            else candidate.sensitivity.value,
        )
    if candidate.content is None or contains_secret(candidate.content):
        return CandidateDecision(CandidateState.REJECTED, "secret_or_empty")
    if not set(candidate.grounded_message_ids).issubset(user_message_ids):
        return CandidateDecision(CandidateState.REVIEW, "ungrounded")
    if not candidate.grounded_message_ids:
        return CandidateDecision(CandidateState.REVIEW, "ungrounded")
    if user_content is not None:
        candidate_terms = {
            term for term in re.findall(r"[a-z0-9]{4,}", candidate.content.casefold())
        }
        source_terms = set(re.findall(r"[a-z0-9]{4,}", user_content.casefold()))
        overlap = candidate_terms.intersection(source_terms)
        if len(candidate_terms) < 2 or len(overlap) < max(2, math.ceil(len(candidate_terms) * 0.8)):
            return CandidateDecision(CandidateState.REVIEW, "insufficient_grounding")
        if re.search(
            r"\b(?:maybe|might|perhaps|possibly|not sure|i think|i believe|could be)\b",
            candidate.content.casefold(),
        ):
            return CandidateDecision(CandidateState.REVIEW, "uncertain_evidence")
        # A high lexical overlap is not support when a claim reverses the
        # source polarity (for example, "I like coffee" vs "I do not like
        # coffee").  Keep this conservative and review ambiguous language.
        source_negated = bool(
            re.search(
                r"\b(?:no|not|never|don't|doesn't|isn't|can't|won't)\b", user_content.casefold()
            )
        )
        candidate_negated = bool(
            re.search(
                r"\b(?:no|not|never|don't|doesn't|isn't|can't|won't)\b",
                candidate.content.casefold(),
            )
        )
        if source_negated != candidate_negated:
            return CandidateDecision(CandidateState.REVIEW, "negation_conflict")
    if candidate.scope is None or (
        candidate.scope.type is MemoryScopeType.USER and not allow_shared_user_promotion
    ):
        return CandidateDecision(CandidateState.REVIEW, "shared_scope_requires_policy")
    if (
        candidate.scope.type is MemoryScopeType.AGENT
        and candidate.scope.agent_profile_id != run_agent_profile_id
    ):
        return CandidateDecision(CandidateState.REVIEW, "risky_scope")
    if existing_conflict or candidate.action in {MemoryAction.DISPUTE, MemoryAction.SUPERSEDE}:
        return CandidateDecision(CandidateState.REVIEW, "conflict")
    if candidate.confidence < 0.60:
        return CandidateDecision(CandidateState.REJECTED, "low_confidence")
    if candidate.confidence < 0.85:
        return CandidateDecision(CandidateState.REVIEW, "review_threshold")
    return CandidateDecision(CandidateState.ACCEPTED, "auto_commit")


_EXPLICIT_MEMORY_REQUEST = re.compile(
    r"(?:^|[,;:]\s*|\bplease\s+|\b(?:can|could|would)\s+you\s+)"
    r"(?:remember|keep\s+in\s+mind|don['’]?t\s+forget)\b|"
    r"\b(?:you|aura|this\s+agent)\s+should\s+"
    r"(?:remember|keep\s+in\s+mind|don['’]?t\s+forget)\b|"
    r"\b(?:save|store|note)\s+(?:the\s+fact(?:\s+that)?|in\s+memory|for\s+later)\b",
    re.IGNORECASE,
)
_QUESTION_MEMORY_COMMAND = re.compile(
    r"\b(?:can|could|would)\s+you\s+(?:please\s+)?"
    r"(?:remember|keep\s+in\s+mind|don['’]?t\s+forget)\b|"
    r"\b(?:can|could|would)\s+you\s+(?:please\s+)?"
    r"(?:save|store|note)\s+(?:the\s+fact(?:\s+that)?|in\s+memory|for\s+later)\b|"
    r"\bplease\s+(?:remember|keep\s+in\s+mind|don['’]?t\s+forget)\b|"
    r"\bplease\s+(?:save|store|note)\s+(?:the\s+fact(?:\s+that)?|in\s+memory|for\s+later)\b",
    re.IGNORECASE,
)
_EPISTEMIC_FRAMING = re.compile(
    r"\b(?:i\s+(?:think|believe|heard|read|wonder)|i\s+was\s+told|"
    r"my\s+understanding\s+is)\b",
    re.IGNORECASE,
)
_PERSONAL_MEMORY_MARKER = re.compile(
    r"(?:\b(?:my|our)\b|"
    r"\bowner\s+(?:prefer|prefers|like|likes|love|loves|enjoy|enjoys|hate|hates|"
    r"avoid|avoids|need|needs|want|wants|own|owns|bought|use|uses|usually|always|"
    r"never|have|has|work|works|live|lives|plan|plans|promise|promised|correct|making|"
    r"working)\b|"
    r"\bpersonal\s+(?:project|preference|commitment|routine|plan|goal)\b|"
    r"\b(?:i|we|i['’]?m|i\s+am)\s+(?:prefer|like|love|enjoy|hate|avoid|need|want|"
    r"own|bought|use|usually|always|never|have|work|live|plan|promise|promised|"
    r"correct|making|working)\b)",
    re.IGNORECASE,
)
_DATE_EXPRESSION = (
    r"(?:january|february|march|april|may|june|july|august|september|october|"
    r"november|december)\s+\d{1,2}(?:st|nd|rd|th)?(?:,\s*\d{4})?|"
    r"\d{1,2}(?:st|nd|rd|th)?\s+(?:january|february|march|april|may|june|july|"
    r"august|september|october|november|december)(?:\s+\d{4})?|"
    r"\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?"
)
_DATED_PERSONAL_EVENT = re.compile(
    r"\b(?P<subject>[A-Za-z][A-Za-z-]{1,40})['’]s\s+"
    r"(?P<event>birthday|birth\s+date|anniversary)\b(?!\s+(?:party|celebration|event|"
    r"plan|plans|planning))[^.!?]{0,32}\b(?P<date>" + _DATE_EXPRESSION + r")\b",
    re.IGNORECASE,
)
_USER_FAMILY_EVENT = re.compile(
    r"\b(?:my|our)\s+(?:wife|husband|partner|mother|father|mom|dad|sister|brother|"
    r"friend|daughter|son|child|family|parents)\s+"
    r"(?P<subject>[A-Za-z][A-Za-z-]{1,40})(?:['’]s)?\s+"
    r"(?P<event>birthday|birth\s+date|anniversary)\b(?!\s+(?:party|celebration|event|"
    r"plan|plans|planning))[^.!?]{0,32}\b(?P<date>" + _DATE_EXPRESSION + r")\b",
    re.IGNORECASE,
)
_CANDIDATE_FAMILY_EVENT = re.compile(
    r"\b(?:my|our)\s+(?P<relation>wife|husband|partner|mother|father|mom|dad|sister|"
    r"brother|friend|daughter|son|child|family|parents)['’]s?\s+"
    r"(?P<event>birthday|birth\s+date|anniversary)\b(?!\s+(?:party|celebration|event|"
    r"plan|plans|planning))[^.!?]{0,32}\b(?P<date>" + _DATE_EXPRESSION + r")\b",
    re.IGNORECASE,
)
_USER_FAMILY_RELATION_EVENT = _CANDIDATE_FAMILY_EVENT

_MONTH_NUMBERS = {
    month: index
    for index, month in enumerate(
        (
            "january",
            "february",
            "march",
            "april",
            "may",
            "june",
            "july",
            "august",
            "september",
            "october",
            "november",
            "december",
        ),
        start=1,
    )
}


def _normalized_event_date(value: str) -> str:
    compact = re.sub(r"\s+", " ", value.casefold().replace(",", "").strip())
    parts = compact.split()
    if len(parts) >= 2 and parts[0] in _MONTH_NUMBERS:
        day = re.sub(r"(?:st|nd|rd|th)$", "", parts[1])
        return f"{_MONTH_NUMBERS[parts[0]]:02d}-{int(day):02d}-{' '.join(parts[2:])}"
    if len(parts) >= 2 and parts[1] in _MONTH_NUMBERS:
        day = re.sub(r"(?:st|nd|rd|th)$", "", parts[0])
        return f"{_MONTH_NUMBERS[parts[1]]:02d}-{int(day):02d}-{' '.join(parts[2:])}"
    return compact


def classify_retention_basis(user_content: str) -> MemoryRetentionBasis:
    """Classify retention from authenticated user text, never assistant text."""

    if "?" in user_content and not _QUESTION_MEMORY_COMMAND.search(user_content):
        return MemoryRetentionBasis.NONE
    if _EXPLICIT_MEMORY_REQUEST.search(user_content):
        return MemoryRetentionBasis.EXPLICIT_REQUEST
    if _EPISTEMIC_FRAMING.search(user_content):
        return MemoryRetentionBasis.NONE
    if _PERSONAL_MEMORY_MARKER.search(user_content):
        return MemoryRetentionBasis.PERSONAL
    return MemoryRetentionBasis.NONE


def normalize_retention_horizon(
    candidate: MemoryCandidate, *, user_content: str
) -> MemoryCandidate:
    """Apply bounded deterministic horizons for durable family events."""

    content = candidate.content or ""
    candidate_event = _DATED_PERSONAL_EVENT.search(content)
    candidate_relation_event = _CANDIDATE_FAMILY_EVENT.search(content)
    user_events = {
        (
            match.group("subject").casefold(),
            match.group("event").casefold(),
            _normalized_event_date(match.group("date")),
        )
        for match in _USER_FAMILY_EVENT.finditer(user_content)
    }
    user_relation_events = {
        (
            match.group("relation").casefold(),
            match.group("event").casefold(),
            _normalized_event_date(match.group("date")),
        )
        for match in _USER_FAMILY_RELATION_EVENT.finditer(user_content)
    }
    stable_family_event = False
    if candidate_event is not None:
        stable_family_event = (
            candidate_event.group("subject").casefold(),
            candidate_event.group("event").casefold(),
            _normalized_event_date(candidate_event.group("date")),
        ) in user_events
    elif candidate_relation_event is not None:
        stable_family_event = (
            candidate_relation_event.group("relation").casefold(),
            candidate_relation_event.group("event").casefold(),
            _normalized_event_date(candidate_relation_event.group("date")),
        ) in user_relation_events
    if (
        candidate.action is MemoryAction.IGNORE
        or not candidate.content
        or not stable_family_event
    ):
        return candidate
    return replace(
        candidate,
        half_life_days=max(candidate.half_life_days or DEFAULT_MEMORY_HALF_LIFE_DAYS, 365.0),
        valid_to=None,
    )


# Short, conservative credential patterns are applied before a memory reaches
# either adapter.  This is intentionally a deny rule, not a secret detector.
_SECRET_PATTERNS = (
    re.compile(
        r"(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|secret|password|private[_ -]?key|credential)\s*[:=]",
        re.I,
    ),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|pk)_[A-Za-z0-9]{16,}\b", re.I),
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b", re.I),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{10,}\b", re.I),
    re.compile(r"\bnpm_[A-Za-z0-9]{20,}\b", re.I),
    re.compile(r"\b(?:glpat-|pat_|token_)[A-Za-z0-9_-]{16,}\b", re.I),
    re.compile(
        r"\b(?:otp|one[- ]time password|pin|passcode|recovery code|seed phrase|mnemonic)\s*[:=]?\s*[A-Za-z0-9 -]{4,}",
        re.I,
    ),
    re.compile(r"\b(?:ya29\.|1//)[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I),
)

# Credential values may be expressed conversationally rather than with a
# machine-token prefix.  Keep this detector shared by pre-inference admission
# and every persistence validator; provider labels are never authoritative.
_CREDENTIAL_VALUE_PATTERNS = (
    re.compile(
        r"\b(?:password|passphrase|api[_ -]?key|access[_ -]?token|client[_ -]?secret|private[_ -]?key|credential|secret)\s*(?:is|:|=)\s*[^\s,.;]{4,}",
        re.I,
    ),
    re.compile(
        r"\b(?:one[- ]time|verification|security|authentication)\s+(?:code|passcode|password|pin|otp)\s*(?:is|:|=)?\s*[A-Za-z0-9]{4,}",
        re.I,
    ),
    re.compile(r"\b(?:otp|pin|passcode|recovery code)\s*(?:is|:|=)\s*[A-Za-z0-9]{4,}", re.I),
    re.compile(
        r"\b(?:recovery|seed)\s+phrase\s*(?:is|:|=)\s*(?:[A-Za-z]{2,}\s+){2,}[A-Za-z]{2,}", re.I
    ),
)


class MemoryError(RuntimeError):
    """Base class for deterministic memory application errors."""


class MemoryNotFound(LookupError, MemoryError):
    pass


class MemoryVersionConflict(MemoryError):
    pass


class MemoryIdempotencyConflict(MemoryError):
    pass


class MemoryPurgeReplayNotFound(MemoryNotFound, MemoryIdempotencyConflict):
    """Content-safe not-found that also preserves legacy replay handling."""


class MemoryPurgeConfirmationRequired(MemoryError):
    pass


class MemoryScopeAuthorizationRequired(MemoryError):
    pass


class MemoryValidationError(ValueError, MemoryError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryScope:
    type: MemoryScopeType
    agent_profile_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.type is MemoryScopeType.AGENT and self.agent_profile_id is None:
            raise MemoryValidationError("agent scope requires an agent profile")
        if self.type is MemoryScopeType.USER and self.agent_profile_id is not None:
            raise MemoryValidationError("user scope cannot have an agent profile")


@dataclass(frozen=True, slots=True)
class MemoryProvenance:
    id: UUID
    source_type: str
    source_id: UUID | None = None
    conversation_id: UUID | None = None
    run_id: UUID | None = None
    message_id: UUID | None = None
    observed_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence_digest: str | None = None
    # Evidence content is retained for inspection but is never accepted by
    # telemetry.  It is optional because automatic extraction may only have IDs.
    evidence: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.source_type not in MEMORY_PROVENANCE_TYPES:
            raise MemoryValidationError("unsupported memory provenance type")
        if self.evidence is not None and contains_secret(self.evidence):
            raise MemoryValidationError("credential-like provenance evidence is not accepted")
        if self.evidence_digest is not None and not re.fullmatch(
            r"[0-9a-f]{64}", self.evidence_digest
        ):
            raise MemoryValidationError("provenance digest must be a sha256 digest")


@dataclass(frozen=True, slots=True)
class MemoryRevision:
    id: UUID
    memory_id: UUID
    revision: int
    kind: MemoryKind
    content: str
    observed_at: datetime
    created_at: datetime
    confidence: float
    importance: float
    half_life_days: float
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    provenance_ids: tuple[UUID, ...] = ()
    correction_reason: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryEmbedding:
    id: UUID
    revision_id: UUID
    generation: int
    model_id: str
    model_revision: str | None
    dimension: int
    # Vector digest and model identity digest are intentionally distinct.
    digest: str
    created_at: datetime
    vector: tuple[float, ...] | None = None
    generation_id: UUID | None = None
    model_digest: str | None = None


@dataclass(slots=True)
class MemoryEmbeddingGeneration:
    id: UUID
    generation: int
    model_id: str
    model_revision: str | None
    dimension: int
    status: str
    created_at: datetime
    activated_at: datetime | None = None
    model_digest: str | None = None
    issuer: str = ""
    subject: str = ""


@dataclass(frozen=True, slots=True)
class MemoryRelation:
    memory_id: UUID
    relation: str
    created_at: datetime


@dataclass(slots=True)
class MemoryRecord:
    id: UUID
    issuer: str
    subject: str
    kind: MemoryKind
    scope: MemoryScope
    status: MemoryLifecycleStatus
    pinned: bool
    version: int
    current_revision_id: UUID
    revisions: list[MemoryRevision]
    provenance: list[MemoryProvenance] = field(default_factory=list)
    embeddings: list[MemoryEmbedding] = field(default_factory=list)
    related_memory_ids: tuple[UUID, ...] = ()
    relations: list[MemoryRelation] = field(default_factory=list)
    reinforced_at: datetime | None = None
    dormant_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    embedding_generations: list[MemoryEmbeddingGeneration] = field(default_factory=list)

    @property
    def current_revision(self) -> MemoryRevision:
        return next(item for item in self.revisions if item.id == self.current_revision_id)

    @property
    def content(self) -> str:
        return self.current_revision.content

    def relevance(self, now: datetime | None = None) -> float:
        """Return deterministic factual relevance without changing lifecycle."""

        stamp = self.reinforced_at or self.current_revision.observed_at
        age_days = max(0.0, ((now or datetime.now(UTC)) - stamp).total_seconds() / 86400)
        return 2 ** (-age_days / self.current_revision.half_life_days)


@dataclass(frozen=True, slots=True)
class MemoryAuditRecord:
    id: UUID
    issuer: str
    subject: str
    memory_id: UUID
    action: str
    created_at: datetime


@dataclass(frozen=True, slots=True)
class MemoryActivityItem:
    """Identifier-only activity projection exposed to the run boundary."""

    id: UUID
    action: str
    status: str
    scope: dict[str, object] | None
    candidate_id: UUID | None = None
    memory_id: UUID | None = None
    memory_revision_id: UUID | None = None
    policy_revision_id: UUID | None = None
    embedding_generation_id: UUID | None = None
    reconciliation_status: str = "authoritative"
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class MemoryActivitySnapshot:
    """Owner-scoped durable run activity without memory text or evidence."""

    run_id: UUID
    processing_status: str
    items: tuple[MemoryActivityItem, ...] = ()
    last_event_id: UUID | None = None
    reconciled_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(frozen=True, slots=True)
class MemoryFilters:
    kind: MemoryKind | None = None
    scope_type: MemoryScopeType | None = MemoryScopeType.USER
    agent_profile_id: UUID | None = None
    status: MemoryLifecycleStatus | None = None
    q: str | None = None
    include_historical: bool = False
    limit: int = 50
    authorized_agent_ids: frozenset[UUID] = frozenset()
    include_all_scopes: bool = False
    provenance_type: str | None = None
    confidence_min: float | None = None
    confidence_max: float | None = None
    created_from: datetime | None = None
    created_to: datetime | None = None
    # Cursors are transport-neutral, owner/filter-bound sort positions.  The
    # API keeps the literal query out of URLs; repositories only receive the
    # decoded position after its owner/filter binding has been checked.
    cursor_updated_at: datetime | None = None
    cursor_id: UUID | None = None

    def __post_init__(self) -> None:
        if self.scope_type is None and not self.include_all_scopes:
            raise MemoryValidationError("an explicit memory scope is required")
        if (
            self.scope_type is MemoryScopeType.AGENT
            and self.agent_profile_id is None
            and not self.include_all_scopes
        ):
            raise MemoryValidationError("agent scope requires an agent profile")
        if self.scope_type is MemoryScopeType.USER and self.agent_profile_id is not None:
            raise MemoryValidationError("user scope cannot have an agent profile")


def _cursor_binding(issuer: str, subject: str, fingerprint: str) -> str:
    return hashlib.sha256(f"{issuer}\x00{subject}\x00{fingerprint}".encode()).hexdigest()


def encode_memory_cursor(
    issuer: str, subject: str, fingerprint: str, updated_at: datetime, item_id: UUID
) -> str:
    """Create an opaque, owner- and filter-bound cursor without private text."""

    payload = {
        "v": 1,
        "b": _cursor_binding(issuer, subject, fingerprint),
        "t": updated_at.isoformat(),
        "i": str(item_id),
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_memory_cursor(
    cursor: str, issuer: str, subject: str, fingerprint: str
) -> tuple[datetime, UUID]:
    """Validate a cursor and return its deterministic sort position."""

    if not cursor or len(cursor) > 1024:
        raise MemoryValidationError("invalid memory cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(payload, dict) or payload.get("v") != 1:
            raise ValueError
        if payload.get("b") != _cursor_binding(issuer, subject, fingerprint):
            raise ValueError
        stamp = datetime.fromisoformat(str(payload["t"]))
        identifier = UUID(str(payload["i"]))
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeError) as exc:
        raise MemoryValidationError("invalid memory cursor") from exc
    return stamp, identifier


def contains_secret(value: str) -> bool:
    return any(
        pattern.search(value) for pattern in (*_SECRET_PATTERNS, *_CREDENTIAL_VALUE_PATTERNS)
    )


_SENSITIVE_PATTERNS: tuple[tuple[MemorySensitivity, re.Pattern[str]], ...] = (
    (
        MemorySensitivity.CREDENTIAL,
        re.compile(r"\b(password|token|api[ -]?key|secret|private key|oauth|credential)\b", re.I),
    ),
    (
        MemorySensitivity.HEALTH,
        re.compile(
            r"\b(diabetes|diagnos(?:is|ed)|cancer|oncolog(?:y|ist)|medication|prescription|severe depression|major depression|depression|suicid(?:al|e)|病|health|medical)\b",
            re.I,
        ),
    ),
    (
        MemorySensitivity.FINANCE,
        re.compile(
            r"\b(bank|iban|credit[ -]?card|salary|income|tax|finance|routing number|account number|mortgage|balance|financial|loan)\b",
            re.I,
        ),
    ),
    (
        MemorySensitivity.IDENTITY,
        re.compile(
            r"\b(passport|social[ -]?security|ssn|national id(?:entification)?|identity number|driver(?:'s)? license|date of birth)\b|\b\d{3}-\d{2}-\d{4}\b",
            re.I,
        ),
    ),
    (
        MemorySensitivity.INTIMATE,
        re.compile(
            r"\b(sexual|intimate|pregnan(?:t|cy)|relationship abuse|sexual orientation|abortion)\b",
            re.I,
        ),
    ),
    (
        MemorySensitivity.PRECISE_LOCATION,
        re.compile(
            r"\b(latitude|longitude|gps|home address|street address|precise location|coordinates|geolocation)\b|\b\d{1,5}\s+[A-Za-z0-9.'-]+\s+(?:street|st|road|rd|avenue|ave|lane|ln|boulevard|blvd)\b",
            re.I,
        ),
    ),
)

# Provider credentials are rejected before inference.  Unknown token-like
# values fail closed as UNKNOWN_RISK rather than relying on provider labels.
_UNKNOWN_TOKEN_PATTERN = re.compile(
    r"\b(?=[A-Za-z0-9_-]{20,}\b)(?=[A-Za-z0-9_-]*[A-Za-z])(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]+\b"
)


def classify_sensitivity(content: str | None) -> MemorySensitivity:
    """Apply the versioned fail-closed classifier independently of providers."""

    if not content or contains_secret(content):
        return MemorySensitivity.CREDENTIAL if content else MemorySensitivity.UNKNOWN_RISK
    for category, pattern in _SENSITIVE_PATTERNS:
        if pattern.search(content):
            return category
    if _UNKNOWN_TOKEN_PATTERN.search(content):
        return MemorySensitivity.UNKNOWN_RISK
    return MemorySensitivity.ORDINARY


def validate_provenance(provenance: MemoryProvenance) -> None:
    if provenance.evidence is not None and (
        contains_secret(provenance.evidence)
        or classify_sensitivity(provenance.evidence) is MemorySensitivity.CREDENTIAL
    ):
        raise MemoryValidationError("credential-like provenance evidence is not accepted")


def validate_revision(
    content: str,
    confidence: float,
    importance: float,
    half_life_days: float,
    valid_from: datetime | None,
    valid_to: datetime | None,
) -> None:
    if not content.strip() or len(content) > 32768:
        raise MemoryValidationError("memory content must be between 1 and 32768 characters")
    if contains_secret(content) or classify_sensitivity(content) is MemorySensitivity.CREDENTIAL:
        raise MemoryValidationError("credential-like memory content is not accepted")
    if not 0 <= confidence <= 1 or not 0 <= importance <= 1:
        raise MemoryValidationError("confidence and importance must be between zero and one")
    if not MIN_HALF_LIFE_DAYS <= half_life_days <= MAX_HALF_LIFE_DAYS:
        raise MemoryValidationError("half-life must be between 0.25 and 3650 days")
    if valid_from and valid_to and valid_to < valid_from:
        raise MemoryValidationError("valid-to must not precede valid-from")


def validate_memory_text(value: str, field: str = "memory text") -> None:
    if contains_secret(value) or classify_sensitivity(value) is MemorySensitivity.CREDENTIAL:
        raise MemoryValidationError(f"credential-like {field} is not accepted")


class MemoryRepository(Protocol):
    def set_embedding_queue_boundary(
        self, boundary: Callable[..., Awaitable[object]] | None
    ) -> None: ...

    async def reserve_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool: ...

    async def release_reindex_command(
        self, issuer: str, subject: str, idempotency_key: str, fingerprint: str
    ) -> None: ...

    async def complete_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> None: ...

    async def get_run_memory_activity(
        self, run_id: UUID, issuer: str, subject: str
    ) -> MemoryActivitySnapshot: ...

    async def list_memories(
        self, issuer: str, subject: str, filters: MemoryFilters
    ) -> list[MemoryRecord]: ...
    async def get_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...
    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord: ...
    async def reinforce_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...
    async def revise_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...
    async def set_status(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...
    async def set_pinned(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...
    async def purge(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryAuditRecord: ...
    async def register_embedding_generation(
        self, issuer: str, subject: str, **kwargs: object
    ) -> MemoryEmbeddingGeneration: ...
    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration: ...
    async def list_embedding_generations(
        self, issuer: str, subject: str, *, status: str | None = None
    ) -> list[MemoryEmbeddingGeneration]: ...
    async def activate_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration: ...
    async def attach_embedding(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...
    async def save_model_configuration(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration, **kwargs: object
    ) -> MemoryModelConfiguration: ...
    async def get_model_configuration(
        self, issuer: str, subject: str
    ) -> MemoryModelConfiguration: ...
    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob: ...
    async def settle_processing_job(
        self,
        job_id: UUID,
        lease_id: UUID,
        *,
        issuer: str,
        subject: str,
        retryable: bool = False,
        error_class: str | None = None,
    ) -> MemoryProcessingJob: ...
    async def settle_embedding_job(
        self,
        job_id: UUID,
        *,
        issuer: str,
        subject: str,
        lease_id: UUID,
        retryable: bool = False,
        failed: bool = False,
        error_class: str | None = None,
    ) -> MemoryEmbeddingJob: ...
    async def claim_embedding_job_for_revision(
        self,
        issuer: str,
        subject: str,
        *,
        revision_id: UUID,
        generation_id: UUID,
        lease_seconds: float = 60.0,
    ) -> MemoryEmbeddingJob | None: ...

    async def list_candidates(
        self, issuer: str, subject: str, **kwargs: object
    ) -> list[MemoryCandidate]: ...
    async def get_candidate_for_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryCandidate | None: ...
    async def get_candidate(
        self, issuer: str, subject: str, candidate_id: UUID
    ) -> MemoryCandidate: ...
    async def approve_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate: ...
    async def reject_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate: ...


def _fingerprint(operation: str, values: object) -> str:
    return hashlib.sha256(
        json.dumps([operation, values], default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _command_values(values: dict[str, object]) -> dict[str, object]:
    """Return stable command input, excluding generated provenance metadata."""

    return {
        key: value for key, value in values.items() if key not in {"idempotency_key", "provenance"}
    }


def _owner(record: MemoryRecord, issuer: str, subject: str) -> bool:
    return record.issuer == issuer and record.subject == subject


def _scope_authorized(
    record: MemoryRecord,
    scope_type: MemoryScopeType | None,
    agent_profile_id: UUID | None,
    authorized_agent_ids: frozenset[UUID],
) -> bool:
    if scope_type is None and agent_profile_id is None:
        # Omitted detail scope is the user collection.  Agent records require
        # an explicit agent selector; IDs never resolve across scopes.
        return record.scope.type is MemoryScopeType.USER
    if record.scope.type is MemoryScopeType.USER:
        return scope_type in (None, MemoryScopeType.USER) and agent_profile_id is None
    return (
        scope_type is MemoryScopeType.AGENT
        and agent_profile_id == record.scope.agent_profile_id
        and (not authorized_agent_ids or record.scope.agent_profile_id in authorized_agent_ids)
    )


class _MemoryStoreBase:
    """Deterministic in-memory implementation of the public memory port."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.memories: dict[UUID, MemoryRecord] = {}
        self.purge_audit: list[MemoryAuditRecord] = []
        self._idempotency: dict[tuple[str, str, str], tuple[str, object]] = {}
        self._purge_tombstones: set[tuple[str, str, str]] = set()
        self._purge_fences: set[tuple[str, str, UUID]] = set()
        self.embedding_generations: dict[UUID, MemoryEmbeddingGeneration] = {}
        self.embedding_jobs: dict[UUID, MemoryEmbeddingJob] = {}
        self.processing_jobs: dict[UUID, MemoryProcessingJob] = {}
        self.candidates: dict[UUID, MemoryCandidate] = {}
        self.outcomes: list[dict[str, object]] = []
        self.model_configurations: dict[tuple[str, str], MemoryModelConfiguration] = {}
        self._clock = clock or (lambda: datetime.now(UTC))
        self._candidate_evidence_loader: Callable[..., Awaitable[object]] | None = None
        self._embedding_queue_boundary: Callable[..., Awaitable[object]] | None = None

    def set_candidate_evidence_loader(
        self, loader: Callable[..., Awaitable[object]] | None
    ) -> None:
        self._candidate_evidence_loader = loader

    def set_embedding_queue_boundary(
        self, boundary: Callable[..., Awaitable[object]] | None
    ) -> None:
        self._embedding_queue_boundary = boundary

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def reserve_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool:
        generation = self.embedding_generations.get(generation_id)
        if generation is None or generation.issuer != issuer or generation.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        if generation.status != "building":
            raise MemoryValidationError("embedding generation is not resumable")
        command_key = f"reindex:{idempotency_key}"
        prior = self._replay(issuer, subject, command_key, fingerprint)
        if prior is not None:
            return prior is _REINDEX_PENDING
        self._record_replay(
            issuer,
            subject,
            command_key,
            fingerprint,
            _REINDEX_PENDING,
        )
        return True

    async def release_reindex_command(
        self, issuer: str, subject: str, idempotency_key: str, fingerprint: str
    ) -> None:
        command_key = (issuer, subject, f"reindex:{idempotency_key}")
        prior = self._idempotency.get(command_key)
        if prior is not None and prior[0] == fingerprint:
            self._idempotency.pop(command_key, None)

    async def complete_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> None:
        command_key = (issuer, subject, f"reindex:{idempotency_key}")
        prior = self._idempotency.get(command_key)
        if prior is None or prior[0] != fingerprint:
            raise MemoryIdempotencyConflict("reindex command receipt is unavailable")
        generation = await self.get_embedding_generation(issuer, subject, generation_id)
        self._idempotency[command_key] = (
            fingerprint,
            MemoryReindexSnapshot(None, generation, 0, 0),
        )

    async def get_active_embedding_generation(
        self, issuer: str, subject: str
    ) -> MemoryEmbeddingGeneration | None:
        """Return the generation atomically selected for this owner."""
        configuration = self.model_configurations.get((issuer, subject))
        if configuration is None:
            return None
        if configuration.embedding_generation is None:
            matches = [
                item
                for item in self.embedding_generations.values()
                if item.issuer == issuer
                and item.subject == subject
                and item.status == "active"
                and item.model_id == configuration.embedding_model_id
                and item.model_revision == configuration.embedding_model_revision
            ]
            if len(matches) == 1:
                configuration = replace(
                    configuration,
                    embedding_generation=matches[0].id,
                    version=configuration.version + 1,
                )
                self.model_configurations[(issuer, subject)] = configuration
        if configuration.embedding_generation is None:
            return None
        generation = self.embedding_generations.get(configuration.embedding_generation)
        if (
            generation is None
            or generation.status != "active"
            or generation.issuer != issuer
            or generation.subject != subject
        ):
            return None
        return generation

    async def _ensure_active_embedding_job(
        self, issuer: str, subject: str, record: MemoryRecord
    ) -> None:
        """Queue the selected generation without invoking an embedding provider."""

        generation = await self.get_active_embedding_generation(issuer, subject)
        if generation is None:
            return
        queue = self._embedding_queue_boundary or self.queue_embedding_job
        await queue(
            issuer,
            subject,
            memory_id=record.id,
            revision_id=record.current_revision_id,
            generation_id=generation.id,
        )

    async def _stage_active_embedding_job(
        self, issuer: str, subject: str, record: MemoryRecord
    ) -> MemoryEmbeddingJob | None:
        """Persist the selected-generation job without crossing instrumentation."""

        generation = await self.get_active_embedding_generation(issuer, subject)
        if generation is None:
            return None
        self._assert_not_fenced(issuer, subject, record.id)
        job_id = uuid5(
            MEMORY_ID_NAMESPACE,
            f"embedding-job:{issuer}:{subject}:{record.current_revision_id}:{generation.id}",
        )
        existing = self.embedding_jobs.get(job_id)
        if existing is not None:
            return replace(existing, lease_id=None, lease_until=None)
        item = MemoryEmbeddingJob(
            job_id,
            issuer,
            subject,
            record.id,
            record.current_revision_id,
            generation.id,
            available_at=self._now(),
        )
        self.embedding_jobs[job_id] = item
        return item

    def _find(
        self,
        issuer: str,
        subject: str,
        memory_id: UUID,
        *,
        scope_type: MemoryScopeType | None = None,
        agent_profile_id: UUID | None = None,
        authorized_agent_ids: frozenset[UUID] = frozenset(),
    ) -> MemoryRecord:
        record = self.memories.get(memory_id)
        if (
            record is None
            or not _owner(record, issuer, subject)
            or not _scope_authorized(record, scope_type, agent_profile_id, authorized_agent_ids)
        ):
            raise MemoryNotFound("memory not found")
        return record

    def _assert_not_fenced(self, issuer: str, subject: str, memory_id: UUID) -> None:
        if (issuer, subject, memory_id) in self._purge_fences:
            raise MemoryNotFound("memory not found")

    def _candidate_owned(self, issuer: str, subject: str, candidate_id: UUID) -> MemoryCandidate:
        candidate = self.candidates.get(candidate_id)
        if candidate is None or candidate.issuer != issuer or candidate.subject != subject:
            raise MemoryNotFound("memory candidate not found")
        return candidate

    async def get_run_memory_activity(
        self, run_id: UUID, issuer: str, subject: str
    ) -> MemoryActivitySnapshot:
        jobs = [
            item
            for item in self.processing_jobs.values()
            if item.run_id == run_id and item.issuer == issuer and item.subject == subject
        ]
        if not jobs:
            raise MemoryNotFound("run memory activity not found")
        job_ids = {item.id for item in jobs}
        items: list[MemoryActivityItem] = []
        for candidate in self.candidates.values():
            if candidate.job_id not in job_ids:
                continue
            # Ignored and rejected extraction decisions are content-free
            # diagnostics, not owner-facing memory activity.
            if candidate.state is CandidateState.REJECTED:
                continue
            scope = None
            if candidate.scope is not None:
                scope = {"type": candidate.scope.type.value}
                if candidate.scope.agent_profile_id is not None:
                    scope["agentProfileId"] = str(candidate.scope.agent_profile_id)
            if candidate.state is CandidateState.ACCEPTED:
                action = {
                    MemoryAction.CREATE: "created",
                    MemoryAction.REINFORCE: "reinforced",
                    MemoryAction.DISPUTE: "disputed",
                    MemoryAction.SUPERSEDE: "disputed",
                }.get(candidate.action, "queued_for_review")
                status = "completed"
            else:
                action = "queued_for_review"
                candidate_job = next((item for item in jobs if item.id == candidate.job_id), None)
                status = (
                    "completed"
                    if candidate_job is not None
                    and candidate_job.status
                    in {ProcessingJobStatus.COMPLETED, ProcessingJobStatus.FAILED}
                    else "queued"
                )
            items.append(
                MemoryActivityItem(
                    memory_activity_id(candidate.job_id),
                    action,
                    status,
                    scope,
                    candidate_id=candidate.id,
                    memory_id=candidate.memory_id,
                    occurred_at=candidate.decided_at or candidate.created_at,
                )
            )
        processing = (
            "running"
            if any(item.status is ProcessingJobStatus.RUNNING for item in jobs)
            else "queued"
            if any(
                item.status in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE}
                for item in jobs
            )
            else "settled"
        )
        return MemoryActivitySnapshot(
            run_id,
            processing,
            tuple(sorted(items, key=lambda item: (item.occurred_at, item.id))[:100]),
            reconciled_at=self._now(),
        )

    async def list_candidates(
        self, issuer: str, subject: str, **kwargs: object
    ) -> list[MemoryCandidate]:
        state = kwargs.get("state")
        action = kwargs.get("action")
        sensitivity = kwargs.get("sensitivity")
        run_id = kwargs.get("run_id")
        limit = max(1, min(int(kwargs.get("limit", 30)), 100))
        values: list[MemoryCandidate] = []
        for candidate in self.candidates.values():
            if candidate.issuer != issuer or candidate.subject != subject:
                continue
            if state is not None and candidate.state.value != str(state):
                continue
            if action is not None and candidate.action.value != str(action):
                continue
            if sensitivity is not None and candidate.sensitivity.value != str(sensitivity):
                continue
            if run_id is not None:
                job = self.processing_jobs.get(candidate.job_id)
                if job is None or job.run_id != run_id:
                    continue
            cursor_created_at = kwargs.get("cursor_created_at")
            cursor_id = kwargs.get("cursor_id")
            if isinstance(cursor_created_at, datetime) and isinstance(cursor_id, UUID):
                if not (
                    candidate.created_at < cursor_created_at
                    or (
                        candidate.created_at == cursor_created_at
                        and candidate.id < cursor_id
                    )
                ):
                    continue
            values.append(candidate)
        return sorted(values, key=lambda item: (item.created_at, item.id), reverse=True)[:limit]

    async def get_candidate(self, issuer: str, subject: str, candidate_id: UUID) -> MemoryCandidate:
        return self._candidate_owned(issuer, subject, candidate_id)

    async def get_candidate_for_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryCandidate | None:
        matches = [
            candidate
            for candidate in self.candidates.values()
            if candidate.job_id == job_id
            and candidate.issuer == issuer
            and candidate.subject == subject
        ]
        return max(matches, key=lambda item: (item.created_at, item.id), default=None)

    async def _decide_candidate_owner(
        self,
        issuer: str,
        subject: str,
        candidate_id: UUID,
        *,
        expected_version: int,
        idempotency_key: str | None,
        edit: Mapping[str, object] | None = None,
    ) -> MemoryCandidate:
        candidate = self._candidate_owned(issuer, subject, candidate_id)
        fingerprint = _fingerprint(
            "candidate.approve",
            {"candidate": str(candidate_id), "expected": expected_version, "edit": edit or {}},
        )
        command_key = f"candidate:{idempotency_key}" if idempotency_key else None
        prior = self._replay(issuer, subject, command_key, fingerprint)
        pending_command = False
        if prior is not None:
            assert isinstance(prior, MemoryCandidate)
            if prior.state is not CandidateState.RETRYABLE:
                if prior.state is CandidateState.ACCEPTED and prior.memory_id is not None:
                    record = self.memories.get(prior.memory_id)
                    if record is not None:
                        try:
                            await self._ensure_active_embedding_job(issuer, subject, record)
                        except Exception:
                            pass
                return prior
            pending_command = True
        if candidate.version != expected_version:
            raise MemoryVersionConflict("memory candidate version conflict")
        if candidate.state not in {CandidateState.PROPOSED, CandidateState.REVIEW} and not (
            pending_command and candidate.state is CandidateState.RETRYABLE
        ):
            raise MemoryVersionConflict("memory candidate is already decided")
        job = self.processing_jobs.get(candidate.job_id)
        if edit is not None:
            try:
                scope_value = edit["scope"]
                scope = (
                    scope_value
                    if isinstance(scope_value, MemoryScope)
                    else MemoryScope(
                        MemoryScopeType(str(cast(Mapping[str, object], scope_value)["type"])),
                        UUID(str(cast(Mapping[str, object], scope_value)["agentProfileId"]))
                        if cast(Mapping[str, object], scope_value).get("agentProfileId") is not None
                        else None,
                    )
                )
                candidate = replace(
                    candidate,
                    action=MemoryAction(str(edit["action"])),
                    content=str(edit["content"]),
                    kind=MemoryKind(str(edit["kind"])),
                    scope=scope,
                    confidence=float(edit["confidence"]),
                    importance=float(edit["importance"]),
                    half_life_days=float(edit["halfLifeDays"]),
                    valid_to=cast(datetime | None, edit.get("validTo")),
                    related_memory_id=cast(UUID | None, edit.get("relatedMemoryId")),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise MemoryValidationError("memory candidate edit is invalid") from exc
        else:
            # ``review`` is a provider disposition, not an approval action.
            # Normalize legacy rows so an owner can approve grounded content
            # without having to rewrite the candidate first.
            candidate = _normalize_candidate_for_approval(candidate)
        if candidate.content is None or contains_secret(candidate.content):
            raise MemoryValidationError("credential-like candidate content is not accepted")
        if (
            classify_sensitivity(candidate.content) is MemorySensitivity.CREDENTIAL
            or candidate.sensitivity is MemorySensitivity.CREDENTIAL
        ):
            raise MemoryValidationError("credential-like candidate content is not accepted")
        scope = candidate.scope
        if scope is None:
            raise MemoryValidationError("memory candidate scope is required")
        if scope.type is MemoryScopeType.AGENT and scope.agent_profile_id is None:
            raise MemoryValidationError("agent candidate scope requires an agent profile")
        if (
            job is not None
            and scope.type is MemoryScopeType.AGENT
            and scope.agent_profile_id != job.agent_profile_id
        ):
            raise MemoryScopeAuthorizationRequired("candidate agent scope is not authorized")
        validate_revision(
            candidate.content,
            candidate.confidence,
            candidate.importance
            if candidate.importance is not None
            else DEFAULT_MEMORY_IMPORTANCE,
            candidate.half_life_days
            if candidate.half_life_days is not None
            else DEFAULT_MEMORY_HALF_LIFE_DAYS,
            None,
            candidate.valid_to,
        )
        if not pending_command and command_key is not None:
            claimed = replace(candidate, state=CandidateState.RETRYABLE)
            self.candidates[candidate_id] = claimed
            self._record_replay(issuer, subject, command_key, fingerprint, claimed)
        if edit is not None:
            if job is None:
                raise MemoryValidationError("edited memory evidence is unavailable")
            grounded = set(candidate.grounded_message_ids)
            if not grounded or not grounded.issubset(set(job.user_message_ids)):
                raise MemoryValidationError("edited memory candidate grounding is invalid")
            if (
                candidate.scope
                and candidate.scope.type is MemoryScopeType.AGENT
                and candidate.scope.agent_profile_id != job.agent_profile_id
            ):
                raise MemoryScopeAuthorizationRequired("edited agent scope is not authorized")
            if (
                candidate.scope
                and candidate.scope.type is MemoryScopeType.USER
                and not job.allow_shared_user_promotion
            ):
                raise MemoryScopeAuthorizationRequired("shared-user promotion is not authorized")
            conflicts = await self.list_memories(
                issuer,
                subject,
                MemoryFilters(
                    scope_type=None,
                    include_all_scopes=True,
                    include_historical=True,
                    limit=100000,
                ),
            )
            existing_conflict = any(
                item.content == candidate.content and item.scope != candidate.scope
                for item in conflicts
            )
            user_content: str | None = None
            evidence_loader = self._candidate_evidence_loader
            if evidence_loader is not None:
                evidence = await evidence_loader(job)
                user_content = getattr(evidence, "user_content", None)
                if not isinstance(user_content, str):
                    raise MemoryValidationError("edited memory evidence is unavailable")
            decision = decide_candidate(
                candidate,
                user_message_ids=frozenset(job.user_message_ids),
                run_agent_profile_id=job.agent_profile_id,
                existing_conflict=existing_conflict,
                user_content=user_content,
                allow_shared_user_promotion=job.allow_shared_user_promotion,
            )
            if decision.state is CandidateState.REJECTED:
                raise MemoryValidationError("edited memory candidate failed deterministic policy")
        provenance = [
            MemoryProvenance(
                uuid5(MEMORY_ID_NAMESPACE, f"candidate-review:{candidate.id}"),
                "run",
                source_id=job.run_id if job is not None else None,
                run_id=job.run_id if job is not None else None,
                observed_at=self._now(),
            )
        ]
        if candidate.action in {MemoryAction.REINFORCE, MemoryAction.CREATE}:
            matches = await self.list_memories(
                issuer,
                subject,
                MemoryFilters(
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                    include_historical=True,
                    limit=100000,
                ),
            )
            duplicate = next((item for item in matches if item.content == candidate.content), None)
            if duplicate is not None:
                result = await self.reinforce_memory(
                    issuer,
                    subject,
                    duplicate.id,
                    provenance=provenance,
                    idempotency_key=f"memory-action:{candidate.id}",
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                candidate = replace(candidate, memory_id=result.id)
            else:
                result = await self.create_memory(
                    issuer,
                    subject,
                    content=candidate.content,
                    kind=candidate.kind or MemoryKind.SEMANTIC,
                    scope=scope,
                    confidence=candidate.confidence,
                    importance=(
                        candidate.importance
                        if candidate.importance is not None
                        else DEFAULT_MEMORY_IMPORTANCE
                    ),
                    half_life_days=(
                        candidate.half_life_days
                        if candidate.half_life_days is not None
                        else DEFAULT_MEMORY_HALF_LIFE_DAYS
                    ),
                    valid_to=candidate.valid_to,
                    provenance=provenance,
                    idempotency_key=f"memory-action:{candidate.id}",
                    _defer_embedding_queue=True,
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                candidate = replace(candidate, memory_id=result.id)
        elif candidate.action in {MemoryAction.DISPUTE, MemoryAction.SUPERSEDE}:
            if candidate.related_memory_id is None:
                raise MemoryValidationError("related memory is required")
            result = await self.set_status(
                issuer,
                subject,
                candidate.related_memory_id,
                status=(
                    MemoryLifecycleStatus.DISPUTED
                    if candidate.action is MemoryAction.DISPUTE
                    else MemoryLifecycleStatus.SUPERSEDED
                ),
                related_memory_id=None,
                expected_version=(
                    await self.get_memory(
                        issuer,
                        subject,
                        candidate.related_memory_id,
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                    )
                ).version,
                idempotency_key=f"memory-action:{candidate.id}",
                scope_type=scope.type,
                agent_profile_id=scope.agent_profile_id,
            )
            candidate = replace(candidate, memory_id=result.id)
        else:
            raise MemoryValidationError("candidate action is not approvable")
        decided = replace(
            candidate,
            state=CandidateState.ACCEPTED,
            decision_reason="owner_approved",
            version=candidate.version + 1,
            decided_at=self._now(),
        )
        self.candidates[candidate_id] = decided
        self._record_replay(issuer, subject, command_key, fingerprint, decided)
        try:
            await self._ensure_active_embedding_job(issuer, subject, result)
        except Exception:
            # Approval is already durably accepted.  Queue repair is retried
            # by an idempotent approval replay and never invokes inference.
            pass
        return decided

    async def approve_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate:
        return await self._decide_candidate_owner(
            issuer,
            subject,
            candidate_id,
            expected_version=int(kwargs.get("expected_version", 1)),
            idempotency_key=str(kwargs.get("idempotency_key"))
            if kwargs.get("idempotency_key")
            else None,
            edit=cast(Mapping[str, object] | None, kwargs.get("edit")),
        )

    async def reject_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate:
        candidate = self._candidate_owned(issuer, subject, candidate_id)
        expected = int(kwargs.get("expected_version", 1))
        key = str(kwargs.get("idempotency_key")) if kwargs.get("idempotency_key") else None
        reason = str(kwargs.get("reason", "owner_rejected"))
        validate_memory_text(reason, "rejection reason")
        fingerprint = _fingerprint(
            "candidate.reject",
            {"candidate": str(candidate_id), "expected": expected, "reason": reason[:64]},
        )
        command_key = f"candidate:{key}" if key else None
        prior = self._replay(issuer, subject, command_key, fingerprint)
        if prior is not None:
            assert isinstance(prior, MemoryCandidate)
            return prior
        if candidate.version != expected:
            raise MemoryVersionConflict("memory candidate version conflict")
        decided = _content_free_candidate(
            replace(
                candidate,
                state=CandidateState.REJECTED,
                decision_reason=reason[:64],
            )
        )
        decided = replace(
            decided,
            state=CandidateState.REJECTED,
            decision_reason=reason[:64],
            version=candidate.version + 1,
            decided_at=self._now(),
        )
        self.candidates[candidate_id] = decided
        self._record_replay(issuer, subject, command_key, fingerprint, decided)
        return decided


class MemoryProcessingService:
    """Worker-side orchestration for extraction, acceptance, and embedding.

    The service accepts identifier-only job metadata and receives turn content
    from the owner-scoped conversation application port.  Providers are
    injected through the runtime ports; provider payloads never cross this
    boundary.
    """

    def __init__(
        self,
        repository: MemoryRepository,
        inference: Any,
        embedder: Any,
        *,
        clock: Callable[[], datetime] | None = None,
        evidence_loader: Callable[
            [MemoryProcessingJob], Awaitable[MemoryTurnEvidence | tuple[str, str]]
        ]
        | None = None,
        job_loader: Callable[[UUID], Awaitable[MemoryProcessingJob]] | None = None,
        telemetry: MemoryTelemetry | None = None,
        maintenance_telemetry: MemoryTelemetry | None = None,
    ) -> None:
        self.repository = repository
        self.inference = inference
        self.embedder = embedder
        self._clock = clock or (lambda: datetime.now(UTC))
        self._evidence_loader = evidence_loader
        self._job_loader = job_loader
        self._telemetry = telemetry
        self._maintenance_telemetry = maintenance_telemetry
        self.jobs: dict[tuple[str, str, UUID], MemoryProcessingJob] = {}
        self.candidates: dict[UUID, MemoryCandidate] = {}
        self.outcomes: list[dict[str, object]] = []
        self.configurations: dict[tuple[str, str], MemoryModelConfiguration] = {}

    def _emit(
        self,
        operation: str,
        started: float,
        *,
        trace_id: str,
        outcome: str,
        error_class: str | None = None,
        memory_id: UUID | None = None,
        revision_id: UUID | None = None,
        generation_id: UUID | None = None,
        attempt_count: int | None = None,
        backlog: int | None = None,
        progress: float | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            normalized_outcome = {
                "received": "ok",
                "parked": "retryable",
            }.get(outcome, outcome)
            self._telemetry(
                operation=operation,
                duration_ms=max(0.0, (monotonic() - started) * 1000),
                trace_id=trace_id,
                outcome=normalized_outcome,
                error_class=error_class,
                memory_id=str(memory_id) if memory_id else None,
                memory_revision_id=str(revision_id) if revision_id else None,
                generation_id=str(generation_id) if generation_id else None,
                attempt_count=attempt_count,
                backlog=backlog,
                progress=progress,
            )
        except Exception:
            # Telemetry is explicitly non-blocking and cannot alter durable
            # processing state or provider retry behavior.
            return

    def _emit_maintenance_transition(
        self,
        started: float,
        *,
        trace_id: str,
        memory_id: UUID | None,
        destination_status: MemoryLifecycleStatus | None = None,
    ) -> None:
        """Route one lifecycle transition to the maintenance telemetry sink."""

        operation = (
            "memory.decay"
            if destination_status is MemoryLifecycleStatus.DORMANT
            else "memory.archive"
            if destination_status is MemoryLifecycleStatus.ARCHIVED
            else "memory.maintenance"
        )
        if self._maintenance_telemetry is None:
            self._emit(operation, started, trace_id=trace_id, outcome="ok", memory_id=memory_id)
            return
        extraction_telemetry = self._telemetry
        self._telemetry = self._maintenance_telemetry
        try:
            self._emit(operation, started, trace_id=trace_id, outcome="ok", memory_id=memory_id)
        finally:
            self._telemetry = extraction_telemetry

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    async def _embed(self, model_id: str, content: str, context: ProviderTraceContext) -> object:
        embed = cast(Callable[..., Awaitable[object]], self.embedder.embed)
        try:
            return await embed(model_id, content, context=context)
        except TypeError as exc:
            if "context" not in str(exc):
                raise
            return await embed(model_id, content)

    async def _selected_generation(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration
    ) -> MemoryEmbeddingGeneration | None:
        generation_id = configuration.embedding_generation
        if not isinstance(generation_id, UUID):
            return None
        loader = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "get_embedding_generation", None),
        )
        if not callable(loader):
            return None
        try:
            generation = cast(
                MemoryEmbeddingGeneration, await loader(issuer, subject, generation_id)
            )
        except MemoryNotFound:
            return None
        return generation if generation.status == "active" else None

    def _job_provenance(self, job: MemoryProcessingJob) -> tuple[MemoryProvenance, ...]:
        return tuple(
            MemoryProvenance(
                uuid5(
                    MEMORY_ID_NAMESPACE,
                    f"provenance:{job.id}:{message_id}:{job.evidence_digest or ''}",
                ),
                "conversation_message",
                source_id=message_id,
                conversation_id=job.conversation_id,
                run_id=job.run_id,
                message_id=message_id,
                observed_at=self._now(),
                evidence_digest=job.evidence_digest,
            )
            for message_id in sorted(job.user_message_ids, key=str)
        )

    async def configure_models(
        self, configuration: MemoryModelConfiguration
    ) -> MemoryModelConfiguration:
        save = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "save_model_configuration", None),
        )
        if callable(save):
            saved = cast(
                MemoryModelConfiguration,
                await save(configuration.issuer, configuration.subject, configuration),
            )
            configuration = saved
        self.configurations[(configuration.issuer, configuration.subject)] = configuration
        return configuration

    async def model_configuration(self, issuer: str, subject: str) -> MemoryModelConfiguration:
        load = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "get_model_configuration", None),
        )
        if callable(load):
            # Production workers are long-lived while owners may update model
            # selection in the API process.  SQL is authoritative here; a
            # process-local snapshot must not keep jobs parked after setup or
            # an embedding-generation cutover.
            configuration = cast(MemoryModelConfiguration, await load(issuer, subject))
            self.configurations[(issuer, subject)] = configuration
            return configuration
        try:
            return self.configurations[(issuer, subject)]
        except KeyError as exc:
            raise MemoryNotFound("memory model configuration not found") from exc

    async def enqueue(
        self,
        issuer: str,
        subject: str,
        *,
        run_id: UUID,
        conversation_id: UUID,
        correlation_id: UUID | None = None,
        causation_id: UUID | None = None,
        agent_revision_id: UUID | None = None,
        user_message_ids: tuple[UUID, ...] = (),
        assistant_message_ids: tuple[UUID, ...] = (),
        evidence_digest: str | None = None,
        agent_profile_id: UUID | None = None,
        allow_shared_user_promotion: bool = False,
        memory_policy_revision_id: UUID | None = None,
    ) -> MemoryProcessingJob:
        key = (issuer, subject, run_id)
        prior = self.jobs.get(key)
        if prior is not None and prior.status is not ProcessingJobStatus.RETRYABLE:
            return prior
        job = MemoryProcessingJob(
            uuid5(MEMORY_ID_NAMESPACE, f"job:{issuer}:{subject}:{run_id}"),
            issuer,
            subject,
            run_id,
            conversation_id,
            correlation_id=correlation_id,
            causation_id=causation_id,
            available_at=self._now(),
            agent_revision_id=agent_revision_id,
            user_message_ids=user_message_ids,
            assistant_message_ids=assistant_message_ids,
            evidence_digest=evidence_digest,
            agent_profile_id=agent_profile_id,
            allow_shared_user_promotion=allow_shared_user_promotion,
            memory_policy_revision_id=memory_policy_revision_id,
        )
        self.jobs[key] = job
        persist = cast(
            Callable[[MemoryProcessingJob], Awaitable[object]] | None,
            getattr(self.repository, "enqueue_processing_job", None),
        )
        if callable(persist):
            job = cast(MemoryProcessingJob, await persist(job))
            self.jobs[key] = job
        return job

    @staticmethod
    def _action_from_provider(
        raw: object,
        job: MemoryProcessingJob,
        evidence_handles: Mapping[str, UUID] | None = None,
        run_agent_profile_id: UUID | None = None,
    ) -> MemoryCandidate:
        """Normalize a structured port result without accepting extra fields."""

        candidate_id = uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}")
        if isinstance(raw, MemoryCandidate):
            resolved_scope = raw.scope
            if raw.scope is not None and raw.scope.type is MemoryScopeType.AGENT:
                resolved_agent = run_agent_profile_id or job.agent_profile_id
                if resolved_agent is None:
                    raise MemoryValidationError("agent scope cannot be resolved")
                resolved_scope = MemoryScope(MemoryScopeType.AGENT, resolved_agent)
            provider_requested_review = raw.action is MemoryAction.REVIEW
            action = (
                MemoryAction.CREATE
                if provider_requested_review and raw.content is not None
                else raw.action
            )
            return MemoryCandidate(
                candidate_id,
                job.id,
                job.issuer,
                job.subject,
                action,
                raw.content,
                raw.kind,
                resolved_scope,
                raw.confidence,
                raw.importance
                if raw.importance is not None
                else (
                    DEFAULT_MEMORY_IMPORTANCE
                    if raw.content is not None
                    else None
                ),
                raw.half_life_days
                if raw.half_life_days is not None
                else (
                    DEFAULT_MEMORY_HALF_LIFE_DAYS
                    if raw.content is not None
                    else None
                ),
                raw.valid_to,
                raw.sensitivity,
                raw.grounded_message_ids,
                raw.related_memory_id,
                CandidateState.REVIEW if provider_requested_review else raw.state,
                "provider_requested_review" if provider_requested_review else raw.decision_reason,
                retention_basis=raw.retention_basis,
            )
        payload_method = getattr(raw, "as_payload", None)
        data: Mapping[str, object] | None
        if callable(payload_method):
            data = cast(Mapping[str, object], payload_method())
        elif isinstance(raw, Mapping):
            data = cast(Mapping[str, object], raw)
        else:
            data = cast(Mapping[str, object] | None, getattr(raw, "__dict__", None))
        if not isinstance(data, Mapping):
            raise MemoryValidationError("structured memory action is malformed")
        allowed = {
            "action",
            "content",
            "kind",
            "scope_type",
            "agent_profile_id",
            "confidence",
            "importance",
            "half_life_days",
            "valid_to",
            "sensitivity",
            "retention_basis",
            "grounded_message_ids",
            "grounded_evidence_handles",
            "related_memory_id",
        }
        if set(data) - allowed:
            raise MemoryValidationError("structured memory action contains unsupported fields")
        required = {
            "action",
            "content",
            "kind",
            "scope_type",
            "agent_profile_id",
            "confidence",
            "importance",
            "half_life_days",
            "valid_to",
            "sensitivity",
            "retention_basis",
            "grounded_evidence_handles",
            "related_memory_id",
        }
        if not required <= set(data):
            raise MemoryValidationError("structured memory action is incomplete")
        try:
            action = MemoryAction(str(data.get("action")))
            kind = MemoryKind(str(data["kind"])) if data.get("kind") is not None else None
            scope = None
            if data.get("scope_type") is not None:
                scope_type = MemoryScopeType(str(data["scope_type"]))
                if scope_type is MemoryScopeType.AGENT:
                    # Provider UUIDs are proposals only.  Bind the candidate
                    # to the server-resolved current run agent.
                    resolved_agent = run_agent_profile_id or job.agent_profile_id
                    if resolved_agent is None:
                        raise MemoryValidationError("agent scope cannot be resolved")
                    scope = MemoryScope(scope_type, resolved_agent)
                else:
                    scope = MemoryScope(scope_type)
            raw_handles = data.get("grounded_evidence_handles", ())
            if not isinstance(raw_handles, (list, tuple)):
                raise MemoryValidationError("grounded evidence handles are malformed")
            handle_map = evidence_handles or {}
            if any(str(item) not in handle_map for item in raw_handles):
                raise MemoryValidationError("grounded evidence handle is unavailable")
            grounded = tuple(handle_map[str(item)] for item in raw_handles)
            # Provider-supplied database IDs are never trusted.  They are
            # accepted only as an ungrounded legacy shape, forcing review.
            raw_grounded = data.get("grounded_message_ids", ())
            if not isinstance(raw_grounded, (list, tuple)):
                raise MemoryValidationError("grounded message identifiers are malformed")
            if raw_grounded:
                grounded = ()
            if data.get("related_memory_id") is not None:
                raise MemoryValidationError("related memory identifiers require server resolution")
            related = None
            valid_to = data.get("valid_to")
            if isinstance(valid_to, str):
                valid_to = datetime.fromisoformat(valid_to)
                if valid_to.tzinfo is None:
                    raise MemoryValidationError("valid-to must include timezone")
            elif valid_to is not None:
                raise MemoryValidationError("valid-to must be an RFC3339 string or null")
            content = str(data["content"]) if data.get("content") is not None else None
            retention_basis = MemoryRetentionBasis(str(data["retention_basis"]))
            if action in {MemoryAction.CREATE, MemoryAction.REVIEW}:
                if (
                    not content
                    or kind is None
                    or scope is None
                    or data.get("importance") is None
                    or data.get("half_life_days") is None
                    or not grounded
                    or retention_basis is MemoryRetentionBasis.NONE
                ):
                    raise MemoryValidationError("memory action proposal is incomplete")
            provider_requested_review = action is MemoryAction.REVIEW
            if provider_requested_review and content is not None:
                action = MemoryAction.CREATE
            return MemoryCandidate(
                candidate_id,
                job.id,
                job.issuer,
                job.subject,
                action,
                content,
                kind,
                scope,
                float(data.get("confidence", 0.0)),
                (
                    float(data["importance"])
                    if data.get("importance") is not None
                    else (DEFAULT_MEMORY_IMPORTANCE if content is not None else None)
                ),
                (
                    float(data["half_life_days"])
                    if data.get("half_life_days") is not None
                    else (DEFAULT_MEMORY_HALF_LIFE_DAYS if content is not None else None)
                ),
                valid_to,
                MemorySensitivity(str(data.get("sensitivity", "ordinary"))),
                grounded,
                related,
                CandidateState.REVIEW if provider_requested_review else CandidateState.PROPOSED,
                "provider_requested_review" if provider_requested_review else None,
                retention_basis=retention_basis,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise MemoryValidationError("structured memory action is malformed") from exc

    async def process(
        self,
        job: MemoryProcessingJob,
        *,
        user_content: str,
        assistant_content: str,
        user_message_ids: frozenset[UUID],
        run_agent_profile_id: UUID | None = None,
        provenance: Iterable[MemoryProvenance] = (),
        allow_shared_user_promotion: bool = False,
    ) -> MemoryCandidate:
        trace_id = (job.correlation_id or job.id).hex
        process_started = monotonic()
        queue_wait = max(0.0, (datetime.now(UTC) - job.available_at).total_seconds())
        queue_started = monotonic() - queue_wait
        self._emit(
            "memory.job.queue",
            queue_started,
            trace_id=trace_id,
            outcome="ok",
            attempt_count=job.attempt_count,
            backlog=len(self.jobs),
        )
        # A purge fences the deterministic action key, including delayed
        # in-memory redelivery after a worker restart.  Check the fence before
        # invoking inference so a purged turn cannot recreate content.
        tombstones = getattr(self.repository, "_purge_tombstones", set())
        if (job.issuer, job.subject, f"memory-job:{job.id}") in tombstones:
            raise MemoryIdempotencyConflict("memory action is unavailable after purge")
        config = await self.model_configuration(job.issuer, job.subject)
        prior = next((item for item in self.candidates.values() if item.job_id == job.id), None)
        if prior is not None and prior.state is not CandidateState.RETRYABLE:
            return prior
        # Secrets are rejected from the turn before constructing an inference
        # request.  Persist only the deterministic, content-free outcome.
        if classify_sensitivity(user_content) is MemorySensitivity.CREDENTIAL or contains_secret(
            assistant_content
        ):
            candidate = MemoryCandidate(
                uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}"),
                job.id,
                job.issuer,
                job.subject,
                MemoryAction.IGNORE,
                None,
                None,
                None,
                0.0,
                state=CandidateState.REJECTED,
                decision_reason="credential",
                sensitivity=MemorySensitivity.CREDENTIAL,
            )
            self.candidates[candidate.id] = candidate
            persist_candidate = cast(
                Callable[[MemoryCandidate], Awaitable[object]] | None,
                getattr(self.repository, "persist_candidate", None),
            )
            if callable(persist_candidate):
                await persist_candidate(candidate)
            record_outcome = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "record_action_outcome", None),
            )
            if callable(record_outcome):
                await record_outcome(
                    candidate_id=candidate.id,
                    job_id=job.id,
                    issuer=job.issuer,
                    subject=job.subject,
                    action=MemoryAction.IGNORE.value,
                    outcome="ignored",
                    error_class="credential",
                )
            self._emit(
                "memory.candidate",
                process_started,
                trace_id=trace_id,
                outcome="rejected",
                error_class="credential",
                attempt_count=job.attempt_count,
            )
            return candidate
        # Evidence handles are intentionally invocation-local capabilities.
        # They contain no durable identifiers and cannot be replayed by a
        # provider or correlated across jobs.
        ordered_message_ids = sorted(user_message_ids, key=str)
        segment_count = max(1, math.ceil(len(user_content) / 8192))
        invocation_nonce = secrets.token_urlsafe(18)
        evidence_handles = (
            {
                secrets.token_urlsafe(18): ordered_message_ids[
                    min(index, len(ordered_message_ids) - 1)
                ]
                for index in range(segment_count)
            }
            if ordered_message_ids
            else {}
        )
        request_segments: list[dict[str, str]] = []
        handle_values = tuple(evidence_handles)
        for index in range(segment_count):
            handle = (
                handle_values[index]
                if index < len(handle_values)
                else f"opaque:{invocation_nonce}:{index}"
            )
            request_segments.append(
                {
                    "handle": handle,
                    "text": user_content[index * 8192 : (index + 1) * 8192],
                }
            )
        request = StructuredInferenceRequest(
            model_id=config.extraction_model_id,
            schema=MEMORY_ACTION_SCHEMA,
            input={
                "decision_contract": {
                    "allowed_actions": [item.value for item in MemoryAction],
                    "instructions": (
                        f"Apply {MEMORY_EXTRACTION_POLICY_VERSION}. Extract only durable "
                        "owner-relevant facts explicitly stated by the user. "
                        "Ignore ordinary world knowledge, topical questions, and facts "
                        "provided only by the assistant. "
                        "For create or review, copy the durable fact into content, choose "
                        "kind from the enum, choose scope_type agent or user, and return "
                        "the matching opaque evidence handle. Set retention_basis to personal "
                        "for owner facts, or explicit_request only when the user asks Aura "
                        "to remember/save/note it. Use none with ignore otherwise. Choose "
                        "importance from "
                        "0 to 1 based on durable value and propose a half_life_days between "
                        "0.25 and 3650 based on the fact's relevance horizon; if uncertain, "
                        "use 0.5 importance and 30 days. Set valid_to only when the user "
                        "states a concrete end date; otherwise return null and do not invent "
                        "an expiry. Use action ignore when there is no durable personal or "
                        "explicitly requested fact. Never invent identifiers or evidence "
                        "handles. Assistant context is background only and is never grounding."
                    ),
                    "required_for_create_or_review": [
                        "content",
                        "kind",
                        "scope_type",
                        "retention_basis",
                        "grounded_evidence_handles",
                        "importance",
                        "half_life_days",
                        "sensitivity",
                        "valid_to",
                        "agent_profile_id",
                        "related_memory_id",
                    ],
                    "scope_guidance": {
                        "agent": "private to the current agent",
                        "user": "shared user memory; policy may require review",
                    },
                },
                "evidence_segments": request_segments,
                "assistant_context": assistant_content[:8192],
            },
            trace=ProviderTraceContext(
                trace_id=trace_id,
                correlation_id=str(job.correlation_id) if job.correlation_id else None,
                causation_id=str(job.causation_id) if job.causation_id else None,
                job_id=str(job.id),
                run_id=str(job.run_id),
                conversation_id=str(job.conversation_id),
            ),
        )
        extraction_started = monotonic()
        try:
            infer = cast(
                Callable[[StructuredInferenceRequest], Awaitable[object]] | None,
                getattr(self.inference, "infer", None),
            )
            if not callable(infer):
                raise MemoryValidationError("structured inference provider is unavailable")
            raw = await infer(request)
        except Exception:
            self._emit(
                "memory.extraction",
                extraction_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                attempt_count=job.attempt_count,
            )
            candidate = MemoryCandidate(
                uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}"),
                job.id,
                job.issuer,
                job.subject,
                MemoryAction.REVIEW,
                None,
                None,
                None,
                0.0,
                state=CandidateState.RETRYABLE,
                decision_reason="provider_error",
            )
        else:
            try:
                candidate = self._action_from_provider(
                    raw,
                    job,
                    evidence_handles,
                    run_agent_profile_id=run_agent_profile_id or job.agent_profile_id,
                )
                # Provider output cannot choose ownership or bind a candidate
                # to a different job.  Normalize the identity to the server
                # job before any persistence, while retaining only
                # policy-relevant fields.
                if candidate.issuer != job.issuer or candidate.subject != job.subject:
                    raise MemoryValidationError("structured action owner mismatch")
                if candidate.job_id != job.id:
                    candidate = MemoryCandidate(
                        candidate.id,
                        job.id,
                        job.issuer,
                        job.subject,
                        candidate.action,
                        candidate.content,
                        candidate.kind,
                        candidate.scope,
                        candidate.confidence,
                        candidate.importance,
                        candidate.half_life_days,
                        candidate.valid_to,
                        candidate.sensitivity,
                        candidate.grounded_message_ids,
                        candidate.related_memory_id,
                        candidate.state,
                        candidate.decision_reason,
                        retention_basis=candidate.retention_basis,
                    )
            except MemoryValidationError:
                # The provider transport succeeded, but its schema-valid
                # payload did not satisfy the domain action contract.  This is
                # terminal review work, not an outage: retrying would replay
                # the same malformed decision indefinitely.  Do not retain
                # any provider-supplied content in this diagnostic outcome.
                self._emit(
                    "memory.extraction",
                    extraction_started,
                    trace_id=trace_id,
                    outcome="error",
                    error_class="validation",
                    attempt_count=job.attempt_count,
                )
                candidate = MemoryCandidate(
                    uuid5(MEMORY_ID_NAMESPACE, f"candidate:{job.id}"),
                    job.id,
                    job.issuer,
                    job.subject,
                    MemoryAction.IGNORE,
                    None,
                    None,
                    None,
                    0.0,
                    state=CandidateState.REJECTED,
                    decision_reason="invalid_provider_output",
                    retention_basis=MemoryRetentionBasis.NONE,
                )
            else:
                self._emit(
                    "memory.extraction",
                    extraction_started,
                    trace_id=trace_id,
                    outcome="ok",
                    attempt_count=job.attempt_count,
                )
        if candidate.state is CandidateState.RETRYABLE:
            self.candidates[candidate.id] = candidate
            persist_candidate = cast(
                Callable[[MemoryCandidate], Awaitable[object]] | None,
                getattr(self.repository, "persist_candidate", None),
            )
            if callable(persist_candidate):
                await persist_candidate(candidate)
            self._emit(
                "memory.error",
                process_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.retry",
                process_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="provider",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                process_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="provider",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            self.outcomes.append({"job_id": job.id, "action": "review", "outcome": "retryable"})
            return candidate
        conflict = False
        if candidate.content:
            existing = await self.repository.list_memories(
                job.issuer,
                job.subject,
                MemoryFilters(
                    scope_type=None,
                    include_all_scopes=True,
                    include_historical=True,
                    q=candidate.content,
                    limit=200,
                ),
            )
            conflict = any(
                item.content == candidate.content
                and item.scope != (candidate.scope or MemoryScope(MemoryScopeType.USER))
                for item in existing
            )
        retention_gate_started = monotonic()
        retention_basis = classify_retention_basis(user_content)
        if candidate.action is not MemoryAction.IGNORE:
            candidate = replace(candidate, retention_basis=retention_basis)
            candidate = normalize_retention_horizon(candidate, user_content=user_content)
        self._emit(
            "memory.retention_gate",
            retention_gate_started,
            trace_id=trace_id,
            outcome=retention_basis.value,
            attempt_count=job.attempt_count,
        )
        policy_started = monotonic()
        provider_requested_review = candidate.decision_reason == "provider_requested_review"
        if candidate.decision_reason == "invalid_provider_output":
            decision = CandidateDecision(CandidateState.REJECTED, "invalid_provider_output")
        elif candidate.action is not MemoryAction.IGNORE and retention_basis is MemoryRetentionBasis.NONE:
            decision = CandidateDecision(CandidateState.REJECTED, "not_personal")
        else:
            decision = decide_candidate(
                candidate,
                user_message_ids=user_message_ids,
                run_agent_profile_id=run_agent_profile_id or job.agent_profile_id,
                existing_conflict=conflict,
                user_content=user_content,
                allow_shared_user_promotion=allow_shared_user_promotion,
            )
        if provider_requested_review and decision.state is CandidateState.ACCEPTED:
            decision = CandidateDecision(CandidateState.REVIEW, "provider_requested_review")
        decision_outcome = decision.state.value
        if candidate.action is MemoryAction.IGNORE and decision.reason != "invalid_provider_output":
            decision_outcome = "ignored"
        self._emit(
            "memory.policy",
            policy_started,
            trace_id=trace_id,
            outcome=decision_outcome,
            attempt_count=job.attempt_count,
        )
        candidate = replace(candidate, state=decision.state, decision_reason=decision.reason)
        candidate = _content_free_candidate(candidate)
        self.candidates[candidate.id] = candidate
        persist_candidate = cast(
            Callable[[MemoryCandidate], Awaitable[object]] | None,
            getattr(self.repository, "persist_candidate", None),
        )
        if callable(persist_candidate):
            await persist_candidate(candidate)
        if decision.state is not CandidateState.ACCEPTED:
            self._emit(
                "memory.candidate",
                process_started,
                trace_id=trace_id,
                outcome=decision_outcome,
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                process_started,
                trace_id=trace_id,
                outcome=decision.state.value,
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            self.outcomes.append(
                {
                    "job_id": job.id,
                    "action": candidate.action.value,
                    "outcome": "review" if decision.state is CandidateState.REVIEW else "ignored",
                }
            )
            record_outcome = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "record_action_outcome", None),
            )
            if callable(record_outcome):
                await record_outcome(
                    candidate_id=candidate.id,
                    job_id=job.id,
                    issuer=job.issuer,
                    subject=job.subject,
                    action=candidate.action.value,
                    outcome="review" if decision.state is CandidateState.REVIEW else "ignored",
                )
            return candidate
        scope = candidate.scope or MemoryScope(MemoryScopeType.USER)
        prov = tuple(provenance)
        action_started = monotonic()
        matches = await self.repository.list_memories(
            job.issuer,
            job.subject,
            MemoryFilters(
                scope_type=scope.type,
                agent_profile_id=scope.agent_profile_id,
                include_historical=True,
                limit=100000,
            ),
        )
        duplicate = next(
            (item for item in matches if candidate.content and item.content == candidate.content),
            None,
        )
        if duplicate is not None and candidate.action in {
            MemoryAction.CREATE,
            MemoryAction.REINFORCE,
        }:
            reinforce = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "reinforce_memory", None),
            )
            if callable(reinforce):
                record = cast(
                    MemoryRecord,
                    await reinforce(
                        job.issuer,
                        job.subject,
                        duplicate.id,
                        provenance=prov,
                        idempotency_key=f"memory-action:{candidate.id}",
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                        authorized_agent_ids=frozenset({scope.agent_profile_id})
                        if scope.agent_profile_id
                        else frozenset(),
                    ),
                )
            else:
                record = duplicate
                record.reinforced_at = self._now()
                record.status = MemoryLifecycleStatus.ACTIVE
                record.provenance.extend(prov)
        elif candidate.action is MemoryAction.CREATE:
            record = await self.repository.create_memory(
                job.issuer,
                job.subject,
                kind=candidate.kind or MemoryKind.SEMANTIC,
                scope=scope,
                content=candidate.content,
                confidence=candidate.confidence,
                importance=candidate.importance or 0.5,
                half_life_days=candidate.half_life_days or 30.0,
                valid_to=candidate.valid_to,
                provenance=prov,
                idempotency_key=f"memory-job:{job.id}",
                agent_profile_id=scope.agent_profile_id,
            )
        else:
            record = next(
                (
                    item
                    for item in matches
                    if candidate.content and item.content == candidate.content
                ),
                None,
            )
            if record is None:
                if (
                    candidate.action in {MemoryAction.SUPERSEDE, MemoryAction.DISPUTE}
                    and candidate.related_memory_id
                ):
                    related = await self.repository.get_memory(
                        job.issuer,
                        job.subject,
                        candidate.related_memory_id,
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                    )
                    transition = (
                        MemoryLifecycleStatus.SUPERSEDED
                        if candidate.action is MemoryAction.SUPERSEDE
                        else MemoryLifecycleStatus.DISPUTED
                    )
                    record = await self.repository.set_status(
                        job.issuer,
                        job.subject,
                        candidate.related_memory_id,
                        status=transition,
                        expected_version=related.version,
                        scope_type=scope.type,
                        agent_profile_id=scope.agent_profile_id,
                    )
                else:
                    record = await self.repository.create_memory(
                        job.issuer,
                        job.subject,
                        kind=candidate.kind or MemoryKind.SEMANTIC,
                        scope=scope,
                        content=candidate.content,
                        confidence=candidate.confidence,
                        importance=candidate.importance or 0.5,
                        half_life_days=candidate.half_life_days or 30.0,
                        provenance=prov,
                        idempotency_key=f"memory-job:{job.id}",
                        agent_profile_id=scope.agent_profile_id,
                    )
            else:
                record.reinforced_at = self._now()
                record.status = MemoryLifecycleStatus.ACTIVE
                record.provenance.extend(prov)
        candidate = replace(candidate, memory_id=record.id)
        link_job = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "link_processing_job_memory", None),
        )
        if callable(link_job):
            await link_job(job.id, job.issuer, job.subject, record.id)
        self.candidates[candidate.id] = candidate
        persist_candidate = cast(
            Callable[[MemoryCandidate], Awaitable[object]] | None,
            getattr(self.repository, "persist_candidate", None),
        )
        if callable(persist_candidate):
            await persist_candidate(candidate)
        self.outcomes.append(
            {
                "job_id": job.id,
                "action": candidate.action.value,
                "outcome": "created",
                "memory_id": record.id,
                "revision_id": record.current_revision_id,
            }
        )
        record_outcome = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "record_action_outcome", None),
        )
        if callable(record_outcome):
            await record_outcome(
                candidate_id=candidate.id,
                job_id=job.id,
                issuer=job.issuer,
                subject=job.subject,
                action=candidate.action.value,
                outcome="created",
                memory_id=record.id,
                revision_id=record.current_revision_id,
            )
        self._emit(
            "memory.candidate",
            process_started,
            trace_id=trace_id,
            outcome="accepted",
            memory_id=record.id,
            revision_id=record.current_revision_id,
            attempt_count=job.attempt_count,
        )
        self._emit(
            "memory.action",
            action_started,
            trace_id=trace_id,
            outcome="accepted",
            memory_id=record.id,
            revision_id=record.current_revision_id,
            attempt_count=job.attempt_count,
        )
        # Embedding failures remain retryable worker work; the accepted memory
        # is already durable and never turns a completed conversation into an error.
        embedding_job: MemoryEmbeddingJob | None = None
        embedding_started = monotonic()
        try:
            selected_generation = await self._selected_generation(job.issuer, job.subject, config)
            generation_id = selected_generation.id if selected_generation is not None else None
            if selected_generation is not None:
                queue_embedding = cast(
                    Callable[..., Awaitable[object]] | None,
                    getattr(self.repository, "queue_embedding_job", None),
                )
                if callable(queue_embedding):
                    queued_embedding = cast(
                        MemoryEmbeddingJob,
                        await queue_embedding(
                            job.issuer,
                            job.subject,
                            memory_id=record.id,
                            revision_id=record.current_revision_id,
                            generation_id=generation_id,
                        ),
                    )
                    claim_embedding = cast(
                        Callable[..., Awaitable[object]] | None,
                        getattr(self.repository, "claim_embedding_job_by_id", None),
                    )
                    if callable(claim_embedding):
                        claimed_embedding = await claim_embedding(
                            queued_embedding.id, job.issuer, job.subject
                        )
                        if claimed_embedding is None:
                            raise MemoryValidationError(
                                "embedding work is already claimed or settled"
                            )
                        embedding_job = cast(MemoryEmbeddingJob, claimed_embedding)
                    else:
                        raise MemoryValidationError("embedding claim capability is unavailable")
                if embedding_job is None or embedding_job.lease_id is None:
                    raise MemoryValidationError("embedding work was not durably claimed")
                if (
                    embedding_job.issuer != job.issuer
                    or embedding_job.subject != job.subject
                    or embedding_job.memory_id != record.id
                    or embedding_job.revision_id != record.current_revision_id
                    or embedding_job.generation_id != generation_id
                ):
                    raise MemoryValidationError("embedding work does not match accepted revision")
                embedding = cast(
                    Any,
                    await self._embed(
                        selected_generation.model_id,
                        record.content,
                        ProviderTraceContext(
                            trace_id=trace_id,
                            correlation_id=str(job.correlation_id) if job.correlation_id else None,
                            causation_id=str(job.causation_id) if job.causation_id else None,
                            job_id=str(job.id),
                            run_id=str(job.run_id),
                            conversation_id=str(job.conversation_id),
                            generation_id=str(generation_id),
                        ),
                    ),
                )
                if (
                    embedding.model_id != selected_generation.model_id
                    or embedding.model_revision != selected_generation.model_revision
                    or embedding.model_digest != selected_generation.model_digest
                    or embedding.dimension != selected_generation.dimension
                ):
                    raise MemoryValidationError("embedding provider identity or dimension mismatch")
                self._emit(
                    "memory.embedding",
                    embedding_started,
                    trace_id=trace_id,
                    outcome="ok",
                    memory_id=record.id,
                    revision_id=record.current_revision_id,
                    generation_id=generation_id,
                    attempt_count=job.attempt_count,
                    backlog=0,
                )
                await self.repository.attach_embedding(
                    job.issuer,
                    job.subject,
                    record.id,
                    revision_id=record.current_revision_id,
                    generation_id=UUID(str(generation_id)),
                    vector=embedding.vector,
                    digest=embedding.digest,
                    model_id=embedding.model_id,
                    model_revision=embedding.model_revision,
                    model_digest=embedding.model_digest,
                    scope_type=scope.type,
                    agent_profile_id=scope.agent_profile_id,
                )
                settle_embedding = cast(
                    Callable[..., Awaitable[object]] | None,
                    getattr(self.repository, "settle_embedding_job", None),
                )
                if callable(settle_embedding):
                    await settle_embedding(
                        embedding_job.id,
                        issuer=job.issuer,
                        subject=job.subject,
                        lease_id=embedding_job.lease_id,
                    )
            else:
                # The accepted revision is durable, but without a selected
                # active generation it remains an explicit missing-embedding
                # backlog item for maintenance/reindex.
                self._emit(
                    "memory.embedding",
                    embedding_started,
                    trace_id=trace_id,
                    outcome="retryable",
                    memory_id=record.id,
                    revision_id=record.current_revision_id,
                    backlog=1,
                )
        except Exception:
            self._emit(
                "memory.embedding",
                embedding_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                memory_id=record.id,
                revision_id=record.current_revision_id,
                attempt_count=job.attempt_count,
                backlog=1,
            )
            self._emit(
                "memory.error",
                process_started,
                trace_id=trace_id,
                outcome="error",
                error_class="provider",
                memory_id=record.id,
                revision_id=record.current_revision_id,
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.retry",
                process_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="provider",
                memory_id=record.id,
                revision_id=record.current_revision_id,
                attempt_count=job.attempt_count,
            )
            self.outcomes.append(
                {
                    "job_id": job.id,
                    "action": "embedding",
                    "outcome": "retryable",
                    "memory_id": record.id,
                }
            )
            if embedding_job is not None:
                settle_embedding = cast(
                    Callable[..., Awaitable[object]] | None,
                    getattr(self.repository, "settle_embedding_job", None),
                )
                if callable(settle_embedding):
                    if embedding_job.lease_id is not None:
                        await settle_embedding(
                            embedding_job.id,
                            issuer=job.issuer,
                            subject=job.subject,
                            lease_id=embedding_job.lease_id,
                            retryable=True,
                            error_class="provider",
                        )
        self._emit(
            "memory.job",
            process_started,
            trace_id=trace_id,
            outcome="accepted",
            memory_id=record.id,
            revision_id=record.current_revision_id,
            attempt_count=job.attempt_count,
            backlog=len(self.jobs),
        )
        return candidate

    async def _resume_accepted_candidate(
        self, job: MemoryProcessingJob, candidate: MemoryCandidate
    ) -> MemoryCandidate:
        """Resume the durable action phase without a second model decision."""

        if candidate.memory_id is not None:
            return candidate
        if candidate.content is None or candidate.scope is None:
            raise MemoryValidationError("accepted candidate is incomplete")
        matches = await self.repository.list_memories(
            job.issuer,
            job.subject,
            MemoryFilters(
                scope_type=candidate.scope.type,
                agent_profile_id=candidate.scope.agent_profile_id,
                include_historical=True,
                limit=100000,
            ),
        )
        record = next((item for item in matches if item.content == candidate.content), None)
        if record is None:
            record = await self.repository.create_memory(
                job.issuer,
                job.subject,
                kind=candidate.kind or MemoryKind.SEMANTIC,
                scope=candidate.scope,
                content=candidate.content,
                confidence=candidate.confidence,
                importance=candidate.importance or 0.5,
                half_life_days=candidate.half_life_days or 30.0,
                valid_to=candidate.valid_to,
                provenance=self._job_provenance(job),
                idempotency_key=f"memory-job:{job.id}",
                agent_profile_id=candidate.scope.agent_profile_id,
            )
        else:
            record = await self.repository.reinforce_memory(
                job.issuer,
                job.subject,
                record.id,
                provenance=self._job_provenance(job),
                idempotency_key=f"memory-action:{candidate.id}",
                scope_type=candidate.scope.type,
                agent_profile_id=candidate.scope.agent_profile_id,
            )
        candidate = replace(candidate, memory_id=record.id)
        link_job = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "link_processing_job_memory", None),
        )
        if callable(link_job):
            await link_job(job.id, job.issuer, job.subject, record.id)
        persist = cast(
            Callable[[MemoryCandidate], Awaitable[object]] | None,
            getattr(self.repository, "persist_candidate", None),
        )
        if callable(persist):
            await persist(candidate)
        record_outcome = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "record_action_outcome", None),
        )
        if callable(record_outcome):
            await record_outcome(
                candidate_id=candidate.id,
                job_id=job.id,
                issuer=job.issuer,
                subject=job.subject,
                action=candidate.action.value,
                outcome="created",
                memory_id=record.id,
                revision_id=record.current_revision_id,
            )
        return candidate

    async def process_job(
        self, job_id: UUID, *, lease_id: UUID | None = None
    ) -> MemoryCandidate | None:
        """Process a previously enqueued job when its evidence is available.

        Durable deployments hydrate the evidence through the conversation
        application port before calling ``process``.  The in-memory adapter
        keeps this method intentionally content-free for worker composition.
        """

        job = next((item for item in self.jobs.values() if item.id == job_id), None)
        if job is None:
            loader = self._job_loader
            if loader is not None:
                job = await loader(job_id)
            else:
                raise MemoryValidationError(
                    "owner-scoped memory processing lookup requires a job loader"
                )
            self.jobs[(job.issuer, job.subject, job.run_id)] = job
        elif lease_id is not None:
            # A worker may pass a lease obtained from a fresh claim while the
            # process-local cache still contains an older retryable snapshot.
            # Replace that snapshot before validating the capability.
            durable_loader = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "get_processing_job", None),
            )
            if callable(durable_loader):
                job = cast(
                    MemoryProcessingJob, await durable_loader(job.id, job.issuer, job.subject)
                )
        job_started = monotonic()
        trace_id = (job.correlation_id or job.id).hex
        claim = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "claim_processing_job_by_id", None),
        )
        if callable(claim):
            # A loaded job is only a snapshot.  Never reuse its lease: every
            # delivery must obtain a fresh capability from the durable owner
            # boundary.  The sole exception is a lease returned by the same
            # claim call and explicitly handed in by a worker loop.
            if lease_id is None:
                claimed = await claim(job.id, job.issuer, job.subject)
                if claimed is None:
                    # A completed job or an unexpired lease is already settled
                    # by another worker.  Do not invoke inference twice.
                    return None
                job = cast(MemoryProcessingJob, claimed)
                lease_id = job.lease_id
            elif job.lease_id != lease_id:
                raise MemoryValidationError("processing job lease capability is stale")
            self.jobs[(job.issuer, job.subject, job.run_id)] = job

        async def settle_evidence_rejection() -> None:
            capability = lease_id or job.lease_id
            if capability is None:
                return
            settle = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "settle_processing_job", None),
            )
            if callable(settle):
                settled = await settle(
                    job.id,
                    capability,
                    issuer=job.issuer,
                    subject=job.subject,
                    retryable=False,
                    error_class="evidence",
                )
                if isinstance(settled, MemoryProcessingJob):
                    self.jobs[(settled.issuer, settled.subject, settled.run_id)] = settled

        async def settle_parked(error_class: str) -> None:
            capability = lease_id or job.lease_id
            if capability is None:
                return
            settle = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "settle_processing_job", None),
            )
            if callable(settle):
                settled = await settle(
                    job.id,
                    capability,
                    issuer=job.issuer,
                    subject=job.subject,
                    retryable=True,
                    error_class=error_class,
                )
                if isinstance(settled, MemoryProcessingJob):
                    self.jobs[(settled.issuer, settled.subject, settled.run_id)] = settled

        prior = next((item for item in self.candidates.values() if item.job_id == job.id), None)
        if prior is None:
            durable_candidate_loader = cast(
                Callable[[UUID, str, str], Awaitable[object]] | None,
                getattr(self.repository, "get_candidate_for_job", None),
            )
            if callable(durable_candidate_loader):
                prior_value = await durable_candidate_loader(job.id, job.issuer, job.subject)
                if prior_value is not None:
                    prior = cast(MemoryCandidate, prior_value)
                    self.candidates[prior.id] = prior
        if prior is not None and prior.state is not CandidateState.RETRYABLE:
            # Accepted candidates carry the durable memory identity.  Resume
            # the embedding phase after a worker crash without asking the
            # extractor to make a second decision.  Review/rejected outcomes
            # are already terminal and need no provider work.
            if prior.state is CandidateState.ACCEPTED and prior.memory_id is None:
                prior = await self._resume_accepted_candidate(job, prior)
            if prior.state is CandidateState.ACCEPTED and prior.memory_id is not None:
                try:
                    config = await self.model_configuration(job.issuer, job.subject)
                    selected_generation = await self._selected_generation(
                        job.issuer, job.subject, config
                    )
                    generation_id = (
                        selected_generation.id if selected_generation is not None else None
                    )
                    if selected_generation is not None:
                        record = await self.repository.get_memory(
                            job.issuer,
                            job.subject,
                            prior.memory_id,
                            scope_type=prior.scope.type if prior.scope else None,
                            agent_profile_id=prior.scope.agent_profile_id if prior.scope else None,
                        )
                        if not any(
                            item.revision_id == record.current_revision_id
                            and item.generation_id == generation_id
                            for item in record.embeddings
                        ):
                            queue_embedding = cast(
                                Callable[..., Awaitable[object]] | None,
                                getattr(self.repository, "queue_embedding_job", None),
                            )
                            queued_embedding = (
                                cast(
                                    MemoryEmbeddingJob | None,
                                    await queue_embedding(
                                        job.issuer,
                                        job.subject,
                                        memory_id=record.id,
                                        revision_id=record.current_revision_id,
                                        generation_id=generation_id,
                                    ),
                                )
                                if callable(queue_embedding)
                                else None
                            )
                            if queued_embedding is None:
                                raise MemoryValidationError("embedding work was not durably queued")
                            claim_embedding = cast(
                                Callable[..., Awaitable[object]] | None,
                                getattr(self.repository, "claim_embedding_job_by_id", None),
                            )
                            if callable(claim_embedding):
                                claimed_embedding = await claim_embedding(
                                    queued_embedding.id, job.issuer, job.subject
                                )
                                if claimed_embedding is None:
                                    raise MemoryValidationError(
                                        "embedding work is already claimed or settled"
                                    )
                                embedding_job = cast(MemoryEmbeddingJob, claimed_embedding)
                            else:
                                raise MemoryValidationError(
                                    "embedding claim capability is unavailable"
                                )
                            if embedding_job.lease_id is None:
                                raise MemoryValidationError(
                                    "embedding work was not durably claimed"
                                )
                            if (
                                embedding_job.issuer != job.issuer
                                or embedding_job.subject != job.subject
                                or embedding_job.memory_id != record.id
                                or embedding_job.revision_id != record.current_revision_id
                                or embedding_job.generation_id != generation_id
                            ):
                                raise MemoryValidationError(
                                    "embedding work does not match accepted revision"
                                )
                            embedding = cast(
                                Any,
                                await self._embed(
                                    selected_generation.model_id,
                                    record.content,
                                    ProviderTraceContext(
                                        trace_id=trace_id,
                                        correlation_id=str(job.correlation_id)
                                        if job.correlation_id
                                        else None,
                                        causation_id=str(job.causation_id)
                                        if job.causation_id
                                        else None,
                                        job_id=str(job.id),
                                        run_id=str(job.run_id),
                                        conversation_id=str(job.conversation_id),
                                        generation_id=str(generation_id),
                                    ),
                                ),
                            )
                            if (
                                embedding.model_id != selected_generation.model_id
                                or embedding.model_revision != selected_generation.model_revision
                                or embedding.model_digest != selected_generation.model_digest
                                or embedding.dimension != selected_generation.dimension
                            ):
                                raise MemoryValidationError(
                                    "embedding provider identity or dimension mismatch"
                                )
                            await self.repository.attach_embedding(
                                job.issuer,
                                job.subject,
                                record.id,
                                revision_id=record.current_revision_id,
                                generation_id=generation_id,
                                vector=embedding.vector,
                                digest=embedding.digest,
                                model_id=embedding.model_id,
                                model_revision=embedding.model_revision,
                                model_digest=embedding.model_digest,
                                scope_type=record.scope.type,
                                agent_profile_id=record.scope.agent_profile_id,
                            )
                            settle_embedding = cast(
                                Callable[..., Awaitable[object]] | None,
                                getattr(self.repository, "settle_embedding_job", None),
                            )
                            if callable(settle_embedding):
                                await settle_embedding(
                                    embedding_job.id,
                                    issuer=job.issuer,
                                    subject=job.subject,
                                    lease_id=embedding_job.lease_id,
                                )
                except Exception:
                    # The accepted memory remains durable; the embedding job
                    # remains retryable for maintenance and is not a second
                    # extraction decision.
                    pass
            await settle_evidence_rejection()
            return prior

        try:
            config = await self.model_configuration(job.issuer, job.subject)
            if await self._selected_generation(job.issuer, job.subject, config) is None:
                await settle_parked("embedding_generation")
                return None
        except MemoryNotFound:
            # Configuration is owner state, not candidate evidence.  Park the
            # job with metadata-only retry state and avoid loading content or
            # invoking either provider until maintenance can retry it.
            await settle_parked("unconfigured")
            self._emit(
                "memory.error",
                job_started,
                trace_id=trace_id,
                outcome="error",
                error_class="queue",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.retry",
                job_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="queue",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                job_started,
                trace_id=trace_id,
                outcome="retryable",
                error_class="queue",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            return None

        if self._evidence_loader is None:
            await settle_parked("evidence_loader")
            return None
        try:
            evidence = await self._evidence_loader(job)
        except MemoryValidationError:
            await settle_evidence_rejection()
            self._emit(
                "memory.error",
                job_started,
                trace_id=trace_id,
                outcome="error",
                error_class="validation",
                attempt_count=job.attempt_count,
            )
            self._emit(
                "memory.job",
                job_started,
                trace_id=trace_id,
                outcome="error",
                error_class="validation",
                attempt_count=job.attempt_count,
                backlog=len(self.jobs),
            )
            return None
        if isinstance(evidence, MemoryTurnEvidence):
            try:
                evidence.validate_for(job)
            except MemoryValidationError:
                # Evidence mismatches are terminal for this attempt and must
                # never reach an inference provider.  Keep the outcome
                # content-free so callers can safely acknowledge the job.
                await settle_evidence_rejection()
                self._emit(
                    "memory.error",
                    job_started,
                    trace_id=trace_id,
                    outcome="error",
                    error_class="validation",
                    attempt_count=job.attempt_count,
                )
                self._emit(
                    "memory.job",
                    job_started,
                    trace_id=trace_id,
                    outcome="error",
                    error_class="validation",
                    attempt_count=job.attempt_count,
                    backlog=len(self.jobs),
                )
                return None
            user_content, assistant_content = evidence.user_content, evidence.assistant_content
            message_ids = frozenset(evidence.user_message_ids)
            # Provenance is reconstructed from the server-verified envelope,
            # never from provider output.  It is stable across retries and
            # contains identifiers/digests only (not turn text).
            provenance = tuple(
                MemoryProvenance(
                    uuid5(
                        MEMORY_ID_NAMESPACE,
                        f"provenance:{job.id}:{message_id}:{evidence.evidence_digest}",
                    ),
                    "conversation_message",
                    source_id=message_id,
                    conversation_id=job.conversation_id,
                    run_id=job.run_id,
                    message_id=message_id,
                    observed_at=self._now(),
                    evidence_digest=evidence.evidence_digest,
                )
                for message_id in sorted(evidence.user_message_ids, key=str)
            )
        else:
            # Keep a narrow compatibility seam for the in-process conversation
            # port.  Production loaders return MemoryTurnEvidence, while this
            # tuple form is accepted only when the job has no digest to verify.
            # When a digest is present, validate it before invoking inference.
            if not isinstance(evidence, (tuple, list)) or len(evidence) != 2:  # pyright: ignore[reportUnnecessaryIsInstance]
                raise MemoryValidationError("server-validated memory evidence is required")
            user_content, assistant_content = (str(evidence[0]), str(evidence[1]))
            if job.evidence_digest is not None:
                digest = hashlib.sha256(user_content.encode()).hexdigest()
                if digest != job.evidence_digest:
                    await settle_evidence_rejection()
                    self._emit(
                        "memory.error",
                        job_started,
                        trace_id=trace_id,
                        outcome="error",
                        error_class="validation",
                        attempt_count=job.attempt_count,
                    )
                    self._emit(
                        "memory.job",
                        job_started,
                        trace_id=trace_id,
                        outcome="error",
                        error_class="validation",
                        attempt_count=job.attempt_count,
                        backlog=len(self.jobs),
                    )
                    return None
            message_ids = frozenset(job.user_message_ids)
            provenance = tuple(
                MemoryProvenance(
                    uuid5(
                        MEMORY_ID_NAMESPACE,
                        f"provenance:{job.id}:{message_id}:{job.evidence_digest or ''}",
                    ),
                    "conversation_message",
                    source_id=message_id,
                    conversation_id=job.conversation_id,
                    run_id=job.run_id,
                    message_id=message_id,
                    observed_at=self._now(),
                    evidence_digest=job.evidence_digest,
                )
                for message_id in sorted(job.user_message_ids, key=str)
            )
        result = await self.process(
            job,
            user_content=user_content,
            assistant_content=assistant_content,
            user_message_ids=message_ids,
            run_agent_profile_id=job.agent_profile_id,
            provenance=provenance,
            allow_shared_user_promotion=job.allow_shared_user_promotion,
        )
        capability = lease_id or job.lease_id
        if capability is not None:
            settle = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "settle_processing_job", None),
            )
            if callable(settle):
                settled = await settle(
                    job.id,
                    capability,
                    issuer=job.issuer,
                    subject=job.subject,
                    retryable=result.state is CandidateState.RETRYABLE,
                    error_class="provider" if result.state is CandidateState.RETRYABLE else None,
                )
                if isinstance(settled, MemoryProcessingJob):
                    self.jobs[(settled.issuer, settled.subject, settled.run_id)] = settled
        return result

    async def process_command(self, command: MemoryProcessingCommand) -> MemoryProcessingSettlement:
        """Validate an identifier envelope and return durable settlement."""

        loader = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "get_processing_job", None),
        )
        # The command is identifier-only; production resolves the owner from
        # the authenticated run loader before using the owner-required SQL
        # lookup.  This prevents an ownerless repository read.
        if self._job_loader is not None:
            job = await self._job_loader(command.job_id)
        elif callable(loader):
            try:
                cached = next(
                    (item for item in self.jobs.values() if item.id == command.job_id), None
                )
                if cached is None:
                    raise MemoryNotFound("memory processing job not found")
                job = cast(
                    MemoryProcessingJob, await loader(command.job_id, cached.issuer, cached.subject)
                )
            except MemoryNotFound:
                raise
        else:
            cached = next((item for item in self.jobs.values() if item.id == command.job_id), None)
            if cached is None:
                raise MemoryNotFound("memory processing job not found")
            job = cached
        if (
            job.id != command.job_id
            or job.run_id != command.run_id
            or job.conversation_id != command.conversation_id
            or job.correlation_id != command.correlation_id
            or job.causation_id != command.causation_id
            or job.agent_revision_id != command.agent_revision_id
            or command.user_message_id not in job.user_message_ids
            or command.assistant_message_id not in job.assistant_message_ids
        ):
            raise MemoryValidationError("memory command does not match durable job")
        result = await self.process_job(command.job_id)
        settled_job = job
        if callable(loader) and job.issuer and job.subject:
            try:
                settled_job = cast(
                    MemoryProcessingJob, await loader(command.job_id, job.issuer, job.subject)
                )
            except MemoryNotFound:
                pass
        else:
            settled_job = next(
                (item for item in self.jobs.values() if item.id == command.job_id),
                job,
            )
        terminal = settled_job.status in {ProcessingJobStatus.COMPLETED, ProcessingJobStatus.FAILED}
        return MemoryProcessingSettlement(
            command.command_id,
            command.job_id,
            settled_job.status,
            terminal,
            result,
        )

    async def maintain(self, issuer: str, subject: str, *, now: datetime | None = None) -> int:
        maintenance_started = monotonic()
        trace_id = uuid5(MEMORY_ID_NAMESPACE, f"maintenance:{issuer}:{subject}").hex
        stamp = now or self._now()
        changed = 0
        records = await self.repository.list_memories(
            issuer,
            subject,
            MemoryFilters(
                scope_type=None, include_all_scopes=True, include_historical=True, limit=100000
            ),
        )
        for record in records:
            if record.pinned or record.status in {
                MemoryLifecycleStatus.DISABLED,
                MemoryLifecycleStatus.DISPUTED,
                MemoryLifecycleStatus.SUPERSEDED,
                MemoryLifecycleStatus.ARCHIVED,
            }:
                continue
            if (
                record.current_revision.valid_to is not None
                and record.current_revision.valid_to <= stamp
            ):
                if record.status is MemoryLifecycleStatus.ACTIVE:
                    transition_started = monotonic()
                    await self.repository.set_status(
                        issuer,
                        subject,
                        record.id,
                        status=MemoryLifecycleStatus.DORMANT,
                        expected_version=record.version,
                        scope_type=record.scope.type,
                        agent_profile_id=record.scope.agent_profile_id,
                    )
                    changed += 1
                    self._emit_maintenance_transition(
                        transition_started,
                        trace_id=trace_id,
                        memory_id=record.id,
                        destination_status=MemoryLifecycleStatus.DORMANT,
                    )
            elif (
                record.status is MemoryLifecycleStatus.ACTIVE
                and record.relevance(stamp) < DORMANT_THRESHOLD
            ):
                transition_started = monotonic()
                await self.repository.set_status(
                    issuer,
                    subject,
                    record.id,
                    status=MemoryLifecycleStatus.DORMANT,
                    expected_version=record.version,
                    scope_type=record.scope.type,
                    agent_profile_id=record.scope.agent_profile_id,
                )
                changed += 1
                self._emit_maintenance_transition(
                    transition_started,
                    trace_id=trace_id,
                    memory_id=record.id,
                    destination_status=MemoryLifecycleStatus.DORMANT,
                )
            elif (
                record.status is MemoryLifecycleStatus.DORMANT
                and record.dormant_at
                and (stamp - record.dormant_at).total_seconds() >= ARCHIVE_AFTER_DAYS * 86400
            ):
                transition_started = monotonic()
                await self.repository.set_status(
                    issuer,
                    subject,
                    record.id,
                    status=MemoryLifecycleStatus.ARCHIVED,
                    expected_version=record.version,
                    scope_type=record.scope.type,
                    agent_profile_id=record.scope.agent_profile_id,
                )
                changed += 1
                self._emit_maintenance_transition(
                    transition_started,
                    trace_id=trace_id,
                    memory_id=record.id,
                    destination_status=MemoryLifecycleStatus.ARCHIVED,
                )
        save_state = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "save_maintenance_state", None),
        )
        if callable(save_state):
            await save_state(issuer, subject, ran_at=stamp)
        # Transition telemetry is emitted once per dormant/archive record;
        # emit a separate sweep span even when no record changes.
        self._emit_maintenance_transition(maintenance_started, trace_id=trace_id, memory_id=None)
        return changed


class MemoryReindexService:
    """Resumable parallel-generation re-embedding coordinator."""

    def __init__(
        self,
        repository: MemoryRepository,
        embedder: Any,
        *,
        telemetry: MemoryTelemetry | None = None,
        trace_context_factory: Callable[..., AbstractContextManager[object]] | None = None,
    ) -> None:
        self.repository = repository
        self.embedder = embedder
        self._telemetry = telemetry
        # The domain receives a context-manager port so composition can attach
        # the platform's metadata-only MemoryTraceContext without importing
        # platform telemetry into the domain.
        self._trace_context_factory = trace_context_factory

    def _emit(
        self,
        operation: str,
        started: float,
        *,
        trace_id: str,
        outcome: str,
        generation_id: UUID | None = None,
        memory_id: UUID | None = None,
        revision_id: UUID | None = None,
        backlog: int | None = None,
        progress: float | None = None,
    ) -> None:
        if self._telemetry is None:
            return
        try:
            normalized_outcome = {"received": "ok", "rejected": "error", "parked": "retryable"}.get(
                outcome, outcome
            )
            self._telemetry(
                operation=operation,
                duration_ms=max(0.0, (monotonic() - started) * 1000),
                trace_id=trace_id,
                outcome=normalized_outcome,
                error_class=None if outcome == "ok" else "provider",
                generation_id=str(generation_id) if generation_id else None,
                memory_id=str(memory_id) if memory_id else None,
                memory_revision_id=str(revision_id) if revision_id else None,
                backlog=backlog,
                progress=progress,
            )
        except Exception:
            return

    async def resume(self, issuer: str, subject: str, generation_id: UUID) -> int:
        trace_id = uuid5(MEMORY_ID_NAMESPACE, f"reindex:{issuer}:{subject}:{generation_id}").hex
        generation = next(
            (
                item
                for item in getattr(self.repository, "embedding_generations", {}).values()
                if item.id == generation_id
            ),
            None,
        )
        if generation is None:
            load_generation = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(self.repository, "get_embedding_generation", None),
            )
            if callable(load_generation):
                generation = cast(
                    MemoryEmbeddingGeneration, await load_generation(issuer, subject, generation_id)
                )
        if generation is None:
            raise MemoryNotFound("embedding generation not found")
        if generation.status != "building":
            # A completed generation is resumable as a no-op; failed/retired
            # generations are not eligible for another activation attempt.
            if generation.status == "active":
                return 0
            raise MemoryValidationError("embedding generation is not resumable")
        load_configuration = cast(
            Callable[..., Awaitable[object]] | None,
            getattr(self.repository, "get_model_configuration", None),
        )
        if callable(load_configuration):
            try:
                configuration = cast(
                    MemoryModelConfiguration, await load_configuration(issuer, subject)
                )
            except MemoryNotFound:
                configuration = None
            if (
                configuration is not None
                and configuration.embedding_generation != generation_id
                and (
                    generation.model_id != configuration.embedding_model_id
                    or generation.model_revision != configuration.embedding_model_revision
                )
            ):
                raise MemoryValidationError("embedding generation does not match configured target")
        records = await self.repository.list_memories(
            issuer,
            subject,
            MemoryFilters(
                scope_type=None, include_all_scopes=True, include_historical=True, limit=100000
            ),
        )
        retained_revisions = [
            (record, revision) for record in records for revision in record.revisions
        ]
        processed = sum(
            1
            for record, revision in retained_revisions
            if any(
                item.revision_id == revision.id and item.generation_id == generation_id
                for item in record.embeddings
            )
        )
        pending_revisions = [
            (record, revision)
            for record, revision in retained_revisions
            if not any(
                item.revision_id == revision.id and item.generation_id == generation_id
                for item in record.embeddings
            )
        ]
        total_revisions = len(retained_revisions)
        try:
            for record, revision in pending_revisions:
                chunk_started = monotonic()
                claim_revision = cast(
                    Callable[..., Awaitable[object]] | None,
                    getattr(self.repository, "claim_embedding_job_for_revision", None),
                )
                if not callable(claim_revision):
                    raise MemoryValidationError("embedding claim capability is unavailable")
                claimed_job = cast(
                    MemoryEmbeddingJob | None,
                    await claim_revision(
                        issuer, subject, revision_id=revision.id, generation_id=generation_id
                    ),
                )
                if claimed_job is None:
                    queue_job = cast(
                        Callable[..., Awaitable[object]] | None,
                        getattr(self.repository, "queue_embedding_job", None),
                    )
                    if not callable(queue_job):
                        raise MemoryValidationError("embedding work is not durably queued")
                    await queue_job(
                        issuer,
                        subject,
                        memory_id=record.id,
                        revision_id=revision.id,
                        generation_id=generation_id,
                    )
                    claimed_job = cast(
                        MemoryEmbeddingJob | None,
                        await claim_revision(
                            issuer, subject, revision_id=revision.id, generation_id=generation_id
                        ),
                    )
                if claimed_job is None or claimed_job.lease_id is None:
                    # Another worker owns this revision or its retry is not
                    # available yet; never invoke a provider without a lease.
                    continue
                revision_trace_id = uuid5(
                    MEMORY_ID_NAMESPACE, f"reindex:{generation_id}:{record.id}:{revision.id}"
                ).hex
                trace_scope = (
                    self._trace_context_factory(
                        revision_trace_id,
                        str(claimed_job.id),
                        str(record.id),
                        str(revision.id),
                        str(generation_id),
                    )
                    if self._trace_context_factory is not None
                    else nullcontext()
                )
                with trace_scope:
                    embed = cast(Callable[..., Awaitable[object]], self.embedder.embed)
                    if classify_sensitivity(revision.content) is MemorySensitivity.CREDENTIAL:
                        settle_job = cast(
                            Callable[..., Awaitable[object]] | None,
                            getattr(self.repository, "settle_embedding_job", None),
                        )
                        if callable(settle_job):
                            await settle_job(
                                claimed_job.id,
                                issuer=issuer,
                                subject=subject,
                                lease_id=claimed_job.lease_id,
                                failed=True,
                                error_class="credential",
                            )
                        self._emit(
                            "memory.reindex.chunk",
                            chunk_started,
                            trace_id=revision_trace_id,
                            outcome="error",
                            generation_id=generation_id,
                            memory_id=record.id,
                            revision_id=revision.id,
                            backlog=max(0, total_revisions - processed),
                            progress=processed / max(1, total_revisions),
                        )
                        raise MemoryValidationError("credential memory revision cannot be embedded")
                    try:
                        context = ProviderTraceContext(
                            trace_id=revision_trace_id,
                            job_id=str(claimed_job.id),
                            generation_id=str(generation_id),
                        )
                        try:
                            result = cast(
                                Any,
                                await embed(generation.model_id, revision.content, context=context),
                            )
                        except TypeError as exc:
                            if "context" not in str(exc):
                                raise
                            result = cast(Any, await embed(generation.model_id, revision.content))
                        if (
                            result.model_id != generation.model_id
                            or result.model_revision != generation.model_revision
                            or result.model_digest != generation.model_digest
                            or result.dimension != generation.dimension
                        ):
                            raise MemoryValidationError(
                                "embedding provider identity or dimension mismatch"
                            )
                        await self.repository.attach_embedding(
                            issuer,
                            subject,
                            record.id,
                            revision_id=revision.id,
                            generation_id=generation_id,
                            vector=result.vector,
                            digest=result.digest,
                            model_id=result.model_id,
                            model_revision=result.model_revision,
                            model_digest=result.model_digest,
                            scope_type=record.scope.type,
                            agent_profile_id=record.scope.agent_profile_id,
                        )
                    except Exception:
                        settle_job = cast(
                            Callable[..., Awaitable[object]] | None,
                            getattr(self.repository, "settle_embedding_job", None),
                        )
                        if callable(settle_job):
                            await settle_job(
                                claimed_job.id,
                                issuer=issuer,
                                subject=subject,
                                lease_id=claimed_job.lease_id,
                                retryable=True,
                                error_class="provider",
                            )
                        self._emit(
                            "memory.reindex.chunk",
                            chunk_started,
                            trace_id=revision_trace_id,
                            outcome="error",
                            generation_id=generation_id,
                            memory_id=record.id,
                            revision_id=revision.id,
                            backlog=max(0, total_revisions - processed),
                            progress=processed / max(1, total_revisions),
                        )
                        raise
                    settle_job = cast(
                        Callable[..., Awaitable[object]] | None,
                        getattr(self.repository, "settle_embedding_job", None),
                    )
                    if callable(settle_job):
                        await settle_job(
                            claimed_job.id,
                            issuer=issuer,
                            subject=subject,
                            lease_id=claimed_job.lease_id,
                        )
                    processed += 1
                    save_state = cast(
                        Callable[..., Awaitable[object]] | None,
                        getattr(self.repository, "save_maintenance_state", None),
                    )
                    if callable(save_state):
                        await save_state(
                            issuer,
                            subject,
                            ran_at=datetime.now(UTC),
                            generation=generation.generation,
                            generation_id=generation_id,
                            cursor=record.id,
                            completed=processed,
                        )
                    self._emit(
                        "memory.reindex.chunk",
                        chunk_started,
                        trace_id=revision_trace_id,
                        outcome="ok",
                        generation_id=generation_id,
                        memory_id=record.id,
                        revision_id=revision.id,
                        backlog=max(0, total_revisions - processed),
                        progress=processed / max(1, total_revisions),
                    )
            switch_started = monotonic()
            try:
                await self.repository.activate_embedding_generation(issuer, subject, generation_id)
            except Exception:
                self._emit(
                    "memory.reindex.switch",
                    switch_started,
                    trace_id=trace_id,
                    outcome="error",
                    generation_id=generation_id,
                    backlog=max(0, total_revisions - processed),
                    progress=processed / max(1, total_revisions),
                )
                raise
            self._emit(
                "memory.reindex.switch",
                switch_started,
                trace_id=trace_id,
                outcome="ok",
                generation_id=generation_id,
                backlog=0,
            )
        except Exception:
            # A provider outage or worker interruption must leave a building
            # generation resumable.  It is not a terminal generation state;
            # the next maintenance cycle retries the unfinished revision.
            raise
        return processed


MemoryProcessor = MemoryProcessingService


# The in-memory adapter is split only to keep the worker orchestration above
# the adapter methods; the public class remains one deterministic store.
class MemoryStore(_MemoryStoreBase):
    async def list_embedding_generations(
        self, issuer: str, subject: str, *, status: str | None = None
    ) -> list[MemoryEmbeddingGeneration]:
        return sorted(
            [
                item
                for item in self.embedding_generations.values()
                if item.issuer == issuer
                and item.subject == subject
                and (status is None or item.status == status)
            ],
            key=lambda item: item.generation,
        )

    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        item = self.embedding_generations.get(generation_id)
        if item is None or item.issuer != issuer or item.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        return item

    async def list_memories(
        self, issuer: str, subject: str, filters: MemoryFilters | None = None
    ) -> list[MemoryRecord]:
        criteria = filters or MemoryFilters()
        results: list[MemoryRecord] = []
        for item in self.memories.values():
            if not _owner(item, issuer, subject):
                continue
            if criteria.kind and item.kind is not criteria.kind:
                continue
            if (
                criteria.scope_type
                and (
                    not criteria.include_all_scopes
                    or criteria.scope_type is MemoryScopeType.AGENT
                )
                and item.scope.type is not criteria.scope_type
            ):
                continue
            if (
                criteria.agent_profile_id
                and item.scope.agent_profile_id != criteria.agent_profile_id
            ):
                continue
            if (
                item.scope.type is MemoryScopeType.AGENT
                and not criteria.include_all_scopes
                and criteria.authorized_agent_ids
                and item.scope.agent_profile_id not in criteria.authorized_agent_ids
            ):
                continue
            if criteria.status and item.status is not criteria.status:
                continue
            if criteria.provenance_type and not any(
                source.source_type == criteria.provenance_type for source in item.provenance
            ):
                continue
            if (
                criteria.confidence_min is not None
                and item.current_revision.confidence < criteria.confidence_min
            ):
                continue
            if (
                criteria.confidence_max is not None
                and item.current_revision.confidence > criteria.confidence_max
            ):
                continue
            if criteria.created_from is not None and item.created_at < criteria.created_from:
                continue
            if criteria.created_to is not None and item.created_at >= criteria.created_to:
                continue
            if criteria.q and criteria.q.casefold() not in item.content.casefold():
                continue
            if not criteria.include_historical and item.status in {
                MemoryLifecycleStatus.DORMANT,
                MemoryLifecycleStatus.ARCHIVED,
                MemoryLifecycleStatus.DISABLED,
                MemoryLifecycleStatus.DISPUTED,
                MemoryLifecycleStatus.SUPERSEDED,
            }:
                continue
            results.append(item)
        results.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        if criteria.cursor_updated_at is not None and criteria.cursor_id is not None:
            results = [
                item
                for item in results
                if (item.updated_at, item.id)
                < (criteria.cursor_updated_at, criteria.cursor_id)
            ]
        return results[: max(1, min(criteria.limit, 100_000))]

    async def get_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        return self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )

    def _replay(
        self, issuer: str, subject: str, key: str | None, fingerprint: str
    ) -> object | None:
        if not key:
            return None
        prior = self._idempotency.get((issuer, subject, key))
        if (issuer, subject, key) in self._purge_tombstones:
            raise MemoryIdempotencyConflict("idempotency key is unavailable after purge")
        if prior is None:
            return None
        if prior[0] != fingerprint:
            raise MemoryIdempotencyConflict("idempotency key payload conflict")
        return prior[1]

    def _purge_tombstone(self, issuer: str, subject: str, key: object) -> None:
        if key and (issuer, subject, str(key)) in self._purge_tombstones:
            raise MemoryPurgeReplayNotFound("memory not found")

    def _record_replay(
        self, issuer: str, subject: str, key: str | None, fingerprint: str, value: object
    ) -> None:
        if key:
            self._idempotency[(issuer, subject, key)] = (fingerprint, value)

    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        kind = MemoryKind(str(kwargs["kind"]))
        scope = kwargs["scope"]
        if not isinstance(scope, MemoryScope):
            scope = MemoryScope(MemoryScopeType(str(scope)), kwargs.get("agent_profile_id"))  # type: ignore[arg-type]
        if scope.type is MemoryScopeType.AGENT:
            authorized = frozenset(kwargs.get("authorized_agent_ids", frozenset()))
            if authorized and scope.agent_profile_id not in authorized:
                raise MemoryScopeAuthorizationRequired("agent scope is not authorized")
        content = str(kwargs["content"])
        confidence = float(kwargs.get("confidence", 1.0))
        importance = float(kwargs.get("importance", 0.5))
        half_life = float(kwargs.get("half_life_days", 30.0))
        valid_from = kwargs.get("valid_from")
        valid_to = kwargs.get("valid_to")
        validate_revision(content, confidence, importance, half_life, valid_from, valid_to)  # type: ignore[arg-type]
        key = kwargs.get("idempotency_key")
        fingerprint = _fingerprint("create", _command_values(kwargs))
        prior = self._replay(issuer, subject, str(key) if key else None, fingerprint)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            await self._ensure_active_embedding_job(issuer, subject, prior)
            return prior
        now = self._now()
        memory_id = UUID(str(kwargs["memory_id"])) if kwargs.get("memory_id") else uuid4()
        self._assert_not_fenced(issuer, subject, memory_id)
        revision_id = uuid4()
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", [])))
        for item in provenance:
            validate_provenance(item)
        revision = MemoryRevision(
            revision_id,
            memory_id,
            1,
            kind,
            content,
            kwargs.get("observed_at") or now,
            now,
            confidence,
            importance,
            half_life,
            valid_from,
            valid_to,
            tuple(item.id for item in provenance),
        )  # type: ignore[arg-type]
        record = MemoryRecord(
            memory_id,
            issuer,
            subject,
            kind,
            scope,
            MemoryLifecycleStatus.ACTIVE,
            bool(kwargs.get("pinned", False)),
            1,
            revision_id,
            [revision],
            provenance,
            list(kwargs.get("embeddings", [])),
            tuple(kwargs.get("related_memory_ids", ())),
            [],
            now,
            None,
            None,
            now,
            now,
        )
        self.memories[memory_id] = record
        generation = await self.get_active_embedding_generation(issuer, subject)
        await self._stage_active_embedding_job(issuer, subject, record)
        if (
            not kwargs.get("_defer_embedding_queue")
            and self._embedding_queue_boundary is not None
            and generation is not None
        ):
            await self._embedding_queue_boundary(
                issuer,
                subject,
                memory_id=record.id,
                revision_id=record.current_revision_id,
                generation_id=generation.id,
            )
        self._record_replay(issuer, subject, str(key) if key else None, fingerprint, record)
        return record

    async def reinforce_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        key = kwargs.get("idempotency_key")
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", ())))
        fp = _fingerprint(
            "reinforce",
            {"memory_id": str(memory_id), "provenance": [str(item.id) for item in provenance]},
        )
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        for item in provenance:
            validate_provenance(item)
        existing_provenance = {item.id for item in record.provenance}
        record.provenance.extend(item for item in provenance if item.id not in existing_provenance)
        record.reinforced_at = self._now()
        record.status = MemoryLifecycleStatus.ACTIVE
        record.dormant_at = None
        record.updated_at = self._now()
        record.version += 1
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def revise_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        content = str(kwargs["content"])
        reason = kwargs.get("reason")
        if reason is not None:
            validate_memory_text(str(reason), "correction reason")
        current = record.current_revision
        confidence = float(
            current.confidence if kwargs.get("confidence") is None else kwargs["confidence"]
        )
        importance = float(
            current.importance if kwargs.get("importance") is None else kwargs["importance"]
        )
        half_life = float(
            current.half_life_days
            if kwargs.get("half_life_days") is None
            else kwargs["half_life_days"]
        )
        valid_from = (
            current.valid_from if kwargs.get("valid_from") is None else kwargs["valid_from"]
        )
        valid_to = current.valid_to if kwargs.get("valid_to") is None else kwargs["valid_to"]
        validate_revision(content, confidence, importance, half_life, valid_from, valid_to)  # type: ignore[arg-type]
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("revise", _command_values(kwargs) | {"memory_id": str(memory_id)})
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        now = self._now()
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", [])))
        for item in provenance:
            validate_provenance(item)
        kind = MemoryKind(str(kwargs["kind"])) if kwargs.get("kind") is not None else record.kind
        revision = MemoryRevision(
            uuid4(),
            memory_id,
            len(record.revisions) + 1,
            kind,
            content,
            kwargs.get("observed_at") or now,
            now,
            confidence,
            importance,
            half_life,
            valid_from,
            valid_to,
            tuple(item.id for item in provenance),
            str(reason) if reason is not None else None,
        )  # type: ignore[arg-type]
        record.revisions.append(revision)
        record.provenance.extend(provenance)
        record.current_revision_id = revision.id
        record.kind = kind
        record.version += 1
        record.reinforced_at = now
        record.updated_at = now
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def set_status(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        status = MemoryLifecycleStatus(str(kwargs["status"]))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint(
            "status",
            {
                "memory": str(memory_id),
                "status": status.value,
                "expected": expected,
                "related": kwargs.get("related_memory_id"),
                "scope_type": kwargs.get("scope_type"),
                "agent_profile_id": kwargs.get("agent_profile_id"),
            },
        )
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        related = kwargs.get("related_memory_id")
        relation = (
            "disputes"
            if status is MemoryLifecycleStatus.DISPUTED
            else "supersedes"
            if status is MemoryLifecycleStatus.SUPERSEDED
            else None
        )
        related_record: MemoryRecord | None = None
        if isinstance(related, UUID) and relation is not None:
            try:
                if related == memory_id:
                    raise MemoryNotFound("related memory not found")
                related_record = self.memories.get(related)
                if related_record is None or not _owner(related_record, issuer, subject):
                    raise MemoryNotFound("related memory not found")
                self._find(
                    issuer,
                    subject,
                    related,
                    scope_type=related_record.scope.type,
                    agent_profile_id=related_record.scope.agent_profile_id,
                    authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
                )
                if related_record.scope != record.scope:
                    raise MemoryNotFound("related memory not found")
            except MemoryNotFound as exc:
                raise MemoryNotFound("related memory not found") from exc
        now = self._now()
        record.status = status
        record.version += 1
        record.updated_at = now
        if status is MemoryLifecycleStatus.DORMANT:
            record.dormant_at = now
        if status is MemoryLifecycleStatus.ARCHIVED:
            record.archived_at = now
        if related_record is not None and relation is not None:
            record.relations.append(MemoryRelation(related, relation, now))
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def set_pinned(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint(
            "pin",
            {
                "memory": str(memory_id),
                "expected": expected,
                "pinned": bool(kwargs["pinned"]),
                "scope_type": kwargs.get("scope_type"),
                "agent_profile_id": kwargs.get("agent_profile_id"),
            },
        )
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        record.pinned = bool(kwargs["pinned"])
        record.version += 1
        record.updated_at = self._now()
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def purge(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryAuditRecord:
        if kwargs.get("confirmation") != PURGE_CONFIRMATION:
            raise MemoryPurgeConfirmationRequired("exact purge confirmation is required")
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint(
            "purge",
            {
                "memory": str(memory_id),
                "expected": expected,
                "confirmation": kwargs.get("confirmation"),
                "scope_type": kwargs.get("scope_type"),
                "agent_profile_id": kwargs.get("agent_profile_id"),
            },
        )
        self._purge_tombstone(issuer, subject, key)
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryAuditRecord)
            return prior
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        audit = MemoryAuditRecord(uuid4(), issuer, subject, memory_id, "purge", self._now())
        candidate_ids = {
            candidate.id
            for candidate in self.candidates.values()
            if candidate.issuer == issuer
            and candidate.subject == subject
            and (candidate.memory_id == memory_id or candidate.related_memory_id == memory_id)
        }
        job_ids = {
            job.id
            for job in self.processing_jobs.values()
            if job.issuer == issuer and job.subject == subject and job.memory_id == memory_id
        }
        # Derive deterministic action links before scrubbing the memory.  This
        # covers a crash after create/reinforce committed but before candidate
        # or job linkage was written.
        for (id_issuer, id_subject, idempotency_key), (_, value) in tuple(
            self._idempotency.items()
        ):
            if (id_issuer, id_subject) != (issuer, subject):
                continue
            if isinstance(value, MemoryRecord) and value.id == memory_id:
                if idempotency_key.startswith("memory-job:"):
                    try:
                        job_ids.add(UUID(idempotency_key.removeprefix("memory-job:")))
                    except ValueError:
                        pass
                elif idempotency_key.startswith("memory-action:"):
                    try:
                        candidate_id = UUID(idempotency_key.removeprefix("memory-action:"))
                    except ValueError:
                        continue
                    candidate_ids.add(candidate_id)
                    candidate = self.candidates.get(candidate_id)
                    if candidate is not None:
                        job_ids.add(candidate.job_id)
        job_ids.update(
            candidate.job_id
            for candidate in self.candidates.values()
            if candidate.id in candidate_ids
        )
        job_ids.update(
            UUID(str(outcome["job_id"]))
            for outcome in self.outcomes
            if outcome.get("issuer", issuer) == issuer
            and outcome.get("subject", subject) == subject
            and outcome.get("memory_id") == memory_id
            and outcome.get("job_id") is not None
        )
        self._purge_fences.add((issuer, subject, memory_id))
        for job_id, job in tuple(self.processing_jobs.items()):
            if job_id in job_ids:
                self.processing_jobs[job_id] = replace(
                    job,
                    status=ProcessingJobStatus.FAILED,
                    last_error_class="purged",
                    lease_id=None,
                    lease_until=None,
                    user_message_ids=(),
                    assistant_message_ids=(),
                    evidence_digest=None,
                    memory_id=None,
                )
        self.candidates = {
            candidate_id: candidate
            for candidate_id, candidate in self.candidates.items()
            if candidate_id not in candidate_ids and candidate.job_id not in job_ids
        }
        self.outcomes = [
            outcome
            for outcome in self.outcomes
            if outcome.get("memory_id") != memory_id and outcome.get("job_id") not in job_ids
        ]
        for embedding_id, embedding_job in tuple(self.embedding_jobs.items()):
            if (
                embedding_job.issuer == issuer
                and embedding_job.subject == subject
                and embedding_job.memory_id == memory_id
            ):
                del self.embedding_jobs[embedding_id]
        del self.memories[memory_id]
        stale_keys: list[tuple[str, str, str]] = []
        for key, value in self._idempotency.items():
            if key[:2] != (issuer, subject):
                continue
            if isinstance(value[1], MemoryRecord) and value[1].id == memory_id:
                stale_keys.append(key)
                continue
            if key[2].startswith("memory-job:"):
                try:
                    if UUID(key[2].removeprefix("memory-job:")) in job_ids:
                        stale_keys.append(key)
                except ValueError:
                    continue
        for stale_key in stale_keys:
            del self._idempotency[stale_key]
            self._purge_tombstones.add(stale_key)
        self.purge_audit.append(audit)
        self._record_replay(issuer, subject, str(key) if key else None, fp, audit)
        return audit

    async def link_processing_job_memory(
        self, job_id: UUID, issuer: str, subject: str, memory_id: UUID
    ) -> None:
        try:
            job = await self.get_processing_job(job_id, issuer, subject)
        except MemoryNotFound:
            # Direct in-memory application calls may process an already
            # materialized job without staging the worker row first. Durable
            # repositories always stage it and therefore enforce the link.
            return
        self._assert_not_fenced(issuer, subject, memory_id)
        self.processing_jobs[job_id] = replace(job, memory_id=memory_id)

    async def persist_candidate(
        self, candidate: MemoryCandidate, **kwargs: object
    ) -> MemoryCandidate:
        job = await self.get_processing_job(candidate.job_id, candidate.issuer, candidate.subject)
        del job
        expected_version = kwargs.get("expected_version")
        existing = self.candidates.get(candidate.id)
        if existing is not None and expected_version is not None:
            if existing.version != int(expected_version):
                raise MemoryVersionConflict("memory candidate version conflict")
        self.candidates[candidate.id] = candidate
        idempotency_key = kwargs.get("idempotency_key")
        fingerprint = kwargs.get("fingerprint")
        if idempotency_key and isinstance(fingerprint, str):
            self._record_replay(
                candidate.issuer,
                candidate.subject,
                f"candidate:{idempotency_key}",
                fingerprint,
                candidate,
            )
        return candidate

    async def record_action_outcome(self, **kwargs: object) -> None:
        job_id = kwargs.get("job_id")
        issuer = str(kwargs.get("issuer", ""))
        subject = str(kwargs.get("subject", ""))
        if not isinstance(job_id, UUID):
            raise MemoryValidationError("action outcome requires a job")
        await self.get_processing_job(job_id, issuer, subject)
        self.outcomes.append(dict(kwargs))

    async def register_embedding_generation(
        self, issuer: str, subject: str, **kwargs: object
    ) -> MemoryEmbeddingGeneration:
        now = self._now()
        generation = int(kwargs["generation"])
        model_id = str(kwargs["model_id"])
        dimension = int(kwargs["dimension"])
        if dimension < 1 or not model_id:
            raise MemoryValidationError("embedding model and dimension are required")
        digest = kwargs.get("model_digest")
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise MemoryValidationError("embedding model digest must be sha256")
        if any(
            item.generation == generation and item.issuer == issuer and item.subject == subject
            for item in self.embedding_generations.values()
        ):
            raise MemoryValidationError("embedding generation number already exists")
        item = MemoryEmbeddingGeneration(
            uuid4(),
            generation,
            model_id,
            kwargs.get("model_revision"),
            dimension,
            "building",
            now,
            None,
            str(digest) if digest else None,
            issuer,
            subject,
        )
        self.embedding_generations[item.id] = item
        return item

    async def activate_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        try:
            item = self.embedding_generations[generation_id]
        except KeyError as exc:
            raise MemoryNotFound("embedding generation not found") from exc
        if item.issuer and item.issuer != issuer or item.subject and item.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        if item.status != "building":
            raise MemoryValidationError("only a building embedding generation can be activated")
        retained = [
            revision.id
            for record in self.memories.values()
            if _owner(record, issuer, subject)
            for revision in record.revisions
        ]
        embedded = {
            embedding.revision_id
            for record in self.memories.values()
            if _owner(record, issuer, subject)
            for embedding in record.embeddings
            if embedding.generation_id == generation_id
        }
        if set(retained) - embedded:
            raise MemoryValidationError("embedding generation is incomplete")
        configuration = self.model_configurations.get((issuer, subject))
        if (
            configuration is not None
            and configuration.embedding_generation != generation_id
            and (
                item.model_id != configuration.embedding_model_id
                or item.model_revision != configuration.embedding_model_revision
            )
        ):
            raise MemoryValidationError("embedding generation does not match configured target")
        if configuration is not None:
            previous_id = configuration.embedding_generation
            if isinstance(previous_id, UUID) and previous_id != generation_id:
                previous = self.embedding_generations.get(previous_id)
                if (
                    previous is not None
                    and previous.issuer == issuer
                    and previous.subject == subject
                ):
                    previous.status = "retired"
            self.model_configurations[(issuer, subject)] = replace(
                configuration,
                embedding_generation=generation_id,
                version=configuration.version + 1,
            )
        item.status = "active"
        item.activated_at = self._now()
        activated = item
        self.embedding_generations[generation_id] = item
        for record in self.memories.values():
            record.embedding_generations = [
                activated if generation.id == generation_id else generation
                for generation in record.embedding_generations
            ]
        return activated

    async def mark_embedding_generation_failed(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        item = self.embedding_generations.get(generation_id)
        if item is None or item.issuer != issuer or item.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        if item.status != "building":
            raise MemoryValidationError("only a building embedding generation can fail")
        item.status = "failed"
        return item

    async def attach_embedding(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        self._assert_not_fenced(issuer, subject, memory_id)
        record = self._find(
            issuer,
            subject,
            memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        revision_id = kwargs.get("revision_id", record.current_revision_id)
        if revision_id not in {item.id for item in record.revisions}:
            raise MemoryNotFound("memory revision not found")
        generation_id = kwargs["generation_id"]
        generation = self.embedding_generations.get(generation_id)
        if generation is None:
            raise MemoryNotFound("embedding generation not found")
        if generation.issuer != issuer or generation.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        vector = tuple(float(value) for value in cast(Iterable[object], kwargs["vector"]))
        if len(vector) != generation.dimension or not all(math.isfinite(value) for value in vector):
            raise MemoryValidationError("embedding dimension or values do not match generation")
        digest = str(kwargs.get("digest", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MemoryValidationError("embedding digest must be sha256")
        if not {"model_id", "model_revision", "model_digest"} <= kwargs.keys():
            raise MemoryValidationError("observed embedding identity is required")
        model_id = str(kwargs["model_id"])
        model_revision = kwargs["model_revision"]
        model_digest = kwargs["model_digest"]
        if (
            model_id != generation.model_id
            or model_revision != generation.model_revision
            or model_digest != generation.model_digest
        ):
            raise MemoryValidationError("embedding provider identity does not match generation")
        if any(
            item.revision_id == revision_id and item.generation_id == generation.id
            for item in record.embeddings
        ):
            raise MemoryValidationError("embedding already exists for revision and generation")
        record.embeddings.append(
            MemoryEmbedding(
                uuid4(),
                revision_id,
                generation.generation,
                model_id,
                model_revision,
                generation.dimension,
                digest,
                self._now(),
                vector,
                generation.id,
                model_digest,
            )
        )
        if all(item.id != generation.id for item in record.embedding_generations):
            record.embedding_generations.append(generation)
        return record

    async def save_model_configuration(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration, **kwargs: object
    ) -> MemoryModelConfiguration:
        if configuration.issuer != issuer or configuration.subject != subject:
            raise MemoryScopeAuthorizationRequired("model configuration owner mismatch")
        expected = kwargs.get("expected_version")
        prior = self.model_configurations.get((issuer, subject))
        key = kwargs.get("idempotency_key")
        fingerprint = _fingerprint(
            "model.configuration",
            {
                "extraction": configuration.extraction_model_id,
                "embedding": configuration.embedding_model_id,
                "expectedVersion": expected,
                "dimension": kwargs.get("dimension"),
                "modelDigest": kwargs.get("model_digest"),
            },
        )
        command_key = f"model:{key}" if key else None
        replay = self._replay(issuer, subject, command_key, fingerprint)
        if replay is not None:
            if not isinstance(replay, MemoryModelConfiguration):
                raise MemoryIdempotencyConflict("idempotency key is unavailable")
            return replay
        if prior is None and expected is not None and int(expected) != 1:
            raise MemoryVersionConflict("model configuration version conflict")
        if expected is not None and prior is not None and prior.version != int(expected):
            raise MemoryVersionConflict("model configuration version conflict")
        selected = configuration.embedding_generation
        if selected is not None:
            generation = self.embedding_generations.get(selected)
            if generation is None or generation.issuer != issuer or generation.subject != subject:
                raise MemoryNotFound("embedding generation not found")
        if prior is not None and (
            prior.embedding_model_id != configuration.embedding_model_id
            or prior.embedding_model_revision != configuration.embedding_model_revision
        ):
            # A newer model target supersedes unfinished building generations;
            # retain them as audit metadata but never let them starve the
            # current resumable cutover.
            for existing_generation in self.embedding_generations.values():
                if (
                    existing_generation.issuer == issuer
                    and existing_generation.subject == subject
                    and existing_generation.status == "building"
                ):
                    existing_generation.status = "retired"
            previous = (
                self.embedding_generations.get(prior.embedding_generation)
                if prior.embedding_generation
                else None
            )
            dimension = int(kwargs.get("dimension", previous.dimension if previous else 0))
            if dimension > 0:
                next_number = (
                    max(
                        (
                            item.generation
                            for item in self.embedding_generations.values()
                            if item.issuer == issuer and item.subject == subject
                        ),
                        default=0,
                    )
                    + 1
                )
                generation_id = uuid4()
                self.embedding_generations[generation_id] = MemoryEmbeddingGeneration(
                    generation_id,
                    next_number,
                    configuration.embedding_model_id,
                    configuration.embedding_model_revision,
                    dimension,
                    "building",
                    self._now(),
                    None,
                    str(kwargs["model_digest"]) if kwargs.get("model_digest") else None,
                    issuer,
                    subject,
                )
                selected = prior.embedding_generation
        saved = replace(configuration, embedding_generation=selected)
        self.model_configurations[(issuer, subject)] = saved
        self._record_replay(issuer, subject, command_key, fingerprint, saved)
        return saved

    async def get_model_configuration(self, issuer: str, subject: str) -> MemoryModelConfiguration:
        try:
            configuration = self.model_configurations[(issuer, subject)]
        except KeyError as exc:
            raise MemoryNotFound("memory model configuration not found") from exc
        if configuration.embedding_generation is None:
            matches = [
                item
                for item in self.embedding_generations.values()
                if item.issuer == issuer
                and item.subject == subject
                and item.status == "active"
                and item.model_id == configuration.embedding_model_id
                and item.model_revision == configuration.embedding_model_revision
            ]
            if len(matches) == 1:
                configuration = replace(
                    configuration,
                    embedding_generation=matches[0].id,
                    version=configuration.version + 1,
                )
                self.model_configurations[(issuer, subject)] = configuration
        return configuration

    async def queue_embedding_job(
        self, issuer: str, subject: str, *, memory_id: UUID, revision_id: UUID, generation_id: UUID
    ) -> MemoryEmbeddingJob:
        self._assert_not_fenced(issuer, subject, memory_id)
        record = self.memories.get(memory_id)
        if record is None or not _owner(record, issuer, subject):
            raise MemoryNotFound("memory not found")
        if revision_id not in {item.id for item in record.revisions}:
            raise MemoryNotFound("memory revision not found")
        generation = self.embedding_generations.get(generation_id)
        if generation is None or generation.issuer != issuer or generation.subject != subject:
            raise MemoryNotFound("embedding generation not found")
        job_id = uuid5(
            MEMORY_ID_NAMESPACE, f"embedding-job:{issuer}:{subject}:{revision_id}:{generation_id}"
        )
        existing = self.embedding_jobs.get(job_id)
        if existing is not None:
            # Queue/replay is not a lease capability; the current lease, if
            # any, belongs to the worker that claimed the durable row.
            return replace(existing, lease_id=None, lease_until=None)
        item = MemoryEmbeddingJob(
            job_id, issuer, subject, memory_id, revision_id, generation_id, available_at=self._now()
        )
        self.embedding_jobs[job_id] = item
        return item

    async def enqueue_processing_job(self, job: MemoryProcessingJob) -> MemoryProcessingJob:
        if job.memory_id is not None:
            self._assert_not_fenced(job.issuer, job.subject, job.memory_id)
        existing = self.processing_jobs.get(job.id)
        if existing is not None:
            if (existing.issuer, existing.subject) != (job.issuer, job.subject):
                raise MemoryNotFound("memory processing job not found")
            return existing
        self.processing_jobs[job.id] = job
        return job

    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob:
        job = self.processing_jobs.get(job_id)
        if job is None or (job.issuer, job.subject) != (issuer, subject):
            raise MemoryNotFound("memory processing job not found")
        return job

    async def claim_processing_job_by_id(
        self, job_id: UUID, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryProcessingJob | None:
        job = await self.get_processing_job(job_id, issuer, subject)
        now = self._now()
        if job.available_at > now:
            return None
        if job.status not in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE} and not (
            job.status is ProcessingJobStatus.RUNNING
            and job.lease_until is not None
            and job.lease_until <= now
        ):
            return None
        claimed = replace(
            job,
            status=ProcessingJobStatus.RUNNING,
            attempt_count=job.attempt_count + 1,
            lease_id=uuid4(),
            lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
        )
        self.processing_jobs[job_id] = claimed
        return claimed

    async def settle_processing_job(
        self,
        job_id: UUID,
        lease_id: UUID,
        *,
        issuer: str,
        subject: str,
        retryable: bool = False,
        error_class: str | None = None,
    ) -> MemoryProcessingJob:
        job = await self.get_processing_job(job_id, issuer, subject)
        if job.status is not ProcessingJobStatus.RUNNING or job.lease_id != lease_id:
            raise MemoryValidationError("processing job lease is stale")
        settled = replace(
            job,
            status=ProcessingJobStatus.RETRYABLE if retryable else ProcessingJobStatus.COMPLETED,
            last_error_class=error_class,
            lease_id=None,
            lease_until=None,
        )
        if retryable:
            settled = replace(
                settled,
                available_at=self._now()
                + timedelta(seconds=min(3600, 2 ** min(job.attempt_count, 10))),
            )
        self.processing_jobs[job_id] = settled
        return settled

    async def claim_embedding_job(
        self, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryEmbeddingJob | None:
        now = self._now()
        for item in sorted(self.embedding_jobs.values(), key=lambda value: value.available_at):
            if item.issuer != issuer or item.subject != subject or item.available_at > now:
                continue
            generation = self.embedding_generations.get(item.generation_id)
            if generation is None or generation.status != "active":
                continue
            if item.status not in {
                ProcessingJobStatus.QUEUED,
                ProcessingJobStatus.RETRYABLE,
            } and not (
                item.status is ProcessingJobStatus.RUNNING
                and item.lease_until
                and item.lease_until <= now
            ):
                continue
            claimed = replace(
                item,
                status=ProcessingJobStatus.RUNNING,
                attempt_count=item.attempt_count + 1,
                lease_id=uuid4(),
                lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
            )
            self.embedding_jobs[item.id] = claimed
            return claimed
        return None

    async def settle_embedding_job(
        self,
        job_id: UUID,
        *,
        issuer: str,
        subject: str,
        lease_id: UUID,
        retryable: bool = False,
        failed: bool = False,
        error_class: str | None = None,
    ) -> MemoryEmbeddingJob:
        item = self.embedding_jobs.get(job_id)
        if item is None or (issuer, subject) != (item.issuer, item.subject):
            raise MemoryNotFound("embedding job not found")
        if item.status is not ProcessingJobStatus.RUNNING or item.lease_id != lease_id:
            raise MemoryValidationError("embedding job lease is stale")
        provider_retry = failed and error_class == "provider"
        settled = replace(
            item,
            status=ProcessingJobStatus.FAILED
            if failed and not provider_retry
            else (
                ProcessingJobStatus.RETRYABLE
                if retryable or provider_retry
                else ProcessingJobStatus.COMPLETED
            ),
            last_error_class=error_class,
            lease_id=None,
            lease_until=None,
        )
        if retryable or provider_retry:
            settled = replace(
                settled,
                available_at=self._now()
                + timedelta(seconds=min(3600, 2 ** min(item.attempt_count, 10))),
            )
        self.embedding_jobs[job_id] = settled
        return settled

    async def claim_embedding_job_by_id(
        self, job_id: UUID, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryEmbeddingJob | None:
        item = self.embedding_jobs.get(job_id)
        now = self._now()
        if (
            item is None
            or (item.issuer, item.subject) != (issuer, subject)
            or item.available_at > now
        ):
            return None
        if item.status not in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE} and not (
            item.status is ProcessingJobStatus.RUNNING
            and item.lease_until is not None
            and item.lease_until <= now
        ):
            return None
        self._assert_not_fenced(issuer, subject, item.memory_id)
        claimed = replace(
            item,
            status=ProcessingJobStatus.RUNNING,
            attempt_count=item.attempt_count + 1,
            lease_id=uuid4(),
            lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
        )
        self.embedding_jobs[job_id] = claimed
        return claimed

    async def claim_embedding_job_for_revision(
        self,
        issuer: str,
        subject: str,
        *,
        revision_id: UUID,
        generation_id: UUID,
        lease_seconds: float = 60.0,
    ) -> MemoryEmbeddingJob | None:
        now = self._now()
        for item in sorted(self.embedding_jobs.values(), key=lambda value: value.available_at):
            if (
                item.issuer != issuer
                or item.subject != subject
                or item.revision_id != revision_id
                or item.generation_id != generation_id
                or item.available_at > now
            ):
                continue
            if item.status not in {
                ProcessingJobStatus.QUEUED,
                ProcessingJobStatus.RETRYABLE,
            } and not (
                item.status is ProcessingJobStatus.RUNNING
                and item.lease_until is not None
                and item.lease_until <= now
            ):
                continue
            self._assert_not_fenced(issuer, subject, item.memory_id)
            claimed = replace(
                item,
                status=ProcessingJobStatus.RUNNING,
                attempt_count=item.attempt_count + 1,
                lease_id=uuid4(),
                lease_until=now + timedelta(seconds=max(1.0, lease_seconds)),
            )
            self.embedding_jobs[item.id] = claimed
            return claimed
        return None


MemoryCatalog = MemoryStore
MemoryService = MemoryStore


__all__ = [
    "ARCHIVE_AFTER_DAYS",
    "DORMANT_THRESHOLD",
    "MAX_HALF_LIFE_DAYS",
    "MIN_HALF_LIFE_DAYS",
    "PURGE_CONFIRMATION",
    "CandidateDecision",
    "CandidateState",
    "MemoryAction",
    "MemoryAuditRecord",
    "MemoryCandidate",
    "MemoryCatalog",
    "MemoryEmbedding",
    "MemoryError",
    "MemoryEmbeddingGeneration",
    "MemoryFilters",
    "MemoryIdempotencyConflict",
    "MemoryKind",
    "MemoryLifecycleStatus",
    "MemoryNotFound",
    "MemoryProvenance",
    "MemoryPurgeConfirmationRequired",
    "MemoryPurgeReplayNotFound",
    "MemoryRecord",
    "MemoryRelation",
    "MemoryScopeAuthorizationRequired",
    "MemoryRepository",
    "MemoryRevision",
    "MemoryScope",
    "MemoryScopeType",
    "MemoryCollectionScopeType",
    "MemoryService",
    "MEMORY_ACTION_SCHEMA",
    "MEMORY_EXTRACTION_POLICY_VERSION",
    "MEMORY_ID_NAMESPACE",
    "MEMORY_PROCESSING_SCHEMA_VERSION",
    "MEMORY_PROCESSING_TOPIC",
    "SENSITIVITY_POLICY_VERSION",
    "MemoryEmbeddingJob",
    "MemoryModelConfiguration",
    "MemoryModelApplicationService",
    "MemoryModelConfigurationSnapshot",
    "MemoryModelDescriptor",
    "MemoryReindexSnapshot",
    "MemoryProcessingCommand",
    "MemoryProcessingJob",
    "MemoryProcessingService",
    "MemoryProcessingSettlement",
    "MemoryProcessor",
    "MemoryReindexService",
    "MemoryRetentionBasis",
    "MemorySensitivity",
    "MemoryStore",
    "MemoryTurnEvidence",
    "MemoryValidationError",
    "MemoryVersionConflict",
    "ProcessingJobStatus",
    "classify_retention_basis",
    "normalize_retention_horizon",
    "classify_sensitivity",
    "contains_secret",
    "decide_candidate",
    "validate_memory_text",
    "validate_provenance",
    "validate_revision",
]
