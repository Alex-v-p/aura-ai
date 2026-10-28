"""Domain-level invariants for owner-scoped in-app notifications."""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.automation.notifications.public import (
    BriefingNotificationSource,
    CreateBriefingNotification,
    CreateWorkflowNotification,
    DueDeliveryResult,
    NotificationApplicationService,
    NotificationIdempotencyConflict,
    NotificationNotFound,
    NotificationView,
    WorkflowNotificationSource,
    as_payload,
)
from aura_core.domains.automation.notifications.repository import (
    MemoryNotificationRepository,
    SqlNotificationRepository,
    _idempotency_lock_key,  # pyright: ignore[reportPrivateUsage]
)
from aura_core.domains.automation.reminders.public import ReminderStatus
from aura_core.domains.automation.reminders.repository import (
    MemoryReminderRepository,
    SqlReminderRepository,
)

ROOT = Path(__file__).parents[1]
PUBLIC = ROOT / "src/aura_core/domains/automation/notifications/public.py"
PERSISTENCE = ROOT / "src/aura_core/domains/automation/notifications/persistence.py"


def test_workflow_notification_result_identity_is_uuid_end_to_end() -> None:
    result_id = uuid4()
    command = CreateWorkflowNotification(
        issuer="https://aura.example",
        subject="owner",
        workflow_id=uuid4(),
        workflow_revision_id=uuid4(),
        workflow_instance_id=uuid4(),
        workflow_step_ordinal=1,
        workflow_step_execution_id=uuid4(),
        workflow_result_id=result_id,
        idempotency_key="workflow-result-uuid",
    )
    source = WorkflowNotificationSource(
        "workflow",
        command.workflow_id,
        command.workflow_revision_id,
        command.workflow_instance_id,
        command.workflow_step_ordinal,
        command.workflow_step_execution_id,
        command.workflow_result_id,
    )

    assert isinstance(command.workflow_result_id, UUID)
    assert source.workflow_result_id == result_id
    assert source.payload()["workflowResultId"] == str(result_id)


def test_sql_notification_advisory_lock_key_is_deterministic_unambiguous_and_nul_free() -> None:
    commands = (
        ("issuer\x00one", "subject", "request"),
        ("issuer", "subject", "request"),
        ("issuer", "subject", "request\x00suffix"),
        ("issuer", "subject\x00suffix", "request"),
        ("issuer:subject", "request", "id"),
        ("issuer", "subject:request", "id"),
    )
    keys = [_idempotency_lock_key(*command) for command in commands]

    # PostgreSQL rejects NUL bytes in text parameters.  The owner and
    # idempotency components must still remain independently partitioned.
    assert keys[0] == _idempotency_lock_key(*commands[0])
    assert len(set(keys)) == len(commands)
    assert all("\x00" not in key for key in keys)


@asynccontextmanager
async def sql_repositories() -> AsyncGenerator[tuple[Any, Any, Any, Any, list[datetime]]]:
    """Yield real SQL repositories against an explicitly disposable database."""

    url = os.environ.get("AURA_0055_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0055_DATABASE_URL is required for PostgreSQL coverage")
    try:
        from alembic import command
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message
            for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = [datetime.now(UTC)]
    reminders = SqlReminderRepository(sessions, clock=lambda: clock[0])
    notifications = SqlNotificationRepository(sessions, clock=lambda: clock[0])
    notifications.set_reminder_delivery(reminders)
    try:
        yield engine, sessions, reminders, notifications, clock
    finally:
        await engine.dispose()


async def sql_due_reminder(
    reminders: SqlReminderRepository,
    clock: list[datetime],
    *,
    issuer: str,
    subject: str,
    suffix: str,
) -> Any:
    due = clock[0] + timedelta(minutes=1)
    item = await reminders.create(
        issuer,
        subject,
        content="sql integration reminder",
        due_at=due,
        idempotency_key=f"create-{suffix}",
    )
    clock[0] = due + timedelta(minutes=1)
    return item


