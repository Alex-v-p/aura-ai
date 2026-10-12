"""Acceptance and replay coverage for the AURA-0064 reminder-series domain.

These tests deliberately use the public reminder port and the deterministic
in-memory composition.  They do not inspect SQLAlchemy mappings or require a
running database; PostgreSQL locking and migration behavior is covered by the
dedicated migration/system tests.
"""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest
from aura_core.domains.automation.notifications.repository import (
    MemoryNotificationRepository,
)
from aura_core.domains.automation.reminders.public import (
    ReminderOccurrenceStatus as OccurrenceStatus,
)
from aura_core.domains.automation.reminders.public import (
    ReminderSeriesIdempotencyConflict,
    ReminderSeriesNotFound,
    ReminderSeriesStatus,
    ReminderSeriesTransitionError,
    ReminderSeriesVersionConflict,
    as_payload,
)
from aura_core.domains.automation.reminders.recurrence import (
    RecurrenceFrequency,
    RecurrenceRule,
    RecurrenceValidationError,
    next_occurrence,
    validate_timezone,
)
from aura_core.domains.automation.reminders.repository import (
    MemoryReminderRepository,
)

OWNER_ISSUER = "https://authentik.example"
OWNER = "series-owner"
OTHER_OWNER = "other-owner"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
SCHEMA_PATH = (
    Path(__file__).parents[3]
    / "contracts/events/automation/v1/reminder-series-events.schema.json"
)


def _assert_reminder_series_event_schema(payload: dict[str, object]) -> None:
    """Validate the closed, metadata-only producer envelope without a plugin."""

    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    properties = cast(dict[str, object], schema["$defs"]["EventProperties"]["properties"])
    required = set(cast(list[str], schema["$defs"]["EventProperties"]["required"]))
    assert isinstance(properties, dict)
    assert required <= payload.keys()
    assert set(payload) <= set(properties)
    assert payload["schemaVersion"] == 1
    assert payload["aggregateType"] == "automation.reminder-series"
    for field in ("aggregateId", "commandId", "correlationId", "causationId"):
        UUID(cast(str, payload[field]))
    datetime.fromisoformat(cast(str, payload["createdAt"]))
    assert cast(str, payload["eventType"]) in {
        "reminder.series.created",
        "reminder.series.updated",
        "reminder.series.paused",
        "reminder.series.resumed",
        "reminder.series.skipped",
        "reminder.series.advanced",
        "reminder.series.completed",
        "reminder.series.canceled",
    }
    assert cast(str, payload["status"]) in {"active", "paused", "completed", "canceled"}
    version = payload["version"]
    assert type(version) is int and 1 <= version <= 2_147_483_647
    for field in ("occurrenceCount", "skippedCount", "missedCount"):
        value = payload[field]
        assert type(value) is int and 0 <= value <= 2_147_483_647


def _assert_rejects_private_event_field(payload: dict[str, object]) -> None:
    private = dict(payload)
    private["privateContent"] = "do not ship"
    with pytest.raises(AssertionError):
        _assert_reminder_series_event_schema(private)


def _rule(
    frequency: RecurrenceFrequency,
    *,
    interval: int = 1,
    weekdays: tuple[int, ...] = (),
    month_day: int | None = None,
    count: int | None = None,
    end_date: date | None = None,
) -> RecurrenceRule:
    return RecurrenceRule(
        frequency=frequency,
        interval=interval,
        weekdays=weekdays,
        month_day=month_day,
        count=count,
        end_date=end_date,
    )


def _repo(clock: list[datetime] | None = None) -> MemoryReminderRepository:
    current = clock or [NOW]
    return MemoryReminderRepository(clock=lambda: current[0])


def _series_key(label: str) -> str:
    """Map readable fixture labels to stable UUID idempotency keys."""

    return str(uuid5(NAMESPACE_URL, f"aura0064:{label}"))


@pytest.mark.asyncio
async def test_one_shot_reminder_projection_remains_byte_compatible_with_series_support() -> None:
    repository = _repo()
    reminder = await repository.create(
        OWNER_ISSUER,
        OWNER,
        content="one-shot compatibility",
        due_at=datetime(2026, 2, 1, 9, tzinfo=UTC),
        idempotency_key="one-shot-compatible",
    )
    payload = as_payload(reminder)
    assert set(payload) == {
        "id",
        "content",
        "dueAt",
        "status",
        "version",
        "createdAt",
        "updatedAt",
        "firedAt",
        "completedAt",
        "canceledAt",
    }
    assert "seriesId" not in payload
    assert "occurrenceSequence" not in payload


