"""Composition helpers for the knowledge memory boundary."""

# ruff: noqa: E501, B009

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.execution.runs.public import Run, RunStatus, SqlRunRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.domains.interaction.agents.public import (
    AgentConfigurationRepository,
    AgentRevision,
    MemoryPolicy,
)
from aura_core.domains.interaction.conversations.public import (
    Conversation,
    Message,
    MessageRole,
    MessageState,
    SqlConversationRepository,
)
from aura_core.domains.knowledge.memory import persistence as memory_persistence
from aura_core.domains.knowledge.memory.public import (
    MEMORY_PROCESSING_TOPIC,
    MemoryActivitySnapshot,
    MemoryAuditRecord,
    MemoryCandidate,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryFilters,
    MemoryIdempotencyConflict,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingCommand,
    MemoryProcessingJob,
    MemoryProcessingService,
    MemoryPurgeConfirmationRequired,
    MemoryRecord,
    MemoryReindexService,
    MemoryRepository,
    MemoryScope,
    MemoryScopeAuthorizationRequired,
    MemoryScopeType,
    MemorySensitivity,
    MemoryStore,
    MemoryTurnEvidence,
    MemoryValidationError,
    MemoryVersionConflict,
    classify_sensitivity,
)
from aura_core.domains.knowledge.memory.recall import (
    MemoryQueryEmbedding,
    MemoryQueryEmbeddingPort,
    MemoryRecallService,
    MemoryRecallTelemetryEvent,
    MemoryRecallTelemetryPort,
)
from aura_core.domains.knowledge.memory.repository import SqlMemoryRepository
from aura_core.platform.outbox import OutboxCommand, make_identifier_command
from aura_core.platform.telemetry import (
    MemoryTraceContext,
    MetadataMetrics,
    Stopwatch,
    memory_trace_context,
    record_memory_operation,
    record_memory_processing,
    record_memory_retrieval,
)
from aura_core.providers.embeddings.ollama.adapter import OllamaEmbeddingAdapter
from aura_core.providers.models.ollama.adapter import OllamaAdapter
from aura_core.runtime.models.ports import ProviderTraceContext

# Importing the mapping module here is intentional: bootstrap is the only
# composition location that registers domain mappings with Core metadata.
_ = memory_persistence

_MEMORY_COMMAND_NAMESPACE = UUID("4c2a5df3-8b07-4a6d-a9ca-3d19f4ee0d4e")


def make_agent_policy_loader(
    agents: AgentConfigurationRepository,
) -> Callable[[str, str, UUID], Awaitable[tuple[AgentRevision, MemoryPolicy]]]:
    """Build the authenticated pinned-agent resolver for worker composition.

    The adapter remains owned by the agent domain; memory only consumes this
    public query seam and never reaches into agent persistence mappings.
    """

    async def resolve(
        issuer: str, subject: str, agent_revision_id: UUID
    ) -> tuple[AgentRevision, MemoryPolicy]:
        revision = agents.resolve_revision_unchecked(agent_revision_id)
        policy = await agents.get_memory_policy(issuer, subject, revision.memory_policy_revision_id)
        return revision, policy

    return resolve


def memory_command_factory(
    conversation: Conversation, run: Run, assistant: Message
) -> OutboxCommand:
    """Build the deterministic, identifier-only completed-turn command.

    The run id is also the durable processing-job id.  This makes a command
    replay self-describing to the worker without putting conversation content
    into the outbox envelope; the worker resolves all evidence from SQL.
    """

    conversation_id = conversation.id
    run_id = run.id
    assistant_id = assistant.id
    agent_revision_id = run.agent_revision_id
    command_id = uuid5(_MEMORY_COMMAND_NAMESPACE, f"command:{run_id}")
    return make_identifier_command(
        MEMORY_PROCESSING_TOPIC,
        command_id,
        run_id,
        conversation_id,
        correlation_id=run.attempt_id or run_id,
        causation_id=run_id,
        identifiers=(
            ("jobId", run_id),
            ("agentRevisionId", agent_revision_id),
            ("userMessageId", run.user_message_id),
            ("assistantMessageId", assistant_id),
        ),
    )