async def cleanup_sql_subjects(sessions: Any, prefix: str) -> None:
    from sqlalchemy import text

    async with sessions() as session, session.begin():
        for statement in (
            "DELETE FROM notification_events WHERE principal_subject LIKE :prefix",
            "DELETE FROM notifications WHERE principal_subject LIKE :prefix",
            "DELETE FROM notification_command_idempotency WHERE principal_subject LIKE :prefix",
            "DELETE FROM outbox WHERE aggregate_id IN "
            "(SELECT id FROM reminders WHERE principal_subject LIKE :prefix)",
            "DELETE FROM reminder_command_idempotency WHERE principal_subject LIKE :prefix",
            "DELETE FROM reminders WHERE principal_subject LIKE :prefix",
        ):
            await session.execute(text(statement), {"prefix": f"{prefix}%"})


def test_notification_domain_exposes_durable_lifecycle_and_owner_port() -> None:
    source = PUBLIC.read_text(encoding="utf-8")
    lowered = source.lower()
    for marker in ("delivered", "acknowledged", "reminder", "version"):
        assert marker in lowered
    assert "content" not in lowered.split("event", 1)[-1]


def test_notification_persistence_prevents_duplicate_delivery_per_reminder() -> None:
    source = PERSISTENCE.read_text(encoding="utf-8")
    assert "reminder_id" in source
    assert "UniqueConstraint" in source
    assert "version" in source
    assert "acknowledged" in source


class _Repository:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str, Any]] = []
        self.result = DueDeliveryResult((), 0, 0, 0.0)

    async def list(self, issuer: str, subject: str, **kwargs: Any) -> object:
        self.calls.append(("list", issuer, subject, kwargs))
        return object()

    async def get(self, issuer: str, subject: str, notification_id: object) -> object:
        self.calls.append(("get", issuer, subject, notification_id))
        return object()

    async def acknowledge(
        self, issuer: str, subject: str, notification_id: object, **kwargs: Any
    ) -> object:
        self.calls.append(("acknowledge", issuer, subject, kwargs))
        return object()

    async def deliver_due(self, *, limit: int = 100) -> DueDeliveryResult:
        self.calls.append(("deliver_due", "", "", limit))
        return self.result

    async def replay(self, issuer: str, subject: str, **kwargs: Any) -> tuple[object, ...]:
        self.calls.append(("replay", issuer, subject, kwargs))
        return ()

    async def cursor_is_authorized(self, issuer: str, subject: str, event_id: object) -> bool:
        self.calls.append(("cursor_is_authorized", issuer, subject, event_id))
        return True

    def stream(self, issuer: str, subject: str, **kwargs: Any) -> Any:
        self.calls.append(("stream", issuer, subject, kwargs))
        async def events() -> Any:
            if False:
                yield None
        return events()


@pytest.mark.asyncio
async def test_application_service_keeps_owner_scope_and_batch_boundaries() -> None:
    repository = _Repository()
    service = NotificationApplicationService(repository)  # type: ignore[arg-type]
    await service.list("issuer", "subject", view=NotificationView.ALL, limit=10)
    await service.replay("issuer", "subject")
    assert repository.calls[0][1:3] == ("issuer", "subject")
    assert repository.calls[1][0] == "replay"
    with pytest.raises(ValueError):
        await service.deliver_due(limit=101)
    await service.deliver_due(limit=100)
    assert repository.calls[-1] == ("deliver_due", "", "", 100)


@pytest.mark.asyncio
async def test_briefing_notification_command_is_source_neutral_idempotent_and_content_free(
) -> None:
    repository = MemoryNotificationRepository()
    schedule_id = uuid4()
    occurrence_id = uuid4()
    result_id = uuid4()
    command = CreateBriefingNotification(
        "issuer",
        "briefing-owner",
        schedule_id,
        occurrence_id,
        result_id,
        "briefing-key",
    )

    first = await repository.create_briefing_notification(command)
    retry = await repository.create_briefing_notification(command)

    assert first.id == retry.id
    assert isinstance(first.source, BriefingNotificationSource)
    assert first.reminder_id is None
    assert first.briefing_result_id == result_id
    payload = as_payload(first)
    assert payload["briefingScheduleId"] == str(schedule_id)
    assert payload["briefingOccurrenceId"] == str(occurrence_id)
    assert payload["briefingResultId"] == str(result_id)
    assert "reminderId" not in payload
    events = await repository.replay("issuer", "briefing-owner")
    assert len(events) == 1
    assert "reminderId" not in events[0].payload()
    assert not {"content", "prompt", "response"} & set(events[0].payload())


