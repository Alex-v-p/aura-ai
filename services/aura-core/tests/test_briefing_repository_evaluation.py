"""State-machine and owner-isolation evaluations for the briefing repository."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from uuid import uuid4

import pytest
from aura_core.domains.automation.briefings.public import (
    BriefingDailyCadence,
    BriefingExecutionPolicyReferences,
    BriefingIdempotencyConflict,
    BriefingNotFound,
    BriefingOccurrenceStatus,
    BriefingResult,
    BriefingResultStatus,
    BriefingSourceCompleteness,
    BriefingSourceGrants,
    BriefingVersionConflict,
)
from aura_core.domains.automation.briefings.repository import MemoryBriefingRepository

NOW = datetime(2026, 1, 2, 12, tzinfo=UTC)
OWNER = ("https://authentik.example", "briefing-owner")
OTHER = ("https://authentik.example", "other-owner")


def _policies() -> BriefingExecutionPolicyReferences:
    return BriefingExecutionPolicyReferences(uuid4(), uuid4(), uuid4(), uuid4())


async def _schedule(repository: MemoryBriefingRepository, *, owner: tuple[str, str] = OWNER):
    return await repository.create_schedule(
        *owner,
        display_name="Daily briefing",
        cadence=BriefingDailyCadence(time(9), "UTC"),
        sources=BriefingSourceGrants(True, (uuid4(),), (uuid4(),)),
        agent_revision_id=uuid4(),
        execution_policies=_policies(),
        idempotency_key="create-schedule",
        now=NOW,
    )


@pytest.mark.asyncio
async def test_duplicate_create_and_due_claim_are_idempotent() -> None:
    repository = MemoryBriefingRepository()
    first = await _schedule(repository)
    replay = await repository.create_schedule(
        *OWNER,
        display_name="Daily briefing",
        cadence=first.cadence,
        sources=first.sources,
        agent_revision_id=first.agent_revision_id,
        execution_policies=first.execution_policies,
        idempotency_key="create-schedule",
        now=NOW,
    )
    assert replay == first

    claimed = await repository.claim_due_occurrences(NOW + timedelta(days=1))
    duplicate_scan = await repository.claim_due_occurrences(NOW + timedelta(days=1))
    assert len(claimed) == 1
    assert duplicate_scan == ()

    with pytest.raises(BriefingIdempotencyConflict):
        await repository.create_schedule(
            *OWNER,
            display_name="Different payload",
            cadence=first.cadence,
            sources=first.sources,
            agent_revision_id=first.agent_revision_id,
            execution_policies=first.execution_policies,
            idempotency_key="create-schedule",
            now=NOW,
        )


@pytest.mark.asyncio
async def test_pause_resume_preserves_owner_scope_and_stale_versions_fail() -> None:
    repository = MemoryBriefingRepository()
    schedule = await _schedule(repository)
    paused = await repository.pause_schedule(
        *OWNER, schedule.id, expected_version=schedule.version, idempotency_key="pause"
    )
    assert (
        await repository.pause_schedule(
            *OWNER, schedule.id, expected_version=schedule.version, idempotency_key="pause"
        )
        == paused
    )
    assert paused.status.value == "paused"
    with pytest.raises(BriefingVersionConflict):
        await repository.resume_schedule(
            *OWNER, schedule.id, expected_version=schedule.version, idempotency_key="stale"
        )
    resumed = await repository.resume_schedule(
        *OWNER, schedule.id, expected_version=paused.version, idempotency_key="resume"
    )
    assert (
        await repository.resume_schedule(
            *OWNER, schedule.id, expected_version=paused.version, idempotency_key="resume"
        )
        == resumed
    )
    assert resumed.status.value == "active"
    with pytest.raises(BriefingNotFound):
        await repository.get_schedule(*OTHER, schedule.id)


@pytest.mark.asyncio
async def test_schedule_delete_is_tombstone_and_explicit_history_deletion_is_separate() -> None:
    repository = MemoryBriefingRepository()
    schedule = await _schedule(repository)
    receipt = await repository.delete_schedule(
        *OWNER,
        schedule.id,
        expected_version=schedule.version,
        idempotency_key="delete",
    )
    assert receipt.scope.value == "schedule"
    deleted = await repository.get_schedule(*OWNER, schedule.id)
    assert deleted.status.value == "deleted"
    occurrences = await repository.list_occurrences(*OWNER, schedule.id)
    assert len(occurrences.items) == 1

    history = await repository.delete_history(
        *OWNER,
        schedule.id,
        expected_schedule_version=deleted.version,
        idempotency_key="history-delete",
    )
    assert history.scope.value == "occurrence_history"
    # History deletion is scoped to terminal occurrences; the scheduled row
    # remains visible until it reaches a terminal state.
    assert len((await repository.list_occurrences(*OWNER, schedule.id)).items) == 1


@pytest.mark.asyncio
async def test_schedule_delete_durably_settles_running_occurrence_cancellation() -> None:
    repository = MemoryBriefingRepository()
    schedule = await _schedule(repository)
    occurrence = (await repository.list_occurrences(*OWNER, schedule.id)).items[0]
    run_id = uuid4()
    await repository.admit_occurrence(
        *OWNER,
        schedule.id,
        occurrence.id,
        run_id=run_id,
        expected_version=occurrence.version,
    )
    canceled: list[object] = []

    class Cancellation:
        async def cancel_briefing(self, lineage: object) -> object:
            canceled.append(lineage)
            return type("Settled", (), {"status": "canceled"})()

    await repository.delete_schedule(
        *OWNER,
        schedule.id,
        expected_version=schedule.version,
        idempotency_key="delete-running",
        cancel_run=Cancellation(),
    )
    assert len(canceled) == 1
    settled = (await repository.list_occurrences(*OWNER, schedule.id)).items[0]
    assert settled.status is BriefingOccurrenceStatus.CANCELED
    assert settled.result_id is not None


@pytest.mark.asyncio
async def test_history_deletion_cannot_remove_a_nonterminal_occurrence() -> None:
    repository = MemoryBriefingRepository()
    schedule = await _schedule(repository)
    occurrence = (await repository.list_occurrences(*OWNER, schedule.id)).items[0]
    await repository.admit_occurrence(
        *OWNER,
        schedule.id,
        occurrence.id,
        run_id=uuid4(),
        expected_version=occurrence.version,
    )
    await repository.delete_history(
        *OWNER,
        schedule.id,
        expected_schedule_version=schedule.version,
        idempotency_key="history-delete-running",
    )
    remaining = (await repository.list_occurrences(*OWNER, schedule.id)).items
    assert len(remaining) == 1
    assert remaining[0].status is BriefingOccurrenceStatus.ADMITTED


@pytest.mark.asyncio
async def test_result_settlement_is_one_per_occurrence_and_content_deletion_is_manual() -> None:
    repository = MemoryBriefingRepository()
    schedule = await _schedule(repository)
    occurrence = (await repository.list_occurrences(*OWNER, schedule.id)).items[0]
    result_id = uuid4()
    result = BriefingResult(
        result_id,
        schedule.id,
        occurrence.id,
        OWNER[0],
        OWNER[1],
        uuid4(),
        BriefingResultStatus.PARTIAL,
        1,
        BriefingSourceCompleteness.PARTIAL,
        "owner-private result",
        NOW,
        NOW,
    )
    assert await repository.create_result(result) == result
    assert await repository.create_result(result) == result
    with pytest.raises(BriefingNotFound):
        await repository.get_result(*OTHER, result_id)

    receipt = await repository.delete_result_content(
        *OWNER, result_id, expected_version=1, idempotency_key="result-delete"
    )
    assert receipt.scope.value == "result_content"
    deleted = await repository.get_result(*OWNER, result_id)
    assert deleted.content is None
    assert deleted.status is BriefingResultStatus.DELETED
