"""Provider-neutral values that must not cross the public calendar boundary.

These records are used only between the calendar application service, its
provider port, and the repository.  In particular, ``provider_id`` and the
provider's next-page state never appear in public DTOs.
"""

from __future__ import annotations

from dataclasses import dataclass

from .public import CalendarQuery, CalendarRequestContext, CalendarRevocationLease

__all__ = ["CalendarRequestContext", "CalendarRevocationLease"]


@dataclass(frozen=True, slots=True)
class CalendarDiscoveryRecord:
    """One readable provider calendar before Aura metadata is projected."""

    provider_id: str
    display_name: str
    time_zone: str | None
    readable: bool = True


@dataclass(frozen=True, slots=True)
class CalendarDiscoveryPage:
    """One bounded provider page with server-only continuation state."""

    items: tuple[CalendarDiscoveryRecord, ...]
    next_state: str | None


@dataclass(frozen=True, slots=True)
class CalendarDiscoveryQuery:
    """Stable, content-free binding for a discovery cursor handle."""

    limit: int


@dataclass(frozen=True, slots=True)
class CalendarConflictQuery:
    """Content-free binding for a conflict relationship continuation."""

    query: CalendarQuery
    conflict_limit: int


@dataclass(frozen=True, slots=True)
class CalendarConflictCursorState:
    """Server-only continuation; event content is deliberately absent."""

    provider_cursor: str | None
    pages_consumed: int
    emitted_relationships: tuple[str, ...]


type CalendarCursorBinding = CalendarQuery | CalendarDiscoveryQuery | CalendarConflictQuery
