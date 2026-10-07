"""PostgreSQL adapter for the public memory repository port."""

# SQLAlchemy's dynamically typed row attributes are normalized at hydration.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownMemberType=false, reportPrivateUsage=false

# SQL statements and hydration stay close to their transaction boundaries.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.knowledge.memory.persistence import (
    MemoryEmbeddingGenerationRow,
    MemoryEmbeddingRow,
    MemoryIdempotencyRow,
    MemoryProvenanceRow,
    MemoryPurgeAuditRow,
    MemoryRelationRow,
    MemoryRevisionRow,
    MemoryRow,
)
from aura_core.domains.knowledge.memory.public import (
    PURGE_CONFIRMATION,
    MemoryAuditRecord,
    MemoryEmbedding,
    MemoryEmbeddingGeneration,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryNotFound,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryPurgeReplayNotFound,
    MemoryRecord,
    MemoryRelation,
    MemoryRevision,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemoryValidationError,
    MemoryVersionConflict,
    _fingerprint,
    validate_memory_text,
    validate_provenance,
    validate_revision,
)


def _vector_tuple(value: object, dimension: int) -> tuple[float, ...] | None:
    """Normalize psycopg/pgvector values without evaluating driver text."""

    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not (text.startswith("[") and text.endswith("]")):
            raise ValueError("invalid persisted embedding vector")
        inner = text[1:-1].strip()
        values = () if not inner else tuple(float(part.strip()) for part in inner.split(","))
    else:
        values = tuple(float(item) for item in cast(Iterable[object], value))
    if len(values) != dimension or not all(math.isfinite(item) for item in values):
        raise ValueError("persisted embedding dimension is invalid")
    return values


