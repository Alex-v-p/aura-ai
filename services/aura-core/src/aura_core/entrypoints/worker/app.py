"""Worker composition root; durable deployments replace the in-process outbox.

The run and memory consumers intentionally share a deployable boundary but
have separate JetStream subjects and processing loops.  Memory processing is
identifier-only and cannot delay acknowledgement or execution of run work.
"""

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any, Protocol, cast
from uuid import UUID, uuid4, uuid5

from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.execution.runs.ports import RunEventPort
from aura_core.domains.execution.runs.public import (
    ChatCompletionPort,
    RunClaimLost,
    RunCoordinator,
)
from aura_core.domains.governance.approvals.public import (
    Approval,
    ApprovalApplicationService,
)
from aura_core.domains.interaction.agents.adapters import SqlAgentStore
from aura_core.domains.interaction.agents.public import AgentCatalog, AgentConfigurationService
from aura_core.domains.interaction.personas.adapters import SqlPersonaStore
from aura_core.domains.interaction.personas.public import (
    PersonaCatalog,
    PersonaConfigurationService,
)
from aura_core.domains.knowledge.memory.public import (
    MEMORY_ID_NAMESPACE,
    MEMORY_PROCESSING_SCHEMA_VERSION,
    MEMORY_PROCESSING_TOPIC,
    MemoryProcessingCommand,
    MemoryValidationError,
    memory_activity_id,
)
from aura_core.entrypoints.worker.documents import (
    DocumentWorker,
    run_document_loop,
)
from aura_core.entrypoints.worker.documents import (
    run_document_once as _run_document_once,
)
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.outbox import (
    APPROVAL_EVENT_TOPIC,
    RUN_RESUME_TOPIC,
    OutboxCommand,
    TransactionalOutboxTransport,
)
from aura_core.platform.outbox.nats import NatsIdentifierConsumer, NatsOutbox, NatsRunConsumer
from aura_core.platform.telemetry import (
    MemoryTraceContext,
    MetadataMetrics,
    Stopwatch,
    StructuredContainerLogExporter,
    TelemetryLifecycle,
    current_memory_trace_context,
    memory_trace_context,
    new_span_id,
)
from aura_core.providers.embeddings.ollama.adapter import OllamaEmbeddingAdapter
from aura_core.providers.models.ollama.adapter import OllamaAdapter
from aura_core.runtime.tools import default_tool_registry


async def run_document_housekeeping(worker: DocumentWorker) -> None:
    """Bounded recovery for upload sessions left behind by a crash."""

    while True:
        try:
            await worker.service.recover_stale_uploads(limit=100)
        except asyncio.CancelledError:
            raise
        except Exception:
            # The next bounded pass retries; no document metadata is logged.
            pass
        await asyncio.sleep(60.0)


async def run_document_once(worker: DocumentWorker, consumer: object) -> None:
    """Expose the document dispatch seam from the production worker module."""

    await _run_document_once(worker, consumer)


class MemoryJobProcessor(Protocol):
    async def process_command(self, command: MemoryProcessingCommand) -> object: ...


MemoryActivityEmitter = Callable[[MemoryProcessingCommand, str, object | None], Awaitable[None]]


async def run_once(
    coordinator: RunCoordinator,
    provider: ChatCompletionPort,
    consumer: NatsRunConsumer,
) -> None:
    command = await consumer.receive()
    if command is None:
        return
    try:
        if command.run_id is None:
            raise ValueError("run consumer received a command without a run identity")
        await coordinator.execute(command.run_id, provider)
    except Exception:
        # Leave the JetStream delivery unacknowledged so it can be redelivered.
        raise
    else:
        acknowledge = getattr(consumer, "ack", None)
        if acknowledge is not None:
            await acknowledge()


