"""Private, automation-owned reminder proposals.

Proposals are deliberately distinct from governance approvals.  This module
owns the validated reminder arguments (including their private content), while
the approval domain stores only an identifier and lifecycle metadata.
"""

# ruff: noqa: E501

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import DateTime, String, Text, func, select
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Mapped, mapped_column

from aura_core.domains.automation.reminders.public import (
    ReminderProposal,
    ReminderProposalPort,
    canonical_due_at,
    ensure_due_at_future,
    normalize_content,
)
from aura_core.platform.database.base import Base

PROPOSAL_EXPIRY = timedelta(hours=1)


class ReminderProposalError(Exception):
    """Base class for invalid or inaccessible reminder proposals."""


class ReminderProposalNotFound(ReminderProposalError):
    pass


ReminderProposalRepository = ReminderProposalPort


def proposal_expiry(*, created_at: datetime, due_at: datetime) -> datetime:
    created = created_at.astimezone(UTC)
    due = canonical_due_at(due_at)
    return min(created + PROPOSAL_EXPIRY, due)


def _proposal(row: ReminderProposalRow) -> ReminderProposal:
    return ReminderProposal(
        row.id,
        row.principal_issuer,
        row.principal_subject,
        row.content,
        row.due_at.astimezone(UTC),
        row.run_id,
        row.conversation_id,
        row.tool_call_id,
        row.created_at.astimezone(UTC),
        row.expires_at.astimezone(UTC),
    )


class ReminderProposalRow(Base):
    __tablename__ = "reminder_proposals"
    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    principal_issuer: Mapped[str] = mapped_column(String(1024), nullable=False, index=True)
    principal_subject: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    run_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    conversation_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    tool_call_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryReminderProposalRepository:
    def __init__(self, *, clock: Callable[[], datetime] | None = None) -> None:
        self._clock = clock or (lambda: datetime.now(UTC))
        self._items: dict[UUID, ReminderProposal] = {}
        self._lock = asyncio.Lock()

    def _now(self) -> datetime:
        value = self._clock()
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    async def create(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        due_at: datetime,
        run_id: UUID,
        conversation_id: UUID,
        tool_call_id: UUID,
        now: datetime | None = None,
        transaction: object | None = None,
    ) -> ReminderProposal:
        del transaction
        if not issuer or not subject:
            raise ValueError("authenticated owner is required")
        created = (now or self._now()).astimezone(UTC)
        normalized_content = normalize_content(content)
        normalized_due = ensure_due_at_future(due_at, now=created)
        item = ReminderProposal(
            uuid4(),
            issuer,
            subject,
            normalized_content,
            normalized_due,
            run_id,
            conversation_id,
            tool_call_id,
            created,
            proposal_expiry(created_at=created, due_at=normalized_due),
        )
        async with self._lock:
            self._items[item.id] = item
        return item

    async def get(self, issuer: str, subject: str, proposal_id: UUID) -> ReminderProposal:
        item = self._items.get(proposal_id)
        if item is None or (item.issuer, item.subject) != (issuer, subject):
            raise ReminderProposalNotFound("reminder proposal not found")
        return item


class SqlReminderProposalRepository:
    def __init__(self, sessions: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] | None = None) -> None:
        self.sessions = sessions
        self._clock = clock or (lambda: datetime.now(UTC))

    def _now(self) -> datetime:
        value = self._clock()
        return value.astimezone(UTC) if value.tzinfo else value.replace(tzinfo=UTC)

    async def create(
        self,
        issuer: str,
        subject: str,
        *,
        content: str,
        due_at: datetime,
        run_id: UUID,
        conversation_id: UUID,
        tool_call_id: UUID,
        now: datetime | None = None,
        session: AsyncSession | None = None,
        transaction: object | None = None,
    ) -> ReminderProposal:
        if transaction is not None:
            if not isinstance(transaction, AsyncSession):
                raise TypeError("proposal transaction must be an async database session")
            session = transaction
        if not issuer or not subject:
            raise ValueError("authenticated owner is required")
        created = (now or self._now()).astimezone(UTC)
        normalized_content = normalize_content(content)
        normalized_due = ensure_due_at_future(due_at, now=created)
        item = ReminderProposal(
            uuid4(), issuer, subject, normalized_content, normalized_due, run_id,
            conversation_id, tool_call_id, created,
            proposal_expiry(created_at=created, due_at=normalized_due),
        )
        if session is not None:
            session.add(
                ReminderProposalRow(
                    id=item.id,
                    principal_issuer=issuer,
                    principal_subject=subject,
                    content=item.content,
                    due_at=item.due_at,
                    run_id=item.run_id,
                    conversation_id=item.conversation_id,
                    tool_call_id=item.tool_call_id,
                    created_at=item.created_at,
                    expires_at=item.expires_at,
                )
            )
            return item
        async with self.sessions() as owned_session, owned_session.begin():
            return await self.create(
                issuer, subject, content=content, due_at=due_at, run_id=run_id,
                conversation_id=conversation_id, tool_call_id=tool_call_id,
                now=created, session=owned_session,
            )

    async def get(self, issuer: str, subject: str, proposal_id: UUID) -> ReminderProposal:
        async with self.sessions() as session:
            row = (
                await session.execute(
                    select(ReminderProposalRow).where(
                        ReminderProposalRow.id == proposal_id,
                        ReminderProposalRow.principal_issuer == issuer,
                        ReminderProposalRow.principal_subject == subject,
                    )
                )
            ).scalar_one_or_none()
            if row is None:
                raise ReminderProposalNotFound("reminder proposal not found")
            return _proposal(row)


__all__ = [
    "MemoryReminderProposalRepository",
    "PROPOSAL_EXPIRY",
    "ReminderProposal",
    "ReminderProposalError",
    "ReminderProposalNotFound",
    "ReminderProposalRepository",
    "ReminderProposalRow",
    "SqlReminderProposalRepository",
    "proposal_expiry",
]