def _production_memory_loaders(
    sessions: async_sessionmaker[AsyncSession],
    repository: MemoryRepository,
    *,
    agent_policy_loader: Callable[[str, str, UUID], Awaitable[tuple[AgentRevision, MemoryPolicy]]]
    | None = None,
) -> tuple[
    Callable[[UUID], Awaitable[MemoryProcessingJob]],
    Callable[[MemoryProcessingJob], Awaitable[MemoryTurnEvidence]],
]:
    """Resolve completed-turn evidence through public conversation ports."""

    identities = SqlIdentityRepository()
    conversations = SqlConversationRepository()
    runs = SqlRunRepository()

    async def resolve(
        job_id: UUID, expected: MemoryProcessingJob | None = None
    ) -> tuple[MemoryProcessingJob, MemoryTurnEvidence]:
        async with sessions() as session:
            run_id = expected.run_id if expected is not None else job_id
            run = await runs.get(session, run_id)
            if run is None or run.status is not RunStatus.COMPLETED:
                raise MemoryValidationError("completed memory run is unavailable")
            # Resolve the principal through the conversation owner, never from
            # provider or command metadata.
            conversation = await conversations.get(session, run.conversation_id)
            if conversation is None:
                raise MemoryValidationError("memory conversation is unavailable")
            principal = await identities.get(session, conversation.principal_id)
            if principal is None:
                raise MemoryValidationError("memory owner is unavailable")
            issuer, subject = principal.issuer, principal.subject
            if expected is not None and (expected.issuer, expected.subject) != (issuer, subject):
                raise MemoryValidationError("memory evidence owner mismatch")
            if expected is not None and expected.conversation_id != run.conversation_id:
                raise MemoryValidationError("memory evidence conversation mismatch")
            if expected is not None and expected.agent_revision_id != run.agent_revision_id:
                raise MemoryValidationError("memory evidence agent revision mismatch")
            pinned_agent: AgentRevision | None = None
            pinned_policy: MemoryPolicy | None = None
            if agent_policy_loader is not None:
                pinned_agent, pinned_policy = await agent_policy_loader(
                    issuer, subject, run.agent_revision_id
                )
                if pinned_agent.memory_policy_revision_id != run.memory_policy_revision_id:
                    raise MemoryValidationError(
                        "run memory policy is not pinned to its agent revision"
                    )
            if (
                expected is not None
                and expected.memory_policy_revision_id is not None
                and expected.memory_policy_revision_id != run.memory_policy_revision_id
            ):
                raise MemoryValidationError("memory policy revision mismatch")
            user_ids = expected.user_message_ids if expected is not None else (run.user_message_id,)
            assistant_ids = (
                expected.assistant_message_ids
                if expected is not None
                else ((run.assistant_message_id,) if run.assistant_message_id is not None else ())
            )
            if not user_ids or not assistant_ids or run.user_message_id not in user_ids:
                raise MemoryValidationError("memory evidence message identifiers are incomplete")
            if run.assistant_message_id not in assistant_ids:
                raise MemoryValidationError("memory evidence assistant identifier mismatch")
            messages = {
                message.id: message
                for message in await conversations.messages(session, run.conversation_id)
            }
            if set(messages) & (set(user_ids) | set(assistant_ids)) != set(user_ids) | set(
                assistant_ids
            ):
                raise MemoryValidationError("memory evidence messages are unavailable")
            users = [messages[item] for item in user_ids]
            assistants = [messages[item] for item in assistant_ids]
            if any(
                item.role is not MessageRole.USER or item.state is not MessageState.COMPLETE
                for item in users
            ):
                raise MemoryValidationError("memory user evidence role or state mismatch")
            if any(
                item.role is not MessageRole.ASSISTANT or item.state is not MessageState.COMPLETE
                for item in assistants
            ):
                raise MemoryValidationError("memory assistant evidence role or state mismatch")
            user_content = "\n".join(item.content for item in users)
            assistant_content = "\n".join(item.content for item in assistants)
            digest = hashlib.sha256(user_content.encode()).hexdigest()
            if expected is not None and expected.evidence_digest != digest:
                raise MemoryValidationError("memory evidence digest mismatch")
            job = expected or MemoryProcessingJob(
                run.id,
                issuer,
                subject,
                run.id,
                run.conversation_id,
                agent_revision_id=run.agent_revision_id,
                agent_profile_id=(
                    pinned_agent.profile_id
                    if pinned_agent is not None
                    else conversation.agent_profile_id
                ),
                user_message_ids=tuple(user_ids),
                assistant_message_ids=tuple(assistant_ids),
                evidence_digest=digest,
                correlation_id=run.attempt_id or run.id,
                causation_id=run.id,
                available_at=datetime.now(UTC),
                memory_policy_revision_id=run.memory_policy_revision_id,
                allow_shared_user_promotion=(
                    pinned_policy.allow_shared_user_promotion
                    if pinned_policy is not None
                    else False
                ),
            )
            if job.memory_policy_revision_id is None:
                job = replace(job, memory_policy_revision_id=run.memory_policy_revision_id)
            if pinned_policy is not None and pinned_agent is not None:
                if job.allow_shared_user_promotion != pinned_policy.allow_shared_user_promotion:
                    raise MemoryValidationError(
                        "memory job promotion flag does not match pinned policy"
                    )
                if job.agent_profile_id != pinned_agent.profile_id:
                    raise MemoryValidationError("memory job agent profile is not pinned")
            evidence = MemoryTurnEvidence(
                issuer,
                subject,
                run.id,
                run.conversation_id,
                run.agent_revision_id,
                tuple(user_ids),
                tuple(assistant_ids),
                user_content,
                assistant_content,
                digest,
            )
            return job, evidence

    async def load_job(job_id: UUID) -> MemoryProcessingJob:
        job, _ = await resolve(job_id)
        persist = cast(
            Callable[[MemoryProcessingJob], Awaitable[MemoryProcessingJob]] | None,
            getattr(repository, "enqueue_processing_job", None),
        )
        if callable(persist):
            job = await persist(job)
        return job

    async def load_evidence(job: MemoryProcessingJob) -> MemoryTurnEvidence:
        _, evidence = await resolve(job.id, job)
        return evidence

    return load_job, load_evidence


