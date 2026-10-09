"""AURA-0038 extraction policy, worker retry, and embedding contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import aura_core.bootstrap.memory_uow as memory_uow
import pytest
from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.bootstrap.memory_uow import memory_command_factory
from aura_core.domains.execution.runs.dto import Run, RunStatus
from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.interaction.conversations.dto import (
    Conversation,
    Message,
    MessageRole,
    MessageState,
)
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.domains.knowledge.memory.public import (
    CandidateState,
    MemoryAction,
    MemoryCandidate,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryIdempotencyConflict,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingCommand,
    MemoryProcessingJob,
    MemoryProcessingService,
    MemoryProvenance,
    MemoryRecord,
    MemoryRetentionBasis,
    MemoryScope,
    MemoryScopeType,
    MemorySensitivity,
    MemoryStore,
    MemoryTurnEvidence,
    MemoryValidationError,
    MemoryVersionConflict,
    ProcessingJobStatus,
    classify_retention_basis,
    classify_sensitivity,
    contains_secret,
    decide_candidate,
    normalize_retention_horizon,
)
from aura_core.entrypoints.worker.app import (
    _configure_worker_memory_recall,
    emit_memory_activity,
    run_memory_once,
)
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.telemetry import MetadataMetrics, record_memory_processing
from aura_core.providers.embeddings.ollama.adapter import OllamaEmbeddingAdapter
from aura_core.runtime.models.ports import (
    EmbeddingResult,
    ModelDescriptor,
    StructuredInferenceRequest,
)

ISSUER = "https://issuer.example"
OWNER = "owner"
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CURRENT_AGENT = UUID("11111111-1111-4111-8111-111111111111")


class _Inference:
    def __init__(self, result: object = None, *, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.calls: list[dict[str, object]] = []

    async def infer(self, request: object) -> object:
        if isinstance(request, Mapping):
            payload = dict(cast(Mapping[str, object], request))
        else:
            raw_payload = cast(object, getattr(request, "input", {}))
            payload = (
                dict(cast(Mapping[str, object], raw_payload))
                if isinstance(raw_payload, Mapping)
                else {}
            )
        self.calls.append(payload)
        if self.error is not None:
            raise self.error
        return self.result

    async def infer_memory(self, request: object) -> object:
        # Keep compatibility while the Core worker moves to the generic
        # StructuredInferencePort.
        return await self.infer(request)


class _Embedding:
    def __init__(
        self,
        vector: Sequence[float] = (0.1, 0.2, 0.3),
        *,
        error: Exception | None = None,
        model_revision: str | None = None,
        model_digest: str = "a" * 64,
    ) -> None:
        self.vector = tuple(vector)
        self.digest = "a" * 64
        self.model_id = ""
        self.model_revision = model_revision
        self.model_digest = model_digest
        self.dimension = len(self.vector)
        self.error = error
        self.calls: list[str] = []

    async def embed(self, model_id: str, content: str) -> object:
        self.model_id = model_id
        self.calls.append(content)
        if self.error is not None:
            raise self.error
        return type(
            "EmbeddingResult",
            (),
            {
                "vector": self.vector,
                "digest": self.digest,
                "model_id": model_id,
                "model_revision": self.model_revision,
                "model_digest": self.model_digest,
                "dimension": len(self.vector),
            },
        )()


class _DurableMemoryStore(MemoryStore):
    """Small durable seam double for restart/claim tests."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(clock=clock)
        self.processing_jobs: dict[UUID, MemoryProcessingJob] = {}
        self.persisted_candidates: dict[UUID, MemoryCandidate] = {}
        self.persisted_outcomes: list[dict[str, object]] = []
        self.embedding_jobs: dict[UUID, MemoryEmbeddingJob] = {}


    async def enqueue_processing_job(self, job: MemoryProcessingJob) -> MemoryProcessingJob:
        job_id = job.id
        prior = self.processing_jobs.get(job_id)
        if prior is not None:
            return prior
        self.processing_jobs[job_id] = job
        return job

    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        generation = self.embedding_generations.get(generation_id)
        if generation is None or (generation.issuer, generation.subject) != (issuer, subject):
            raise MemoryNotFound("embedding generation not found")
        return generation

    async def get_processing_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryProcessingJob:
        job = self.processing_jobs.get(job_id)
        if job is None or (job.issuer, job.subject) != (issuer, subject):
            raise MemoryNotFound("memory processing job not found")
        return job

    async def claim_processing_job_by_id(
        self,
        job_id: UUID,
        issuer: str,
        subject: str,
        *,
        lease_seconds: float = 60.0,
    ) -> MemoryProcessingJob | None:
        job = self.processing_jobs.get(job_id)
        if job is None or (job.issuer, job.subject) != (issuer, subject):
            return None
        if job.status not in {ProcessingJobStatus.QUEUED, ProcessingJobStatus.RETRYABLE}:
            return None
        claimed = replace(
            job,
            status=ProcessingJobStatus.RUNNING,
            attempt_count=job.attempt_count + 1,
            lease_id=uuid4(),
            lease_until=NOW + timedelta(seconds=max(1.0, lease_seconds)),
        )
        self.processing_jobs[job_id] = claimed
        return claimed

    async def settle_processing_job(
        self,
        job_id: UUID,
        lease_id: UUID,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        retryable: bool = False,
        error_class: str | None = None,
    ) -> MemoryProcessingJob:
        job = self.processing_jobs[job_id]
        if (
            job.lease_id != lease_id
            or job.status is not ProcessingJobStatus.RUNNING
            or issuer is not None
            and job.issuer != issuer
            or subject is not None
            and job.subject != subject
        ):
            raise MemoryValidationError("processing job lease is stale")
        settled = replace(
            job,
            status=ProcessingJobStatus.RETRYABLE if retryable else ProcessingJobStatus.COMPLETED,
            last_error_class=error_class,
            lease_id=None,
            lease_until=None,
        )
        self.processing_jobs[job_id] = settled
        return settled

    async def list_processing_owners(self) -> list[tuple[str, str]]:
        return sorted({(job.issuer, job.subject) for job in self.processing_jobs.values()})

    async def persist_candidate(
        self, candidate: MemoryCandidate, **kwargs: object
    ) -> MemoryCandidate:
        del kwargs
        self.persisted_candidates[candidate.id] = candidate
        return candidate

    async def get_candidate_for_job(
        self, job_id: UUID, issuer: str, subject: str
    ) -> MemoryCandidate | None:
        return next(
            (
                candidate
                for candidate in self.persisted_candidates.values()
                if candidate.job_id == job_id
                and candidate.issuer == issuer
                and candidate.subject == subject
            ),
            None,
        )

    async def record_action_outcome(self, **kwargs: object) -> None:
        self.persisted_outcomes.append(dict(kwargs))

    async def queue_embedding_job(
        self, issuer: str, subject: str, *, memory_id: UUID, revision_id: UUID,
        generation_id: UUID,
    ) -> MemoryEmbeddingJob:
        prior = next(
            (
                job
                for job in self.embedding_jobs.values()
                if (job.issuer, job.subject, job.revision_id, job.generation_id)
                == (issuer, subject, revision_id, generation_id)
            ),
            None,
        )
        if prior is not None:
            return prior
        job = MemoryEmbeddingJob(uuid4(), issuer, subject, memory_id, revision_id, generation_id)
        self.embedding_jobs[job.id] = job
        return job

    async def settle_embedding_job(
        self,
        job_id: UUID,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        lease_id: UUID | None = None,
        retryable: bool = False,
        failed: bool = False,
        error_class: str | None = None,
    ) -> MemoryEmbeddingJob:
        job = self.embedding_jobs[job_id]
        if lease_id is not None and job.lease_id != lease_id:
            raise MemoryValidationError("embedding job lease is stale")
        settled = replace(
            job,
            status=(
                ProcessingJobStatus.FAILED
                if failed
                else (
                    ProcessingJobStatus.RETRYABLE
                    if retryable
                    else ProcessingJobStatus.COMPLETED
                )
            ),
            last_error_class=error_class,
            lease_id=None,
            lease_until=None,
        )
        self.embedding_jobs[job_id] = settled
        return settled


