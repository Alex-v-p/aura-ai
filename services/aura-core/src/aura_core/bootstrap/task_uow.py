"""Composition boundary for the Task Center projection."""

from __future__ import annotations

from typing import Any

from aura_core.domains.execution.tasks.repository import (
    MemoryTaskProjectionRepository,
    TaskProjectionRepository,
)
from aura_core.domains.execution.tasks.service import TaskProjectionService
from aura_core.domains.execution.tasks.sql_repository import SqlTaskProjectionRepository


def task_projection_repository(
    sessions: Any = None, *, testing: bool = False
) -> TaskProjectionRepository:
    if testing or sessions is None:
        return MemoryTaskProjectionRepository()
    return SqlTaskProjectionRepository(sessions)


def task_projection_service(
    sessions: Any = None, *, testing: bool = False, sources: tuple[Any, ...] = ()
) -> TaskProjectionService:
    # The capability is deliberately minted inside the Core composition
    # closure. It is never exported, serialized, or accepted from a caller.
    capability = object()
    for source in sources:
        bind = getattr(source, "_ApplicationTaskSourceAdapter__bind_capability", None)
        if callable(bind):
            bind(capability)
    return TaskProjectionService(
        task_projection_repository(sessions, testing=testing),
        sources,
        capability=capability,
    )


__all__ = ["task_projection_repository", "task_projection_service"]