async def run_memory_once(
    processor: MemoryJobProcessor,
    consumer: NatsIdentifierConsumer,
    activity_emitter: MemoryActivityEmitter | None = None,
) -> None:
    """Process one memory job and ack only after Core commits its result."""

    try:
        command = await consumer.receive()
    except (TypeError, ValueError):
        # A malformed generic envelope is a poison delivery.  Terminate it
        # explicitly rather than letting an unbounded retry loop consume the
        # memory worker.
        await consumer.reject(error_class="validation")
        return
    if command is None:
        return
    try:
        if command.topic != MEMORY_PROCESSING_TOPIC:
            raise MemoryValidationError("unexpected memory command subject")
        if command.schema_version != MEMORY_PROCESSING_SCHEMA_VERSION:
            raise MemoryValidationError("unsupported memory command version")
        # Parsing and validation stay in knowledge.memory.  The canonical
        # schema parser owns the producer envelope and preserves the exact
        # generic command identity for Core's durable job checks.
        memory_command = MemoryProcessingCommand.from_payload(command.payload())
        _verify_memory_trace_links(command, memory_command)
    except (MemoryValidationError, TypeError, ValueError):
        await consumer.reject(error_class="validation")
        return
    context = _memory_context(memory_command)
    with memory_trace_context(context):
        if activity_emitter is not None:
            await activity_emitter(memory_command, "queued", None)
        try:
            # Core resolves the owner-scoped evidence and reports the durable
            # terminal settlement.  A provider result or an in-memory
            # candidate is not sufficient to acknowledge the delivery.
            result = await processor.process_command(memory_command)
        except Exception as error:
            delivery_count = int(getattr(consumer, "delivery_count", 1))
            await consumer.nack(
                retry_delay=_retry_delay(delivery_count),
                error_class=_memory_error_class(error),
            )
            return
        if not _is_terminal_settlement(result):
            delivery_count = int(getattr(consumer, "delivery_count", 1))
            await consumer.nack(
                retry_delay=_retry_delay(delivery_count),
                error_class="delivery",
            )
            return
        if activity_emitter is not None:
            settlement_status = getattr(getattr(result, "status", None), "value", None)
            await activity_emitter(
                memory_command,
                "failed" if settlement_status == "failed" else "completed",
                result,
            )
        await consumer.ack()


async def run_resume_once(
    consumer: NatsIdentifierConsumer,
    handler: Callable[[UUID], Awaitable[None]],
) -> None:
    """Consume one durable approval resume command after validating its event id."""

    try:
        command = await consumer.receive()
    except (TypeError, ValueError):
        await consumer.reject(error_class="validation")
        return
    if command is None:
        return
    try:
        if command.topic != RUN_RESUME_TOPIC:
            raise ValueError("unexpected resume command subject")
        event_id = command.identifier("eventId")
        if event_id is None:
            raise ValueError("resume command is missing eventId")
        await handler(UUID(event_id))
    except (TypeError, ValueError):
        await consumer.reject(error_class="validation")
        return
    except Exception:
        delivery_count = int(getattr(consumer, "delivery_count", 1))
        await consumer.nack(
            retry_delay=_retry_delay(delivery_count),
            error_class="delivery",
        )
        return
    await consumer.ack()


def _memory_context(memory_command: MemoryProcessingCommand) -> MemoryTraceContext:
    """Build one trace context from the validated producer envelope."""

    return MemoryTraceContext(
        # Correlation identifies the durable command across redelivery; each
        # delivery gets a fresh trace root so retries remain distinguishable.
        trace_id=uuid4().hex,
        span_id=new_span_id(),
        command_id=memory_command.command_id.hex,
        job_id=memory_command.job_id.hex,
        run_id=memory_command.run_id.hex,
        conversation_id=memory_command.conversation_id.hex,
        correlation_id=memory_command.correlation_id.hex,
        causation_id=memory_command.causation_id.hex,
        agent_revision_id=memory_command.agent_revision_id.hex,
        user_message_id=memory_command.user_message_id.hex,
        assistant_message_id=memory_command.assistant_message_id.hex,
        link_span_ids=(),
    )


def _verify_memory_trace_links(
    command: OutboxCommand,
    memory_command: MemoryProcessingCommand,
) -> None:
    """Ensure Core receives every identifier from the transport envelope."""

    expected: tuple[tuple[str, str], ...] = (
        ("command_id", str(command.id)),
        ("job_id", command.identifier("jobId") or ""),
        ("run_id", str(command.run_id)),
        ("conversation_id", str(command.conversation_id)),
        ("correlation_id", str(command.correlation_id or command.run_id)),
        ("causation_id", str(command.causation_id or command.run_id)),
        ("agent_revision_id", command.identifier("agentRevisionId") or ""),
        ("user_message_id", command.identifier("userMessageId") or ""),
        ("assistant_message_id", command.identifier("assistantMessageId") or ""),
    )
    for field, expected_value in expected:
        value = getattr(memory_command, field, None)
        if not expected_value or value is None or str(value) != expected_value:
            raise MemoryValidationError(f"memory command {field} does not match envelope")