def test_worker_composition_attaches_owner_scoped_memory_recall(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = object()
    recall = object()
    calls: dict[str, object] = {}

    def make_repository(sessions: object, *, metrics: object) -> object:
        calls["sessions"] = sessions
        calls["metrics"] = metrics
        return repository

    def make_recall(
        actual_repository: object, *, settings: object, metrics: object
    ) -> object:
        calls["repository"] = actual_repository
        calls["settings"] = settings
        calls["recall_metrics"] = metrics
        return recall

    monkeypatch.setattr(memory_uow, "memory_repository", make_repository)
    monkeypatch.setattr(memory_uow, "memory_recall_service", make_recall)
    store = SqlConversationStore(None)  # type: ignore[arg-type]
    metrics = MetadataMetrics()
    settings = object()
    sessions = object()

    returned = _configure_worker_memory_recall(
        store,
        sessions,
        settings=settings,
        metrics=metrics,
    )

    assert returned is repository
    assert store.memory_recall is recall
    assert store.memory_activity_repository is repository
    assert calls == {
        "sessions": sessions,
        "metrics": metrics,
        "repository": repository,
        "settings": settings,
        "recall_metrics": metrics,
    }


class _CrashStore(_DurableMemoryStore):
    def __init__(self, crash_at: str, *, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(clock=clock)
        self.crash_at = crash_at
        self.crashed = False

    def _crash(self, stage: str) -> None:
        if self.crash_at == stage and not self.crashed:
            self.crashed = True
            if stage == "embedding":
                # Model a worker cancellation at the embedding boundary.  The
                # processing path intentionally catches provider Exceptions,
                # but cancellation must unwind and leave the lease recoverable.
                raise asyncio.CancelledError("injected crash at embedding")
            raise RuntimeError(f"injected crash at {stage}")

    async def persist_candidate(
        self, candidate: MemoryCandidate, **kwargs: object
    ) -> MemoryCandidate:
        result = await super().persist_candidate(candidate, **kwargs)
        self._crash("candidate")
        return result


    async def record_action_outcome(self, **kwargs: object) -> None:
        await super().record_action_outcome(**kwargs)
        self._crash("action")

    async def queue_embedding_job(
        self,
        issuer: str,
        subject: str,
        *,
        memory_id: UUID,
        revision_id: UUID,
        generation_id: UUID,
    ) -> MemoryEmbeddingJob:
        result = await super().queue_embedding_job(
            issuer,
            subject,
            memory_id=memory_id,
            revision_id=revision_id,
            generation_id=generation_id,
        )
        self._crash("embedding")
        return result

    async def settle_processing_job(
        self,
        job_id: UUID,
        lease_id: UUID,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        retryable: bool = False,
        error_class: str | None = None,
    ) -> MemoryProcessingJob:
        result = await super().settle_processing_job(
            job_id,
            lease_id,
            issuer=issuer,
            subject=subject,
            retryable=retryable,
            error_class=error_class,
        )
        self._crash("settle")
        return result


class _GenerationMemoryStore(MemoryStore):
    """In-memory provider seam with the generation lookup used by workers."""

    async def get_embedding_generation(
        self, issuer: str, subject: str, generation_id: UUID
    ) -> MemoryEmbeddingGeneration:
        generation = self.embedding_generations.get(generation_id)
        if generation is None or (generation.issuer, generation.subject) != (issuer, subject):
            raise MemoryNotFound("embedding generation not found")
        return generation


class _PurgeDuringLinkStore(MemoryStore):
    """Inject a purge after creation and before the processing link is written."""

    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        super().__init__(clock=clock)
        self.link_attempts = 0

    async def link_processing_job_memory(
        self, job_id: UUID, issuer: str, subject: str, memory_id: UUID
    ) -> None:
        self.link_attempts += 1
        record = self.memories[memory_id]
        await self.purge(
            issuer,
            subject,
            memory_id,
            confirmation="PURGE MEMORY",
            expected_version=record.version,
            idempotency_key=f"purge-link:{job_id}",
            scope_type=record.scope.type,
            agent_profile_id=record.scope.agent_profile_id,
            authorized_agent_ids=frozenset(
                {record.scope.agent_profile_id}
                if record.scope.agent_profile_id is not None
                else set()
            ),
        )
        raise RuntimeError("injected crash between memory creation and link")


def _candidate(
    *,
    action: MemoryAction = MemoryAction.CREATE,
    confidence: float = 0.9,
    message_id: UUID | None = None,
    scope: MemoryScope | None = None,
    sensitivity: MemorySensitivity = MemorySensitivity.ORDINARY,
    content: str | None = "The owner prefers concise answers.",
) -> MemoryCandidate:
    return MemoryCandidate(
        uuid4(),
        uuid4(),
        ISSUER,
        OWNER,
        action,
        content,
        MemoryKind.PREFERENCE if content else None,
        scope or MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        confidence,
        importance=0.8 if content else None,
        half_life_days=30 if content else None,
        sensitivity=sensitivity,
        grounded_message_ids=(message_id,) if message_id else (),
    )


async def _ready_generation(
    repository: MemoryStore,
    *,
    generation: int = 1,
    model_id: str = "embedder",
    dimension: int = 3,
) -> MemoryEmbeddingGeneration:
    """Create a selected generation for worker-path fixtures."""

    item = await repository.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=generation,
        model_id=model_id,
        dimension=dimension,
        model_digest=("a" if generation == 1 else "b") * 64,
    )
    return await repository.activate_embedding_generation(ISSUER, OWNER, item.id)


def test_candidate_policy_has_explicit_confidence_and_grounding_bands() -> None:
    message_id = uuid4()
    assert (
            decide_candidate(
                _candidate(confidence=0.9, message_id=message_id),
                user_message_ids=frozenset({message_id}),
                run_agent_profile_id=CURRENT_AGENT,
        ).state
        is CandidateState.ACCEPTED
    )
    assert (
            decide_candidate(
                _candidate(confidence=0.60, message_id=message_id),
                user_message_ids=frozenset({message_id}),
                run_agent_profile_id=CURRENT_AGENT,
        ).state
        is CandidateState.REVIEW
    )
    assert (
            decide_candidate(
                _candidate(confidence=0.849, message_id=message_id),
                user_message_ids=frozenset({message_id}),
                run_agent_profile_id=CURRENT_AGENT,
        ).state
        is CandidateState.REVIEW
    )
    assert (
            decide_candidate(
                _candidate(confidence=0.599, message_id=message_id),
                user_message_ids=frozenset({message_id}),
                run_agent_profile_id=CURRENT_AGENT,
        ).state
        is CandidateState.REJECTED
    )
    assert (
        decide_candidate(_candidate(confidence=0.99), user_message_ids=frozenset()).reason
        == "ungrounded"
    )


@pytest.mark.parametrize(
    ("sensitivity", "expected"),
    [
        (MemorySensitivity.SENSITIVE, CandidateState.REVIEW),
        (MemorySensitivity.CREDENTIAL, CandidateState.REJECTED),
    ],
)
def test_sensitive_and_credential_candidates_cannot_auto_commit(
    sensitivity: MemorySensitivity, expected: CandidateState
) -> None:
    message_id = uuid4()
    content = (
        "The owner's medical diagnosis is private."
        if sensitivity is MemorySensitivity.SENSITIVE
        else "The owner password is secret."
    )
    assert (
        decide_candidate(
            _candidate(sensitivity=sensitivity, message_id=message_id, content=content),
                user_message_ids=frozenset({message_id}),
                run_agent_profile_id=CURRENT_AGENT,
        ).state
        is expected
    )


def test_agent_scope_requires_current_run_agent_and_shared_promotion_is_reviewed() -> None:
    message_id = uuid4()
    agent_id = uuid4()
    candidate = _candidate(
        message_id=message_id,
        scope=MemoryScope(MemoryScopeType.AGENT, agent_id),
    )
    assert (
        decide_candidate(
            candidate,
            user_message_ids=frozenset({message_id}),
            run_agent_profile_id=uuid4(),
        ).reason
        == "risky_scope"
    )
    assert (
        decide_candidate(
            candidate,
            user_message_ids=frozenset({message_id}),
            run_agent_profile_id=agent_id,
        ).state
        is CandidateState.ACCEPTED
    )


@pytest.mark.parametrize(
    ("scope", "sensitivity", "expected"),
    [
        (MemoryScope(MemoryScopeType.USER), MemorySensitivity.ORDINARY, CandidateState.REVIEW),
        (None, MemorySensitivity.ORDINARY, CandidateState.REVIEW),
        (
            MemoryScope(MemoryScopeType.AGENT, uuid4()),
            MemorySensitivity.SENSITIVE,
            CandidateState.REVIEW,
        ),
        (
            MemoryScope(MemoryScopeType.AGENT, uuid4()),
            MemorySensitivity.UNKNOWN_RISK,
            CandidateState.REVIEW,
        ),
    ],
)
def test_shared_default_review_and_sensitive_categories_are_always_reviewed(
    scope: MemoryScope | None,
    sensitivity: MemorySensitivity,
    expected: CandidateState,
) -> None:
    message_id = uuid4()
    candidate = _candidate(scope=scope, sensitivity=sensitivity, message_id=message_id)
    run_agent = scope.agent_profile_id if scope and scope.type is MemoryScopeType.AGENT else uuid4()
    assert (
        decide_candidate(
            candidate,
            user_message_ids=frozenset({message_id}),
            run_agent_profile_id=run_agent,
        ).state
        is expected
    )
def test_evaluation_fixture_covers_required_memory_families() -> None:
    fixture = Path(__file__).parent / "fixtures" / "memory" / "evaluation_cases.json"
    cases = json.loads(fixture.read_text())
    assert {
        "family_fact",
        "changing_project_state",
        "meal_context",
        "preference",
        "correction",
        "contradiction",
        "agent_private",
        "shared_user",
        "duplicate_reinforcement",
    } <= {case["category"] for case in cases}
    assert all(
        case["expected_action"] in {action.value for action in MemoryAction} for case in cases
    )
    assert all(case["expected_horizon"] in {"short", "medium", "long"} for case in cases)


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("I prefer concise answers.", MemoryRetentionBasis.PERSONAL),
        ("My Orion project is blocked on review.", MemoryRetentionBasis.PERSONAL),
        (
            "Please remember that Star Wars premiered in 1977.",
            MemoryRetentionBasis.EXPLICIT_REQUEST,
        ),
        ("Can you remember my sister's birthday?", MemoryRetentionBasis.EXPLICIT_REQUEST),
        ("You should remember my sister's birthday.", MemoryRetentionBasis.EXPLICIT_REQUEST),
        (
            "Across my agents, remember my weekly summary preference.",
            MemoryRetentionBasis.EXPLICIT_REQUEST,
        ),
        ("What does save mean?", MemoryRetentionBasis.NONE),
        ("I remember Star Wars premiered in 1977.", MemoryRetentionBasis.NONE),
        ("Save that file to disk.", MemoryRetentionBasis.NONE),
        ("Note the difference between these answers.", MemoryRetentionBasis.NONE),
        ("Store the report in the archive.", MemoryRetentionBasis.NONE),
        ("A sister is a female sibling.", MemoryRetentionBasis.NONE),
        ("Who directed Star Wars?", MemoryRetentionBasis.NONE),
        ("Star Wars premiered in 1977.", MemoryRetentionBasis.NONE),
        ("I think Star Wars premiered in 1977.", MemoryRetentionBasis.NONE),
        ("I heard that Star Wars premiered in 1977.", MemoryRetentionBasis.NONE),
        ("I wonder whether Star Wars premiered in 1977.", MemoryRetentionBasis.NONE),
        ("My understanding is Star Wars premiered in 1977.", MemoryRetentionBasis.NONE),
        ("My sister Nora's birthday is July 14.", MemoryRetentionBasis.PERSONAL),
        ("My favorite trilogy is the original Star Wars trilogy.", MemoryRetentionBasis.PERSONAL),
        ("My car needs servicing.", MemoryRetentionBasis.PERSONAL),
        ("My project is blocked on review.", MemoryRetentionBasis.PERSONAL),
        ("The Sun gives us light.", MemoryRetentionBasis.NONE),
        ("We know Earth is round.", MemoryRetentionBasis.NONE),
    ],
)
def test_retention_basis_requires_a_personal_connection_or_explicit_request(
    content: str, expected: MemoryRetentionBasis
) -> None:
    assert classify_retention_basis(content) is expected


def test_family_birthday_horizon_is_durable_and_open_ended() -> None:
    candidate = replace(
        _candidate(content="My sister Nora's birthday is July 14."),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )

    normalized = normalize_retention_horizon(
        candidate, user_content="My sister Nora's birthday is July 14."
    )

    assert normalized.half_life_days == 365
    assert normalized.valid_to is None


def test_split_family_evidence_normalizes_canonicalized_birthday() -> None:
    candidate = replace(
        _candidate(content="Nora’s birthday is July 14."),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )

    normalized = normalize_retention_horizon(
        candidate, user_content="My sister Nora’s birthday is July 14."
    )

    assert normalized.half_life_days == 365
    assert normalized.valid_to is None


def test_family_subject_fallback_does_not_cross_named_subjects() -> None:
    candidate = replace(
        _candidate(content="Nora’s birthday is July 14."),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )

    normalized = normalize_retention_horizon(
        candidate,
        user_content="My sister Nora is planning Alice’s birthday on July 14.",
    )

    assert normalized.half_life_days == 30
    assert normalized.valid_to == NOW + timedelta(days=30)


@pytest.mark.parametrize(
    "user_content",
    [
        "My sister Nora's birthday is July 15.",
        "My sister Nora's birthday is June 14.",
    ],
)
def test_family_subject_fallback_requires_exact_event_date(user_content: str) -> None:
    candidate = replace(
        _candidate(content="Nora's birthday is July 14."),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )

    normalized = normalize_retention_horizon(candidate, user_content=user_content)

    assert normalized.half_life_days == 30
    assert normalized.valid_to == NOW + timedelta(days=30)


def test_family_anniversary_horizon_is_durable_and_open_ended() -> None:
    candidate = replace(
        _candidate(content="My parents’ anniversary is June 2."),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )

    normalized = normalize_retention_horizon(
        candidate, user_content="My parents’ anniversary is June 2."
    )

    assert normalized.half_life_days == 365
    assert normalized.valid_to is None


@pytest.mark.parametrize(
    "content",
    [
        "My sister's birthday party is Saturday.",
        "My sister's birthday is Saturday.",
        "The project deadline is tomorrow.",
    ],
)
def test_family_horizon_normalizer_preserves_transient_or_unrelated_candidates(
    content: str,
) -> None:
    candidate = replace(
        _candidate(content=content),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )

    normalized = normalize_retention_horizon(
        candidate, user_content="My sister's birthday is July 14."
    )

    assert normalized.half_life_days == 30
    assert normalized.valid_to == NOW + timedelta(days=30)


@pytest.mark.asyncio
async def test_family_birthday_provider_short_horizon_is_normalized_before_commit() -> None:
    message_id = uuid4()
    candidate = replace(
        _candidate(message_id=message_id, content="My sister Nora's birthday is July 14."),
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
    )
    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository, _Inference(candidate), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())

    result = await processor.process(
        job,
        user_content="My sister Nora's birthday is July 14.",
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )

    assert result.state is CandidateState.ACCEPTED
    assert result.half_life_days == 365
    assert result.valid_to is None
    record = next(iter(repository.memories.values()))
    revision = record.revisions[0]
    assert revision.half_life_days == 365
    assert revision.valid_to is None


def test_memory_command_factory_is_deterministic_identifier_only_and_owner_bound() -> None:
    conversation_id = uuid4()
    user_message_id = uuid4()
    assistant_message_id = uuid4()
    agent_revision_id = uuid4()
    run_id = uuid4()
    conversation = Conversation(
        ISSUER,
        OWNER,
        "Memory command test",
        uuid4(),
        agent_revision_id,
        "fake",
        id=conversation_id,
    )
    run = Run(
        conversation_id,
        user_message_id,
        agent_revision_id,
        uuid4(),
        "fake",
        "fake",
        id=run_id,
        status=RunStatus.COMPLETED,
        attempt_id=uuid4(),
    )
    assistant = Message(
        conversation_id,
        MessageRole.ASSISTANT,
        "private answer content",
        MessageState.COMPLETE,
        run_id,
        id=assistant_message_id,
    )
    command = memory_command_factory(conversation, run, assistant)
    assert command.topic == "aura.memory.process.v1"
    assert command.run_id == run_id
    assert command.conversation_id == conversation_id
    assert command.identifier("jobId") == str(run_id)
    assert command.identifier("userMessageId") == str(user_message_id)
    assert command.identifier("assistantMessageId") == str(assistant_message_id)
    assert command.identifier("agentRevisionId") == str(agent_revision_id)
    wire = command.wire_payload().decode()
    assert "private answer content" not in wire
    assert "evidence" not in wire
    assert memory_command_factory(conversation, run, assistant).id == command.id


