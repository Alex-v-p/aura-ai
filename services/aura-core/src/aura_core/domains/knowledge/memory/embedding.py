"""Embedding and resumable reindex coordination for memory."""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from datetime import UTC, datetime
from time import monotonic
from uuid import UUID, uuid5

from aura_core.domains.knowledge.memory.contracts import (
    MEMORY_ID_NAMESPACE,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryNotFound,
    MemorySensitivity,
    MemoryValidationError,
    classify_sensitivity,
)
from aura_core.domains.knowledge.memory.ports import MemoryTelemetry
from aura_core.domains.knowledge.memory.repository_ports import MemoryReindexRepository
from aura_core.runtime.models.ports import EmbeddingPort, ProviderTraceContext


class MemoryReindexService:
    """Resumable parallel-generation re-embedding coordinator."""

    def __init__(
        self,
        repository: MemoryReindexRepository,
        embedder: EmbeddingPort,
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
        generation = await self.repository.get_embedding_generation(issuer, subject, generation_id)
        if generation.status != "building":
            # A completed generation is resumable as a no-op; failed/retired
            # generations are not eligible for another activation attempt.
            if generation.status == "active":
                return 0
            raise MemoryValidationError("embedding generation is not resumable")
        try:
            configuration = await self.repository.get_model_configuration(issuer, subject)
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
                claimed_job = await self.repository.claim_embedding_job_for_revision(
                    issuer, subject, revision_id=revision.id, generation_id=generation_id
                )
                if claimed_job is None:
                    await self.repository.queue_embedding_job(
                        issuer,
                        subject,
                        memory_id=record.id,
                        revision_id=revision.id,
                        generation_id=generation_id,
                    )
                    claimed_job = await self.repository.claim_embedding_job_for_revision(
                        issuer, subject, revision_id=revision.id, generation_id=generation_id
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
                    if classify_sensitivity(revision.content) is MemorySensitivity.CREDENTIAL:
                        await self.repository.settle_embedding_job(
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
                        result = await self.embedder.embed(
                            generation.model_id, revision.content, context=context
                        )
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
                        await self.repository.settle_embedding_job(
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
                    await self.repository.settle_embedding_job(
                        claimed_job.id,
                        issuer=issuer,
                        subject=subject,
                        lease_id=claimed_job.lease_id,
                    )
                    processed += 1
                    await self.repository.save_maintenance_state(
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


class MemoryEmbeddingJobCoordinator:
    """Durable queue/claim/settle protocol for primary memory processing."""

    def __init__(self, repository: MemoryReindexRepository) -> None:
        self.repository = repository

    async def claim(
        self,
        issuer: str,
        subject: str,
        *,
        memory_id: UUID,
        revision_id: UUID,
        generation_id: UUID,
    ) -> MemoryEmbeddingJob:
        queued = await self.repository.queue_embedding_job(
            issuer,
            subject,
            memory_id=memory_id,
            revision_id=revision_id,
            generation_id=generation_id,
        )
        claimed = await self.repository.claim_embedding_job_by_id(queued.id, issuer, subject)
        if claimed is None:
            raise MemoryValidationError("embedding work is already claimed or settled")
        if claimed.lease_id is None:
            raise MemoryValidationError("embedding work was not durably claimed")
        if (
            claimed.issuer != issuer
            or claimed.subject != subject
            or claimed.memory_id != memory_id
            or claimed.revision_id != revision_id
            or claimed.generation_id != generation_id
        ):
            raise MemoryValidationError("embedding work does not match accepted revision")
        return claimed

    async def settle(
        self,
        job: MemoryEmbeddingJob,
        *,
        issuer: str,
        subject: str,
        retryable: bool = False,
        failed: bool = False,
        error_class: str | None = None,
    ) -> MemoryEmbeddingJob:
        if job.lease_id is None:
            raise MemoryValidationError("embedding work was not durably claimed")
        return await self.repository.settle_embedding_job(
            job.id,
            issuer=issuer,
            subject=subject,
            lease_id=job.lease_id,
            retryable=retryable,
            failed=failed,
            error_class=error_class,
        )


__all__ = ["MemoryEmbeddingJobCoordinator", "MemoryReindexService"]
