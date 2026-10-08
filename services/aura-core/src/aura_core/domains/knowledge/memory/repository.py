"""PostgreSQL adapter for the public memory repository port."""

# SQLAlchemy's dynamically typed row attributes are normalized at hydration.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportArgumentType=false, reportUnknownMemberType=false, reportPrivateUsage=false

# SQL statements and hydration stay close to their transaction boundaries.
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import math
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.knowledge.memory.persistence import (
    MemoryActionOutcomeRow,
    MemoryCandidateRow,
    MemoryEmbeddingGenerationRow,
    MemoryEmbeddingJobRow,
    MemoryEmbeddingRow,
    MemoryIdempotencyRow,
    MemoryMaintenanceStateRow,
    MemoryModelConfigurationRow,
    MemoryProcessingJobRow,
    MemoryProvenanceRow,
    MemoryPurgeAuditRow,
    MemoryRelationRow,
    MemoryRevisionRow,
    MemoryRow,
)
from aura_core.domains.knowledge.memory.public import (
    MEMORY_ID_NAMESPACE,
    PURGE_CONFIRMATION,
    CandidateState,
    MemoryAction,
    MemoryAuditRecord,
    MemoryCandidate,
    MemoryEmbedding,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingJob,
    MemoryProvenance,
    MemoryPurgeConfirmationRequired,
    MemoryPurgeReplayNotFound,
    MemoryRecord,
    MemoryRelation,
    MemoryRevision,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemorySensitivity,
    MemoryValidationError,
    MemoryVersionConflict,
    ProcessingJobStatus,
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

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        """Return the repository clock as an aware UTC instant.

        The injectable clock is intentionally limited to lease/backoff state so
        production keeps real-time pacing while tests can deterministically
        advance retry availability.
        """

        value = self._clock()
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def set_clock_for_testing(self, clock: Callable[[], datetime]) -> None:
        """Replace the clock through an explicit deterministic test seam."""

        self._clock = clock

    async def get_embedding_generation(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration:
        async with self.sessions() as session:
            row = await session.get(MemoryEmbeddingGenerationRow, generation_id)
            if row is None or row.principal_issuer != issuer or row.principal_subject != subject:
                raise MemoryNotFound("embedding generation not found")
            return MemoryEmbeddingGeneration(row.id, row.generation, row.model_id, row.model_revision, row.dimension, row.status, row.created_at or datetime.now(UTC), row.activated_at, row.model_digest, row.principal_issuer, row.principal_subject)

    async def list_embedding_generations(self, issuer: str, subject: str, *, status: str | None = None) -> list[MemoryEmbeddingGeneration]:
        async with self.sessions() as session:
            query = select(MemoryEmbeddingGenerationRow).where(
                MemoryEmbeddingGenerationRow.principal_issuer == issuer,
                MemoryEmbeddingGenerationRow.principal_subject == subject,
            ).order_by(MemoryEmbeddingGenerationRow.generation)
            if status is not None:
                query = query.where(MemoryEmbeddingGenerationRow.status == status)
            rows = (await session.execute(query)).scalars().all()
            return [MemoryEmbeddingGeneration(row.id, row.generation, row.model_id, row.model_revision, row.dimension, row.status, row.created_at or datetime.now(UTC), row.activated_at, row.model_digest, row.principal_issuer, row.principal_subject) for row in rows]

    async def list_processing_owners(self) -> list[tuple[str, str]]:
        async with self.sessions() as session:
            rows = (await session.execute(text(
                "SELECT principal_issuer, principal_subject FROM memories "
                "UNION SELECT principal_issuer, principal_subject FROM memory_processing_jobs "
                "UNION SELECT principal_issuer, principal_subject FROM memory_model_configurations "
                "UNION SELECT principal_issuer, principal_subject FROM memory_embedding_generations"
            ))).all()
            return [(str(row[0]), str(row[1])) for row in rows]

    async def save_maintenance_state(
        self, issuer: str, subject: str, *, ran_at: datetime,
        generation: int | None = None, generation_id: UUID | None = None,
        cursor: UUID | None = None, completed: int = 0,
    ) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(
                MemoryMaintenanceStateRow,
                (issuer, subject), with_for_update=True,
            )
            if row is None:
                row = MemoryMaintenanceStateRow(
                    principal_issuer=issuer, principal_subject=subject,
                    last_run_at=ran_at, last_reindex_generation=generation,
                    reindex_generation_id=generation_id, reindex_cursor=cursor,
                    reindex_completed=completed, updated_at=ran_at,
                )
                session.add(row)
            else:
                row.last_run_at = ran_at
                if generation is not None:
                    row.last_reindex_generation = generation
                row.reindex_generation_id = generation_id
                row.reindex_cursor = cursor
                row.reindex_completed = completed
                row.updated_at = ran_at

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
    async def _assert_not_fenced(session: AsyncSession, issuer: str, subject: str, memory_id: UUID) -> None:
        fenced = (await session.execute(text(
            "SELECT 1 FROM memory_purge_fences WHERE principal_issuer = :issuer "
            "AND principal_subject = :subject AND memory_id = :memory_id"
        ), {"issuer": issuer, "subject": subject, "memory_id": memory_id})).scalar_one_or_none()
        if fenced is not None:
            raise MemoryNotFound("memory not found")

    @staticmethod
    def _stage_idempotency(session: AsyncSession, issuer: str, subject: str, key: object, fingerprint: str, memory_id: UUID, audit_id: UUID | None = None) -> None:
        if key:
            session.add(MemoryIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=str(key), fingerprint=fingerprint, memory_id=memory_id, audit_id=audit_id))

    @staticmethod
    async def _stage_missing_embedding(
        session: AsyncSession, issuer: str, subject: str,
        memory_id: UUID, revision_id: UUID,
    ) -> None:
        """Queue the selected owner generation for every new revision."""

        configuration = await session.get(
            MemoryModelConfigurationRow, (issuer, subject)
        )
        generation_id = configuration.embedding_generation if configuration else None
        if generation_id is None:
            return
        generation = (await session.execute(select(MemoryEmbeddingGenerationRow).where(
            MemoryEmbeddingGenerationRow.id == generation_id,
            MemoryEmbeddingGenerationRow.principal_issuer == issuer,
            MemoryEmbeddingGenerationRow.principal_subject == subject,
        ))).scalar_one_or_none()
        if generation is None:
            raise MemoryNotFound("embedding generation not found")
        job_id = uuid5(MEMORY_ID_NAMESPACE, f"embedding-job:{issuer}:{subject}:{revision_id}:{generation_id}")
        existing = await session.get(MemoryEmbeddingJobRow, job_id)
        if existing is None:
            session.add(MemoryEmbeddingJobRow(
                id=job_id, principal_issuer=issuer, principal_subject=subject,
                memory_id=memory_id, revision_id=revision_id,
                generation_id=generation_id, status="queued", attempt_count=0,
            ))

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
            select(MemoryEmbeddingRow).where(
                MemoryEmbeddingRow.revision_id.in_(revision_ids),
                MemoryEmbeddingRow.principal_issuer == row.principal_issuer,
                MemoryEmbeddingRow.principal_subject == row.principal_subject,
            )
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
            select(MemoryEmbeddingGenerationRow).where(
                MemoryEmbeddingGenerationRow.id.in_(generation_ids),
                MemoryEmbeddingGenerationRow.principal_issuer == row.principal_issuer,
                MemoryEmbeddingGenerationRow.principal_subject == row.principal_subject,
            )
        )).scalars().all() if generation_ids else []
        record.embedding_generations = [
            MemoryEmbeddingGeneration(item.id, item.generation, item.model_id, item.model_revision, item.dimension, item.status, item.created_at or datetime.now(UTC), item.activated_at, item.model_digest, item.principal_issuer, item.principal_subject)
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
            if criteria.scope_type and not criteria.include_all_scopes:
                query = query.where(MemoryRow.scope_type == criteria.scope_type.value)
            if criteria.agent_profile_id:
                query = query.where(MemoryRow.agent_profile_id == criteria.agent_profile_id)
            if criteria.scope_type is MemoryScopeType.AGENT and not criteria.include_all_scopes and criteria.agent_profile_id not in criteria.authorized_agent_ids:
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
            return values[: max(1, min(criteria.limit, 100_000))]

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
        now = self._now()
        memory_id = UUID(str(kwargs["memory_id"])) if kwargs.get("memory_id") else uuid4()
        revision_id = uuid4()
        provenance = list(kwargs.get("provenance", []))
        for item in provenance:
            validate_provenance(item)
        key = kwargs.get("idempotency_key")
        fingerprint = _fingerprint("create", {k: v for k, v in kwargs.items() if k not in {"idempotency_key", "provenance"}})
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            await self._assert_not_fenced(session, issuer, subject, memory_id)
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
            await self._stage_missing_embedding(session, issuer, subject, memory_id, revision_id)
            self._stage_idempotency(session, issuer, subject, key, fingerprint, memory_id)
        return await self.get_memory(issuer, subject, memory_id, scope_type=scope.type, agent_profile_id=scope.agent_profile_id)

    async def reinforce_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        key = kwargs.get("idempotency_key")
        provenance = list(cast(Iterable[MemoryProvenance], kwargs.get("provenance", ())))
        fp = _fingerprint("reinforce", {"memory_id": str(memory_id), "provenance": [str(item.id) for item in provenance]})
        for item in provenance:
            validate_provenance(item)
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, key)
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            prior = await self._replay(session, issuer, subject, key, fp)
            if prior is not None:
                assert isinstance(prior, MemoryRecord)
                return prior
            now = datetime.now(UTC)
            row.status = MemoryLifecycleStatus.ACTIVE.value
            row.dormant_at = None
            row.reinforced_at = now
            row.updated_at = now
            row.version += 1
            for item in provenance:
                existing_provenance = await session.get(MemoryProvenanceRow, item.id)
                if existing_provenance is not None:
                    if existing_provenance.memory_id != memory_id:
                        raise MemoryValidationError("provenance is already bound to another memory")
                    continue
                session.add(MemoryProvenanceRow(id=item.id, memory_id=memory_id, source_type=item.source_type, source_id=item.source_id, conversation_id=item.conversation_id, run_id=item.run_id, message_id=item.message_id, evidence_digest=item.evidence_digest, evidence=item.evidence, observed_at=item.observed_at, created_at=now))
            self._stage_idempotency(session, issuer, subject, key, fp, memory_id)
        return await self.get_memory(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))

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
            await self._stage_missing_embedding(session, issuer, subject, memory_id, revision_id)
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
            candidate_ids = (await session.execute(text(
                "SELECT id FROM memory_candidates WHERE principal_issuer = :issuer "
                "AND principal_subject = :subject AND (related_memory_id = :memory_id OR memory_id = :memory_id)"
            ), {"issuer": issuer, "subject": subject, "memory_id": memory_id})).scalars().all()
            job_ids = (await session.execute(text(
                "SELECT id FROM memory_processing_jobs WHERE principal_issuer = :issuer AND principal_subject = :subject AND (memory_id = :memory_id OR id IN (SELECT job_id FROM memory_candidates WHERE principal_issuer = :issuer AND principal_subject = :subject AND (related_memory_id = :memory_id OR memory_id = :memory_id)) OR id IN (SELECT job_id FROM memory_action_outcomes WHERE principal_issuer = :issuer AND principal_subject = :subject AND memory_id = :memory_id) OR EXISTS (SELECT 1 FROM memory_command_idempotency AS idem WHERE idem.principal_issuer = :issuer AND idem.principal_subject = :subject AND idem.memory_id = :memory_id AND (idem.idempotency_key = 'memory-job:' || memory_processing_jobs.id::text OR idem.idempotency_key IN (SELECT 'memory-action:' || c.id::text FROM memory_candidates AS c WHERE c.job_id = memory_processing_jobs.id AND c.principal_issuer = :issuer AND c.principal_subject = :subject))))"
            ), {"issuer": issuer, "subject": subject, "memory_id": memory_id})).scalars().all()
            # Derive every processing link from the memory identity, including
            # rows written in a crash window where candidate linkage was not
            # yet persisted.  All rows are owner constrained.
            await session.execute(text(
                "UPDATE memory_processing_jobs SET status = 'failed', last_error_class = 'purged', "
                "lease_id = NULL, lease_until = NULL, user_message_ids = '[]'::jsonb, "
                "assistant_message_ids = '[]'::jsonb, evidence_digest = NULL, memory_id = NULL "
                "WHERE principal_issuer = :issuer "
                "AND principal_subject = :subject AND (memory_id = :memory_id OR id IN "
                "(SELECT job_id FROM memory_candidates WHERE principal_issuer = :issuer AND principal_subject = :subject AND (related_memory_id = :memory_id OR memory_id = :memory_id)) "
                "OR id IN (SELECT job_id FROM memory_action_outcomes WHERE principal_issuer = :issuer AND principal_subject = :subject AND memory_id = :memory_id) "
                "OR EXISTS (SELECT 1 FROM memory_command_idempotency AS idem WHERE idem.principal_issuer = :issuer AND idem.principal_subject = :subject AND idem.memory_id = :memory_id AND (idem.idempotency_key = 'memory-job:' || memory_processing_jobs.id::text OR idem.idempotency_key IN (SELECT 'memory-action:' || c.id::text FROM memory_candidates AS c WHERE c.job_id = memory_processing_jobs.id AND c.principal_issuer = :issuer AND c.principal_subject = :subject))))"
            ), {"issuer": issuer, "subject": subject, "memory_id": memory_id})
            # The accepted-action link is also derivable from the deterministic
            # create idempotency key, covering a crash between memory creation
            # and candidate/job-link persistence.
            await session.execute(text(
                "UPDATE memory_processing_jobs AS jobs SET status = 'failed', "
                "last_error_class = 'purged', lease_id = NULL, lease_until = NULL, "
                "user_message_ids = '[]'::jsonb, assistant_message_ids = '[]'::jsonb, "
                "evidence_digest = NULL, memory_id = NULL "
                "FROM memory_command_idempotency AS idem "
                "WHERE idem.principal_issuer = :issuer "
                "AND idem.principal_subject = :subject "
                "AND idem.memory_id = :memory_id "
                "AND idem.idempotency_key = 'memory-job:' || jobs.id::text "
                "AND jobs.principal_issuer = :issuer "
                "AND jobs.principal_subject = :subject"
            ), {"issuer": issuer, "subject": subject, "memory_id": memory_id})
            if candidate_ids or job_ids:
                await session.execute(text("DELETE FROM memory_action_outcomes WHERE principal_issuer = :issuer AND principal_subject = :subject AND (candidate_id = ANY(:candidate_ids) OR job_id = ANY(:job_ids) OR memory_id = :memory_id)"), {"issuer": issuer, "subject": subject, "candidate_ids": candidate_ids or [UUID(int=0)], "job_ids": job_ids or [UUID(int=0)], "memory_id": memory_id})
            else:
                await session.execute(text("DELETE FROM memory_action_outcomes WHERE principal_issuer = :issuer AND principal_subject = :subject AND memory_id = :memory_id"), {"issuer": issuer, "subject": subject, "memory_id": memory_id})
            await session.execute(text(
                "DELETE FROM memory_candidates WHERE principal_issuer = :issuer "
                "AND principal_subject = :subject AND (related_memory_id = :memory_id OR memory_id = :memory_id OR job_id = ANY(:job_ids))"
            ), {"issuer": issuer, "subject": subject, "memory_id": memory_id, "job_ids": job_ids or [UUID(int=0)]})
            await session.execute(text("DELETE FROM memory_embedding_jobs WHERE principal_issuer = :issuer AND principal_subject = :subject AND memory_id = :memory_id"), {"issuer": issuer, "subject": subject, "memory_id": memory_id})
            await session.execute(delete(MemoryRevisionRow).where(MemoryRevisionRow.memory_id == memory_id))
            await session.execute(
                MemoryIdempotencyRow.__table__.update()  # type: ignore[reportAttributeAccessIssue]
                .where(MemoryIdempotencyRow.memory_id == memory_id)
                # Retain only a content-free replay tombstone.  The actual
                # purged memory id must not remain discoverable through the
                # idempotency table, while the key/fingerprint still fences
                # replay of the original operation.
                .values(tombstone=True, audit_id=None, memory_id=UUID(int=0))
            )
            await session.delete(row)
            audit_id = uuid4()
            audit_row = MemoryPurgeAuditRow(id=audit_id, memory_id=memory_id, principal_issuer=issuer, principal_subject=subject, created_at=datetime.now(UTC))
            session.add(audit_row)
            await session.flush()
            await session.execute(text(
                "INSERT INTO memory_purge_fences(principal_issuer, principal_subject, memory_id) "
                "VALUES (:issuer, :subject, :memory_id) ON CONFLICT DO NOTHING"
            ), {"issuer": issuer, "subject": subject, "memory_id": memory_id})
            await session.refresh(audit_row)
            audit_created_at = audit_row.created_at or datetime.now(UTC)
            if key:
                # Keep the purge receipt replayable, but do not preserve the
                # purged memory identity in command metadata.
                session.add(MemoryIdempotencyRow(principal_issuer=issuer, principal_subject=subject, idempotency_key=str(key), fingerprint=fp, memory_id=UUID(int=0), audit_id=audit_id, tombstone=False))
        return MemoryAuditRecord(audit_id, issuer, subject, memory_id, "purge", audit_created_at)

    async def register_embedding_generation(self, issuer: str, subject: str, **kwargs: object) -> MemoryEmbeddingGeneration:
        generation = int(kwargs["generation"])
        model_id = str(kwargs["model_id"])
        dimension = int(kwargs["dimension"])
        model_digest = kwargs.get("model_digest")
        if not model_id or dimension < 1:
            raise ValueError("embedding model and dimension are required")
        if model_digest is not None and (not isinstance(model_digest, str) or len(model_digest) != 64 or not all(char in "0123456789abcdef" for char in model_digest)):
            raise ValueError("embedding model digest must be sha256")
        item = MemoryEmbeddingGeneration(uuid4(), generation, model_id, kwargs.get("model_revision"), dimension, "building", datetime.now(UTC), None, str(model_digest) if model_digest else None, issuer, subject)
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, f"embedding-generation:{generation}")
            existing = (await session.execute(
                select(MemoryEmbeddingGenerationRow.id).where(
                    MemoryEmbeddingGenerationRow.generation == generation,
                    MemoryEmbeddingGenerationRow.principal_issuer == issuer,
                    MemoryEmbeddingGenerationRow.principal_subject == subject,
                )
            )).scalar_one_or_none()
            if existing is not None:
                raise MemoryValidationError("embedding generation number already exists")
            session.add(MemoryEmbeddingGenerationRow(id=item.id, generation=item.generation, model_id=item.model_id, model_revision=item.model_revision, model_digest=item.model_digest, dimension=item.dimension, status=item.status, created_at=item.created_at, principal_issuer=issuer, principal_subject=subject))
        return item

    async def activate_embedding_generation(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration:
        async with self.sessions() as session, session.begin():
            row = await session.get(MemoryEmbeddingGenerationRow, generation_id, with_for_update=True)
            if row is None:
                raise MemoryNotFound("embedding generation not found")
            if row.principal_issuer != issuer or row.principal_subject != subject:
                raise MemoryNotFound("embedding generation not found")
            if row.status != "building":
                raise MemoryValidationError("only a building embedding generation can be activated")
            retained = (await session.execute(select(func.count()).select_from(MemoryRevisionRow).join(MemoryRow, MemoryRow.id == MemoryRevisionRow.memory_id).where(
                MemoryRow.principal_issuer == issuer, MemoryRow.principal_subject == subject,
            ))).scalar_one()
            embedded = (await session.execute(select(func.count(func.distinct(MemoryEmbeddingRow.revision_id))).select_from(MemoryEmbeddingRow).join(MemoryRevisionRow, MemoryRevisionRow.id == MemoryEmbeddingRow.revision_id).join(MemoryRow, MemoryRow.id == MemoryRevisionRow.memory_id).where(
                MemoryRow.principal_issuer == issuer, MemoryRow.principal_subject == subject,
                MemoryEmbeddingRow.principal_issuer == issuer, MemoryEmbeddingRow.principal_subject == subject,
                MemoryEmbeddingRow.generation_id == generation_id,
            ))).scalar_one()
            if retained != embedded:
                raise MemoryValidationError("embedding generation is incomplete")
            configuration = await session.get(
                MemoryModelConfigurationRow, (issuer, subject), with_for_update=True
            )
            if configuration is not None and configuration.embedding_generation != generation_id and (
                row.model_id != configuration.embedding_model_id
                or row.model_revision != configuration.embedding_model_revision
            ):
                raise MemoryValidationError("embedding generation does not match configured target")
            previous_generation = configuration.embedding_generation if configuration else None
            if previous_generation is not None and previous_generation != generation_id:
                previous = await session.get(MemoryEmbeddingGenerationRow, previous_generation, with_for_update=True)
                if previous is not None and previous.principal_issuer == issuer and previous.principal_subject == subject:
                    previous.status = "retired"
            row.status, row.activated_at = "active", datetime.now(UTC)
            if configuration is not None:
                configuration.embedding_generation = generation_id
                configuration.version += 1
                configuration.updated_at = datetime.now(UTC)
            return MemoryEmbeddingGeneration(row.id, row.generation, row.model_id, row.model_revision, row.dimension, row.status, row.created_at or datetime.now(UTC), row.activated_at, row.model_digest, row.principal_issuer, row.principal_subject)

    async def mark_embedding_generation_failed(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration:
        async with self.sessions() as session, session.begin():
            row = await session.get(MemoryEmbeddingGenerationRow, generation_id, with_for_update=True)
            if row is None or row.principal_issuer != issuer or row.principal_subject != subject:
                raise MemoryNotFound("embedding generation not found")
            if row.status != "building":
                raise MemoryValidationError("only a building embedding generation can fail")
            row.status = "failed"
            return MemoryEmbeddingGeneration(row.id, row.generation, row.model_id, row.model_revision, row.dimension, row.status, row.created_at or datetime.now(UTC), row.activated_at, row.model_digest, row.principal_issuer, row.principal_subject)

    async def attach_embedding(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        async with self.sessions() as session, session.begin():
            await self._assert_not_fenced(session, issuer, subject, memory_id)
            row = await self._owned(session, issuer, subject, memory_id, lock=True, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))
            generation_id = kwargs["generation_id"]
            generation = await session.get(MemoryEmbeddingGenerationRow, generation_id)
            if generation is None:
                raise MemoryNotFound("embedding generation not found")
            if generation.principal_issuer != issuer or generation.principal_subject != subject:
                raise MemoryNotFound("embedding generation not found")
            revision_id = kwargs.get("revision_id", row.current_revision_id)
            revision = (await session.execute(select(MemoryRevisionRow).where(MemoryRevisionRow.id == revision_id, MemoryRevisionRow.memory_id == memory_id))).scalar_one_or_none()
            if revision is None:
                raise MemoryNotFound("memory revision not found")
            if revision.memory_id != row.id:
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
                    MemoryEmbeddingRow.principal_issuer == issuer,
                    MemoryEmbeddingRow.principal_subject == subject,
                )
            )).scalar_one_or_none()
            if duplicate is not None:
                raise ValueError("embedding already exists for revision and generation")
            session.add(MemoryEmbeddingRow(id=uuid4(), revision_id=revision_id, generation=generation.generation, generation_id=generation.id, principal_issuer=issuer, principal_subject=subject, model_id=generation.model_id, model_revision=generation.model_revision, dimension=generation.dimension, digest=digest, vector=vector, created_at=datetime.now(UTC)))
        return await self.get_memory(issuer, subject, memory_id, scope_type=kwargs.get("scope_type"), agent_profile_id=kwargs.get("agent_profile_id"))

    async def save_model_configuration(self, issuer: str, subject: str, configuration: MemoryModelConfiguration, **kwargs: object) -> MemoryModelConfiguration:
        if configuration.issuer != issuer or configuration.subject != subject:
            raise MemoryScopeAuthorizationRequired("model configuration owner mismatch")
        async with self.sessions() as session, session.begin():
            row = await session.get(MemoryModelConfigurationRow, (issuer, subject), with_for_update=True)
            expected = kwargs.get("expected_version")
            if row is not None and expected is not None and row.version != int(expected):
                raise MemoryVersionConflict("model configuration version conflict")
            selected_generation = configuration.embedding_generation
            if configuration.embedding_generation is not None:
                selected = await session.get(MemoryEmbeddingGenerationRow, configuration.embedding_generation)
                if selected is None or selected.principal_issuer != issuer or selected.principal_subject != subject:
                    raise MemoryNotFound("embedding generation not found")
            # A model change creates a discoverable building generation while
            # leaving the currently selected generation active until a full
            # reindex atomically activates its replacement.
            if row is not None and (
                row.embedding_model_id != configuration.embedding_model_id
                or row.embedding_model_revision != configuration.embedding_model_revision
            ):
                # Only the newest configured target is resumable. Older
                # building generations remain as audit metadata but cannot
                # starve the current cutover or be activated later.
                await session.execute(update(MemoryEmbeddingGenerationRow).where(
                    MemoryEmbeddingGenerationRow.principal_issuer == issuer,
                    MemoryEmbeddingGenerationRow.principal_subject == subject,
                    MemoryEmbeddingGenerationRow.status == "building",
                ).values(status="retired"))
                base = await session.get(MemoryEmbeddingGenerationRow, row.embedding_generation) if row.embedding_generation else None
                dimension = int(kwargs.get("dimension", base.dimension if base is not None else 0))
                if dimension > 0:
                    next_generation = (await session.execute(select(func.max(MemoryEmbeddingGenerationRow.generation)).where(
                        MemoryEmbeddingGenerationRow.principal_issuer == issuer,
                        MemoryEmbeddingGenerationRow.principal_subject == subject,
                    ))).scalar_one() or 0
                    building = MemoryEmbeddingGenerationRow(
                        id=uuid4(), generation=int(next_generation) + 1,
                        model_id=configuration.embedding_model_id,
                        model_revision=configuration.embedding_model_revision,
                        model_digest=kwargs.get("model_digest"), dimension=dimension,
                        status="building", principal_issuer=issuer, principal_subject=subject,
                        created_at=datetime.now(UTC),
                    )
                    session.add(building)
                    selected_generation = row.embedding_generation
            if row is None:
                row = MemoryModelConfigurationRow(principal_issuer=issuer, principal_subject=subject, extraction_model_id=configuration.extraction_model_id, extraction_model_revision=configuration.extraction_model_revision, embedding_model_id=configuration.embedding_model_id, embedding_model_revision=configuration.embedding_model_revision, embedding_generation=configuration.embedding_generation, version=configuration.version, updated_at=datetime.now(UTC))
                session.add(row)
            else:
                row.extraction_model_id = configuration.extraction_model_id
                row.extraction_model_revision = configuration.extraction_model_revision
                row.embedding_model_id = configuration.embedding_model_id
                row.embedding_model_revision = configuration.embedding_model_revision
                row.embedding_generation = selected_generation
                row.version += 1
                row.updated_at = datetime.now(UTC)
                configuration = MemoryModelConfiguration(issuer, subject, configuration.extraction_model_id, configuration.embedding_model_id, configuration.extraction_model_revision, configuration.embedding_model_revision, selected_generation, row.version)
        return configuration

    async def get_model_configuration(self, issuer: str, subject: str) -> MemoryModelConfiguration:
        async with self.sessions() as session:
            row = await session.get(MemoryModelConfigurationRow, (issuer, subject))
            if row is None:
                raise MemoryNotFound("memory model configuration not found")
            return MemoryModelConfiguration(issuer, subject, row.extraction_model_id, row.embedding_model_id, row.extraction_model_revision, row.embedding_model_revision, row.embedding_generation, row.version)

    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob:
        async with self.sessions() as session:
            query = select(MemoryProcessingJobRow).where(
                MemoryProcessingJobRow.id == job_id,
                MemoryProcessingJobRow.principal_issuer == issuer,
                MemoryProcessingJobRow.principal_subject == subject,
            )
            row = (await session.execute(query)).scalar_one_or_none()
            if row is None:
                raise MemoryNotFound("memory processing job not found")
            return MemoryProcessingJob(
                row.id, row.principal_issuer, row.principal_subject, row.run_id,
                row.conversation_id, ProcessingJobStatus(row.status), row.attempt_count,
                row.available_at, row.correlation_id, row.causation_id,
                row.last_error_class, row.agent_revision_id,
                tuple(UUID(item) for item in row.user_message_ids or []),
                tuple(UUID(item) for item in row.assistant_message_ids or []), row.evidence_digest,
                row.lease_id, row.lease_until, agent_profile_id=row.agent_profile_id,
                memory_id=row.memory_id,
            )

    async def enqueue_processing_job(self, job: MemoryProcessingJob) -> MemoryProcessingJob:
        existing_job_id: UUID | None = None
        async with self.sessions() as session, session.begin():
            if job.memory_id is not None:
                await self._assert_not_fenced(session, job.issuer, job.subject, job.memory_id)
            existing = (await session.execute(select(MemoryProcessingJobRow).where(
                MemoryProcessingJobRow.principal_issuer == job.issuer,
                MemoryProcessingJobRow.principal_subject == job.subject,
                MemoryProcessingJobRow.run_id == job.run_id,
            ))).scalar_one_or_none()
            if existing is not None:
                existing_job_id = existing.id
            else:
                session.add(MemoryProcessingJobRow(
                    id=job.id, principal_issuer=job.issuer, principal_subject=job.subject,
                    run_id=job.run_id, conversation_id=job.conversation_id,
                    causation_id=job.causation_id, correlation_id=job.correlation_id,
                    agent_revision_id=job.agent_revision_id,
                    agent_profile_id=job.agent_profile_id,
                    user_message_ids=[str(item) for item in job.user_message_ids],
                    assistant_message_ids=[str(item) for item in job.assistant_message_ids],
                    evidence_digest=job.evidence_digest, status=job.status.value,
                    attempt_count=job.attempt_count, available_at=job.available_at,
                    last_error_class=job.last_error_class, memory_id=job.memory_id,
                ))
        if existing_job_id is not None:
            return await self.get_processing_job(existing_job_id, job.issuer, job.subject)
        return job

    async def claim_processing_job(
        self, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryProcessingJob | None:
        now = self._now()
        lease_id = uuid4()
        async with self.sessions() as session, session.begin():
            row = (await session.execute(
                select(MemoryProcessingJobRow).where(
                    MemoryProcessingJobRow.principal_issuer == issuer,
                    MemoryProcessingJobRow.principal_subject == subject,
                    MemoryProcessingJobRow.available_at <= now,
                    ((MemoryProcessingJobRow.status.in_(("queued", "retryable"))) | ((MemoryProcessingJobRow.status == "running") & (MemoryProcessingJobRow.lease_until < now))),
                ).order_by(MemoryProcessingJobRow.created_at).limit(1).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if row is None:
                return None
            row.status = "running"
            row.attempt_count += 1
            row.lease_id = lease_id
            row.lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
            return MemoryProcessingJob(
                row.id, row.principal_issuer, row.principal_subject, row.run_id, row.conversation_id,
                ProcessingJobStatus.RUNNING, row.attempt_count, row.available_at, row.correlation_id,
                row.causation_id, row.last_error_class, row.agent_revision_id,
                tuple(UUID(item) for item in row.user_message_ids or []),
                tuple(UUID(item) for item in row.assistant_message_ids or []), row.evidence_digest,
                row.lease_id, row.lease_until, agent_profile_id=row.agent_profile_id,
                memory_id=row.memory_id,
            )

    async def claim_processing_job_by_id(
        self, job_id: UUID, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryProcessingJob | None:
        """Claim exactly the delivered owner-scoped job identifier."""

        now = self._now()
        lease_id = uuid4()
        async with self.sessions() as session, session.begin():
            row = (await session.execute(
                select(MemoryProcessingJobRow).where(
                    MemoryProcessingJobRow.id == job_id,
                    MemoryProcessingJobRow.principal_issuer == issuer,
                    MemoryProcessingJobRow.principal_subject == subject,
                    MemoryProcessingJobRow.available_at <= now,
                    (
                        MemoryProcessingJobRow.status.in_(("queued", "retryable"))
                        | (
                            (MemoryProcessingJobRow.status == "running")
                            & (MemoryProcessingJobRow.lease_until < now)
                        )
                    ),
                ).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if row is None:
                return None
            row.status = "running"
            row.attempt_count += 1
            row.lease_id = lease_id
            row.lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
            return MemoryProcessingJob(
                row.id, row.principal_issuer, row.principal_subject, row.run_id,
                row.conversation_id, ProcessingJobStatus.RUNNING, row.attempt_count,
                row.available_at, row.correlation_id, row.causation_id,
                row.last_error_class, row.agent_revision_id,
                tuple(UUID(item) for item in row.user_message_ids or []),
                tuple(UUID(item) for item in row.assistant_message_ids or []), row.evidence_digest,
                row.lease_id, row.lease_until, agent_profile_id=row.agent_profile_id,
            )

    async def get_candidate_for_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryCandidate | None:
        async with self.sessions() as session:
            row = (await session.execute(
                select(MemoryCandidateRow).where(
                    MemoryCandidateRow.job_id == job_id,
                    MemoryCandidateRow.principal_issuer == issuer,
                    MemoryCandidateRow.principal_subject == subject,
                ).order_by(MemoryCandidateRow.created_at.desc()).limit(1)
            )).scalar_one_or_none()
            if row is None:
                return None
            scope = (
                MemoryScope(MemoryScopeType(row.scope_type), row.agent_profile_id)
                if row.scope_type is not None else None
            )
            return MemoryCandidate(
                row.id, row.job_id, row.principal_issuer, row.principal_subject,
                MemoryAction(row.action), row.content,
                MemoryKind(row.kind) if row.kind else None, scope, row.confidence,
                row.importance, row.half_life_days, row.valid_to,
                MemorySensitivity(row.sensitivity),
                tuple(UUID(item) for item in row.grounded_message_ids or []),
                row.related_memory_id, CandidateState(row.state), row.decision_reason, row.memory_id,
            )

    async def settle_processing_job(
        self, job_id: UUID, lease_id: UUID, *, issuer: str, subject: str, retryable: bool = False,
        error_class: str | None = None
    ) -> MemoryProcessingJob:
        async with self.sessions() as session, session.begin():
            row = await session.get(MemoryProcessingJobRow, job_id, with_for_update=True)
            if (
                row is None or row.lease_id != lease_id or row.status != "running"
                or row.principal_issuer != issuer
                or row.principal_subject != subject
            ):
                raise MemoryValidationError("processing job lease is stale")
            row.status = "retryable" if retryable else "completed"
            row.last_error_class = error_class
            row.completed_at = None if retryable else datetime.now(UTC)
            if retryable:
                row.available_at = self._now() + timedelta(seconds=min(3600, 2 ** min(row.attempt_count, 10)))
            row.lease_id = None
            row.lease_until = None
            return MemoryProcessingJob(
                row.id, row.principal_issuer, row.principal_subject, row.run_id, row.conversation_id,
                ProcessingJobStatus.RETRYABLE if retryable else ProcessingJobStatus.COMPLETED,
                row.attempt_count, row.available_at, row.correlation_id, row.causation_id,
                row.last_error_class, row.agent_revision_id,
                tuple(UUID(item) for item in row.user_message_ids or []),
                tuple(UUID(item) for item in row.assistant_message_ids or []), row.evidence_digest,
                agent_profile_id=row.agent_profile_id, memory_id=row.memory_id,
            )

    async def link_processing_job_memory(
        self, job_id: UUID, issuer: str, subject: str, memory_id: UUID
    ) -> None:
        """Record the accepted memory link before any later retry can run."""

        async with self.sessions() as session, session.begin():
            job = (await session.execute(
                select(MemoryProcessingJobRow).where(
                    MemoryProcessingJobRow.id == job_id,
                    MemoryProcessingJobRow.principal_issuer == issuer,
                    MemoryProcessingJobRow.principal_subject == subject,
                ).with_for_update()
            )).scalar_one_or_none()
            if job is None:
                raise MemoryNotFound("memory processing job not found")
            memory = (await session.execute(
                select(MemoryRow.id).where(
                    MemoryRow.id == memory_id,
                    MemoryRow.principal_issuer == issuer,
                    MemoryRow.principal_subject == subject,
                )
            )).scalar_one_or_none()
            if memory is None:
                raise MemoryNotFound("memory not found")
            await self._assert_not_fenced(session, issuer, subject, memory_id)
            job.memory_id = memory_id

    async def queue_embedding_job(
        self, issuer: str, subject: str, *, memory_id: UUID, revision_id: UUID, generation_id: UUID
    ) -> MemoryEmbeddingJob:
        item = MemoryEmbeddingJob(
            uuid5(MEMORY_ID_NAMESPACE, f"embedding-job:{issuer}:{subject}:{revision_id}:{generation_id}"),
            issuer, subject, memory_id, revision_id, generation_id,
        )
        async with self.sessions() as session, session.begin():
            await self._assert_not_fenced(session, issuer, subject, memory_id)
            owned_memory = (await session.execute(select(MemoryRow.id).where(
                MemoryRow.id == memory_id,
                MemoryRow.principal_issuer == issuer,
                MemoryRow.principal_subject == subject,
            ))).scalar_one_or_none()
            if owned_memory is None:
                raise MemoryNotFound("memory not found")
            owned_revision = (await session.execute(select(MemoryRevisionRow.id).where(
                MemoryRevisionRow.id == revision_id,
                MemoryRevisionRow.memory_id == memory_id,
            ))).scalar_one_or_none()
            if owned_revision is None:
                raise MemoryNotFound("memory revision not found")
            generation = (await session.execute(select(MemoryEmbeddingGenerationRow).where(
                MemoryEmbeddingGenerationRow.id == generation_id,
                MemoryEmbeddingGenerationRow.principal_issuer == issuer,
                MemoryEmbeddingGenerationRow.principal_subject == subject,
            ))).scalar_one_or_none()
            if generation is None:
                raise MemoryNotFound("embedding generation not found")
            existing = (await session.execute(select(MemoryEmbeddingJobRow).where(
                MemoryEmbeddingJobRow.principal_issuer == issuer,
                MemoryEmbeddingJobRow.principal_subject == subject,
                MemoryEmbeddingJobRow.revision_id == revision_id,
                MemoryEmbeddingJobRow.generation_id == generation_id,
            ))).scalar_one_or_none()
            if existing is not None:
                # Queue/replay is not a lease capability.  A row may already
                # be running under another worker; callers must perform an
                # exact claim before invoking a provider or settling it.
                return MemoryEmbeddingJob(existing.id, issuer, subject, existing.memory_id, existing.revision_id, existing.generation_id, ProcessingJobStatus(existing.status), existing.attempt_count, existing.available_at, existing.last_error_class, None, None)
            session.add(MemoryEmbeddingJobRow(
                id=item.id, principal_issuer=issuer, principal_subject=subject,
                memory_id=memory_id, revision_id=revision_id, generation_id=generation_id,
                status=item.status.value, attempt_count=0, available_at=item.available_at,
            ))
        return item

    async def persist_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        async with self.sessions() as session, session.begin():
            job_owner = (await session.execute(select(MemoryProcessingJobRow.principal_issuer, MemoryProcessingJobRow.principal_subject).where(MemoryProcessingJobRow.id == candidate.job_id))).one_or_none()
            if job_owner is None or tuple(job_owner) != (candidate.issuer, candidate.subject):
                raise MemoryNotFound("memory processing job not found")
            existing = await session.get(MemoryCandidateRow, candidate.id)
            if existing is None:
                session.add(MemoryCandidateRow(
                    id=candidate.id, job_id=candidate.job_id, principal_issuer=candidate.issuer,
                    principal_subject=candidate.subject, action=candidate.action.value,
                    content=candidate.content, kind=candidate.kind.value if candidate.kind else None,
                    scope_type=candidate.scope.type.value if candidate.scope else None,
                    agent_profile_id=candidate.scope.agent_profile_id if candidate.scope else None,
                    confidence=candidate.confidence, importance=candidate.importance,
                    half_life_days=candidate.half_life_days, valid_to=candidate.valid_to,
                    sensitivity=candidate.sensitivity.value,
                    grounded_message_ids=[str(item) for item in candidate.grounded_message_ids],
                    related_memory_id=candidate.related_memory_id, state=candidate.state.value,
                    decision_reason=candidate.decision_reason, memory_id=candidate.memory_id,
                ))
            else:
                if existing.principal_issuer != candidate.issuer or existing.principal_subject != candidate.subject or existing.job_id != candidate.job_id:
                    raise MemoryNotFound("memory candidate not found")
                existing.action = candidate.action.value
                existing.content = candidate.content
                existing.kind = candidate.kind.value if candidate.kind else None
                existing.scope_type = candidate.scope.type.value if candidate.scope else None
                existing.agent_profile_id = candidate.scope.agent_profile_id if candidate.scope else None
                existing.confidence = candidate.confidence
                existing.importance = candidate.importance
                existing.half_life_days = candidate.half_life_days
                existing.valid_to = candidate.valid_to
                existing.sensitivity = candidate.sensitivity.value
                existing.grounded_message_ids = [str(item) for item in candidate.grounded_message_ids]
                existing.related_memory_id = candidate.related_memory_id
                existing.memory_id = candidate.memory_id
                existing.state = candidate.state.value
                existing.decision_reason = candidate.decision_reason
        return candidate

    async def record_action_outcome(
        self, *, candidate_id: UUID | None, job_id: UUID, issuer: str, subject: str,
        action: str, outcome: str, memory_id: UUID | None = None,
        revision_id: UUID | None = None, error_class: str | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            job_owner = (await session.execute(select(MemoryProcessingJobRow.principal_issuer, MemoryProcessingJobRow.principal_subject).where(MemoryProcessingJobRow.id == job_id))).one_or_none()
            if job_owner is None or tuple(job_owner) != (issuer, subject):
                raise MemoryNotFound("memory processing job not found")
            if candidate_id is not None:
                candidate_owner = (await session.execute(select(MemoryCandidateRow.principal_issuer, MemoryCandidateRow.principal_subject).where(MemoryCandidateRow.id == candidate_id))).one_or_none()
                if candidate_owner is None or tuple(candidate_owner) != (issuer, subject):
                    raise MemoryNotFound("memory candidate not found")
            existing = (await session.execute(
                select(MemoryActionOutcomeRow).where(
                    MemoryActionOutcomeRow.job_id == job_id,
                    MemoryActionOutcomeRow.principal_issuer == issuer,
                    MemoryActionOutcomeRow.principal_subject == subject,
                    MemoryActionOutcomeRow.action == action,
                ).limit(1)
            )).scalar_one_or_none()
            if existing is not None:
                existing.candidate_id = candidate_id
                existing.outcome = outcome
                existing.memory_id = memory_id
                existing.revision_id = revision_id
                existing.error_class = error_class
                return
            session.add(MemoryActionOutcomeRow(
                id=uuid5(MEMORY_ID_NAMESPACE, f"outcome:{job_id}:{action}"), candidate_id=candidate_id, job_id=job_id,
                principal_issuer=issuer, principal_subject=subject, action=action,
                outcome=outcome, memory_id=memory_id, revision_id=revision_id,
                error_class=error_class,
            ))

    async def settle_embedding_job(
        self, job_id: UUID, *, issuer: str, subject: str,
        lease_id: UUID, retryable: bool = False, failed: bool = False,
        error_class: str | None = None
    ) -> MemoryEmbeddingJob:
        async with self.sessions() as session, session.begin():
            row = await session.get(MemoryEmbeddingJobRow, job_id, with_for_update=True)
            if row is None or row.principal_issuer != issuer or row.principal_subject != subject:
                raise MemoryNotFound("embedding job not found")
            if row.status != "running" or row.lease_id != lease_id:
                raise MemoryValidationError("embedding job lease is stale")
            await self._assert_not_fenced(session, issuer, subject, row.memory_id)
            # Provider outages remain recoverable regardless of attempt count;
            # only explicit purge failures are terminal.  This prevents a
            # durable accepted revision from becoming permanently unembedded.
            provider_retry = failed and error_class == "provider"
            row.status = "failed" if failed and not provider_retry else ("retryable" if retryable or provider_retry else "completed")
            row.last_error_class = error_class
            if retryable or provider_retry:
                row.available_at = self._now() + timedelta(seconds=min(3600, 2 ** min(row.attempt_count, 10)))
            row.lease_id = None
            row.lease_until = None
            return MemoryEmbeddingJob(row.id, row.principal_issuer, row.principal_subject, row.memory_id, row.revision_id, row.generation_id, ProcessingJobStatus(row.status), row.attempt_count, row.available_at, row.last_error_class, row.lease_id, row.lease_until)

    async def claim_embedding_job(
        self, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryEmbeddingJob | None:
        """Claim one owner-scoped embedding retry with an expiring lease."""

        now = self._now()
        lease_id = uuid4()
        async with self.sessions() as session, session.begin():
            row = (await session.execute(
                select(MemoryEmbeddingJobRow).join(
                    MemoryEmbeddingGenerationRow,
                    MemoryEmbeddingGenerationRow.id == MemoryEmbeddingJobRow.generation_id,
                ).where(
                    MemoryEmbeddingJobRow.principal_issuer == issuer,
                    MemoryEmbeddingJobRow.principal_subject == subject,
                    MemoryEmbeddingGenerationRow.principal_issuer == issuer,
                    MemoryEmbeddingGenerationRow.principal_subject == subject,
                    MemoryEmbeddingGenerationRow.status == "active",
                    MemoryEmbeddingJobRow.available_at <= now,
                    (
                        MemoryEmbeddingJobRow.status.in_(("queued", "retryable"))
                        | ((MemoryEmbeddingJobRow.status == "running") & (MemoryEmbeddingJobRow.lease_until < now))
                    ),
                ).order_by(MemoryEmbeddingJobRow.created_at).limit(1).with_for_update(skip_locked=True)
            )).scalar_one_or_none()
            if row is None:
                return None
            await self._assert_not_fenced(session, issuer, subject, row.memory_id)
            row.status = "running"
            row.attempt_count += 1
            row.lease_id = lease_id
            row.lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
            return MemoryEmbeddingJob(
                row.id, issuer, subject, row.memory_id, row.revision_id,
                row.generation_id, ProcessingJobStatus.RUNNING, row.attempt_count,
                row.available_at, row.last_error_class, row.lease_id, row.lease_until,
            )

    async def claim_embedding_job_by_id(
        self, job_id: UUID, issuer: str, subject: str, *, lease_seconds: float = 60.0
    ) -> MemoryEmbeddingJob | None:
        now = self._now()
        lease_id = uuid4()
        async with self.sessions() as session, session.begin():
            row = (await session.execute(select(MemoryEmbeddingJobRow).where(
                MemoryEmbeddingJobRow.id == job_id,
                MemoryEmbeddingJobRow.principal_issuer == issuer,
                MemoryEmbeddingJobRow.principal_subject == subject,
                MemoryEmbeddingJobRow.available_at <= now,
                (
                    MemoryEmbeddingJobRow.status.in_(("queued", "retryable"))
                    | ((MemoryEmbeddingJobRow.status == "running") & (MemoryEmbeddingJobRow.lease_until < now))
                ),
            ).with_for_update(skip_locked=True))).scalar_one_or_none()
            if row is None:
                return None
            await self._assert_not_fenced(session, issuer, subject, row.memory_id)
            row.status = "running"
            row.attempt_count += 1
            row.lease_id = lease_id
            row.lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
            return MemoryEmbeddingJob(row.id, issuer, subject, row.memory_id, row.revision_id, row.generation_id, ProcessingJobStatus.RUNNING, row.attempt_count, row.available_at, row.last_error_class, row.lease_id, row.lease_until)

    async def claim_embedding_job_for_revision(
        self, issuer: str, subject: str, *, revision_id: UUID, generation_id: UUID,
        lease_seconds: float = 60.0,
    ) -> MemoryEmbeddingJob | None:
        """Claim only the embedding work bound to one revision/generation."""

        now = self._now()
        lease_id = uuid4()
        async with self.sessions() as session, session.begin():
            row = (await session.execute(select(MemoryEmbeddingJobRow).where(
                MemoryEmbeddingJobRow.principal_issuer == issuer,
                MemoryEmbeddingJobRow.principal_subject == subject,
                MemoryEmbeddingJobRow.revision_id == revision_id,
                MemoryEmbeddingJobRow.generation_id == generation_id,
                MemoryEmbeddingJobRow.available_at <= now,
                (
                    MemoryEmbeddingJobRow.status.in_(("queued", "retryable"))
                    | ((MemoryEmbeddingJobRow.status == "running") & (MemoryEmbeddingJobRow.lease_until < now))
                ),
            ).with_for_update(skip_locked=True))).scalar_one_or_none()
            if row is None:
                return None
            await self._assert_not_fenced(session, issuer, subject, row.memory_id)
            row.status = "running"
            row.attempt_count += 1
            row.lease_id = lease_id
            row.lease_until = now + timedelta(seconds=max(1.0, lease_seconds))
            return MemoryEmbeddingJob(
                row.id, issuer, subject, row.memory_id, row.revision_id,
                row.generation_id, ProcessingJobStatus.RUNNING, row.attempt_count,
                row.available_at, row.last_error_class, row.lease_id, row.lease_until,
            )


__all__ = ["SqlMemoryRepository"]