def _is_terminal_settlement(result: object) -> bool:
    status = getattr(result, "status", None)
    status_value = getattr(status, "value", status)
    # A candidate/action result is not an acknowledgement boundary.  Only the
    # memory application service's durable settlement, explicitly marked
    # completed or failed, permits JetStream acknowledgement.
    if status_value not in {"completed", "failed"}:
        return False
    terminal = getattr(result, "terminal", None)
    if terminal is True:
        return True
    durable = getattr(result, "durable", None)
    if durable is True:
        return True
    settled = getattr(result, "settled", None)
    return settled is True


def _memory_error_class(error: BaseException) -> str:
    """Map retryable worker failures to bounded, metadata-only classes."""

    name = type(error).__name__.casefold()
    if any(token in name for token in ("conflict", "contention", "lease", "lock")):
        return "conflict"
    if any(token in name for token in ("timeout", "temporarily", "unavailable")):
        return "timeout"
    return "provider"


async def emit_memory_activity(
    publisher: RunEventPort,
    store: object,
    metrics: object,
    command: MemoryProcessingCommand,
    status: str,
    result: object | None,
) -> None:
    """Persist an identifier-only activity event before local fan-out."""

    if status not in {"queued", "completed", "failed"}:
        raise MemoryValidationError("unsupported memory activity status")
    started = monotonic()
    candidate = getattr(result, "candidate", None) if result is not None else None
    candidate_action = getattr(getattr(candidate, "action", None), "value", None)
    candidate_state = getattr(getattr(candidate, "state", None), "value", None)
    action_values = {
        "create": "created",
        "reinforce": "reinforced",
        "dispute": "disputed",
        "supersede": "created",
        "review": "queued_for_review",
        "ignore": "queued_for_review",
    }
    if candidate_action is None:
        action = "queued_for_review"
    elif str(candidate_state) in {"proposed", "review", "retryable"}:
        # Provider review is a disposition, not a durable create.  The
        # candidate may carry action=create after normalization, but its
        # review state must remain visible until the owner decides it.
        action = "queued_for_review"
    elif str(candidate_action) not in action_values:
        raise MemoryValidationError("unsupported memory candidate action")
    else:
        action = action_values[str(candidate_action)]
    candidate_scope = getattr(candidate, "scope", None)
    scope: dict[str, object] | None = None
    if candidate_scope is not None:
        scope_type = getattr(getattr(candidate_scope, "type", None), "value", None)
        if scope_type in {"user", "agent"}:
            scope = {"type": scope_type}
            agent_id = getattr(candidate_scope, "agent_profile_id", None)
            if scope_type == "agent" and agent_id is not None:
                scope["agentProfileId"] = str(agent_id)
    activity_id = memory_activity_id(command.job_id)
    event_id = uuid5(MEMORY_ID_NAMESPACE, f"activity-event:{command.job_id}:{status}")
    candidate_id = getattr(candidate, "id", None)
    memory_id = getattr(candidate, "memory_id", None)
    revision_id = getattr(result, "revision_id", None) if result is not None else None
    policy_revision_id = (
        getattr(result, "policy_revision_id", None) if result is not None else None
    )
    embedding_generation_id = (
        getattr(result, "embedding_generation_id", None) if result is not None else None
    )
    terminal_rejected = status in {"completed", "failed"} and str(candidate_state) == "rejected"
    if terminal_rejected:
        # Rejected/ignored extraction is a content-free terminal diagnostic,
        # not an owner-actionable candidate.  Keep the reconciliation event
        # but never expose identifiers that would invite inspection.
        action = "queued_for_review"
        candidate_id = None
        memory_id = None
        revision_id = None
        policy_revision_id = None
        embedding_generation_id = None
    data: dict[str, object] = {
        "id": str(activity_id),
        "action": action,
        "status": status,
        "scope": scope,
        "candidateId": str(candidate_id) if candidate_id is not None else None,
        "memoryId": str(memory_id) if memory_id is not None else None,
        "memoryRevisionId": str(revision_id) if revision_id is not None else None,
        "policyRevisionId": str(policy_revision_id) if policy_revision_id is not None else None,
        "embeddingGenerationId": (
            str(embedding_generation_id) if embedding_generation_id is not None else None
        ),
        "reconciliationStatus": "pending" if status == "queued" else "authoritative",
        "occurredAt": datetime.now(UTC).isoformat(),
    }
    event = RunEvent(
        event_id,
        0,
        "memory.activity",
        command.run_id,
        command.conversation_id,
        datetime.now(UTC),
        data,
    )
    trace_context = current_memory_trace_context()
    trace_id = trace_context.trace_id if trace_context is not None else uuid4().hex
    parent_span_id = trace_context.span_id if trace_context is not None else None
    trace_metadata = {
        "trace_id": trace_id,
        "run_id": str(command.run_id),
        "conversation_id": str(command.conversation_id),
    }
    try:
        # PersistentEventPublisher commits this event before its optional
        # external wakeup and process-local subscriber notification.
        await publisher.publish(event)
        record_span = getattr(metrics, "record_span", None)
        if callable(record_span):
            record_span(
                "aura.runtime.stream_delivery",
                "memory.activity.publish",
                (monotonic() - started) * 1000,
                span_id=new_span_id(),
                parent_span_id=parent_span_id,
                dependency="event_store",
                outcome="ok",
                **trace_metadata,
            )
        increment = getattr(metrics, "increment", None)
        if callable(increment):
            increment(
                "aura.runtime.stream_delivery",
                "memory_activity_publication_outcome",
                outcome="ok",
                **trace_metadata,
            )
    except Exception:
        record_span = getattr(metrics, "record_span", None)
        if callable(record_span):
            record_span(
                "aura.runtime.stream_delivery",
                "memory.activity.publish",
                (monotonic() - started) * 1000,
                span_id=new_span_id(),
                parent_span_id=parent_span_id,
                dependency="event_store",
                outcome="error",
                error_class="persistence",
                **trace_metadata,
            )
        increment = getattr(metrics, "increment", None)
        if callable(increment):
            increment(
                "aura.runtime.stream_delivery",
                "memory_activity_publication_outcome",
                outcome="error",
                **trace_metadata,
            )
        raise