async def _create_series(
    repository: MemoryReminderRepository,
    *,
    owner: str = OWNER,
    rule: RecurrenceRule | None = None,
    local_start: datetime = datetime(2026, 1, 5, 9, 30),
    timezone: str = "Europe/Brussels",
    key: str | None = None,
) -> Any:
    return await repository.create_series(
        OWNER_ISSUER,
        owner,
        content="take medication",
        rule=rule or _rule(RecurrenceFrequency.DAILY),
        local_start=local_start,
        timezone=timezone,
        idempotency_key=_series_key(key or str(uuid4())),
    )


def test_recurrence_patterns_preserve_wall_clock_and_month_end_overflow() -> None:
    daily = _rule(RecurrenceFrequency.DAILY, interval=2)
    weekdays = _rule(RecurrenceFrequency.SELECTED_WEEKDAY, weekdays=(1, 3, 5))
    weekly = _rule(RecurrenceFrequency.WEEKLY, interval=2, weekdays=(1,))
    monthly = _rule(RecurrenceFrequency.MONTHLY, month_day=31)

    assert next_occurrence(daily, datetime(2026, 1, 5, 9, 30), "Europe/Brussels")[0] == datetime(
        2026, 1, 7, 9, 30
    )
    assert (
        next_occurrence(weekdays, datetime(2026, 1, 5, 9, 30), "Europe/Brussels")[0].date()
        == date(2026, 1, 7)
    )
    assert (
        next_occurrence(weekly, datetime(2026, 1, 5, 9, 30), "Europe/Brussels")[0].date()
        == date(2026, 1, 19)
    )
    assert (
        next_occurrence(monthly, datetime(2026, 1, 31, 9, 30), "Europe/Brussels")[0].date()
        == date(2026, 2, 28)
    )
    assert (
        next_occurrence(monthly, datetime(2026, 2, 28, 9, 30), "Europe/Brussels")[0].date()
        == date(2026, 3, 31)
    )


def test_dst_gap_moves_to_first_valid_instant_and_overlap_fires_once_early() -> None:
    gap = next_occurrence(
        _rule(RecurrenceFrequency.DAILY), datetime(2026, 3, 28, 2, 30), "Europe/Brussels"
    )[1]
    overlap = next_occurrence(
        _rule(RecurrenceFrequency.DAILY), datetime(2026, 10, 24, 2, 30), "Europe/Brussels"
    )[1]
    assert gap.astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    assert overlap.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    assert next_occurrence(
        _rule(RecurrenceFrequency.DAILY), datetime(2026, 10, 24, 2, 30), "Europe/Brussels"
    )[1] == overlap


def test_dst_gap_transition_advances_from_the_first_valid_boundary() -> None:
    rule = _rule(RecurrenceFrequency.DAILY)
    local, due = next_occurrence(rule, datetime(2026, 3, 28, 2, 30), "Europe/Brussels")
    assert local == datetime(2026, 3, 29, 2, 30)
    assert due.astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
    following_local, following_due = next_occurrence(rule, local, "Europe/Brussels")
    assert following_local == datetime(2026, 3, 30, 2, 30)
    assert following_due.astimezone(UTC) == datetime(2026, 3, 30, 0, 30, tzinfo=UTC)


def test_selected_weekday_interval_and_weekly_multi_day_stay_in_eligible_week() -> None:
    every_other_week = _rule(
        RecurrenceFrequency.SELECTED_WEEKDAY,
        interval=2,
        weekdays=(1, 3),
    )
    local, _ = next_occurrence(
        every_other_week,
        datetime(2026, 1, 5, 9, 30),
        "Europe/Brussels",
        anchor_local=datetime(2026, 1, 5, 9, 30),
    )
    assert local == datetime(2026, 1, 7, 9, 30)
    later_local, _ = next_occurrence(
        every_other_week,
        local,
        "Europe/Brussels",
        anchor_local=datetime(2026, 1, 5, 9, 30),
    )
    assert later_local == datetime(2026, 1, 19, 9, 30)

    multi_day_week = _rule(RecurrenceFrequency.WEEKLY, weekdays=(1, 3))
    next_local, _ = next_occurrence(
        multi_day_week,
        datetime(2026, 1, 5, 9, 30),
        "Europe/Brussels",
        anchor_local=datetime(2026, 1, 5, 9, 30),
    )
    assert next_local == datetime(2026, 1, 7, 9, 30)


@pytest.mark.asyncio
async def test_create_materializes_exactly_one_future_occurrence_and_is_replayable() -> None:
    repository = _repo()
    series = await _create_series(repository, key="series-create")
    replay = await _create_series(repository, key="series-create")
    assert replay == series
    assert series.status is ReminderSeriesStatus.ACTIVE
    assert series.next_reminder_id is not None
    occurrences = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert len([item for item in occurrences if item.status is OccurrenceStatus.SCHEDULED]) == 1
    assert occurrences[0].sequence == 1
    assert occurrences[0].reminder_id == series.next_reminder_id

    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await _create_series(
            repository,
            key="series-create",
            rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(1,)),
        )


