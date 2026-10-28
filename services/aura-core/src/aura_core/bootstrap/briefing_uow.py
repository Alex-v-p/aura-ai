"""Composition helpers for the Core scheduled briefing boundary."""

import inspect
from collections.abc import Mapping
from datetime import datetime
from uuid import UUID

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
from aura_core.platform.outbox import BRIEFING_EVENT_SUBJECT, OutboxCommand, SqlOutboxRepository
from aura_core.platform.telemetry import MetadataMetrics


class SqlBriefingLifecycleEventStage(BriefingLifecycleEventPort):
    """Stage schema-exact briefing facts in Core's transaction outbox."""

    def __init__(self, outbox: SqlOutboxRepository | None = None) -> None:
        self._outbox = outbox or SqlOutboxRepository()

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
