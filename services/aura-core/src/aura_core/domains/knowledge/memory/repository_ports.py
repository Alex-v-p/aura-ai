"""Typed, role-oriented repository ports for the memory domain.

The data-contract leaf does not know about persistence. Application services
depend on the smallest role they need, while ``MemoryRepository`` remains the
source-compatible composite for adapters and callers that need the complete
memory boundary.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Protocol
from uuid import UUID

from aura_core.domains.knowledge.memory.contracts import (
    MemoryActivitySnapshot,
    MemoryAuditRecord,
    MemoryCandidate,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryModelConfiguration,
    MemoryProcessingJob,
    MemoryRecord,
)


class MemoryQueryRepository(Protocol):
    """Owner-scoped reads shared by processing and retrieval."""

    async def list_memories(
        self, issuer: str, subject: str, filters: MemoryFilters
    ) -> list[MemoryRecord]: ...

    async def get_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...

    async def get_run_memory_activity(
        self, run_id: UUID, issuer: str, subject: str
    ) -> MemoryActivitySnapshot: ...

    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob: ...

    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration: ...

    async def list_embedding_generations(
        self, issuer: str, subject: str, *, status: str | None = None
    ) -> list[MemoryEmbeddingGeneration]: ...

    async def get_model_configuration(
        self, issuer: str, subject: str
    ) -> MemoryModelConfiguration: ...


class MemoryMutationRepository(Protocol):
    """Owner-scoped durable memory mutations."""

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

    async def attach_embedding(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord: ...

    async def purge(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryAuditRecord: ...


class MemoryEmbeddingRepository(Protocol):
    """Embedding job capabilities used by processing and reindexing."""

    async def claim_embedding_job_for_revision(
        self,
        issuer: str,
        subject: str,
        *,
        revision_id: UUID,
        generation_id: UUID,
        lease_seconds: float = 60.0,
    ) -> MemoryEmbeddingJob | None: ...

    async def claim_embedding_job_by_id(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryEmbeddingJob | None: ...

    async def queue_embedding_job(
        self,
        issuer: str,
        subject: str,
        *,
        memory_id: UUID,
        revision_id: UUID,
        generation_id: UUID,
    ) -> MemoryEmbeddingJob: ...

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


class MemoryMaintenanceRepository(Protocol):
    """Model-generation and maintenance state capabilities."""

    async def register_embedding_generation(
        self, issuer: str, subject: str, **kwargs: object
    ) -> MemoryEmbeddingGeneration: ...

    async def save_model_configuration(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration, **kwargs: object
    ) -> MemoryModelConfiguration: ...

    async def save_maintenance_state(
        self,
        issuer: str,
        subject: str,
        *,
        ran_at: datetime,
        generation: int | None = None,
        generation_id: UUID | None = None,
        cursor: UUID | None = None,
        completed: int = 0,
    ) -> None: ...

    async def activate_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration: ...


class MemoryCandidateRepository(Protocol):
    """Durable candidate and action-outcome capabilities."""

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

    async def persist_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate: ...

    async def link_processing_job_memory(
        self, job_id: UUID, issuer: str, subject: str, memory_id: UUID
    ) -> None: ...

    async def record_action_outcome(
        self,
        *,
        candidate_id: UUID | None,
        job_id: UUID,
        issuer: str,
        subject: str,
        action: str,
        outcome: str,
        memory_id: UUID | None = None,
        revision_id: UUID | None = None,
        error_class: str | None = None,
    ) -> None: ...


class MemoryProcessingRepository(
    MemoryQueryRepository,
    MemoryMutationRepository,
    MemoryEmbeddingRepository,
    MemoryMaintenanceRepository,
    MemoryCandidateRepository,
    Protocol,
):
    """Complete durable capability required by the extraction worker."""

    async def is_processing_command_purged(
        self, issuer: str, subject: str, command_id: UUID
    ) -> bool: ...

    async def enqueue_processing_job(self, job: MemoryProcessingJob) -> MemoryProcessingJob: ...

    async def claim_processing_job_by_id(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob | None: ...

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


class MemoryReindexRepository(
    MemoryQueryRepository,
    MemoryMutationRepository,
    MemoryEmbeddingRepository,
    MemoryMaintenanceRepository,
    Protocol,
):
    """Complete durable capability required by the resumable reindex worker."""


class MemoryReindexCommandRepository(Protocol):
    """Idempotent reindex command receipts used by the model service."""

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


class MemoryRepository(
    MemoryProcessingRepository,
    MemoryReindexRepository,
    MemoryReindexCommandRepository,
    Protocol,
):
    """Source-compatible composite of the complete memory repository port."""

    def set_embedding_queue_boundary(
        self, boundary: Callable[..., Awaitable[object]] | None
    ) -> None: ...


__all__ = [
    "MemoryCandidateRepository",
    "MemoryEmbeddingRepository",
    "MemoryMaintenanceRepository",
    "MemoryMutationRepository",
    "MemoryProcessingRepository",
    "MemoryQueryRepository",
    "MemoryReindexCommandRepository",
    "MemoryReindexRepository",
    "MemoryRepository",
]