def test_memory_command_payload_rejects_version_and_identifier_shape_mismatches() -> None:
    command = memory_command_factory(
        Conversation(ISSUER, OWNER, "test", uuid4(), uuid4(), "fake", id=uuid4()),
        Run(
            uuid4(), uuid4(), uuid4(), uuid4(), "fake", "fake", id=uuid4(),
            status=RunStatus.COMPLETED, attempt_id=uuid4(),
        ),
        Message(
            uuid4(), MessageRole.ASSISTANT, "answer", MessageState.COMPLETE,
            uuid4(), id=uuid4(),
        ),
    )
    payload = command.payload()
    for mismatch in (
        {**payload, "schemaVersion": 2},
        {key: value for key, value in payload.items() if key != "jobId"},
        {**payload, "unexpected": "value"},
        {**payload, "jobId": "not-a-uuid"},
    ):
        with pytest.raises(MemoryValidationError):
            MemoryProcessingCommand.from_payload(mismatch)


def test_memory_command_from_outbox_accepts_integer_schema_and_validates_identifiers() -> None:
    conversation_id = uuid4()
    run_id = uuid4()
    agent_revision_id = uuid4()
    command = memory_command_factory(
        Conversation(ISSUER, OWNER, "test", uuid4(), agent_revision_id, "fake", id=conversation_id),
        Run(
            conversation_id,
            uuid4(),
            agent_revision_id,
            uuid4(),
            "fake",
            "fake",
            id=run_id,
            status=RunStatus.COMPLETED,
        ),
        Message(
            conversation_id,
            MessageRole.ASSISTANT,
            "answer",
            MessageState.COMPLETE,
            run_id,
            id=uuid4(),
        ),
    )

    parsed = MemoryProcessingCommand.from_outbox(command)
    assert parsed.schema_version == 1
    assert parsed.command_id == command.id
    assert parsed.job_id == run_id
    with pytest.raises(MemoryValidationError):
        MemoryProcessingCommand.from_outbox(replace(command, schema_version=2))

    class _MalformedOutbox:
        topic = command.topic
        id = command.id
        run_id = command.run_id
        conversation_id = command.conversation_id
        correlation_id = command.correlation_id
        causation_id = command.causation_id
        created_at = command.created_at
        attempt_id = command.attempt_id
        generation_id = command.generation_id

        def __init__(self, value: object) -> None:
            self.value = value

        def identifier(self, name: str) -> object:
            return self.value if name == "jobId" else command.identifier(name)

    for malformed in ("", uuid4(), "not-a-uuid"):
        with pytest.raises(MemoryValidationError):
            MemoryProcessingCommand.from_outbox(_MalformedOutbox(malformed))


@pytest.mark.asyncio
async def test_memory_store_candidate_lookup_for_job_is_owner_scoped_and_latest() -> None:
    store = MemoryStore(clock=lambda: NOW)
    job_id = uuid4()
    first = replace(_candidate(), job_id=job_id, created_at=NOW)
    second = replace(_candidate(), job_id=job_id, created_at=NOW + timedelta(seconds=1))
    foreign = replace(_candidate(), job_id=job_id, subject="another-owner")
    store.candidates.update({first.id: first, second.id: second, foreign.id: foreign})

    assert await store.get_candidate_for_job(job_id, ISSUER, OWNER) == second
    assert await store.get_candidate_for_job(job_id, ISSUER, "another-owner") == foreign
    assert await store.get_candidate_for_job(job_id, ISSUER, "missing-owner") is None


@pytest.mark.asyncio
async def test_rejected_extraction_is_omitted_from_run_activity() -> None:
    store = MemoryStore(clock=lambda: NOW)
    run_id = uuid4()
    job = MemoryProcessingJob(
        uuid4(), ISSUER, OWNER, run_id, uuid4(), status=ProcessingJobStatus.COMPLETED
    )
    candidate = replace(
        _candidate(),
        job_id=job.id,
        content=None,
        kind=None,
        scope=None,
        confidence=0.0,
        importance=None,
        half_life_days=None,
        state=CandidateState.REJECTED,
        decision_reason="invalid_provider_output",
    )
    store.processing_jobs[job.id] = job
    store.candidates[candidate.id] = candidate

    activity = await store.get_run_memory_activity(run_id, ISSUER, OWNER)

    assert activity.items == ()
    assert activity.processing_status == "settled"


@pytest.mark.asyncio
async def test_terminal_rejected_activity_has_no_candidate_identifier() -> None:
    class Publisher:
        def __init__(self) -> None:
            self.events: list[RunEvent] = []

        async def publish(self, event: RunEvent) -> None:
            self.events.append(event)

    command = MemoryProcessingCommand(
        uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4(), uuid4()
    )
    candidate = replace(
        _candidate(),
        job_id=command.job_id,
        state=CandidateState.REJECTED,
        decision_reason="not_personal",
    )
    settlement = type("Settlement", (), {"candidate": candidate})()
    publisher = Publisher()

    await emit_memory_activity(
        publisher,
        MemoryStore(clock=lambda: NOW),
        MetadataMetrics(),
        command,
        "completed",
        settlement,
    )

    data = publisher.events[0].data
    assert data["status"] == "completed"
    assert data["action"] == "queued_for_review"
    assert data["candidateId"] is None
    assert data["memoryId"] is None
    assert data["memoryRevisionId"] is None


@pytest.mark.asyncio
async def test_provider_never_receives_database_message_ids_and_common_token_claim_requires_review(
) -> None:
    message_id = uuid4()
    processor = MemoryProcessingService(
        MemoryStore(clock=lambda: NOW),
        _Inference(
            {
                "action": "create",
                "content": "The weather is sunny today",
                "kind": "semantic",
                "scope_type": "agent",
                "agent_profile_id": str(uuid4()),
                "confidence": 0.99,
                "grounded_evidence_handles": ["evidence-0"],
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_revision_id=CURRENT_AGENT,
        agent_profile_id=CURRENT_AGENT,
    )
    result = await processor.process(
        job,
        user_content="Please explain the weather forecast.",
        assistant_content="The weather is sunny today.",
        user_message_ids=frozenset({message_id}),
    )
    assert result.state is CandidateState.REJECTED
    # The capture object is intentionally inspected after execution: raw
    # persisted message identifiers must never be serialized into provider
    # input; evidence is represented by bounded handles instead.
    calls = cast(_Inference, processor.inference).calls
    assert calls
    assert str(message_id) not in repr(calls[0])


@pytest.mark.asyncio
async def test_complete_world_or_assistant_only_proposals_fail_personal_retention_gate() -> None:
    class WorldFactInference:
        async def infer(self, request: StructuredInferenceRequest) -> dict[str, object]:
            segments = cast(list[Mapping[str, object]], request.input["evidence_segments"])
            return {
                "action": "create",
                "content": "Star Wars premiered in 1977.",
                "kind": "semantic",
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.99,
                "importance": 0.8,
                "half_life_days": 365,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "personal",
                "grounded_evidence_handles": [str(segments[0]["handle"])],
                "related_memory_id": None,
            }

    for user_content, assistant_content in (
        ("Star Wars premiered in 1977.", "Understood."),
        ("What year did Star Wars premiere?", "Star Wars premiered in 1977."),
        ("Save that file to disk.", "I saved the file."),
        ("A sister is a female sibling.", "That is correct."),
    ):
        repository = MemoryStore(clock=lambda: NOW)
        processor = MemoryProcessingService(
            repository,
            WorldFactInference(),
            _Embedding(),
            clock=lambda: NOW,
        )
        await processor.configure_models(
            MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
        )
        job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())

        result = await processor.process(
            job,
            user_content=user_content,
            assistant_content=assistant_content,
            user_message_ids=frozenset({uuid4()}),
        )

        assert result.state is CandidateState.REJECTED
        assert result.decision_reason == "not_personal"
        assert await repository.list_memories(ISSUER, OWNER) == []


@pytest.mark.asyncio
async def test_shared_candidate_conflicts_with_private_memory_across_scopes() -> None:
    class SharedPreferenceInference:
        async def infer(self, request: StructuredInferenceRequest) -> dict[str, object]:
            segments = cast(list[Mapping[str, object]], request.input["evidence_segments"])
            return {
                "action": "create",
                "content": "I prefer concise answers.",
                "kind": "preference",
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.99,
                "importance": 0.8,
                "half_life_days": 365,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "explicit_request",
                "grounded_evidence_handles": [str(segments[0]["handle"])],
                "related_memory_id": None,
            }

    current_time = [NOW]
    repository = MemoryStore(clock=lambda: current_time[0])
    await repository.create_memory(
        ISSUER,
        OWNER,
        content="I prefer concise answers.",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        confidence=0.9,
        importance=0.8,
        half_life_days=365,
    )
    for index in range(250):
        current_time[0] += timedelta(seconds=1)
        await repository.create_memory(
            ISSUER,
            OWNER,
            content=f"Unrelated decoy memory {index}.",
            kind=MemoryKind.SEMANTIC,
            scope=MemoryScope(MemoryScopeType.AGENT, uuid4()),
            confidence=0.9,
            importance=0.5,
            half_life_days=30,
        )
    processor = MemoryProcessingService(
        repository, SharedPreferenceInference(), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    message_id = uuid4()
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    result = await processor.process(
        job,
        user_content="Across my agents, remember that I prefer concise answers.",
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.REVIEW
    assert result.decision_reason == "conflict"
    assert len(repository.memories) == 251


def test_server_evidence_binding_rejects_owner_run_message_and_digest_mismatches() -> None:
    run_id = uuid4()
    conversation_id = uuid4()
    user_id, assistant_id, agent_revision_id = uuid4(), uuid4(), uuid4()
    digest = hashlib.sha256(b"authenticated user evidence").hexdigest()
    job = MemoryProcessingJob(
        uuid4(),
        ISSUER,
        OWNER,
        run_id,
        conversation_id,
        agent_revision_id=agent_revision_id,
        user_message_ids=(user_id,),
        assistant_message_ids=(assistant_id,),
        evidence_digest=digest,
    )
    evidence = MemoryTurnEvidence(
        ISSUER,
        OWNER,
        run_id,
        conversation_id,
        agent_revision_id,
        (user_id,),
        (assistant_id,),
        "authenticated user evidence",
        "assistant context",
        digest,
    )
    evidence.validate_for(job)
    for bad in (
        replace(evidence, issuer="https://foreign.example"),
        replace(evidence, run_id=uuid4()),
        replace(evidence, user_message_ids=(uuid4(),)),
        replace(evidence, evidence_digest="f" * 64),
    ):
        with pytest.raises(MemoryValidationError):
            bad.validate_for(job)


@pytest.mark.asyncio
async def test_completed_turn_stages_one_memory_command_without_provider_work() -> None:
    outbox = InMemoryOutbox()
    store = ConversationStore(default_model="fake")
    store.set_memory_command_factory(memory_command_factory, outbox)
    models = (ModelDescriptor("fake", "Fake", "fake", ("chat",)),)
    _conversation, _, run = await store.create(
        ISSUER,
        OWNER,
        "Remember this completed turn.",
        "fake",
        models,
        str(uuid4()),
    )
    claim = await store.start_run(run.id, worker_id=uuid4())
    await store.append_assistant(
        run.id,
        "A completed answer.",
        MessageState.COMPLETE,
        attempt_id=claim.run.attempt_id,
    )
    await store.finish_run(run.id, RunStatus.COMPLETED, attempt_id=claim.run.attempt_id)
    command = await outbox.receive()
    assert command.topic == "aura.memory.process.v1"
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(outbox.receive(), timeout=0.01)


@pytest.mark.asyncio
async def test_factory_envelope_reaches_worker_and_ack_follows_durable_settlement() -> None:
    conversation_id, run_id = uuid4(), uuid4()
    user_message_id, assistant_message_id = uuid4(), uuid4()
    agent_revision_id, attempt_id = uuid4(), uuid4()
    conversation = Conversation(
        ISSUER, OWNER, "Memory command", uuid4(), agent_revision_id, "fake", id=conversation_id
    )
    run = Run(
        conversation_id,
        user_message_id,
        agent_revision_id,
        uuid4(),
        "fake",
        "fake",
        id=run_id,
        status=RunStatus.COMPLETED,
        attempt_id=attempt_id,
    )
    assistant = Message(
        conversation_id,
        MessageRole.ASSISTANT,
        "The owner prefers concise answers.",
        MessageState.COMPLETE,
        run_id,
        id=assistant_message_id,
    )
    command = memory_command_factory(conversation, run, assistant)
    content = "The owner prefers concise answers."
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    job = MemoryProcessingJob(
        run_id,
        ISSUER,
        OWNER,
        run_id,
        conversation_id,
        correlation_id=attempt_id,
        causation_id=run_id,
        agent_revision_id=agent_revision_id,
        agent_profile_id=CURRENT_AGENT,
        user_message_ids=(user_message_id,),
        assistant_message_ids=(assistant_message_id,),
        evidence_digest=hashlib.sha256(content.encode()).hexdigest(),
    )
    repository.processing_jobs[job.id] = job
    processor = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=user_message_id)),
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(loaded, content, "Understood."),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    processor.jobs[(ISSUER, OWNER, run_id)] = job

    class Consumer:
        delivery_count = 1

        def __init__(self) -> None:
            self.acknowledged = False
            self.nacks: list[str] = []
            self.rejections: list[str] = []

        async def receive(self) -> object:
            return command

        async def ack(self) -> None:
            self.acknowledged = True

        async def nack(self, **kwargs: object) -> None:
            self.nacks.append(str(kwargs.get("error_class")))

        async def reject(self, **kwargs: object) -> None:
            self.rejections.append(str(kwargs.get("error_class")))

    consumer = Consumer()
    await run_memory_once(processor, consumer)  # type: ignore[arg-type]
    assert consumer.acknowledged is True, (consumer.nacks, consumer.rejections)
    assert consumer.nacks == []
    assert consumer.rejections == []
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED
    redelivery = Consumer()
    await run_memory_once(processor, redelivery)  # type: ignore[arg-type]
    assert redelivery.acknowledged is True
    assert redelivery.nacks == []
    assert redelivery.rejections == []
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED


@pytest.mark.asyncio
async def test_invalid_provider_output_is_rejected_but_provider_outage_is_retryable() -> None:
    user_message_id = uuid4()
    for result, error, expected_state in (
        ({"action": "not-a-real-action"}, None, CandidateState.REJECTED),
        ({"action": "create", "content": "x" * 40000}, None, CandidateState.REJECTED),
        (None, RuntimeError("provider unavailable"), CandidateState.RETRYABLE),
    ):
        repository = MemoryStore(clock=lambda: NOW)
        inference = _Inference(result, error=error)
        processor = MemoryProcessingService(repository, inference, _Embedding(), clock=lambda: NOW)
        await processor.configure_models(
            MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
        )
        job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
        candidate = await processor.process(
            job,
            user_content="The owner prefers concise answers.",
            assistant_content="Understood.",
            user_message_ids=frozenset({user_message_id}),
            run_agent_profile_id=CURRENT_AGENT,
        )
        assert candidate.state is expected_state
        assert await repository.list_memories(ISSUER, OWNER) == []


@pytest.mark.asyncio
async def test_schema_valid_domain_invalid_output_settles_processing_job() -> None:
    message_id = uuid4()
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    # The transport/schema accepts this shape, but a create action without
    # canonical content is invalid at the memory domain boundary.
    inference = _Inference({"action": "create", "confidence": 0.99})
    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(loaded, "A durable fact.", "Noted."),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER,
            OWNER,
            "extractor",
            "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_profile_id=CURRENT_AGENT,
    )

    candidate = await processor.process_job(job.id)

    assert candidate is not None
    assert candidate.state is CandidateState.REJECTED
    assert candidate.decision_reason == "invalid_provider_output"
    assert candidate.content is None
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED
    assert repository.processing_jobs[job.id].last_error_class is None
    assert repository.memories == {}