@pytest.mark.asyncio
async def test_every_series_command_has_exact_replay_and_conflicting_key_semantics() -> None:
    # Update includes its normalized arguments in the idempotency fingerprint;
    # a key reused with a different rule or content must never become a second
    # mutation.  The lifecycle commands are checked independently so each
    # command's first committed version is unambiguous.
    repository = _repo()
    series = await _create_series(repository, key="idempotency-update-create")
    updated = await repository.update_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=series.version,
        idempotency_key=_series_key("idempotency-update"),
        content="updated content",
        local_start=datetime(2026, 1, 6, 10, 0),
        timezone="Europe/Brussels",
        rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
    )
    assert (
        await repository.update_series(
            OWNER_ISSUER,
            OWNER,
            series.id,
            expected_version=series.version,
            idempotency_key=_series_key("idempotency-update"),
            content="updated content",
            local_start=datetime(2026, 1, 6, 10, 0),
            timezone="Europe/Brussels",
            rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
        )
        == updated
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await repository.update_series(
            OWNER_ISSUER,
            OWNER,
            series.id,
            expected_version=series.version,
            idempotency_key=_series_key("idempotency-update"),
            content="different content",
            local_start=datetime(2026, 1, 6, 10, 0),
            timezone="Europe/Brussels",
            rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
        )

    pause_repo = _repo()
    pause_series = await _create_series(pause_repo, key="idempotency-pause-create")
    paused = await pause_repo.pause_series(
        OWNER_ISSUER,
        OWNER,
        pause_series.id,
        expected_version=pause_series.version,
        idempotency_key=_series_key("idempotency-pause"),
    )
    assert (
        await pause_repo.pause_series(
            OWNER_ISSUER,
            OWNER,
            pause_series.id,
            expected_version=pause_series.version,
            idempotency_key=_series_key("idempotency-pause"),
        )
        == paused
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await pause_repo.pause_series(
            OWNER_ISSUER,
            OWNER,
            pause_series.id,
            expected_version=999,
            idempotency_key=_series_key("idempotency-pause"),
        )

    resumed = await pause_repo.resume_series(
        OWNER_ISSUER,
        OWNER,
        pause_series.id,
        expected_version=paused.version,
        idempotency_key=_series_key("idempotency-resume"),
    )
    assert (
        await pause_repo.resume_series(
            OWNER_ISSUER,
            OWNER,
            pause_series.id,
            expected_version=paused.version,
            idempotency_key=_series_key("idempotency-resume"),
        )
        == resumed
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await pause_repo.resume_series(
            OWNER_ISSUER,
            OWNER,
            pause_series.id,
            expected_version=999,
            idempotency_key=_series_key("idempotency-resume"),
        )

    skip_repo = _repo()
    skip_series = await _create_series(skip_repo, key="idempotency-skip-create")
    skipped = await skip_repo.skip_next(
        OWNER_ISSUER,
        OWNER,
        skip_series.id,
        expected_version=skip_series.version,
        idempotency_key=_series_key("idempotency-skip"),
    )
    assert (
        await skip_repo.skip_next(
            OWNER_ISSUER,
            OWNER,
            skip_series.id,
            expected_version=skip_series.version,
            idempotency_key=_series_key("idempotency-skip"),
        )
        == skipped
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await skip_repo.skip_next(
            OWNER_ISSUER,
            OWNER,
            skip_series.id,
            expected_version=999,
            idempotency_key=_series_key("idempotency-skip"),
        )

    cancel_repo = _repo()
    cancel_series = await _create_series(cancel_repo, key="idempotency-cancel-create")
    canceled = await cancel_repo.cancel_series(
        OWNER_ISSUER,
        OWNER,
        cancel_series.id,
        expected_version=cancel_series.version,
        idempotency_key=_series_key("idempotency-cancel"),
    )
    assert (
        await cancel_repo.cancel_series(
            OWNER_ISSUER,
            OWNER,
            cancel_series.id,
            expected_version=cancel_series.version,
            idempotency_key=_series_key("idempotency-cancel"),
        )
        == canceled
    )
    with pytest.raises(ReminderSeriesIdempotencyConflict):
        await cancel_repo.cancel_series(
            OWNER_ISSUER,
            OWNER,
            cancel_series.id,
            expected_version=999,
            idempotency_key=_series_key("idempotency-cancel"),
        )


@pytest.mark.asyncio
async def test_owner_isolation_and_validation_are_enforced_before_persistence() -> None:
    repository = _repo()
    series = await _create_series(repository)
    with pytest.raises(ReminderSeriesNotFound):
        await repository.get_series(OWNER_ISSUER, OTHER_OWNER, series.id)
    assert (await repository.list_series(OWNER_ISSUER, OTHER_OWNER)).items == ()
    with pytest.raises(ReminderSeriesVersionConflict):
        await repository.skip_next(
            OWNER_ISSUER,
            OWNER,
            series.id,
            expected_version=99,
            idempotency_key=_series_key("wrong-version"),
        )

    with pytest.raises(RecurrenceValidationError):
        await _create_series(repository, rule=_rule(RecurrenceFrequency.DAILY, interval=0))
    with pytest.raises(RecurrenceValidationError):
        validate_timezone("Not/A-Timezone")
    with pytest.raises(RecurrenceValidationError):
        await _create_series(
            repository,
            rule=_rule(RecurrenceFrequency.SELECTED_WEEKDAY, weekdays=(0,)),
        )


@pytest.mark.asyncio
async def test_skip_pause_resume_edit_and_cancel_only_affect_future_occurrence() -> None:
    repository = _repo()
    series = await _create_series(
        repository,
        rule=_rule(RecurrenceFrequency.DAILY, count=4),
        key="lifecycle-create",
    )
    first = await repository.skip_next(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=series.version,
        idempotency_key=_series_key("skip"),
    )
    history = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert any(item.sequence == 1 and item.status is OccurrenceStatus.SKIPPED for item in history)
    assert first.status is ReminderSeriesStatus.ACTIVE
    assert first.version == series.version + 1
    assert first.next_reminder_id != series.next_reminder_id

    paused = await repository.pause_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=first.version,
        idempotency_key=_series_key("pause"),
    )
    assert paused.status is ReminderSeriesStatus.PAUSED
    resumed = await repository.resume_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=paused.version,
        idempotency_key=_series_key("resume"),
    )
    assert resumed.status is ReminderSeriesStatus.ACTIVE
    assert first.next_due_at is not None
    assert resumed.next_due_at is not None
    assert resumed.next_due_at > first.next_due_at

    edited = await repository.update_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=resumed.version,
        rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
        local_start=datetime(2026, 1, 6, 10, 0),
        timezone="Europe/Brussels",
        idempotency_key=_series_key("edit"),
    )
    assert edited.version == resumed.version + 1
    history = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert any(item.status is OccurrenceStatus.REPLACED for item in history)
    assert sum(item.status is OccurrenceStatus.SCHEDULED for item in history) == 1

    canceled = await repository.cancel_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=edited.version,
        idempotency_key=_series_key("cancel"),
    )
    assert canceled.status is ReminderSeriesStatus.CANCELED
    assert canceled.next_reminder_id is None


