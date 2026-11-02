"""Core-owned durable in-app notification boundary."""

from aura_core.domains.automation.notifications.public import (
    BriefingNotificationSource,
    CreateBriefingNotification,
    DeliverBriefingNotification,
    Notification,
    NotificationApplicationService,
    NotificationError,
    NotificationNotFound,
    NotificationPage,
    NotificationRepository,
    NotificationSource,
    NotificationSourceKind,
    NotificationStatus,
    NotificationVersionConflict,
    NotificationView,
    ReminderNotificationSource,
    as_payload,
)

__all__ = [
    "Notification",
    "BriefingNotificationSource",
    "CreateBriefingNotification",
    "DeliverBriefingNotification",
    "NotificationApplicationService",
    "NotificationError",
    "NotificationNotFound",
    "NotificationPage",
    "NotificationRepository",
    "NotificationStatus",
    "NotificationSource",
    "NotificationSourceKind",
    "NotificationVersionConflict",
    "NotificationView",
    "ReminderNotificationSource",
    "as_payload",
]