class InstrumentedMemoryRepository:
    """Application-boundary decorator shared by HTTP and worker callers."""

    def __init__(self, inner: MemoryRepository, metrics: MetadataMetrics, dependency: str) -> None:
        self._inner = inner
        self._metrics = metrics
        self._dependency = dependency

    def set_run_recall_metadata_loader(self, loader: Callable[..., Awaitable[object]]) -> None:
        method = getattr(self._inner, "set_run_recall_metadata_loader", None)
        if callable(method):
            method(loader)

    def set_candidate_evidence_loader(
        self, loader: Callable[..., Awaitable[object]] | None
    ) -> None:
        method = getattr(self._inner, "set_candidate_evidence_loader", None)
        if callable(method):
            method(loader)

    async def get_run_memory_activity(
        self, run_id: UUID, issuer: str, subject: str
    ) -> MemoryActivitySnapshot:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "get_run_memory_activity")
        )
        return cast(MemoryActivitySnapshot, await method(run_id, issuer, subject))

    async def reserve_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> bool:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "reserve_reindex_command"))
        return bool(
            await method(issuer, subject, generation_id, idempotency_key, fingerprint)
        )

    async def release_reindex_command(
        self, issuer: str, subject: str, idempotency_key: str, fingerprint: str
    ) -> None:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "release_reindex_command")
        )
        await method(issuer, subject, idempotency_key, fingerprint)

    async def complete_reindex_command(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
        fingerprint: str,
    ) -> None:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "complete_reindex_command")
        )
        await method(issuer, subject, generation_id, idempotency_key, fingerprint)

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
        if isinstance(
            exc, (MemoryPurgeConfirmationRequired, MemoryValidationError, ValueError, TypeError)
        ):
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
        if isinstance(filters, MemoryFilters) and filters.include_all_scopes:
            return "all"
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
        trace_memory_id, trace_revision_id, trace_generation_id = trace_ids
        # Correlation and identifiers remain trace attributes; every API or
        # worker invocation gets a fresh root so retries cannot be mistaken
        # for one request in telemetry.
        trace_id = uuid4().hex
        try:
            result = await method(*args, **kwargs)
        except Exception as exc:
            record_memory_operation(
                self._metrics,
                timer,
                outcome="error",
                scope_type=self._scope(kwargs),
                lifecycle_status="unknown",
                dependency=self._dependency,
                trace_id=trace_id,
                memory_id=trace_memory_id,
                memory_revision_id=trace_revision_id,
                generation_id=trace_generation_id,
                error_class=self._error_class(exc),
                operation=operation,
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
            self._metrics,
            timer,
            outcome="ok",
            scope_type=self._scope(kwargs, result),
            lifecycle_status=lifecycle_status,
            dependency=self._dependency,
            trace_id=trace_id,
            memory_id=result_memory_id,
            memory_revision_id=revision_id,
            operation=operation,
            generation_id=generation_id,
        )
        return result

    async def list_memories(
        self, issuer: str, subject: str, filters: MemoryFilters
    ) -> list[MemoryRecord]:
        result = await self._invoke(
            "memory.list",
            self._inner.list_memories,
            (None, None, None),
            issuer,
            subject,
            filters=filters,
        )
        return result  # type: ignore[return-value]

    async def get_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        result = await self._invoke(
            "memory.get",
            self._inner.get_memory,
            (str(memory_id), None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def create_memory(self, issuer: str, subject: str, **kwargs: object) -> MemoryRecord:
        result = await self._invoke(
            "memory.create",
            self._inner.create_memory,
            (None, None, None),
            issuer,
            subject,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def reinforce_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        result = await self._invoke(
            "memory.reinforce",
            self._inner.reinforce_memory,
            (str(memory_id), None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def revise_memory(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        result = await self._invoke(
            "memory.revise",
            self._inner.revise_memory,
            (str(memory_id), None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def set_status(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        result = await self._invoke(
            "memory.status",
            self._inner.set_status,
            (str(memory_id), None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def set_pinned(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        result = await self._invoke(
            "memory.pin",
            self._inner.set_pinned,
            (str(memory_id), None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def purge(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryAuditRecord:
        result = await self._invoke(
            "memory.purge",
            self._inner.purge,
            (str(memory_id), None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def register_embedding_generation(
        self, issuer: str, subject: str, **kwargs: object
    ) -> MemoryEmbeddingGeneration:
        result = await self._invoke(
            "memory.embedding.register",
            self._inner.register_embedding_generation,
            (None, None, None),
            issuer,
            subject,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "get_embedding_generation")
        )
        return cast(MemoryEmbeddingGeneration, await method(issuer, subject, generation_id))

    async def list_embedding_generations(
        self, issuer: str, subject: str, *, status: str | None = None
    ) -> list[MemoryEmbeddingGeneration]:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "list_embedding_generations")
        )
        return cast(list[MemoryEmbeddingGeneration], await method(issuer, subject, status=status))

    async def get_active_embedding_generation(
        self, issuer: str, subject: str
    ) -> MemoryEmbeddingGeneration | None:
        method = cast(
            Callable[..., Awaitable[object]],
            getattr(self._inner, "get_active_embedding_generation"),
        )
        result = await self._invoke(
            "memory.retrieval.generation", method, (None, None, None), issuer, subject
        )
        return cast(MemoryEmbeddingGeneration | None, result)

    async def search_lexical(
        self, issuer: str, subject: str, **kwargs: object
    ) -> list[MemoryRecord]:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "search_lexical"))
        result = await self._invoke(
            "memory.retrieval.lexical", method, (None, None, None), issuer, subject, **kwargs
        )
        return cast(list[MemoryRecord], result)

    async def search_vector(
        self, issuer: str, subject: str, **kwargs: object
    ) -> list[MemoryRecord]:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "search_vector"))
        result = await self._invoke(
            "memory.retrieval.vector", method, (None, None, None), issuer, subject, **kwargs
        )
        return cast(list[MemoryRecord], result)

    async def activate_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        result = await self._invoke(
            "memory.embedding.activate",
            self._inner.activate_embedding_generation,
            (None, None, str(generation_id)),
            issuer,
            subject,
            generation_id,
        )
        return result  # type: ignore[return-value]

    async def attach_embedding(
        self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
    ) -> MemoryRecord:
        revision_id = kwargs.get("revision_id")
        result = await self._invoke(
            "memory.embedding.attach",
            self._inner.attach_embedding,
            (str(memory_id), str(revision_id) if isinstance(revision_id, UUID) else None, None),
            issuer,
            subject,
            memory_id,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def save_model_configuration(
        self, issuer: str, subject: str, configuration: MemoryModelConfiguration, **kwargs: object
    ) -> MemoryModelConfiguration:
        result = await self._invoke(
            "memory.model.configure",
            self._inner.save_model_configuration,
            (None, None, None),
            issuer,
            subject,
            configuration,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def get_model_configuration(self, issuer: str, subject: str) -> MemoryModelConfiguration:
        result = await self._invoke(
            "memory.model.get",
            self._inner.get_model_configuration,
            (None, None, None),
            issuer,
            subject,
        )
        return result  # type: ignore[return-value]

    async def enqueue_processing_job(self, job: MemoryProcessingJob) -> MemoryProcessingJob:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "enqueue_processing_job")
        )
        result = await self._invoke("memory.job.enqueue", method, (None, None, None), job)
        return result  # type: ignore[return-value]

    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "get_processing_job"))
        result = await self._invoke(
            "memory.job.get", method, (None, None, None), job_id, issuer, subject
        )
        return result  # type: ignore[return-value]

    async def claim_processing_job_by_id(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob | None:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "claim_processing_job_by_id")
        )
        result = await self._invoke(
            "memory.job.claim", method, (None, None, None), job_id, issuer, subject
        )
        return result  # type: ignore[return-value]

    async def claim_processing_job(self, issuer: str, subject: str) -> MemoryProcessingJob | None:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "claim_processing_job")
        )
        result = await self._invoke(
            "memory.job.claim_next", method, (None, None, None), issuer, subject
        )
        return result  # type: ignore[return-value]

    async def settle_processing_job(
        self, job_id: UUID, lease_id: UUID, *, issuer: str, subject: str, **kwargs: object
    ) -> MemoryProcessingJob:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "settle_processing_job")
        )
        result = await self._invoke(
            "memory.job.settle",
            method,
            (None, None, None),
            job_id,
            lease_id,
            issuer=issuer,
            subject=subject,
            **kwargs,
        )
        return result  # type: ignore[return-value]

    async def persist_candidate(self, candidate: object) -> object:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "persist_candidate"))
        return await self._invoke("memory.candidate.persist", method, (None, None, None), candidate)

    async def get_candidate_for_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryCandidate | None:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "get_candidate_for_job")
        )
        result = await self._invoke(
            "memory.candidate.get", method, (None, None, None), job_id, issuer, subject
        )
        return cast(MemoryCandidate | None, result)

    async def list_candidates(
        self, issuer: str, subject: str, **kwargs: object
    ) -> list[MemoryCandidate]:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "list_candidates"))
        result = await self._invoke(
            "memory.candidate.list", method, (None, None, None), issuer, subject, **kwargs
        )
        return cast(list[MemoryCandidate], result)

    async def get_candidate(self, issuer: str, subject: str, candidate_id: UUID) -> MemoryCandidate:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "get_candidate"))
        return cast(
            MemoryCandidate,
            await self._invoke(
                "memory.candidate.detail", method, (None, None, None), issuer, subject, candidate_id
            ),
        )

    async def approve_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "approve_candidate"))
        return cast(
            MemoryCandidate,
            await self._invoke(
                "memory.candidate.approve",
                method,
                (None, None, None),
                issuer,
                subject,
                candidate_id,
                **kwargs,
            ),
        )

    async def reject_candidate(
        self, issuer: str, subject: str, candidate_id: UUID, **kwargs: object
    ) -> MemoryCandidate:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "reject_candidate"))
        return cast(
            MemoryCandidate,
            await self._invoke(
                "memory.candidate.reject",
                method,
                (None, None, None),
                issuer,
                subject,
                candidate_id,
                **kwargs,
            ),
        )

    async def record_action_outcome(self, **kwargs: object) -> None:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "record_action_outcome")
        )
        await self._invoke("memory.outcome.record", method, (None, None, None), **kwargs)

    async def queue_embedding_job(self, issuer: str, subject: str, **kwargs: object) -> object:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "queue_embedding_job"))
        return await self._invoke(
            "memory.embedding.queue", method, (None, None, None), issuer, subject, **kwargs
        )

    async def settle_embedding_job(
        self, job_id: UUID, *, issuer: str, subject: str, lease_id: UUID, **kwargs: object
    ) -> MemoryEmbeddingJob:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "settle_embedding_job")
        )
        return cast(
            MemoryEmbeddingJob,
            await self._invoke(
                "memory.embedding.settle",
                method,
                (None, None, None),
                job_id,
                issuer=issuer,
                subject=subject,
                lease_id=lease_id,
                **kwargs,
            ),
        )

    async def claim_embedding_job(self, issuer: str, subject: str) -> object:
        method = cast(Callable[..., Awaitable[object]], getattr(self._inner, "claim_embedding_job"))
        return await method(issuer, subject)

    async def claim_embedding_job_by_id(self, job_id: UUID, issuer: str, subject: str) -> object:
        method = cast(
            Callable[..., Awaitable[object]], getattr(self._inner, "claim_embedding_job_by_id")
        )
        return await method(job_id, issuer, subject)

    async def claim_embedding_job_for_revision(
        self,
        issuer: str,
        subject: str,
        *,
        revision_id: UUID,
        generation_id: UUID,
        lease_seconds: float = 60.0,
    ) -> MemoryEmbeddingJob | None:
        method = cast(
            Callable[..., Awaitable[object]],
            getattr(self._inner, "claim_embedding_job_for_revision"),
        )
        result = await method(
            issuer,
            subject,
            revision_id=revision_id,
            generation_id=generation_id,
            lease_seconds=lease_seconds,
        )
        return cast(MemoryEmbeddingJob | None, result)

    async def list_processing_owners(self) -> list[tuple[str, str]]:
        method = cast(
            Callable[[], Awaitable[list[tuple[str, str]]]],
            getattr(self._inner, "list_processing_owners"),
        )
        return await method()

    async def link_processing_job_memory(
        self, job_id: UUID, issuer: str, subject: str, memory_id: UUID
    ) -> None:
        method_value = getattr(self._inner, "link_processing_job_memory", None)
        if not callable(method_value):
            return
        method = cast(Callable[..., Awaitable[object]], method_value)
        await method(job_id, issuer, subject, memory_id)

    async def save_maintenance_state(self, issuer: str, subject: str, **kwargs: object) -> None:
        method_value = getattr(self._inner, "save_maintenance_state", None)
        if not callable(method_value):
            return
        method = cast(Callable[..., Awaitable[object]], method_value)
        await method(issuer, subject, **kwargs)