@pytest.mark.asyncio
async def test_occurrence_completion_does_not_complete_series_but_count_exhaustion_does() -> None:
    repository = _repo()
    series = await _create_series(
        repository,
        rule=_rule(RecurrenceFrequency.DAILY, count=2),
        key="count-create",
    )
    now = datetime(2026, 1, 5, 9, 31, tzinfo=UTC)
    fired = (await repository.fire_due(now=now, limit=100))[0]
    assert fired.status.value == "fired"
    first_reminder_id = series.next_reminder_id
    assert first_reminder_id is not None
    advanced = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    assert advanced.next_reminder_id is not None
    assert advanced.next_reminder_id != first_reminder_id
    history = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert any(item.sequence == 1 and item.status is OccurrenceStatus.FIRED for item in history)
    assert any(
        item.sequence == 2
        and item.status is OccurrenceStatus.SCHEDULED
        and item.reminder_id == advanced.next_reminder_id
        for item in history
    )
    completed_reminder = await repository.complete(
        OWNER_ISSUER,
        OWNER,
        first_reminder_id,
        expected_version=2,
        idempotency_key=_series_key("complete-1"),
    )
    completed = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    assert completed_reminder.status.value == "completed"
    assert completed.status is ReminderSeriesStatus.ACTIVE
    assert completed.next_reminder_id is not None
    await repository.fire_due(now=now + timedelta(days=1), limit=100)
    terminal = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    assert terminal.status is ReminderSeriesStatus.COMPLETED
    assert terminal.next_reminder_id is None


