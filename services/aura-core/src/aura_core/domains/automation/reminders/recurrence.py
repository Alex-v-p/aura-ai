"""Deterministic wall-clock recurrence rules for reminder series.

This module deliberately contains no persistence or provider dependencies.  A
rule is evaluated in local calendar time and only then converted to an instant
in its validated IANA timezone.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class RecurrenceValidationError(ValueError):
    """Raised when a recurrence rule or timezone is invalid."""


class RecurrenceFrequency(StrEnum):
    DAILY = "daily"
    SELECTED_WEEKDAY = "selected_weekday"
    WEEKLY = "weekly"
    MONTHLY = "monthly"


def validate_timezone(value: str) -> str:
    if not value or len(value) > 128 or value.upper() in {"UTC", "GMT"}:
        # UTC is a valid IANA zone and is intentionally accepted below.  The
        # branch only rejects empty/oversized values; keep the check explicit
        # for clearer validation errors.
        if value not in {"UTC", "GMT"}:
            raise RecurrenceValidationError("timezone must be an IANA timezone")
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise RecurrenceValidationError("timezone must be an IANA timezone") from exc
    return value


def _last_day(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]


def _add_months(value: date, months: int, requested_day: int) -> date:
    absolute = value.year * 12 + value.month - 1 + months
    year, month_index = divmod(absolute, 12)
    month = month_index + 1
    return date(year, month, min(requested_day, _last_day(year, month)))


def _localize(value: datetime, timezone: str) -> datetime:
    """Resolve a naive local wall time with explicit DST gap/overlap rules."""

    zone = ZoneInfo(validate_timezone(timezone))
    naive = value.replace(tzinfo=None)
    candidates: list[datetime] = []
    for fold in (0, 1):
        candidate = naive.replace(tzinfo=zone, fold=fold)
        round_trip = candidate.astimezone(ZoneInfo("UTC")).astimezone(zone)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)
    if candidates:
        # For an overlap, the earlier instant is the deterministic choice.
        return min(candidates, key=lambda item: item.astimezone(ZoneInfo("UTC")))

    # A gap has no valid wall-clock instant. Find the transition boundary and
    # use its first valid local instant (02:30 becomes 03:00, not 03:30).
    def is_valid(candidate: datetime) -> bool:
        localized = candidate.replace(tzinfo=zone, fold=0)
        round_trip = localized.astimezone(ZoneInfo("UTC")).astimezone(zone)
        return round_trip.replace(tzinfo=None) == candidate

    low = naive
    high = naive + timedelta(days=2)
    while not is_valid(high):
        high += timedelta(days=2)
    while low < high:
        midpoint = low + (high - low) // 2
        if is_valid(midpoint):
            high = midpoint
        else:
            low = midpoint + timedelta(microseconds=1)
    return low.replace(tzinfo=zone, fold=0)


@dataclass(frozen=True, slots=True)
class RecurrenceRule:
    frequency: RecurrenceFrequency
    interval: int = 1
    weekdays: tuple[int, ...] = ()
    month_day: int | None = None
    count: int | None = None
    end_date: date | None = None

    def __post_init__(self) -> None:
        frequency = RecurrenceFrequency(self.frequency)
        object.__setattr__(self, "frequency", frequency)
        if self.interval < 1 or self.interval > 365:
            raise RecurrenceValidationError("recurrence interval must be between 1 and 365")
        days = tuple(sorted(set(int(day) for day in self.weekdays)))
        if any(day < 1 or day > 7 for day in days):
            raise RecurrenceValidationError("weekdays must contain ISO values from 1 to 7")
        if (
            frequency in {RecurrenceFrequency.SELECTED_WEEKDAY, RecurrenceFrequency.WEEKLY}
            and not days
        ):
            raise RecurrenceValidationError("selected weekday and weekly rules require weekdays")
        if (
            frequency not in {RecurrenceFrequency.SELECTED_WEEKDAY, RecurrenceFrequency.WEEKLY}
            and days
        ):
            raise RecurrenceValidationError("weekdays are only valid for weekday and weekly rules")
        object.__setattr__(self, "weekdays", days)
        if self.month_day is not None and not 1 <= self.month_day <= 31:
            raise RecurrenceValidationError("monthly day must be between 1 and 31")
        if frequency is RecurrenceFrequency.MONTHLY and self.month_day is None:
            raise RecurrenceValidationError("monthly rules require month_day")
        if frequency is not RecurrenceFrequency.MONTHLY and self.month_day is not None:
            raise RecurrenceValidationError("month_day is only valid for monthly rules")
        if self.count is not None and not 1 <= self.count <= 10000:
            raise RecurrenceValidationError("recurrence count must be between 1 and 10000")
        if self.count is not None and self.end_date is not None:
            raise RecurrenceValidationError("count and end_date cannot both be set")

    def to_payload(self) -> dict[str, object]:
        return {
            "frequency": self.frequency.value,
            "interval": self.interval,
            "weekdays": list(self.weekdays),
            "monthDay": self.month_day,
            "count": self.count,
            "endDate": self.end_date.isoformat() if self.end_date else None,
        }

    @classmethod
    def from_payload(cls, payload: dict[str, object]) -> RecurrenceRule:
        raw_frequency = payload.get("frequency")
        if not isinstance(raw_frequency, str):
            raise RecurrenceValidationError("frequency must be a string")
        raw_interval = payload.get("interval", 1)
        if type(raw_interval) is not int:
            raise RecurrenceValidationError("interval must be an integer")
        raw_days = payload.get("weekdays", [])
        if not isinstance(raw_days, list):
            raise RecurrenceValidationError("weekdays must be a list of integers")
        days: list[int] = []
        for item in cast(list[object], raw_days):
            if type(item) is not int:
                raise RecurrenceValidationError("weekdays must be a list of integers")
            days.append(item)
        raw_month_day = payload.get("monthDay", payload.get("monthlyDay"))
        if raw_month_day is not None and type(raw_month_day) is not int:
            raise RecurrenceValidationError("monthDay must be an integer")
        raw_count = payload.get("count")
        if raw_count is not None and type(raw_count) is not int:
            raise RecurrenceValidationError("count must be an integer")
        raw_end = payload.get("endDate")
        if raw_end is not None and not isinstance(raw_end, str):
            raise RecurrenceValidationError("endDate must be an ISO date")
        return cls(
            RecurrenceFrequency(raw_frequency),
            raw_interval,
            tuple(days),
            raw_month_day,
            raw_count,
            date.fromisoformat(raw_end) if raw_end else None,
        )

    @property
    def monthly_day(self) -> int | None:
        """Compatibility spelling used by the transport-facing design docs."""
        return self.month_day

    def next_local(self, previous: datetime, *, anchor: datetime | None = None) -> datetime:
        """Return the next naive local date/time after ``previous``."""

        local = previous.replace(tzinfo=None)
        if self.frequency is RecurrenceFrequency.DAILY:
            return local + timedelta(days=self.interval)
        if self.frequency is RecurrenceFrequency.MONTHLY:
            next_day = _add_months(local.date(), self.interval, self.month_day or local.day)
            return datetime.combine(next_day, local.timetz().replace(tzinfo=None))

        selected = self.weekdays
        origin = (anchor or previous).replace(tzinfo=None)
        origin_monday = origin.date() - timedelta(days=origin.weekday())
        # Weekday rules are anchored to the first local week. Interval 2 means
        # every other eligible week, while multiple selected days remain in
        # the same eligible week instead of being skipped.
        week = (local.date() - origin_monday).days // 7
        for offset in range(0, 7 * (self.interval + 2)):
            candidate = local.date() + timedelta(days=offset + 1)
            candidate_week = (candidate - origin_monday).days // 7
            if (
                candidate.isoweekday() in selected
                and candidate_week >= week
                and candidate_week % self.interval == 0
            ):
                return datetime.combine(candidate, local.timetz().replace(tzinfo=None))
        raise RecurrenceValidationError("unable to calculate weekly recurrence")

    def instant(self, local: datetime, timezone: str) -> datetime:
        return _localize(local, timezone)


def next_occurrence(
    rule: RecurrenceRule,
    previous_local: datetime,
    timezone: str,
    *,
    anchor_local: datetime | None = None,
) -> tuple[datetime, datetime]:
    local = rule.next_local(previous_local, anchor=anchor_local)
    return local, rule.instant(local, timezone)


__all__ = [
    "RecurrenceFrequency",
    "RecurrenceRule",
    "RecurrenceValidationError",
    "next_occurrence",
    "validate_timezone",
]
