"""SQL adapter for the execution-owned task projection table.

The adapter uses only the projection table and JSON-free scalar columns.  It
does not import or inspect any source domain mapping.
"""

# ruff: noqa: E501

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.execution.tasks.contracts import (
    TaskCommand,
    TaskCommandReceipt,
    TaskCommandState,
    TaskListFilter,
    TaskNormalizedStatus,
    TaskProjectionCapability,
    TaskProjectionRebuildState,
    TaskProjectionRow,
    TaskProjectionSnapshot,
    TaskSourceKind,
    decode_cursor,
    encode_cursor,
)
from aura_core.domains.execution.tasks.repository import TaskProjectionRepository


def _row(value: Mapping[str, Any]) -> TaskProjectionRow:
    return TaskProjectionRow(
        owner_scope_id=UUID(str(value["owner_scope_id"])),
        source_kind=TaskSourceKind(str(value["source_kind"])),
        source_id=UUID(str(value["source_id"])),
        source_version=int(value["source_version"]),
        status=TaskNormalizedStatus(str(value["status"])),
        source_status=str(value["source_status"]),
        source_outcome=value["source_outcome"],
        stage=value["stage"],
        progress=float(value["progress"]) if value["progress"] is not None else None,
        capabilities=TaskProjectionCapability(
            bool(value["can_cancel"]), bool(value["can_retry"]), bool(value["has_source_detail"])
        ),
        route_key=str(value["route_key"]),
        failure_class=value["failure_class"],
        created_at=value["created_at"],
        updated_at=value["updated_at"],
        finished_at=value["finished_at"],
        source_deleted=bool(value["source_deleted"]),
        projection_version=int(value["projection_version"]),
    )