def _retry_delay(delivery_count: int) -> float:
    """Bound worker retry backoff while JetStream retains redelivery state."""

    return min(30.0, 2.0 ** max(0, min(delivery_count, 5) - 1))


async def run_memory_loop(
    processor: MemoryJobProcessor,
    consumer: NatsIdentifierConsumer,
    activity_emitter: MemoryActivityEmitter | None = None,
) -> None:
    """Keep memory redelivery semantics independent from run execution."""

    while True:
        try:
            await run_memory_once(processor, consumer, activity_emitter)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Leave the delivery unacknowledged.  JetStream will redeliver the
            # identifier after the worker lease/ack timeout.
            await asyncio.sleep(0.2)


async def run_resume_loop(
    consumer: NatsIdentifierConsumer,
    handler: Callable[[UUID], Awaitable[None]],
) -> None:
    """Keep durable approval resumption independent from run execution."""

    while True:
        try:
            await run_resume_once(consumer, handler)
        except asyncio.CancelledError:
            raise
        except Exception:
            await asyncio.sleep(0.2)


async def run_memory_maintenance(processor: object) -> None:
    """Invoke the Core-owned maintenance use case when configured."""

    method = getattr(processor, "maintain", None)
    if callable(method):
        with memory_trace_context(_housekeeping_context(processor, "maintenance")):
            result = method()
            if inspect.isawaitable(result):
                await result


async def run_memory_reindex(processor: object) -> None:
    """Resume the Core-owned embedding reindex use case when configured."""

    method = getattr(processor, "resume_reindex", None)
    if callable(method):
        with memory_trace_context(_housekeeping_context(processor, "reindex")):
            result = method()
            if inspect.isawaitable(result):
                await result


def _housekeeping_context(processor: object, operation: str) -> MemoryTraceContext:
    """Build a fresh trace for one non-conversational housekeeping pass.

    Core may provide a richer context with job, memory, revision, generation,
    and link identifiers through ``housekeeping_trace_context``.  The
    fallback remains metadata-only and deliberately uses a fresh opaque trace
    rather than deriving a root from an operation name.
    """

    supplied = getattr(processor, "housekeeping_trace_context", None)
    if callable(supplied):
        candidate = supplied(operation)
        if isinstance(candidate, MemoryTraceContext):
            return candidate
    return MemoryTraceContext(trace_id=uuid4().hex, span_id=new_span_id())


