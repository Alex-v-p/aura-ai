"""Session-scoped PostgreSQL run repository."""

from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.domains.execution.runs.dto import Run, RunError, RunStatus
from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.execution.runs.persistence import RunEventRow, RunRow
from aura_core.domains.interaction.conversations.dto import now


class SqlRunRepository:
    @staticmethod
    def _run(row: RunRow) -> Run:
        error = row.error or None
        return Run(
            row.conversation_id,
            row.user_message_id,
            row.agent_revision_id,
            row.model_policy_revision_id,
            row.provider,
            row.model_id,
            row.retry_of_run_id,
            RunStatus(row.status),
            row.id,
            row.assistant_message_id,
            row.created_at or now(),
            row.started_at,
            row.finished_at,
            RunError(**cast(dict[str, Any], error)) if error else None,
            row.attempt_id,
            row.attempt_count,
            row.lease_expires_at,
        )

    async def get(
        self, session: AsyncSession, run_id: UUID, *, lock: bool = False
    ) -> Run | None:
        row = await session.get(RunRow, run_id, with_for_update=lock)
        return self._run(row) if row else None

    async def list(self, session: AsyncSession, conversation_id: UUID) -> list[Run]:
        rows = (
            await session.execute(
                select(RunRow)
                .where(RunRow.conversation_id == conversation_id)
                .order_by(RunRow.created_at)
            )
        ).scalars().all()
        return [self._run(row) for row in rows]

    async def active(
        self, session: AsyncSession, conversation_id: UUID, *, lock: bool = False
    ) -> Run | None:
        query = select(RunRow).where(
            RunRow.conversation_id == conversation_id,
            RunRow.status.in_(["queued", "running", "cancel_requested"]),
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        return self._run(row) if row else None

    async def expired(self, session: AsyncSession, cutoff: datetime) -> list[Run]:
        rows = (
            await session.execute(
                select(RunRow)
                .where(
                    RunRow.status == "running",
                    RunRow.lease_expires_at.is_not(None),
                    RunRow.lease_expires_at <= cutoff,
                )
                .with_for_update(skip_locked=True)
            )
        ).scalars().all()
        return [self._run(row) for row in rows]

    def stage(self, session: AsyncSession, run: Run) -> None:
        session.add(
            RunRow(
                id=run.id,
                conversation_id=run.conversation_id,
                user_message_id=run.user_message_id,
                assistant_message_id=run.assistant_message_id,
                agent_revision_id=run.agent_revision_id,
                model_policy_revision_id=run.model_policy_revision_id,
                provider=run.provider,
                model_id=run.model_id,
                retry_of_run_id=run.retry_of_run_id,
                status=run.status.value,
                error=self._error(run.error),
                attempt_id=run.attempt_id,
                attempt_count=run.attempt_count,
                lease_expires_at=run.lease_expires_at,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
            )
        )

    async def save(self, session: AsyncSession, run: Run) -> None:
        row = await session.get(RunRow, run.id, with_for_update=True)
        if row is None:
            return
        row.assistant_message_id = run.assistant_message_id
        row.status = run.status.value
        row.error = self._error(run.error)
        row.attempt_id = run.attempt_id
        row.attempt_count = run.attempt_count
        row.lease_expires_at = run.lease_expires_at
        row.started_at = run.started_at
        row.finished_at = run.finished_at

    @staticmethod
    def _error(error: RunError | None) -> dict[str, object] | None:
        if error is None:
            return None
        return {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "trace_id": error.trace_id,
        }

    async def persist_event(self, session: AsyncSession, event: RunEvent) -> None:
        if await session.get(RunEventRow, event.event_id) is None:
            session.add(
                RunEventRow(
                    id=event.event_id,
                    run_id=event.run_id,
                    conversation_id=event.conversation_id,
                    sequence=event.sequence,
                    event_type=event.event_type,
                    payload=cast(dict[str, object], event.payload()),
                    occurred_at=event.occurred_at,
                )
            )

    async def event_history(self, session: AsyncSession, run_id: UUID) -> list[RunEvent]:
        rows = (
            await session.execute(
                select(RunEventRow)
                .where(RunEventRow.run_id == run_id)
                .order_by(RunEventRow.sequence)
            )
        ).scalars().all()
        return [self._event(row) for row in rows]

    async def event(
        self, session: AsyncSession, event_id: UUID, run_id: UUID
    ) -> RunEvent | None:
        row = await session.get(RunEventRow, event_id)
        return self._event(row) if row is not None and row.run_id == run_id else None

    @staticmethod
    def _event(row: RunEventRow) -> RunEvent:
        return RunEvent(
            event_id=row.id,
            sequence=row.sequence,
            event_type=row.event_type,
            run_id=row.run_id,
            conversation_id=row.conversation_id,
            occurred_at=row.occurred_at,
            data=cast(dict[str, Any], row.payload.get("data", row.payload)),
            schema_version=int(cast(int | str, row.payload.get("schemaVersion", 1))),
        )