def memory_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    testing: bool = False,
    metrics: MetadataMetrics | None = None,
) -> MemoryRepository:
    if testing or sessions is None:
        repository: MemoryRepository = MemoryStore()
        dependency = "memory_store"
    else:
        repository = SqlMemoryRepository(sessions)
        dependency = "postgresql"
    return InstrumentedMemoryRepository(repository, metrics or MetadataMetrics(), dependency)


class _OllamaMemoryQueryEmbeddingPort(MemoryQueryEmbeddingPort):
    """Composition adapter from the configured Ollama embedding port."""

    def __init__(self, provider: OllamaEmbeddingAdapter) -> None:
        self.provider = provider

    async def embed_query(
        self, query: str, generation: MemoryEmbeddingGeneration
    ) -> MemoryQueryEmbedding:
        result = await self.provider.embed(
            generation.model_id,
            query,
            context=ProviderTraceContext(generation_id=str(generation.id)),
        )
        if (
            result.model_id != generation.model_id
            or result.dimension != generation.dimension
            or (
                generation.model_revision is not None
                and result.model_revision != generation.model_revision
            )
            or result.model_digest != generation.model_digest
        ):
            raise MemoryValidationError("query embedding identity does not match active generation")
        return MemoryQueryEmbedding(
            result.vector,
            generation.id,
            result.model_id,
            result.model_revision,
            result.dimension,
            result.digest,
            result.model_digest,
        )