@pytest.mark.asyncio
async def test_extraction_telemetry_distinguishes_invalid_output_from_provider_retry() -> None:
    async def run_case(
        result: object, error: Exception | None
    ) -> tuple[CandidateState, tuple[object, ...]]:
        metrics = MetadataMetrics()

        def telemetry(**event: object) -> None:
            record_memory_processing(
                metrics,
                component="aura.knowledge.memory_extraction",
                dependency="memory_worker",
                **event,
            )

        processor = MemoryProcessingService(
            MemoryStore(clock=lambda: NOW),
            _Inference(result, error=error),
            _Embedding(),
            clock=lambda: NOW,
            telemetry=telemetry,
        )
        await processor.configure_models(
            MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
        )
        job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
        candidate = await processor.process(
            job,
            user_content="A private durable fact.",
            assistant_content="Noted.",
            user_message_ids=frozenset({uuid4()}),
        )
        return candidate.state, tuple(metrics.snapshot())

    invalid_state, invalid_measurements = await run_case(
        {"action": "create", "confidence": 0.99}, None
    )
    retry_state, retry_measurements = await run_case(None, RuntimeError("provider unavailable"))

    assert invalid_state is CandidateState.REJECTED
    invalid_extraction = [
        item for item in invalid_measurements if item.metric == "memory_extraction_duration_ms"
    ]
    assert len(invalid_extraction) == 1
    assert dict(invalid_extraction[0].dimensions) == {
        "dependency": "memory_worker",
        "error_class": "validation",
        "outcome": "error",
    }
    assert any(
        item.metric == "memory_candidate_outcome"
        and dict(item.dimensions).get("outcome") == "rejected"
        for item in invalid_measurements
    )
    assert any(
        item.metric == "memory_candidate_decision"
        and dict(item.dimensions).get("outcome") == "rejected"
        for item in invalid_measurements
    )
    assert not any(item.metric == "memory_retry_count" for item in invalid_measurements)

    assert retry_state is CandidateState.RETRYABLE
    retry_extraction = [
        item for item in retry_measurements if item.metric == "memory_extraction_duration_ms"
    ]
    assert len(retry_extraction) == 1
    assert dict(retry_extraction[0].dimensions) == {
        "dependency": "memory_worker",
        "error_class": "provider",
        "outcome": "error",
    }
    assert any(item.metric == "memory_processing_errors" for item in retry_measurements)
    assert any(item.metric == "memory_retry_count" for item in retry_measurements)
    rendered = repr(invalid_measurements + retry_measurements)
    assert "A private durable fact." not in rendered
    assert "provider unavailable" not in rendered


def test_foreign_evidence_ids_and_untrusted_categories_fail_closed() -> None:
    local_id, foreign_id = uuid4(), uuid4()
    assert (
        decide_candidate(
            _candidate(message_id=foreign_id),
            user_message_ids=frozenset({local_id}),
        ).state
        is CandidateState.REVIEW
    )
    with pytest.raises(MemoryValidationError):
        MemoryCandidate(
            uuid4(),
            uuid4(),
            ISSUER,
            OWNER,
            "not-an-action",  # type: ignore[arg-type]
            "A fact",
            MemoryKind.SEMANTIC,
            MemoryScope(MemoryScopeType.USER),
            0.9,
        )


def test_candidate_and_action_normalization_ids_are_stable_for_replayed_jobs() -> None:
    job = MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4())
    raw = {
        "action": "create",
        "content": "The owner prefers concise answers.",
        "kind": "preference",
        "scope_type": "agent",
        "agent_profile_id": str(uuid4()),
        "confidence": 0.9,
        "importance": 0.5,
        "half_life_days": 30,
        "valid_to": None,
        "sensitivity": "ordinary",
        "retention_basis": "personal",
        "grounded_evidence_handles": ["user-0"],
        "related_memory_id": None,
    }
    handles = {"user-0": uuid4()}
    job = replace(job, agent_profile_id=CURRENT_AGENT)
    first = MemoryProcessingService._action_from_provider(  # pyright: ignore[reportPrivateUsage]
        raw, job, handles
    )
    second = MemoryProcessingService._action_from_provider(  # pyright: ignore[reportPrivateUsage]
        raw, job, handles
    )
    assert first.id == second.id
    assert first.id != job.id
    assert first.grounded_message_ids == second.grounded_message_ids


@pytest.mark.parametrize(
    "field_update",
    [
        {"related_memory_id": str(uuid4())},
        {"valid_to": 123},
    ],
)
def test_provider_cannot_bind_related_memory_or_non_string_validity(
    field_update: dict[str, object],
) -> None:
    job = replace(
        MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4()),
        agent_profile_id=CURRENT_AGENT,
    )
    raw: dict[str, object] = {
        "action": "create",
        "content": "The owner prefers concise answers.",
        "kind": "preference",
        "scope_type": "agent",
        "agent_profile_id": str(CURRENT_AGENT),
        "confidence": 0.9,
        "importance": 0.5,
        "half_life_days": 30,
        "valid_to": None,
        "sensitivity": "ordinary",
        "retention_basis": "personal",
        "grounded_evidence_handles": ["user-0"],
        "related_memory_id": None,
    }
    raw.update(field_update)

    with pytest.raises(MemoryValidationError):
        MemoryProcessingService._action_from_provider(  # pyright: ignore[reportPrivateUsage]
            raw, job, {"user-0": uuid4()}
        )