@pytest.mark.asyncio
async def test_workflow_notification_is_owner_scoped_idempotent_and_content_free() -> None:
    repository = MemoryNotificationRepository()
    result_id = uuid4()
    command = CreateWorkflowNotification(
        "issuer",
        "workflow-owner",
        uuid4(),
        uuid4(),
        uuid4(),
        3,
        uuid4(),
        result_id,
        "workflow-key",
    )
    first = await repository.create_workflow_notification(command)
    retry = await repository.create_workflow_notification(command)
    duplicate_result = await repository.create_workflow_notification(
        CreateWorkflowNotification(
            command.issuer,
            command.subject,
            command.workflow_id,
            command.workflow_revision_id,
            command.workflow_instance_id,
            command.workflow_step_ordinal,
            command.workflow_step_execution_id,
            command.workflow_result_id,
            "workflow-duplicate-key",
        )
    )
    acknowledged = await repository.acknowledge(
        "issuer",
        "workflow-owner",
        first.id,
        expected_version=1,
        idempotency_key="workflow-ack",
    )
    with pytest.raises(NotificationNotFound):
        await repository.get("issuer", "foreign-owner", first.id)
    events = await repository.replay("issuer", "workflow-owner")
    assert first.id == retry.id == duplicate_result.id == acknowledged.id
    assert isinstance(first.source, WorkflowNotificationSource)
    assert first.source.workflow_result_id == result_id
    assert len(events) == 2
    assert all(event.source == first.source for event in events)
    assert await repository.cursor_is_authorized("issuer", "workflow-owner", events[0].id)
    assert not await repository.cursor_is_authorized("issuer", "foreign-owner", events[0].id)
    assert not {"content", "prompt", "response"} & set(events[0].payload())


@pytest.mark.asyncio
async def test_concurrent_memory_scans_create_one_notification_and_fire_once() -> None:
    now = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    reminders = MemoryReminderRepository(clock=lambda: now[0])
    reminder = await reminders.create(
        "issuer",
        "owner",
        content="private",
        due_at=now[0].replace(hour=13),
        idempotency_key="create",
    )
    notifications = MemoryNotificationRepository(reminders, clock=lambda: now[0])
    now[0] = now[0].replace(hour=14)
    first, second = await asyncio.gather(
        notifications.deliver_due(), notifications.deliver_due()
    )
    assert len(first.notifications) + len(second.notifications) == 1
    assert first.fired_count + second.fired_count == 1
    stored = await reminders.get("issuer", "owner", reminder.id)
    assert stored.status is ReminderStatus.FIRED
    page = await notifications.list("issuer", "owner", view=NotificationView.ALL, limit=10)
    assert len(page.items) == 1


@pytest.mark.asyncio
async def test_postgresql_briefing_acknowledgement_replays_complete_source_lineage() -> None:
    """The SQL event row must satisfy the migration's three-ID briefing check."""

    async with sql_repositories() as (_, sessions, _, notifications, _):
        prefix = f"aura0132-briefing-{uuid4()}-"
        issuer = "https://aura0132.example"
        subject = f"{prefix}owner"
        schedule_id = uuid4()
        occurrence_id = uuid4()
        result_id = uuid4()
        try:
            created = await notifications.create_briefing_notification(
                CreateBriefingNotification(
                    issuer,
                    subject,
                    schedule_id,
                    occurrence_id,
                    result_id,
                    "briefing-delivery-1",
                )
            )
            acknowledged = await notifications.acknowledge(
                issuer,
                subject,
                created.id,
                expected_version=1,
                idempotency_key="briefing-ack-1",
            )
            events = await notifications.replay(issuer, subject)
            assert acknowledged.status.value == "acknowledged"
            assert len(events) == 2
            for event in events:
                source = event.source
                assert isinstance(source, BriefingNotificationSource)
                assert source.briefing_schedule_id == schedule_id
                assert source.briefing_occurrence_id == occurrence_id
                assert source.briefing_result_id == result_id
                payload = event.payload()
                assert payload["briefingScheduleId"] == str(schedule_id)
                assert payload["briefingOccurrenceId"] == str(occurrence_id)
                assert payload["briefingResultId"] == str(result_id)
                assert "reminderId" not in payload
        finally:
            await cleanup_sql_subjects(sessions, prefix)


