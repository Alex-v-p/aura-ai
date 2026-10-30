"""Public completion-delivery boundary for conversational research."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.notifications.public import (
    CreateResearchNotification,
    NotificationApplicationService,
)
from aura_core.domains.interaction.conversations.dto import (
    ResearchActivityReference,
    WebCitation,
)
from aura_core.domains.interaction.research_delivery.persistence import (
    ResearchCompletionDeliveryRow,
)
from aura_core.domains.knowledge.research.public import (
    ResearchContentState,
    ResearchTerminalDeliverySnapshot,
)

RESEARCH_COMPLETION_COMPONENT = "aura.interaction.research_completion_delivery@1"


@dataclass(frozen=True, slots=True)
class ResearchCompletionDeliveryInput:
    """Opaque owner/run destination captured at admission time."""

    issuer: str
    subject: str
    research_id: UUID
    conversation_id: UUID | None
    branch_id: UUID | None
    originating_run_id: UUID
    terminal_version: int
    origin_kind: str = "conversation"
    origin: Mapping[str, object] = field(
        default_factory=lambda: cast(Mapping[str, object], {})
    )


@dataclass(frozen=True, slots=True)
class ResearchCompletionDeliveryResult:
    research_id: UUID
    append_state: str
    notification_state: str
    message_id: UUID | None = None


@dataclass(slots=True)
class _Delivery:
    id: UUID
    issuer: str
    subject: str
    research_id: UUID
    conversation_id: UUID | None
    branch_id: UUID | None
    originating_run_id: UUID
    terminal_version: int
    origin_kind: str
    origin: Mapping[str, object]
    append_key: str
    notification_key: str
    append_state: str = "pending"
    message_id: UUID | None = None
    notification_state: str = "pending"
    last_error: str | None = None


def _delivery_keys(
    research_id: UUID, conversation_id: UUID | None, terminal_version: int
) -> tuple[str, str]:
    suffix = f"{research_id}:{conversation_id}:{terminal_version}"
    return f"research-append:{suffix}", f"research-notification:{suffix}"


def _same_registration(delivery: _Delivery, item: ResearchCompletionDeliveryInput,
                       append_key: str, notification_key: str) -> bool:
    return (
        delivery.issuer == item.issuer
        and delivery.subject == item.subject
        and delivery.research_id == item.research_id
        and delivery.conversation_id == item.conversation_id
        and delivery.branch_id == item.branch_id
        and delivery.originating_run_id == item.originating_run_id
        and delivery.origin_kind == item.origin_kind
        and delivery.terminal_version == item.terminal_version
        and dict(delivery.origin) == dict(item.origin)
        and delivery.append_key == append_key
        and delivery.notification_key == notification_key
    )


def _compact_completion(
    task: ResearchTerminalDeliverySnapshot,
) -> tuple[str, tuple[WebCitation, ...], tuple[UUID, ...]]:
    citations = tuple(
        WebCitation(item.source_id, item.label, item.url, item.retrieved_at, item.locator)
        for item in task.citations[:32]
    )
    artifacts = tuple(
        UUID(str(value["artifactId"]))
        for value in task.artifact_refs[:8]
        if value.get("artifactId") is not None
    )
    return task.synthesized_answer, citations, artifacts


def _uuid_text(value: object) -> str | None:
    try:
        return UUID(str(value)).hex
    except (AttributeError, TypeError, ValueError):
        return None


class ResearchCompletionDeliveryRepository(Protocol):
    async def register(self, item: ResearchCompletionDeliveryInput) -> _Delivery: ...
    async def get(self, research_id: UUID, conversation_id: UUID | None) -> _Delivery | None: ...
    async def list_for_research(self, research_id: UUID) -> tuple[_Delivery, ...]: ...
    async def save(self, item: _Delivery) -> None: ...


class MemoryResearchCompletionDeliveryRepository:
    def __init__(self) -> None:
        self.items: dict[tuple[UUID, UUID | None], _Delivery] = {}

    async def register(self, item: ResearchCompletionDeliveryInput) -> _Delivery:
        key = (item.research_id, item.conversation_id)
        existing = self.items.get(key)
        append_key, notification_key = _delivery_keys(
            item.research_id, item.conversation_id, item.terminal_version
        )
        if existing is not None:
            if not _same_registration(existing, item, append_key, notification_key):
                raise ValueError("research delivery destination conflict")
            return existing
        value = _Delivery(
            uuid4(), item.issuer, item.subject, item.research_id, item.conversation_id,
            item.branch_id, item.originating_run_id, item.terminal_version,
            item.origin_kind, dict(item.origin), append_key, notification_key,
        )
        self.items[key] = value
        return value

    async def get(self, research_id: UUID, conversation_id: UUID | None) -> _Delivery | None:
        return self.items.get((research_id, conversation_id))

    async def list_for_research(self, research_id: UUID) -> tuple[_Delivery, ...]:
        return tuple(item for item in self.items.values() if item.research_id == research_id)

    async def save(self, item: _Delivery) -> None:
        self.items[(item.research_id, item.conversation_id)] = item


class SqlResearchCompletionDeliveryRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    @staticmethod
    def _value(row: ResearchCompletionDeliveryRow) -> _Delivery:
        return _Delivery(
            row.id, row.principal_issuer, row.principal_subject, row.research_id,
            row.conversation_id, row.branch_id, row.originating_run_id,
            row.terminal_version,
            row.origin_kind,
            dict(row.origin or {}),
            row.append_key,
            row.notification_key,
            row.append_status, row.append_message_id, row.notification_status,
            row.last_error,
        )

    async def register(self, item: ResearchCompletionDeliveryInput) -> _Delivery:
        append_key, notification_key = _delivery_keys(
            item.research_id, item.conversation_id, item.terminal_version
        )
        async with self.sessions() as session, session.begin():
            row = (
                await session.execute(
                    select(ResearchCompletionDeliveryRow)
                    .where(
                        ResearchCompletionDeliveryRow.research_id == item.research_id,
                        ResearchCompletionDeliveryRow.conversation_id == item.conversation_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is not None:
                existing = self._value(row)
                if not _same_registration(existing, item, append_key, notification_key):
                    raise ValueError("research delivery destination conflict")
                return existing
            row = ResearchCompletionDeliveryRow(
                id=uuid4(), principal_issuer=item.issuer, principal_subject=item.subject,
                research_id=item.research_id, conversation_id=item.conversation_id,
                branch_id=item.branch_id, originating_run_id=item.originating_run_id,
                terminal_version=item.terminal_version, origin_kind=item.origin_kind,
                origin=dict(item.origin),
                append_key=append_key,
                notification_key=notification_key,
            )
            session.add(row)
            await session.flush()
            return self._value(row)

    async def get(self, research_id: UUID, conversation_id: UUID | None) -> _Delivery | None:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(ResearchCompletionDeliveryRow).where(
                        ResearchCompletionDeliveryRow.research_id == research_id,
                        ResearchCompletionDeliveryRow.conversation_id == conversation_id,
                    )
                )
            ).scalar_one_or_none()
            return self._value(row) if row is not None else None

    async def list_for_research(self, research_id: UUID) -> tuple[_Delivery, ...]:
        async with self.sessions() as session:
            rows = (
                await session.execute(
                    select(ResearchCompletionDeliveryRow).where(
                        ResearchCompletionDeliveryRow.research_id == research_id
                    )
                )
            ).scalars().all()
            return tuple(self._value(row) for row in rows)

    async def save(self, item: _Delivery) -> None:
        async with self.sessions() as session, session.begin():
            row = await session.get(ResearchCompletionDeliveryRow, item.id, with_for_update=True)
            if row is None:
                return
            row.append_status = item.append_state
            row.append_message_id = item.message_id
            row.notification_status = item.notification_state
            row.last_error = item.last_error
            row.updated_at = datetime.now(UTC)


class ResearchCompletionDeliveryService:
    """Exactly-once transcript append followed by independent notification."""

    component = RESEARCH_COMPLETION_COMPONENT

    def __init__(
        self,
        repository: ResearchCompletionDeliveryRepository,
        conversations: object,
        notifications: NotificationApplicationService,
        *,
        metrics: object | None = None,
        snapshot_provider: Callable[..., Awaitable[ResearchTerminalDeliverySnapshot]] | None = None,
        source_claim: Callable[..., Awaitable[object]] | None = None,
        source_settle_claim: Callable[..., Awaitable[None]] | None = None,
    ) -> None:
        self.repository = repository
        self.conversations = conversations
        self.notifications = notifications
        self.metrics = metrics
        self.snapshot_provider = snapshot_provider
        self.source_claim = source_claim
        self.source_settle_claim = source_settle_claim

    async def register(self, item: ResearchCompletionDeliveryInput) -> _Delivery:
        return await self.repository.register(item)

    async def ensure_delivery(self, snapshot: ResearchTerminalDeliverySnapshot) -> _Delivery:
        """Reconcile a missing admission record from source-owned origin data."""
        origin = snapshot.origin
        surface = str(origin.get("surface", "research_api"))
        conversation_id = None
        branch_id = None
        run_id = snapshot.research_id
        kind = "dedicated"
        if surface == "conversation" and origin.get("conversationId") is not None:
            conversation_id = UUID(str(origin["conversationId"]))
            branch_id = UUID(str(origin["branchId"])) if origin.get("branchId") else None
            run_id = UUID(str(origin["runId"])) if origin.get("runId") else snapshot.research_id
            kind = "conversation"
        return await self.register(
            ResearchCompletionDeliveryInput(
                snapshot.issuer,
                snapshot.subject,
                snapshot.research_id,
                conversation_id,
                branch_id,
                run_id,
                snapshot.version,
                kind,
                origin,
            )
        )

    async def deliver(
        self, task: ResearchTerminalDeliverySnapshot
    ) -> ResearchCompletionDeliveryResult | None:
        if not isinstance(task, ResearchTerminalDeliverySnapshot):  # pyright: ignore[reportUnnecessaryIsInstance]
            raise TypeError("research delivery requires an authoritative terminal snapshot")
        started = monotonic()
        trace_id, run_id, conversation_id, span_id = self._trace_context(task)
        emitted = False
        observation = "success"
        error_class: str | None = None
        dependency = "core_postgresql"

        def set_phase(
            next_observation: str,
            next_error_class: str | None,
            next_dependency: str,
        ) -> None:
            nonlocal observation, error_class, dependency
            observation = next_observation
            error_class = next_error_class
            dependency = next_dependency

        def emit_once() -> None:
            nonlocal emitted
            if emitted:
                return
            # Set the guard before calling the sink.  A sink outage must never
            # cause a second terminal observation from an outer error handler.
            emitted = True
            self._metric(
                task,
                observation,
                (monotonic() - started) * 1000,
                error_class=error_class,
                dependency=dependency,
                trace_id=trace_id,
                span_id=span_id,
                run_id=run_id,
                conversation_id=conversation_id,
            )

        try:
            set_phase("unknown", "storage_unavailable", "core_postgresql")
            deliveries = await self.repository.list_for_research(task.research_id)
            if not deliveries:
                set_phase("unknown", "settlement_unknown", "core_postgresql")
                emit_once()
                return None

            claim: object | None = None
            if self.source_claim is not None and task.content_state is ResearchContentState.ACTIVE:
                set_phase("unknown", "settlement_unknown", "core_postgresql")
                claim = await self.source_claim(
                    task.issuer,
                    task.subject,
                    task.research_id,
                    expected_version=task.version,
                    phase="append_and_notify",
                )
                set_phase("success", None, "core_postgresql")

            last: ResearchCompletionDeliveryResult | None = None
            for delivery in deliveries:
                if task.content_state is not ResearchContentState.ACTIVE:
                    delivery.append_state = "skipped_deleted"
                    delivery.notification_state = "skipped_deleted"
                    set_phase("unknown", "storage_unavailable", "core_postgresql")
                    await self.repository.save(delivery)
                    set_phase("skipped", None, "core_postgresql")
                    last = ResearchCompletionDeliveryResult(
                        task.research_id, delivery.append_state, delivery.notification_state
                    )
                    continue
                if delivery.append_state != "settled":
                    set_phase("unknown", "validation_failed", "core_postgresql")
                    content, citations, artifacts = _compact_completion(task)
                    set_phase("success", None, "core_postgresql")
                    if delivery.origin_kind == "dedicated":
                        delivery.append_state = "skipped_dedicated"
                        delivery.notification_state = "pending"
                        set_phase("unknown", "storage_unavailable", "core_postgresql")
                        await self.repository.save(delivery)
                        set_phase("success", None, "core_postgresql")
                    else:
                        append = getattr(self.conversations, "append_research_completion", None)
                        if not callable(append):
                            set_phase("unknown", "delivery_failed", "conversation_public_api")
                            raise RuntimeError("conversation completion boundary is unavailable")
                        set_phase("unknown", "delivery_failed", "conversation_public_api")
                        try:
                            append_completion = cast(Callable[..., Awaitable[object]], append)
                            message = await append_completion(
                                delivery.issuer,
                                delivery.subject,
                                delivery.conversation_id,
                                content,
                                branch_id=delivery.branch_id,
                                idempotency_key=delivery.append_key,
                                citations=citations,
                                research_activity=ResearchActivityReference(
                                    task.research_id, task.mode.value, "admitted"
                                ),
                                artifact_refs=artifacts,
                            )
                        except Exception as exc:
                            delivery.append_state = "unknown"
                            delivery.last_error = type(exc).__name__[:128]
                            set_phase("unknown", "storage_unavailable", "core_postgresql")
                            await self.repository.save(delivery)
                            set_phase("unknown", "delivery_failed", "conversation_public_api")
                            raise
                        delivery.message_id = getattr(message, "id", None)
                        delivery.append_state = "settled"
                        delivery.last_error = None
                        set_phase("unknown", "storage_unavailable", "core_postgresql")
                        await self.repository.save(delivery)
                        set_phase("success", None, "core_postgresql")
                if delivery.notification_state != "settled":
                    set_phase("unknown", "delivery_failed", "notification_public_api")
                    try:
                        await self.notifications.create_research_notification(
                            CreateResearchNotification(
                                delivery.issuer,
                                delivery.subject,
                                task.research_id,
                                delivery.notification_key,
                            )
                        )
                    except Exception as exc:
                        delivery.notification_state = "unknown"
                        delivery.last_error = type(exc).__name__[:128]
                        set_phase("unknown", "storage_unavailable", "core_postgresql")
                        await self.repository.save(delivery)
                        set_phase("unknown", "delivery_failed", "notification_public_api")
                        raise
                    delivery.notification_state = "settled"
                    delivery.last_error = None
                    set_phase("unknown", "storage_unavailable", "core_postgresql")
                    await self.repository.save(delivery)
                    set_phase("success", None, "core_postgresql")
                last = ResearchCompletionDeliveryResult(
                    task.research_id,
                    delivery.append_state,
                    delivery.notification_state,
                    delivery.message_id,
                )
            if claim is not None and self.source_settle_claim is not None:
                set_phase("unknown", "settlement_unknown", "core_postgresql")
                await self.source_settle_claim(task.issuer, task.subject, claim)
                set_phase("success", None, "core_postgresql")
            emit_once()
            return last
        except Exception:
            emit_once()
            raise

    @staticmethod
    def _trace_context(
        task: ResearchTerminalDeliverySnapshot,
    ) -> tuple[str, str | None, str | None, str]:
        origin = task.origin
        run_id = _uuid_text(origin.get("runId"))
        conversation_id = _uuid_text(origin.get("conversationId"))
        trace_id = run_id or task.research_id.hex
        return trace_id, run_id, conversation_id, uuid4().hex[:16]

    def _metric(
        self,
        task: ResearchTerminalDeliverySnapshot,
        outcome: str,
        latency_ms: float | None = None,
        *,
        error_class: str | None = None,
        dependency: str = "core_postgresql",
        trace_id: str,
        span_id: str,
        run_id: str | None,
        conversation_id: str | None,
    ) -> None:
        increment = getattr(self.metrics, "increment", None)
        if callable(increment):
            try:
                dimensions: dict[str, str] = {
                    "operation": "research_completion.deliver",
                    "outcome": outcome,
                    "dependency": dependency,
                }
                if error_class is not None:
                    dimensions["error_class"] = error_class
                increment(
                    self.component,
                    "research_completion_outcome_count",
                    **dimensions,
                )
                observe = getattr(self.metrics, "observe", None)
                if callable(observe) and latency_ms is not None:
                    observe(
                        self.component,
                        "research_completion_latency_ms",
                        latency_ms,
                        **dimensions,
                    )
                span = getattr(self.metrics, "record_span", None)
                if callable(span):
                    span(
                        self.component,
                        "research_completion.deliver",
                        latency_ms or 0.0,
                        trace_id=trace_id,
                        span_id=span_id,
                        parent_span_id=None,
                        run_id=run_id,
                        conversation_id=conversation_id,
                        outcome=outcome,
                        dependency=dependency,
                        error_class=error_class,
                    )
            except Exception:
                pass


__all__ = [
    "MemoryResearchCompletionDeliveryRepository",
    "ResearchCompletionDeliveryInput",
    "ResearchCompletionDeliveryResult",
    "ResearchCompletionDeliveryService",
    "SqlResearchCompletionDeliveryRepository",
]
