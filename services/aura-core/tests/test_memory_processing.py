"""AURA-0038 extraction policy, worker retry, and embedding contract tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.bootstrap.memory_uow import memory_command_factory
from aura_core.domains.execution.runs.dto import Run, RunStatus
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
    MemoryScope,
    MemoryScopeType,
    MemorySensitivity,
    MemoryStore,
    MemoryTurnEvidence,
    MemoryValidationError,
    ProcessingJobStatus,
    classify_sensitivity,
    contains_secret,
    decide_candidate,
)
from aura_core.entrypoints.worker.app import run_memory_once
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.runtime.models.ports import EmbeddingResult, ModelDescriptor

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
        self, vector: Sequence[float] = (0.1, 0.2, 0.3), *, error: Exception | None = None
    ) -> None:
        self.vector = tuple(vector)
        self.digest = "a" * 64
        self.error = error
        self.calls: list[str] = []

    async def embed(self, model_id: str, content: str) -> object:
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
                "model_revision": "test-revision",
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
    ) -> object:
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

    async def persist_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
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

    async def persist_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
        result = await super().persist_candidate(candidate)
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
    assert {case["category"] for case in cases} == {
        "family_fact",
        "changing_project_state",
        "meal_context",
        "preference",
        "correction",
        "contradiction",
        "agent_private",
        "shared_user",
        "duplicate_reinforcement",
    }
    assert all(
        case["expected_action"] in {action.value for action in MemoryAction} for case in cases
    )
    assert all(case["expected_horizon"] in {"short", "medium", "long"} for case in cases)


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
    assert result.state is CandidateState.REVIEW
    # The capture object is intentionally inspected after execution: raw
    # persisted message identifiers must never be serialized into provider
    # input; evidence is represented by bounded handles instead.
    calls = cast(_Inference, processor.inference).calls
    assert calls
    assert str(message_id) not in repr(calls[0])


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
async def test_malformed_oversized_and_provider_outage_are_retryable_without_memory_write() -> None:
    user_message_id = uuid4()
    for result, error in (
        ({"action": "not-a-real-action"}, None),
        ({"action": "create", "content": "x" * 40000}, None),
        (None, RuntimeError("provider unavailable")),
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
        assert candidate.state is CandidateState.RETRYABLE
        assert await repository.list_memories(ISSUER, OWNER) == []


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
        "grounded_evidence_handles": ["user-0"],
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
                "action": "unknown",
                "content": "A fact",
                "kind": "semantic",
                "scope_type": "user",
                "confidence": 0.99,
                "grounded_message_ids": [str(message_id)],
            },
            CandidateState.RETRYABLE,
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
            CandidateState.RETRYABLE,
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
    assert result.state is CandidateState.REVIEW
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
                "grounded_evidence_handles": [handles[0]],
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
        scope_type=MemoryScopeType.AGENT, agent_profile_id=CURRENT_AGENT,
    )
    settled = await repository.settle_embedding_job(
        claimed.id, issuer=ISSUER, subject=OWNER, lease_id=claimed.lease_id,
    )
    assert settled.status is ProcessingJobStatus.COMPLETED
    assert repository.embedding_jobs[queued.id].status is ProcessingJobStatus.COMPLETED
    assert record.embeddings[0].generation_id == generation.id


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
    embedding = _Embedding()
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
