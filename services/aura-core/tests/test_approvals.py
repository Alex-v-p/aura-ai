"""Behavioral coverage for the approval/proposal privacy boundary.

The approval domain intentionally carries only identifiers and lifecycle
metadata.  Reminder arguments remain in the automation-owned proposal store
and are returned only through the owner-authorized detail DTO.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator, Awaitable
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.domains.automation.reminders.proposals import (
    MemoryReminderProposalRepository,
    ReminderProposalNotFound,
    SqlReminderProposalRepository,
    proposal_expiry,
)
from aura_core.domains.automation.reminders.public import (
    ReminderCommandProposal,
    ReminderProposal,
)
from aura_core.domains.automation.reminders.repository import SqlReminderRepository
from aura_core.domains.governance.approvals.persistence import ApprovalRow
from aura_core.domains.governance.approvals.public import (
    Approval,
    ApprovalDetail,
    ApprovalExecutionStatus,
    ApprovalIdempotencyConflict,
    ApprovalNotFound,
    ApprovalReplayEvent,
    ApprovalStatus,
    ApprovalTransitionError,
    ApprovalVersionConflict,
    as_payload,
    decode_cursor,
    detail_payload,
    encode_cursor,
)
from aura_core.domains.governance.approvals.repository import (
    MemoryApprovalRepository,
    SqlApprovalRepository,
)
from aura_core.platform.telemetry import MetadataMetrics

ISSUER = "https://authentik.example"
OWNER = "owner"
OTHER_OWNER = "other-owner"
CREATED = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


@pytest.mark.asyncio
async def test_proposal_normalizes_private_arguments_and_pins_owner() -> None:
    repository = MemoryReminderProposalRepository(clock=lambda: CREATED)
    proposal = await repository.create(
        ISSUER,
        OWNER,
        content="  private reminder text  ",
        due_at=datetime(2026, 10, 11, 14, tzinfo=UTC),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )

    assert proposal.content == "private reminder text"
    assert proposal.due_at == datetime(2026, 10, 11, 14, tzinfo=UTC)
    assert proposal.created_at == CREATED
    assert proposal.expires_at == CREATED + timedelta(hours=1)

    with pytest.raises(ReminderProposalNotFound):
        await repository.get(ISSUER, OTHER_OWNER, proposal.id)


@pytest.mark.asyncio
async def test_proposal_expiry_is_capped_by_requested_due_instant() -> None:
    due = CREATED + timedelta(minutes=20)
    repository = MemoryReminderProposalRepository(clock=lambda: CREATED)
    proposal = await repository.create(
        ISSUER,
        OWNER,
        content="soon",
        due_at=due,
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    assert proposal.expires_at == due
    assert proposal_expiry(created_at=CREATED, due_at=CREATED + timedelta(hours=2)) == (
        CREATED + timedelta(hours=1)
    )


def _approval() -> Approval:
    now = CREATED
    return Approval(
        id=uuid4(),
        issuer=ISSUER,
        subject=OWNER,
        proposal_id=uuid4(),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
        tool_id="reminder.create",
        tool_version=1,
        status=ApprovalStatus.PENDING,
        execution_status=ApprovalExecutionStatus.NOT_STARTED,
        version=1,
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(hours=1),
    )


def test_approval_statuses_and_transport_payload_are_metadata_only() -> None:
    assert [item.value for item in ApprovalStatus] == [
        "pending",
        "approved",
        "rejected",
        "expired",
        "canceled",
    ]
    assert [item.value for item in ApprovalExecutionStatus] == [
        "not_started",
        "queued",
        "executing",
        "completed",
        "failed",
        "denied",
    ]

    approval = _approval()
    payload = as_payload(approval)
    assert payload["toolId"] == "reminder.create"
    assert not {"content", "dueAt", "arguments", "prompt", "response"} & set(payload)

    event = ApprovalReplayEvent(
        id=uuid4(),
        approval_id=approval.id,
        proposal_id=approval.proposal_id,
        run_id=approval.run_id,
        conversation_id=approval.conversation_id,
        tool_id=approval.tool_id,
        tool_version=approval.tool_version,
        status=ApprovalStatus.PENDING,
        execution_status=ApprovalExecutionStatus.NOT_STARTED,
        version=approval.version,
        occurred_at=CREATED,
    )
    event_payload = event.payload()
    assert event_payload["eventType"] == "approval.pending"
    assert not {"content", "dueAt", "arguments", "prompt", "response"} & set(event_payload)


def test_approval_detail_is_the_only_content_bearing_authorized_projection() -> None:
    approval = _approval()
    # A small immutable proposal-shaped object avoids reaching into any ORM
    # mapping and keeps this test at the public application boundary.
    from aura_core.domains.automation.reminders.proposals import ReminderProposal

    proposal = ReminderProposal(
        id=approval.proposal_id,
        issuer=ISSUER,
        subject=OWNER,
        content="private approval content",
        due_at=CREATED + timedelta(hours=2),
        run_id=approval.run_id,
        conversation_id=approval.conversation_id,
        tool_call_id=approval.tool_call_id,
        created_at=CREATED,
        expires_at=approval.expires_at,
    )
    detail = detail_payload(ApprovalDetail(approval=approval, proposal=proposal))
    assert detail["proposal"] == {
        "id": str(proposal.id),
        "content": proposal.content,
        "dueAt": proposal.due_at.isoformat(),
    }
    assert "private approval content" not in repr(as_payload(approval))


def test_typed_command_detail_is_bounded_and_excludes_private_arguments() -> None:
    approval = _approval()
    proposal = ReminderCommandProposal(
        id=approval.proposal_id,
        issuer=ISSUER,
        subject=OWNER,
        operation="series.create",
        tool_id="reminder.series.create",
        tool_version=1,
        target_id=None,
        expected_version=None,
        normalized_arguments={
            "content": "private recurring reminder",
            "recurrence": {"frequency": "daily", "interval": 1},
            "timezone": "Europe/Brussels",
            "target": "private target query",
            "dueAt": "2026-10-11T14:00:00+02:00",
        },
        timezone="Europe/Brussels",
        run_id=approval.run_id,
        conversation_id=approval.conversation_id,
        tool_call_id=approval.tool_call_id,
        created_at=CREATED,
        expires_at=approval.expires_at,
        target_type="series",
    )

    detail = detail_payload(ApprovalDetail(approval=approval, proposal=proposal))
    command = cast(dict[str, object], detail["proposal"])
    assert command == {
        "id": str(proposal.id),
        "operation": "series.create",
        "summary": "Create recurring reminder",
        "targetType": "series",
        "targetId": None,
        "expectedVersion": None,
    }
    assert len(cast(str, command["summary"])) <= 120
    assert not {
        "content",
        "recurrence",
        "timezone",
        "target",
        "dueAt",
        "normalizedArguments",
        "arguments",
    } & set(command)


@pytest.mark.parametrize(
    "tool_id",
    [
        "reminder.create",
        "reminder.series.create",
        "reminder.reschedule",
        "reminder.series.update",
        "reminder.series.pause",
        "reminder.series.resume",
        "reminder.cancel",
    ],
)
def test_approval_replay_event_accepts_additive_reminder_command_ids_without_schema_change(
    tool_id: str,
) -> None:
    approval = replace(_approval(), tool_id=tool_id)
    event = ApprovalReplayEvent(
        id=uuid4(),
        approval_id=approval.id,
        proposal_id=approval.proposal_id,
        run_id=approval.run_id,
        conversation_id=approval.conversation_id,
        tool_id=tool_id,
        tool_version=1,
        status=ApprovalStatus.PENDING,
        execution_status=ApprovalExecutionStatus.NOT_STARTED,
        version=approval.version,
        occurred_at=CREATED,
    )
    payload = event.payload()
    assert payload["schemaVersion"] == 1
    assert payload["toolId"] == tool_id
    assert not {
        "content",
        "recurrence",
        "timezone",
        "dueAt",
        "arguments",
        "query",
        "result",
    } & set(payload)


def test_approval_cursor_round_trip_is_owner_bound() -> None:
    event_id = uuid4()
    cursor = encode_cursor(ISSUER, OWNER, CREATED, event_id)
    stamp, decoded = decode_cursor(cursor, ISSUER, OWNER)
    assert (stamp, decoded) == (CREATED, event_id)
    with pytest.raises(ValueError):
        decode_cursor(cursor, ISSUER, OTHER_OWNER)
    filtered_cursor = encode_cursor(ISSUER, OWNER, CREATED, event_id, ApprovalStatus.REJECTED)
    with pytest.raises(ValueError):
        decode_cursor(filtered_cursor, ISSUER, OWNER, ApprovalStatus.PENDING)


@pytest.mark.asyncio
async def test_memory_approval_requires_explicit_decision_and_replays_idempotently() -> None:
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    detail = await repository.create_pending(
        ISSUER,
        OWNER,
        content="  create the private reminder  ",
        due_at=CREATED + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    assert detail.approval.status is ApprovalStatus.PENDING
    assert detail.approval.execution_status is ApprovalExecutionStatus.NOT_STARTED
    assert isinstance(detail.proposal, ReminderProposal)
    assert detail.proposal.content == "create the private reminder"
    with pytest.raises(ApprovalNotFound):
        await repository.get(ISSUER, OTHER_OWNER, detail.approval.id)

    approved = await repository.decide(
        ISSUER,
        OWNER,
        detail.approval.id,
        decision=ApprovalStatus.APPROVED,
        expected_version=1,
        idempotency_key="approve-once",
    )
    replay = await repository.decide(
        ISSUER,
        OWNER,
        detail.approval.id,
        decision=ApprovalStatus.APPROVED,
        expected_version=1,
        idempotency_key="approve-once",
    )
    assert approved == replay
    assert approved.status is ApprovalStatus.APPROVED
    assert approved.execution_status is ApprovalExecutionStatus.QUEUED


def _decision_measurements(metrics: MetadataMetrics) -> tuple[Any, ...]:
    return tuple(
        measurement
        for measurement in metrics.snapshot()
        if measurement.metric in {"approval_decision_outcome", "approval_decision_duration_ms"}
        or (
            measurement.kind == "span"
            and dict(measurement.trace_attributes).get("operation") == "approval.decide"
        )
    )


@pytest.mark.asyncio
async def test_memory_every_decision_attempt_emits_one_bounded_telemetry_record() -> None:
    metrics = MetadataMetrics()
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED),
        clock=lambda: CREATED,
        metrics=metrics,
    )
    detail = await repository.create_pending(
        ISSUER,
        OWNER,
        content="telemetry",
        due_at=CREATED + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )

    await repository.approve(
        ISSUER,
        OWNER,
        detail.approval.id,
        expected_version=1,
        idempotency_key="telemetry-success",
    )
    # The exact same command is an idempotent replay, not a second transition.
    await repository.approve(
        ISSUER,
        OWNER,
        detail.approval.id,
        expected_version=1,
        idempotency_key="telemetry-success",
    )
    with pytest.raises(ApprovalIdempotencyConflict):
        await repository.reject(
            ISSUER,
            OWNER,
            detail.approval.id,
            expected_version=1,
            idempotency_key="telemetry-success",
        )
    with pytest.raises(ApprovalVersionConflict):
        await repository.reject(
            ISSUER,
            OWNER,
            detail.approval.id,
            expected_version=1,
            idempotency_key="telemetry-version-conflict",
        )

    measurements = _decision_measurements(metrics)
    decision_outcomes = [
        measurement
        for measurement in measurements
        if measurement.kind == "metric" and measurement.metric == "approval_decision_outcome"
    ]
    decision_durations = [
        measurement
        for measurement in measurements
        if measurement.kind == "metric" and measurement.metric == "approval_decision_duration_ms"
    ]
    decision_spans = [
        measurement
        for measurement in measurements
        if measurement.kind == "span"
        and dict(measurement.trace_attributes).get("operation") == "approval.decide"
    ]
    assert [dict(item.dimensions)["outcome"] for item in decision_outcomes] == [
        "approved",
        "duplicate",
        "conflict",
        "conflict",
    ]
    assert len(decision_durations) == len(decision_spans) == len(decision_outcomes) == 4
    assert metrics.stats().rejected == 0


class _WaitingRunPort:
    async def cancel_waiting_run(
        self, run_id: object, approval_id: object, *, transaction: object
    ) -> object:
        return {"run_id": run_id, "approval_id": approval_id}


@pytest.mark.asyncio
async def test_memory_waiting_cancel_and_persistence_failure_emit_one_decision_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = MetadataMetrics()
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED),
        clock=lambda: CREATED,
        metrics=metrics,
        execution_port=_WaitingRunPort(),
    )
    detail = await repository.create_pending(
        ISSUER,
        OWNER,
        content="cancel telemetry",
        due_at=CREATED + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    await repository.cancel_waiting_run(
        ISSUER,
        OWNER,
        detail.approval.run_id,
        detail.approval.id,
        expected_version=1,
        idempotency_key="waiting-cancel",
    )

    failing = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED),
        clock=lambda: CREATED,
        metrics=metrics,
    )
    failed_detail = await failing.create_pending(
        ISSUER,
        OWNER,
        content="persistence failure",
        due_at=CREATED + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )

    def fail_append(*args: object, **kwargs: object) -> None:
        raise RuntimeError("persistence failure")

    monkeypatch.setattr(failing, "_append_event", fail_append)
    with pytest.raises(RuntimeError, match="persistence failure"):
        await failing.approve(
            ISSUER,
            OWNER,
            failed_detail.approval.id,
            expected_version=1,
            idempotency_key="persistence-failure",
        )

    outcomes = [
        measurement
        for measurement in metrics.snapshot()
        if measurement.kind == "metric" and measurement.metric == "approval_decision_outcome"
    ]
    spans = [
        measurement
        for measurement in metrics.snapshot()
        if measurement.kind == "span"
        and dict(measurement.trace_attributes).get("operation") == "approval.decide"
    ]
    durations = [
        measurement
        for measurement in metrics.snapshot()
        if measurement.kind == "metric"
        and measurement.metric == "approval_decision_duration_ms"
    ]
    assert [dict(item.dimensions)["outcome"] for item in outcomes] == ["canceled", "error"]
    assert len(spans) == len(durations) == len(outcomes) == 2
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_memory_approval_expiry_is_due_time_capped_and_rejects_late_decision() -> None:
    now = CREATED
    clock_now = [now]
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: clock_now[0]),
        clock=lambda: clock_now[0],
    )
    detail = await repository.create_pending(
        ISSUER,
        OWNER,
        content="soon",
        due_at=now + timedelta(minutes=10),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    assert detail.approval.expires_at == now + timedelta(minutes=10)
    clock_now[0] = now + timedelta(minutes=10)
    with pytest.raises((ApprovalVersionConflict, ApprovalTransitionError)):
        await repository.decide(
            ISSUER,
            OWNER,
            detail.approval.id,
            decision=ApprovalStatus.APPROVED,
            expected_version=1,
            idempotency_key="late-approve",
        )
    expired = await repository.get(ISSUER, OWNER, detail.approval.id)
    assert expired.approval.status is ApprovalStatus.EXPIRED
    assert expired.approval.execution_status is ApprovalExecutionStatus.DENIED


@pytest.mark.asyncio
async def test_memory_approval_decision_race_has_one_winner_and_one_version() -> None:
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    detail = await repository.create_pending(
        ISSUER,
        OWNER,
        content="race",
        due_at=CREATED + timedelta(hours=1),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )

    async def decide(decision: ApprovalStatus) -> object:
        try:
            return await repository.decide(
                ISSUER,
                OWNER,
                detail.approval.id,
                decision=decision,
                expected_version=1,
                idempotency_key=f"race-{decision.value}",
            )
        except (ApprovalVersionConflict, ApprovalTransitionError) as exc:
            return exc

    results = await asyncio.gather(
        decide(ApprovalStatus.APPROVED), decide(ApprovalStatus.REJECTED)
    )
    winners = [item for item in results if isinstance(item, Approval)]
    assert len(winners) == 1
    final = await repository.get(ISSUER, OWNER, detail.approval.id)
    assert final.approval.version == 2
    assert final.approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}


@pytest.mark.asyncio
async def test_memory_approval_replay_is_owner_scoped_and_cursor_resumable() -> None:
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    first = await repository.create_pending(
        ISSUER,
        OWNER,
        content="first",
        due_at=CREATED + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    second = await repository.create_pending(
        ISSUER,
        OWNER,
        content="second",
        due_at=CREATED + timedelta(hours=3),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    events = await repository.replay(ISSUER, OWNER)
    assert len(events) == 2
    assert await repository.cursor_is_authorized(ISSUER, OWNER, events[0].id)
    assert not await repository.cursor_is_authorized(ISSUER, OTHER_OWNER, events[0].id)
    assert await repository.replay(ISSUER, OWNER, after=events[0].id) == (events[1],)
    assert first.approval.id != second.approval.id


@pytest.mark.asyncio
async def test_memory_approval_list_status_filter_binds_cursor() -> None:
    repository = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    pending = await repository.create_pending(
        ISSUER,
        OWNER,
        content="pending",
        due_at=CREATED + timedelta(hours=2),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    rejected = await repository.create_pending(
        ISSUER,
        OWNER,
        content="rejected",
        due_at=CREATED + timedelta(hours=3),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
    )
    await repository.reject(
        ISSUER,
        OWNER,
        rejected.approval.id,
        expected_version=1,
        idempotency_key="reject-filter",
    )
    page = await repository.list(ISSUER, OWNER, status=ApprovalStatus.REJECTED, limit=1)
    assert [item.id for item in page.items] == [rejected.approval.id]
    assert pending.approval.id not in {item.id for item in page.items}
    assert page.next_cursor is None

    unfiltered = await repository.list(ISSUER, OWNER, limit=10)
    assert len(unfiltered.items) == 2
    assert unfiltered.next_cursor is None


@pytest.mark.asyncio
async def test_memory_approval_terminal_outcomes_are_owner_scoped_and_replayed() -> None:
    async def pending(repository: MemoryApprovalRepository, suffix: str):
        return await repository.create_pending(
            ISSUER,
            OWNER,
            content=f"{suffix} private proposal",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )

    rejected_repo = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    rejected = await pending(rejected_repo, "rejected")
    rejected_result = await rejected_repo.reject(
        ISSUER,
        OWNER,
        rejected.approval.id,
        expected_version=1,
        idempotency_key="terminal-reject",
    )
    assert rejected_result.status is ApprovalStatus.REJECTED
    assert rejected_result.execution_status is ApprovalExecutionStatus.DENIED

    canceled_repo = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    canceled = await pending(canceled_repo, "canceled")
    canceled_result = await canceled_repo.cancel(
        ISSUER,
        OWNER,
        canceled.approval.id,
        expected_version=1,
        idempotency_key="terminal-cancel",
    )
    assert canceled_result.status is ApprovalStatus.CANCELED
    assert canceled_result.execution_status is ApprovalExecutionStatus.DENIED

    expiry_clock = [CREATED]
    expired_repo = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: expiry_clock[0]),
        clock=lambda: expiry_clock[0],
    )
    expired = await pending(expired_repo, "expired")
    expiry_clock[0] = expired.approval.expires_at
    expired_result = (await expired_repo.expire_due(now=expiry_clock[0]))[0]
    assert expired_result.status is ApprovalStatus.EXPIRED
    assert expired_result.execution_status is ApprovalExecutionStatus.DENIED

    approved_repo = MemoryApprovalRepository(
        MemoryReminderProposalRepository(clock=lambda: CREATED), clock=lambda: CREATED
    )
    approved = await pending(approved_repo, "approved")
    approved_result = await approved_repo.approve(
        ISSUER,
        OWNER,
        approved.approval.id,
        expected_version=1,
        idempotency_key="terminal-approve",
    )
    assert approved_result.status is ApprovalStatus.APPROVED
    assert approved_result.execution_status is ApprovalExecutionStatus.QUEUED

    for repository, expected in (
        (rejected_repo, ApprovalStatus.REJECTED),
        (canceled_repo, ApprovalStatus.CANCELED),
        (expired_repo, ApprovalStatus.EXPIRED),
        (approved_repo, ApprovalStatus.APPROVED),
    ):
        events = await repository.replay(ISSUER, OWNER)
        assert events[-1].status is expected
        assert await repository.cursor_is_authorized(ISSUER, OWNER, events[-1].id)
        assert not await repository.cursor_is_authorized(ISSUER, OTHER_OWNER, events[-1].id)


@pytest.mark.asyncio
async def test_sql_expiry_failure_records_one_post_rollback_error_span(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Result:
        def scalars(self) -> Result:
            return self

        def all(self) -> list[ApprovalRow]:
            return [row]

    class Session:
        def __init__(self) -> None:
            self.added: list[object] = []

        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        def begin(self) -> Session:
            return self

        async def execute(self, _statement: object) -> Result:
            return Result()

        def add(self, value: object) -> None:
            self.added.append(value)

    class Sessions:
        def __init__(self) -> None:
            self.session = Session()

        def __call__(self) -> Session:
            return self.session

    row = ApprovalRow(
        id=uuid4(),
        principal_issuer=ISSUER,
        principal_subject=OWNER,
        proposal_id=uuid4(),
        run_id=uuid4(),
        conversation_id=uuid4(),
        tool_call_id=uuid4(),
        tool_id="reminder.create",
        tool_version=1,
        status=ApprovalStatus.PENDING.value,
        execution_status=ApprovalExecutionStatus.NOT_STARTED.value,
        version=1,
        created_at=CREATED,
        updated_at=CREATED,
        expires_at=CREATED,
    )
    metrics = MetadataMetrics()
    sessions = Sessions()
    repository = SqlApprovalRepository(
        cast(Any, sessions),
        proposal_repository=cast(Any, None),
        clock=lambda: CREATED,
        metrics=metrics,
    )

    async def fail_lock(*_: object) -> None:
        raise RuntimeError("owner lock failure")

    monkeypatch.setattr(repository, "_lock_owner_stream", fail_lock)
    with pytest.raises(RuntimeError, match="owner lock failure"):
        await repository.expire_due(now=CREATED)

    spans = [
        measurement
        for measurement in metrics.snapshot()
        if measurement.kind == "span"
        and dict(measurement.trace_attributes).get("operation") == "approval.expire"
    ]
    assert len(spans) == 1
    assert dict(spans[0].dimensions) == {
        "dependency": "postgresql",
        "error_class": "database",
        "outcome": "error",
    }
    assert row.status == ApprovalStatus.PENDING.value
    assert row.version == 1
    assert sessions.session.added == []
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_approval_housekeeping_emits_bounded_expiry_failure_metric() -> None:
    from aura_core.entrypoints.worker.app import run_approval_housekeeping

    class FailingService:
        attempts = 0

        async def expire_due(self, *, limit: int) -> tuple[Approval, ...]:
            del limit
            if self.attempts == 0:
                self.attempts += 1
                raise RuntimeError("expiry persistence failure")
            raise asyncio.CancelledError

    metrics = MetadataMetrics()
    with pytest.raises(asyncio.CancelledError):
        await run_approval_housekeeping(
            cast(Any, FailingService()),
            cast(Any, None),
            cast(Any, None),
            cast(Any, None),
            metrics,
            interval_seconds=0.1,
        )

    outcomes = [
        measurement
        for measurement in metrics.snapshot()
        if measurement.kind == "metric" and measurement.metric == "approval_expiry_outcome"
    ]
    assert len(outcomes) == 1
    assert dict(outcomes[0].dimensions) == {"outcome": "failed"}
    assert metrics.stats().rejected == 0


@asynccontextmanager
async def sql_approval_repository() -> AsyncGenerator[
    tuple[Any, Any, Any, list[datetime], str]
]:
    """Yield real approval/restart boundaries against an explicit disposable DB."""

    url = os.environ.get("AURA_0056_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0056_DATABASE_URL is required for PostgreSQL approval coverage")
    try:
        from alembic import command
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message
            for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise
    engine = create_async_engine(url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = [CREATED]
    repository = SqlApprovalRepository(
        sessions,
        proposal_repository=SqlReminderProposalRepository(sessions, clock=lambda: clock[0]),
        clock=lambda: clock[0],
    )
    prefix = f"aura0056-approval-{uuid4()}-"
    try:
        yield engine, sessions, repository, clock, prefix
    finally:
        from sqlalchemy import text

        async with sessions() as session, session.begin():
            await session.execute(
                text(
                    "DELETE FROM outbox WHERE aggregate_type = 'governance.approval' "
                    "AND aggregate_id IN (SELECT id FROM approvals "
                    "WHERE principal_subject LIKE :prefix)"
                ),
                {"prefix": f"{prefix}%"},
            )
            await session.execute(
                text(
                    "DELETE FROM outbox WHERE aggregate_type = 'automation.reminder' "
                    "AND aggregate_id IN (SELECT id FROM reminders "
                    "WHERE principal_subject LIKE :prefix)"
                ),
                {"prefix": f"{prefix}%"},
            )
            for table in (
                "approval_command_idempotency",
                "approval_events",
                "approvals",
                "reminder_proposals",
                "reminder_command_idempotency",
                "reminders",
            ):
                await session.execute(
                    text(f"DELETE FROM {table} WHERE principal_subject LIKE :prefix"),
                    {"prefix": f"{prefix}%"},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_approval_create_is_atomic_and_metadata_only() -> None:
    from sqlalchemy import text

    async with sql_approval_repository() as (_, sessions, repository, _, prefix):
        issuer = ISSUER
        subject = f"{prefix}owner"
        detail = await repository.create_pending(
            issuer,
            subject,
            content="private SQL proposal",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        async with sessions() as session:
            proposal_count = await session.execute(
                text("SELECT COUNT(*) FROM reminder_proposals WHERE id = :id"),
                {"id": detail.proposal.id},
            )
            approval_count = await session.execute(
                text("SELECT COUNT(*) FROM approvals WHERE id = :id"),
                {"id": detail.approval.id},
            )
            outbox = await session.execute(
                text(
                    "SELECT aggregate_type, aggregate_id, payload FROM outbox "
                    "WHERE aggregate_id = :id"
                ),
                {"id": detail.approval.id},
            )
        assert proposal_count.scalar_one() == 1
        assert approval_count.scalar_one() == 1
        row = outbox.mappings().one()
        assert row["aggregate_type"] == "governance.approval"
        assert row["aggregate_id"] == detail.approval.id
        assert not {"content", "dueAt", "arguments", "prompt", "response"} & set(row["payload"])
        assert (await repository.get(issuer, subject, detail.approval.id)).proposal.content == (
            "private SQL proposal"
        )


@pytest.mark.asyncio
async def test_postgresql_every_decision_attempt_emits_one_bounded_telemetry_record() -> None:
    async with sql_approval_repository() as (_, _, repository, _, prefix):
        metrics = MetadataMetrics()
        repository.metrics = metrics
        issuer = ISSUER
        subject = f"{prefix}telemetry-owner"
        detail = await repository.create_pending(
            issuer,
            subject,
            content="SQL telemetry",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        approved = await repository.approve(
            issuer,
            subject,
            detail.approval.id,
            expected_version=1,
            idempotency_key="sql-telemetry-success",
        )
        assert approved.status is ApprovalStatus.APPROVED
        replay = await repository.approve(
            issuer,
            subject,
            detail.approval.id,
            expected_version=1,
            idempotency_key="sql-telemetry-success",
        )
        assert replay == approved
        with pytest.raises(ApprovalIdempotencyConflict):
            await repository.reject(
                issuer,
                subject,
                detail.approval.id,
                expected_version=1,
                idempotency_key="sql-telemetry-success",
            )
        with pytest.raises(ApprovalVersionConflict):
            await repository.reject(
                issuer,
                subject,
                detail.approval.id,
                expected_version=1,
                idempotency_key="sql-telemetry-version-conflict",
            )

        outcomes = [
            measurement
            for measurement in metrics.snapshot()
            if measurement.kind == "metric" and measurement.metric == "approval_decision_outcome"
        ]
        durations = [
            measurement
            for measurement in metrics.snapshot()
            if measurement.kind == "metric"
            and measurement.metric == "approval_decision_duration_ms"
        ]
        spans = [
            measurement
            for measurement in metrics.snapshot()
            if measurement.kind == "span"
            and dict(measurement.trace_attributes).get("operation") == "approval.decide"
        ]
        assert [dict(item.dimensions)["outcome"] for item in outcomes] == [
            "approved",
            "duplicate",
            "conflict",
            "conflict",
        ]
        assert len(durations) == len(spans) == len(outcomes) == 4
        assert metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_postgresql_approval_race_and_duplicate_resume_are_exactly_once() -> None:
    async with sql_approval_repository() as (_, sessions, repository, _, prefix):
        issuer = ISSUER
        subject = f"{prefix}owner"
        detail = await repository.create_pending(
            issuer,
            subject,
            content="race SQL proposal",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        results = cast(tuple[Any, ...], await asyncio.gather(
            repository.approve(
                issuer,
                subject,
                detail.approval.id,
                expected_version=1,
                idempotency_key="race-approve",
            ),
            repository.reject(
                issuer,
                subject,
                detail.approval.id,
                expected_version=1,
                idempotency_key="race-reject",
            ),
            return_exceptions=True,
        ))
        winners = [item for item in results if isinstance(item, Approval)]
        assert len(winners) == 1
        final = await repository.get(issuer, subject, detail.approval.id)
        assert final.approval.version == 2
        assert final.approval.status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}
        events = await repository.replay(issuer, subject)
        assert [event.status for event in events] == [ApprovalStatus.PENDING, final.approval.status]
        assert [event.version for event in events] == [1, 2]

        if final.approval.status is ApprovalStatus.APPROVED:
            restarted = SqlApprovalRepository(
                sessions,
                proposal_repository=SqlReminderProposalRepository(
                    sessions, clock=lambda: CREATED
                ),
                clock=lambda: CREATED,
            )
            first = await restarted.approve(
                issuer,
                subject,
                detail.approval.id,
                expected_version=1,
                idempotency_key="race-approve",
            )
            assert first == winners[0]
            reminder_repository = SqlReminderRepository(sessions, clock=lambda: CREATED)
            proposal = final.proposal
            created = await reminder_repository.create(
                issuer,
                subject,
                content=proposal.content,
                due_at=proposal.due_at,
                idempotency_key=f"approval:{detail.approval.id}",
            )
            replay = await reminder_repository.create(
                issuer,
                subject,
                content=proposal.content,
                due_at=proposal.due_at,
                idempotency_key=f"approval:{detail.approval.id}",
            )
            assert replay == created
            assert replay.issuer == issuer
            assert replay.subject == subject


@pytest.mark.asyncio
async def test_postgresql_replay_cursor_is_append_stable_for_equal_timestamps() -> None:
    async with sql_approval_repository() as (_, _, repository, _, prefix):
        issuer = ISSUER
        subject = f"{prefix}replay-owner"
        first = await repository.create_pending(
            issuer,
            subject,
            content="first equal-time approval",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        second = await repository.create_pending(
            issuer,
            subject,
            content="second equal-time approval",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        await repository.approve(
            issuer,
            subject,
            first.approval.id,
            expected_version=1,
            idempotency_key="equal-time-approve",
        )

        events = await repository.replay(issuer, subject)
        assert [event.approval_id for event in events] == [
            first.approval.id,
            second.approval.id,
            first.approval.id,
        ]
        resumed = await repository.replay(issuer, subject, after=events[1].id)
        assert [event.approval_id for event in resumed] == [first.approval.id]
        assert resumed[0].status is ApprovalStatus.APPROVED


@pytest.mark.asyncio
async def test_postgresql_same_owner_event_position_is_commit_stable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async with sql_approval_repository() as (_, _, repository, _, prefix):
        issuer = ISSUER
        subject = f"{prefix}commit-owner"
        first_staged = asyncio.Event()
        release_first = asyncio.Event()
        original_stage = repository._stage
        first_call = True

        async def hold_first_stage(*args: Any, **kwargs: Any) -> None:
            nonlocal first_call
            await original_stage(*args, **kwargs)
            if first_call:
                first_call = False
                first_staged.set()
                await release_first.wait()

        monkeypatch.setattr(repository, "_stage", hold_first_stage)
        first_task = asyncio.create_task(
            repository.create_pending(
                issuer,
                subject,
                content="first transaction",
                due_at=CREATED + timedelta(hours=2),
                run_id=uuid4(),
                conversation_id=uuid4(),
                tool_call_id=uuid4(),
            )
        )
        await first_staged.wait()
        second_task = asyncio.create_task(
            repository.create_pending(
                issuer,
                subject,
                content="second transaction",
                due_at=CREATED + timedelta(hours=2),
                run_id=uuid4(),
                conversation_id=uuid4(),
                tool_call_id=uuid4(),
            )
        )
        await asyncio.sleep(0.05)
        assert not second_task.done()
        assert await repository.replay(issuer, subject) == ()

        release_first.set()
        first, second = await asyncio.gather(first_task, second_task)
        events = await repository.replay(issuer, subject)
        assert [event.approval_id for event in events] == [
            first.approval.id,
            second.approval.id,
        ]
        resumed = await repository.replay(issuer, subject, after=events[0].id)
        assert [event.approval_id for event in resumed] == [second.approval.id]


@pytest.mark.asyncio
async def test_postgresql_mixed_owner_expiry_and_decisions_do_not_deadlock() -> None:
    async with sql_approval_repository() as (_, _, repository, clock, prefix):
        issuer = ISSUER
        first_subject = f"{prefix}expiry-a"
        second_subject = f"{prefix}expiry-b"
        first = await repository.create_pending(
            issuer,
            first_subject,
            content="first expiry race",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        second = await repository.create_pending(
            issuer,
            second_subject,
            content="second expiry race",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        clock[0] = first.approval.expires_at

        results: tuple[object, object, object] = await asyncio.wait_for(
            cast(
                Awaitable[tuple[object, object, object]],
                asyncio.gather(
                    repository.expire_due(now=clock[0]),
                    repository.approve(
                        issuer,
                        first_subject,
                        first.approval.id,
                        expected_version=1,
                        idempotency_key="expiry-decision-a",
                    ),
                    repository.approve(
                        issuer,
                        second_subject,
                        second.approval.id,
                        expected_version=1,
                        idempotency_key="expiry-decision-b",
                    ),
                    return_exceptions=True,
                ),
            ),
            timeout=5,
        )
        for result in results:
            if isinstance(result, BaseException):
                assert isinstance(result, (ApprovalVersionConflict, ApprovalTransitionError))

        first_events = await repository.replay(issuer, first_subject)
        second_events = await repository.replay(issuer, second_subject)
        assert [event.version for event in first_events] == [1, 2]
        assert [event.version for event in second_events] == [1, 2]


@pytest.mark.asyncio
async def test_postgresql_cancel_and_expiry_races_have_one_terminal_winner() -> None:
    async with sql_approval_repository() as (_, _, repository, clock, prefix):
        issuer = ISSUER
        subject = f"{prefix}owner"
        cancel_detail = await repository.create_pending(
            issuer,
            subject,
            content="cancel race",
            due_at=CREATED + timedelta(hours=2),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        cancel_results = cast(tuple[Any, ...], await asyncio.gather(
            repository.cancel(
                issuer,
                subject,
                cancel_detail.approval.id,
                expected_version=1,
                idempotency_key="cancel-race",
            ),
            repository.reject(
                issuer,
                subject,
                cancel_detail.approval.id,
                expected_version=1,
                idempotency_key="reject-race",
            ),
            return_exceptions=True,
        ))
        cancel_winners = [item for item in cancel_results if isinstance(item, Approval)]
        assert len(cancel_winners) == 1
        cancel_final = await repository.get(issuer, subject, cancel_detail.approval.id)
        assert cancel_final.approval.status in {ApprovalStatus.CANCELED, ApprovalStatus.REJECTED}
        assert cancel_final.approval.version == 2

        expiry_detail = await repository.create_pending(
            issuer,
            subject,
            content="expiry race",
            due_at=CREATED + timedelta(minutes=10),
            run_id=uuid4(),
            conversation_id=uuid4(),
            tool_call_id=uuid4(),
        )
        clock[0] = expiry_detail.approval.expires_at
        expiry_results = cast(tuple[Any, ...], await asyncio.gather(
            repository.expire_due(now=clock[0]),
            repository.approve(
                issuer,
                subject,
                expiry_detail.approval.id,
                expected_version=1,
                idempotency_key="expiry-approve-race",
            ),
            return_exceptions=True,
        ))
        expiry_final = await repository.get(issuer, subject, expiry_detail.approval.id)
        assert expiry_final.approval.status in {ApprovalStatus.EXPIRED, ApprovalStatus.APPROVED}
        assert expiry_final.approval.version == 2
        assert any(not isinstance(result, BaseException) for result in expiry_results)
