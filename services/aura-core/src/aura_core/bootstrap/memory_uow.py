"""Composition helpers for the knowledge memory boundary."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Awaitable, Callable
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.knowledge.memory import persistence as memory_persistence
from aura_core.domains.knowledge.memory.public import (
    MemoryAuditRecord,
    MemoryEmbeddingGeneration,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryNotFound,
    MemoryPurgeConfirmationRequired,
    MemoryRecord,
    MemoryRepository,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemoryStore,
    MemoryValidationError,
    MemoryVersionConflict,
)
from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository
from aura_core.platform.telemetry import MetadataMetrics, Stopwatch, record_memory_operation

# Importing the mapping module here is intentional: bootstrap is the only
# composition location that registers domain mappings with Core metadata.
_ = memory_persistence


class InstrumentedMemoryRepository:
    """Application-boundary decorator shared by HTTP and worker callers."""

    def __init__(self, inner: MemoryRepository, metrics: MetadataMetrics, dependency: str) -> None:
        self._inner = inner
        self._metrics = metrics
        self._dependency = dependency

    @staticmethod
    def _error_class(exc: Exception) -> str:
        if isinstance(exc, MemoryNotFound):
            return "not_found"
        if isinstance(exc, MemoryScopeAuthorizationRequired):
            return "authorization"
        if isinstance(exc, MemoryIdempotencyConflict):
            return "idempotency"
        if isinstance(exc, MemoryVersionConflict):
            return "conflict"
        if isinstance(exc, (MemoryPurgeConfirmationRequired, MemoryValidationError, ValueError, TypeError)):
            return "validation"
        return "persistence"

    @staticmethod
    def _scope(kwargs: dict[str, object], result: object | None = None) -> str:
        scope = kwargs.get("scope_type")
        if isinstance(scope, MemoryScopeType):
            return scope.value
        if isinstance(scope, str) and scope in {item.value for item in MemoryScopeType}:
            return scope
        body_scope = kwargs.get("scope")
        if isinstance(body_scope, MemoryScope):
            return body_scope.type.value
        filters = kwargs.get("filters")
        if isinstance(filters, MemoryFilters) and filters.scope_type is not None:
            return filters.scope_type.value
        if isinstance(result, MemoryRecord):
            return result.scope.type.value
        return "unknown"

    async def _invoke(
        self,
        operation: str,
        method: Callable[..., Awaitable[object]],
        trace_ids: tuple[str | None, str | None, str | None],
        *args: object,
        **kwargs: object,
    ) -> object:
        timer = Stopwatch()
        trace_id = uuid4().hex
        trace_memory_id, trace_revision_id, trace_generation_id = trace_ids
        try:
            result = await method(*args, **kwargs)
        except Exception as exc:
            record_memory_operation(
                self._metrics, timer, outcome="error", scope_type=self._scope(kwargs),
                lifecycle_status="unknown", dependency=self._dependency, trace_id=trace_id,
                memory_id=trace_memory_id, memory_revision_id=trace_revision_id,
                generation_id=trace_generation_id,
                error_class=self._error_class(exc), operation=operation,
            )
            raise
        result_memory_id = trace_memory_id
        revision_id = trace_revision_id
        generation_id = trace_generation_id
        lifecycle_status = "unknown"
        if isinstance(result, MemoryRecord):
            result_memory_id = trace_memory_id or str(result.id)
            revision_id = revision_id or str(result.current_revision_id)
            lifecycle_status = result.status.value
        elif isinstance(result, MemoryAuditRecord):
            result_memory_id = str(result.memory_id)
        elif isinstance(result, MemoryEmbeddingGeneration):
            generation_id = trace_generation_id or str(result.id)
        record_memory_operation(
            self._metrics, timer, outcome="ok", scope_type=self._scope(kwargs, result),
            lifecycle_status=lifecycle_status, dependency=self._dependency, trace_id=trace_id,
            memory_id=result_memory_id, memory_revision_id=revision_id, operation=operation,
            generation_id=generation_id,
        )
        return result

    async def list_memories(self, issuer: str, subject: str, filters: MemoryFilters) -> list[MemoryRecord]:
        result = await self._invoke("memory.list", self._inner.list_memories, (None, None, None), issuer, subject, filters=filters)
        return result  # type: ignore[return-value]

    async def get_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        result = await self._invoke("memory.get", self._inner.get_memory, (str(memory_id), None, None), issuer, subject, memory_id, **kwargs)
        return result  # type: ignore[return-value]

    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        result = await self._invoke("memory.create", self._inner.create_memory, (None, None, None), issuer, subject, **kwargs)
        return result  # type: ignore[return-value]

    async def revise_memory(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        result = await self._invoke("memory.revise", self._inner.revise_memory, (str(memory_id), None, None), issuer, subject, memory_id, **kwargs)
        return result  # type: ignore[return-value]

    async def set_status(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        result = await self._invoke("memory.status", self._inner.set_status, (str(memory_id), None, None), issuer, subject, memory_id, **kwargs)
        return result  # type: ignore[return-value]

    async def set_pinned(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        result = await self._invoke("memory.pin", self._inner.set_pinned, (str(memory_id), None, None), issuer, subject, memory_id, **kwargs)
        return result  # type: ignore[return-value]

    async def purge(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryAuditRecord:
        result = await self._invoke("memory.purge", self._inner.purge, (str(memory_id), None, None), issuer, subject, memory_id, **kwargs)
        return result  # type: ignore[return-value]

    async def register_embedding_generation(self, issuer: str, subject: str, **kwargs: object) -> MemoryEmbeddingGeneration:
        result = await self._invoke("memory.embedding.register", self._inner.register_embedding_generation, (None, None, None), issuer, subject, **kwargs)
        return result  # type: ignore[return-value]

    async def activate_embedding_generation(self, issuer: str, subject: str, generation_id: UUID) -> MemoryEmbeddingGeneration:
        result = await self._invoke("memory.embedding.activate", self._inner.activate_embedding_generation, (None, None, str(generation_id)), issuer, subject, generation_id)
        return result  # type: ignore[return-value]

    async def attach_embedding(self, issuer: str, subject: str, memory_id: UUID, **kwargs: object) -> MemoryRecord:
        revision_id = kwargs.get("revision_id")
        result = await self._invoke(
            "memory.embedding.attach", self._inner.attach_embedding,
            (str(memory_id), str(revision_id) if isinstance(revision_id, UUID) else None, None),
            issuer, subject, memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]


def memory_repository(
    sessions: async_sessionmaker[AsyncSession] | None, *, testing: bool = False,
    metrics: MetadataMetrics | None = None,
) -> MemoryRepository:
    if testing or sessions is None:
        repository: MemoryRepository = MemoryStore()
        dependency = "memory_store"
    else:
        repository = SqlMemoryRepository(sessions)
        dependency = "postgresql"
    return InstrumentedMemoryRepository(repository, metrics or MetadataMetrics(), dependency)


__all__ = ["memory_repository"]
