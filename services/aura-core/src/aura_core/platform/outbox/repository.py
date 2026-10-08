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

        existing = await session.get(OutboxRow, command.id)
        if existing is None:
            session.add(
                OutboxRow(
                    id=command.id,
                    topic=command.topic,
                    aggregate_id=command.run_id,
                    payload=json.loads(command.wire_payload()),
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
            OutboxCommand.from_record(row.id, row.topic, row.payload, row.created_at or now())
            for row in rows
        ]

    async def mark_published(self, session: AsyncSession, command_id: UUID) -> None:
        row = await session.get(OutboxRow, command_id, with_for_update=True)
        if row is not None and row.published_at is None:
            row.published_at = now()
