# ruff: noqa: E501,E701,E702
"""Metadata-only persisted operational live-event boundary."""

from __future__ import annotations

from typing import Any, Literal

OperationResourceType = Literal[
    "dashboard", "saved_query", "alert_rule", "alert_instance",
    "alert_acknowledgement", "alert_suppression", "incident", "annotation",
]


class OperationalLiveService:
    def __init__(self, repository: Any) -> None: self.repository = repository
    async def publish(self, resource_type: OperationResourceType, resource_id: str, version: str, change: str, state: str | None = None) -> dict[str, object]:
        return await self.repository.append_event(resource_type, resource_id, version, change, state)
    async def after(self, last_event_id: str | None, *, limit: int = 1000) -> list[dict[str, object]]:
        return await self.repository.events_after(last_event_id, limit=limit)


__all__ = ["OperationalLiveService"]
