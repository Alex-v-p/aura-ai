"""Black-box evaluation of real adapter discovery and event pagination."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid5

import httpx
import pytest
from aura_core.domains.integrations.calendar.application import CalendarApplicationService
from aura_core.domains.integrations.calendar.internal import CalendarDiscoveryQuery
from aura_core.domains.integrations.calendar.public import (
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarQuery,
    CalendarValidationError,
    SelectableCalendar,
    SelectableCalendarPage,
)
from aura_core.providers.calendars.google_calendar import (
    GoogleCalendarAdapter,
    GoogleCalendarCredential,
)

OWNER = ("https://identity.example", "calendar-owner")
FOREIGN_OWNER = ("https://identity.example", "foreign-owner")
CALENDAR_A = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
CALENDAR_B = UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
NOW = datetime(2026, 1, 1, tzinfo=UTC)
CALENDAR_NAMESPACE = UUID("4c3fef10-5e6e-4b22-9b2d-9df9c3f1f8c2")


class TtlProviderCursor:
    """A Valkey-shaped provider cursor port used to assert its contract."""

    def __init__(self) -> None:
        self.now = NOW
        self.values: dict[str, tuple[tuple[str, str], str, str, datetime]] = {}
        self.encoded: list[tuple[tuple[str, str], str, str]] = []

    def encode(self, owner: tuple[str, str], scope: str, provider_token: str) -> str:
        handle = f"opaque-provider-{len(self.values) + 1}"
        self.values[handle] = (owner, scope, provider_token, self.now + timedelta(minutes=10))
        self.encoded.append((owner, scope, provider_token))
        return handle

    def decode(self, owner: tuple[str, str], scope: str, cursor: str) -> str:
        value = self.values.get(cursor)
        if value is None or value[3] <= self.now or value[:2] != (owner, scope):
            raise ValueError("calendar provider cursor is invalid")
        del self.values[cursor]
        return value[2]


class QueryCursorStore:
    """One-shot owner/query-bound application cursor store."""

    def __init__(self) -> None:
        self.now = NOW
        self.values: dict[str, tuple[tuple[str, str], str, object, datetime]] = {}
        self._next = 0

    @staticmethod
    def _fingerprint(query: object) -> str:
        if isinstance(query, CalendarDiscoveryQuery):
            return f"discovery|{query.limit}"
        assert isinstance(query, CalendarQuery)
        return "|".join(
            (
                query.range_start.isoformat(),
                query.range_end.isoformat(),
                query.display_time_zone,
                ",".join(map(str, query.calendar_ids)),
                str(query.limit),
            )
        )

    async def put(self, owner: tuple[str, str], query: object, value: object) -> str:
        self._next += 1
        handle = f"opaque-query-{self._next}"
        self.values[handle] = (
            owner,
            self._fingerprint(query),
            value,
            self.now + timedelta(minutes=10),
        )
        return handle

    async def consume(self, owner: tuple[str, str], query: object, cursor: str) -> object:
        value = self.values.get(cursor)
        if (
            value is None
            or value[3] <= self.now
            or value[:2] != (owner, self._fingerprint(query))
        ):
            raise CalendarValidationError("calendar cursor is invalid")
        del self.values[cursor]
        return value[2]


class CalendarMetadataRepository:
    """Server-side metadata projection used by the application boundary test."""

    def __init__(self) -> None:
        self.connection = CalendarConnection(
            CalendarConnectionStatus.CONNECTED,
            True,
            0,
            NOW,
            NOW,
        )
        self.provider_ids: dict[UUID, str] = {}
        self.calendars: dict[UUID, SelectableCalendar] = {}
        self.selected: dict[tuple[str, str], tuple[UUID, ...]] = {
            OWNER: (CALENDAR_A, CALENDAR_B),
        }
        self.discovery_writes = 0

    async def get_connection(self, owner: tuple[str, str]) -> CalendarConnection:
        del owner
        return self.connection

    async def selected_ids(self, owner: tuple[str, str]) -> tuple[UUID, ...]:
        return self.selected.get(owner, ())

    async def provider_calendar_id(self, owner: tuple[str, str], calendar_id: UUID) -> str:
        del owner
        return self.provider_ids[calendar_id]

    async def upsert_discovered(
        self, owner: tuple[str, str], calendars: Sequence[object], *, observed_at: datetime
    ) -> SelectableCalendarPage:
        self.discovery_writes += 1
        for item in calendars:
            provider_id = getattr(item, "provider_id", None)
            if not isinstance(provider_id, str):
                continue
            aura_id = uuid5(CALENDAR_NAMESPACE, f"{owner[0]}:{owner[1]}:{provider_id}")
            self.provider_ids[aura_id] = provider_id
            self.calendars[aura_id] = SelectableCalendar(
                aura_id,
                str(getattr(item, "display_name", "")),
                str(getattr(item, "time_zone", None) or "UTC"),
                False,
                observed_at,
            )
        return SelectableCalendarPage(
            tuple(self.calendars.values()), None, 1, observed_at
        )


async def _credential(owner: tuple[str, str]) -> GoogleCalendarCredential:
    del owner
    return GoogleCalendarCredential(
        "access-token", expires_at=datetime.now(UTC) + timedelta(hours=1)
    )


def _adapter(handler: Any) -> tuple[GoogleCalendarAdapter, httpx.AsyncClient]:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return (
        GoogleCalendarAdapter(
            client_id="client-id",
            client_secret="client-secret",
            http_client=client,
            credential_loader=_credential,
            calendar_id_resolver=lambda owner, calendar_id: _resolve_calendar(
                owner, calendar_id
            ),
        ),
        client,
    )


async def _resolve_calendar(owner: tuple[str, str], calendar_id: UUID) -> str:
    del owner
    return {CALENDAR_A: "google-calendar-a", CALENDAR_B: "google-calendar-b"}[calendar_id]


def _query(*, cursor: str | None = None, limit: int = 1) -> CalendarQuery:
    return CalendarQuery(
        NOW,
        NOW + timedelta(days=1),
        "UTC",
        (CALENDAR_A, CALENDAR_B),
        limit=limit,
        cursor=cursor,
    )


@pytest.mark.asyncio
async def test_real_adapter_discovery_persists_stable_server_ids_without_exposing_provider_ids(
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "google-primary",
                        "summary": "Primary",
                        "timeZone": "Europe/Brussels",
                        "accessRole": "owner",
                    }
                ]
            },
        )

    adapter, client = _adapter(handler)
    repository = CalendarMetadataRepository()
    cursors = QueryCursorStore()
    service = CalendarApplicationService(
        cast(Any, repository), cast(Any, adapter), cursor_store=cast(Any, cursors)
    )
    try:
        first = await service.discover(OWNER, limit=100)
        second = await service.discover(OWNER, limit=100)
    finally:
        await client.aclose()

    assert repository.discovery_writes == 2
    assert len(first.items) == 1
    assert first.items[0].id == second.items[0].id
    assert first.items[0].selected is False
    assert repository.provider_ids[first.items[0].id] == "google-primary"
    assert not hasattr(first.items[0], "provider_id")
    assert "google-primary" not in repr(first)


@pytest.mark.asyncio
async def test_discovery_cursor_is_owner_bound_opaque_single_use_and_ttl_bounded() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("pageToken") == "provider-calendar-page-2":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "google-secondary",
                            "summary": "Secondary",
                            "accessRole": "reader",
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "items": [
                    {"id": "google-primary", "summary": "Primary", "accessRole": "owner"}
                ],
                "nextPageToken": "provider-calendar-page-2",
            },
        )

    adapter, client = _adapter(handler)
    repository = CalendarMetadataRepository()
    cursors = QueryCursorStore()
    service = CalendarApplicationService(
        cast(Any, repository), cast(Any, adapter), cursor_store=cast(Any, cursors)
    )
    try:
        first = await service.discover(OWNER, limit=100)
        assert first.next_cursor is not None
        assert "provider-calendar-page-2" not in first.next_cursor

        # The continuation must be held by the server-side cursor store.  These
        # calls deliberately remain in the evaluation so a raw Google token,
        # replay, owner mismatch, or missing TTL state cannot regress silently.
        with pytest.raises(CalendarValidationError):
            await service.discover(FOREIGN_OWNER, cursor=first.next_cursor, limit=100)
        second = await service.discover(OWNER, cursor=first.next_cursor, limit=100)
        assert {item.display_name for item in second.items} == {"Primary", "Secondary"}
        assert seen[1].url.params.get("pageToken") == "provider-calendar-page-2"
        with pytest.raises(CalendarValidationError):
            await service.discover(OWNER, cursor=first.next_cursor, limit=100)

        third = await service.discover(OWNER, limit=100)
        assert third.next_cursor is not None
        cursors.now += timedelta(minutes=10, seconds=1)
        with pytest.raises(CalendarValidationError):
            await service.discover(OWNER, cursor=third.next_cursor, limit=100)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_multi_calendar_event_cursor_continues_only_the_calendar_with_provider_page() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        calendar_path = request.url.path.rsplit("/", 2)[-2]
        page_token = request.url.params.get("pageToken")
        if calendar_path == "google-calendar-a" and page_token is None:
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "provider-event-a-1",
                            "summary": "A1",
                            "start": {"dateTime": "2026-01-01T09:00:00Z"},
                            "end": {"dateTime": "2026-01-01T10:00:00Z"},
                        }
                    ],
                    "nextPageToken": "provider-event-a-page-2",
                },
            )
        if calendar_path == "google-calendar-a" and page_token == "provider-event-a-page-2":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "provider-event-a-2",
                            "summary": "A2",
                            "start": {"dateTime": "2026-01-01T11:00:00Z"},
                            "end": {"dateTime": "2026-01-01T12:00:00Z"},
                        }
                    ]
                },
            )
        pytest.fail("the continuation must not dispatch the other calendar")

    adapter, client = _adapter(handler)
    repository = CalendarMetadataRepository()
    cursors = QueryCursorStore()
    service = CalendarApplicationService(
        cast(Any, repository),
        cast(Any, adapter),
        cursor_store=cursors,
    )
    try:
        first = await service.events(OWNER, _query())
        assert first.next_cursor is not None
        assert "provider-event-a-page-2" not in first.next_cursor
        assert first.items[0].calendar_id == CALENDAR_A

        second = await service.events(OWNER, _query(cursor=first.next_cursor))
        assert second.items[0].calendar_id == CALENDAR_A
        assert len(seen) == 2
        assert all("provider-event-a-page-2" not in repr(value) for value in cursors.values)
        assert all(
            "provider-event-a-page-2" not in value
            for value in repository.provider_ids.values()
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_event_cursor_rejects_query_owner_mismatch_replay_and_valkey_loss() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "provider-event-a-1",
                        "summary": "A1",
                        "start": {"dateTime": "2026-01-01T09:00:00Z"},
                        "end": {"dateTime": "2026-01-01T10:00:00Z"},
                    }
                ],
                "nextPageToken": "provider-event-a-page-2",
            },
        )

    adapter, client = _adapter(handler)
    repository = CalendarMetadataRepository()
    cursors = QueryCursorStore()
    service = CalendarApplicationService(
        cast(Any, repository), cast(Any, adapter), cursor_store=cast(Any, cursors)
    )
    try:
        first = await service.events(OWNER, _query())
        assert first.next_cursor is not None

        with pytest.raises(CalendarValidationError):
            await service.events(FOREIGN_OWNER, _query(cursor=first.next_cursor))
        with pytest.raises(CalendarValidationError):
            await service.events(OWNER, _query(cursor=first.next_cursor, limit=2))

        await service.events(OWNER, _query(cursor=first.next_cursor))
        with pytest.raises(CalendarValidationError):
            await service.events(OWNER, _query(cursor=first.next_cursor))

        lost = await service.events(OWNER, _query())
        assert lost.next_cursor is not None
        del cursors.values[lost.next_cursor]
        with pytest.raises(CalendarValidationError):
            await service.events(OWNER, _query(cursor=lost.next_cursor))
    finally:
        await client.aclose()