def _configure_worker_memory_recall(
    store: SqlConversationStore,
    sessions: Any,
    *,
    settings: object,
    metrics: MetadataMetrics,
) -> object:
    """Attach the shared owner-scoped memory recall boundary to run execution."""

    from aura_core.bootstrap import memory_uow

    repository = memory_uow.memory_repository(sessions, metrics=metrics)
    store.set_memory_recall(
        memory_uow.memory_recall_service(repository, settings=settings, metrics=metrics)
    )
    store.set_memory_activity_repository(repository)
    return repository


async def run_memory_housekeeping(processor: object, interval_seconds: float = 60.0) -> None:
    """Run maintenance and resumable reindex work outside the run loop."""

    while True:
        try:
            await run_memory_maintenance(processor)
            await run_memory_reindex(processor)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Maintenance is independently retryable; a provider or database
            # outage must not stop either consumer loop.
            pass
        await asyncio.sleep(max(1.0, interval_seconds))


async def _resume_approval(
    approval: Approval,
    approval_service: ApprovalApplicationService,
    store: SqlConversationStore,
    coordinator: RunCoordinator,
    provider: ChatCompletionPort,
    metrics: MetadataMetrics,
    event_id: UUID | None = None,
) -> None:
    """Consume one owner-authorized decision through public application ports."""

    run_id = approval.run_id
    approval_id = approval.id
    status = approval.status.value
    trace_id = event_id or approval_id
    span_id = new_span_id()
    started = monotonic()
    trace = {
        "approval_id": str(approval_id),
        "run_id": str(run_id),
        "conversation_id": str(approval.conversation_id),
    }
    if event_id is not None:
        trace["event_id"] = str(event_id)
    if status == "pending":
        metrics.record_span(
            "aura.governance.approval_workflow",
            "approval.resume",
            (monotonic() - started) * 1000,
            trace_id=trace_id.hex,
            span_id=span_id,
            parent_span_id=None,
            dependency="postgresql",
            outcome="ignored",
            **trace,
        )
        return

    async def execute_recoverable() -> None:
        """Do not acknowledge while an executing run is still recoverable."""

        _conversation, before = await store.find_run_any(run_id)
        lease = before.lease_expires_at
        if (
            before.status.value == "running"
            and before.tool_lifecycle_status in {"approved", "denied", "executing"}
            and lease is not None
            and lease > datetime.now(UTC)
        ):
            raise RuntimeError("approval resume lease is still active")
        await coordinator.execute(run_id, provider)
        _conversation, after = await store.find_run_any(run_id)
        if (
            after.status.value == "running"
            and after.tool_lifecycle_status in {"approved", "denied", "executing"}
        ):
            raise RuntimeError("approval resume remains in progress")

    async def reconcile_completed() -> bool:
        _conversation, current = await store.find_run_any(run_id)
        if current.status.value != "completed":
            return False
        return await coordinator.reconcile_approval_completion(run_id, approval_id)

    try:
        _conversation, current_run = await store.find_run_any(run_id)
        if current_run.status.value == "completed" and await reconcile_completed():
            return
        if (
            current_run.status.value == "running"
            and current_run.tool_lifecycle_status in {"approved", "denied", "executing"}
        ):
            # A redelivery can arrive after the tool marked the run executing
            # but before its lease was recovered. Let start_run reclaim the
            # expired lease and replay the idempotent mutation.
            await execute_recoverable()
            return
        run = await store.resume_waiting_run(run_id, approval_id, approved=status == "approved")
    except RunClaimLost:
        _conversation, current_run = await store.find_run_any(run_id)
        if current_run.status.value == "completed" and await reconcile_completed():
            return
        if (
            current_run.status.value == "running"
            and current_run.tool_lifecycle_status in {"approved", "denied", "executing"}
        ):
            await execute_recoverable()
            return
        metrics.record_span(
            "aura.governance.approval_workflow",
            "approval.resume",
            (monotonic() - started) * 1000,
            trace_id=trace_id.hex,
            span_id=span_id,
            parent_span_id=None,
            dependency="postgresql",
            outcome="duplicate",
            error_class="lifecycle",
            **trace,
        )
        return
    except Exception:
        metrics.record_span(
            "aura.governance.approval_workflow",
            "approval.resume",
            (monotonic() - started) * 1000,
            trace_id=trace_id.hex,
            span_id=span_id,
            parent_span_id=None,
            dependency="postgresql",
            outcome="error",
            error_class="persistence",
            **trace,
        )
        raise
    metrics.record_span(
        "aura.governance.approval_workflow",
        "approval.resume",
        (monotonic() - started) * 1000,
        trace_id=trace_id.hex,
        span_id=span_id,
        parent_span_id=None,
        dependency="postgresql",
        outcome="resumed",
        **trace,
    )
    # The transactional outbox already carries the durable wakeup.  Execute
    # directly after the row transition so a worker that consumed the wakeup
    # cannot wait for another detached queue command.
    await coordinator.execute(run.id, provider)