@pytest.mark.asyncio
async def test_postgresql_briefing_idempotency_serializes_changed_payload_and_duplicates() -> None:
    """Both advisory locks make conflicts deterministic under concurrent delivery."""

    async with sql_repositories() as (_, sessions, _, notifications, _):
        prefix = f"aura0132-idempotency-{uuid4()}-"
        issuer = "https://aura0132.example"
        subject = f"{prefix}owner"
        schedule_id = uuid4()
        occurrence_id = uuid4()
        first_result_id = uuid4()
        second_result_id = uuid4()
        try:
            changed_payload = await asyncio.gather(
                notifications.create_briefing_notification(
                    CreateBriefingNotification(
                        issuer,
                        subject,
                        schedule_id,
                        occurrence_id,
                        first_result_id,
                        "same-key",
                    )
                ),
                notifications.create_briefing_notification(
                    CreateBriefingNotification(
                        issuer,
                        subject,
                        schedule_id,
                        occurrence_id,
                        second_result_id,
                        "same-key",
                    )
                ),
                return_exceptions=True,
            )
            assert (
                sum(
                    isinstance(item, NotificationIdempotencyConflict)
                    for item in changed_payload
                )
                == 1
            )
            assert sum(not isinstance(item, Exception) for item in changed_payload) == 1

            duplicate = await asyncio.gather(
                notifications.create_briefing_notification(
                    CreateBriefingNotification(
                        issuer,
                        subject,
                        schedule_id,
                        occurrence_id,
                        first_result_id,
                        "distinct-key-a",
                    )
                ),
                notifications.create_briefing_notification(
                    CreateBriefingNotification(
                        issuer,
                        subject,
                        schedule_id,
                        occurrence_id,
                        first_result_id,
                        "distinct-key-b",
                    )
                ),
            )
            assert duplicate[0].id == duplicate[1].id
            assert len(await notifications.replay(issuer, subject)) == 1
        finally:
            await cleanup_sql_subjects(sessions, prefix)


@pytest.mark.asyncio
async def test_acknowledgement_is_owner_scoped_replayable_and_does_not_complete_reminder() -> None:
    now = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    reminders = MemoryReminderRepository(clock=lambda: now[0])
    reminder = await reminders.create(
        "issuer",
        "owner",
        content="private",
        due_at=datetime(2026, 1, 1, 13, tzinfo=UTC),
        idempotency_key="create",
    )
    notifications = MemoryNotificationRepository(reminders, clock=lambda: now[0])
    now[0] = datetime(2026, 1, 1, 14, tzinfo=UTC)
    delivered = (await notifications.deliver_due()).notifications[0]
    with pytest.raises(NotificationNotFound):
        await notifications.get("issuer", "other", delivered.id)
    acknowledged = await notifications.acknowledge(
        "issuer", "owner", delivered.id, expected_version=1, idempotency_key="ack"
    )
    assert acknowledged.status.value == "acknowledged"
    unread = await notifications.list("issuer", "owner", view=NotificationView.UNREAD, limit=10)
    assert unread.items == ()
    assert (await reminders.get("issuer", "owner", reminder.id)).status is ReminderStatus.FIRED
    events = await notifications.replay("issuer", "owner")
    assert [event.status.value for event in events] == ["delivered", "acknowledged"]
    assert events[0].payload()["eventType"] == "notification.delivered"
    assert events[1].payload()["eventType"] == "notification.acknowledged"
    assert not {"content", "prompt", "response"} & set(events[0].payload())
    assert await notifications.cursor_is_authorized("issuer", "owner", events[0].id)
    assert not await notifications.cursor_is_authorized("issuer", "other", events[0].id)


@pytest.mark.asyncio
async def test_cancel_and_fire_race_has_one_terminal_outcome_and_no_duplicate_inbox() -> None:
    now = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    reminders = MemoryReminderRepository(clock=lambda: now[0])
    reminder = await reminders.create(
        "issuer",
        "owner",
        content="race",
        due_at=datetime(2026, 1, 1, 13, tzinfo=UTC),
        idempotency_key="create-race",
    )
    notifications = MemoryNotificationRepository(reminders, clock=lambda: now[0])
    now[0] = datetime(2026, 1, 1, 14, tzinfo=UTC)
    results = await asyncio.gather(
        notifications.deliver_due(),
        reminders.cancel(
            "issuer", "owner", reminder.id, expected_version=1, idempotency_key="cancel-race"
        ),
        return_exceptions=True,
    )
    stored = await reminders.get("issuer", "owner", reminder.id)
    assert stored.status in {ReminderStatus.FIRED, ReminderStatus.CANCELED}
    page = await notifications.list("issuer", "owner", view=NotificationView.ALL, limit=10)
    assert len(page.items) <= 1
    if stored.status is ReminderStatus.CANCELED:
        assert page.items == ()
    else:
        assert len(page.items) == 1
    assert any(not isinstance(result, BaseException) for result in results)