class SqlMemoryRepository:
    """Transactional owner-scoped implementation of :class:`MemoryRepository`."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    async def _lock_command(session: AsyncSession, issuer: str, subject: str, key: object) -> None:
        if key:
            canonical = "".join(
                f"{len(part)}:{part};" for part in (issuer, subject, str(key))
            )
            lock_key = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:lock_key, 0))"),
                {"lock_key": lock_key},
            )

    async def _replay(self, session: AsyncSession, issuer: str, subject: str, key: object, fingerprint: str) -> MemoryRecord | MemoryAuditRecord | None:
        if not key:
            return None
        prior = await session.get(MemoryIdempotencyRow, (issuer, subject, str(key)))
        if prior is None:
            return None
        if prior.fingerprint != fingerprint:
            raise MemoryIdempotencyConflict("idempotency key payload conflict")
        if prior.tombstone:
            raise MemoryIdempotencyConflict("idempotency key is unavailable after purge")
        if prior.audit_id:
            audit = await session.get(MemoryPurgeAuditRow, prior.audit_id)
            if audit is not None:
                return MemoryAuditRecord(audit.id, issuer, subject, audit.memory_id, audit.action, audit.created_at or datetime.now(UTC))
        row = (await session.execute(select(MemoryRow).where(MemoryRow.id == prior.memory_id, MemoryRow.principal_issuer == issuer, MemoryRow.principal_subject == subject))).scalar_one_or_none()
        if row is None:
            raise MemoryIdempotencyConflict("idempotency key is unavailable after purge")
        return await self._hydrate(session, row)

    @staticmethod
    def _stage_idempotency(session: AsyncSession, issuer: str, subject: str, key: object, fingerprint: str, memory_id: UUID, audit_id: UUID | None = None) -> None:
        if key:
            session.add(MemoryIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=str(key), fingerprint=fingerprint, memory_id=memory_id, audit_id=audit_id))

    @staticmethod
    def _revision(row: MemoryRevisionRow) -> MemoryRevision:
        return MemoryRevision(
            row.id, row.memory_id, row.revision, MemoryKind(row.kind), row.content,
            row.observed_at, row.created_at or datetime.now(UTC), row.confidence,
            row.importance, row.half_life_days, row.valid_from, row.valid_to,
            tuple(UUID(item) for item in (row.provenance_ids or [])),
            row.correction_reason,
        )

    @staticmethod
    def _provenance(row: MemoryProvenanceRow) -> MemoryProvenance:
        return MemoryProvenance(
            row.id, row.source_type, row.source_id, row.conversation_id,
            row.run_id, row.message_id, row.observed_at, row.evidence_digest, row.evidence,
            row.created_at or datetime.now(UTC),
        )

    async def _hydrate(self, session: AsyncSession, row: MemoryRow) -> MemoryRecord:
        revisions = (await session.execute(
            select(MemoryRevisionRow).where(MemoryRevisionRow.memory_id == row.id).order_by(MemoryRevisionRow.revision)
        )).scalars().all()
        provenance = (await session.execute(
            select(MemoryProvenanceRow).where(MemoryProvenanceRow.memory_id == row.id).order_by(MemoryProvenanceRow.observed_at)
        )).scalars().all()
        revision_ids = [item.id for item in revisions]
        embeddings = (await session.execute(
            select(MemoryEmbeddingRow).where(MemoryEmbeddingRow.revision_id.in_(revision_ids))
        )).scalars().all() if revision_ids else []
        generation_ids = {item.generation_id for item in embeddings}
        relation_rows = (await session.execute(
            select(MemoryRelationRow).where(MemoryRelationRow.memory_id == row.id)
        )).scalars().all()
        record = MemoryRecord(
            row.id, row.principal_issuer, row.principal_subject, MemoryKind(row.kind),
            MemoryScope(MemoryScopeType(row.scope_type), row.agent_profile_id),
            MemoryLifecycleStatus(row.status), row.pinned, row.version, row.current_revision_id,
            [self._revision(item) for item in revisions],
            [self._provenance(item) for item in provenance],
            [MemoryEmbedding(
                item.id, item.revision_id, item.generation, item.model_id,
                item.model_revision, item.dimension, item.digest,
                item.created_at or datetime.now(UTC),
                _vector_tuple(item.vector, item.dimension),
                item.generation_id,
            ) for item in embeddings],
            tuple(item.related_memory_id for item in relation_rows),
            [MemoryRelation(item.related_memory_id, item.relation, item.created_at or datetime.now(UTC)) for item in relation_rows],
            row.reinforced_at, row.dormant_at, row.archived_at,
            row.created_at or datetime.now(UTC), row.updated_at or datetime.now(UTC),
        )
        generation_rows = (await session.execute(
            select(MemoryEmbeddingGenerationRow).where(MemoryEmbeddingGenerationRow.id.in_(generation_ids))
        )).scalars().all() if generation_ids else []
        record.embedding_generations = [
            MemoryEmbeddingGeneration(item.id, item.generation, item.model_id, item.model_revision, item.dimension, item.status, item.created_at or datetime.now(UTC), item.activated_at, item.model_digest)
            for item in generation_rows
        ]
        return record

    async def _owned(
        self, session: AsyncSession, issuer: str, subject: str, memory_id: UUID,
        *, lock: bool = False, scope_type: MemoryScopeType | None = None,
        agent_profile_id: UUID | None = None,
    ) -> MemoryRow:
        query = select(MemoryRow).where(
            MemoryRow.id == memory_id,
            MemoryRow.principal_issuer == issuer,
            MemoryRow.principal_subject == subject,
        )
        row = (await session.execute(query.with_for_update() if lock else query)).scalar_one_or_none()
        if row is None:
            raise MemoryNotFound("memory not found")
        if scope_type in (None, MemoryScopeType.USER) and (row.scope_type != "user" or agent_profile_id is not None):
            raise MemoryNotFound("memory not found")
        if scope_type is MemoryScopeType.AGENT and (row.scope_type != "agent" or row.agent_profile_id != agent_profile_id):
            raise MemoryNotFound("memory not found")
        return row

    async def list_memories(self, issuer: str, subject: str, filters: MemoryFilters | None = None) -> list[MemoryRecord]:
        criteria = filters or MemoryFilters()
        async with self.sessions() as session:
            query = select(MemoryRow).where(MemoryRow.principal_issuer == issuer, MemoryRow.principal_subject == subject)
            if criteria.kind:
                query = query.where(MemoryRow.kind == criteria.kind.value)
            if criteria.scope_type:
                query = query.where(MemoryRow.scope_type == criteria.scope_type.value)
            if criteria.agent_profile_id:
                query = query.where(MemoryRow.agent_profile_id == criteria.agent_profile_id)
            if criteria.scope_type is MemoryScopeType.AGENT and criteria.agent_profile_id not in criteria.authorized_agent_ids:
                # An explicit selector is the minimum authorization at this
                # foundation boundary; a caller may further restrict grants.
                if criteria.authorized_agent_ids:
                    return []
            if criteria.status:
                query = query.where(MemoryRow.status == criteria.status.value)
            elif not criteria.include_historical:
                query = query.where(MemoryRow.status == MemoryLifecycleStatus.ACTIVE.value)
            rows = (await session.execute(query.order_by(MemoryRow.updated_at.desc()))).scalars().all()
            values = [await self._hydrate(session, row) for row in rows]
            if criteria.q:
                needle = criteria.q.casefold()
                values = [item for item in values if needle in item.content.casefold()]
            return values[: max(1, min(criteria.limit, 100))]

    async def get_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        async with self.sessions() as session:
            return await self._hydrate(session, await self._owned(session, issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id")))

    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        kind = MemoryKind(str(kwargs["kind"]))
        scope = kwargs["scope"]
        if not isinstance(scope, MemoryScope):
            scope = MemoryScope(MemoryScopeType(str(scope)), kwargs.get("agent_profile_id"))  # type: ignore[arg-type]
        authorized_agent_ids = frozenset(kwargs.get("authorized_agent_ids", frozenset()))
        if scope.type is MemoryScopeType.AGENT and authorized_agent_ids and scope.agent_profile_id not in authorized_agent_ids:
            raise MemoryScopeAuthorizationRequired("agent scope is not authorized")
        content = str(kwargs["content"])
        confidence, importance = float(kwargs.get("confidence", 1.0)), float(kwargs.get("importance", 0.5))
        half_life = float(kwargs.get("half_life_days", 30.0))
        valid_from, valid_to = kwargs.get("valid_from"), kwargs.get("valid_to")
        validate_revision(content, confidence, importance, half_life, valid_from, valid_to)  # type: ignore[arg-type]
        now = datetime.now(UTC)
        memory_id = UUID(str(kwargs["memory_id"])) if kwargs.get("memory_id") else uuid4()
        revision_id = uuid4()
        provenance = list(kwargs.get("provenance", []))
        for item in provenance:
            validate_provenance(item)
        key = kwargs.get("idempotency_key")
        fingerprint = _fingerprint("create", {k: v for k, v in kwargs.items() if k not in {"idempotency_key", "provenance"}})
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            if prior is not None:
                assert isinstance(prior, MemoryRecord)
                return prior
            # Idempotency uses the Core command table only through its public
            # row shape; storing a small per-memory key is sufficient here.
            row = MemoryRow(id=memory_id, principal_issuer=issuer, principal_subject=subject, kind=kind.value, scope_type=scope.type.value, agent_profile_id=scope.agent_profile_id, current_revision_id=revision_id, created_at=now, updated_at=now)
            session.add(row)
            session.add(MemoryRevisionRow(id=revision_id, memory_id=memory_id, revision=1, kind=kind.value, content=content, confidence=confidence, importance=importance, half_life_days=half_life, observed_at=kwargs.get("observed_at") or now, valid_from=valid_from, valid_to=valid_to, provenance_ids=[str(item.id) for item in provenance], correction_reason=None))
            for item in provenance:
                session.add(MemoryProvenanceRow(id=item.id, memory_id=memory_id, source_type=item.source_type, source_id=item.source_id, conversation_id=item.conversation_id, run_id=item.run_id, message_id=item.message_id, evidence_digest=item.evidence_digest, evidence=item.evidence, observed_at=item.observed_at, created_at=now))
            self._stage_idempotency(session, issuer, subject, key, fingerprint, memory_id)
        return await self.get_memory(issuer, subject, memory_id, scope_type=scope.type, agent_profile_id=scope.agent_profile_id)

    async def revise_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        content = str(kwargs["content"])
        reason = kwargs.get("reason")
        if reason is not None:
            validate_memory_text(str(reason), "correction reason")
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("revise", {k: v for k, v in kwargs.items() if k not in {"idempotency_key", "provenance"}} | {"memory_id": str(memory_id)})
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            prior = await self._replay(session, issuer, subject, key, fp)
            if prior is not None:
                assert isinstance(prior, MemoryRecord)
                return prior
            expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
            if row.version != expected:
                raise MemoryVersionConflict("memory version conflict")
            previous = (await session.execute(select(MemoryRevisionRow).where(MemoryRevisionRow.id == row.current_revision_id))).scalar_one()
            confidence = float(previous.confidence if kwargs.get("confidence") is None else kwargs["confidence"])
            importance = float(previous.importance if kwargs.get("importance") is None else kwargs["importance"])
            half_life = float(previous.half_life_days if kwargs.get("half_life_days") is None else kwargs["half_life_days"])
            valid_from = previous.valid_from if kwargs.get("valid_from") is None else kwargs["valid_from"]
            valid_to = previous.valid_to if kwargs.get("valid_to") is None else kwargs["valid_to"]
            validate_revision(content, confidence, importance, half_life, valid_from, valid_to)  # type: ignore[arg-type]
            number = (await session.execute(select(MemoryRevisionRow.revision).where(MemoryRevisionRow.memory_id == memory_id).order_by(MemoryRevisionRow.revision.desc()).limit(1))).scalar_one() + 1
            revision_id, now = uuid4(), datetime.now(UTC)
            provenance = list(kwargs.get("provenance", []))
            for item in provenance:
                validate_provenance(item)
            kind = MemoryKind(str(kwargs["kind"])) if kwargs.get("kind") is not None else MemoryKind(row.kind)
            row.kind = kind.value
            session.add(MemoryRevisionRow(id=revision_id, memory_id=memory_id, revision=number, kind=kind.value, content=content, confidence=confidence, importance=importance, half_life_days=half_life, observed_at=kwargs.get("observed_at") or now, valid_from=valid_from, valid_to=valid_to, provenance_ids=[str(item.id) for item in provenance], correction_reason=str(reason) if reason is not None else None))
            for item in provenance:
                session.add(MemoryProvenanceRow(id=item.id, memory_id=memory_id, source_type=item.source_type, source_id=item.source_id, conversation_id=item.conversation_id, run_id=item.run_id, message_id=item.message_id, evidence_digest=item.evidence_digest, evidence=item.evidence, observed_at=item.observed_at, created_at=now))
            row.current_revision_id, row.version, row.reinforced_at, row.updated_at = revision_id, row.version + 1, now, now
            self._stage_idempotency(session, issuer, subject, key, fp, memory_id)
        return await self.get_memory(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))

    async def set_status(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        key = kwargs.get("idempotency_key")
        status = MemoryLifecycleStatus(str(kwargs["status"]))
        fp = _fingerprint("status", {"memory": str(memory_id), "status": status.value, "expected": kwargs.get("expected_version", kwargs.get("expectedVersion")), "related": kwargs.get("related_memory_id"), "scope_type": kwargs.get("scope_type"), "agent_profile_id": kwargs.get("agent_profile_id")})
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            prior = await self._replay(session, issuer, subject, key, fp)
            if prior is not None:
                assert isinstance(prior, MemoryRecord)
                return prior
            expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
            if row.version != expected:
                raise MemoryVersionConflict("memory version conflict")
            now = datetime.now(UTC)
            row.status, row.version, row.updated_at = status.value, row.version + 1, now
            if status is MemoryLifecycleStatus.DORMANT:
                row.dormant_at = now
            if status is MemoryLifecycleStatus.ARCHIVED:
                row.archived_at = now
            related = kwargs.get("related_memory_id")
            relation = "disputes" if status is MemoryLifecycleStatus.DISPUTED else "supersedes" if status is MemoryLifecycleStatus.SUPERSEDED else None
            if isinstance(related, UUID) and relation is not None:
                if related == memory_id:
                    raise MemoryNotFound("related memory not found")
                related_row = (await session.execute(select(MemoryRow).where(MemoryRow.id == related, MemoryRow.principal_issuer == issuer, MemoryRow.principal_subject == subject))).scalar_one_or_none()
                if related_row is None:
                    raise MemoryNotFound("related memory not found")
                authorized_agent_ids = frozenset(kwargs.get("authorized_agent_ids", frozenset()))
                if related_row.scope_type != row.scope_type or related_row.agent_profile_id != row.agent_profile_id:
                    raise MemoryNotFound("related memory not found")
                if related_row.scope_type == "agent" and (
                    related_row.agent_profile_id != kwargs.get("agent_profile_id")
                    and related_row.agent_profile_id not in authorized_agent_ids
                ):
                    raise MemoryNotFound("related memory not found")
                session.add(MemoryRelationRow(memory_id=memory_id, related_memory_id=related, relation=relation, created_at=now))
            self._stage_idempotency(session, issuer, subject, key, fp, memory_id)
        return await self.get_memory(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))

    async def set_pinned(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("pin", {"memory": str(memory_id), "expected": kwargs.get("expected_version", kwargs.get("expectedVersion")), "pinned": bool(kwargs["pinned"]), "scope_type": kwargs.get("scope_type"), "agent_profile_id": kwargs.get("agent_profile_id")})
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            prior = await self._replay(session, issuer, subject, key, fp)
            if prior is not None:
                assert isinstance(prior, MemoryRecord)
                return prior
            expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
            if row.version != expected:
                raise MemoryVersionConflict("memory version conflict")
            row.pinned, row.version, row.updated_at = bool(kwargs["pinned"]), row.version + 1, datetime.now(UTC)
            self._stage_idempotency(session, issuer, subject, key, fp, memory_id)
        return await self.get_memory(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))

    async def purge(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryAuditRecord:
        if kwargs.get("confirmation") != PURGE_CONFIRMATION:
            raise MemoryPurgeConfirmationRequired("exact purge confirmation is required")
        key = kwargs.get("idempotency_key")
        fp = _fingerprint("purge", {"memory": str(memory_id), "expected": kwargs.get("expected_version", kwargs.get("expectedVersion")), "confirmation": kwargs.get("confirmation"), "scope_type": kwargs.get("scope_type"), "agent_profile_id": kwargs.get("agent_profile_id")})
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            if key:
                prior = await session.get(MemoryIdempotencyRow, (issuer, subject, str(key)))
                if prior is not None:
                    if prior.tombstone:
                        raise MemoryPurgeReplayNotFound("memory not found")
                    if prior.fingerprint != fp:
                        raise MemoryIdempotencyConflict("idempotency key payload conflict")
                    audit = await session.get(MemoryPurgeAuditRow, prior.audit_id) if prior.audit_id else None
                    if audit is not None:
                        return MemoryAuditRecord(audit.id, issuer, subject, audit.memory_id, audit.action, audit.created_at or datetime.now(UTC))
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            expected = int(kwargs.get("expected_version", kwargs.get("expectedVersion", 0)))
            if row.version != expected:
                raise MemoryVersionConflict("memory version conflict")
            revisions = (await session.execute(select(MemoryRevisionRow.id).where(MemoryRevisionRow.memory_id == memory_id))).scalars().all()
            await session.execute(delete(MemoryEmbeddingRow).where(MemoryEmbeddingRow.revision_id.in_(revisions)))
            await session.execute(delete(MemoryProvenanceRow).where(MemoryProvenanceRow.memory_id == memory_id))
            await session.execute(delete(MemoryRelationRow).where((MemoryRelationRow.memory_id == memory_id) | (MemoryRelationRow.related_memory_id == memory_id)))
            await session.execute(delete(MemoryRevisionRow).where(MemoryRevisionRow.memory_id == memory_id))
            await session.execute(delete(MemoryIdempotencyRow).where(MemoryIdempotencyRow.memory_id == memory_id))
            await session.delete(row)
            audit_id = uuid4()
            audit_row = MemoryPurgeAuditRow(id=audit_id, memory_id=memory_id, principal_issuer=issuer, principal_subject=subject, created_at=datetime.now(UTC))
            session.add(audit_row)
            await session.flush()
            await session.refresh(audit_row)
            audit_created_at = audit_row.created_at or datetime.now(UTC)
            if key:
                session.add(MemoryIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=str(key), fingerprint=fp, memory_id=memory_id, audit_id=audit_id, tombstone=False))
        return MemoryAuditRecord(audit_id, issuer, subject, memory_id, "purge", audit_created_at)

    async def register_embedding_generation(self, issuer: str, subject: str, **kwargs: object) -> MemoryEmbeddingGeneration:
        del issuer, subject
        generation = int(kwargs["generation"])
        model_id = str(kwargs["model_id"])
        dimension = int(kwargs["dimension"])
        model_digest = kwargs.get("model_digest")
        if not model_id or dimension < 1:
            raise ValueError("embedding model and dimension are required")
        if model_digest is not None and (not isinstance(model_digest, str) or len(model_digest) != 64 or not all(char in "0123456789abcdef" for char in model_digest)):
            raise ValueError("embedding model digest must be sha256")
        item = MemoryEmbeddingGeneration(uuid4(), generation, model_id, kwargs.get("model_revision"), dimension, "building", datetime.now(UTC), None, str(model_digest) if model_digest else None)
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, "embedding", "generation", generation)
            existing = (await session.execute(
                select(MemoryEmbeddingGenerationRow.id).where(
                    MemoryEmbeddingGenerationRow.generation == generation
                )
            )).scalar_one_or_none()
            if existing is not None:
                raise MemoryValidationError("embedding generation number already exists")
            session.add(MemoryEmbeddingGenerationRow(id=item.id, generation=item.generation, model_id=item.model_id, model_revision=item.model_revision, model_digest=item.model_digest, dimension=item.dimension, status=item.status, created_at=item.created_at))
        return item

    async def activate_embedding_generation(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration:
        del issuer, subject
        async with self.sessions() as session, session.begin():
            row = await session.get(MemoryEmbeddingGenerationRow, generation_id, with_for_update=True)
            if row is None:
                raise MemoryNotFound("embedding generation not found")
            row.status, row.activated_at = "active", datetime.now(UTC)
            return MemoryEmbeddingGeneration(row.id, row.generation, row.model_id, row.model_revision, row.dimension, row.status, row.created_at or datetime.now(UTC), row.activated_at, row.model_digest)

    async def attach_embedding(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        async with self.sessions() as session, session.begin():
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            generation_id = kwargs["generation_id"]
            generation = await session.get(MemoryEmbeddingGenerationRow, generation_id)
            if generation is None:
                raise MemoryNotFound("embedding generation not found")
            revision_id = kwargs.get("revision_id", row.current_revision_id)
            revision = (await session.execute(select(MemoryRevisionRow).where(MemoryRevisionRow.id == revision_id, MemoryRevisionRow.memory_id == memory_id))).scalar_one_or_none()
            if revision is None:
                raise MemoryNotFound("memory revision not found")
            vector = [float(value) for value in cast(Iterable[object], kwargs["vector"])]
            digest = str(kwargs["digest"])
            if len(vector) != generation.dimension or not all(math.isfinite(value) for value in vector):
                raise MemoryValidationError("embedding dimension or values do not match generation")
            if len(digest) != 64 or not all(char in "0123456789abcdef" for char in digest):
                raise MemoryValidationError("embedding digest must be sha256")
            duplicate = (await session.execute(
                select(MemoryEmbeddingRow.id).where(
                    MemoryEmbeddingRow.revision_id == revision_id,
                    MemoryEmbeddingRow.generation_id == generation.id,
                )
            )).scalar_one_or_none()
            if duplicate is not None:
                raise ValueError("embedding already exists for revision and generation")
            session.add(MemoryEmbeddingRow(id=uuid4(), revision_id=revision_id, generation=generation.generation, generation_id=generation.id, model_id=generation.model_id, model_revision=generation.model_revision, dimension=generation.dimension, digest=digest, vector=vector, created_at=datetime.now(UTC)))
        return await self.get_memory(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))


__all__ = ["SqlMemoryRepository"]
