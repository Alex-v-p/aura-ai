"""Application-boundary tests for owner scope and read gating."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.domains.integrations.calendar.application import CalendarApplicationService
from aura_core.domains.integrations.calendar.internal import CalendarDiscoveryPage
from aura_core.domains.integrations.calendar.public import (
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarEventPage,
    CalendarNotConnected,
    CalendarQuery,
    CalendarRequestContext,
    CalendarSelection,
    CalendarValidationError,
    SelectableCalendarPage,
)

OWNER = ("https://identity.example", "owner-a")
FOREIGN = ("https://identity.example", "owner-b")
CALENDAR = uuid4()
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeRepository:
    def __init__(
        self, status: CalendarConnectionStatus = CalendarConnectionStatus.CONNECTED
    ) -> None:
        self.status = status
        self.selected: dict[tuple[str, str], tuple[UUID, ...]] = {OWNER: (CALENDAR,)}
        self.owners_seen: list[tuple[str, str]] = []

    async def get_connection(self, owner: tuple[str, str]) -> CalendarConnection:
        self.owners_seen.append(owner)
        return CalendarConnection(
            self.status,
            self.status is CalendarConnectionStatus.CONNECTED,
            1,
            NOW,
            NOW,
        )

    async def save_connection(
        self,
        owner: tuple[str, str],
        connection: CalendarConnection,
        *,
        expected_version: int | None,
    ) -> CalendarConnection:
        del owner, expected_version
        return connection

    async def selected_ids(self, owner: tuple[str, str]) -> tuple[UUID, ...]:
        self.owners_seen.append(owner)
        return self.selected.get(owner, ())

    async def replace_selection(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
    ) -> CalendarSelection:
        self.owners_seen.append(owner)
        if expected_version != 1:
            raise ValueError("version conflict")
        selected = tuple(calendar_ids)
        self.selected[owner] = selected
        return CalendarSelection(selected, 2, NOW)

    async def list_calendars(
        self, owner: tuple[str, str], *, cursor: str | None, limit: int
    ) -> SelectableCalendarPage:
        self.owners_seen.append(owner)
        return SelectableCalendarPage((), cursor, 1, NOW)

    async def upsert_discovered(
        self, owner: tuple[str, str], calendars: Sequence[object], *, observed_at: datetime
    ) -> SelectableCalendarPage:
        del calendars, observed_at
        self.owners_seen.append(owner)
        return SelectableCalendarPage((), None, 1, NOW)


class FakeProvider:
    def __init__(self) -> None:
        self.retrieve_owners: list[tuple[str, str]] = []

    async def discover(
        self,
        owner: tuple[str, str],
        *,
        cursor: str | None,
        limit: int,
        request: CalendarRequestContext,
    ) -> CalendarDiscoveryPage:
        del owner, cursor, limit, request
        return CalendarDiscoveryPage((), None)

    async def retrieve(
        self, owner: tuple[str, str], query: CalendarQuery, *, request: Any = None
    ) -> CalendarEventPage:
        del request
        self.retrieve_owners.append(owner)
        return CalendarEventPage(
            query.range_start,
            query.range_end,
            query.display_time_zone,
            (),
            None,
            False,
            (),
            NOW,
        )

    async def revoke(self, owner: tuple[str, str], *, request: CalendarRequestContext) -> None:
        del owner, request


def _query(calendar_id: UUID = CALENDAR) -> CalendarQuery:
    return CalendarQuery(
        NOW,
        datetime(2026, 1, 2, tzinfo=UTC),
        "UTC",
        (calendar_id,),
    )


@pytest.mark.asyncio
async def test_reads_fail_closed_when_connection_is_not_connected() -> None:
    repository = FakeRepository(CalendarConnectionStatus.REAUTHORIZATION_REQUIRED)
    provider = FakeProvider()
    service = CalendarApplicationService(cast(Any, repository), cast(Any, provider))

    with pytest.raises(CalendarNotConnected):
        await service.events(OWNER, _query())
    assert provider.retrieve_owners == []


@pytest.mark.asyncio
async def test_selected_calendar_allowlist_is_owner_scoped_before_provider_dispatch() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    service = CalendarApplicationService(cast(Any, repository), cast(Any, provider))

    with pytest.raises(CalendarValidationError):
        await service.events(FOREIGN, _query())
    assert provider.retrieve_owners == []
    assert FOREIGN in repository.owners_seen


@pytest.mark.asyncio
async def test_valid_query_dispatches_only_the_authenticated_owner() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    service = CalendarApplicationService(cast(Any, repository), cast(Any, provider))

    result = await service.events(OWNER, _query())

    assert result.items == ()
    assert provider.retrieve_owners == [OWNER]


@pytest.mark.asyncio
async def test_discovery_is_owner_scoped_and_does_not_implicitly_select() -> None:
    repository = FakeRepository()
    provider = FakeProvider()
    service = CalendarApplicationService(cast(Any, repository), cast(Any, provider))

    page = await service.discover(OWNER, limit=100)

    assert page.items == ()
    assert repository.selected[OWNER] == (CALENDAR,)
