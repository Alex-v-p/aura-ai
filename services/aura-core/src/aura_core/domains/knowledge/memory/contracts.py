"""Provider-neutral durable memory commands, DTOs, and persistence ports.

This module is deliberately self-contained: extraction, retrieval, and provider
adapters can depend on these contracts without importing a storage adapter.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import cast
from uuid import UUID, uuid5

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
            candidate.importance if candidate.importance is not None else DEFAULT_MEMORY_IMPORTANCE
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
_DETERMINISTIC_PREFERENCE_STATEMENT = re.compile(
    r"^\s*(?:[^?!.]{1,240}\b(?:is|are)\s+my\s+(?:favorite|preferred)\b[^?!.]{1,240}"
    r"|i\s+(?:prefer|like|love)\b[^?!.]{1,240})[.!]?\s*$",
    re.IGNORECASE,
)
_FALLBACK_REINFORCEMENT_MARKER = re.compile(
    r"^provider_ignore_reinforcement@(?P<version>[1-9][0-9]*)$"
)
_FALLBACK_REINFORCEMENT_TARGET_MARKER = re.compile(
    r"^pif@(?P<memory_id>[0-9a-fA-F]{32})@(?P<version>[1-9][0-9]*)$"
)
_TEMPORAL_FALLBACK_QUALIFIER = re.compile(
    r"\b(?:today|tomorrow|tonight|this\s+(?:time|week|month|year)|"
    r"next\s+(?:week|month|year)|for\s+now|right\s+now|at\s+the\s+moment|"
    r"temporarily|until|while|currently|if|when|unless|during|"
    r"for\s+(?:a|an|one|\d+)\s+(?:day|week|month|year)s?)\b",
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


def deterministic_fallback_kind(
    user_content: str, retention_basis: MemoryRetentionBasis
) -> MemoryKind | None:
    """Return a conservative kind for provider-ignore recovery, if any.

    This is deliberately narrower than the general personal classifier.  The
    fallback may only derive canonical content from an exact owner-authored
    preference statement or an explicit remember/save command.
    """

    if _TEMPORAL_FALLBACK_QUALIFIER.search(user_content):
        return None
    if (
        retention_basis is MemoryRetentionBasis.PERSONAL
        and _DETERMINISTIC_PREFERENCE_STATEMENT.search(user_content)
    ):
        return MemoryKind.PREFERENCE
    if retention_basis is MemoryRetentionBasis.EXPLICIT_REQUEST and _EXPLICIT_MEMORY_REQUEST.search(
        user_content
    ):
        return MemoryKind.SEMANTIC
    return None


def _deterministic_create_idempotency_key(issuer: str, subject: str, content: str) -> str:
    """Return a content-free receipt shared by concurrent fallback creates."""

    digest = hashlib.sha256(f"{issuer}\x00{subject}\x00{content}".encode()).hexdigest()
    return f"memory-fallback:{digest}"


def normalize_retention_horizon(
    candidate: MemoryCandidate, *, user_content: str
) -> MemoryCandidate:
    """Apply bounded deterministic horizons for durable family events."""

    content = candidate.content or ""
    candidate_relation_event = _CANDIDATE_FAMILY_EVENT.search(content)
    candidate_event = (
        None if candidate_relation_event is not None else _DATED_PERSONAL_EVENT.search(content)
    )
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
    if candidate.action is MemoryAction.IGNORE or not candidate.content or not stable_family_event:
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
        r"(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|secret|password|"
        r"private[_ -]?key|credential)\s*[:=]",
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
        r"\b(?:otp|one[- ]time password|pin|passcode|recovery code|seed phrase|"
        r"mnemonic)\s*[:=]?\s*[A-Za-z0-9 -]{4,}",
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
        r"\b(?:password|passphrase|api[_ -]?key|access[_ -]?token|client[_ -]?secret|"
        r"private[_ -]?key|credential|secret)\s*(?:is|:|=)\s*[^\s,.;]{4,}",
        re.I,
    ),
    re.compile(
        r"\b(?:one[- ]time|verification|security|authentication)\s+(?:code|passcode|"
        r"password|pin|otp)\s*(?:is|:|=)?\s*[A-Za-z0-9]{4,}",
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
    provenance: list[MemoryProvenance] = field(default_factory=lambda: list[MemoryProvenance]())
    embeddings: list[MemoryEmbedding] = field(default_factory=lambda: list[MemoryEmbedding]())
    related_memory_ids: tuple[UUID, ...] = ()
    relations: list[MemoryRelation] = field(default_factory=lambda: list[MemoryRelation]())
    reinforced_at: datetime | None = None
    dormant_at: datetime | None = None
    archived_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    embedding_generations: list[MemoryEmbeddingGeneration] = field(
        default_factory=lambda: list[MemoryEmbeddingGeneration]()
    )

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
    authorized_agent_ids: frozenset[UUID] | None = None
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
        raw_payload = json.loads(base64.urlsafe_b64decode(padded.encode()))
        if not isinstance(raw_payload, dict):
            raise ValueError
        payload = cast(dict[str, object], raw_payload)
        if payload.get("v") != 1:
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
            r"\b(diabetes|diagnos(?:is|ed)|cancer|oncolog(?:y|ist)|medication|"
            r"prescription|severe depression|major depression|depression|suicid(?:al|e)|"
            r"病|health|medical)\b",
            re.I,
        ),
    ),
    (
        MemorySensitivity.FINANCE,
        re.compile(
            r"\b(bank|iban|credit[ -]?card|salary|income|tax|finance|routing number|"
            r"account number|mortgage|balance|financial|loan)\b",
            re.I,
        ),
    ),
    (
        MemorySensitivity.IDENTITY,
        re.compile(
            r"\b(passport|social[ -]?security|ssn|national id(?:entification)?|"
            r"identity number|driver(?:'s)? license|date of birth)\b|"
            r"\b\d{3}-\d{2}-\d{4}\b",
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
            r"\b(latitude|longitude|gps|home address|street address|precise location|"
            r"coordinates|geolocation)\b|\b\d{1,5}\s+[A-Za-z0-9.'-]+\s+"
            r"(?:street|st|road|rd|avenue|ave|lane|ln|boulevard|blvd)\b",
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
    authorized_agent_ids: frozenset[UUID] | None,
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
        and (authorized_agent_ids is None or record.scope.agent_profile_id in authorized_agent_ids)
    )


# Internal implementation modules consume these helpers through named,
# non-private aliases. The underscored definitions remain the compatibility
# names used by existing domain code while the aliases make the module seam
# explicit to static analysis.
command_values = _command_values
content_free_candidate = _content_free_candidate
deterministic_create_idempotency_key = _deterministic_create_idempotency_key
fingerprint = _fingerprint
normalize_candidate_for_approval = _normalize_candidate_for_approval
owner = _owner
scope_authorized = _scope_authorized
fallback_reinforcement_marker = _FALLBACK_REINFORCEMENT_MARKER
fallback_reinforcement_target_marker = _FALLBACK_REINFORCEMENT_TARGET_MARKER
user_family_relation_event = _USER_FAMILY_RELATION_EVENT
reindex_pending = _REINDEX_PENDING


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
    "MemoryRevision",
    "MemoryScope",
    "MemoryScopeType",
    "MemoryCollectionScopeType",
    "MEMORY_ACTION_SCHEMA",
    "MEMORY_EXTRACTION_POLICY_VERSION",
    "MEMORY_ID_NAMESPACE",
    "MEMORY_PROCESSING_SCHEMA_VERSION",
    "MEMORY_PROCESSING_TOPIC",
    "MEMORY_PROVENANCE_TYPES",
    "MEMORY_RELATION_TYPES",
    "SENSITIVITY_POLICY_VERSION",
    "MemoryEmbeddingJob",
    "MemoryModelConfiguration",
    "MemoryModelConfigurationSnapshot",
    "MemoryModelDescriptor",
    "MemoryReindexSnapshot",
    "MemoryProcessingCommand",
    "MemoryProcessingJob",
    "MemoryProcessingSettlement",
    "MemoryRetentionBasis",
    "MemorySensitivity",
    "MemoryTurnEvidence",
    "MemoryValidationError",
    "MemoryVersionConflict",
    "ProcessingJobStatus",
    "classify_retention_basis",
    "normalize_retention_horizon",
    "classify_sensitivity",
    "contains_secret",
    "deterministic_fallback_kind",
    "decide_candidate",
    "validate_memory_text",
    "validate_provenance",
    "validate_revision",
]
