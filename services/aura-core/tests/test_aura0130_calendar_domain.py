"""Adversarial tests for the provider-neutral calendar domain boundary."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from aura_core.domains.integrations.calendar.public import (
    CalendarConflictRelationship,
    CalendarConnection,
    CalendarConnectionAction,
    CalendarConnectionStatus,
    CalendarEventAvailability,
    CalendarQuery,
    CalendarValidationError,
    TimedCalendarEvent,
    evaluate_conflicts,
    normalize_all_day_event,
    normalize_timed_event,
    validate_query,
)

OWNER_CALENDAR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
UTC_START = datetime(2026, 3, 29, 0, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (CalendarConnectionStatus.DISCONNECTED, ("connect",)),
        (CalendarConnectionStatus.CONNECTING, ("complete_connection",)),
        (CalendarConnectionStatus.CONNECTED, ("disconnect",)),
        (
            CalendarConnectionStatus.REAUTHORIZATION_REQUIRED,
            ("reauthorize", "disconnect"),
        ),
        (CalendarConnectionStatus.REVOCATION_UNCERTAIN, ("disconnect",)),
        (CalendarConnectionStatus.DISABLED, ()),
        (CalendarConnectionStatus.UNAVAILABLE, ()),
    ],
)
def test_connection_actions_are_closed_lifecycle_contract(
    status: CalendarConnectionStatus, expected: tuple[str, ...]
) -> None:
    connection = CalendarConnection(status, False, 0, None, UTC_START)

    assert connection.available_actions == expected
    assert set(connection.available_actions) <= {
        action.value for action in CalendarConnectionAction
    }


def _query(**overrides: object) -> CalendarQuery:
    values: dict[str, object] = {
        "range_start": UTC_START,
        "range_end": UTC_START + timedelta(days=1),
        "display_time_zone": "Europe/Brussels",
        "calendar_ids": (OWNER_CALENDAR,),
        "limit": 50,
        "cursor": None,
        "timeout_ms": 5_000,
    }
    values.update(overrides)
    return CalendarQuery(**values)  # type: ignore[arg-type]


def _event(
    event_id: str,
    start: datetime,
    end: datetime,
    *,
    availability: CalendarEventAvailability = CalendarEventAvailability.BUSY,
) -> TimedCalendarEvent:
    return TimedCalendarEvent(
        id=event_id,
        calendar_id=OWNER_CALENDAR,
        title="fixture title",
        description=None,
        location=None,
        availability=availability,
        start_instant=start,
        end_instant=end,
        event_time_zone="Europe/Brussels",
        display_time_zone="Europe/Brussels",
        spans_multiple_days=False,
    )


def test_query_normalizes_instants_to_utc_and_preserves_display_zone() -> None:
    query = validate_query(
        _query(
            range_start=datetime(2026, 3, 29, 1, 0, tzinfo=timezone(timedelta(hours=1))),
            range_end=datetime(2026, 3, 29, 4, 0, tzinfo=timezone(timedelta(hours=2))),
        )
    )

    assert query.range_start.tzinfo is UTC
    assert query.range_end.tzinfo is UTC
    assert query.display_time_zone == "Europe/Brussels"


@pytest.mark.parametrize(
    "overrides",
    [
        {"range_start": datetime(2026, 1, 1), "range_end": datetime(2026, 1, 2)},
        {"range_end": UTC_START},
        {"range_end": UTC_START + timedelta(days=31, seconds=1)},
        {"display_time_zone": "Not/AZone"},
        {"calendar_ids": ()},
        {"calendar_ids": tuple(uuid4() for _ in range(51))},
        {"calendar_ids": (OWNER_CALENDAR, OWNER_CALENDAR)},
        {"limit": 0},
        {"limit": 101},
        {"timeout_ms": 499},
        {"timeout_ms": 10_001},
    ],
)
def test_query_rejects_invalid_ranges_selection_timezone_and_bounds(
    overrides: dict[str, object],
) -> None:
    with pytest.raises(CalendarValidationError):
        validate_query(_query(**overrides))


def test_normalization_handles_dst_gap_overlap_and_cross_display_day() -> None:
    gap = normalize_timed_event(
        event_id="dst-gap",
        calendar_id=OWNER_CALENDAR,
        title="gap",
        description=None,
        location=None,
        availability=CalendarEventAvailability.BUSY,
        start_instant=datetime(2026, 3, 29, 0, 30, tzinfo=UTC),
        end_instant=datetime(2026, 3, 29, 2, 30, tzinfo=UTC),
        event_time_zone="Europe/Brussels",
        display_time_zone="Europe/Brussels",
    )
    overlap = normalize_timed_event(
        event_id="dst-overlap",
        calendar_id=OWNER_CALENDAR,
        title="overlap",
        description=None,
        location=None,
        availability=CalendarEventAvailability.BUSY,
        start_instant=datetime(2026, 10, 25, 0, 30, tzinfo=UTC),
        end_instant=datetime(2026, 10, 25, 2, 30, tzinfo=UTC),
        event_time_zone="Europe/Brussels",
        display_time_zone="Europe/Brussels",
    )

    assert gap.start_instant.tzinfo is UTC
    assert overlap.end_instant > overlap.start_instant
    assert gap.spans_multiple_days is False
    assert overlap.spans_multiple_days is False

    cross_day = normalize_timed_event(
        event_id="cross-day",
        calendar_id=OWNER_CALENDAR,
        title="cross",
        description=None,
        location=None,
        availability=CalendarEventAvailability.BUSY,
        start_instant=datetime(2026, 1, 1, 21, 0, tzinfo=UTC),
        end_instant=datetime(2026, 1, 2, 1, 0, tzinfo=UTC),
        event_time_zone="UTC",
        display_time_zone="Europe/Brussels",
    )
    assert cross_day.spans_multiple_days is True


def test_normalization_enforces_closed_content_bounds() -> None:
    with pytest.raises(CalendarValidationError):
        normalize_timed_event(
            event_id="x",
            calendar_id=OWNER_CALENDAR,
            title="t" * 1001,
            description=None,
            location=None,
            availability=CalendarEventAvailability.BUSY,
            start_instant=UTC_START,
            end_instant=UTC_START + timedelta(hours=1),
            event_time_zone="UTC",
            display_time_zone="UTC",
        )


def test_conflicts_skip_free_and_adjacency_and_emit_canonical_relationships() -> None:
    start = datetime(2026, 1, 1, 9, tzinfo=UTC)
    events = (
        _event("contains", start, start + timedelta(hours=3)),
        _event("contained", start + timedelta(hours=1), start + timedelta(hours=2)),
        _event("same", start, start + timedelta(hours=3)),
        _event("adjacent", start + timedelta(hours=3), start + timedelta(hours=4)),
        _event(
            "free",
            start + timedelta(minutes=30),
            start + timedelta(hours=1),
            availability=CalendarEventAvailability.FREE,
        ),
    )

    conflicts = evaluate_conflicts(events)
    assert [(item.left.event_id, item.right.event_id, item.relationship) for item in conflicts] == [
        ("contains", "same", CalendarConflictRelationship.SAME_INTERVAL),
        ("contains", "contained", CalendarConflictRelationship.CONTAINS),
        ("same", "contained", CalendarConflictRelationship.CONTAINS),
    ]
    assert all(
        item.left.event_id < item.right.event_id
        or item.relationship is CalendarConflictRelationship.CONTAINS
        for item in conflicts
    )


def test_conflicts_are_deterministic_and_bounded() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    events = tuple(_event(str(index), start, start + timedelta(hours=2)) for index in range(30))
    first = evaluate_conflicts(events, limit=200)
    second = evaluate_conflicts(tuple(reversed(events)), limit=200)

    assert len(first) == 200
    assert first == second
    with pytest.raises(CalendarValidationError):
        evaluate_conflicts(events, limit=201)


def test_conflicts_dedupe_repeated_provider_rows_but_preserve_cross_calendar_identity() -> None:
    start = datetime(2026, 1, 1, 9, tzinfo=UTC)
    primary = _event("shared", start, start + timedelta(hours=1))
    duplicate_primary = replace(primary)
    other_calendar = replace(
        primary,
        calendar_id=UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
    )

    conflicts = evaluate_conflicts((primary, duplicate_primary, other_calendar))

    assert len(conflicts) == 1
    assert conflicts[0].left.calendar_id != conflicts[0].right.calendar_id
    assert conflicts[0].left.event_id == conflicts[0].right.event_id == "shared"


@pytest.mark.parametrize(
    ("event_time_zone", "display_time_zone", "start_date", "end_date"),
    [
        ("Not/IANA", "UTC", datetime(2026, 1, 1).date(), datetime(2026, 1, 2).date()),
        ("Europe/Brussels", "Not/IANA", datetime(2026, 1, 1).date(), datetime(2026, 1, 2).date()),
        (
            "Europe/Brussels",
            "Europe/Brussels",
            datetime(2026, 1, 2).date(),
            datetime(2026, 1, 2).date(),
        ),
    ],
)
def test_all_day_normalization_rejects_invalid_iana_and_non_half_open_midnight_ranges(
    event_time_zone: str, display_time_zone: str, start_date: date, end_date: date
) -> None:
    with pytest.raises(CalendarValidationError):
        normalize_all_day_event(
            event_id="all-day",
            calendar_id=OWNER_CALENDAR,
            title="holiday",
            description=None,
            location=None,
            availability=CalendarEventAvailability.BUSY,
            start_date=start_date,
            end_date_exclusive=end_date,
            event_time_zone=event_time_zone,
            display_time_zone=display_time_zone,
        )


def test_calendar_domain_public_module_has_no_provider_or_persistence_imports() -> None:
    public_path = (
        Path(__file__).parents[1] / "src/aura_core/domains/integrations/calendar/public.py"
    )
    source = public_path.read_text(encoding="utf-8")
    assert "sqlalchemy" not in source
    assert "google" not in source.lower()
    assert "providers." not in source
