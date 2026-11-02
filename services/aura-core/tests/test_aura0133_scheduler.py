"""Deterministic schedule and due-time evaluations for AURA-0133.

These checks exercise the public briefing value objects and recurrence helpers.
They intentionally avoid SQLAlchemy internals so the same invariants hold for
the in-memory and PostgreSQL compositions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from aura_core.domains.automation.briefings.public import (
    BriefingDailyCadence,
    BriefingSourceGrants,
    BriefingValidationError,
    due_instant,
    next_due,
    occurrence_id,
)


def _cadence(local_time: time = time(2, 30), zone: str = "Europe/Brussels") -> BriefingDailyCadence:
    return BriefingDailyCadence(local_time, zone)


def test_due_identity_is_stable_for_one_schedule_local_date() -> None:
    schedule_id = uuid4()
    first = occurrence_id(schedule_id, date(2026, 10, 25))
    second = occurrence_id(schedule_id, date(2026, 10, 25))

    assert first == second
    assert first != occurrence_id(schedule_id, date(2026, 10, 26))
    assert first != occurrence_id(uuid4(), date(2026, 10, 25))


def test_dst_gap_moves_to_the_first_valid_instant() -> None:
    # Europe/Brussels skips 02:30 on the spring transition.
    due = due_instant(date(2026, 3, 29), time(2, 30), "Europe/Brussels")

    assert due == datetime(2026, 3, 29, 1, 0, tzinfo=UTC)


def test_dst_overlap_is_emitted_at_the_earlier_instant_once() -> None:
    # The 02:30 wall time occurs twice; the earlier UTC instant is selected.
    due = due_instant(date(2026, 10, 25), time(2, 30), "Europe/Brussels")

    assert due == datetime(2026, 10, 25, 0, 30, tzinfo=UTC)
    local_date, repeated = next_due(
        datetime(2026, 10, 25, 0, 0, tzinfo=UTC), _cadence()
    )
    assert local_date == date(2026, 10, 25)
    assert repeated == due


def test_next_due_preserves_local_wall_time_across_offset_change() -> None:
    local_date, due = next_due(datetime(2026, 3, 28, 12, tzinfo=UTC), _cadence())
    following_date, following_due = next_due(due, _cadence())

    assert local_date == date(2026, 3, 29)
    assert following_date == date(2026, 3, 30)
    assert due.astimezone(ZoneInfo("Europe/Brussels")).hour == 3
    assert following_due.astimezone(ZoneInfo("Europe/Brussels")).hour == 2


@pytest.mark.parametrize("zone", ["", "Mars/Olympus", "UTC+01:00"])
def test_cadence_requires_an_explicit_iana_timezone(zone: str) -> None:
    with pytest.raises(ValueError):
        BriefingDailyCadence(time(9), zone)


def test_cadence_rejects_offset_bearing_or_subminute_local_times() -> None:
    with pytest.raises(BriefingValidationError):
        BriefingDailyCadence(time(9, tzinfo=UTC), "UTC")
    with pytest.raises(BriefingValidationError):
        BriefingDailyCadence(time(9, 1, 1), "UTC")


def test_initial_sources_require_calendar_and_never_model_memory() -> None:
    sources = BriefingSourceGrants(True, (uuid4(),), (uuid4(),))
    payload = sources.as_payload()

    assert payload["includeReminders"] is True
    assert "memory" not in payload
    assert "memory_ids" not in payload
    with pytest.raises(BriefingValidationError):
        BriefingSourceGrants(False, (uuid4(),), ())
    with pytest.raises(BriefingValidationError):
        BriefingSourceGrants(True, (), ())