@pytest.mark.asyncio
async def test_mature_series_edit_replaces_only_future_occurrence_without_new_start() -> None:
    clock = [NOW]
    repository = _repo(clock)
    series = await _create_series(
        repository,
        rule=_rule(RecurrenceFrequency.DAILY),
        key="mature-edit-create",
    )
    clock[0] = datetime(2026, 1, 5, 9, 31, tzinfo=UTC)
    assert (await repository.fire_due(now=clock[0], limit=100))[0].status.value == "fired"
    mature = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    history_before = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert any(
        item.sequence == 1 and item.status is OccurrenceStatus.FIRED
        for item in history_before
    )
    pending_before = next(
        item for item in history_before if item.status is OccurrenceStatus.SCHEDULED
    )

    edited = await repository.update_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=mature.version,
        content="updated after first firing",
        rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
        timezone="America/Los_Angeles",
        idempotency_key=_series_key("mature-edit"),
    )
    assert edited.content == "updated after first firing"
    assert edited.rule.frequency is RecurrenceFrequency.WEEKLY
    assert edited.timezone == "America/Los_Angeles"
    assert edited.local_start == mature.local_start
    history_after = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert any(
        item.sequence == 1 and item.status is OccurrenceStatus.FIRED
        for item in history_after
    )
    replaced = next(
        item for item in history_after if item.sequence == pending_before.sequence
    )
    replacement = next(
        item for item in history_after if item.status is OccurrenceStatus.SCHEDULED
    )
    assert replaced.status is OccurrenceStatus.REPLACED
    assert replacement.local_due_at == pending_before.local_due_at
    assert sum(item.status is OccurrenceStatus.SCHEDULED for item in history_after) == 1

    clock[0] = replacement.due_at + timedelta(minutes=1)
    fired = await repository.fire_due(now=clock[0], limit=100)
    assert len(fired) == 1
    advanced = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    history_final = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert advanced.missed_count == edited.missed_count
    assert not any(item.status is OccurrenceStatus.SKIPPED for item in history_final)
    next_scheduled = next(
        item for item in history_final if item.status is OccurrenceStatus.SCHEDULED
    )
    assert next_scheduled.local_due_at == pending_before.local_due_at + timedelta(days=7)


@pytest.mark.asyncio
async def test_series_edit_with_explicit_local_start_reanchors_memory_cadence() -> None:
    clock = [NOW]
    repository = _repo(clock)
    series = await _create_series(
        repository,
        rule=_rule(RecurrenceFrequency.DAILY),
        key="explicit-local-start-create",
    )
    requested_local_start = datetime(2026, 1, 7, 11, 15)
    edited = await repository.update_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=series.version,
        local_start=requested_local_start,
        timezone="Europe/Brussels",
        rule=_rule(RecurrenceFrequency.DAILY),
        idempotency_key=_series_key("explicit-local-start-edit"),
    )
    assert edited.local_start == requested_local_start
    history_after_edit = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    replaced = next(item for item in history_after_edit if item.sequence == 1)
    replacement = next(
        item for item in history_after_edit if item.status is OccurrenceStatus.SCHEDULED
    )
    assert replaced.status is OccurrenceStatus.REPLACED
    assert replacement.local_due_at == requested_local_start
    assert sum(item.status is OccurrenceStatus.SCHEDULED for item in history_after_edit) == 1

    fired = await repository.fire_due(
        now=replacement.due_at + timedelta(minutes=1),
        limit=100,
    )
    assert len(fired) == 1
    history_after_fire = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    next_scheduled = next(
        item for item in history_after_fire if item.status is OccurrenceStatus.SCHEDULED
    )
    assert next_scheduled.local_due_at == requested_local_start + timedelta(days=1)


@pytest.mark.asyncio
async def test_end_date_termination_completes_series_without_materializing_after_limit() -> None:
    repository = _repo()
    series = await _create_series(
        repository,
        rule=_rule(RecurrenceFrequency.DAILY, end_date=date(2026, 1, 6)),
        key="end-date-create",
    )
    await repository.fire_due(now=datetime(2026, 1, 5, 10, tzinfo=UTC), limit=100)
    await repository.fire_due(now=datetime(2026, 1, 6, 10, tzinfo=UTC), limit=100)
    terminal = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    assert terminal.status is ReminderSeriesStatus.COMPLETED
    assert terminal.next_reminder_id is None
    occurrences = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert not any(item.status is OccurrenceStatus.SCHEDULED for item in occurrences)


