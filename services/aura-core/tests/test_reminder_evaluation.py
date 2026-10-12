"""Deterministic, replayable evaluators for the AURA-0054 acceptance criteria."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import NAMESPACE_URL, uuid5
from zoneinfo import ZoneInfo

import pytest
from aura_core.domains.automation.notifications.repository import MemoryNotificationRepository
from aura_core.domains.automation.reminders.public import (
    RecurrenceFrequency,
    RecurrenceRule,
    ReminderNotFound,
    ReminderSeriesStatus,
    ReminderStatus,
    ReminderValidationError,
    ReminderView,
    normalize_due_at,
)
from aura_core.domains.automation.reminders.recurrence import next_occurrence
from aura_core.domains.automation.reminders.repository import MemoryReminderRepository

FIXTURE_REVISION = "reminder-eval-fixtures-v1"
OWNER_ISSUER = "https://evaluation.example"
OWNER = "owner"
FOREIGN_OWNER = "foreign-owner"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
RECURRING_FIXTURE_REVISION = "recurring-reminder-eval-fixtures-v1"
RECURRING_EVALUATORS = {
    "recurring_reminder_rule_correctness",
    "recurring_reminder_timezone_correctness",
    "recurring_reminder_exactly_one_future_occurrence",
    "recurring_reminder_bounded_catch_up",
    "recurring_reminder_race_safety",
    "recurring_reminder_owner_isolation",
}


def _recurring_key(label: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"aura0064-evaluator:{label}"))


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    evaluator_id: str
    fixture_revision: str
    replay_key: str
    score: float
    outcome: str
    metadata: dict[str, str]

    def payload(self) -> dict[str, Any]:
        return {
            "evaluatorId": self.evaluator_id,
            "fixtureRevision": self.fixture_revision,
            "replayKey": self.replay_key,
            "score": self.score,
            "outcome": self.outcome,
            "metadata": self.metadata,
        }


def _result(evaluator_id: str, replay_key: str, passed: bool, **metadata: str) -> EvaluationResult:
    return EvaluationResult(
        evaluator_id,
        FIXTURE_REVISION,
        replay_key,
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        metadata,
    )


def _recurring_result(
    evaluator_id: str, replay_key: str, passed: bool, **metadata: str
) -> EvaluationResult:
    return EvaluationResult(
        evaluator_id,
        RECURRING_FIXTURE_REVISION,
        replay_key,
        1.0 if passed else 0.0,
        "pass" if passed else "fail",
        metadata,
    )


async def _results() -> tuple[EvaluationResult, ...]:
    repository = MemoryReminderRepository(clock=lambda: NOW)
    created = await repository.create(
        OWNER_ISSUER,
        OWNER,
        content="evaluation-only reminder content",
        due_at=datetime(2026, 2, 1, 9, tzinfo=UTC),
        idempotency_key="evaluation-create",
    )
    completed = await repository.complete(
        OWNER_ISSUER,
        OWNER,
        created.id,
        expected_version=1,
        idempotency_key="evaluation-complete",
    )
    lifecycle = (
        created.status is ReminderStatus.SCHEDULED
        and completed.status is ReminderStatus.COMPLETED
        and completed.version == 2
    )

    owner_read = (await repository.get(OWNER_ISSUER, OWNER, created.id)).id == created.id
    try:
        await repository.get(OWNER_ISSUER, FOREIGN_OWNER, created.id)
    except ReminderNotFound:
        foreign_denied = True
    else:
        foreign_denied = False

    equivalent_one = normalize_due_at(
        datetime(2026, 3, 29, 1, 30, tzinfo=UTC), now=NOW
    )
    equivalent_two = normalize_due_at(
        datetime(2026, 3, 29, 3, 30, tzinfo=ZoneInfo("Europe/Brussels")), now=NOW
    )
    try:
        normalize_due_at(datetime(2026, 1, 1, 12, 0), now=NOW)
    except ReminderValidationError:
        naive_denied = True
    else:
        naive_denied = False
    time_valid = equivalent_one == equivalent_two and equivalent_one.tzinfo is UTC and naive_denied

    return (
        _result(
            "reminder_lifecycle_correctness",
            "reminder-lifecycle-v1",
            lifecycle,
            initial_status=created.status.value,
            terminal_status=completed.status.value,
        ),
        _result(
            "reminder_owner_authorization",
            "reminder-owner-v1",
            owner_read and foreign_denied,
            owner_read=str(owner_read).lower(),
            foreign_denied=str(foreign_denied).lower(),
        ),
        _result(
            "reminder_time_normalization",
            "reminder-time-v1",
            time_valid,
            normalized_timezone="UTC",
            explicit_offset_required=str(naive_denied).lower(),
        ),
    )


async def _recurring_results() -> tuple[EvaluationResult, ...]:
    daily = RecurrenceRule(RecurrenceFrequency.DAILY, interval=2)
    daily_next, _ = next_occurrence(daily, datetime(2026, 1, 5, 9, 30), "Europe/Brussels")
    weekly = RecurrenceRule(RecurrenceFrequency.WEEKLY, weekdays=(1, 3))
    weekly_next, _ = next_occurrence(
        weekly,
        datetime(2026, 1, 5, 9, 30),
        "Europe/Brussels",
        anchor_local=datetime(2026, 1, 5, 9, 30),
    )
    rule_correct = daily_next == datetime(2026, 1, 7, 9, 30) and weekly_next == datetime(
        2026, 1, 7, 9, 30
    )

    gap_local, gap_due = next_occurrence(
        RecurrenceRule(RecurrenceFrequency.DAILY),
        datetime(2026, 3, 28, 2, 30),
        "Europe/Brussels",
    )
    overlap_due = next_occurrence(
        RecurrenceRule(RecurrenceFrequency.DAILY),
        datetime(2026, 10, 24, 2, 30),
        "Europe/Brussels",
    )[1]
    timezone_correct = (
        gap_local == datetime(2026, 3, 29, 2, 30)
        and gap_due.astimezone(UTC) == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)
        and overlap_due.astimezone(UTC) == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    )

    exact_repository = MemoryReminderRepository(clock=lambda: NOW)
    exact_series = await exact_repository.create_series(
        OWNER_ISSUER,
        OWNER,
        content="private evaluator reminder",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=RecurrenceRule(RecurrenceFrequency.DAILY),
        idempotency_key=_recurring_key("exact-create"),
    )
    exact_occurrences = await exact_repository.list_occurrences(
        OWNER_ISSUER, OWNER, exact_series.id
    )
    exact_one = (
        sum(item.status.value == "scheduled" for item in exact_occurrences) == 1
        and exact_series.next_reminder_id is not None
    )

    recovery_repository = MemoryReminderRepository(clock=lambda: NOW)
    recovery_series = await recovery_repository.create_series(
        OWNER_ISSUER,
        OWNER,
        content="private evaluator recovery",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=RecurrenceRule(RecurrenceFrequency.DAILY),
        idempotency_key=_recurring_key("recovery-create"),
    )
    recovery_notifications = MemoryNotificationRepository(
        recovery_repository,
        clock=lambda: NOW + timedelta(days=9),
    )
    recovery_result = await recovery_notifications.deliver_due(limit=10)
    recovery_updated = await recovery_repository.get_series(
        OWNER_ISSUER, OWNER, recovery_series.id
    )
    bounded = (
        recovery_result.fired_count == 1
        and recovery_updated.missed_count == 5
        and recovery_updated.next_due_at is not None
        and recovery_updated.next_due_at > NOW + timedelta(days=9)
    )

    race_repository = MemoryReminderRepository(clock=lambda: NOW)
    race_series = await race_repository.create_series(
        OWNER_ISSUER,
        OWNER,
        content="private evaluator race",
        local_start=datetime(2026, 1, 5, 9, 30),
        timezone="Europe/Brussels",
        rule=RecurrenceRule(RecurrenceFrequency.DAILY),
        idempotency_key=_recurring_key("race-create"),
    )
    race_notifications = MemoryNotificationRepository(
        race_repository,
        clock=lambda: NOW + timedelta(days=9),
    )
    race_results = await asyncio.gather(
        race_notifications.deliver_due(limit=10),
        race_repository.cancel_series(
            OWNER_ISSUER,
            OWNER,
            race_series.id,
            expected_version=race_series.version,
            idempotency_key=_recurring_key("race-cancel"),
        ),
        return_exceptions=True,
    )
    race_updated = await race_repository.get_series(OWNER_ISSUER, OWNER, race_series.id)
    race_safe = (
        race_updated.status in {ReminderSeriesStatus.ACTIVE, ReminderSeriesStatus.CANCELED}
        and len((await race_notifications.list(OWNER_ISSUER, OWNER, limit=10)).items) <= 1
        and sum(getattr(item, "fired_count", 0) for item in race_results) <= 1
    )

    foreign_denied = False
    try:
        await exact_repository.get_series(
            "https://evaluation.example", "foreign-owner", exact_series.id
        )
    except Exception:
        foreign_denied = True

    return (
        _recurring_result(
            "recurring_reminder_rule_correctness",
            "recurring-eval-rule-v1",
            rule_correct,
            daily_next="2026-01-07T09:30",
            weekly_next="2026-01-07T09:30",
        ),
        _recurring_result(
            "recurring_reminder_timezone_correctness",
            "recurring-eval-timezone-v1",
            timezone_correct,
            dst_gap="first-valid-instant",
            overlap="earlier-instant",
        ),
        _recurring_result(
            "recurring_reminder_exactly_one_future_occurrence",
            "recurring-eval-exact-one-v1",
            exact_one,
            scheduled_count="1",
        ),
        _recurring_result(
            "recurring_reminder_bounded_catch_up",
            "recurring-eval-catch-up-v1",
            bounded,
            delivered="1",
            additionally_missed="5",
        ),
        _recurring_result(
            "recurring_reminder_race_safety",
            "recurring-eval-race-v1",
            race_safe,
            notification_count="at-most-one",
        ),
        _recurring_result(
            "recurring_reminder_owner_isolation",
            "recurring-eval-owner-v1",
            foreign_denied,
            foreign_read="denied",
        ),
    )


@pytest.mark.asyncio
async def test_required_reminder_evaluators_are_replayable_and_content_free() -> None:
    first = await _results()
    second = await _results()
    assert first == second
    assert {item.evaluator_id for item in first} == {
        "reminder_lifecycle_correctness",
        "reminder_owner_authorization",
        "reminder_time_normalization",
    }
    assert all(item.outcome == "pass" and item.score == 1.0 for item in first)
    assert len({item.replay_key for item in first}) == len(first)
    for item in first:
        payload = item.payload()
        assert set(payload) == {
            "evaluatorId",
            "fixtureRevision",
            "replayKey",
            "score",
            "outcome",
            "metadata",
        }
        assert "evaluation-only" not in repr(payload)
        assert not any(
            forbidden in repr(payload).lower()
            for forbidden in ("content", "subject", "issuer", "prompt", "response")
        )


@pytest.mark.asyncio
async def test_required_recurring_evaluators_are_replayable_and_metadata_only() -> None:
    first = await _recurring_results()
    second = await _recurring_results()
    assert first == second
    assert {item.evaluator_id for item in first} == RECURRING_EVALUATORS
    assert all(item.outcome == "pass" and item.score == 1.0 for item in first)
    assert {item.fixture_revision for item in first} == {RECURRING_FIXTURE_REVISION}
    assert len({item.replay_key for item in first}) == len(first)
    for item in first:
        payload = item.payload()
        assert set(payload) == {
            "evaluatorId",
            "fixtureRevision",
            "replayKey",
            "score",
            "outcome",
            "metadata",
        }
        rendered = repr(payload["metadata"]).lower()
        assert not any(
            forbidden in rendered
            for forbidden in (
                "private",
                "content",
                "subject",
                "issuer",
                "timezone",
                "local_start",
                "due_at",
                "owner",
            )
        )


@pytest.mark.asyncio
async def test_upcoming_and_history_evaluation_views_have_distinct_deterministic_ordering() -> None:
    repository = MemoryReminderRepository(clock=lambda: NOW)
    upcoming = await repository.create(
        OWNER_ISSUER,
        OWNER,
        content="upcoming",
        due_at=datetime(2026, 2, 2, tzinfo=UTC),
        idempotency_key="ordering-upcoming",
    )
    earlier = await repository.create(
        OWNER_ISSUER,
        OWNER,
        content="earlier",
        due_at=datetime(2026, 2, 1, tzinfo=UTC),
        idempotency_key="ordering-earlier",
    )
    await repository.complete(
        OWNER_ISSUER, OWNER, earlier.id, expected_version=1, idempotency_key="ordering-complete"
    )
    upcoming_page = await repository.list(
        OWNER_ISSUER, OWNER, view=ReminderView.UPCOMING, limit=10
    )
    history_page = await repository.list(OWNER_ISSUER, OWNER, view=ReminderView.HISTORY, limit=10)
    assert [item.id for item in upcoming_page.items] == [upcoming.id]
    assert [item.id for item in history_page.items] == [earlier.id]