@pytest.mark.asyncio
async def test_invented_content_with_a_valid_message_id_is_not_auto_committed() -> None:
    message_id = uuid4()
    repository = MemoryStore(clock=lambda: NOW)
    candidate = _candidate(message_id=message_id, content="The owner secretly moved abroad.")
    processor = MemoryProcessingService(
        repository, _Inference(candidate), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    job = replace(job, agent_profile_id=CURRENT_AGENT)
    result = await processor.process(
        job,
        user_content="I enjoy tea.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert result.state is CandidateState.REVIEW
    assert await repository.list_memories(ISSUER, OWNER) == []


@pytest.mark.asyncio
async def test_sensitive_provider_mislabel_and_unknown_action_fail_closed() -> None:
    message_id = uuid4()
    for provider_result, expected_state in (
        (
            {
                "action": "create",
                "content": "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature",
                "kind": "semantic",
                "scope_type": "user",
                "confidence": 0.99,
                "sensitivity": "ordinary",
                "grounded_message_ids": [str(message_id)],
            },
            CandidateState.REJECTED,
        ),
        (
            {
                "action": "review",
                "content": "password: do-not-persist",
                "kind": "semantic",
                "scope_type": "user",
                "confidence": 0.99,
                "sensitivity": "ordinary",
                "grounded_message_ids": [str(message_id)],
            },
            CandidateState.REJECTED,
        ),
        (
            {
                "action": "create",
                "content": "The owner likes astronomy.",
                "kind": "semantic",
                "scope_type": "user",
                "confidence": 0.99,
                "sensitivity": "credential",
                "grounded_message_ids": [str(message_id)],
            },
            CandidateState.REJECTED,
        ),
        (
            {
                "action": "unknown",
                "content": "A fact",
                "kind": "semantic",
                "scope_type": "user",
                "confidence": 0.99,
                "grounded_message_ids": [str(message_id)],
            },
            CandidateState.REJECTED,
        ),
        (
            {
                "action": "create",
                "content": "A fact with an unrecognized sensitivity label.",
                "kind": "semantic",
                "scope_type": "agent",
                "agent_profile_id": str(CURRENT_AGENT),
                "confidence": 0.99,
                "sensitivity": "not-a-category",
                "grounded_message_ids": [str(message_id)],
            },
            CandidateState.REJECTED,
        ),
    ):
        repository = MemoryStore(clock=lambda: NOW)
        processor = MemoryProcessingService(
            repository, _Inference(provider_result), _Embedding(), clock=lambda: NOW
        )
        await processor.configure_models(
            MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
        )
        job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
        candidate = await processor.process(
            job,
            user_content="A normal user fact.",
            assistant_content="Noted.",
            user_message_ids=frozenset({message_id}),
        )
        assert candidate.state is expected_state
        assert await repository.list_memories(ISSUER, OWNER) == []


@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("The owner's diabetes diagnosis is private.", MemorySensitivity.HEALTH),
        ("The owner's bank account is private.", MemorySensitivity.FINANCE),
        ("The owner's passport number is private.", MemorySensitivity.IDENTITY),
        ("The owner's SSN is private.", MemorySensitivity.IDENTITY),
        ("The owner's intimate relationship is private.", MemorySensitivity.INTIMATE),
        ("The owner's precise location is private.", MemorySensitivity.PRECISE_LOCATION),
        ("The owner's home address is private.", MemorySensitivity.PRECISE_LOCATION),
        ("The owner's API key: abc is private.", MemorySensitivity.CREDENTIAL),
        ("The owner does not have diabetes.", MemorySensitivity.HEALTH),
        ("The owner has severe depression.", MemorySensitivity.HEALTH),
        ("The owner's mortgage balance is private.", MemorySensitivity.FINANCE),
        ("The owner's driver's license number is private.", MemorySensitivity.IDENTITY),
        ("The owner had an abortion.", MemorySensitivity.INTIMATE),
        ("The owner shared an OTP and PIN.", MemorySensitivity.CREDENTIAL),
        ("The owner's recovery seed phrase is private.", MemorySensitivity.CREDENTIAL),
    ],
)
def test_sensitivity_taxonomy_fails_closed_for_categories_and_negation(
    content: str, expected: MemorySensitivity
) -> None:
    assert classify_sensitivity(content) is expected
    message_id = uuid4()
    decision = decide_candidate(
        _candidate(content=content, message_id=message_id),
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert decision.state in {CandidateState.REVIEW, CandidateState.REJECTED}


@pytest.mark.parametrize(
    "secret",
    [
        "Google OAuth bearer ya29.a0ARrdaM_example_token",
        "Slack bot xoxb-123456789012-123456789012-example",
        "npm access npm_abcdefghijklmnopqrstuvwxyz1234567890",
    ],
)
def test_provider_token_families_are_rejected_before_persistence(secret: str) -> None:
    assert classify_sensitivity(secret) is MemorySensitivity.CREDENTIAL
    assert contains_secret(secret)


@pytest.mark.asyncio
async def test_opaque_grounding_handle_cannot_bind_a_foreign_message_id() -> None:
    message_id = uuid4()
    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "create",
                "content": "The owner prefers concise answers.",
                "kind": "preference",
                "scope_type": "agent",
                "agent_profile_id": str(CURRENT_AGENT),
                "confidence": 0.99,
                "grounded_evidence_handles": ["evidence:foreign:user:99"],
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    result = await processor.process(
        job,
        user_content="The owner prefers concise answers. A second opaque segment.",
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert result.state is CandidateState.REJECTED
    assert repository.memories == {}


@pytest.mark.asyncio
async def test_multi_segment_opaque_handles_and_negation_force_contradiction_review() -> None:
    message_id = uuid4()
    calls: list[list[str]] = []

    class ContradictionInference:
        async def infer(self, request: object) -> object:
            payload = cast(Mapping[str, object], getattr(request, "input", {}))
            segments = cast(list[Mapping[str, object]], payload["evidence_segments"])
            handles = [str(segment["handle"]) for segment in segments]
            calls.append(handles)
            return {
                "action": "create",
                "content": "I enjoy coffee.",
                "kind": "preference",
                "scope_type": "agent",
                "agent_profile_id": str(CURRENT_AGENT),
                "confidence": 0.99,
                "importance": 0.5,
                "half_life_days": 30,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "personal",
                "grounded_evidence_handles": [handles[0]],
                "related_memory_id": None,
            }

    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository, ContradictionInference(), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    content = "I do not enjoy coffee. " + ("context segment " * 700) + "I enjoy tea."
    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert result.state is CandidateState.REVIEW
    assert repository.memories == {}
    assert len(calls) == 1 and len(calls[0]) >= 2
    assert str(message_id) not in calls[0][0]


@pytest.mark.asyncio
async def test_absent_configuration_has_no_provider_fallback() -> None:
    inference = _Inference(_candidate())
    processor = MemoryProcessingService(
        MemoryStore(clock=lambda: NOW), inference, _Embedding(), clock=lambda: NOW
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    with pytest.raises(MemoryNotFound):
        await processor.process(
            job,
            user_content="A user fact.",
            assistant_content="Noted.",
            user_message_ids=frozenset({uuid4()}),
        )
    assert inference.calls == []


@pytest.mark.asyncio
async def test_model_configuration_refreshes_durable_selection_after_worker_start() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository, _Inference(_candidate()), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    await repository.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder-v2", version=1),
        expected_version=1,
        dimension=3,
        model_digest="b" * 64,
    )

    refreshed = await processor.model_configuration(ISSUER, OWNER)

    assert refreshed.embedding_model_id == "embedder-v2"


@pytest.mark.asyncio
async def test_structured_request_carries_domain_owned_memory_decision_contract() -> None:
    seen: list[StructuredInferenceRequest] = []

    class CapturingInference:
        async def infer(self, request: StructuredInferenceRequest) -> dict[str, object]:
            seen.append(request)
            handle = str(request.input["evidence_segments"][0]["handle"])  # type: ignore[index]
            return {
                "action": "review",
                "content": "I prefer concise answers.",
                "kind": "preference",
                "scope_type": "user",
                "confidence": 0.7,
                "importance": 0.5,
                "half_life_days": 30,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "personal",
                "grounded_evidence_handles": [handle],
                "agent_profile_id": None,
                "related_memory_id": None,
            }

    processor = MemoryProcessingService(
        MemoryStore(clock=lambda: NOW), CapturingInference(), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())

    result = await processor.process(
        job,
        user_content="I prefer concise answers.",
        assistant_content="Understood.",
        user_message_ids=frozenset({uuid4()}),
    )

    assert result.state is CandidateState.REVIEW
    assert result.action is MemoryAction.CREATE
    assert result.importance == 0.5
    assert result.half_life_days == 30.0
    contract = cast(Mapping[str, object], seen[0].input["decision_contract"])
    assert contract["allowed_actions"] == [item.value for item in MemoryAction]
    instructions = str(contract["instructions"])
    assert "memory-extraction-policy-v2" in instructions
    assert "personal" in instructions and "explicit_request" in instructions
    assert "Assistant context is background only" in instructions
    assert contract["required_for_create_or_review"] == [
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
    ]
    assert contract["scope_guidance"] == {
        "agent": "private to the current agent",
        "user": "shared user memory; policy may require review",
    }


@pytest.mark.asyncio
async def test_provider_review_candidate_is_approvable_without_edit() -> None:
    message_id = uuid4()

    class ReviewInference:
        async def infer(self, request: StructuredInferenceRequest) -> dict[str, object]:
            handle = str(request.input["evidence_segments"][0]["handle"])  # type: ignore[index]
            return {
                "action": "review",
                "content": "I prefer concise answers.",
                "kind": "preference",
                "scope_type": "agent",
                "agent_profile_id": None,
                "confidence": 0.7,
                "importance": 0.5,
                "half_life_days": 30,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "personal",
                "grounded_evidence_handles": [handle],
                "related_memory_id": None,
            }

    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository, ReviewInference(), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )

    candidate = await processor.process(
        job,
        user_content="I prefer concise answers.",
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )

    assert candidate.state is CandidateState.REVIEW
    assert candidate.action is MemoryAction.CREATE
    assert candidate.importance == 0.5
    assert candidate.half_life_days == 30.0
    approved = await repository.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=candidate.version,
        idempotency_key=str(uuid4()),
    )
    assert approved.state is CandidateState.ACCEPTED
    assert approved.action is MemoryAction.CREATE
    assert approved.memory_id is not None


@pytest.mark.asyncio
async def test_legacy_review_action_candidate_is_approvable_with_safe_defaults() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    job = MemoryProcessingJob(
        uuid4(),
        ISSUER,
        OWNER,
        uuid4(),
        uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )
    await repository.enqueue_processing_job(job)
    candidate = MemoryCandidate(
        uuid4(),
        job.id,
        ISSUER,
        OWNER,
        MemoryAction.REVIEW,
        "The owner prefers tea.",
        MemoryKind.PREFERENCE,
        MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        0.9,
        state=CandidateState.REVIEW,
    )
    await repository.persist_candidate(candidate)

    approved = await repository.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key=str(uuid4()),
    )

    assert approved.state is CandidateState.ACCEPTED
    assert approved.action is MemoryAction.CREATE
    assert approved.memory_id is not None


@pytest.mark.asyncio
async def test_approved_candidate_queues_and_replays_embedding_job_repair() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    generation = await repository.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="a" * 64,
    )
    await repository.save_model_configuration(
        ISSUER,
        OWNER,
        MemoryModelConfiguration(
            ISSUER,
            OWNER,
            "extractor",
            "embedder",
            embedding_model_revision="rev-1",
            embedding_generation=generation.id,
        ),
        expected_version=1,
        dimension=3,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    job = MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4())
    await repository.enqueue_processing_job(job)
    candidate = MemoryCandidate(
        uuid4(),
        job.id,
        ISSUER,
        OWNER,
        MemoryAction.REVIEW,
        "The owner prefers tea.",
        MemoryKind.PREFERENCE,
        MemoryScope(MemoryScopeType.USER),
        0.9,
        state=CandidateState.REVIEW,
    )
    await repository.persist_candidate(candidate)
    key = str(uuid4())

    approved = await repository.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key=key,
    )
    assert approved.memory_id is not None
    assert len(repository.embedding_jobs) == 1
    embedding_job_id = next(iter(repository.embedding_jobs))

    # Simulate a pre-fix accepted receipt whose queue row was lost. Replay is
    # the supported repair path and must not create a duplicate job.
    repository.embedding_jobs.clear()
    replay = await repository.approve_candidate(
        ISSUER,
        OWNER,
        candidate.id,
        expected_version=1,
        idempotency_key=key,
    )
    assert replay == approved
    assert set(repository.embedding_jobs) == {embedding_job_id}


@pytest.mark.asyncio
async def test_legacy_credential_labeled_candidate_cannot_be_approved() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    job = MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4())
    await repository.enqueue_processing_job(job)
    candidate = MemoryCandidate(
        uuid4(),
        job.id,
        ISSUER,
        OWNER,
        MemoryAction.REVIEW,
        "The owner likes astronomy.",
        MemoryKind.SEMANTIC,
        MemoryScope(MemoryScopeType.USER),
        0.9,
        sensitivity=MemorySensitivity.CREDENTIAL,
        state=CandidateState.REVIEW,
    )
    await repository.persist_candidate(candidate)

    with pytest.raises(MemoryValidationError, match="credential-like"):
        await repository.approve_candidate(
            ISSUER,
            OWNER,
            candidate.id,
            expected_version=1,
            idempotency_key=str(uuid4()),
        )


@pytest.mark.asyncio
async def test_cross_owner_configuration_and_job_identity_cannot_be_reused() -> None:
    inference = _Inference(_candidate())
    processor = MemoryProcessingService(
        MemoryStore(clock=lambda: NOW), inference, _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    with pytest.raises(MemoryNotFound):
        await processor.model_configuration(ISSUER, "another-owner")
    owner_job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    other_job = await processor.enqueue(
        ISSUER, "another-owner", run_id=owner_job.run_id, conversation_id=owner_job.conversation_id
    )
    assert other_job.id != owner_job.id


@pytest.mark.asyncio
async def test_configuration_rejects_a_generation_owned_by_another_principal() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    foreign = await repository.register_embedding_generation(
        ISSUER,
        "another-owner",
        generation=1,
        model_id="foreign-embedder",
        dimension=3,
        model_digest="a" * 64,
    )
    processor = MemoryProcessingService(
        repository, _Inference(_candidate()), _Embedding(), clock=lambda: NOW
    )
    with pytest.raises(MemoryNotFound):
        await processor.configure_models(
            MemoryModelConfiguration(
                ISSUER,
                OWNER,
                "extractor",
                "embedder",
                embedding_generation=foreign.id,
            )
        )


@pytest.mark.asyncio
async def test_discard_outcome_is_content_free() -> None:
    message_id = uuid4()
    processor = MemoryProcessingService(
        MemoryStore(clock=lambda: NOW),
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "confidence": 0.99,
                "grounded_message_ids": [str(message_id)],
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    candidate = await processor.process(
        job,
        user_content="A meal context.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
    )
    assert candidate.state is CandidateState.REJECTED
    assert all("content" not in outcome for outcome in processor.outcomes)


@pytest.mark.asyncio
async def test_purge_tombstone_blocks_restart_recreate_and_reembedding() -> None:
    message_id = uuid4()
    repository = MemoryStore(clock=lambda: NOW)
    generation = await repository.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="b" * 64,
    )
    processor = MemoryProcessingService(
        repository, _Inference(_candidate(message_id=message_id)), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder", embedding_generation=generation.id
        )
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    record = next(iter(repository.memories.values()))
    if not record.embeddings:
        await repository.queue_embedding_job(
            ISSUER,
            OWNER,
            memory_id=record.id,
            revision_id=record.current_revision_id,
            generation_id=generation.id,
        )
    await repository.purge(
        ISSUER,
        OWNER,
        record.id,
        confirmation="PURGE MEMORY",
        expected_version=record.version,
        idempotency_key="purge-after-job",
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=CURRENT_AGENT,
        authorized_agent_ids=frozenset({CURRENT_AGENT}),
    )
    restarted = MemoryProcessingService(
        repository, _Inference(_candidate(message_id=message_id)), _Embedding(), clock=lambda: NOW
    )
    await restarted.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    with pytest.raises(MemoryIdempotencyConflict):
        await restarted.process(
            job,
            user_content="The owner prefers concise answers.",
            assistant_content="Noted.",
            user_message_ids=frozenset({message_id}),
        )
    assert await repository.list_memories(ISSUER, OWNER) == []


@pytest.mark.asyncio
async def test_purge_fence_wins_when_worker_crashes_between_create_and_link() -> None:
    message_id = uuid4()
    repository = _PurgeDuringLinkStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=message_id)),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    with pytest.raises(RuntimeError, match="between memory creation and link"):
        await processor.process(
            job,
            user_content="The owner prefers concise answers.",
            assistant_content="Noted.",
            user_message_ids=frozenset({message_id}),
            run_agent_profile_id=CURRENT_AGENT,
        )
    assert repository.link_attempts == 1
    assert repository.memories == {}
    with pytest.raises(MemoryIdempotencyConflict):
        await processor.process(
            job,
            user_content="The owner prefers concise answers.",
            assistant_content="Noted.",
            user_message_ids=frozenset({message_id}),
            run_agent_profile_id=CURRENT_AGENT,
        )
    assert repository.memories == {}


