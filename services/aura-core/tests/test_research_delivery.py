"""Black-box acceptance tests for conversational research delivery.

These tests deliberately exercise public Core boundaries.  They do not inspect
provider adapters or the research coordinator's private implementation.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.automation.notifications.public import (
    CreateResearchNotification,
    NotificationApplicationService,
)
from aura_core.domains.automation.notifications.repository import MemoryNotificationRepository
from aura_core.domains.interaction.agents.public import AgentCatalog, ToolGrant
from aura_core.domains.interaction.conversations.dto import ResearchActivityReference
from aura_core.domains.interaction.research_delivery.public import (
    MemoryResearchCompletionDeliveryRepository,
    ResearchCompletionDeliveryInput,
    ResearchCompletionDeliveryService,
)
from aura_core.domains.knowledge.research.public import (
    DEFAULT_BUDGETS,
    MemoryResearchRepository,
    ResearchApplicationService,
    ResearchCitation,
    ResearchConflict,
    ResearchContentState,
    ResearchEvidence,
    ResearchMode,
    ResearchNotFound,
    ResearchOutcome,
    ResearchStage,
    ResearchTask,
    ResearchTerminalDeliverySnapshot,
    ResearchValidationError,
)
from aura_core.entrypoints.worker.research_delivery import run_research_consumer_once
from aura_core.runtime.tools.public import (
    ResearchContext,
    ToolCall,
    ToolExecutionError,
    ToolExecutor,
    ToolPolicyEvaluator,
    default_tool_registry,
    validate_tool_arguments,
)

OWNER_ISSUER = "https://aura.test"
OWNER_SUBJECT = "delivery-owner"


def test_research_tool_requires_exact_grant_and_custom_agents_are_denied() -> None:
    catalog = AgentCatalog()
    registry = default_tool_registry()
    evaluator = ToolPolicyEvaluator(registry)
    descriptor = registry.get("web.research", 1)
    assert descriptor is not None
    assert descriptor.permission_scope == "research:run"
    assert descriptor.timeout_ms == 30_000
    assert descriptor.retry_limit == 0
    assert descriptor.observable_component == "aura.runtime.tool_execution@1"

    custom = catalog.create_agent("research-custom", "Custom", "test", "test")
    denied = evaluator.decide(
        catalog.get_tool_policy(custom.current_revision.tool_policy_revision_id),
        ToolCall("web.research", {"question": "x", "mode": "quick"}),
    )
    assert not denied.allowed
    assert denied.error_code == "TOOL_NOT_GRANTED"

    policy = catalog.create_tool_policy(custom.id, (ToolGrant("web.research", 1),))
    permitted = evaluator.decide(
        policy, ToolCall("web.research", {"question": "x", "mode": "quick"})
    )
    assert permitted.allowed
    original = custom.current_revision
    revised = catalog.attach_tool_policy(custom.id, policy.id, custom.version)
    assert revised.current_revision_id != original.id
    assert catalog.resolve_revision(original.id).tool_policy_revision_id != policy.id
    assert (
        catalog.resolve_revision(revised.current_revision.id).tool_policy_revision_id == policy.id
    )


@dataclass
class _Admission:
    research_id: UUID
    mode: ResearchMode
    admission_state: str = "admitted"
    version: int = 1


@pytest.mark.asyncio
async def test_tool_admission_returns_only_stable_task_reference_and_is_idempotent() -> None:
    catalog = AgentCatalog()
    policy = catalog.get_tool_policy(
        catalog.list_agents()[0].current_revision.tool_policy_revision_id
    )
    calls: list[dict[str, object]] = []
    task_id = uuid4()

    async def admit(arguments: dict[str, object]) -> dict[str, object]:
        calls.append(arguments)
        return {
            "taskRef": {"taskId": str(task_id)},
            "mode": "auto",
            "admissionState": "admitted",
        }

    result = await ToolExecutor(default_tool_registry()).execute(
        policy,
        ToolCall("web.research", {"question": "Compare two public facts", "mode": "auto"}),
        ResearchContext(admit),
    )
    assert result.status == "completed"
    assert result.result == {
        "taskRef": {"taskId": str(task_id)},
        "mode": "auto",
        "admissionState": "admitted",
    }
    assert set(result.result) == {"taskRef", "mode", "admissionState"}
    assert calls == [{"question": "Compare two public facts", "mode": "auto"}]


def test_research_preset_limits_and_client_detach_semantics_are_closed() -> None:
    assert DEFAULT_BUDGETS[ResearchMode.QUICK].wall_time_seconds == 90
    assert DEFAULT_BUDGETS[ResearchMode.QUICK].fetched_sources == 6
    assert DEFAULT_BUDGETS[ResearchMode.AUTO].wall_time_seconds == 600
    assert DEFAULT_BUDGETS[ResearchMode.AUTO].fetched_sources == 15
    assert DEFAULT_BUDGETS[ResearchMode.DEEP].wall_time_seconds == 1800
    assert DEFAULT_BUDGETS[ResearchMode.DEEP].fetched_sources == 30
    assert DEFAULT_BUDGETS[ResearchMode.AGENT_LED].wall_time_seconds == 3600
    assert DEFAULT_BUDGETS[ResearchMode.AGENT_LED].fetched_sources == 60
    # The contract has only these modes; detach is a client subscription
    # transition and must not become another Core run state.
    assert {item.value for item in ResearchMode} == {"quick", "auto", "deep", "agent_led"}


@pytest.mark.asyncio
async def test_admission_idempotency_survives_concurrent_calls_and_service_restart() -> None:
    repository = MemoryResearchRepository()
    settings = SimpleNamespace(
        search_enabled=False,
        static_fetch_enabled=False,
        browser_fetch_enabled=False,
    )
    providers = SimpleNamespace(admission_enabled=True, settings=settings)
    first_service = ResearchApplicationService(repository, provider_clients=providers)
    first, second = await asyncio.gather(
        first_service.admit(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            question="same request",
            mode=ResearchMode.QUICK,
            idempotency_key="delivery-admission-1",
        ),
        first_service.admit(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            question="same request",
            mode=ResearchMode.QUICK,
            idempotency_key="delivery-admission-1",
        ),
    )
    assert first.research_id == second.research_id
    restarted = ResearchApplicationService(repository, provider_clients=providers)
    replay = await restarted.admit(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        question="same request",
        mode=ResearchMode.QUICK,
        idempotency_key="delivery-admission-1",
    )
    assert replay.research_id == first.research_id
    assert len(await restarted.list(OWNER_ISSUER, OWNER_SUBJECT)) == 1


@pytest.mark.asyncio
async def test_admission_origin_is_durable_and_reconcilable_after_restart() -> None:
    repository = MemoryResearchRepository()
    settings = SimpleNamespace(
        search_enabled=False, static_fetch_enabled=False, browser_fetch_enabled=False
    )
    providers = SimpleNamespace(admission_enabled=True, settings=settings)
    origin = {
        "surface": "conversation",
        "issuer": OWNER_ISSUER,
        "subject": OWNER_SUBJECT,
        "conversationId": str(uuid4()),
        "branchId": str(uuid4()),
        "runId": str(uuid4()),
    }
    service = ResearchApplicationService(repository, provider_clients=providers)
    admission = await service.admit(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        question="origin survives a commit",
        mode=ResearchMode.QUICK,
        idempotency_key="origin-admission",
        origin=origin,
    )
    queued = await service.get(OWNER_ISSUER, OWNER_SUBJECT, admission.research_id)
    settled = await service.settle(
        queued,
        ResearchOutcome.COMPLETED,
        synthesized_answer="Persisted answer",
    )
    restarted = ResearchApplicationService(repository, provider_clients=providers)
    snapshot = await restarted.delivery_snapshot(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        settled.id,
        expected_version=settled.version,
        expected_origin=origin,
    )
    assert snapshot.origin == origin
    delivery = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(),
        _Conversation(),
        NotificationApplicationService(_NotificationRepository()),
    )
    registered = await delivery.ensure_delivery(snapshot)
    assert registered.origin == origin
    assert registered.conversation_id == UUID(origin["conversationId"])


@pytest.mark.asyncio
async def test_registration_failure_after_admission_retries_from_persisted_origin() -> None:
    """A committed admission must not lose its conversational destination."""

    repository = MemoryResearchRepository()
    providers = SimpleNamespace(
        admission_enabled=True,
        settings=SimpleNamespace(
            search_enabled=False,
            static_fetch_enabled=False,
            browser_fetch_enabled=False,
        ),
    )
    origin = {
        "surface": "conversation",
        "issuer": OWNER_ISSUER,
        "subject": OWNER_SUBJECT,
        "conversationId": str(uuid4()),
        "branchId": str(uuid4()),
        "runId": str(uuid4()),
    }
    research = ResearchApplicationService(repository, provider_clients=providers)
    admission = await research.admit(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        question="commit before registration",
        mode=ResearchMode.QUICK,
        idempotency_key="commit-before-registration",
        origin=origin,
    )
    task = await research.get(OWNER_ISSUER, OWNER_SUBJECT, admission.research_id)
    task = await research.settle(
        task, ResearchOutcome.COMPLETED, synthesized_answer="persisted answer"
    )

    class FailingRepository(MemoryResearchCompletionDeliveryRepository):
        async def register(self, item: ResearchCompletionDeliveryInput):
            raise RuntimeError("registration outage after source commit")

    failed = ResearchCompletionDeliveryService(
        FailingRepository(),
        _Conversation(),
        NotificationApplicationService(_NotificationRepository()),
    )
    snapshot = await research.delivery_snapshot(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        task.id,
        expected_version=task.version,
        expected_origin=origin,
    )
    with pytest.raises(RuntimeError):
        await failed.ensure_delivery(snapshot)

    restarted = ResearchApplicationService(repository, provider_clients=providers)
    recovered_snapshot = await restarted.delivery_snapshot(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        task.id,
        expected_version=task.version,
        expected_origin=origin,
    )
    recovered = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(),
        _Conversation(),
        NotificationApplicationService(_NotificationRepository()),
    )
    delivery = await recovered.ensure_delivery(recovered_snapshot)
    assert delivery.origin == origin
    assert delivery.conversation_id == UUID(origin["conversationId"])


@pytest.mark.asyncio
async def test_dedicated_origin_is_reconciled_without_transcript_append() -> None:
    task = _task(
        origin={
            "surface": "research_api",
            "issuer": OWNER_ISSUER,
            "subject": OWNER_SUBJECT,
        }
    )
    snapshot = ResearchTerminalDeliverySnapshot(
        task.id,
        task.issuer,
        task.subject,
        task.version,
        task.outcome,  # type: ignore[arg-type]
        task.content_state,
        task.failure_class,
        task.synthesized_answer or "answer",
        task.citations,
        task.artifact_refs,
        task.origin,
        task.mode,
    )
    conversations = _Conversation()
    notifications = _NotificationRepository()
    service = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(),
        conversations,
        NotificationApplicationService(notifications),
    )
    item = await service.ensure_delivery(snapshot)
    notifications.fail_once = True
    with pytest.raises(RuntimeError):
        await service.deliver(snapshot)
    result = await service.deliver(snapshot)
    assert item.origin_kind == "dedicated"
    assert result is not None and result.append_state == "skipped_dedicated"
    assert conversations.calls == []
    assert len(notifications.commands) == 2
    assert len(notifications._items) == 1


@pytest.mark.asyncio
async def test_delivery_accepts_only_source_authorized_terminal_snapshots() -> None:
    service, _repository, task = _delivery(_Conversation(), _NotificationRepository())
    with pytest.raises(TypeError, match="terminal snapshot"):
        await service.deliver(cast(Any, task))


def _task(
    *,
    state: ResearchContentState = ResearchContentState.ACTIVE,
    origin: dict[str, object] | None = None,
    answer: str = "Authoritative compact answer.",
    citations: tuple[ResearchCitation, ...] | None = None,
    artifact_refs: tuple[dict[str, object], ...] | None = None,
) -> ResearchTask:
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    task_id = uuid4()
    source_id = uuid4()
    evidence = ResearchEvidence(
        source_id,
        task_id,
        "Public source",
        "https://example.com/report",
        "Example",
        now,
        "text/html",
        "static",
        None,
        ("bounded cited excerpt",),
        "a" * 64,
        "#section",
    )
    citation = ResearchCitation(source_id, "Public source", evidence.url, now, "#section")
    return ResearchTask(
        task_id,
        OWNER_ISSUER,
        OWNER_SUBJECT,
        "What is the public fact?",
        ResearchMode.AUTO,
        ResearchStage.TERMINAL,
        ResearchOutcome.COMPLETED,
        state,
        4,
        now,
        now,
        now,
        DEFAULT_BUDGETS[ResearchMode.AUTO],
        evidence=evidence and (evidence,),
        synthesized_answer=answer,
        citations=(citation,) if citations is None else citations,
        artifact_refs=({"artifactId": str(uuid4())},) if artifact_refs is None else artifact_refs,
        origin={} if origin is None else origin,
    )


def _snapshot(
    task: ResearchTask, *, state: ResearchContentState | None = None
) -> ResearchTerminalDeliverySnapshot:
    return ResearchTerminalDeliverySnapshot(
        task.id,
        task.issuer,
        task.subject,
        task.version,
        task.outcome,  # type: ignore[arg-type]
        state or task.content_state,
        task.failure_class,
        task.synthesized_answer or "answer",
        task.citations,
        task.artifact_refs,
        task.origin,
        task.mode,
    )


class _Conversation:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.messages: list[object] = []
        self.fail_once = False

    async def append_research_completion(
        self,
        issuer: str,
        subject: str,
        conversation_id: UUID,
        text: str,
        **kwargs: object,
    ) -> object:
        self.calls.append(
            {
                "issuer": issuer,
                "subject": subject,
                "conversation_id": conversation_id,
                "text": text,
                **kwargs,
            }
        )
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic unknown append outcome")
        message = SimpleNamespace(id=uuid4())
        self.messages.append(message)
        return message


class _NotificationRepository(MemoryNotificationRepository):
    def __init__(self) -> None:
        super().__init__()
        self.commands: list[CreateResearchNotification] = []
        self.fail_once = False

    async def create_research_notification(self, command: CreateResearchNotification):
        self.commands.append(command)
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("synthetic notification outage")
        return await super().create_research_notification(command)


def _delivery(
    conversations: object, notifications: _NotificationRepository
) -> tuple[
    ResearchCompletionDeliveryService,
    MemoryResearchCompletionDeliveryRepository,
    ResearchTask,
]:
    repository = MemoryResearchCompletionDeliveryRepository()
    service = ResearchCompletionDeliveryService(
        repository,
        conversations,
        NotificationApplicationService(notifications),
    )
    return service, repository, _task()


@pytest.mark.asyncio
async def test_terminal_events_are_idempotent_and_notification_retry_is_independent() -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()
    service, repository, task = _delivery(conversations, notifications)
    research_id = task.id
    conversation_id = uuid4()
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            research_id,
            conversation_id,
            None,
            uuid4(),
            4,
        )
    )
    first = await service.deliver(_snapshot(task))
    second = await service.deliver(_snapshot(task))
    assert first is not None and first.append_state == "settled"
    assert second is not None and second.notification_state == "settled"
    assert len(conversations.calls) == 1
    assert len(notifications.commands) == 1

    # A notification outage is retryable without replaying the transcript.
    notifications.fail_once = True
    item = await repository.get(research_id, conversation_id)
    assert item is not None
    item.notification_state = "unknown"
    await repository.save(item)
    with pytest.raises(RuntimeError):
        await service.deliver(_snapshot(task))
    await service.deliver(_snapshot(task))
    assert len(conversations.calls) == 1
    assert len(conversations.messages) == 1
    assert len(notifications.commands) == 3  # failed attempt plus successful retry and initial


@pytest.mark.asyncio
async def test_unknown_append_outcome_reconciles_by_delivery_key_before_retry() -> None:
    conversations = _Conversation()
    conversations.fail_once = True
    notifications = _NotificationRepository()
    service, _repository, task = _delivery(conversations, notifications)
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, task.id, uuid4(), None, uuid4(), 5
        )
    )
    with pytest.raises(RuntimeError):
        await service.deliver(_snapshot(task))
    # The retry is safe even when the first append's outcome was unknown.
    result = await service.deliver(_snapshot(task))
    assert result is not None
    assert result.append_state == "settled"
    assert len(conversations.messages) == 1
    assert len(notifications.commands) == 1


@pytest.mark.asyncio
async def test_deleted_research_never_reappears_in_conversation_or_notification() -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()
    service, _repository, task = _delivery(conversations, notifications)
    research_id = task.id
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, research_id, uuid4(), None, uuid4(), 2
        )
    )
    result = await service.deliver(_snapshot(task, state=ResearchContentState.DELETED))
    assert result is not None
    assert result.append_state == "skipped_deleted"
    assert result.notification_state == "skipped_deleted"
    assert conversations.calls == []
    assert notifications.commands == []


@pytest.mark.asyncio
async def test_delivery_is_owner_and_lineage_bound_and_content_free_to_notifications() -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()
    service, _repository, task = _delivery(conversations, notifications)
    research_id = task.id
    conversation_id = uuid4()
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, research_id, conversation_id, uuid4(), uuid4(), 3
        )
    )
    await service.deliver(_snapshot(task))
    call = conversations.calls[0]
    assert call["issuer"] == OWNER_ISSUER
    assert call["subject"] == OWNER_SUBJECT
    assert call["conversation_id"] == conversation_id
    assert "What is the public fact?" not in str(call)
    assert "bounded cited excerpt" not in str(call)
    assert isinstance(call["research_activity"], ResearchActivityReference)
    assert call["research_activity"].task_id == research_id  # type: ignore[union-attr]
    assert call["citations"]
    assert call["artifact_refs"]
    artifact_values = call["artifact_refs"]
    assert isinstance(artifact_values, tuple)
    assert all(isinstance(value, UUID) for value in artifact_values)
    command = notifications.commands[0]
    assert command.research_id == research_id
    assert "question" not in repr(command).lower()
    assert "url" not in repr(command).lower()


@pytest.mark.asyncio
async def test_concurrent_terminal_events_do_not_duplicate_transcript_or_notification() -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()
    service, _repository, task = _delivery(conversations, notifications)
    research_id = task.id
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, research_id, uuid4(), None, uuid4(), 1
        )
    )
    results = await asyncio.gather(*(service.deliver(_snapshot(task)) for _ in range(2)))
    assert all(result is not None for result in results)
    assert len(conversations.messages) == 1
    assert len(notifications.commands) == 1


def test_terminal_delivery_is_bounded_to_authoritative_answer_citations_and_artifacts() -> None:
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    task_id = uuid4()
    citations = tuple(
        ResearchCitation(uuid4(), f"Source {index}", "https://example.com/source", now, None)
        for index in range(32)
    )
    artifacts = tuple({"artifactId": str(uuid4())} for _ in range(8))
    snapshot = ResearchTerminalDeliverySnapshot(
        task_id,
        OWNER_ISSUER,
        OWNER_SUBJECT,
        5,
        ResearchOutcome.COMPLETED,
        ResearchContentState.ACTIVE,
        None,
        "The persisted compact answer is authoritative.",
        citations,
        artifacts,
        {"surface": "research_api"},
        ResearchMode.AGENT_LED,
    )
    assert snapshot.synthesized_answer.startswith("The persisted")
    assert len(snapshot.citations) == 32
    assert len(snapshot.artifact_refs) == 8
    with pytest.raises(ResearchValidationError):
        ResearchTerminalDeliverySnapshot(
            task_id, OWNER_ISSUER, OWNER_SUBJECT, 5, ResearchOutcome.COMPLETED,
            ResearchContentState.ACTIVE, None, "x", citations + citations[:1], (), {},
        )
    with pytest.raises(ResearchValidationError):
        ResearchTerminalDeliverySnapshot(
            task_id, OWNER_ISSUER, OWNER_SUBJECT, 5, ResearchOutcome.COMPLETED,
            ResearchContentState.ACTIVE, None, "x", (), artifacts + artifacts[:1], {},
        )


@pytest.mark.asyncio
async def test_research_private_content_purge_clears_answer_and_lineage_content() -> None:
    repository = MemoryResearchRepository()
    task = _task()
    repository.tasks[task.id] = task
    await repository.purge_private_content(task.id)
    purged = await repository.get((OWNER_ISSUER, OWNER_SUBJECT), task.id)
    assert purged.question == ""
    assert purged.synthesized_answer is None
    assert purged.evidence == ()
    assert purged.citations == ()
    assert purged.artifact_refs == ()


@pytest.mark.asyncio
async def test_snapshot_rejects_foreign_owner_and_origin_fences() -> None:
    repository = MemoryResearchRepository()
    settings = SimpleNamespace(
        search_enabled=False,
        static_fetch_enabled=False,
        browser_fetch_enabled=False,
    )
    service = ResearchApplicationService(
        repository, provider_clients=SimpleNamespace(admission_enabled=True, settings=settings)
    )
    admission = await service.admit(
        OWNER_ISSUER, OWNER_SUBJECT, question="owner fence", mode=ResearchMode.QUICK,
        idempotency_key="owner-fence", origin={"surface": "research_api"},
    )
    queued = await service.get(OWNER_ISSUER, OWNER_SUBJECT, admission.research_id)
    terminal = await service.settle(queued, ResearchOutcome.COMPLETED, synthesized_answer="answer")
    with pytest.raises(ResearchNotFound):
        await service.delivery_snapshot(
            OWNER_ISSUER, "foreign-owner", terminal.id, expected_version=terminal.version,
        )
    with pytest.raises(ResearchConflict):
        await service.delivery_snapshot(
            OWNER_ISSUER, OWNER_SUBJECT, terminal.id, expected_version=terminal.version,
            expected_origin={"surface": "conversation", "conversationId": str(uuid4())},
        )


@pytest.mark.asyncio
async def test_delivery_snapshot_reauthorizes_artifact_owner_lifecycle_and_task_lineage() -> None:
    class Artifacts:
        def __init__(self, artifact: object) -> None:
            self.artifact = artifact

        async def get(self, _issuer: str, _subject: str, _artifact_id: UUID) -> object:
            return self.artifact

    repository = MemoryResearchRepository()
    settings = SimpleNamespace(
        search_enabled=False,
        static_fetch_enabled=False,
        browser_fetch_enabled=False,
    )
    artifact_id = uuid4()
    service = ResearchApplicationService(
        repository,
        provider_clients=SimpleNamespace(admission_enabled=True, settings=settings),
        artifact_service=Artifacts(
            SimpleNamespace(
                issuer=OWNER_ISSUER,
                subject=OWNER_SUBJECT,
                lifecycle="active",
                lineage=SimpleNamespace(task_id=None),
            )
        ),
    )
    admission = await service.admit(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        question="artifact lineage",
        mode=ResearchMode.QUICK,
        idempotency_key="artifact-lineage",
    )
    queued = await service.get(OWNER_ISSUER, OWNER_SUBJECT, admission.research_id)
    terminal = await service.settle(
        queued,
        ResearchOutcome.COMPLETED,
        synthesized_answer="answer",
        artifact_refs=({"artifactId": str(artifact_id)},),
    )
    artifact = cast(Any, service.artifact_service).artifact
    artifact.lineage.task_id = terminal.id
    snapshot = await service.delivery_snapshot(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        terminal.id,
        expected_version=terminal.version,
    )
    assert snapshot.artifact_refs == ({"artifactId": str(artifact_id)},)
    for field, value in (
        ("issuer", "https://foreign.example"),
        ("subject", "foreign"),
        ("lifecycle", "deleted"),
        ("lineage", SimpleNamespace(task_id=uuid4())),
    ):
        artifact = SimpleNamespace(
            issuer=OWNER_ISSUER,
            subject=OWNER_SUBJECT,
            lifecycle="active",
            lineage=SimpleNamespace(task_id=terminal.id),
        )
        setattr(artifact, field, value)
        cast(Any, service.artifact_service).artifact = artifact
        with pytest.raises(ResearchConflict):
            await service.delivery_snapshot(
                OWNER_ISSUER,
                OWNER_SUBJECT,
                terminal.id,
                expected_version=terminal.version,
            )


@pytest.mark.asyncio
async def test_settlement_rejects_foreign_citations_and_invalid_artifact_refs() -> None:
    repository = MemoryResearchRepository()
    settings = SimpleNamespace(
        search_enabled=False,
        static_fetch_enabled=False,
        browser_fetch_enabled=False,
    )
    service = ResearchApplicationService(
        repository,
        provider_clients=SimpleNamespace(admission_enabled=True, settings=settings),
    )
    admission = await service.admit(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        question="citation ownership",
        mode=ResearchMode.QUICK,
        idempotency_key="citation-ownership",
    )
    queued = await service.get(OWNER_ISSUER, OWNER_SUBJECT, admission.research_id)
    now = datetime(2026, 10, 30, 12, tzinfo=UTC)
    foreign_source = ResearchEvidence(
        uuid4(),
        uuid4(),
        "Foreign",
        "https://example.com/foreign",
        "Foreign",
        now,
        "text/html",
        "static",
        None,
        ("bounded",),
        "a" * 64,
        None,
    )
    foreign_citation = ResearchCitation(
        foreign_source.source_id,
        foreign_source.label,
        foreign_source.url,
        now,
        None,
    )
    with pytest.raises(ResearchValidationError):
        await service.settle(
            queued,
            ResearchOutcome.COMPLETED,
            evidence=(foreign_source,),
            citations=(foreign_citation,),
        )
    task = _task(artifact_refs=({"artifactId": "foreign-not-a-uuid"},))
    delivery, _repository, _task_value = _delivery(_Conversation(), _NotificationRepository())
    await delivery.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, task.id, uuid4(), None, uuid4(), task.version
        )
    )
    with pytest.raises(ValueError):
        await delivery.deliver(_snapshot(task))


def test_research_runtime_accepts_maximum_agent_led_payload_and_rejects_smuggled_fields() -> None:
    descriptor = default_tool_registry().get("web.research", 1)
    assert descriptor is not None
    args = {
        "question": "q" * 10_000,
        "mode": "agent_led",
        "budget": {
            "wallTimeSeconds": 3600,
            "fetchedSources": 60,
            "searchRequests": 20,
            "browserPages": 15,
            "modelCalls": 32,
        },
        "seedUrls": [f"https://example.com/{index}" for index in range(20)],
        "domains": [f"example{index}.com" for index in range(20)],
    }
    normalized = validate_tool_arguments(descriptor, args)
    assert normalized == args
    with pytest.raises(ToolExecutionError):
        validate_tool_arguments(descriptor, {**args, "assistantMessage": "must not cross boundary"})


class _RecordingMetrics:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def increment(self, component: str, metric: str, **metadata: object) -> None:
        self.calls.append(("increment", component, {"metric": metric, **metadata}))

    def observe(self, component: str, metric: str, value: float, **metadata: object) -> None:
        self.calls.append(("observe", component, {"metric": metric, "value": value, **metadata}))

    def record_span(self, component: str, operation: str, value: float, **metadata: object) -> None:
        self.calls.append(("span", component, {"operation": operation, "value": value, **metadata}))


class _OutageMetrics(_RecordingMetrics):
    def increment(self, component: str, metric: str, **metadata: object) -> None:
        raise RuntimeError("observatory unavailable")

    def observe(self, component: str, metric: str, value: float, **metadata: object) -> None:
        raise RuntimeError("observatory unavailable")

    def record_span(self, component: str, operation: str, value: float, **metadata: object) -> None:
        raise RuntimeError("observatory unavailable")


@pytest.mark.asyncio
async def test_delivery_emits_one_outcome_latency_and_span_per_attempt_and_is_observatory_isolated(
) -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()
    metrics = _RecordingMetrics()
    repository = MemoryResearchCompletionDeliveryRepository()
    service = ResearchCompletionDeliveryService(
        repository, conversations, NotificationApplicationService(notifications), metrics=metrics
    )
    task = _task()
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            task.id,
            uuid4(),
            None,
            uuid4(),
            task.version,
        )
    )
    await service.deliver(_snapshot(task))
    assert [item[0] for item in metrics.calls] == ["increment", "observe", "span"]
    assert all(item[2]["dependency"] == "core_postgresql" for item in metrics.calls)
    assert all(
        item[1] == "aura.interaction.research_completion_delivery@1"
        for item in metrics.calls
    )
    outage = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(), _Conversation(),
        NotificationApplicationService(_NotificationRepository()), metrics=_OutageMetrics()
    )
    await outage.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            task.id,
            uuid4(),
            None,
            uuid4(),
            task.version,
        )
    )
    assert await outage.deliver(_snapshot(task)) is not None


class _IdempotentConversation(_Conversation):
    def __init__(self) -> None:
        super().__init__()
        self._by_key: dict[str, object] = {}
        self.lose_first_reply = True

    async def append_research_completion(
        self,
        issuer: str,
        subject: str,
        conversation_id: UUID,
        text: str,
        **kwargs: object,
    ) -> object:
        key = str(kwargs["idempotency_key"])
        if key in self._by_key:
            return self._by_key[key]
        message = await super().append_research_completion(
            issuer, subject, conversation_id, text, **kwargs
        )
        self._by_key[key] = message
        if self.lose_first_reply:
            self.lose_first_reply = False
            raise RuntimeError("reply lost after commit")
        return message


@pytest.mark.asyncio
async def test_unknown_append_outcome_does_not_duplicate_transcript_and_redelivery_survives_restart(
) -> None:
    conversations = _IdempotentConversation()
    notifications = _NotificationRepository()
    repository = MemoryResearchCompletionDeliveryRepository()
    service = ResearchCompletionDeliveryService(
        repository, conversations, NotificationApplicationService(notifications)
    )
    task = _task()
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            task.id,
            uuid4(),
            None,
            uuid4(),
            task.version,
        )
    )
    with pytest.raises(RuntimeError):
        await service.deliver(_snapshot(task))
    restarted = ResearchCompletionDeliveryService(
        repository, conversations, NotificationApplicationService(notifications)
    )
    await restarted.deliver(_snapshot(task))
    assert len(conversations.messages) == 1


@pytest.mark.asyncio
async def test_deletion_between_append_and_notification_suppresses_notification_on_retry() -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()
    notifications.fail_once = True
    repository = MemoryResearchCompletionDeliveryRepository()
    task = _task()
    active = ResearchTerminalDeliverySnapshot(
        task.id,
        task.issuer,
        task.subject,
        task.version,
        task.outcome,  # type: ignore[arg-type]
        task.content_state,
        task.failure_class,
        task.synthesized_answer or "answer",
        task.citations,
        task.artifact_refs,
        task.origin,
        task.mode,
    )
    deleted = replace(active, content_state=ResearchContentState.DELETED)
    snapshots = [active, deleted]

    async def snapshot_provider(_task: ResearchTask) -> ResearchTerminalDeliverySnapshot:
        return snapshots.pop(0)

    service = ResearchCompletionDeliveryService(
        repository,
        conversations,
        NotificationApplicationService(notifications),
        snapshot_provider=snapshot_provider,
    )
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, task.id, uuid4(), None, uuid4(), task.version
        )
    )
    with pytest.raises(RuntimeError):
        await service.deliver(snapshots.pop(0))
    result = await service.deliver(snapshots.pop(0))
    assert result is not None
    assert result.append_state == "skipped_deleted"
    assert conversations.messages
    assert len(notifications.commands) == 1


@pytest.mark.asyncio
async def test_delivery_claim_is_acquired_before_append_and_settled_after_notification() -> None:
    events: list[str] = []

    async def claim(*_args: object, **kwargs: object) -> object:
        events.append(f"claim:{kwargs['phase']}")
        return "claim-token"

    async def settle_claim(*_args: object) -> None:
        events.append("settle")

    conversations = _Conversation()
    notifications = _NotificationRepository()
    service = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(),
        conversations,
        NotificationApplicationService(notifications),
        source_claim=claim,
        source_settle_claim=settle_claim,
    )
    task = _task()
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, task.id, uuid4(), None, uuid4(), task.version
        )
    )
    await service.deliver(
        ResearchTerminalDeliverySnapshot(
            task.id,
            task.issuer,
            task.subject,
            task.version,
            task.outcome,  # type: ignore[arg-type]
            task.content_state,
            task.failure_class,
            task.synthesized_answer or "answer",
            task.citations,
            task.artifact_refs,
            task.origin,
            task.mode,
        )
    )
    assert events == ["claim:append_and_notify", "settle"]
    assert conversations.calls and notifications.commands


@pytest.mark.asyncio
async def test_deleted_source_claim_blocks_append_before_any_delivery_side_effect() -> None:
    conversations = _Conversation()
    notifications = _NotificationRepository()

    async def deleted_claim(*_args: object, **_kwargs: object) -> object:
        raise ResearchConflict("source was deleted before delivery claim")

    service = ResearchCompletionDeliveryService(
        MemoryResearchCompletionDeliveryRepository(),
        conversations,
        NotificationApplicationService(notifications),
        source_claim=deleted_claim,
    )
    task = _task()
    await service.register(
        ResearchCompletionDeliveryInput(
            OWNER_ISSUER, OWNER_SUBJECT, task.id, uuid4(), None, uuid4(), task.version
        )
    )
    snapshot = ResearchTerminalDeliverySnapshot(
        task.id,
        task.issuer,
        task.subject,
        task.version,
        task.outcome,  # type: ignore[arg-type]
        task.content_state,
        task.failure_class,
        task.synthesized_answer or "answer",
        task.citations,
        task.artifact_refs,
        task.origin,
        task.mode,
    )
    with pytest.raises(ResearchConflict):
        await service.deliver(snapshot)
    assert conversations.calls == []
    assert notifications.commands == []


@pytest.mark.asyncio
async def test_research_claim_and_deletion_admission_are_mutually_exclusive() -> None:
    repository = MemoryResearchRepository()
    task = _task()
    repository.tasks[task.id] = task
    application = ResearchApplicationService(repository)

    claim = await application.claim_delivery(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        task.id,
        expected_version=task.version,
    )
    with pytest.raises(ResearchConflict):
        await application.delete(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            task.id,
            expected_version=task.version,
        )
    await application.settle_delivery_claim(OWNER_ISSUER, OWNER_SUBJECT, claim)
    deleted = await application.delete(
        OWNER_ISSUER,
        OWNER_SUBJECT,
        task.id,
        expected_version=task.version,
    )
    assert deleted.content_state in {
        ResearchContentState.DELETION_PENDING,
        ResearchContentState.DELETED,
    }
    assert deleted.synthesized_answer is None

    with pytest.raises(ResearchConflict):
        await application.claim_delivery(
            OWNER_ISSUER,
            OWNER_SUBJECT,
            task.id,
            expected_version=deleted.version,
        )


@pytest.mark.asyncio
async def test_research_consumer_nacks_then_processes_next_command_and_acknowledges_after_restart(
) -> None:
    class Consumer:
        def __init__(self, commands: list[object]) -> None:
            self.commands = commands
            self.delivery_count = 1
            self.acks = 0
            self.nacks: list[dict[str, object]] = []

        async def receive(self) -> object:
            return self.commands.pop(0) if self.commands else None

        async def ack(self) -> None:
            self.acks += 1

        async def nack(self, **kwargs: object) -> None:
            self.nacks.append(kwargs)

    class Coordinator:
        def __init__(self) -> None:
            self.calls = 0

        async def run_once(self, _research_id: UUID) -> object:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("transient")
            return SimpleNamespace(terminal=False)

    consumer = Consumer(
        [
            SimpleNamespace(aggregate_id=uuid4()),
            SimpleNamespace(aggregate_id=uuid4()),
        ]
    )
    coordinator = Coordinator()
    await run_research_consumer_once(cast(Any, coordinator), consumer, SimpleNamespace())
    assert consumer.nacks and consumer.acks == 0
    consumer.delivery_count = 2
    await run_research_consumer_once(cast(Any, coordinator), consumer, SimpleNamespace())
    assert consumer.acks == 1
    assert coordinator.calls == 2
