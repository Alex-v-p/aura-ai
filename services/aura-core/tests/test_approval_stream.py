"""Replay semantics shared by the approval SSE transport."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from aura_core.domains.automation.reminders.proposals import MemoryReminderProposalRepository
from aura_core.domains.governance.approvals.public import (
    ApprovalExecutionStatus,
    ApprovalReplayEvent,
    ApprovalStatus,
)
from aura_core.domains.governance.approvals.repository import MemoryApprovalRepository


@pytest.mark.asyncio
async def test_approval_wakeup_methods_are_explicit_and_release_replay_waiters() -> None:
    repository = MemoryApprovalRepository(MemoryReminderProposalRepository())
    waiting = asyncio.create_task(repository.wait_for_wakeup(timeout=1.0))
    await asyncio.sleep(0)
    event_id = uuid4()
    await repository.notify_wakeup(event_id)
    assert await waiting

    waiting = asyncio.create_task(repository.wait_for_wakeup(timeout=1.0))
    await asyncio.sleep(0)
    await repository.notify_resume_wakeup(event_id)
    assert await waiting


@pytest.mark.asyncio
async def test_approval_replay_after_last_event_id_is_lossless_and_content_free() -> None:
    now = datetime(2026, 10, 10, 12, tzinfo=UTC)
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: now), clock=lambda: now
    )
    first = await repository.create_pending(
        "https://authentik.example",
        "owner",
        content="private one",
        due_at=now + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    await repository.decide(
        "https://authentik.example",
        "owner",
        first.approval.id,
        decision=ApprovalStatus.REJECTED,
        expected_version=1,
        idempotency_key="reject",
    )
    events = await repository.replay("https://authentik.example", "owner")
    assert len(events) == 2
    resumed = await repository.replay(
        "https://authentik.example", "owner", after=events[0].id
    )
    assert resumed == (events[1],)
    payload = events[1].payload()
    assert payload["status"] == "rejected"
    assert not {"content", "dueAt", "arguments", "prompt", "response"} & set(payload)

    # A cursor from another owner is not a valid replay position.
    assert not await repository.cursor_is_authorized(
        "https://authentik.example", "other", events[0].id
    )


@pytest.mark.parametrize(
    "execution_status",
    (
        ApprovalExecutionStatus.EXECUTING,
        ApprovalExecutionStatus.COMPLETED,
        ApprovalExecutionStatus.FAILED,
    ),
)
def test_approval_replay_preserves_execution_lifecycle_without_private_payload(
    execution_status: ApprovalExecutionStatus,
) -> None:
    approval_id = uuid4()
    event = ApprovalReplayEvent(
        id=uuid4(),
        approval_id=approval_id,
        proposal_id=uuid4(),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_id="reminder.create",
        tool_version=1,
        status=ApprovalStatus.APPROVED,
        execution_status=execution_status,
        version=3,
        occurred_at=datetime(2026, 10, 10, 12, tzinfo=UTC),
    )
    payload = event.payload()
    assert payload["executionStatus"] == execution_status.value
    assert payload["version"] == 3
    assert not {"content", "dueAt", "arguments", "prompt", "response"} & set(payload)