@pytest.mark.asyncio
async def test_bounded_downtime_recovery_delivers_one_and_counts_missed_occurrences() -> None:
    repository = _repo()
    series = await _create_series(
        repository,
        rule=_rule(RecurrenceFrequency.DAILY),
        key="recovery-create",
    )
    # The first due occurrence is Jan 5; Jan 6-10 are five additional
    # overdue occurrences. Recovery delivers Jan 5 once and schedules Jan 11.
    recovery_now = NOW + timedelta(days=9)
    notification = MemoryNotificationRepository(repository, clock=lambda: recovery_now)
    result = await notification.deliver_due(limit=100)
    assert result.fired_count == 1
    assert result.duplicate_count == 0
    updated = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    assert updated.missed_count == 5
    assert updated.next_reminder_id is not None
    assert updated.next_due_at is not None
    assert updated.next_due_at > recovery_now
    history = await repository.list_occurrences(OWNER_ISSUER, OWNER, series.id)
    assert sum(item.status is OccurrenceStatus.SKIPPED for item in history) == 5
    assert len([item for item in history if item.status is OccurrenceStatus.SCHEDULED]) == 1
    replay = await notification.deliver_due(limit=100)
    assert replay.fired_count == 0
    assert len((await notification.list(OWNER_ISSUER, OWNER, limit=100)).items) == 1


@pytest.mark.asyncio
async def test_concurrent_fire_and_cancel_have_one_committed_outcome_and_no_duplicate_notification(
) -> None:
    repository = _repo()
    series = await _create_series(repository, key="race-create")
    notification = MemoryNotificationRepository(repository, clock=lambda: NOW + timedelta(days=9))
    results = await asyncio.gather(
        notification.deliver_due(limit=100),
        repository.cancel_series(
            OWNER_ISSUER,
            OWNER,
            series.id,
            expected_version=series.version,
            idempotency_key=_series_key("race-cancel"),
        ),
        return_exceptions=True,
    )
    stored = await repository.get_series(OWNER_ISSUER, OWNER, series.id)
    assert stored.status in {ReminderSeriesStatus.CANCELED, ReminderSeriesStatus.ACTIVE}
    notifications = await notification.list(OWNER_ISSUER, OWNER, limit=100)
    assert len(notifications.items) <= 1
    assert len({item.reminder_id for item in notifications.items}) <= 1
    # A fire that committed before cancellation remains historical; the
    # cancellation only governs future materialization.
    assert sum(getattr(item, "fired_count", 0) for item in results) <= 1


@pytest.mark.asyncio
async def test_series_events_and_metrics_are_metadata_only() -> None:
    secret = "private recurring reminder content"
    repository = _repo()
    series = await repository.create_series(
        OWNER_ISSUER,
        OWNER,
        content=secret,
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=_rule(RecurrenceFrequency.DAILY),
        idempotency_key=_series_key("metadata-only"),
    )
    assert series.content == secret
    events = cast(tuple[object, ...], tuple(getattr(repository, "events", ())))
    audit = cast(tuple[object, ...], tuple(getattr(repository, "audit", ())))
    assert events
    for record in (*events, *audit):
        assert secret not in repr(record)
        assert "timezone" not in repr(record).lower()
        assert "local_start" not in repr(record).lower()


@pytest.mark.asyncio
async def test_series_event_producers_match_closed_schema_and_normalize_lifecycle_names() -> None:
    repository = _repo()
    series = await _create_series(repository, key="schema-create")
    paused = await repository.pause_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=series.version,
        idempotency_key=_series_key("schema-pause"),
    )
    resumed = await repository.resume_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=paused.version,
        idempotency_key=_series_key("schema-resume"),
    )
    skipped = await repository.skip_next(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=resumed.version,
        idempotency_key=_series_key("schema-skip"),
    )
    await repository.cancel_series(
        OWNER_ISSUER,
        OWNER,
        series.id,
        expected_version=skipped.version,
        idempotency_key=_series_key("schema-cancel"),
    )
    events = cast(tuple[object, ...], tuple(getattr(repository, "events", ())))
    series_events = [cast(dict[str, object], record) for record in events]
    assert [record["eventType"] for record in series_events] == [
        "reminder.series.created",
        "reminder.series.paused",
        "reminder.series.resumed",
        "reminder.series.skipped",
        "reminder.series.canceled",
    ]
    for record in series_events:
        _assert_reminder_series_event_schema(record)
    _assert_rejects_private_event_field(series_events[0])


