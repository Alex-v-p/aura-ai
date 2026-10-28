"""Session-scoped transactional outbox repository."""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.domains.interaction.conversations.dto import now
from aura_core.platform.outbox.persistence import OutboxRow
from aura_core.platform.outbox.service import OutboxCommand


class SqlOutboxRepository:
    def stage(
        self,
        session: AsyncSession,
        *,
        command_id: UUID,
        run_id: UUID,
        conversation_id: UUID,
        correlation_id: UUID,
        causation_id: UUID,
    ) -> None:
        session.add(
            OutboxRow(
                id=command_id,
                topic="aura.runs.execute.v1",
                aggregate_type="run",
                aggregate_id=run_id,
                payload={
                    "schemaVersion": 1,
                    "runId": str(run_id),
                    "conversationId": str(conversation_id),
                    "correlationId": str(correlation_id),
                    "causationId": str(causation_id),
                },
            )
        )

    async def stage_command(
        self,
        session: AsyncSession,
        *,
        command: OutboxCommand,
    ) -> UUID:
        """Stage one generic identifier-only command transactionally.

        Callers own the topic and identifier schema.  The platform only
        persists the command envelope and never interprets its identifiers.
        """

        aggregate_id = command.aggregate_id or command.run_id
        if aggregate_id is None:
            raise ValueError("outbox command has no aggregate identity")
        payload = json.loads(command.wire_payload())
        aggregate_type = command.aggregate_type or "run"

        def matches(row: OutboxRow) -> bool:
            return (
                row.topic == command.topic
                and row.aggregate_type == aggregate_type
                and row.aggregate_id == aggregate_id
                and row.payload == payload
            )

        # ``session.get`` cannot see pending rows in ``session.new``. Check
        # them first so repeated staging in one transaction is idempotent and
        # a same-ID envelope conflict is rejected before flush/commit.
        for pending in session.new:
            if isinstance(pending, OutboxRow) and pending.id == command.id:
                if not matches(pending):
                    raise ValueError("outbox command id conflicts with staged envelope")
                return command.id

        # Source lifecycle staging is invoked while the authoritative
        # aggregate mutation still has pending rows.  Looking up an
        # idempotency key must not trigger an autoflush that can violate a
        # source-owned foreign-key ordering before the aggregate is complete.
        with session.no_autoflush:
            existing = await session.get(OutboxRow, command.id)
        if existing is not None:
            if not matches(existing):
                raise ValueError("outbox command id conflicts with persisted envelope")
            return command.id
        session.add(
            OutboxRow(
                id=command.id,
                topic=command.topic,
                aggregate_type=aggregate_type,
                aggregate_id=aggregate_id,
                payload=payload,
            )
        )
        return command.id

    async def pending(self, session: AsyncSession, limit: int) -> list[OutboxCommand]:
        rows = (
            (
                await session.execute(
                    select(OutboxRow)
                    .where(OutboxRow.published_at.is_(None))
                    .order_by(OutboxRow.created_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )
        return [
            OutboxCommand.from_record(
                row.id,
                row.topic,
                row.payload,
                row.created_at or now(),
                row.aggregate_type,
                row.aggregate_id,
            )
            for row in rows
        ]

    async def mark_published(self, session: AsyncSession, command_id: UUID) -> None:
        row = await session.get(OutboxRow, command_id, with_for_update=True)
        if row is not None and row.published_at is None:
            row.published_at = now()
