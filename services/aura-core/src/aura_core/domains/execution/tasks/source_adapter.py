"""Composition adapters from source-owned public application APIs.

These adapters intentionally accept an application facade, never a snapshot
collection or persistence object.  Source domains retain the implementation
of their snapshot and command methods.
"""

from __future__ import annotations

from collections.abc import Mapping
from inspect import isawaitable
from typing import Any, cast
from uuid import UUID

from aura_core.domains.execution.tasks.contracts import (
    TaskCommand,
    TaskProjectionSnapshot,
    TaskProjectionSnapshotPage,
    TaskProjectionSnapshotQuery,
    TaskSourceKind,
)


class ApplicationTaskSourceAdapter:
    def __init__(self, kind: TaskSourceKind, application: object) -> None:
        self.kind = kind
        self.application = application
        self.__capability: object | None = None

    def __bind_capability(self, capability: object) -> None:  # pyright: ignore[reportUnusedFunction]
        if self.__capability is not None:
            raise PermissionError("task projection capability is already bound")
        self.__capability = capability
        bind_name = f"_{type(self.application).__name__}__bind_capability"
        bind = getattr(self.application, bind_name, None)
        if callable(bind):
            bind(capability)

    async def query_task_projection_snapshots(
        self, query: TaskProjectionSnapshotQuery, *, capability: Any
    ) -> TaskProjectionSnapshot | TaskProjectionSnapshotPage | None:
        if self.__capability is None or capability is not self.__capability:
            raise PermissionError("task projection capability is bound to Core composition")
        validate_name = f"_{type(self.application).__name__}__validate_capability"
        validate = getattr(self.application, validate_name, None)
        if callable(validate):
            validate(capability)
        method = getattr(self.application, "query_task_projection_snapshots", None)
        if not callable(method):
            raise TypeError("source application does not expose its projection query API")
        result = method(query, capability=capability)
        if isawaitable(result):
            result = await result
        return cast(TaskProjectionSnapshot | TaskProjectionSnapshotPage | None, result)

    async def command_task(
        self,
        source_id: UUID,
        command: TaskCommand,
        *,
        owner_scope_id: UUID,
        expected_source_version: int,
        idempotency_key: str,
    ) -> Any:
        method = getattr(self.application, "command_task", None)
        if not callable(method):
            raise TypeError("source application does not expose its command API")
        result = method(
            source_id,
            command,
            owner_scope_id=owner_scope_id,
            expected_source_version=expected_source_version,
            idempotency_key=idempotency_key,
        )
        if isawaitable(result):
            result = await result
        return result

    async def resolve_task_detail(
        self, source_id: UUID, *, owner_scope_id: UUID
    ) -> Mapping[str, object]:
        method = getattr(self.application, "resolve_task_detail", None)
        if not callable(method):
            raise TypeError("source application does not expose its detail API")
        result = method(source_id, owner_scope_id=owner_scope_id)
        if isawaitable(result):
            result = await result
        if not isinstance(result, Mapping):
            raise TypeError("source detail API returned an invalid value")
        return cast(Mapping[str, object], result)


__all__ = ["ApplicationTaskSourceAdapter"]