@pytest.mark.asyncio
async def test_duplicate_job_delivery_and_repeated_turn_converge_on_one_memory() -> None:
    user_message_id = uuid4()
    candidate = _candidate(message_id=user_message_id)
    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository, _Inference(candidate), _Embedding(), clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    run_id = uuid4()
    job = await processor.enqueue(ISSUER, OWNER, run_id=run_id, conversation_id=uuid4())
    duplicate = await processor.enqueue(ISSUER, OWNER, run_id=run_id, conversation_id=uuid4())
    assert duplicate.id == job.id
    first = await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({user_message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    replay = await processor.process(
        duplicate,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({user_message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert replay.id == first.id
    assert len(repository.memories) == 1


@pytest.mark.asyncio
async def test_distinct_runs_reinforce_one_record_and_append_provenance() -> None:
    class SequenceInference:
        def __init__(self, results: Sequence[object]) -> None:
            self.results = list(results)

        async def infer(self, request: object) -> object:
            del request
            return self.results.pop(0)

    first_id, second_id = uuid4(), uuid4()
    first = _candidate(message_id=first_id, action=MemoryAction.CREATE)
    second = _candidate(message_id=second_id, action=MemoryAction.REINFORCE)
    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository,
        SequenceInference((first, second)),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )

    def provenance(run_id: UUID, message_id: UUID) -> MemoryProvenance:
        return MemoryProvenance(
            uuid4(), "run", conversation_id=uuid4(), run_id=run_id, message_id=message_id
        )
    run_a, run_b = uuid4(), uuid4()
    job_a = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=run_a,
        conversation_id=uuid4(),
        user_message_ids=(first_id,),
        agent_profile_id=CURRENT_AGENT,
    )
    job_b = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=run_b,
        conversation_id=uuid4(),
        user_message_ids=(second_id,),
        agent_profile_id=CURRENT_AGENT,
    )
    await processor.process(
        job_a,
        user_content="The owner prefers concise answers.",
        assistant_content="Understood.",
        user_message_ids=frozenset({first_id}),
        run_agent_profile_id=CURRENT_AGENT,
        provenance=(provenance(run_a, first_id),),
    )
    await processor.process(
        job_b,
        user_content="The owner prefers concise answers.",
        assistant_content="I will keep reinforcing that preference.",
        user_message_ids=frozenset({second_id}),
        run_agent_profile_id=CURRENT_AGENT,
        provenance=(provenance(run_b, second_id),),
    )
    records = list(repository.memories.values())
    assert len(records) == 1
    assert len(records[0].provenance) == 2
    assert records[0].status is MemoryLifecycleStatus.ACTIVE


@pytest.mark.asyncio
async def test_candidate_commit_crash_restarts_as_exact_duplicate_reinforcement() -> None:
    message_id = uuid4()
    first_provenance = MemoryProvenance(uuid4(), "run", run_id=uuid4(), message_id=message_id)
    second_provenance = MemoryProvenance(uuid4(), "run", run_id=uuid4(), message_id=message_id)
    repository = _CrashStore("candidate", clock=lambda: NOW)
    existing = await repository.create_memory(
        ISSUER,
        OWNER,
        content="The owner prefers concise answers.",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        provenance=(first_provenance,),
    )
    worker = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=message_id)),
        _Embedding(),
        clock=lambda: NOW,
    )
    await worker.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await worker.enqueue(
        ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4(),
        user_message_ids=(message_id,), agent_profile_id=CURRENT_AGENT,
    )
    with pytest.raises(RuntimeError, match="injected crash at candidate"):
        await worker.process(
            job,
            user_content="The owner prefers concise answers.",
            assistant_content="Noted.",
            user_message_ids=frozenset({message_id}),
            run_agent_profile_id=CURRENT_AGENT,
            provenance=(second_provenance,),
        )
    repository.crashed = True
    restarted = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=message_id)),
        _Embedding(),
        clock=lambda: NOW,
    )
    await restarted.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    result = await restarted.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
        provenance=(second_provenance,),
    )
    assert result.state is CandidateState.ACCEPTED
    records = list(repository.memories.values())
    assert len(records) == 1 and records[0].id == existing.id
    assert len(records[0].provenance) == 2


@pytest.mark.asyncio
async def test_durable_job_restart_reuses_job_and_idempotent_action() -> None:
    user_message_id = uuid4()
    run_id = uuid4()
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    inference = _Inference(_candidate(message_id=user_message_id))
    first_worker = MemoryProcessingService(repository, inference, _Embedding(), clock=lambda: NOW)
    await first_worker.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await first_worker.enqueue(
        ISSUER,
        OWNER,
        run_id=run_id,
        conversation_id=uuid4(),
        user_message_ids=(user_message_id,),
        agent_revision_id=CURRENT_AGENT,
        agent_profile_id=CURRENT_AGENT,
    )
    restarted = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=user_message_id)),
        _Embedding(),
        clock=lambda: NOW,
        job_loader=lambda job_id: repository.get_processing_job(job_id, ISSUER, OWNER),
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Noted."
        ),
    )
    await restarted.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    result = await restarted.process_job(job.id)
    assert result is not None
    assert len(repository.memories) == 1
    assert len(repository.persisted_candidates) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("crash_at", ["candidate", "action", "embedding", "settle"])
async def test_worker_restart_after_each_durable_commit_boundary_is_idempotent(
    crash_at: str,
) -> None:
    message_id = uuid4()
    repository = _CrashStore(crash_at, clock=lambda: NOW)
    inference = _Inference(_candidate(message_id=message_id))
    embedder = _Embedding()
    generation = await repository.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        dimension=3,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    worker = MemoryProcessingService(
        repository,
        inference,
        embedder,
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Understood."
        ),
    )
    await worker.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder", embedding_generation=generation.id
        )
    )
    job = await worker.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_profile_id=CURRENT_AGENT,
    )
    expected_crash = (RuntimeError, asyncio.CancelledError)
    with pytest.raises(expected_crash, match=f"injected crash at {crash_at}"):
        await worker.process_job(job.id)
    crashed = repository.processing_jobs[job.id]
    repository.processing_jobs[job.id] = replace(
        crashed,
        status=ProcessingJobStatus.RETRYABLE,
        lease_id=None,
        lease_until=None,
    )
    restarted = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=message_id)),
        _Embedding(),
        clock=lambda: NOW,
        job_loader=lambda job_id: repository.get_processing_job(job_id, ISSUER, OWNER),
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Understood."
        ),
    )
    await restarted.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder", embedding_generation=generation.id
        )
    )
    result = await restarted.process_job(job.id)
    assert result is not None
    assert len(repository.memories) == 1
    assert len(repository.persisted_candidates) == 1


@pytest.mark.asyncio
async def test_unconfigured_job_parks_before_evidence_loader_or_provider() -> None:
    repository = _DurableMemoryStore(clock=lambda: NOW)
    inference = _Inference(_candidate())
    loader_calls: list[UUID] = []

    async def evidence_loader(job: MemoryProcessingJob) -> tuple[str, str]:
        loader_calls.append(job.id)
        return "A user fact.", "Noted."

    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=evidence_loader,
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    assert await processor.process_job(job.id) is None
    assert inference.calls == []
    assert loader_calls == []
    parked = repository.processing_jobs[job.id]
    assert parked.status is ProcessingJobStatus.RETRYABLE
    assert parked.last_error_class == "unconfigured"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("candidate", "expected_state"),
    [
        (_candidate(action=MemoryAction.IGNORE), CandidateState.REJECTED),
        (_candidate(confidence=0.50, message_id=uuid4()), CandidateState.REJECTED),
        (_candidate(scope=MemoryScope(MemoryScopeType.USER)), CandidateState.REVIEW),
    ],
)
async def test_review_reject_and_ignore_branches_settle_without_memory_write(
    candidate: MemoryCandidate, expected_state: CandidateState
) -> None:
    message_id = next(iter(candidate.grounded_message_ids), uuid4())
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    processor = MemoryProcessingService(
        repository,
        _Inference(candidate),
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Understood."
        ),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_profile_id=CURRENT_AGENT,
    )
    result = await processor.process_job(job.id)
    assert result is not None and result.state is expected_state
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED
    assert repository.memories == {}
    assert all("content" not in outcome for outcome in processor.outcomes)


@pytest.mark.asyncio
async def test_provider_ignore_reinforces_existing_direct_family_fact() -> None:
    """A conservative ignore must not lose a repeat of an exact durable fact."""

    content = "My sister’s birthday is on March 14."
    repository = MemoryStore(clock=lambda: NOW)
    metrics = MetadataMetrics()

    def telemetry(**event: object) -> None:
        record_memory_processing(
            metrics,
            component="aura.knowledge.memory_extraction",
            dependency="memory_worker",
            **event,
        )

    existing = await repository.create_memory(
        ISSUER,
        OWNER,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content=content,
        confidence=0.95,
        importance=0.8,
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
        idempotency_key="seed-family-fact",
    )
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "kind": None,
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.0,
                "importance": None,
                "half_life_days": None,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "none",
                "grounded_evidence_handles": [],
                "related_memory_id": None,
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
        telemetry=telemetry,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    message_id = uuid4()
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )

    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.ACCEPTED
    assert result.action is MemoryAction.REINFORCE
    assert result.decision_reason == "provider_ignore_reinforcement"
    assert result.half_life_days == 365
    assert result.valid_to is None
    assert len(repository.memories) == 1
    reinforced = repository.memories[existing.id]
    assert reinforced.reinforced_at == NOW
    assert reinforced.current_revision.half_life_days == 365
    assert reinforced.current_revision.valid_to is None
    assert len(reinforced.revisions) == 2
    outcome = next(item for item in repository.outcomes if item["action"] == "reinforce")
    assert "error_class" not in outcome
    measurements = metrics.snapshot()
    fallback_measurement = next(
        item for item in measurements if item.metric == "memory_fallback_outcome"
    )
    assert dict(fallback_measurement.dimensions)["outcome"] == "fallback"
    assert "error_class" not in dict(fallback_measurement.dimensions)
    action_measurement = next(
        item for item in measurements if item.metric == "memory_action_outcome"
    )
    assert "error_class" not in dict(action_measurement.dimensions)
    assert metrics.stats().rejected == 0
    assert content not in repr(outcome)
    assert content not in repr(fallback_measurement)
    assert content not in repr(action_measurement)