@pytest.mark.asyncio
async def test_postgresql_staged_series_payload_matches_event_schema_when_configured() -> None:
    database_url = os.environ.get("AURA_0064_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0064_DATABASE_URL is required for PostgreSQL producer coverage")
    try:
        from alembic import command
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from aura_core.platform.outbox.persistence import OutboxRow
        from sqlalchemy import select, text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    from aura_core.domains.automation.reminders.repository import SqlReminderRepository

    repository = SqlReminderRepository(sessions, clock=lambda: NOW)
    subject = f"schema-live-{uuid4().hex}"
    series = None
    series_id: UUID | None = None
    try:
        series = await repository.create_series(
            OWNER_ISSUER,
            subject,
            content="private live producer content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Europe/Brussels",
            rule=_rule(RecurrenceFrequency.DAILY),
            idempotency_key=str(uuid4()),
        )
        series_id = series.id
        async with sessions() as session:
            rows = (
                await session.execute(
                    select(OutboxRow).where(
                        OutboxRow.aggregate_id == series.id,
                        OutboxRow.topic == "aura.automation.reminder-series.v1",
                    )
                )
            ).scalars().all()
        assert rows
        for row in rows:
            _assert_reminder_series_event_schema(row.payload)
            _assert_rejects_private_event_field(row.payload)
    finally:
        async with sessions() as session, session.begin():
            for statement in (
                "DELETE FROM outbox WHERE aggregate_id = :series_id",
                "DELETE FROM reminder_occurrences WHERE series_id = :series_id",
                "DELETE FROM reminder_series_command_idempotency "
                "WHERE principal_subject = :subject",
                "DELETE FROM reminder_series WHERE principal_subject = :subject",
                "DELETE FROM reminders WHERE principal_subject = :subject",
            ):
                await session.execute(
                    text(statement),
                    {"subject": subject, "series_id": series_id},
                )
        await engine.dispose()


