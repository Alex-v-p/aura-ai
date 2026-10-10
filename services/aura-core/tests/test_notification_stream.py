"""Replay and live-stream safety checks for notification SSE."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.automation.notifications.public import NotificationApplicationService
from aura_core.domains.automation.notifications.repository import MemoryNotificationRepository
from aura_core.domains.automation.reminders.repository import MemoryReminderRepository
from aura_core.entrypoints.api.routes.notifications import stream_notification_events
from aura_core.platform.telemetry import MetadataMetrics
from fastapi import HTTPException
from test_notifications import cleanup_sql_subjects, sql_due_reminder, sql_repositories

ROOT = Path(__file__).parents[1]
STREAM = ROOT / "src/aura_core/entrypoints/api/routes/notifications.py"


def _source() -> str:
    return STREAM.read_text(encoding="utf-8")


def test_stream_replays_authorized_persisted_events_before_transport_wakeups() -> None:
    source = _source().lower()
    assert "last-event-id" in source
    assert "history" in source or "replay" in source
    assert "nats" in source
    assert "state.nats" in source or "subscribe" in source
    replay_index = source.index("replay")
    assert replay_index < source.index("nats")


def test_stream_has_heartbeat_and_metadata_only_envelope() -> None:
    source = _source().lower()
    assert "heartbeat" in source
    assert "event_id" in source or "event-id" in source
    for forbidden in ("content", "prompt", "credential"):
        assert forbidden not in source


@pytest.mark.asyncio
async def test_memory_stream_replays_durable_events_after_owner_authorized_cursor() -> None:
    now = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    reminders = MemoryReminderRepository(clock=lambda: now[0])
    await reminders.create(
        "issuer",
        "owner",
        content="private",
        due_at=datetime(2026, 1, 1, 13, tzinfo=UTC),
        idempotency_key="create",
    )
    notifications = MemoryNotificationRepository(reminders, clock=lambda: now[0])
    now[0] = datetime(2026, 1, 1, 14, tzinfo=UTC)
    await notifications.deliver_due()
    events = await notifications.replay("issuer", "owner")
    assert len(events) == 1
    stream = notifications.stream("issuer", "owner", after=None)
    replayed = await asyncio.wait_for(anext(stream), timeout=1)
    assert replayed.id == events[0].id
    assert "private" not in repr(replayed.payload())
    await cast(Any, stream).aclose()


@pytest.mark.asyncio
async def test_postgresql_stream_authorizes_cursor_and_replays_offline_events() -> None:
    async with sql_repositories() as (_, sessions, reminders, notifications, clock):
        prefix = f"aura0055-stream-{uuid4()}-"
        owner = "https://aura0055.example"
        owner_subject = f"{prefix}owner"
        foreign_subject = f"{prefix}foreign"
        try:
            await sql_due_reminder(
                reminders,
                clock,
                issuer=owner,
                subject=owner_subject,
                suffix="owner-1",
            )
            assert len((await notifications.deliver_due(limit=100)).notifications) == 1
            await sql_due_reminder(
                reminders,
                clock,
                issuer=owner,
                subject=owner_subject,
                suffix="owner-2",
            )
            assert len((await notifications.deliver_due(limit=100)).notifications) == 1
            await sql_due_reminder(
                reminders,
                clock,
                issuer=owner,
                subject=foreign_subject,
                suffix="foreign",
            )
            assert len((await notifications.deliver_due(limit=100)).notifications) == 1

            owner_events = await notifications.replay(owner, owner_subject)
            foreign_events = await notifications.replay(owner, foreign_subject)
            assert len(owner_events) == 2
            assert len(foreign_events) == 1
            owner_first_event, owner_second_event = owner_events
            foreign_event = foreign_events[0]

            assert await notifications.cursor_is_authorized(
                owner, owner_subject, owner_first_event.id
            )
            assert not await notifications.cursor_is_authorized(
                owner, owner_subject, foreign_event.id
            )
            replay = await notifications.replay(owner, owner_subject, after=owner_first_event.id)
            assert [item.id for item in replay] == [owner_second_event.id]
            assert (
                await notifications.replay(owner, owner_subject, after=owner_second_event.id)
                == ()
            )

            service = NotificationApplicationService(notifications)
            metrics = MetadataMetrics()
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        aura=SimpleNamespace(notification_service=service, metrics=metrics)
                    )
                )
            )
            session = SimpleNamespace(
                principal=SimpleNamespace(issuer=owner, subject=owner_subject)
            )
            with pytest.raises(HTTPException) as denied:
                await stream_notification_events(
                    cast(Any, request), session=cast(Any, session), last_event_id=foreign_event.id
                )
            assert denied.value.status_code == 410

            response = await stream_notification_events(
                cast(Any, request),
                session=cast(Any, session),
                last_event_id=owner_first_event.id,
            )
            chunk = await asyncio.wait_for(anext(cast(Any, response.body_iterator)), timeout=2)
            text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
            assert str(owner_second_event.id) in text_chunk
            assert str(owner_second_event.notification_id) in text_chunk
            assert "content" not in text_chunk
            replay_measurements = [
                item
                for item in metrics.snapshot()
                if item.metric == "notification_sse_replay_count"
            ]
            assert len(replay_measurements) == 1
            assert replay_measurements[0].value == 1
            assert metrics.stats().rejected == 0
        finally:
            await cleanup_sql_subjects(sessions, prefix)