def memory_recall_service(
    repository: MemoryRepository,
    *,
    settings: object | None = None,
    metrics: MetadataMetrics | None = None,
) -> MemoryRecallService:
    """Compose owner-scoped recall with the configured embedding provider.

    The provider is invoked only after recall resolves the owner-selected
    active generation.  Provider and database failures are converted by the
    recall service into a degraded empty result, so context assembly remains
    independent from memory availability.
    """

    endpoint = str(getattr(settings, "ollama_url", "http://ollama:11434"))
    timeout = float(getattr(settings, "ollama_run_timeout_seconds", 30.0))
    telemetry_metrics = metrics or MetadataMetrics()
    provider = OllamaEmbeddingAdapter(endpoint, timeout, telemetry=telemetry_metrics)

    class _RecallTelemetry(MemoryRecallTelemetryPort):
        def record(self, event: MemoryRecallTelemetryEvent) -> None:
            try:
                record_memory_retrieval(
                    telemetry_metrics,
                    operation=event.operation,
                    duration_ms=event.duration_ms,
                    trace_id=event.trace_id,
                    parent_span_id=event.parent_span_id,
                    outcome=event.outcome,
                    dependency=event.dependency,
                    error_class=event.error_class,
                    retrieval_stage=event.retrieval_stage,
                    degradation=event.degradation,
                    candidate_count=event.candidate_count,
                    # Selection happens before context rendering.  The
                    # provider-bound admitted revision count is emitted by
                    # the conversation context boundary instead.
                    recall_count=(
                        None if event.retrieval_stage == "selection" else event.recall_count
                    ),
                    fallback_outcome=event.fallback_outcome,
                    # Recall estimates content before context delimiters and
                    # trimming.  Final admitted tokens are emitted by the
                    # conversation context boundary.
                    context_tokens=(
                        None if event.retrieval_stage == "selection" else event.context_tokens
                    ),
                    memory_policy_revision_id=event.memory_policy_revision_id,
                    generation_id=event.generation_id,
                    run_id=event.run_id,
                    conversation_id=event.conversation_id,
                    retrieval_version=event.retrieval_version,
                )
            except Exception:
                # Telemetry is strictly advisory to the conversation path.
                return

    return MemoryRecallService(
        repository,
        embedding_port=_OllamaMemoryQueryEmbeddingPort(provider),
        telemetry=_RecallTelemetry(),
    )


