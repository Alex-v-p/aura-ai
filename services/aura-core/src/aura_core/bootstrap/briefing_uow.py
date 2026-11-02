"""Composition helpers for the Core scheduled briefing boundary."""

import inspect
from collections.abc import Mapping
from datetime import datetime
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.briefings.public import (
    BriefingApplicationService,
    BriefingLifecycleEventPort,
    BriefingRepository,
)
from aura_core.domains.automation.briefings.repository import (
    MemoryBriefingRepository,
    SqlBriefingRepository,
)
from aura_core.domains.execution.tasks.transport import TaskSourceInvalidationStage
from aura_core.platform.outbox import BRIEFING_EVENT_SUBJECT, OutboxCommand, SqlOutboxRepository
from aura_core.platform.telemetry import MetadataMetrics


class SqlBriefingLifecycleEventStage(BriefingLifecycleEventPort):
    """Stage schema-exact briefing facts in Core's transaction outbox."""

    def __init__(self, outbox: SqlOutboxRepository | None = None) -> None:
        self._outbox = outbox or SqlOutboxRepository()
        self._task_stage = TaskSourceInvalidationStage(self._outbox)

    async def stage_event(
        self, transaction: object, payload: Mapping[str, object], aggregate_id: UUID
    ) -> None:
        event_id = UUID(str(payload["eventId"]))
        occurred_at = datetime.fromisoformat(str(payload["occurredAt"]))
        command = OutboxCommand(
            event_id,
            BRIEFING_EVENT_SUBJECT,
            None,
            None,
            occurred_at,
            schema_version=1,
            aggregate_type="automation.briefing",
            aggregate_id=aggregate_id,
            event_payload=dict(payload),
        )
        staged = self._outbox.stage_command(transaction, command=command)  # type: ignore[arg-type]
        if inspect.isawaitable(staged):
            await staged
        if str(payload.get("eventType", "")).startswith("briefing.occurrence"):
            occurrence_id = payload.get("occurrenceId")
            occurrence_version = payload.get("occurrenceVersion")
            if occurrence_id is None or occurrence_version is None:
                raise ValueError("briefing occurrence event lacks projection metadata")
            task_event_id = uuid5(NAMESPACE_URL, f"task-source:{event_id}")
            task_payload = {
                "schemaVersion": 1,
                "eventId": str(task_event_id),
                "eventType": "task.source_invalidated",
                "sourceKind": "briefing_occurrence",
                "sourceId": str(occurrence_id),
                "sourceVersion": int(str(occurrence_version)),
                "occurredAt": str(payload["occurredAt"]),
                "correlationId": str(payload.get("eventId", event_id)),
                "causationId": str(payload.get("causationId", event_id)),
            }
            await self._task_stage.stage_task_source_invalidation(
                cast(AsyncSession, transaction), payload=task_payload
            )


def briefing_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    testing: bool = False,
    metrics: MetadataMetrics | None = None,
) -> BriefingRepository:
    if testing:
        return MemoryBriefingRepository(metrics=metrics)
    if sessions is None:
        raise ValueError("database sessions are required for briefing persistence")
    return SqlBriefingRepository(
        sessions,
        metrics=metrics,
        lifecycle_events=SqlBriefingLifecycleEventStage(),
    )


def briefing_service(repository: BriefingRepository) -> BriefingApplicationService:
    return BriefingApplicationService(repository)
