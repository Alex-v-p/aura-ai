"""Composition helpers for the automation reminder boundary."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.reminders.public import (
    ReminderApplicationService,
    ReminderRepository,
)
from aura_core.domains.automation.reminders.repository import (
    MemoryReminderRepository,
    SqlReminderRepository,
)
from aura_core.platform.telemetry import MetadataMetrics


def reminder_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    testing: bool = False,
    metrics: MetadataMetrics | None = None,
) -> ReminderRepository:
    """Compose the durable adapter, or a deterministic test repository."""

    if testing:
        return MemoryReminderRepository(metrics=metrics)
    if sessions is None:
        raise ValueError("database sessions are required for reminder persistence")
    return SqlReminderRepository(sessions, metrics=metrics)


def reminder_service(repository: ReminderRepository) -> ReminderApplicationService:
    """Compose the shared application use-case boundary."""

    return ReminderApplicationService(repository)
