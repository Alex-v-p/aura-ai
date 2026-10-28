"""Execution-owned persistence for the Task Center projection."""

from __future__ import annotations

import asyncio
from collections import deque
from datetime import UTC, datetime
from uuid import UUID, uuid4

from aura_core.domains.execution.tasks.contracts import (
    TaskListFilter,
    TaskNormalizedStatus,
    TaskProjectionRebuildState,
    TaskProjectionRow,
    TaskProjectionSnapshot,
    TaskSourceKind,
    decode_cursor,
    encode_cursor,
    snapshot_to_row,
)


class TaskProjectionRepository:
    async def upsert(
        self, snapshot: TaskProjectionSnapshot
    ) -> TaskProjectionRow:  # pragma: no cover - protocol-like base
        raise NotImplementedError

    async def get(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> TaskProjectionRow | None:  # pragma: no cover
        raise NotImplementedError

    async def list(
        self, query: TaskListFilter
    ) -> tuple[tuple[TaskProjectionRow, ...], str | None]:  # pragma: no cover
        raise NotImplementedError

    async def mark_source_unknown(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> TaskProjectionRow | None:  # pragma: no cover
        raise NotImplementedError

    async def recover_unknown(
        self, snapshot: TaskProjectionSnapshot
    ) -> TaskProjectionRow:  # pragma: no cover
        raise NotImplementedError

    async def stale_source_rows(
        self, source_kind: TaskSourceKind, seen: set[UUID]
    ) -> tuple[TaskProjectionRow, ...]:  # pragma: no cover
        raise NotImplementedError

    async def begin_rebuild(
        self, source_kind: TaskSourceKind
    ) -> TaskProjectionRebuildState:  # pragma: no cover
        raise NotImplementedError

    async def save_rebuild_checkpoint(
        self, state: TaskProjectionRebuildState
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def complete_rebuild(self, state: TaskProjectionRebuildState) -> None:  # pragma: no cover
        raise NotImplementedError

    async def tombstone_stale(
        self, source_kind: TaskSourceKind, seen: set[UUID], started_at: datetime
    ) -> int:  # pragma: no cover
        raise NotImplementedError

    async def command_receipt(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        command: str,
        idempotency_key: str,
    ) -> object | None:  # pragma: no cover
        raise NotImplementedError

    async def save_command_receipt(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        command: str,
        idempotency_key: str,
        receipt: object,
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def enqueue_reconciliation(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def claim_reconciliations(
        self, limit: int = 100
    ) -> tuple[tuple[TaskSourceKind, UUID], ...]:  # pragma: no cover
        raise NotImplementedError

    async def complete_reconciliation(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def append_task_event(
        self, owner_scope_id: UUID, event: object
    ) -> None:  # pragma: no cover
        raise NotImplementedError

    async def replay_task_events(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        after: UUID | None = None,
    ) -> tuple[object, ...]:  # pragma: no cover
        raise NotImplementedError

    async def prune_task_events(self, *, keep: int = 10000) -> int:  # pragma: no cover
        raise NotImplementedError


class MemoryTaskProjectionRepository(TaskProjectionRepository):
    """Deterministic repository used by local composition and unit tests."""

    def __init__(self) -> None:
        self.rows: dict[tuple[TaskSourceKind, UUID], TaskProjectionRow] = {}
        self._lock = asyncio.Lock()
        self.rebuilds: dict[TaskSourceKind, TaskProjectionRebuildState] = {}
        self.rebuild_baselines: dict[UUID, dict[tuple[TaskSourceKind, UUID], int]] = {}
        self.command_receipts: dict[tuple[UUID, TaskSourceKind, UUID, str, str], object] = {}
        self.reconciliation_items: set[tuple[TaskSourceKind, UUID]] = set()
        self.reconciliation_attempts: dict[tuple[TaskSourceKind, UUID], int] = {}
        self.task_events: deque[tuple[UUID, UUID, object]] = deque(maxlen=1024)
        self._event_sequence = 0

    async def upsert(self, snapshot: TaskProjectionSnapshot) -> TaskProjectionRow:
        async with self._lock:
            key = snapshot.key
            existing = self.rows.get(key)
            if existing is not None and existing.source_version >= snapshot.source_version:
                return existing
            projection_version = (existing.projection_version + 1) if existing else 1
            row = snapshot_to_row(snapshot, projection_version)
            self.rows[key] = row
            return row

    async def get(self, source_kind: TaskSourceKind, source_id: UUID) -> TaskProjectionRow | None:
        return self.rows.get((source_kind, source_id))

    async def list(self, query: TaskListFilter) -> tuple[tuple[TaskProjectionRow, ...], str | None]:
        rows = [row for row in self.rows.values() if row.owner_scope_id == query.owner_scope_id]
        if query.source_kind is not None:
            rows = [row for row in rows if row.source_kind is query.source_kind]
        if query.status is not None:
            rows = [row for row in rows if row.status is query.status]
        if query.updated_from is not None:
            rows = [row for row in rows if row.updated_at >= query.updated_from]
        if query.updated_to is not None:
            rows = [row for row in rows if row.updated_at <= query.updated_to]
        rows.sort(key=lambda row: (row.updated_at, row.source_id), reverse=True)
        if query.cursor:
            cursor_at, cursor_id = decode_cursor(query.cursor)
            rows = [row for row in rows if (row.updated_at, row.source_id) < (cursor_at, cursor_id)]
        page = rows[: query.limit]
        next_cursor = (
            encode_cursor(page[-1].updated_at, page[-1].source_id)
            if len(rows) > len(page) and page
            else None
        )
        return tuple(page), next_cursor

    async def mark_source_unknown(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> TaskProjectionRow | None:
        async with self._lock:
            old = self.rows.get((source_kind, source_id))
            if old is None:
                return None
            if old.status is TaskNormalizedStatus.UNKNOWN:
                return old
            snapshot = TaskProjectionSnapshot(
                owner_scope_id=old.owner_scope_id,
                source_kind=old.source_kind,
                source_id=old.source_id,
                source_version=old.source_version,
                status=TaskNormalizedStatus.UNKNOWN,
                source_status=old.source_status,
                source_outcome=old.source_outcome,
                stage=old.stage,
                progress=old.progress,
                capabilities=type(old.capabilities)(),
                route_key=old.route_key,
                failure_class=old.failure_class,
                created_at=old.created_at,
                updated_at=datetime.now(UTC),
                finished_at=old.finished_at,
                source_deleted=old.source_deleted,
            )
            row = snapshot_to_row(snapshot, old.projection_version + 1)
            self.rows[(source_kind, source_id)] = row
            return row

    async def recover_unknown(self, snapshot: TaskProjectionSnapshot) -> TaskProjectionRow:
        async with self._lock:
            old = self.rows.get(snapshot.key)
            if old is None:
                projection_version = 1
            elif old.status is not TaskNormalizedStatus.UNKNOWN:
                return old
            elif old.source_version != snapshot.source_version:
                return old
            else:
                projection_version = old.projection_version + 1
            row = snapshot_to_row(snapshot, projection_version)
            self.rows[snapshot.key] = row
            return row

    async def stale_source_rows(
        self, source_kind: TaskSourceKind, seen: set[UUID]
    ) -> tuple[TaskProjectionRow, ...]:
        return tuple(
            row
            for (kind, source_id), row in self.rows.items()
            if kind is source_kind and source_id not in seen
        )

    async def begin_rebuild(self, source_kind: TaskSourceKind) -> TaskProjectionRebuildState:
        async with self._lock:
            current = self.rebuilds.get(source_kind)
            if current is not None and not current.completed:
                return current
            state = TaskProjectionRebuildState(source_kind, uuid4(), None, datetime.now(UTC))
            self.rebuilds[source_kind] = state
            self.rebuild_baselines[state.pass_id] = {
                key: row.projection_version
                for key, row in self.rows.items()
                if key[0] is source_kind
            }
            return state

    async def save_rebuild_checkpoint(self, state: TaskProjectionRebuildState) -> None:
        async with self._lock:
            current = self.rebuilds.get(state.source_kind)
            if current is not None and current.pass_id == state.pass_id:
                self.rebuilds[state.source_kind] = state

    async def complete_rebuild(self, state: TaskProjectionRebuildState) -> None:
        await self.save_rebuild_checkpoint(
            TaskProjectionRebuildState(
                state.source_kind,
                state.pass_id,
                None,
                state.started_at,
                state.seen_ids,
                True,
            )
        )

    async def tombstone_stale(
        self, source_kind: TaskSourceKind, seen: set[UUID], started_at: datetime
    ) -> int:
        count = 0
        state = self.rebuilds.get(source_kind)
        baseline: dict[tuple[TaskSourceKind, UUID], int] = (
            self.rebuild_baselines.get(state.pass_id, {}) if state else {}
        )
        async with self._lock:
            for key, baseline_projection_version in baseline.items():
                stale = self.rows.get(key)
                if (
                    key[1] in seen
                    or stale is None
                    or stale.source_deleted
                    or stale.projection_version != baseline_projection_version
                ):
                    continue
                tombstone = TaskProjectionSnapshot(
                    owner_scope_id=stale.owner_scope_id,
                    source_kind=stale.source_kind,
                    source_id=stale.source_id,
                    source_version=stale.source_version,
                    status=TaskNormalizedStatus.UNKNOWN,
                    source_status=stale.source_status,
                    source_outcome=stale.source_outcome,
                    stage=None,
                    progress=None,
                    capabilities=type(stale.capabilities)(),
                    route_key=stale.route_key,
                    failure_class=None,
                    created_at=stale.created_at,
                    updated_at=datetime.now(UTC),
                    finished_at=stale.finished_at,
                    source_deleted=False,
                )
                self.rows[key] = snapshot_to_row(tombstone, stale.projection_version + 1)
                count += 1
        del started_at
        return count

    async def command_receipt(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        command: str,
        idempotency_key: str,
    ) -> object | None:
        return self.command_receipts.get(
            (owner_scope_id, source_kind, source_id, command, idempotency_key)
        )

    async def save_command_receipt(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        command: str,
        idempotency_key: str,
        receipt: object,
    ) -> None:
        async with self._lock:
            self.command_receipts[
                (owner_scope_id, source_kind, source_id, command, idempotency_key)
            ] = receipt

    async def enqueue_reconciliation(self, source_kind: TaskSourceKind, source_id: UUID) -> None:
        async with self._lock:
            self.reconciliation_items.add((source_kind, source_id))
            self.reconciliation_attempts.setdefault((source_kind, source_id), 0)

    async def claim_reconciliations(
        self, limit: int = 100
    ) -> tuple[tuple[TaskSourceKind, UUID], ...]:
        async with self._lock:
            values = tuple(
                item
                for item in sorted(self.reconciliation_items, key=lambda item: str(item[1]))
                if self.reconciliation_attempts.get(item, 0) < 8
            )[:limit]
            for item in values:
                self.reconciliation_attempts[item] = self.reconciliation_attempts.get(item, 0) + 1
            return values

    async def complete_reconciliation(self, source_kind: TaskSourceKind, source_id: UUID) -> None:
        async with self._lock:
            self.reconciliation_items.discard((source_kind, source_id))
            self.reconciliation_attempts.pop((source_kind, source_id), None)

    async def append_task_event(self, owner_scope_id: UUID, event: object) -> None:
        event_id = getattr(event, "event_id", None)
        if isinstance(event_id, UUID):
            async with self._lock:
                if not any(item[0] == event_id for item in self.task_events):
                    self._event_sequence += 1
                    if hasattr(event, "sequence"):
                        try:
                            object.__setattr__(event, "sequence", self._event_sequence)
                        except AttributeError, TypeError:
                            pass
                    self.task_events.append((event_id, owner_scope_id, event))

    async def replay_task_events(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        after: UUID | None = None,
    ) -> tuple[object, ...]:
        events = [
            event
            for _, owner, event in self.task_events
            if owner == owner_scope_id
            and getattr(event, "source_kind", None) is source_kind
            and getattr(event, "source_id", None) == source_id
        ]
        if after is not None:
            for index, event in enumerate(events):
                if getattr(event, "event_id", None) == after:
                    events = events[index + 1 :]
                    break
            else:
                raise LookupError("task event cursor is outside replay window")
        return tuple(events)

    async def prune_task_events(self, *, keep: int = 10000) -> int:
        async with self._lock:
            keep = max(1, keep)
            removed = max(0, len(self.task_events) - keep)
            while len(self.task_events) > keep:
                self.task_events.popleft()
            return removed


__all__ = ["TaskProjectionRepository", "MemoryTaskProjectionRepository"]
