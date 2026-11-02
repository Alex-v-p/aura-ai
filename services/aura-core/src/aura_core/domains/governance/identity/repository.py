"""Session-scoped PostgreSQL identity repository."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.domains.governance.identity.persistence import PrincipalRow


@dataclass(frozen=True, slots=True)
class PrincipalRecord:
    id: UUID
    issuer: str
    subject: str


class SqlIdentityRepository:
    async def resolve(self, session: AsyncSession, issuer: str, subject: str) -> PrincipalRecord:
        row = (
            await session.execute(
                select(PrincipalRow).where(
                    PrincipalRow.issuer == issuer, PrincipalRow.subject == subject
                )
            )
        ).scalar_one_or_none()
        if row is None:
            row = PrincipalRow(issuer=issuer, subject=subject)
            session.add(row)
            await session.flush()
        return PrincipalRecord(row.id, row.issuer, row.subject)

    async def get(self, session: AsyncSession, principal_id: UUID) -> PrincipalRecord | None:
        row = await session.get(PrincipalRow, principal_id)
        return PrincipalRecord(row.id, row.issuer, row.subject) if row else None

    async def find(
        self, session: AsyncSession, issuer: str, subject: str
    ) -> PrincipalRecord | None:
        row = (
            await session.execute(
                select(PrincipalRow).where(
                    PrincipalRow.issuer == issuer, PrincipalRow.subject == subject
                )
            )
        ).scalar_one_or_none()
        return PrincipalRecord(row.id, row.issuer, row.subject) if row else None