@pytest.mark.asyncio
async def test_postgresql_concurrent_scheduler_claims_use_skip_locked_and_create_one_notification(
) -> None:
    async with sql_repositories() as (_, sessions, reminders, notifications, clock):
        prefix = f"aura0055-concurrent-{uuid4()}-"
        try:
            items = [
                await sql_due_reminder(
                    reminders,
                    clock,
                    issuer="https://aura0055.example",
                    subject=f"{prefix}{index}",
                    suffix=str(index),
                )
                for index in range(4)
            ]
            results = cast(tuple[Any, Any], await asyncio.gather(
                notifications.deliver_due(limit=100),
                notifications.deliver_due(limit=100),
            ))
            assert sum(int(result.fired_count) for result in results) == len(items)
            delivered = [
                item
                for index in range(4)
                for item in (
                    await notifications.list(
                        "https://aura0055.example",
                        f"{prefix}{index}",
                        view=NotificationView.ALL,
                        limit=10,
                    )
                ).items
            ]
            assert len(delivered) == len(items)
            assert {item.reminder_id for item in delivered} == {item.id for item in items}
            for index, item in enumerate(items):
                stored = await reminders.get(
                    "https://aura0055.example", f"{prefix}{index}", item.id
                )
                assert stored.status is ReminderStatus.FIRED
            from aura_core.domains.automation.notifications.persistence import NotificationEventRow
            from aura_core.platform.outbox.persistence import OutboxRow
            from sqlalchemy import select

            async with sessions() as session:
                lifecycle = (
                    await session.execute(
                        select(NotificationEventRow).where(
                            NotificationEventRow.reminder_id.in_([item.id for item in items])
                        )
                    )
                ).scalars().all()
                outbox = (
                    await session.execute(
                        select(OutboxRow).where(
                            OutboxRow.aggregate_id.in_([item.id for item in items])
                        )
                    )
                ).scalars().all()
            assert len(lifecycle) == len(items)
            assert all(event.status == "delivered" for event in lifecycle)
            fired_outbox = [
                row for row in outbox if row.payload.get("eventType") == "reminder.fired"
            ]
            assert len(fired_outbox) == len(items)
            assert all(row.topic == "aura.automation.reminders.v1" for row in fired_outbox)
            assert all("content" not in row.payload for row in fired_outbox)
            replay = await notifications.replay("https://aura0055.example", f"{prefix}0")
            payload = replay[0].payload()
            assert payload["eventType"] == "notification.delivered"
            assert {
                "schemaVersion",
                "eventId",
                "eventType",
                "notificationId",
                "reminderId",
                "status",
                "notificationVersion",
                "occurredAt",
            } == set(payload)
        finally:
            await cleanup_sql_subjects(sessions, prefix)


@pytest.mark.asyncio
async def test_postgresql_transaction_rollback_leaves_scheduled_for_restart_retry() -> None:
    from sqlalchemy import text

    async with sql_repositories() as (_, sessions, reminders, notifications, clock):
        prefix = f"aura0055-retry-{uuid4()}-"
        subject = f"{prefix}owner"
        try:
            reminder = await sql_due_reminder(
                reminders,
                clock,
                issuer="https://aura0055.example",
                subject=subject,
                suffix="retry",
            )
            with pytest.raises(RuntimeError, match="simulated crash"):
                async with sessions() as session, session.begin():
                    await session.execute(
                        text("UPDATE reminders SET status = 'fired', version = 2 WHERE id = :id"),
                        {"id": reminder.id},
                    )
                    raise RuntimeError("simulated crash")
            stored = await reminders.get("https://aura0055.example", subject, reminder.id)
            assert stored.status is ReminderStatus.SCHEDULED
            first = await notifications.deliver_due(limit=100)
            assert len(first.notifications) == 1
            # A restarted repository must receive the same delivery port as
            # the bootstrap composition; otherwise this test only exercises
            # missing dependency injection instead of restart recovery.
            restarted = SqlNotificationRepository(sessions, clock=lambda: clock[0])
            restarted.set_reminder_delivery(reminders)
            second = await restarted.deliver_due(limit=100)
            assert second.notifications == ()
            stored = await reminders.get("https://aura0055.example", subject, reminder.id)
            assert stored.status is ReminderStatus.FIRED
        finally:
            await cleanup_sql_subjects(sessions, prefix)


