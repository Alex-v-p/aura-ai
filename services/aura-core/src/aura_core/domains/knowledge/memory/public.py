"""Provider-neutral durable memory commands, DTOs, and persistence ports.

This module is deliberately self-contained: extraction, retrieval, and provider
adapters can depend on these contracts without importing a storage adapter.
"""

# Dynamic command kwargs are intentionally normalized at this public seam.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false

# Domain constructor signatures are kept compact and stable for application ports.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol, cast
from uuid import UUID, uuid4

MIN_HALF_LIFE_DAYS = 0.25
MAX_HALF_LIFE_DAYS = 3650.0
DORMANT_THRESHOLD = 0.10
ARCHIVE_AFTER_DAYS = 30
PURGE_CONFIRMATION = "PURGE MEMORY"
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


class MemoryLifecycleStatus(StrEnum):
    ACTIVE = "active"
    DORMANT = "dormant"
    ARCHIVED = "archived"
    DISABLED = "disabled"
    DISPUTED = "disputed"
    SUPERSEDED = "superseded"


# Short, conservative credential patterns are applied before a memory reaches
# either adapter.  This is intentionally a deny rule, not a secret detector.
_SECRET_PATTERNS = (
    re.compile(r"(?:api[_ -]?key|access[_ -]?token|client[_ -]?secret|secret|password|private[_ -]?key|credential)\s*[:=]", re.I),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----", re.I),
    re.compile(r"\b(?:sk|pk)_[A-Za-z0-9]{16,}\b", re.I),
    re.compile(r"\b(?:gh[pousr]|github_pat)_[A-Za-z0-9_]{20,}\b", re.I),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{12,}", re.I),
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/@:]+:[^\s/@]+@", re.I),
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
        if self.evidence_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.evidence_digest):
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
    digest: str
    created_at: datetime
    vector: tuple[float, ...] | None = None
    generation_id: UUID | None = None


@dataclass(frozen=True, slots=True)
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
class MemoryFilters:
    kind: MemoryKind | None = None
    scope_type: MemoryScopeType | None = MemoryScopeType.USER
    agent_profile_id: UUID | None = None
    status: MemoryLifecycleStatus | None = None
    q: str | None = None
    include_historical: bool = False
    limit: int = 50
    authorized_agent_ids: frozenset[UUID] = frozenset()

    def __post_init__(self) -> None:
        if self.scope_type is None:
            raise MemoryValidationError("memory read scope is required")
        if self.scope_type is MemoryScopeType.AGENT and self.agent_profile_id is None:
            raise MemoryValidationError("agent scope requires an agent profile")
        if self.scope_type is MemoryScopeType.USER and self.agent_profile_id is not None:
            raise MemoryValidationError("user scope cannot have an agent profile")


def contains_secret(value: str) -> bool:
    return any(pattern.search(value) for pattern in _SECRET_PATTERNS)


def validate_provenance(provenance: MemoryProvenance) -> None:
    if provenance.evidence is not None and contains_secret(provenance.evidence):
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
    if contains_secret(content):
        raise MemoryValidationError("credential-like memory content is not accepted")
    if not 0 <= confidence <= 1 or not 0 <= importance <= 1:
        raise MemoryValidationError("confidence and importance must be between zero and one")
    if not MIN_HALF_LIFE_DAYS <= half_life_days <= MAX_HALF_LIFE_DAYS:
        raise MemoryValidationError("half-life must be between 0.25 and 3650 days")
    if valid_from and valid_to and valid_to < valid_from:
        raise MemoryValidationError("valid-to must not precede valid-from")


def validate_memory_text(value: str, field: str = "memory text") -> None:
    if contains_secret(value):
        raise MemoryValidationError(f"credential-like {field} is not accepted")