def memory_processing_service(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    metrics: MetadataMetrics | None = None,
    settings: object | None = None,
    evidence_loader: Callable[..., Awaitable[object]] | None = None,
    agent_policy_loader: Callable[[str, str, UUID], Awaitable[tuple[AgentRevision, MemoryPolicy]]]
    | None = None,
) -> object:
    """Compose the worker memory processor without introducing a service."""

    repository = memory_repository(sessions, testing=sessions is None, metrics=metrics)
    memory_metrics = metrics or MetadataMetrics()
    endpoint = str(getattr(settings, "ollama_url", "http://ollama:11434"))
    timeout = float(getattr(settings, "ollama_run_timeout_seconds", 30.0))
    inference_provider = OllamaAdapter(endpoint, timeout, telemetry=memory_metrics)
    embedding_provider = OllamaEmbeddingAdapter(endpoint, timeout, telemetry=memory_metrics)
    job_loader: Callable[[UUID], Awaitable[MemoryProcessingJob]] | None = None
    production_evidence_loader: (
        Callable[[MemoryProcessingJob], Awaitable[MemoryTurnEvidence]] | None
    ) = None
    if sessions is not None:
        job_loader, production_evidence_loader = _production_memory_loaders(
            sessions, repository, agent_policy_loader=agent_policy_loader
        )
    effective_evidence_loader = cast(
        Callable[[MemoryProcessingJob], Awaitable[MemoryTurnEvidence | tuple[str, str]]] | None,
        evidence_loader or production_evidence_loader,
    )

    def emit_memory_telemetry(**event: Any) -> None:
        record_memory_processing(
            memory_metrics,
            component="aura.knowledge.memory_extraction",
            dependency="memory_worker",
            **event,
        )

    def emit_maintenance_telemetry(**event: Any) -> None:
        record_memory_processing(
            memory_metrics,
            component="aura.knowledge.memory_maintenance",
            dependency="memory_worker",
            **event,
        )

    processor = MemoryProcessingService(
        repository,
        inference_provider,
        embedding_provider,
        evidence_loader=effective_evidence_loader,
        job_loader=job_loader,
        telemetry=emit_memory_telemetry,
        maintenance_telemetry=emit_maintenance_telemetry,
    )

    def reindex_trace_context(
        trace_id: str,
        job_id: str,
        memory_id: str,
        memory_revision_id: str,
        generation_id: str,
    ) -> Any:
        return memory_trace_context(
            MemoryTraceContext(
                trace_id=trace_id,
                span_id=uuid5(
                    _MEMORY_COMMAND_NAMESPACE,
                    f"reindex-span:{job_id}:{memory_id}:{memory_revision_id}",
                ).hex[:16],
                job_id=job_id,
                memory_id=memory_id,
                memory_revision_id=memory_revision_id,
                generation_id=generation_id,
            )
        )

    reindex = MemoryReindexService(
        repository,
        embedding_provider,
        telemetry=emit_maintenance_telemetry,
        trace_context_factory=reindex_trace_context,
    )

    class WorkerMemoryService:
        async def process_command(self, command: object) -> object:
            parser = getattr(MemoryProcessingCommand, "from_outbox", None)
            if parser is not None and not isinstance(command, MemoryProcessingCommand):
                command = parser(command)
            return await processor.process_command(cast(MemoryProcessingCommand, command))

        async def process_job(self, job_id: UUID) -> object:
            return await processor.process_job(job_id)

        async def _process_embedding_job(self, job: MemoryEmbeddingJob) -> bool:
            """Claimed owner-scoped embedding work; safe across redelivery."""
            settle = cast(
                Callable[..., Awaitable[object]] | None,
                getattr(repository, "settle_embedding_job", None),
            )
            embedding_started = monotonic()
            trace_id = uuid5(_MEMORY_COMMAND_NAMESPACE, f"embedding:{job.id}").hex
            trace_context = MemoryTraceContext(
                trace_id=trace_id,
                span_id=uuid5(_MEMORY_COMMAND_NAMESPACE, f"embedding-span:{job.id}").hex[:16],
                job_id=str(job.id),
                generation_id=str(job.generation_id),
                memory_id=str(job.memory_id),
                memory_revision_id=str(job.revision_id),
            )
            active_trace = trace_context

            async def settle_and_emit(
                *,
                retryable: bool = False,
                failed: bool = False,
                error_class: str,
                outcome: str,
                backlog: int,
                retry_event: bool = False,
            ) -> None:
                with memory_trace_context(active_trace):
                    try:
                        if callable(settle):
                            await settle(
                                job.id,
                                issuer=job.issuer,
                                subject=job.subject,
                                lease_id=job.lease_id,
                                retryable=retryable,
                                failed=failed,
                                error_class=error_class,
                            )
                    finally:
                        emit_maintenance_telemetry(
                            operation="memory.error" if outcome == "error" else "memory.retry",
                            duration_ms=(monotonic() - embedding_started) * 1000,
                            trace_id=trace_id,
                            outcome=outcome,
                            error_class=error_class,
                            memory_id=str(job.memory_id),
                            memory_revision_id=str(job.revision_id),
                            generation_id=str(job.generation_id),
                            backlog=backlog,
                            attempt_count=job.attempt_count,
                        )
                        emit_maintenance_telemetry(
                            operation="memory.embedding",
                            duration_ms=(monotonic() - embedding_started) * 1000,
                            trace_id=trace_id,
                            outcome=outcome,
                            error_class=error_class,
                            memory_id=str(job.memory_id),
                            memory_revision_id=str(job.revision_id),
                            generation_id=str(job.generation_id),
                            backlog=backlog,
                            attempt_count=job.attempt_count,
                        )
                        if retry_event:
                            emit_maintenance_telemetry(
                                operation="memory.retry",
                                duration_ms=(monotonic() - embedding_started) * 1000,
                                trace_id=trace_id,
                                outcome="retryable",
                                error_class=error_class,
                                memory_id=str(job.memory_id),
                                memory_revision_id=str(job.revision_id),
                                generation_id=str(job.generation_id),
                                backlog=backlog,
                                attempt_count=job.attempt_count,
                            )

            async def resolve_embedding_work() -> tuple[MemoryEmbeddingGeneration, MemoryRecord]:
                with memory_trace_context(trace_context):
                    generation_loader = cast(
                        Callable[..., Awaitable[object]] | None,
                        getattr(repository, "get_embedding_generation", None),
                    )
                    if not callable(generation_loader):
                        raise MemoryNotFound("embedding generation not found")
                    generation = cast(
                        MemoryEmbeddingGeneration,
                        await generation_loader(job.issuer, job.subject, job.generation_id),
                    )
                    if generation.status != "active":
                        raise MemoryValidationError("embedding generation is not active")
                    records = await repository.list_memories(
                        job.issuer,
                        job.subject,
                        MemoryFilters(
                            scope_type=None,
                            include_all_scopes=True,
                            include_historical=True,
                            limit=100_000,
                        ),
                    )
                    record = next((item for item in records if item.id == job.memory_id), None)
                    if record is None:
                        raise MemoryNotFound("memory not found")
                    if (
                        generation.issuer != job.issuer
                        or generation.subject != job.subject
                        or job.memory_id != record.id
                        or job.generation_id != generation.id
                        or job.revision_id not in {revision.id for revision in record.revisions}
                    ):
                        raise MemoryValidationError(
                            "embedding work does not match its claimed revision"
                        )
                    return generation, record

            try:
                generation, record = await resolve_embedding_work()
                with memory_trace_context(trace_context):
                    if any(
                        item.revision_id == job.revision_id
                        and item.generation_id == job.generation_id
                        for item in record.embeddings
                    ):
                        if callable(settle):
                            await settle(
                                job.id,
                                issuer=job.issuer,
                                subject=job.subject,
                                lease_id=job.lease_id,
                            )
                        return True
                    revision_content = next(
                        item.content for item in record.revisions if item.id == job.revision_id
                    )
                    if classify_sensitivity(revision_content) is MemorySensitivity.CREDENTIAL:
                        if callable(settle):
                            await settle(
                                job.id,
                                issuer=job.issuer,
                                subject=job.subject,
                                lease_id=job.lease_id,
                                failed=True,
                                error_class="credential",
                            )
                        return False
                    embedded = await embedding_provider.embed(
                        generation.model_id,
                        revision_content,
                        context=ProviderTraceContext(
                            trace_id=trace_context.trace_id,
                            job_id=str(job.id),
                            generation_id=str(job.generation_id),
                        ),
                    )
                    if (
                        embedded.model_id != generation.model_id
                        or embedded.model_revision != generation.model_revision
                        or embedded.model_digest != generation.model_digest
                        or embedded.dimension != generation.dimension
                    ):
                        raise MemoryValidationError(
                            "embedding provider identity does not match active generation"
                        )
                    await repository.attach_embedding(
                        job.issuer,
                        job.subject,
                        job.memory_id,
                        revision_id=job.revision_id,
                        generation_id=job.generation_id,
                        vector=embedded.vector,
                        digest=embedded.digest,
                        model_id=embedded.model_id,
                        model_revision=embedded.model_revision,
                        model_digest=embedded.model_digest,
                        scope_type=record.scope.type,
                        agent_profile_id=record.scope.agent_profile_id,
                    )
                    if callable(settle):
                        await settle(
                            job.id, issuer=job.issuer, subject=job.subject, lease_id=job.lease_id
                        )
                    emit_maintenance_telemetry(
                        operation="memory.embedding",
                        duration_ms=(monotonic() - embedding_started) * 1000,
                        trace_id=trace_id,
                        outcome="ok",
                        memory_id=str(job.memory_id),
                        memory_revision_id=str(job.revision_id),
                        generation_id=str(job.generation_id),
                        backlog=0,
                        attempt_count=job.attempt_count,
                    )
                return True
            except MemoryNotFound:
                # Purge fences are terminal and must not be resurrected.
                await settle_and_emit(failed=True, error_class="purged", outcome="error", backlog=0)
                return False
            except MemoryValidationError:
                # Generation cutovers and provider identity mismatches are
                # retryable work.  In particular, never terminally fail a
                # building generation because a provider was unavailable.
                await settle_and_emit(
                    retryable=True, error_class="generation", outcome="retryable", backlog=1
                )
                return False
            except Exception:
                await settle_and_emit(
                    retryable=job.attempt_count < 3,
                    failed=job.attempt_count >= 3,
                    error_class="provider",
                    outcome="error",
                    backlog=1,
                    retry_event=True,
                )
                return False

        async def maintain(self) -> int:
            owners_loader = cast(
                Callable[[], Awaitable[list[tuple[str, str]]]] | None,
                getattr(repository, "list_processing_owners", None),
            )
            if not callable(owners_loader):
                return 0
            changed = 0
            for issuer, subject in await owners_loader():
                maintenance_context = MemoryTraceContext(
                    trace_id=uuid5(
                        _MEMORY_COMMAND_NAMESPACE, f"maintenance:{issuer}:{subject}"
                    ).hex,
                    span_id=uuid5(
                        _MEMORY_COMMAND_NAMESPACE, f"maintenance-span:{issuer}:{subject}"
                    ).hex[:16],
                )
                with memory_trace_context(maintenance_context):
                    changed += await processor.maintain(issuer, subject)
                claim_next = cast(
                    Callable[[str, str], Awaitable[MemoryProcessingJob | None]] | None,
                    getattr(repository, "claim_processing_job", None),
                )
                if callable(claim_next):
                    queued = await claim_next(issuer, subject)
                    if queued is not None:
                        await processor.process_job(queued.id, lease_id=queued.lease_id)
                claim_embedding = cast(
                    Callable[[str, str], Awaitable[MemoryEmbeddingJob | None]] | None,
                    getattr(repository, "claim_embedding_job", None),
                )
                if callable(claim_embedding):
                    # One claim per owner/cycle bounds provider work and
                    # leaves remaining retryable jobs for the next cycle.
                    embedding_job = await claim_embedding(issuer, subject)
                    if embedding_job is not None:
                        await self._process_embedding_job(embedding_job)
            return changed

        async def resume_reindex(
            self,
            issuer: str | None = None,
            subject: str | None = None,
            generation_id: UUID | None = None,
        ) -> int:
            # API-triggered resumes are explicitly owner and generation
            # scoped.  The unscoped sweep remains reserved for housekeeping.
            if issuer is not None or subject is not None or generation_id is not None:
                if issuer is None or subject is None or generation_id is None:
                    raise MemoryValidationError("scoped reindex identity is incomplete")
                context = MemoryTraceContext(
                    trace_id=uuid5(
                        _MEMORY_COMMAND_NAMESPACE, f"reindex:{issuer}:{subject}:{generation_id}"
                    ).hex,
                    span_id=uuid5(
                        _MEMORY_COMMAND_NAMESPACE, f"reindex-span:{generation_id}"
                    ).hex[:16],
                    generation_id=str(generation_id),
                )
                with memory_trace_context(context):
                    return await reindex.resume(issuer, subject, generation_id)
            owners_loader = cast(
                Callable[[], Awaitable[list[tuple[str, str]]]] | None,
                getattr(repository, "list_processing_owners", None),
            )
            if not callable(owners_loader):
                return 0
            completed = 0
            for issuer, subject in await owners_loader():
                try:
                    configuration = await processor.model_configuration(issuer, subject)
                except MemoryNotFound:
                    continue
                listed = cast(
                    Callable[..., Awaitable[list[MemoryEmbeddingGeneration]]] | None,
                    getattr(repository, "list_embedding_generations", None),
                )
                building = (
                    await listed(issuer, subject, status="building") if callable(listed) else []
                )
                building = [
                    generation
                    for generation in building
                    if generation.model_id == configuration.embedding_model_id
                    and generation.model_revision == configuration.embedding_model_revision
                ]
                if building:
                    for generation in building:
                        context = MemoryTraceContext(
                            trace_id=uuid5(
                                _MEMORY_COMMAND_NAMESPACE,
                                f"reindex:{issuer}:{subject}:{generation.id}",
                            ).hex,
                            span_id=uuid5(
                                _MEMORY_COMMAND_NAMESPACE, f"reindex-span:{generation.id}"
                            ).hex[:16],
                            generation_id=str(generation.id),
                        )
                        with memory_trace_context(context):
                            completed += await reindex.resume(issuer, subject, generation.id)
                else:
                    generation_id = configuration.embedding_generation
                    if isinstance(generation_id, UUID):
                        context = MemoryTraceContext(
                            trace_id=uuid5(
                                _MEMORY_COMMAND_NAMESPACE,
                                f"reindex:{issuer}:{subject}:{generation_id}",
                            ).hex,
                            span_id=uuid5(
                                _MEMORY_COMMAND_NAMESPACE, f"reindex-span:{generation_id}"
                            ).hex[:16],
                            generation_id=str(generation_id),
                        )
                        with memory_trace_context(context):
                            completed += await reindex.resume(issuer, subject, generation_id)
            return completed

    return WorkerMemoryService()


def memory_evidence_loader(
    sessions: async_sessionmaker[AsyncSession],
    repository: MemoryRepository,
    *,
    agent_policy_loader: Callable[[str, str, UUID], Awaitable[tuple[AgentRevision, MemoryPolicy]]]
    | None = None,
) -> Callable[[MemoryProcessingJob], Awaitable[MemoryTurnEvidence]]:
    """Compose the authenticated original-turn evidence query seam."""

    _, loader = _production_memory_loaders(
        sessions, repository, agent_policy_loader=agent_policy_loader
    )
    return loader


__all__ = [
    "MEMORY_PROCESSING_TOPIC",
    "memory_command_factory",
    "make_agent_policy_loader",
    "memory_processing_service",
    "memory_repository",
]
