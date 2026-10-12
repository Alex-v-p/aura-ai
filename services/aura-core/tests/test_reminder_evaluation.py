"""Deterministic, replayable evaluators for the AURA-0054 acceptance criteria."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from aura_core.domains.automation.reminders.public import (
    ReminderNotFound,
    ReminderStatus,
    ReminderValidationError,
    ReminderView,
    normalize_due_at,
)
from aura_core.domains.automation.reminders.repository import MemoryReminderRepository

FIXTURE_REVISION = "reminder-eval-fixtures-v1"
OWNER_ISSUER = "https://evaluation.example"
OWNER = "owner"
FOREIGN_OWNER = "foreign-owner"
NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


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