@pytest.mark.asyncio
async def test_provider_ignore_does_not_create_new_family_fact() -> None:
    content = "My sister’s birthday is on March 14."
    repository = MemoryStore(clock=lambda: NOW)
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "kind": None,
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.0,
                "importance": None,
                "half_life_days": None,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "none",
                "grounded_evidence_handles": [],
                "related_memory_id": None,
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )

    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({uuid4()}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.REJECTED
    assert result.action is MemoryAction.IGNORE
    assert repository.memories == {}


@pytest.mark.asyncio
async def test_provider_ignore_binds_reinforcement_to_active_target() -> None:
    content = "My sister’s birthday is on March 14."
    repository = MemoryStore(clock=lambda: NOW)
    active = await repository.create_memory(
        ISSUER,
        OWNER,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content=content,
        confidence=0.95,
        importance=0.8,
        half_life_days=365,
        idempotency_key="active-family-fact",
    )
    historical = await repository.create_memory(
        ISSUER,
        OWNER,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content=content,
        confidence=0.95,
        importance=0.8,
        half_life_days=365,
        idempotency_key="historical-family-fact",
    )
    historical_version = historical.version
    historical_reinforced_at = historical.reinforced_at
    await repository.set_status(
        ISSUER,
        OWNER,
        historical.id,
        status=MemoryLifecycleStatus.ARCHIVED,
        expected_version=historical.version,
        scope_type=MemoryScopeType.USER,
    )
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "kind": None,
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.0,
                "importance": None,
                "half_life_days": None,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "none",
                "grounded_evidence_handles": [],
                "related_memory_id": None,
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )
    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({uuid4()}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.ACCEPTED
    assert repository.memories[active.id].reinforced_at == NOW
    assert repository.memories[historical.id].status is MemoryLifecycleStatus.ARCHIVED
    assert repository.memories[historical.id].version == historical_version + 1
    assert repository.memories[historical.id].reinforced_at == historical_reinforced_at


@pytest.mark.asyncio
async def test_provider_ignore_disappearing_target_fails_closed_without_create() -> None:
    content = "My sister’s birthday is on March 14."

    class DisappearingFamilyStore(MemoryStore):
        def __init__(self) -> None:
            super().__init__(clock=lambda: NOW)
            self.target_id: UUID | None = None
            self.list_calls = 0

        async def list_memories(
            self, issuer: str, subject: str, filters: Any = None
        ) -> list[Any]:
            values = await super().list_memories(issuer, subject, filters)
            self.list_calls += 1
            if self.list_calls == 1 and self.target_id is not None:
                self.memories.pop(self.target_id, None)
            return values

    repository = DisappearingFamilyStore()
    existing = await repository.create_memory(
        ISSUER,
        OWNER,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content=content,
        confidence=0.95,
        importance=0.8,
        half_life_days=365,
        idempotency_key="disappearing-family-fact",
    )
    repository.target_id = existing.id
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "kind": None,
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.0,
                "importance": None,
                "half_life_days": None,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "none",
                "grounded_evidence_handles": [],
                "related_memory_id": None,
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )
    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({uuid4()}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.REJECTED
    assert result.decision_reason == "reinforcement_target_unavailable"
    assert repository.memories == {}
    assert not any(item["action"] == "create" for item in repository.outcomes)


@pytest.mark.asyncio
@pytest.mark.parametrize("transition", ["archive", "version"])
async def test_provider_ignore_transition_before_reinforce_fails_closed(
    transition: str,
) -> None:
    content = "My sister’s birthday is on March 14."

    class TransitioningFamilyStore(MemoryStore):
        target_id: UUID | None = None

        async def reinforce_memory(
            self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
        ) -> MemoryRecord:
            assert self.target_id == memory_id
            if transition == "archive":
                self.memories[memory_id].status = MemoryLifecycleStatus.ARCHIVED
            else:
                self.memories[memory_id].version += 1
            return await super().reinforce_memory(issuer, subject, memory_id, **kwargs)

    repository = TransitioningFamilyStore(clock=lambda: NOW)
    existing = await repository.create_memory(
        ISSUER,
        OWNER,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content=content,
        confidence=0.95,
        importance=0.8,
        half_life_days=365,
        idempotency_key="transition-family-fact",
    )
    repository.target_id = existing.id
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "kind": None,
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.0,
                "importance": None,
                "half_life_days": None,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "none",
                "grounded_evidence_handles": [],
                "related_memory_id": None,
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )

    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({uuid4()}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.REJECTED
    assert result.decision_reason == "reinforcement_target_unavailable"
    if transition == "archive":
        assert repository.memories[existing.id].status is MemoryLifecycleStatus.ARCHIVED
    else:
        assert repository.memories[existing.id].status is MemoryLifecycleStatus.ACTIVE


@pytest.mark.asyncio
@pytest.mark.parametrize("revision_race", ["missing", "conflict", "conflict_correction"])
async def test_provider_ignore_revision_race_has_no_accepted_orphan(
    revision_race: str,
) -> None:
    content = "My sister’s birthday is on March 14."
    metrics = MetadataMetrics()

    def telemetry(**event: object) -> None:
        record_memory_processing(
            metrics,
            component="aura.knowledge.memory_extraction",
            dependency="memory_worker",
            **event,
        )

    class RevisionRaceFamilyStore(MemoryStore):
        async def revise_memory(
            self, issuer: str, subject: str, memory_id: UUID, **kwargs: object
        ) -> MemoryRecord:
            if revision_race == "missing":
                self.memories.pop(memory_id, None)
                raise MemoryNotFound("memory disappeared during horizon revision")
            if revision_race == "conflict_correction":
                corrected = await super().revise_memory(
                    issuer,
                    subject,
                    memory_id,
                    content="Owner corrected this family fact.",
                    half_life_days=30,
                    valid_to=NOW + timedelta(days=45),
                    expected_version=kwargs["expected_version"],
                    idempotency_key="owner-correction-before-fallback",
                )
                del corrected
                raise MemoryVersionConflict("owner correction won the horizon race")
            raise MemoryVersionConflict("memory horizon revision changed")

    repository = RevisionRaceFamilyStore(clock=lambda: NOW)
    await repository.create_memory(
        ISSUER,
        OWNER,
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        content=content,
        confidence=0.95,
        importance=0.8,
        half_life_days=30,
        valid_to=NOW + timedelta(days=30),
        idempotency_key="revision-race-family-fact",
    )
    processor = MemoryProcessingService(
        repository,
        _Inference(
            {
                "action": "ignore",
                "content": None,
                "kind": None,
                "scope_type": "user",
                "agent_profile_id": None,
                "confidence": 0.0,
                "importance": None,
                "half_life_days": None,
                "valid_to": None,
                "sensitivity": "ordinary",
                "retention_basis": "none",
                "grounded_evidence_handles": [],
                "related_memory_id": None,
            }
        ),
        _Embedding(),
        clock=lambda: NOW,
        telemetry=telemetry,
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        agent_profile_id=CURRENT_AGENT,
    )

    result = await processor.process(
        job,
        user_content=content,
        assistant_content="Understood.",
        user_message_ids=frozenset({uuid4()}),
        run_agent_profile_id=CURRENT_AGENT,
        allow_shared_user_promotion=True,
    )

    assert result.state is CandidateState.REJECTED
    assert result.decision_reason == "reinforcement_target_unavailable"
    assert result.content is None
    assert metrics.stats().rejected == 0
    assert any(item.metric == "memory_candidate_outcome" for item in metrics.snapshot())
    assert any(item.metric == "memory_job_outcome" for item in metrics.snapshot())
    if revision_race == "missing":
        assert repository.memories == {}
    elif revision_race == "conflict":
        record = next(iter(repository.memories.values()))
        assert record.status is MemoryLifecycleStatus.ACTIVE
        assert record.current_revision.half_life_days == 30
        assert record.current_revision.valid_to is not None
    else:
        record = next(iter(repository.memories.values()))
        assert record.current_revision.content == "Owner corrected this family fact."
        assert record.current_revision.half_life_days == 30
        assert record.current_revision.valid_to == NOW + timedelta(days=45)
        assert len(record.revisions) == 2


@pytest.mark.asyncio
async def test_credential_bearing_server_evidence_is_rejected_before_provider() -> None:
    message_id = uuid4()
    credential = "The owner password: secret-value must not be remembered."
    digest = hashlib.sha256(credential.encode()).hexdigest()
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    inference = _Inference(_candidate(message_id=message_id))
    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(loaded, credential, "Understood."),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        evidence_digest=digest,
        agent_profile_id=CURRENT_AGENT,
    )
    result = await processor.process_job(job.id)
    assert inference.calls == []
    assert result is not None and result.state is CandidateState.REJECTED
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED
    stored = next(iter(repository.persisted_candidates.values()))
    assert stored.content is None
    assert stored.decision_reason == "credential"


@pytest.mark.asyncio
async def test_processing_lease_is_single_owner_and_requires_fresh_capability() -> None:
    repository = _DurableMemoryStore(clock=lambda: NOW)
    job = MemoryProcessingJob(uuid4(), ISSUER, OWNER, uuid4(), uuid4())
    await repository.enqueue_processing_job(job)
    first = await repository.claim_processing_job_by_id(job.id, ISSUER, OWNER)
    assert first is not None and first.lease_id is not None
    assert await repository.claim_processing_job_by_id(job.id, ISSUER, OWNER) is None
    with pytest.raises(MemoryValidationError, match="stale"):
        await repository.settle_processing_job(job.id, uuid4())
    settled = await repository.settle_processing_job(job.id, first.lease_id)
    assert settled.status is ProcessingJobStatus.COMPLETED
    with pytest.raises(MemoryValidationError, match="stale"):
        await repository.settle_processing_job(job.id, first.lease_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("allow_shared_user_promotion", "expected_state"),
    [
        (True, CandidateState.ACCEPTED),
        (False, CandidateState.REVIEW),
    ],
)
async def test_direct_process_job_uses_claimed_policy_snapshot(
    allow_shared_user_promotion: bool, expected_state: CandidateState
) -> None:
    """A direct worker claim must retain the pinned promotion capability."""

    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    message_id = uuid4()
    processor = MemoryProcessingService(
        repository,
        _Inference(_candidate(scope=MemoryScope(MemoryScopeType.USER), message_id=message_id)),
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Noted."
        ),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER,
            OWNER,
            "extractor",
            "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_profile_id=CURRENT_AGENT,
        memory_policy_revision_id=uuid4(),
        allow_shared_user_promotion=allow_shared_user_promotion,
    )
    result = await processor.process_job(job.id)
    assert result is not None and result.state is expected_state
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED
    claimed = repository.processing_jobs[job.id]
    assert claimed.allow_shared_user_promotion is allow_shared_user_promotion
    assert claimed.memory_policy_revision_id == job.memory_policy_revision_id


@pytest.mark.asyncio
async def test_evidence_mismatch_settles_lease_without_provider_invocation() -> None:
    message_id = uuid4()
    expected_digest = hashlib.sha256(b"expected user evidence").hexdigest()
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    inference = _Inference(_candidate(message_id=message_id))
    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(loaded, "different user evidence", "Noted."),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_revision_id=CURRENT_AGENT,
        agent_profile_id=CURRENT_AGENT,
        evidence_digest=expected_digest,
    )
    assert await processor.process_job(job.id) is None
    assert inference.calls == []
    settled = repository.processing_jobs[job.id]
    assert settled.status is ProcessingJobStatus.COMPLETED
    assert settled.last_error_class == "evidence"


@pytest.mark.asyncio
async def test_retryable_job_is_claimable_and_recovered_after_housekeeping() -> None:
    message_id = uuid4()
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await _ready_generation(repository)
    inference = _Inference(_candidate(message_id=message_id), error=RuntimeError("down"))
    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Noted."
        ),
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(message_id,),
        agent_revision_id=CURRENT_AGENT,
        agent_profile_id=CURRENT_AGENT,
    )
    retry = await processor.process_job(job.id)
    assert retry is not None and retry.state is CandidateState.RETRYABLE
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.RETRYABLE

    inference.error = None
    # The restarted worker must retain the selected generation while claiming
    # the retryable processing job.
    recovered = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        job_loader=lambda job_id: repository.get_processing_job(job_id, ISSUER, OWNER),
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "The owner prefers concise answers.", "Noted."
        ),
    )
    await recovered.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder",
            embedding_generation=generation.id,
        )
    )
    recovered.jobs[(ISSUER, OWNER, job.run_id)] = replace(
        job,
        status=ProcessingJobStatus.RUNNING,
        attempt_count=0,
        lease_id=uuid4(),
        lease_until=NOW + timedelta(minutes=5),
    )
    result = await recovered.process_job(job.id)
    assert result is not None and result.state is CandidateState.ACCEPTED
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED
    assert repository.processing_jobs[job.id].attempt_count == 2
    assert len(repository.memories) == 1


async def _evidence_for(job: object, user_content: str, assistant_content: str) -> tuple[str, str]:
    del job
    return user_content, assistant_content


@pytest.mark.asyncio
async def test_job_evidence_digest_mismatch_fails_closed_before_provider_call() -> None:
    user_message_id = uuid4()
    expected_user = "The owner prefers concise answers."
    expected_digest = hashlib.sha256(expected_user.encode()).hexdigest()
    repository = _DurableMemoryStore(clock=lambda: NOW)
    inference = _Inference(_candidate(message_id=user_message_id))
    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(
            loaded, "A different user statement.", "Noted."
        ),
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(user_message_id,),
        evidence_digest=expected_digest,
    )
    result = await processor.process_job(job.id)
    assert result is None or result.state in {CandidateState.REVIEW, CandidateState.REJECTED}
    assert inference.calls == []
    assert await repository.list_memories(ISSUER, OWNER) == []


@pytest.mark.asyncio
async def test_embedding_failure_is_retryable_and_does_not_fail_completed_turn() -> None:
    message_id = uuid4()
    repository = _GenerationMemoryStore(clock=lambda: NOW)
    embedding = _Embedding(error=RuntimeError("embedding provider unavailable"))
    processor = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=message_id)),
        embedding,
        clock=lambda: NOW,
    )
    generation = await repository.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=3, model_digest="b" * 64
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder", embedding_generation=generation.id
        )
    )  # type: ignore[arg-type]
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    candidate = await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert candidate.state is CandidateState.ACCEPTED
    assert len(repository.memories) == 1
    assert any(outcome["outcome"] == "retryable" for outcome in processor.outcomes)


