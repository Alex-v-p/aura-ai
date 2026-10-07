"""Session-scoped PostgreSQL audit repository."""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.domains.governance.audit.persistence import AuditRow


class SqlAuditRepository:
    def stage(
        self,
        session: AsyncSession,
        principal_id: UUID | None,
        action: str,
        resource: str | None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        session.add(
            AuditRow(
                principal_id=principal_id,
                action=action,
                resource=resource,
                metadata_=metadata or {},
            )
        )