@pytest.mark.asyncio
async def test_postgresql_series_lifecycle_replay_catch_up_and_serialized_race() -> None:
    database_url = os.environ.get("AURA_0064_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0064_DATABASE_URL is required for PostgreSQL lifecycle coverage")
    try:
        from alembic import command
        from aura_core.domains.automation.notifications.repository import (
            SqlNotificationRepository,
        )
        from aura_core.domains.automation.reminders.repository import SqlReminderRepository
        from aura_core.entrypoints.cli import alembic_config
        from aura_core.platform.auth import Settings
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    except ImportError as exc:
        pytest.skip(f"PostgreSQL migration dependencies unavailable: {exc}")

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise

    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    clock = [NOW]
    reminders = SqlReminderRepository(sessions, clock=lambda: clock[0])
    notifications = SqlNotificationRepository(sessions, clock=lambda: clock[0])
    notifications.set_reminder_delivery(reminders)
    subject = f"series-live-{uuid4().hex}"
    create_key = str(uuid4())
    series = None
    series_id: UUID | None = None
    try:
        series = await reminders.create_series(
            OWNER_ISSUER,
            subject,
            content="private SQL lifecycle content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Europe/Brussels",
            rule=_rule(RecurrenceFrequency.DAILY),
            idempotency_key=create_key,
        )
        series_id = series.id
        assert await reminders.create_series(
            OWNER_ISSUER,
            subject,
            content="private SQL lifecycle content",
            local_start=datetime(2026, 1, 5, 9, 30),
            timezone="Europe/Brussels",
            rule=_rule(RecurrenceFrequency.DAILY),
            idempotency_key=create_key,
        ) == series
        with pytest.raises(ReminderSeriesIdempotencyConflict):
            await reminders.create_series(
                OWNER_ISSUER,
                subject,
                content="changed private SQL lifecycle content",
                local_start=datetime(2026, 1, 5, 9, 30),
                timezone="Europe/Brussels",
                rule=_rule(RecurrenceFrequency.DAILY),
                idempotency_key=create_key,
            )

        occurrences = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        assert sum(item.status is OccurrenceStatus.SCHEDULED for item in occurrences) == 1

        # Fire the first occurrence, then edit only future cadence fields while
        # retaining the original local_start.
        clock[0] = datetime(2026, 1, 5, 9, 31, tzinfo=UTC)
        first_delivery = await notifications.deliver_due(limit=10)
        assert first_delivery.fired_count == 1
        mature = await reminders.get_series(OWNER_ISSUER, subject, series_id)
        history_before_edit = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        pending_before_edit = next(
            item for item in history_before_edit if item.status is OccurrenceStatus.SCHEDULED
        )
        edit_key = str(uuid4())
        edited = await reminders.update_series(
            OWNER_ISSUER,
            subject,
            series_id,
            expected_version=mature.version,
            content="updated future SQL content",
            timezone="America/Los_Angeles",
            rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
            idempotency_key=edit_key,
        )
        assert edited.local_start == mature.local_start
        assert edited.timezone == "America/Los_Angeles"
        assert edited.rule.frequency is RecurrenceFrequency.WEEKLY
        history_after_edit = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        replaced = next(
            item
            for item in history_after_edit
            if item.sequence == pending_before_edit.sequence
        )
        replacement = next(
            item for item in history_after_edit if item.status is OccurrenceStatus.SCHEDULED
        )
        assert replaced.status is OccurrenceStatus.REPLACED
        assert replacement.local_due_at == pending_before_edit.local_due_at
        replay = await reminders.update_series(
            OWNER_ISSUER,
            subject,
            series_id,
            expected_version=mature.version,
            content="updated future SQL content",
            timezone="America/Los_Angeles",
            rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
            idempotency_key=edit_key,
        )
        assert replay == edited
        with pytest.raises(ReminderSeriesIdempotencyConflict):
            await reminders.update_series(
                OWNER_ISSUER,
                subject,
                series_id,
                expected_version=mature.version,
                content="conflicting future SQL content",
                timezone="America/Los_Angeles",
                rule=_rule(RecurrenceFrequency.WEEKLY, weekdays=(2,)),
                idempotency_key=edit_key,
            )

        # Complete the fired occurrence and verify that the active series still
        # has one pending occurrence; completion is not series completion.
        history = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        fired = next(item for item in history if item.status is OccurrenceStatus.FIRED)
        await reminders.complete(
            OWNER_ISSUER,
            subject,
            fired.reminder_id,
            expected_version=2,
            idempotency_key=str(uuid4()),
        )
        after_completion = await reminders.get_series(OWNER_ISSUER, subject, series_id)
        assert after_completion.status is ReminderSeriesStatus.ACTIVE
        history = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        assert sum(item.status is OccurrenceStatus.SCHEDULED for item in history) == 1

        assert edited.next_due_at is not None
        clock[0] = edited.next_due_at + timedelta(minutes=1)
        catch_up = await notifications.deliver_due(limit=10)
        assert catch_up.fired_count == 1
        caught_up = await reminders.get_series(OWNER_ISSUER, subject, series_id)
        assert caught_up.missed_count == edited.missed_count
        assert caught_up.next_due_at is not None and caught_up.next_due_at > clock[0]
        history_after_fire = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        assert sum(item.status is OccurrenceStatus.SCHEDULED for item in history_after_fire) == 1
        next_scheduled = next(
            item for item in history_after_fire if item.status is OccurrenceStatus.SCHEDULED
        )
        assert next_scheduled.local_due_at == pending_before_edit.local_due_at + timedelta(days=7)

        clock[0] = caught_up.next_due_at + timedelta(minutes=1)
        notifications_before_race = len(
            (await notifications.list(OWNER_ISSUER, subject, limit=100)).items
        )
        history_before_race = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        fired_before_race = sum(
            item.status is OccurrenceStatus.FIRED for item in history_before_race
        )
        race_results = await asyncio.gather(
            notifications.deliver_due(limit=10),
            reminders.cancel_series(
                OWNER_ISSUER,
                subject,
                series_id,
                expected_version=caught_up.version,
                idempotency_key=str(uuid4()),
            ),
            return_exceptions=True,
        )
        stored = await reminders.get_series(OWNER_ISSUER, subject, series_id)
        assert stored.status in {ReminderSeriesStatus.ACTIVE, ReminderSeriesStatus.CANCELED}
        race_delivery, race_command = race_results
        delivery_count = getattr(race_delivery, "fired_count", 0)
        notifications_after_race = len(
            (await notifications.list(OWNER_ISSUER, subject, limit=100)).items
        )
        history = await reminders.list_occurrences(OWNER_ISSUER, subject, series_id)
        scheduled_count = sum(item.status is OccurrenceStatus.SCHEDULED for item in history)
        fired_count = sum(item.status is OccurrenceStatus.FIRED for item in history)
        fired_delta = fired_count - fired_before_race
        if stored.status is ReminderSeriesStatus.ACTIVE:
            assert delivery_count == 1
            assert fired_delta == 1
            assert notifications_after_race == notifications_before_race + 1
            assert isinstance(
                race_command,
                (ReminderSeriesVersionConflict, ReminderSeriesTransitionError),
            )
            assert scheduled_count == 1
        else:
            assert delivery_count == 0
            assert fired_delta == 0
            assert notifications_after_race == notifications_before_race
            assert not isinstance(race_command, Exception)
            if isinstance(race_delivery, Exception):
                assert isinstance(
                    race_delivery,
                    (ReminderSeriesVersionConflict, ReminderSeriesTransitionError),
                )
            assert scheduled_count == 0
    finally:
        async with sessions() as session, session.begin():
            for statement in (
                "DELETE FROM notification_events WHERE principal_subject = :subject",
                "DELETE FROM notifications WHERE principal_subject = :subject",
                "DELETE FROM outbox WHERE aggregate_id = :series_id",
                "DELETE FROM reminder_occurrences WHERE series_id = :series_id",
                "DELETE FROM reminder_series_command_idempotency "
                "WHERE principal_subject = :subject",
                "DELETE FROM reminder_series WHERE principal_subject = :subject",
                "DELETE FROM reminders WHERE principal_subject = :subject",
            ):
                await session.execute(
                    text(statement),
                    {"subject": subject, "series_id": series_id if series else None},
                )
        await engine.dispose()
