"""Inward ports used by the execution-owned task projection."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from uuid import UUID

from aura_core.domains.execution.tasks.contracts import (
    TaskCommand,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotPage,
    TaskProjectionSnapshotQuery,
    TaskSourceKind,
)


class ProjectionReadCapability:
    """Non-delegable marker for source snapshot enumeration."""

    __slots__ = ()

    def __init__(self) -> None:
        raise TypeError("projection capability is composed by Core only")


class TaskSourcePort(Protocol):
    kind: TaskSourceKind

    async def query_task_projection_snapshots(
        self, query: TaskProjectionSnapshotQuery, *, capability: object
    ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None: ...

    async def command_task(
        self,
        source_id: UUID,
        command: TaskCommand,
        *,
        owner_scope_id: UUID,
        expected_source_version: int,
        idempotency_key: str,
    ) -> TaskCommandReceiptLike: ...

    async def resolve_task_detail(
        self, source_id: UUID, *, owner_scope_id: UUID
    ) -> Mapping[str, object]: ...


class TaskCommandReceiptLike(Protocol):
    source_version: int
    accepted: bool
    unknown: bool


type TaskSourceFactory = Callable[[], TaskSourcePort]


@dataclass(frozen=True, slots=True)
class TaskSourceRegistration:
    kind: TaskSourceKind
    source: TaskSourcePort


__all__ = [
    "ProjectionReadCapability",
    "TaskSourcePort",
    "TaskCommandReceiptLike",
    "TaskSourceRegistration",
]
