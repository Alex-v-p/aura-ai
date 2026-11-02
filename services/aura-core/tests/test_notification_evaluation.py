"""Deterministic evaluators for AURA-0055 notification delivery."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.automation.notifications.public import (
    CreateWorkflowNotification,
    NotificationApplicationService,
    NotificationNotFound,
    WorkflowNotificationSource,
)
from aura_core.domains.automation.notifications.repository import MemoryNotificationRepository
from aura_core.domains.automation.reminders.repository import MemoryReminderRepository
from aura_core.entrypoints.api.routes.notifications import stream_notification_events
from aura_core.platform.telemetry import MetadataMetrics

ROOT = Path(__file__).parents[3]


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator_id: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: str
    metadata: dict[str, str]

    def payload(self) -> dict[str, Any]:
        return {
            "evaluatorId": self.evaluator_id,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": self.metadata,
        }


def _result(evaluator_id: str, passed: bool, **metadata: str) -> EvaluationResult:
    return EvaluationResult(
        evaluator_id,
        "notification-delivery-eval-v1",
        f"notification-delivery:{evaluator_id}:v1",
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        metadata,
    )


async def _fixture_repository(metrics: MetadataMetrics) -> tuple[
    MemoryReminderRepository,
    MemoryNotificationRepository,
    UUID,
    str,
    str,
]:
    now = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    reminders = MemoryReminderRepository(clock=lambda: now[0])
    owner = "fixture-owner"
    issuer = "https://aura0055.evaluation"
    reminder = await reminders.create(
        issuer,
        owner,
        content="private fixture reminder",
        due_at=now[0] + timedelta(hours=1),
        idempotency_key="fixture-create-1",
    )
    notifications = MemoryNotificationRepository(
        reminders,
        clock=lambda: now[0],
        metrics=metrics,
    )
    now[0] += timedelta(hours=2)
    return reminders, notifications, reminder.id, issuer, owner


async def _workflow_notification_fixture() -> dict[str, bool]:
    """Exercise workflow source identity, replay, acknowledgement, and privacy."""

    repository = MemoryNotificationRepository()
    result_id = uuid4()
    command = CreateWorkflowNotification(
        "https://aura0055.evaluation",
        "workflow-owner",
        uuid4(),
        uuid4(),
        uuid4(),
        3,
        uuid4(),
        result_id,
        "workflow-source-eval",
    )
    first = await repository.create_workflow_notification(command)
    retry = await repository.create_workflow_notification(command)
    duplicate = await repository.create_workflow_notification(
        CreateWorkflowNotification(
            command.issuer,
            command.subject,
            command.workflow_id,
            command.workflow_revision_id,
            command.workflow_instance_id,
            command.workflow_step_ordinal,
            command.workflow_step_execution_id,
            command.workflow_result_id,
            "workflow-source-duplicate",
        )
    )
    acknowledged = await repository.acknowledge(
        command.issuer,
        command.subject,
        first.id,
        expected_version=1,
        idempotency_key="workflow-source-ack",
    )
    foreign = False
    try:
        await repository.get(command.issuer, "foreign-owner", first.id)
    except NotificationNotFound:
        foreign = True
    events = await repository.replay(command.issuer, command.subject)
    return {
        "duplicate": first.id == retry.id == duplicate.id,
        "owner": foreign
        and await repository.cursor_is_authorized(command.issuer, command.subject, events[0].id)
        and not await repository.cursor_is_authorized(
            command.issuer, "foreign-owner", events[0].id
        ),
        "acknowledged": acknowledged.status.value == "acknowledged",
        "replay": len(events) == 2
        and all(event.source == first.source for event in events)
        and isinstance(first.source, WorkflowNotificationSource),
        "metadata": all(
            not any(
                forbidden in repr(event.payload()).lower()
                for forbidden in ("content", "prompt", "response", "private")
            )
            for event in events
        ),
    }


async def _exactly_once_result() -> EvaluationResult:
    metrics = MetadataMetrics()
    _, notifications, reminder_id, issuer, owner = await _fixture_repository(metrics)
    first, second = await asyncio.gather(
        notifications.deliver_due(limit=100),
        notifications.deliver_due(limit=100),
    )
    page = await notifications.list(issuer, owner, limit=10)
    events = await notifications.replay(issuer, owner)
    workflow = await _workflow_notification_fixture()
    passed = (
        len(first.notifications) + len(second.notifications) == 1
        and first.fired_count + second.fired_count == 1
        and len(page.items) == 1
        and page.items[0].reminder_id == reminder_id
        and len(events) == 1
        and workflow["duplicate"]
        and workflow["metadata"]
        and metrics.stats().rejected == 0
    )
    return _result(
        "reminder_firing_exactly_once",
        passed,
        fired_count=str(first.fired_count + second.fired_count),
        notification_count=str(len(page.items)),
        replay_event_count=str(len(events)),
        rejected_spans=str(metrics.stats().rejected),
        workflow_duplicate=str(workflow["duplicate"]).lower(),
        workflow_metadata_only=str(workflow["metadata"]).lower(),
    )


async def _owner_authorization_result() -> EvaluationResult:
    metrics = MetadataMetrics()
    _, notifications, reminder_id, issuer, owner = await _fixture_repository(metrics)
    delivered = (await notifications.deliver_due(limit=100)).notifications[0]
    foreign = "fixture-foreign"
    foreign_page = await notifications.list(issuer, foreign, limit=10)
    with pytest.raises(NotificationNotFound):
        await notifications.get(issuer, foreign, delivered.id)
    events = await notifications.replay(issuer, owner)
    workflow = await _workflow_notification_fixture()
    passed = (
        delivered.reminder_id == reminder_id
        and foreign_page.items == ()
        and len(events) == 1
        and await notifications.cursor_is_authorized(issuer, owner, events[0].id)
        and not await notifications.cursor_is_authorized(issuer, foreign, events[0].id)
        and workflow["owner"]
        and metrics.stats().rejected == 0
    )
    return _result(
        "notification_owner_authorization",
        passed,
        owner_cursor="authorized",
        foreign_cursor="denied",
        rejected_spans=str(metrics.stats().rejected),
        workflow_owner_cursor=str(workflow["owner"]).lower(),
    )


async def _replay_recovery_result() -> EvaluationResult:
    metrics = MetadataMetrics()
    now = [datetime(2026, 1, 1, 12, tzinfo=UTC)]
    reminders = MemoryReminderRepository(clock=lambda: now[0])
    issuer = "https://aura0055.evaluation"
    owner = "fixture-replay-owner"
    for suffix in ("first", "second"):
        await reminders.create(
            issuer,
            owner,
            content=f"offline fixture {suffix}",
            due_at=now[0] + timedelta(hours=1),
            idempotency_key=f"fixture-replay-{suffix}",
        )
    notifications = MemoryNotificationRepository(
        reminders,
        clock=lambda: now[0] + timedelta(hours=2),
        metrics=metrics,
    )
    delivered = await notifications.deliver_due(limit=100)
    events = await notifications.replay(issuer, owner)
    first, second = events

    # No transport wakeup is required for an offline client to catch up from
    # durable replay. Duplicate wakeups remain hints and cannot duplicate rows.
    offline = await notifications.replay(issuer, owner, after=None)
    waiter = asyncio.create_task(notifications.wait_for_wakeup(timeout=0.5))
    await asyncio.sleep(0)
    await notifications.notify_wakeup(second.id)
    woke = await waiter
    await notifications.notify_wakeup(second.id)
    await notifications.notify_wakeup(second.id)
    after_first = await notifications.replay(issuer, owner, after=first.id)
    after_second = await notifications.replay(issuer, owner, after=second.id)

    service = NotificationApplicationService(notifications)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                aura=SimpleNamespace(notification_service=service, metrics=metrics)
            )
        )
    )
    session = SimpleNamespace(
        principal=SimpleNamespace(issuer=issuer, subject=owner)
    )
    response = await stream_notification_events(
        cast(Any, request),
        session=cast(Any, session),
        last_event_id=first.id,
    )
    try:
        chunk = await asyncio.wait_for(anext(cast(Any, response.body_iterator)), timeout=1)
    finally:
        await cast(Any, response.body_iterator).aclose()
    text_chunk = chunk.decode() if isinstance(chunk, bytes) else str(chunk)
    replay_metric_count = sum(
        item.value
        for item in metrics.snapshot()
        if item.metric == "notification_sse_replay_count"
    )
    workflow = await _workflow_notification_fixture()
    passed = (
        len(delivered.notifications) == 2
        and len(offline) == 2
        and woke
        and [event.id for event in after_first] == [second.id]
        and after_second == ()
        and str(second.id) in text_chunk
        and replay_metric_count >= 1
        and workflow["replay"]
        and workflow["acknowledged"]
        and metrics.stats().rejected == 0
    )
    return _result(
        "notification_replay_recovery",
        passed,
        replay_event_count=str(len(offline)),
        stream_replay_count=str(int(replay_metric_count)),
        duplicate_wakeup="deduplicated",
        rejected_spans=str(metrics.stats().rejected),
        workflow_replay=str(workflow["replay"]).lower(),
        workflow_acknowledged=str(workflow["acknowledged"]).lower(),
    )


async def _results() -> tuple[EvaluationResult, ...]:
    return (
        await _exactly_once_result(),
        await _owner_authorization_result(),
        await _replay_recovery_result(),
    )


@pytest.mark.asyncio
async def test_required_notification_evaluators_are_replayable_and_metadata_only() -> None:
    first = await _results()
    second = await _results()
    assert first == second
    assert {item.evaluator_id for item in first} == {
        "reminder_firing_exactly_once",
        "notification_owner_authorization",
        "notification_replay_recovery",
    }
    assert all(item.outcome == "pass" and item.score == 1.0 for item in first)
    assert len({item.replay_key for item in first}) == len(first)
    for item in first:
        payload = item.payload()
        assert set(payload) == {
            "evaluatorId",
            "fixtureRevision",
            "replayKey",
            "score",
            "outcome",
            "metadata",
        }
        assert "reminder content" not in repr(payload).lower()
        assert not any(
            forbidden in repr(payload).lower()
            for forbidden in ("content", "prompt", "response", "body", "message")
        )


def test_notification_event_contract_is_closed_and_content_free() -> None:
    schema = json.loads(
        (ROOT / "contracts/events/automation/v1/notification-events.schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert schema["$schema"].endswith("draft/2020-12/schema")
    definitions = schema["$defs"]
    common = definitions["NotificationEventProperties"]
    assert common["additionalProperties"] is False
    assert set(common["properties"]) == {
        "schemaVersion",
        "eventId",
        "eventType",
        "notificationId",
        "reminderId",
        "status",
        "notificationVersion",
        "occurredAt",
    }
    assert not {
        "content",
        "prompt",
        "response",
        "ownerIssuer",
        "credential",
    } & set(common["properties"])