class MemoryRepository(Protocol):
    async def list_memories(self, issuer: str, subject: str, filters: MemoryFilters) -> list[MemoryRecord]: ...
    async def get_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord: ...
    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord: ...
    async def revise_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord: ...
    async def set_status(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord: ...
    async def set_pinned(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord: ...
    async def purge(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryAuditRecord: ...
    async def register_embedding_generation(self, issuer: str, subject: str, **kwargs: object) -> MemoryEmbeddingGeneration: ...
    async def activate_embedding_generation(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration: ...
    async def attach_embedding(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord: ...


def _fingerprint(operation: str, values: object) -> str:
    return hashlib.sha256(
        json.dumps([operation, values], default=str, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _command_values(values: dict[str, object]) -> dict[str, object]:
    """Return stable command input, excluding generated provenance metadata."""

    return {
        key: value
        for key, value in values.items()
        if key not in {"idempotency_key", "provenance"}
    }


def _owner(record: MemoryRecord, issuer: str, subject: str) -> bool:
    return record.issuer == issuer and record.subject == subject


def _scope_authorized(
    record: MemoryRecord,
    scope_type: MemoryScopeType | None,
    agent_profile_id: UUID | None,
    authorized_agent_ids: frozenset[UUID],
) -> bool:
    if record.scope.type is MemoryScopeType.USER:
        return scope_type in (None, MemoryScopeType.USER) and agent_profile_id is None
    return (
        scope_type is MemoryScopeType.AGENT
        and agent_profile_id == record.scope.agent_profile_id
        and (
            not authorized_agent_ids
            or record.scope.agent_profile_id in authorized_agent_ids
        )
    )


class MemoryStore:
    """Deterministic in-memory implementation of the public memory port."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self.memories: dict[UUID, MemoryRecord] = {}
        self.purge_audit: list[MemoryAuditRecord] = []
        self._idempotency: dict[tuple[str, str, str], tuple[str, object]] = {}
        self._purge_tombstones: set[tuple[str, str, str]] = set()
        self.embedding_generations: dict[UUID, MemoryEmbeddingGeneration] = {}
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        return value if value.tzinfo else value.replace(tzinfo=UTC)

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
        if record is None or not _owner(record, issuer, subject) or not _scope_authorized(
            record, scope_type, agent_profile_id, authorized_agent_ids
        ):
            raise MemoryNotFound("memory not found")
        return record

    async def list_memories(self, issuer: str, subject: str, filters: MemoryFilters | None = None) -> list[MemoryRecord]:
        criteria = filters or MemoryFilters()
        results: list[MemoryRecord] = []
        for item in self.memories.values():
            if not _owner(item, issuer, subject):
                continue
            if criteria.kind and item.kind is not criteria.kind:
                continue
            if criteria.scope_type and item.scope.type is not criteria.scope_type:
                continue
            if criteria.agent_profile_id and item.scope.agent_profile_id != criteria.agent_profile_id:
                continue
            if item.scope.type is MemoryScopeType.AGENT and criteria.authorized_agent_ids and item.scope.agent_profile_id not in criteria.authorized_agent_ids:
                continue
            if criteria.status and item.status is not criteria.status:
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
        return results[: max(1, min(criteria.limit, 200))]

    async def get_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        return self._find(
            issuer, subject, memory_id,
            scope_type=kwargs.get("scope_type"),
            agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )

    def _replay(self, issuer: str, subject: str, key: str | None, fingerprint: str) -> object | None:
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

    def _record_replay(self, issuer: str, subject: str, key: str | None, fingerprint: str, value: object) -> None:
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
            return prior
        now = self._now()
        memory_id = UUID(str(kwargs["memory_id"])) if kwargs.get("memory_id") else uuid4()
        revision_id = uuid4()
        provenance = list(kwargs.get("provenance", []))
        for item in provenance:
            validate_provenance(item)
        revision = MemoryRevision(revision_id, memory_id, 1, kind, content, kwargs.get("observed_at") or now, now, confidence, importance, half_life, valid_from, valid_to, tuple(item.id for item in provenance))  # type: ignore[arg-type]
        record = MemoryRecord(memory_id, issuer, subject, kind, scope, MemoryLifecycleStatus.ACTIVE, bool(kwargs.get("pinned", False)), 1, revision_id, [revision], provenance, list(kwargs.get("embeddings", [])), tuple(kwargs.get("related_memory_ids", ())), [], now, None, None, now, now)
        self.memories[memory_id] = record
        self._record_replay(issuer, subject, str(key) if key else None, fingerprint, record)
        return record

    async def revise_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        record = self._find(
            issuer, subject, memory_id,
            scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        content = str(kwargs["content"])
        reason = kwargs.get("reason")
        if reason is not None:
            validate_memory_text(str(reason), "correction reason")
        current = record.current_revision
        confidence = float(current.confidence if kwargs.get("confidence") is None else kwargs["confidence"])
        importance = float(current.importance if kwargs.get("importance") is None else kwargs["importance"])
        half_life = float(current.half_life_days if kwargs.get("half_life_days") is None else kwargs["half_life_days"])
        valid_from = current.valid_from if kwargs.get("valid_from") is None else kwargs["valid_from"]
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
        provenance = list(kwargs.get("provenance", []))
        for item in provenance:
            validate_provenance(item)
        kind = MemoryKind(str(kwargs["kind"])) if kwargs.get("kind") is not None else record.kind
        revision = MemoryRevision(uuid4(), memory_id, len(record.revisions) + 1, kind, content, kwargs.get("observed_at") or now, now, confidence, importance, half_life, valid_from, valid_to, tuple(item.id for item in provenance), str(reason) if reason is not None else None)  # type: ignore[arg-type]
        record.revisions.append(revision)
        record.provenance.extend(provenance)
        record.current_revision_id = revision.id
        record.kind = kind
        record.version += 1
        record.reinforced_at = now
        record.updated_at = now
        self._record_replay(issuer, subject, str(key) if key else None, fp, record)
        return record

    async def set_status(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        record = self._find(
            issuer, subject, memory_id,
            scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        status = MemoryLifecycleStatus(str(kwargs["status"]))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("status", {"memory": str(memory_id), "status": status.value, "expected": expected, "related": kwargs.get("related_memory_id"), "scope_type": kwargs.get("scope_type"), "agent_profile_id": kwargs.get("agent_profile_id")})
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryRecord)
            return prior
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        related = kwargs.get("related_memory_id")
        relation = (
            "disputes" if status is MemoryLifecycleStatus.DISPUTED
            else "supersedes" if status is MemoryLifecycleStatus.SUPERSEDED
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
                    issuer, subject, related,
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

    async def set_pinned(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        record = self._find(
            issuer, subject, memory_id,
            scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("pin", {"memory": str(memory_id), "expected": expected, "pinned": bool(kwargs["pinned"]), "scope_type": kwargs.get("scope_type"), "agent_profile_id": kwargs.get("agent_profile_id")})
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

    async def purge(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryAuditRecord:
        if kwargs.get("confirmation") != PURGE_CONFIRMATION:
            raise MemoryPurgeConfirmationRequired("exact purge confirmation is required")
        expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("purge", {"memory": str(memory_id), "expected": expected, "confirmation": kwargs.get("confirmation"), "scope_type": kwargs.get("scope_type"), "agent_profile_id": kwargs.get("agent_profile_id")})
        self._purge_tombstone(issuer, subject, key)
        prior = self._replay(issuer, subject, str(key) if key else None, fp)
        if prior is not None:
            assert isinstance(prior, MemoryAuditRecord)
            return prior
        record = self._find(
            issuer, subject, memory_id,
            scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"),
            authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())),
        )
        if expected != record.version:
            raise MemoryVersionConflict("memory version conflict")
        audit = MemoryAuditRecord(uuid4(), issuer, subject, memory_id, "purge", self._now())
        del self.memories[memory_id]
        stale_keys = [key for key, value in self._idempotency.items() if isinstance(value[1], MemoryRecord) and value[1].id == memory_id]
        for stale_key in stale_keys:
            del self._idempotency[stale_key]
            self._purge_tombstones.add(stale_key)
        self.purge_audit.append(audit)
        self._record_replay(issuer, subject, str(key) if key else None, fp, audit)
        return audit

    async def register_embedding_generation(self, issuer: str, subject: str, **kwargs: object) -> MemoryEmbeddingGeneration:
        del issuer, subject
        now = self._now()
        generation = int(kwargs["generation"])
        model_id = str(kwargs["model_id"])
        dimension = int(kwargs["dimension"])
        if dimension < 1 or not model_id:
            raise MemoryValidationError("embedding model and dimension are required")
        digest = kwargs.get("model_digest")
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", str(digest)):
            raise MemoryValidationError("embedding model digest must be sha256")
        if any(item.generation == generation for item in self.embedding_generations.values()):
            raise MemoryValidationError("embedding generation number already exists")
        item = MemoryEmbeddingGeneration(uuid4(), generation, model_id, kwargs.get("model_revision"), dimension, "building", now, None, str(digest) if digest else None)
        self.embedding_generations[item.id] = item
        return item

    async def activate_embedding_generation(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration:
        del issuer, subject
        try:
            item = self.embedding_generations[generation_id]
        except KeyError as exc:
            raise MemoryNotFound("embedding generation not found") from exc
        activated = MemoryEmbeddingGeneration(item.id, item.generation, item.model_id, item.model_revision, item.dimension, "active", item.created_at, self._now(), item.model_digest)
        self.embedding_generations[generation_id] = activated
        for record in self.memories.values():
            record.embedding_generations = [
                activated if generation.id == generation_id else generation
                for generation in record.embedding_generations
            ]
        return activated

    async def attach_embedding(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        record = self._find(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"), authorized_agent_ids=frozenset(kwargs.get("authorized_agent_ids", frozenset())))
        revision_id = kwargs.get("revision_id", record.current_revision_id)
        if revision_id not in {item.id for item in record.revisions}:
            raise MemoryNotFound("memory revision not found")
        generation_id = kwargs["generation_id"]
        generation = self.embedding_generations.get(generation_id)
        if generation is None:
            raise MemoryNotFound("embedding generation not found")
        vector = tuple(float(value) for value in cast(Iterable[object], kwargs["vector"]))
        if len(vector) != generation.dimension or not all(math.isfinite(value) for value in vector):
            raise MemoryValidationError("embedding dimension or values do not match generation")
        digest = str(kwargs.get("digest", ""))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise MemoryValidationError("embedding digest must be sha256")
        if any(
            item.revision_id == revision_id and item.generation_id == generation.id
            for item in record.embeddings
        ):
            raise MemoryValidationError("embedding already exists for revision and generation")
        record.embeddings.append(MemoryEmbedding(uuid4(), revision_id, generation.generation, generation.model_id, generation.model_revision, generation.dimension, digest, self._now(), vector, generation.id))
        if all(item.id != generation.id for item in record.embedding_generations):
            record.embedding_generations.append(generation)
        return record


MemoryCatalog = MemoryStore
MemoryService = MemoryStore


__all__ = [
    "ARCHIVE_AFTER_DAYS", "DORMANT_THRESHOLD", "MAX_HALF_LIFE_DAYS", "MIN_HALF_LIFE_DAYS",
    "PURGE_CONFIRMATION", "MemoryAuditRecord", "MemoryCatalog", "MemoryEmbedding", "MemoryError",
    "MemoryEmbeddingGeneration", "MemoryFilters", "MemoryIdempotencyConflict", "MemoryKind", "MemoryLifecycleStatus",
    "MemoryNotFound", "MemoryProvenance", "MemoryPurgeConfirmationRequired", "MemoryPurgeReplayNotFound", "MemoryRecord", "MemoryRelation", "MemoryScopeAuthorizationRequired",
    "MemoryRepository", "MemoryRevision", "MemoryScope", "MemoryScopeType", "MemoryService",
    "MemoryStore", "MemoryValidationError", "MemoryVersionConflict", "contains_secret",
    "validate_memory_text", "validate_provenance", "validate_revision",
]