@pytest.mark.asyncio
async def test_inline_embedding_does_not_claim_an_older_owner_backlog() -> None:
    repository = _GenerationMemoryStore(clock=lambda: NOW)
    old_generation = await repository.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder-old", dimension=3,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, old_generation.id)
    old_memory = await repository.create_memory(
        ISSUER, OWNER,
        content="An older queued fact.",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
    )
    old_job = await repository.queue_embedding_job(
        ISSUER, OWNER, memory_id=old_memory.id,
        revision_id=old_memory.current_revision_id, generation_id=old_generation.id,
    )
    message_id = uuid4()
    embedding = _Embedding()
    processor = MemoryProcessingService(
        repository, _Inference(_candidate(message_id=message_id)), embedding, clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder-old",
            embedding_generation=old_generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4(),
        user_message_ids=(message_id,), agent_profile_id=CURRENT_AGENT,
    )
    await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert repository.embedding_jobs[old_job.id].status is ProcessingJobStatus.QUEUED
    new_record = next(
        record for record in repository.memories.values() if record.id != old_memory.id
    )
    assert [item.generation_id for item in new_record.embeddings] == [old_generation.id]


@pytest.mark.asyncio
async def test_agent_scope_background_embedding_uses_owner_and_agent_capabilities() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    generation = await repository.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=3,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    record = await repository.create_memory(
        ISSUER, OWNER,
        content="An agent-private fact.",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
    )
    queued = await repository.queue_embedding_job(
        ISSUER, OWNER, memory_id=record.id,
        revision_id=record.current_revision_id, generation_id=generation.id,
    )
    claimed = await repository.claim_embedding_job(ISSUER, OWNER)
    assert claimed is not None and claimed.lease_id is not None
    result = _Embedding()
    vector = cast(EmbeddingResult, await result.embed(generation.model_id, record.content))
    await repository.attach_embedding(
        ISSUER, OWNER, record.id, revision_id=record.current_revision_id,
        generation_id=generation.id, vector=vector.vector, digest=vector.digest,
        model_id=vector.model_id, model_revision=vector.model_revision,
        model_digest=vector.model_digest,
        scope_type=MemoryScopeType.AGENT, agent_profile_id=CURRENT_AGENT,
    )
    settled = await repository.settle_embedding_job(
        claimed.id, issuer=ISSUER, subject=OWNER, lease_id=claimed.lease_id,
    )
    assert settled.status is ProcessingJobStatus.COMPLETED
    assert repository.embedding_jobs[queued.id].status is ProcessingJobStatus.COMPLETED
    assert record.embeddings[0].generation_id == generation.id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("observed_digest", "succeeds"),
    [("a" * 64, True), ("b" * 64, False)],
)
async def test_composed_embedding_work_requires_provider_verified_generation_identity(
    monkeypatch: pytest.MonkeyPatch, observed_digest: str, succeeds: bool
) -> None:
    repository = _DurableMemoryStore(clock=lambda: NOW)
    generation = await repository.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        dimension=2,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    record = await repository.create_memory(
        ISSUER,
        OWNER,
        content="A composed-worker fact.",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
    )
    queued = await repository.queue_embedding_job(
        ISSUER,
        OWNER,
        memory_id=record.id,
        revision_id=record.current_revision_id,
        generation_id=generation.id,
    )
    repository.embedding_jobs[queued.id] = replace(queued, available_at=NOW)
    claimed = await repository.claim_embedding_job(ISSUER, OWNER)
    assert claimed is not None

    async def embed(
        _provider: OllamaEmbeddingAdapter,
        model_id: str,
        _text: str,
        *,
        context: object | None = None,
    ) -> EmbeddingResult:
        del context
        return EmbeddingResult(
            (0.1, 0.2),
            model_id,
            None,
            2,
            observed_digest,
            "c" * 64,
        )

    monkeypatch.setattr(OllamaEmbeddingAdapter, "embed", embed)

    def use_repository(
        _sessions: object,
        *,
        testing: bool = False,
        metrics: MetadataMetrics | None = None,
    ) -> _DurableMemoryStore:
        del _sessions, testing, metrics
        return repository

    monkeypatch.setattr(
        memory_uow,
        "memory_repository",
        use_repository,
    )
    worker = memory_uow.memory_processing_service(None, metrics=MetadataMetrics())
    process = cast(Any, worker)._process_embedding_job
    assert await process(claimed) is succeeds
    current = await repository.get_memory(ISSUER, OWNER, record.id)
    assert bool(current.embeddings) is succeeds
    settled = repository.embedding_jobs[queued.id]
    assert settled.status is (
        ProcessingJobStatus.COMPLETED if succeeds else ProcessingJobStatus.RETRYABLE
    )


@pytest.mark.asyncio
async def test_one_housekeeping_attempt_requeues_embedding_with_future_retry_time() -> None:
    repository = MemoryStore(clock=lambda: NOW)
    generation = await repository.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder", dimension=3,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    record = await repository.create_memory(
        ISSUER, OWNER, content="A retryable fact.", kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER), confidence=0.9, importance=0.8,
        half_life_days=30,
    )
    await repository.queue_embedding_job(
        ISSUER, OWNER, memory_id=record.id,
        revision_id=record.current_revision_id, generation_id=generation.id,
    )
    calls = 0
    claimed = await repository.claim_embedding_job(ISSUER, OWNER)
    assert claimed is not None and claimed.lease_id is not None
    calls += 1
    retried = await repository.settle_embedding_job(
        claimed.id, issuer=ISSUER, subject=OWNER, lease_id=claimed.lease_id,
        retryable=True, error_class="provider",
    )
    assert calls == 1
    assert retried.status is ProcessingJobStatus.RETRYABLE
    assert retried.available_at > NOW


@pytest.mark.asyncio
async def test_no_generation_configuration_parks_before_inference() -> None:
    repository = _DurableMemoryStore(clock=lambda: NOW)
    inference = _Inference(_candidate())
    processor = MemoryProcessingService(
        repository, inference, _Embedding(), clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(loaded, "A user fact.", "Noted."),
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4(),
        user_message_ids=(uuid4(),), agent_profile_id=CURRENT_AGENT,
    )
    assert await processor.process_job(job.id) is None
    assert inference.calls == []
    assert repository.processing_jobs[job.id].last_error_class == "embedding_generation"


@pytest.mark.asyncio
async def test_orphaned_active_generation_repairs_configuration_and_unparks_job() -> None:
    repository = _DurableMemoryStore(clock=lambda: NOW)
    inference = _Inference(_candidate())
    processor = MemoryProcessingService(
        repository,
        inference,
        _Embedding(),
        clock=lambda: NOW,
        evidence_loader=lambda loaded: _evidence_for(loaded, "A user fact.", "Noted."),
    )
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(
        ISSUER,
        OWNER,
        run_id=uuid4(),
        conversation_id=uuid4(),
        user_message_ids=(uuid4(),),
        agent_profile_id=CURRENT_AGENT,
    )
    assert await processor.process_job(job.id) is None
    assert repository.processing_jobs[job.id].last_error_class == "embedding_generation"

    generation = await _ready_generation(repository, model_id="embedder", dimension=3)
    configuration = await repository.get_model_configuration(ISSUER, OWNER)
    # Simulate the legacy split-save state: the matching active generation is
    # present, but the owner pointer was never written.
    repository.model_configurations[(ISSUER, OWNER)] = replace(
        configuration, embedding_generation=None
    )

    resumed = await processor.process_job(job.id)

    assert resumed is not None
    repaired = await repository.get_model_configuration(ISSUER, OWNER)
    assert repaired.embedding_generation == generation.id
    assert repository.processing_jobs[job.id].status is ProcessingJobStatus.COMPLETED


@pytest.mark.asyncio
async def test_model_change_keeps_inline_embedding_on_old_selected_generation() -> None:
    repository = _GenerationMemoryStore(clock=lambda: NOW)
    old_generation = await repository.register_embedding_generation(
        ISSUER, OWNER, generation=1, model_id="embedder-old", dimension=3,
        model_digest="a" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, old_generation.id)
    new_generation = await repository.register_embedding_generation(
        ISSUER, OWNER, generation=2, model_id="embedder-building", dimension=3,
        model_digest="b" * 64,
    )
    message_id = uuid4()
    embedding = _Embedding()
    processor = MemoryProcessingService(
        repository, _Inference(_candidate(message_id=message_id)), embedding, clock=lambda: NOW
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder-old",
            embedding_generation=old_generation.id,
        )
    )
    job = await processor.enqueue(
        ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4(),
        user_message_ids=(message_id,), agent_profile_id=CURRENT_AGENT,
    )
    await processor.process(
        job, user_content="The owner prefers concise answers.", assistant_content="Noted.",
        user_message_ids=frozenset({message_id}), run_agent_profile_id=CURRENT_AGENT,
    )
    record = next(iter(repository.memories.values()))
    assert embedding.calls == [record.content]
    assert record.embeddings[0].generation_id == old_generation.id
    assert repository.embedding_generations[new_generation.id].status == "building"


@pytest.mark.asyncio
async def test_every_accepted_revision_is_embedded_with_recorded_generation_metadata() -> None:
    message_id = uuid4()
    repository = _GenerationMemoryStore(clock=lambda: NOW)
    embedding = _Embedding(model_revision="rev-1", model_digest="b" * 64)
    generation = await repository.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=3,
        model_digest="b" * 64,
    )
    await repository.activate_embedding_generation(ISSUER, OWNER, generation.id)
    processor = MemoryProcessingService(
        repository,
        _Inference(_candidate(message_id=message_id)),
        embedding,
        clock=lambda: NOW,
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            ISSUER, OWNER, "extractor", "embedder", embedding_generation=generation.id
        )
    )  # type: ignore[arg-type]
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    record = next(iter(repository.memories.values()))
    assert len(record.embeddings) == 1
    assert record.embeddings[0].revision_id == record.current_revision_id
    assert record.embeddings[0].dimension == generation.dimension
    assert record.embeddings[0].model_id == generation.model_id


@pytest.mark.asyncio
async def test_fake_clock_decay_dormancy_archival_and_pinning() -> None:
    now = [NOW]
    repository = MemoryStore(clock=lambda: now[0])
    processor = MemoryProcessingService(
        repository, _Inference(), _Embedding(), clock=lambda: now[0]
    )
    stale = await repository.create_memory(
        ISSUER,
        OWNER,
        content="A stale fact",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.5,
        half_life_days=0.25,
        observed_at=NOW,
    )
    pinned = await repository.create_memory(
        ISSUER,
        OWNER,
        content="A protected preference",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.5,
        half_life_days=0.25,
        observed_at=NOW,
        pinned=True,
    )
    now[0] = NOW + timedelta(days=2)
    assert await processor.maintain(ISSUER, OWNER) == 1
    assert repository.memories[stale.id].status is MemoryLifecycleStatus.DORMANT
    assert repository.memories[pinned.id].status is MemoryLifecycleStatus.ACTIVE
    now[0] = NOW + timedelta(days=33)
    assert await processor.maintain(ISSUER, OWNER) == 1
    assert repository.memories[stale.id].status is MemoryLifecycleStatus.ARCHIVED


@pytest.mark.asyncio
async def test_reinforcement_reactivates_dormant_record_without_erasing_history() -> None:
    message_id = uuid4()
    repository = MemoryStore(clock=lambda: NOW)
    memory = await repository.create_memory(
        ISSUER,
        OWNER,
        content="The owner prefers concise answers.",
        kind=MemoryKind.PREFERENCE,
        scope=MemoryScope(MemoryScopeType.AGENT, CURRENT_AGENT),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
    )
    await repository.set_status(
        ISSUER,
        OWNER,
        memory.id,
        status=MemoryLifecycleStatus.DORMANT,
        expected_version=1,
        scope_type=MemoryScopeType.AGENT,
        agent_profile_id=CURRENT_AGENT,
        authorized_agent_ids=frozenset({CURRENT_AGENT}),
    )
    inference = _Inference(_candidate(action=MemoryAction.REINFORCE, message_id=message_id))
    processor = MemoryProcessingService(repository, inference, _Embedding(), clock=lambda: NOW)
    await processor.configure_models(
        MemoryModelConfiguration(ISSUER, OWNER, "extractor", "embedder")
    )
    job = await processor.enqueue(ISSUER, OWNER, run_id=uuid4(), conversation_id=uuid4())
    await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Noted.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert repository.memories[memory.id].status is MemoryLifecycleStatus.ACTIVE
    assert len(repository.memories[memory.id].revisions) == 1
