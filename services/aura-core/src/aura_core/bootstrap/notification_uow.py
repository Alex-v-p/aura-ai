"""Composition helpers for the Core notification boundary."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.bootstrap.reminder_uow import reminder_repository as compose_reminder_repository
from aura_core.domains.automation.notifications.public import (
    NotificationApplicationService,
    NotificationRepository,
)
from aura_core.domains.automation.notifications.repository import (
    MemoryNotificationRepository,
    SqlNotificationRepository,
)
from aura_core.domains.automation.reminders.public import ReminderDeliveryPort
from aura_core.platform.telemetry import MetadataMetrics


def notification_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    testing: bool = False,
    reminder_repository: object | None = None,
    reminder_delivery: ReminderDeliveryPort | None = None,
    metrics: MetadataMetrics | None = None,
) -> NotificationRepository:
    if testing:
        return MemoryNotificationRepository(reminder_repository, metrics=metrics)
    if sessions is None:
        raise ValueError("database sessions are required for notification persistence")
    repository = SqlNotificationRepository(sessions, metrics=metrics)
    delivery = reminder_delivery
    if delivery is None:
        delivery = compose_reminder_repository(sessions, metrics=metrics)
    repository.set_reminder_delivery(delivery)
    return repository


def notification_service(repository: NotificationRepository) -> NotificationApplicationService:
    return NotificationApplicationService(repository)
