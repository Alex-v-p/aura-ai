"""Session-scoped PostgreSQL conversation repository."""

from dataclasses import dataclass
from datetime import datetime
from typing import cast
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.domains.interaction.conversations.dto import (
    AgentAssignment,
    AssignmentReason,
    Message,
    MessageRole,
    MessageState,
    now,
)
from aura_core.domains.interaction.conversations.persistence import (
    ConversationAgentAssignmentRow,
    ConversationRow,
    IdempotencyRow,
    MessageRow,
)


@dataclass(slots=True)
class ConversationRecord:
    id: UUID
    principal_id: UUID
    title: str
    agent_profile_id: UUID
    agent_revision_id: UUID
    model_id: str
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class IdempotencyRecord:
    fingerprint: str
    response: dict[str, str]


class SqlConversationRepository:
    @staticmethod
    def _record(row: ConversationRow) -> ConversationRecord:
        return ConversationRecord(
            row.id,
            row.principal_id,
            row.title,
            row.agent_profile_id,
            row.agent_revision_id,
            row.model_id,
            row.version,
            row.created_at or now(),
            row.updated_at or now(),
        )

    @staticmethod
    def _message(row: MessageRow) -> Message:
        return Message(
            row.conversation_id,
            MessageRole(row.role),
            row.content,
            MessageState(row.state),
            row.run_id,
            row.id,
            row.created_at or now(),
            row.updated_at or now(),
        )

    async def exists(self, session: AsyncSession, conversation_id: UUID) -> bool:
        return await session.get(ConversationRow, conversation_id) is not None

    async def get(
        self,
        session: AsyncSession,
        conversation_id: UUID,
        principal_id: UUID | None = None,
        *,
        lock: bool = False,
    ) -> ConversationRecord | None:
        query = select(ConversationRow).where(ConversationRow.id == conversation_id)
        if principal_id is not None:
            query = query.where(ConversationRow.principal_id == principal_id)
        row = (
            await session.execute(query.with_for_update() if lock else query)
        ).scalar_one_or_none()
        return self._record(row) if row else None

    async def list(
        self,
        session: AsyncSession,
        principal_id: UUID,
        limit: int,
        cursor: tuple[datetime, UUID] | None,
    ) -> list[ConversationRecord]:
        query = (
            select(ConversationRow)
            .where(ConversationRow.principal_id == principal_id)
            .order_by(ConversationRow.updated_at.desc(), ConversationRow.id.desc())
        )
        if cursor:
            cursor_time, cursor_id = cursor
            query = query.where(
                or_(
                    ConversationRow.updated_at < cursor_time,
                    and_(
                        ConversationRow.updated_at == cursor_time,
                        ConversationRow.id < cursor_id,
                    ),
                )
            )
        rows = (await session.execute(query.limit(limit))).scalars().all()
        return [self._record(row) for row in rows]

    async def messages(self, session: AsyncSession, conversation_id: UUID) -> list[Message]:
        rows = (
            (
                await session.execute(
                    select(MessageRow)
                    .where(MessageRow.conversation_id == conversation_id)
                    .order_by(MessageRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [self._message(row) for row in rows]

    async def assignments(
        self, session: AsyncSession, conversation_id: UUID
    ) -> list[AgentAssignment]:
        rows = (
            await session.execute(
                select(ConversationAgentAssignmentRow)
                .where(ConversationAgentAssignmentRow.conversation_id == conversation_id)
                .order_by(
                    ConversationAgentAssignmentRow.created_at,
                    ConversationAgentAssignmentRow.id,
                )
            )
        ).scalars().all()
        return [
            AgentAssignment(
                row.agent_profile_id,
                row.agent_revision_id,
                AssignmentReason(row.reason),
                row.effective_after_message_id,
                row.created_at or now(),
                row.id,
            )
            for row in rows
        ]

    async def latest_message_id(self, session: AsyncSession, conversation_id: UUID) -> UUID | None:
        row = (
            await session.execute(
                select(MessageRow.id)
                .where(MessageRow.conversation_id == conversation_id)
                .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return row

    async def message(self, session: AsyncSession, message_id: UUID) -> Message | None:
        row = await session.get(MessageRow, message_id)
        return self._message(row) if row else None

    def stage_conversation(self, session: AsyncSession, record: ConversationRecord) -> None:
        session.add(
            ConversationRow(
                id=record.id,
                principal_id=record.principal_id,
                title=record.title,
                agent_profile_id=record.agent_profile_id,
                agent_revision_id=record.agent_revision_id,
                model_id=record.model_id,
                version=record.version,
                created_at=record.created_at,
                updated_at=record.updated_at,
            )
        )

    def stage_message(self, session: AsyncSession, message: Message) -> None:
        session.add(
            MessageRow(
                id=message.id,
                conversation_id=message.conversation_id,
                run_id=message.run_id,
                role=message.role.value,
                content=message.content,
                state=message.state.value,
                created_at=message.created_at,
                updated_at=message.updated_at,
            )
        )

    def stage_assignment(
        self, session: AsyncSession, assignment: AgentAssignment, conversation_id: UUID
    ) -> None:
        session.add(
            ConversationAgentAssignmentRow(
                id=assignment.id,
                conversation_id=conversation_id,
                agent_profile_id=assignment.agent_profile_id,
                agent_revision_id=assignment.agent_revision_id,
                reason=assignment.reason.value,
                effective_after_message_id=assignment.effective_after_message_id,
                created_at=assignment.created_at,
            )
        )

    async def update(
        self,
        session: AsyncSession,
        record: ConversationRecord,
    ) -> None:
        row = await session.get(ConversationRow, record.id, with_for_update=True)
        if row is None:
            return
        row.model_id = record.model_id
        row.agent_profile_id = record.agent_profile_id
        row.agent_revision_id = record.agent_revision_id
        row.version = record.version
        row.updated_at = record.updated_at

    async def append_message(
        self,
        session: AsyncSession,
        message_id: UUID,
        text: str,
        state: MessageState,
    ) -> Message | None:
        row = await session.get(MessageRow, message_id, with_for_update=True)
        if row is None:
            return None
        row.content += text
        row.state = state.value
        row.updated_at = now()
        return self._message(row)

    async def set_message_state(
        self, session: AsyncSession, message_id: UUID, state: MessageState
    ) -> None:
        row = await session.get(MessageRow, message_id, with_for_update=True)
        if row is not None:
            row.state = state.value
            row.updated_at = now()

    async def idempotency(
        self, session: AsyncSession, issuer: str, subject: str, key: str
    ) -> IdempotencyRecord | None:
        row = await session.get(IdempotencyRow, (issuer, subject, key))
        if row is None:
            return None
        return IdempotencyRecord(row.fingerprint, cast(dict[str, str], row.response))

    def stage_idempotency(
        self,
        session: AsyncSession,
        issuer: str,
        subject: str,
        key: str,
        fingerprint: str,
        response: dict[str, str],
    ) -> None:
        session.add(
            IdempotencyRow(
                principal_issuer=issuer,
                principal_subject=subject,
                idempotency_key=key,
                fingerprint=fingerprint,
                response=response,
            )
        )