class SqlTaskProjectionRepository(TaskProjectionRepository):
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def upsert(self, snapshot: TaskProjectionSnapshot) -> TaskProjectionRow:
        async with self.sessions() as session:
            await session.execute(
                text("""
                INSERT INTO task_projection
                    (owner_scope_id, source_kind, source_id, source_version, status,
                     source_status, source_outcome, stage, progress, can_cancel,
                     can_retry, has_source_detail, route_key, failure_class,
                     created_at, updated_at, finished_at, source_deleted, projection_version)
                VALUES (:owner_scope_id, :source_kind, :source_id, :source_version, :status,
                        :source_status, :source_outcome, :stage, :progress, :can_cancel,
                        :can_retry, :has_source_detail, :route_key, :failure_class,
                        :created_at, :updated_at, :finished_at, :source_deleted, 1)
                ON CONFLICT (source_kind, source_id) DO UPDATE SET
                    owner_scope_id = EXCLUDED.owner_scope_id,
                    source_version = EXCLUDED.source_version, status = EXCLUDED.status,
                    source_status = EXCLUDED.source_status, source_outcome = EXCLUDED.source_outcome,
                    stage = EXCLUDED.stage, progress = EXCLUDED.progress,
                    can_cancel = EXCLUDED.can_cancel, can_retry = EXCLUDED.can_retry,
                    has_source_detail = EXCLUDED.has_source_detail, route_key = EXCLUDED.route_key,
                    failure_class = EXCLUDED.failure_class, updated_at = EXCLUDED.updated_at,
                    finished_at = EXCLUDED.finished_at, source_deleted = EXCLUDED.source_deleted,
                    projection_version = task_projection.projection_version + 1
                WHERE task_projection.source_version < EXCLUDED.source_version
            """),
                {
                    "owner_scope_id": snapshot.owner_scope_id,
                    "source_kind": snapshot.source_kind.value,
                    "source_id": snapshot.source_id,
                    "source_version": snapshot.source_version,
                    "status": snapshot.status.value,
                    "source_status": snapshot.source_status,
                    "source_outcome": snapshot.source_outcome,
                    "stage": snapshot.stage,
                    "progress": snapshot.progress,
                    "can_cancel": snapshot.capabilities.can_cancel,
                    "can_retry": snapshot.capabilities.can_retry,
                    "has_source_detail": snapshot.capabilities.has_source_detail,
                    "route_key": snapshot.route_key,
                    "failure_class": snapshot.failure_class,
                    "created_at": snapshot.created_at,
                    "updated_at": snapshot.updated_at,
                    "finished_at": snapshot.finished_at,
                    "source_deleted": snapshot.source_deleted,
                },
            )
            await session.commit()
            result = await session.execute(
                text("SELECT * FROM task_projection WHERE source_kind=:kind AND source_id=:id"),
                {"kind": snapshot.source_kind.value, "id": snapshot.source_id},
            )
            row = result.mappings().first()
            if row is None:
                raise RuntimeError("task projection upsert did not return a row")
            return _row(cast(Mapping[str, Any], row))

    async def get(self, source_kind: TaskSourceKind, source_id: UUID) -> TaskProjectionRow | None:
        async with self.sessions() as session:
            result = await session.execute(
                text("SELECT * FROM task_projection WHERE source_kind=:kind AND source_id=:id"),
                {"kind": source_kind.value, "id": source_id},
            )
            row = result.mappings().first()
            return _row(cast(Mapping[str, Any], row)) if row is not None else None

    async def list(self, query: TaskListFilter) -> tuple[tuple[TaskProjectionRow, ...], str | None]:
        conditions = ["owner_scope_id=:owner"]
        params: dict[str, object] = {"owner": query.owner_scope_id, "limit": query.limit + 1}
        if query.source_kind is not None:
            conditions.append("source_kind=:kind")
            params["kind"] = query.source_kind.value
        if query.status is not None:
            conditions.append("status=:status")
            params["status"] = query.status.value
        if query.updated_from is not None:
            conditions.append("updated_at>=:updated_from")
            params["updated_from"] = query.updated_from
        if query.updated_to is not None:
            conditions.append("updated_at<=:updated_to")
            params["updated_to"] = query.updated_to
        if query.cursor:
            cursor_at, cursor_id = decode_cursor(query.cursor)
            conditions.append("(updated_at, source_id)<(:cursor_at, :cursor_id)")
            params.update({"cursor_at": cursor_at, "cursor_id": cursor_id})
        result_sql = (
            "SELECT * FROM task_projection WHERE "
            + " AND ".join(conditions)
            + " ORDER BY updated_at DESC, source_id DESC LIMIT :limit"
        )
        async with self.sessions() as session:
            result = await session.execute(text(result_sql), params)
            values = [_row(cast(Mapping[str, Any], item)) for item in result.mappings().all()]
        has_next = len(values) > query.limit
        page = tuple(values[: query.limit])
        return page, encode_cursor(
            page[-1].updated_at, page[-1].source_id
        ) if has_next and page else None

    async def mark_source_unknown(
        self, source_kind: TaskSourceKind, source_id: UUID
    ) -> TaskProjectionRow | None:
        async with self.sessions() as session:
            await session.execute(
                text(
                    "UPDATE task_projection SET status='unknown', can_cancel=FALSE, can_retry=FALSE, updated_at=CURRENT_TIMESTAMP, projection_version=projection_version+1 WHERE source_kind=:kind AND source_id=:id AND status <> 'unknown'"
                ),
                {"kind": source_kind.value, "id": source_id},
            )
            await session.commit()
        return await self.get(source_kind, source_id)

    async def recover_unknown(self, snapshot: TaskProjectionSnapshot) -> TaskProjectionRow:
        """Recover local uncertainty without rewriting authoritative version."""
        async with self.sessions() as session:
            await session.execute(
                text("""
                    UPDATE task_projection
                       SET status=:status, source_status=:source_status,
                           source_outcome=:source_outcome, stage=:stage,
                           progress=:progress, can_cancel=:can_cancel,
                           can_retry=:can_retry, has_source_detail=:has_source_detail,
                           route_key=:route_key, failure_class=:failure_class,
                           updated_at=:updated_at, finished_at=:finished_at,
                           source_deleted=:source_deleted,
                           projection_version=projection_version+1
                     WHERE source_kind=:kind AND source_id=:id AND status='unknown'
                       AND source_version=:source_version
                """),
                {
                    "kind": snapshot.source_kind.value,
                    "id": snapshot.source_id,
                    "status": snapshot.status.value,
                    "source_status": snapshot.source_status,
                    "source_outcome": snapshot.source_outcome,
                    "stage": snapshot.stage,
                    "progress": snapshot.progress,
                    "can_cancel": snapshot.capabilities.can_cancel,
                    "can_retry": snapshot.capabilities.can_retry,
                    "has_source_detail": snapshot.capabilities.has_source_detail,
                    "route_key": snapshot.route_key,
                    "failure_class": snapshot.failure_class,
                    "updated_at": snapshot.updated_at,
                    "finished_at": snapshot.finished_at,
                    "source_deleted": snapshot.source_deleted,
                    "source_version": snapshot.source_version,
                },
            )
            await session.commit()
        row = await self.get(snapshot.source_kind, snapshot.source_id)
        if row is None:
            raise RuntimeError("task projection recovery did not return a row")
        return row

    async def stale_source_rows(
        self, source_kind: TaskSourceKind, seen: set[UUID]
    ) -> tuple[TaskProjectionRow, ...]:
        async with self.sessions() as session:
            baseline_result = await session.execute(
                text(
                    "SELECT baseline_source_ids FROM task_projection_rebuilds WHERE source_kind=:kind"
                ),
                {"kind": source_kind.value},
            )
            baseline_row = baseline_result.mappings().first()
            baseline_values: list[object] = (
                json.loads(baseline_row["baseline_source_ids"] or "[]")
                if baseline_row is not None
                else []
            )
            baseline_ids: set[UUID] = {UUID(str(value)) for value in baseline_values}
            result = await session.execute(
                text("SELECT * FROM task_projection WHERE source_kind=:kind"),
                {"kind": source_kind.value},
            )
            return tuple(
                _row(cast(Mapping[str, Any], item))
                for item in result.mappings().all()
                if UUID(str(item["source_id"])) in baseline_ids
                and UUID(str(item["source_id"])) not in seen
            )

    async def begin_rebuild(self, source_kind: TaskSourceKind) -> TaskProjectionRebuildState:
        async with self.sessions() as session:
            result = await session.execute(
                text("SELECT * FROM task_projection_rebuilds WHERE source_kind=:kind"),
                {"kind": source_kind.value},
            )
            row = result.mappings().first()
            if row is not None and not bool(row["completed"]):
                seen = json.loads(row["seen_source_ids"] or "[]")
                return TaskProjectionRebuildState(
                    source_kind,
                    row["pass_id"],
                    row["cursor"],
                    row["started_at"],
                    tuple(UUID(str(item)) for item in seen),
                    False,
                )
            pass_id = UUID(bytes=__import__("secrets").token_bytes(16))
            started_at = datetime.now(UTC)
            await session.execute(
                text("""
                    INSERT INTO task_projection_rebuilds
                        (source_kind, pass_id, cursor, started_at, baseline_source_ids,
                         baseline_projection_versions,
                         seen_source_ids, completed)
                    VALUES (:kind, :pass_id, NULL, :started,
                            (SELECT COALESCE(jsonb_agg(source_id), '[]'::jsonb)
                               FROM task_projection WHERE source_kind=:kind),
                            (SELECT COALESCE(jsonb_object_agg(source_id::text, projection_version), '{}'::jsonb)
                               FROM task_projection WHERE source_kind=:kind),
                            '[]'::jsonb, FALSE)
                    ON CONFLICT (source_kind) DO UPDATE SET
                        pass_id=EXCLUDED.pass_id, cursor=NULL,
                        started_at=EXCLUDED.started_at,
                        baseline_source_ids=EXCLUDED.baseline_source_ids,
                        baseline_projection_versions=EXCLUDED.baseline_projection_versions,
                        seen_source_ids='[]'::jsonb,
                        completed=FALSE, updated_at=CURRENT_TIMESTAMP
                """),
                {"kind": source_kind.value, "pass_id": pass_id, "started": started_at},
            )
            await session.commit()
            return TaskProjectionRebuildState(source_kind, pass_id, None, started_at)

    async def save_rebuild_checkpoint(self, state: TaskProjectionRebuildState) -> None:
        async with self.sessions() as session:
            await session.execute(
                text("""
                    UPDATE task_projection_rebuilds
                       SET cursor=:cursor, seen_source_ids=CAST(:seen AS jsonb),
                           updated_at=CURRENT_TIMESTAMP
                     WHERE source_kind=:kind AND pass_id=:pass_id AND completed=FALSE
                """),
                {
                    "kind": state.source_kind.value,
                    "pass_id": state.pass_id,
                    "cursor": state.cursor,
                    "seen": json.dumps([str(item) for item in state.seen_ids]),
                },
            )
            await session.commit()

    async def complete_rebuild(self, state: TaskProjectionRebuildState) -> None:
        async with self.sessions() as session:
            await session.execute(
                text(
                    "UPDATE task_projection_rebuilds SET completed=TRUE, cursor=NULL, updated_at=CURRENT_TIMESTAMP WHERE source_kind=:kind AND pass_id=:pass_id"
                ),
                {"kind": state.source_kind.value, "pass_id": state.pass_id},
            )
            await session.commit()

    async def tombstone_stale(
        self, source_kind: TaskSourceKind, seen: set[UUID], started_at: datetime
    ) -> int:
        # Only rows present at pass start and whose execution-owned projection
        # revision is unchanged are settled.  Source timestamps are not a
        # fence: an authoritative source may legitimately retain an old time.
        async with self.sessions() as session:
            result = await session.execute(
                text("""
                    SELECT p.*, (b.baseline_projection_versions ->> p.source_id::text)::integer
                        AS baseline_projection_version
                      FROM task_projection p
                      JOIN task_projection_rebuilds b ON b.source_kind=p.source_kind
                     WHERE p.source_kind=:kind
                       AND b.completed=FALSE
                """),
                {"kind": source_kind.value},
            )
            candidates = [
                _row(cast(Mapping[str, Any], item))
                for item in result.mappings().all()
                if UUID(str(item["source_id"])) not in seen
                and item["baseline_projection_version"] is not None
                and int(item["projection_version"])
                == int(item["baseline_projection_version"])
                and not bool(item["source_deleted"])
            ]
            for row in candidates:
                await session.execute(
                    text(
                        """
                        UPDATE task_projection p
                           SET status='unknown', can_cancel=FALSE, can_retry=FALSE,
                               source_deleted=FALSE, updated_at=CURRENT_TIMESTAMP,
                               projection_version=p.projection_version+1
                         WHERE p.source_kind=:kind AND p.source_id=:id
                           AND p.projection_version=:projection_version
                        """
                    ),
                    {
                        "kind": source_kind.value,
                        "id": row.source_id,
                        "projection_version": row.projection_version,
                    },
                )
            await session.commit()
            return len(candidates)

    async def command_receipt(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        command: str,
        idempotency_key: str,
    ) -> TaskCommandReceipt | None:
        async with self.sessions() as session:
            result = await session.execute(
                text(
                    "SELECT * FROM task_projection_command_receipts WHERE owner_scope_id=:owner AND source_kind=:kind AND source_id=:id AND command=:command AND idempotency_key=:key"
                ),
                {
                    "owner": owner_scope_id,
                    "kind": source_kind.value,
                    "id": source_id,
                    "command": command,
                    "key": idempotency_key,
                },
            )
            row = result.mappings().first()
            if row is None:
                return None
            return TaskCommandReceipt(
                source_id,
                TaskCommand(command),
                TaskCommandState(row["state"]),
                int(row["source_version"]),
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
        if not isinstance(receipt, TaskCommandReceipt):
            raise TypeError("invalid task command receipt")
        async with self.sessions() as session:
            await session.execute(
                text("""
                INSERT INTO task_projection_command_receipts
                    (owner_scope_id, source_kind, source_id, command, idempotency_key, state, source_version)
                VALUES (:owner, :kind, :id, :command, :key, :state, :version)
                ON CONFLICT DO NOTHING
            """),
                {
                    "owner": owner_scope_id,
                    "kind": source_kind.value,
                    "id": source_id,
                    "command": command,
                    "key": idempotency_key,
                    "state": receipt.state.value,
                    "version": receipt.source_version,
                },
            )
            await session.commit()

    async def enqueue_reconciliation(self, source_kind: TaskSourceKind, source_id: UUID) -> None:
        async with self.sessions() as session:
            await session.execute(
                text("""
                    INSERT INTO task_projection_reconciliation_queue
                        (source_kind, source_id, next_attempt_at, completed_at)
                    VALUES (:kind, :id, CURRENT_TIMESTAMP, NULL)
                    ON CONFLICT (source_kind, source_id) DO UPDATE SET
                        next_attempt_at = CURRENT_TIMESTAMP,
                        completed_at = NULL
                """),
                {"kind": source_kind.value, "id": source_id},
            )
            await session.commit()

    async def claim_reconciliations(
        self, limit: int = 100
    ) -> tuple[tuple[TaskSourceKind, UUID], ...]:
        async with self.sessions() as session:
            result = await session.execute(
                text("""
                    SELECT source_kind, source_id
                      FROM task_projection_reconciliation_queue
                     WHERE completed_at IS NULL
                       AND next_attempt_at <= CURRENT_TIMESTAMP
                       AND attempts < 8
                     ORDER BY next_attempt_at, source_id
                     LIMIT :limit
                     FOR UPDATE SKIP LOCKED
                """),
                {"limit": limit},
            )
            values = tuple(
                (TaskSourceKind(str(item["source_kind"])), UUID(str(item["source_id"])))
                for item in result.mappings().all()
            )
            for kind, source_id in values:
                await session.execute(
                    text("""
                        UPDATE task_projection_reconciliation_queue
                           SET attempts = attempts + 1,
                               claimed_at = CURRENT_TIMESTAMP,
                               next_attempt_at = CURRENT_TIMESTAMP + LEAST(3600, POWER(2, attempts) * 5) * INTERVAL '1 second'
                         WHERE source_kind=:kind AND source_id=:id
                    """),
                    {"kind": kind.value, "id": source_id},
                )
            await session.commit()
            return values

    async def complete_reconciliation(self, source_kind: TaskSourceKind, source_id: UUID) -> None:
        async with self.sessions() as session:
            await session.execute(
                text("""
                    UPDATE task_projection_reconciliation_queue
                       SET completed_at = CURRENT_TIMESTAMP, claimed_at = NULL
                     WHERE source_kind=:kind AND source_id=:id
                """),
                {"kind": source_kind.value, "id": source_id},
            )
            await session.commit()

    async def append_task_event(self, owner_scope_id: UUID, event: object) -> None:
        event_id = getattr(event, "event_id", None)
        if not isinstance(event_id, UUID):
            raise TypeError("invalid task projection event")
        event_value = cast(Any, event)
        async with self.sessions() as session:
            await session.execute(
                text("""
                    INSERT INTO task_projection_events
                        (event_id, owner_scope_id, task_id, source_kind, source_id,
                         source_version, occurred_at)
                    VALUES (:event_id, :owner, :task_id, :kind, :source_id,
                            :version, :occurred_at)
                    ON CONFLICT (event_id) DO NOTHING
                """),
                {
                    "event_id": event_id,
                    "owner": owner_scope_id,
                    "task_id": event_value.task_id,
                    "kind": event_value.source_kind.value,
                    "source_id": event_value.source_id,
                    "version": event_value.source_version,
                    "occurred_at": event_value.occurred_at,
                },
            )
            await session.commit()

    async def replay_task_events(
        self,
        owner_scope_id: UUID,
        source_kind: TaskSourceKind,
        source_id: UUID,
        after: UUID | None = None,
    ) -> tuple[object, ...]:
        async with self.sessions() as session:
            params: dict[str, object] = {
                "owner": owner_scope_id,
                "kind": source_kind.value,
                "source_id": source_id,
                "limit": 100,
            }
            after_sequence: int | None = None
            if after is not None:
                cursor_result = await session.execute(
                    text("""
                        SELECT event_sequence
                          FROM task_projection_events
                         WHERE event_id=:event_id
                           AND owner_scope_id=:owner
                           AND source_kind=:kind
                           AND source_id=:source_id
                    """),
                    {
                        "event_id": after,
                        "owner": owner_scope_id,
                        "kind": source_kind.value,
                        "source_id": source_id,
                    },
                )
                cursor_row = cursor_result.mappings().first()
                if cursor_row is None:
                    raise LookupError("task event cursor is outside replay window")
                after_sequence = int(cursor_row["event_sequence"])
                params["after_sequence"] = after_sequence
                where_after = " AND event_sequence > :after_sequence"
            else:
                where_after = ""
            result = await session.execute(
                text(f"""
                    SELECT event_sequence, event_id, task_id, source_kind, source_id,
                           source_version, occurred_at
                      FROM task_projection_events
                     WHERE owner_scope_id=:owner AND source_kind=:kind
                       AND source_id=:source_id{where_after}
                     ORDER BY event_sequence
                     LIMIT :limit
                """),
                params,
            )
            from aura_core.domains.execution.tasks.service import TaskStreamEvent

            values = tuple(
                TaskStreamEvent(
                    UUID(str(item["event_id"])),
                    UUID(str(item["task_id"])),
                    TaskSourceKind(str(item["source_kind"])),
                    UUID(str(item["source_id"])),
                    int(item["source_version"]),
                    item["occurred_at"],
                    sequence=int(item["event_sequence"]),
                )
                for item in result.mappings().all()
            )
            if after is not None and not values:
                # A known cursor with no rows is a valid caught-up replay.  A
                # cursor older than retained history is detected by the
                # monotonic sequence floor below.
                floor_result = await session.execute(
                    text("""
                        SELECT MIN(event_sequence) AS floor
                          FROM task_projection_events
                         WHERE owner_scope_id=:owner
                           AND source_kind=:kind
                           AND source_id=:source_id
                    """),
                    {
                        "owner": owner_scope_id,
                        "kind": source_kind.value,
                        "source_id": source_id,
                    },
                )
                floor_row = floor_result.mappings().first()
                floor = floor_row["floor"] if floor_row is not None else None
                if floor is not None and after_sequence is not None and after_sequence < int(floor) - 1:
                    raise LookupError("task event cursor is outside replay window")
            return values

    async def prune_task_events(self, *, keep: int = 10000) -> int:
        """Apply the bounded deployment retention policy to the outbox."""
        async with self.sessions() as session:
            result = await session.execute(
                text("""
                    DELETE FROM task_projection_events
                     WHERE event_sequence < (
                         SELECT COALESCE(MAX(event_sequence), 0) - :keep + 1
                           FROM task_projection_events
                     )
                """),
                {"keep": max(1, keep)},
            )
            await session.commit()
            return int(cast(Any, result).rowcount or 0)


__all__ = ["SqlTaskProjectionRepository"]
