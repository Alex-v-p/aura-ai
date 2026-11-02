"""Composition helpers for the governance approval boundary."""

# ruff: noqa: E501

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.automation.reminders.proposals import (
    MemoryReminderProposalRepository,
    ReminderProposalRepository,
    SqlReminderProposalRepository,
)
from aura_core.domains.governance.approvals.public import (
    ApprovalApplicationService,
    ApprovalExecutionPort,
    ApprovalRepository,
)
from aura_core.domains.governance.approvals.repository import (
    MemoryApprovalRepository,
    SqlApprovalRepository,
)
from aura_core.platform.telemetry import MetadataMetrics


def approval_repository(
    sessions: async_sessionmaker[AsyncSession] | None,
    *,
    testing: bool = False,
    proposal_repository: ReminderProposalRepository | None = None,
    metrics: MetadataMetrics | None = None,
    execution_port: ApprovalExecutionPort | None = None,
) -> ApprovalRepository:
    if testing:
        return MemoryApprovalRepository(
            proposal_repository or MemoryReminderProposalRepository(),
            metrics=metrics,
            execution_port=execution_port,
        )
    if sessions is None:
        raise ValueError("database sessions are required for approval persistence")
    proposal_adapter = proposal_repository or SqlReminderProposalRepository(sessions)
    return SqlApprovalRepository(sessions, proposal_repository=proposal_adapter, execution_port=execution_port, metrics=metrics)


def approval_service(repository: ApprovalRepository, *, execution_port: ApprovalExecutionPort | None = None) -> ApprovalApplicationService:
    return ApprovalApplicationService(repository, execution_port=execution_port)


__all__ = ["approval_repository", "approval_service"]