async def run_approval_housekeeping(
    approval_service: ApprovalApplicationService,
    store: SqlConversationStore,
    coordinator: RunCoordinator,
    provider: ChatCompletionPort,
    metrics: MetadataMetrics,
    interval_seconds: float = 1.0,
) -> None:
    """Expire due approvals and resume their waiting runs durably."""

    while True:
        try:
            expired = await approval_service.expire_due(limit=100)
            metrics.increment(
                "aura.governance.approval_workflow",
                "approval_expiry_outcome",
                outcome="expired" if expired else "empty",
            )
            for approval in expired:
                try:
                    await _resume_approval(
                        approval, approval_service, store, coordinator, provider, metrics
                    )
                    metrics.increment(
                        "aura.governance.approval_workflow",
                        "approval_resume_outcome",
                        outcome="expired",
                    )
                except Exception:
                    metrics.increment(
                        "aura.governance.approval_workflow",
                        "approval_resume_outcome",
                        outcome="failed",
                    )
        except asyncio.CancelledError:
            raise
        except Exception:
            metrics.increment(
                "aura.governance.approval_workflow",
                "approval_expiry_outcome",
                outcome="failed",
            )
        await asyncio.sleep(max(0.1, interval_seconds))


async def run_forever(settings: Settings | None = None) -> None:
    """Production worker loop; every command is acknowledged after execution."""
    config = settings or Settings()
    engine = make_engine(config.database_url)
    sessions = session_factory(engine)
    persona_query = PersonaCatalog()
    catalog = AgentCatalog(persona_query)
    persona_store = SqlPersonaStore(sessions, persona_query)
    persona_service = PersonaConfigurationService(persona_store)
    agent_store = SqlAgentStore(sessions, catalog, persona_service)  # type: ignore[arg-type]
    agent_service = AgentConfigurationService(agent_store, persona_service)
    await persona_store.refresh()
    await agent_store.refresh()
    store = SqlConversationStore(
        sessions,
        catalog,
        persona_query=persona_query,
        persona_admission=persona_service,
    )
    store.agent_store = agent_store
    store.agent_service = agent_service  # type: ignore[attr-defined]
    metrics, telemetry = make_worker_telemetry()
    from aura_core.bootstrap.approval_uow import approval_repository, approval_service
    from aura_core.bootstrap.reminder_uow import reminder_repository, reminder_service
    reminder_repository_instance = reminder_repository(sessions, metrics=metrics)
    reminder_service_instance = reminder_service(reminder_repository_instance)
    approval_repository_instance = approval_repository(
        sessions, metrics=metrics, execution_port=store
    )
    approval_service_instance = approval_service(
        approval_repository_instance,
        execution_port=store,
    )
    # The worker composes the same owner-scoped recall boundary as the API.
    # Run execution happens in this process too, so leaving this unset would
    # silently bypass pinned automatic/off policies before prompt compilation.
    _configure_worker_memory_recall(
        store,
        sessions,
        settings=config,
        metrics=metrics,
    )

    from aura_core.bootstrap import memory_uow

    command_factory = getattr(memory_uow, "memory_command_factory", None)
    if callable(command_factory):
        store.set_memory_command_factory(cast(Callable[..., OutboxCommand], command_factory))
    consumer = NatsRunConsumer(config.nats_url, metrics=metrics)
    from aura_core.domains.knowledge.memory import public as memory_public

    memory_topic = str(getattr(memory_public, "MEMORY_PROCESSING_TOPIC", "aura.memory.process.v1"))
    document_process_topic = "aura.knowledge.documents.process.v1"
    document_delete_topic = "aura.knowledge.documents.delete.v1"
    document_event_topic = "aura.knowledge.documents.events.v1"
    memory_consumer = NatsIdentifierConsumer(
        config.nats_url,
        memory_topic,
        metrics=metrics,
        durable="aura-core-memory-worker",
        component="aura.knowledge.memory_extraction",
    )
    resume_consumer = NatsIdentifierConsumer(
        config.nats_url,
        RUN_RESUME_TOPIC,
        metrics=metrics,
        durable="aura-core-approval-resume-worker",
        component="aura.governance.approval_workflow",
    )
    document_process_consumer = NatsIdentifierConsumer(
        config.nats_url,
        document_process_topic,
        metrics=metrics,
        durable="aura-core-document-process-worker",
        component="aura.knowledge.document_processing",
    )
    document_delete_consumer = NatsIdentifierConsumer(
        config.nats_url,
        document_delete_topic,
        metrics=metrics,
        durable="aura-core-document-delete-worker",
        component="aura.knowledge.document_deletion",
    )
    event_transport = NatsOutbox(
        config.nats_url,
        additional_subjects=(
            memory_topic,
            APPROVAL_EVENT_TOPIC,
            RUN_RESUME_TOPIC,
            document_process_topic,
            document_delete_topic,
            document_event_topic,
        ),
        metrics=metrics,
    )
    memory_processor: MemoryJobProcessor | None = None
    try:
        factory = getattr(memory_uow, "memory_processing_service", None)
        if callable(factory):
            candidate = factory(
                sessions,
                metrics=metrics,
                settings=config,
                agent_policy_loader=memory_uow.make_agent_policy_loader(agent_store),
            )
            if callable(getattr(candidate, "process_command", None)):
                memory_processor = cast(MemoryJobProcessor, candidate)
    except (AttributeError, TypeError):
        # The composition root remains importable while a deployment is
        # running an older Core module; no memory command is acknowledged in
        # that state.
        memory_processor = None
    memory_task: asyncio.Task[None] | None = None
    document_process_task: asyncio.Task[None] | None = None
    document_delete_task: asyncio.Task[None] | None = None
    document_housekeeping_task: asyncio.Task[None] | None = None
    resume_task: asyncio.Task[None] | None = None
    housekeeping_task: asyncio.Task[None] | None = None
    approval_task: asyncio.Task[None] | None = None
    await telemetry.start()
    try:
        await consumer.connect()
        await resume_consumer.connect()
        await document_process_consumer.connect()
        await document_delete_consumer.connect()
        if memory_processor is not None:
            await memory_consumer.connect()
            housekeeping_task = asyncio.create_task(run_memory_housekeeping(memory_processor))
        await event_transport.connect()
        from aura_core.runtime.streaming.publisher import PersistentEventPublisher

        provider = OllamaAdapter(config.ollama_url, config.ollama_run_timeout_seconds)
        from aura_core.bootstrap.document_uow import document_service

        document_worker = DocumentWorker(
            document_service(
                sessions,
                settings=config,
                metrics=metrics,
                embedder=OllamaEmbeddingAdapter(
                    config.ollama_url,
                    config.ollama_run_timeout_seconds,
                    telemetry=metrics,
                ),
            )
        )
        document_process_task = asyncio.create_task(
            run_document_loop(document_worker, document_process_consumer)
        )
        document_delete_task = asyncio.create_task(
            run_document_loop(document_worker, document_delete_consumer)
        )
        document_housekeeping_task = asyncio.create_task(
            run_document_housekeeping(document_worker)
        )
        coordinator = RunCoordinator(
            store,
            PersistentEventPublisher(
                store.append_event,
                event_transport.publish_event,
                metrics=metrics,
            ),
            TransactionalOutboxTransport(),
            metrics,
            lease_seconds=config.ollama_run_timeout_seconds + 30.0,
            timeout_seconds=config.ollama_run_timeout_seconds,
            context_token_budget=config.context_token_budget,
            tool_registry=default_tool_registry(),
            approval_service=approval_service_instance,
            reminder_service=reminder_service_instance,
            thinking_publisher=event_transport,
        )
        # Approval/resumption wakeups are identifier-only; the worker always
        # reloads owner-authorized state through the governance public port.
        async def resume_wakeup(event_id: UUID) -> None:
            event = await approval_service_instance.resume_event(event_id)
            if event is None:
                return
            conversation, _ = await store.find_run_any(event.run_id)
            detail = await approval_service_instance.get(
                conversation.principal_issuer,
                conversation.principal_subject,
                event.approval_id,
            )
            await _resume_approval(
                detail.approval,
                approval_service_instance,
                store,
                coordinator,
                provider,
                metrics,
                event_id,
            )

        # Approval lifecycle events are API/SSE wakeups only.  Resumption is
        # consumed through the durable JetStream command below so a worker
        # outage cannot lose an approved/rejected/expired decision.
        resume_task = asyncio.create_task(run_resume_loop(resume_consumer, resume_wakeup))
        approval_task = asyncio.create_task(
            run_approval_housekeeping(
                approval_service_instance,
                store,
                coordinator,
                provider,
                metrics,
            )
        )
        if memory_processor is not None:
            activity_publisher = coordinator.publisher

            async def publish_activity(
                command: MemoryProcessingCommand,
                activity_status: str,
                result: object | None,
            ) -> None:
                await emit_memory_activity(
                    activity_publisher,
                    store,
                    metrics,
                    command,
                    activity_status,
                    result,
                )

            memory_task = asyncio.create_task(
                run_memory_loop(memory_processor, memory_consumer, publish_activity)
            )
        while True:
            expired_runs = await store.expire_leases()
            for expired_run in expired_runs:
                expiry_timer = Stopwatch()
                expiry_span_id = new_span_id()
                try:
                    if (
                        expired_run.approval_id is not None
                        and expired_run.tool_lifecycle_status in {
                            "approved",
                            "denied",
                            "executing",
                        }
                    ):
                        await coordinator.execute(expired_run.id, provider)
                    else:
                        await coordinator.publish_reconciled_status(expired_run)
                except Exception:
                    metrics.record_span(
                        "aura.execution.run_coordinator",
                        "lease.expire",
                        expiry_timer.elapsed_ms(),
                        trace_id=expired_run.id.hex,
                        span_id=expiry_span_id,
                        parent_span_id=None,
                        dependency="event_store",
                        outcome="error",
                        error_class="delivery",
                        run_id=str(expired_run.id),
                        conversation_id=str(expired_run.conversation_id),
                    )
                    raise
                metrics.record_span(
                    "aura.execution.run_coordinator",
                    "lease.expire",
                    expiry_timer.elapsed_ms(),
                    trace_id=expired_run.id.hex,
                    span_id=expiry_span_id,
                    parent_span_id=None,
                    dependency="event_store",
                    outcome="ok",
                    run_id=str(expired_run.id),
                    conversation_id=str(expired_run.conversation_id),
                )
                metrics.increment(
                    "aura.execution.run_coordinator",
                    "lease_expired",
                    trace_id=expired_run.id.hex,
                    run_id=str(expired_run.id),
                    conversation_id=str(expired_run.conversation_id),
                )
            await run_once(coordinator, provider, consumer)
    finally:
        if approval_task is not None:
            approval_task.cancel()
            await asyncio.gather(approval_task, return_exceptions=True)
        if housekeeping_task is not None:
            housekeeping_task.cancel()
            await asyncio.gather(housekeeping_task, return_exceptions=True)
        if memory_task is not None:
            memory_task.cancel()
            await asyncio.gather(memory_task, return_exceptions=True)
        for task in (document_process_task, document_delete_task):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *(task for task in (document_process_task, document_delete_task) if task is not None),
            return_exceptions=True,
        )
        if document_housekeeping_task is not None:
            document_housekeeping_task.cancel()
            await asyncio.gather(document_housekeeping_task, return_exceptions=True)
        if resume_task is not None:
            resume_task.cancel()
            await asyncio.gather(resume_task, return_exceptions=True)
        await memory_consumer.close()
        await document_process_consumer.close()
        await document_delete_consumer.close()
        await resume_consumer.close()
        await consumer.close()
        await event_transport.close()
        await telemetry.stop()
        await engine.dispose()


def make_worker_telemetry() -> tuple[MetadataMetrics, TelemetryLifecycle]:
    """Build the production metadata-only exporter and its lifecycle."""

    metrics = MetadataMetrics(exporter=StructuredContainerLogExporter())
    return metrics, TelemetryLifecycle(metrics)


def main() -> None:
    import asyncio

    asyncio.run(run_forever())
