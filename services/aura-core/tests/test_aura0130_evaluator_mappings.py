"""Executable, metadata-only synthetic evaluator mappings for AURA-0130."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

import httpx
import pytest
from aura_core.domains.integrations.calendar.application import CalendarApplicationService
from aura_core.domains.integrations.calendar.public import (
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarEventAvailability,
    CalendarRevocationState,
    CalendarValidationError,
    TimedCalendarEvent,
    evaluate_conflicts,
    normalize_timed_event,
)
from aura_core.platform.calendar_oauth import CredentialKeyRing, KeyMaterial
from aura_core.providers.calendars.google_calendar import (
    GOOGLE_CALENDAR_SCOPE,
    CalendarProviderError,
    GoogleCalendarAdapter,
    GoogleCalendarCredential,
    ProviderFailureCategory,
    open_credential,
    seal_credential,
)

REQUIRED_EVALUATORS = frozenset(
    {
        "calendar.provider-boundary.v1",
        "calendar.normalization.v1",
        "calendar.conflict.v1",
        "calendar.credential-lifecycle.v1",
    }
)


@dataclass(frozen=True, slots=True)
class SyntheticEvaluator:
    version: str
    positive: Callable[[], bool]
    negative: Callable[[], bool]


class _LifecycleClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def __call__(self) -> datetime:
        return self.now


class _LifecycleProvider:
    def __init__(self, *, fail_revocation: bool) -> None:
        self.fail_revocation = fail_revocation
        self.revocations = 0

    async def revoke(self, owner: tuple[str, str], *, request: object) -> None:
        del owner, request
        self.revocations += 1
        if self.fail_revocation:
            raise RuntimeError("synthetic provider outage")


class _LifecycleRepository:
    def __init__(self, envelope: object) -> None:
        self.connection = CalendarConnection(
            CalendarConnectionStatus.CONNECTED, True, 1, datetime.now(UTC), datetime.now(UTC)
        )
        self.envelope = envelope
        self.attempts = 0
        self.deadline: datetime | None = None
        self.next_attempt_at: datetime | None = None
        self.receipts: dict[tuple[str, str], object] = {}

    async def find_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str
    ) -> object | None:
        del owner, fingerprint
        return self.receipts.get((operation, key))

    async def claim_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> tuple[bool, object]:
        del owner, fingerprint
        slot = (operation, key)
        if slot in self.receipts:
            return False, self.receipts[slot]
        self.receipts[slot] = response
        return True, response

    async def prepare_revocation(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> tuple[CalendarConnection, CalendarRevocationState | None]:
        del owner
        if self.deadline is None:
            self.deadline = deadline
        if self.attempts == 0:
            return self.connection, None
        return self.connection, CalendarRevocationState(
            self.attempts, self.deadline, self.next_attempt_at
        )

    async def finalize_revocation_attempt(
        self,
        owner: tuple[str, str],
        *,
        now: datetime,
        success: bool,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> CalendarConnection:
        del owner, fingerprint
        self.attempts += 1
        terminal = success or self.attempts >= 6
        if terminal:
            self.envelope = None
            self.connection = CalendarConnection(
                CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
            )
            status = "disconnected"
        else:
            self.connection = CalendarConnection(
                CalendarConnectionStatus.REVOCATION_UNCERTAIN, False, 0, None, now
            )
            self.next_attempt_at = now + timedelta(hours=6)
            status = "revocation_uncertain"
        if idempotency_key is not None:
            self.receipts[("calendar.connection.disconnect", idempotency_key)] = {
                "status": status
            }
        return self.connection

    async def destroy_credential(self, owner: tuple[str, str], connection_id: object) -> None:
        del owner, connection_id
        self.envelope = None

    async def record_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> None:
        del owner, fingerprint
        self.receipts[(operation, key)] = response

    async def connection_id(self, owner: tuple[str, str]) -> str:
        del owner
        return "connection-a"


def _provider_positive() -> bool:
    async def run() -> bool:
        async def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/token"):
                return httpx.Response(
                    200,
                    json={
                        "access_token": "bounded-access",
                        "refresh_token": "bounded-refresh",
                        "expires_in": 3600,
                        "scope": GOOGLE_CALENDAR_SCOPE,
                    },
                )
            return httpx.Response(
                200,
                json={
                    "items": [{"id": "primary", "summary": "Primary", "accessRole": "owner"}]
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleCalendarAdapter(
                client_id="id", client_secret="secret", http_client=client
            )
            material = await adapter.exchange_code(
                code="authorization-code",
                redirect_uri="https://calendar.example/api/v1/integrations/calendar/callback",
                code_verifier="A" * 43,
            )
            credential = GoogleCalendarCredential.from_material(material)
            page = await adapter.list_calendars(credential)
        return page.items[0].provider_id == "primary"

    return asyncio.run(run())


def _provider_negative() -> bool:
    async def run() -> bool:
        calls = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            del request
            return httpx.Response(503, text="provider-private-diagnostics")

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleCalendarAdapter(
                client_id="id", client_secret="secret", http_client=client, max_attempts=2
            )
            try:
                await adapter.list_calendars(
                    GoogleCalendarCredential(
                        "bounded-access",
                        "bounded-refresh",
                        datetime.now(UTC) + timedelta(minutes=10),
                    )
                )
            except CalendarProviderError as error:
                return (
                    error.category is ProviderFailureCategory.UNAVAILABLE
                    and cast(Any, calls) == 2
                )
        return False

    return asyncio.run(run())


def _normalization_positive() -> bool:
    event = normalize_timed_event(
        event_id="fixture",
        calendar_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        title="fixture",
        description=None,
        location=None,
        availability=CalendarEventAvailability.BUSY,
        start_instant=datetime(2026, 1, 1, 9, tzinfo=UTC),
        end_instant=datetime(2026, 1, 1, 10, tzinfo=UTC),
        event_time_zone="UTC",
        display_time_zone="UTC",
    )
    return event.start_instant.tzinfo is UTC


def _normalization_negative() -> bool:
    try:
        normalize_timed_event(
            event_id="fixture",
            calendar_id=UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            title="fixture",
            description=None,
            location=None,
            availability=CalendarEventAvailability.BUSY,
            start_instant=datetime(2026, 1, 1, 10, tzinfo=UTC),
            end_instant=datetime(2026, 1, 1, 9, tzinfo=UTC),
            event_time_zone="UTC",
            display_time_zone="UTC",
        )
    except CalendarValidationError:
        return True
    return False


def _conflict_positive() -> bool:
    start = datetime(2026, 1, 1, 9, tzinfo=UTC)
    events = tuple(
        TimedCalendarEvent(
            event_id,
            UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
            "fixture",
            None,
            None,
            CalendarEventAvailability.BUSY,
            start + timedelta(minutes=offset),
            start + timedelta(hours=1, minutes=offset),
            "UTC",
            "UTC",
            False,
        )
        for event_id, offset in (("left", 0), ("right", 30))
    )
    return len(evaluate_conflicts(events)) == 1


def _conflict_negative() -> bool:
    start = datetime(2026, 1, 1, 9, tzinfo=UTC)
    event = TimedCalendarEvent(
        "free",
        UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
        "fixture",
        None,
        None,
        CalendarEventAvailability.FREE,
        start,
        start + timedelta(hours=1),
        "UTC",
        "UTC",
        False,
    )
    return evaluate_conflicts((event,)) == ()


def _credential_lifecycle_positive() -> bool:
    old_ring = CredentialKeyRing(
        "retained", {"retained": KeyMaterial("retained", b"o" * 32, "active")}
    )
    rotated_ring = CredentialKeyRing(
        "active",
        {
            "active": KeyMaterial("active", b"n" * 32, "active"),
            "retained": KeyMaterial("retained", b"o" * 32, "decrypt_only"),
        },
    )
    owner = "https://identity.example:owner"
    connection = "connection-a"
    envelope = seal_credential(owner, connection, b"credential", old_ring)
    opened = open_credential(owner, connection, envelope, rotated_ring)
    clock = _LifecycleClock()
    repository = _LifecycleRepository(envelope)
    provider = _LifecycleProvider(fail_revocation=False)
    service = CalendarApplicationService(
        cast(Any, repository), cast(Any, provider), clock=clock
    )

    async def disconnect() -> bool:
        result = await service.disconnect_connection(
            ("https://identity.example", "owner-a"), idempotency_key="disconnect"
        )
        return (
            result.status is CalendarConnectionStatus.DISCONNECTED
            and repository.envelope is None
            and provider.revocations == 1
        )

    return (
        opened.as_bytes() == b"credential"
        and envelope.key_id == "retained"
        and CalendarConnection(
            CalendarConnectionStatus.CONNECTED, True, 1, None, datetime.now(UTC)
        ).available_actions
        == ("disconnect",)
        and asyncio.run(disconnect())
    )


def _credential_lifecycle_negative() -> bool:
    async def run() -> bool:
        called = False
        auth_rejected = False

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            del request
            return httpx.Response(200, json={"items": []})

        async def loader(owner: tuple[str, str]) -> GoogleCalendarCredential:
            del owner
            return GoogleCalendarCredential(
                "expired-access",
                "expired-refresh",
                datetime.now(UTC) - timedelta(seconds=1),
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleCalendarAdapter(
                client_id="id",
                client_secret="secret",
                http_client=client,
                credential_loader=loader,
            )
            try:
                await adapter.discover(("issuer", "owner"), cursor=None, limit=1)
            except CalendarProviderError as error:
                auth_rejected = error.category is ProviderFailureCategory.AUTHENTICATION
        if called:
            return False
        if not auth_rejected:
            return False

        old_ring = CredentialKeyRing(
            "retained", {"retained": KeyMaterial("retained", b"o" * 32, "active")}
        )
        envelope = seal_credential("owner", "connection", b"credential", old_ring)
        clock = _LifecycleClock()
        repository = _LifecycleRepository(envelope)
        provider = _LifecycleProvider(fail_revocation=True)
        service = CalendarApplicationService(
            cast(Any, repository), cast(Any, provider), clock=clock
        )
        first = await service.disconnect_connection(
            ("https://identity.example", "owner-a"), idempotency_key="initial"
        )
        clock.now += timedelta(hours=24, seconds=1)
        terminal = await service.disconnect_connection(
            ("https://identity.example", "owner-a"), idempotency_key="terminal"
        )
        return (
            first.status is CalendarConnectionStatus.REVOCATION_UNCERTAIN
            and not first.reads_enabled
            and terminal.status is CalendarConnectionStatus.REVOCATION_UNCERTAIN
            and terminal.revocation_terminal
            and terminal.available_actions == ()
            and repository.envelope is None
            and provider.revocations <= 6
        )

    return asyncio.run(run())


SYNTHETIC_EVALUATOR_MAPPINGS: Mapping[str, SyntheticEvaluator] = {
    "calendar.provider-boundary.v1": SyntheticEvaluator(
        "1.0.0", _provider_positive, _provider_negative
    ),
    "calendar.normalization.v1": SyntheticEvaluator(
        "1.0.0", _normalization_positive, _normalization_negative
    ),
    "calendar.conflict.v1": SyntheticEvaluator(
        "1.0.0", _conflict_positive, _conflict_negative
    ),
    "calendar.credential-lifecycle.v1": SyntheticEvaluator(
        "1.0.0", _credential_lifecycle_positive, _credential_lifecycle_negative
    ),
}


def test_every_required_calendar_evaluator_has_an_executable_mapping() -> None:
    assert set(SYNTHETIC_EVALUATOR_MAPPINGS) == REQUIRED_EVALUATORS
    assert all(evaluator.version == "1.0.0" for evaluator in SYNTHETIC_EVALUATOR_MAPPINGS.values())
    assert all(evaluator.positive() is True for evaluator in SYNTHETIC_EVALUATOR_MAPPINGS.values())
    assert all(evaluator.negative() is True for evaluator in SYNTHETIC_EVALUATOR_MAPPINGS.values())


@pytest.mark.parametrize("evaluator_id", sorted(REQUIRED_EVALUATORS))
def test_synthetic_calendar_evaluator_is_content_free_and_deterministic(evaluator_id: str) -> None:
    evaluator = SYNTHETIC_EVALUATOR_MAPPINGS[evaluator_id]
    assert evaluator.positive() == evaluator.positive()
    assert evaluator.negative() == evaluator.negative()