@pytest.mark.asyncio
async def test_postgresql_cancel_vs_fire_serializes_to_one_terminal_outcome() -> None:
    async with sql_repositories() as (_, sessions, reminders, notifications, clock):
        prefix = f"aura0055-race-{uuid4()}-"
        subject = f"{prefix}owner"
        try:
            reminder = await sql_due_reminder(
                reminders,
                clock,
                issuer="https://aura0055.example",
                subject=subject,
                suffix="race",
            )
            results = cast(tuple[Any, Any], await asyncio.gather(
                notifications.deliver_due(limit=100),
                reminders.cancel(
                    "https://aura0055.example",
                    subject,
                    reminder.id,
                    expected_version=1,
                    idempotency_key="cancel-race",
                ),
                return_exceptions=True,
            ))
            stored = await reminders.get("https://aura0055.example", subject, reminder.id)
            assert stored.status in {ReminderStatus.CANCELED, ReminderStatus.FIRED}
            page = await notifications.list(
                "https://aura0055.example", subject, view=NotificationView.ALL, limit=10
            )
            assert len(page.items) == (1 if stored.status is ReminderStatus.FIRED else 0)
            assert any(not isinstance(result, BaseException) for result in results)
        finally:
            await cleanup_sql_subjects(sessions, prefix)


@pytest.mark.asyncio
async def test_postgresql_notification_unique_reminder_constraint_rejects_duplicate_rows() -> None:
    from sqlalchemy import text
    from sqlalchemy.exc import IntegrityError

    async with sql_repositories() as (_, sessions, reminders, notifications, clock):
        prefix = f"aura0055-unique-{uuid4()}-"
        subject = f"{prefix}owner"
        try:
            reminder = await sql_due_reminder(
                reminders,
                clock,
                issuer="https://aura0055.example",
                subject=subject,
                suffix="unique",
            )
            delivered = (await notifications.deliver_due(limit=100)).notifications[0]
            with pytest.raises(IntegrityError):
                async with sessions() as session, session.begin():
                    await session.execute(
                        text(
                            "INSERT INTO notifications "
                            "(id, principal_issuer, principal_subject, reminder_id, status, "
                            "version, created_at, updated_at, delivered_at) VALUES "
                            "(:id, :issuer, :subject, :reminder, 'delivered', 1, "
                            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                        ),
                        {
                            "id": uuid4(),
                            "issuer": "https://aura0055.example",
                            "subject": subject,
                            "reminder": delivered.reminder_id,
                        },
                    )
            stored = await reminders.get("https://aura0055.example", subject, reminder.id)
            assert stored.status is ReminderStatus.FIRED
        finally:
            await cleanup_sql_subjects(sessions, prefix)


@pytest.mark.asyncio
async def test_postgresql_acknowledgement_replays_exactly_and_rejects_conflicting_reuse() -> None:
    async with sql_repositories() as (_, sessions, reminders, notifications, clock):
        prefix = f"aura0055-ack-{uuid4()}-"
        subject = f"{prefix}owner"
        try:
            reminder = await sql_due_reminder(
                reminders,
                clock,
                issuer="https://aura0055.example",
                subject=subject,
                suffix="ack",
            )
            delivered = (await notifications.deliver_due(limit=100)).notifications[0]
            first = await notifications.acknowledge(
                "https://aura0055.example",
                subject,
                delivered.id,
                expected_version=1,
                idempotency_key="ack-exact",
            )
            replay = await notifications.acknowledge(
                "https://aura0055.example",
                subject,
                delivered.id,
                expected_version=1,
                idempotency_key="ack-exact",
            )
            assert replay == first
            with pytest.raises(NotificationIdempotencyConflict):
                await notifications.acknowledge(
                    "https://aura0055.example",
                    subject,
                    delivered.id,
                    expected_version=2,
                    idempotency_key="ack-exact",
                )
            stored = await reminders.get("https://aura0055.example", subject, reminder.id)
            assert stored.status is ReminderStatus.FIRED
        finally:
            await cleanup_sql_subjects(sessions, prefix)
