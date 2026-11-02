"""Behavioral acceptance tests for the calendar OAuth and credential lifecycle."""

# pyright: reportPrivateUsage=false, reportIncompatibleMethodOverride=false

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.bootstrap.calendar_uow import _CalendarTransientStore
from aura_core.domains.integrations.calendar.application import CalendarApplicationService
from aura_core.domains.integrations.calendar.internal import (
    CalendarDiscoveryPage,
    CalendarDiscoveryRecord,
    CalendarRevocationLease,
)
from aura_core.domains.integrations.calendar.public import (
    CalendarAuthorizationAttempt,
    CalendarAuthorizationRejected,
    CalendarCallbackResult,
    CalendarConnection,
    CalendarConnectionStatus,
    CalendarCredentialEnvelope,
    CalendarEventAvailability,
    CalendarEventPage,
    CalendarNotConnected,
    CalendarQuery,
    CalendarRequestContext,
    CalendarRevocationState,
    CalendarSelection,
    CalendarValidationError,
    CalendarVersionConflict,
    SelectableCalendar,
    SelectableCalendarPage,
    TimedCalendarEvent,
)
from aura_core.providers.calendars.google_calendar import (
    CalendarProviderError,
    ProviderFailureCategory,
)

OWNER = ("https://identity.example", "owner-a")
FOREIGN = ("https://identity.example", "owner-b")
SESSION = "session-a"
FOREIGN_SESSION = "session-b"
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeClock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


class FakeAttemptStore:
    def __init__(self) -> None:
        self.attempts: dict[str, CalendarAuthorizationAttempt] = {}
        self.callbacks: dict[tuple[tuple[str, str], str], CalendarCallbackResult] = {}
        self.consumed_handles: set[str] = set()
        self.consumed_states: set[str] = set()
        self.active_handles: dict[tuple[tuple[str, str], str], str] = {}

    async def create(self, attempt: CalendarAuthorizationAttempt) -> None:
        for handle, existing in self.attempts.items():
            if existing.owner == attempt.owner and existing.session_id == attempt.session_id:
                self.consumed_handles.add(handle)
        self.attempts[attempt.handle] = attempt
        self.active_handles[(attempt.owner, attempt.session_id)] = attempt.handle

    async def consume_navigation(
        self, owner: tuple[str, str], session_id: str, handle: str
    ) -> CalendarAuthorizationAttempt:
        attempt = self.attempts.get(handle)
        if (
            attempt is None
            or handle in self.consumed_handles
            or attempt.owner != owner
            or attempt.session_id != session_id
            or self.active_handles.get((owner, session_id)) != handle
        ):
            raise ValueError("authorization attempt rejected")
        self.consumed_handles.add(handle)
        return attempt

    async def finalize_navigation_attempt(
        self,
        owner: tuple[str, str],
        session_id: str,
        attempt_id: UUID,
        handle: str,
    ) -> None:
        attempt = self.attempts.get(handle)
        if (
            attempt is None
            or attempt.attempt_id != attempt_id
            or attempt.owner != owner
            or attempt.session_id != session_id
            or self.active_handles.get((owner, session_id)) != handle
        ):
            raise ValueError("authorization attempt superseded")

    async def consume_state(
        self, state: str, *, owner: tuple[str, str] | None = None
    ) -> CalendarAuthorizationAttempt:
        attempt = next(
            (item for item in self.attempts.values() if item.state == state), None
        )
        if (
            attempt is None
            or state in self.consumed_states
            or owner is not None
            and attempt.owner != owner
            or self.active_handles.get((attempt.owner, attempt.session_id)) != attempt.handle
        ):
            raise ValueError("callback state rejected")
        self.consumed_states.add(state)
        return attempt

    async def put_callback(self, result: CalendarCallbackResult) -> None:
        self.callbacks[(result.owner, result.session_id)] = result

    async def consume_callback(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarCallbackResult:
        key = (owner, session_id)
        result = self.callbacks.pop(key, None)
        if result is None or result.attempt_id != attempt_id:
            raise ValueError("callback result unavailable")
        return result

    async def peek_callback(
        self, owner: tuple[str, str], session_id: str
    ) -> CalendarCallbackResult:
        result = self.callbacks.get((owner, session_id))
        if result is None:
            raise ValueError("callback result unavailable")
        return result

    async def load_attempt(
        self, owner: tuple[str, str], session_id: str, attempt_id: UUID
    ) -> CalendarAuthorizationAttempt:
        attempt = next(
            (
                value
                for value in self.attempts.values()
                if value.owner == owner
                and value.session_id == session_id
                and value.attempt_id == attempt_id
            ),
            None,
        )
        if attempt is None:
            raise ValueError("authorization attempt unavailable")
        return attempt


class FakeCodec:
    def __init__(self) -> None:
        self.saved: list[CalendarCredentialEnvelope] = []
        self.opened: list[CalendarCredentialEnvelope] = []

    async def seal(
        self, owner: tuple[str, str], connection_id: UUID, credential: object
    ) -> CalendarCredentialEnvelope:
        del owner, connection_id
        envelope = CalendarCredentialEnvelope(b"ciphertext", b"n" * 12, "active-key", b"digest")
        self.saved.append(envelope)
        assert "access-token" not in repr(envelope)
        assert credential is not None
        return envelope

    async def open(
        self,
        owner: tuple[str, str],
        connection_id: UUID,
        envelope: CalendarCredentialEnvelope,
    ) -> object:
        del owner, connection_id
        self.opened.append(envelope)
        return {"access_token": "access-token", "refresh_token": "refresh-token"}


class FakeProvider:
    def __init__(self) -> None:
        self.revocations = 0
        self.exchange_calls: list[tuple[str, str]] = []
        self.fail_revocation = False
        self.fail_authentication = False
        self.discovery_items: tuple[CalendarDiscoveryRecord, ...] = ()
        self.event_page: CalendarEventPage | None = None

    async def authorization_url(self, attempt: CalendarAuthorizationAttempt) -> str:
        return (
            "https://accounts.google.com/o/oauth2/v2/auth?"
            f"state={attempt.state}&nonce={attempt.nonce}&scope=calendar.readonly"
        )

    async def exchange_code(
        self, *, code: str, redirect_uri: str, nonce: str | None = None,
        verifier: str | None = None, code_verifier: str | None = None,
        request: CalendarRequestContext | None = None,
    ) -> object:
        del nonce, verifier, code_verifier, request
        self.exchange_calls.append((code, redirect_uri))
        return {"access_token": "access-token", "refresh_token": "refresh-token"}

    async def discover(
        self,
        owner: tuple[str, str],
        *,
        cursor: str | None,
        limit: int,
        request: CalendarRequestContext,
    ) -> CalendarDiscoveryPage:
        del owner, cursor, limit, request
        return CalendarDiscoveryPage(self.discovery_items, None)

    async def retrieve(
        self, owner: tuple[str, str], query: Any, *, request: Any = None
    ) -> Any:
        del owner, query, request
        if self.fail_authentication:
            raise CalendarProviderError(
                ProviderFailureCategory.AUTHENTICATION,
                retryable=False,
            )
        return self.event_page

    async def revoke(self, owner: tuple[str, str], *, request: CalendarRequestContext) -> None:
        del owner, request
        self.revocations += 1
        if self.fail_revocation:
            raise RuntimeError("provider outage")


class FakeRepository:
    def __init__(self) -> None:
        self.connection_id_value = uuid4()
        self.connection = CalendarConnection(
            CalendarConnectionStatus.DISCONNECTED, False, 0, None, NOW
        )
        self.envelope: CalendarCredentialEnvelope | None = None
        self.destroyed = 0
        self.commits = 0
        self.revocation_attempts = 0
        self.selection: tuple[UUID, ...] = ()
        self.selection_version = 1
        self.idempotency: dict[tuple[str, str], object] = {}
        self.idempotency_fingerprints: dict[tuple[str, str], str] = {}
        self.discovered_calls = 0
        self.discovered_result = SelectableCalendarPage((), None, 1, NOW)
        self.revocation_deadline: datetime | None = None
        self.revocation_next_attempt_at: datetime | None = None
        self.revocation_uncertain = False

    async def get_connection(self, owner: tuple[str, str]) -> CalendarConnection:
        del owner
        return self.connection

    async def save_connection(
        self,
        owner: tuple[str, str],
        connection: CalendarConnection,
        *,
        expected_version: int | None,
    ) -> CalendarConnection:
        del owner, expected_version
        self.connection = connection
        return connection

    async def list_calendars(
        self, owner: tuple[str, str], *, cursor: str | None, limit: int
    ) -> SelectableCalendarPage:
        del owner, cursor, limit
        return SelectableCalendarPage((), None, self.selection_version, NOW)

    async def replace_selection(
        self, owner: tuple[str, str], calendar_ids: Sequence[UUID], *, expected_version: int
    ) -> CalendarSelection:
        del owner
        if expected_version != self.selection_version:
            raise ValueError("selection version conflict")
        self.selection = tuple(calendar_ids)
        self.selection_version += 1
        return CalendarSelection(self.selection, self.selection_version, NOW)

    async def claim_idempotency(
        self,
        owner: tuple[str, str],
        operation: str,
        key: str,
        fingerprint: str,
        response: object,
    ) -> tuple[bool, object]:
        del owner
        slot = (operation, key)
        if slot in self.idempotency:
            if self.idempotency_fingerprints[slot] != fingerprint:
                raise CalendarVersionConflict("idempotency key fingerprint conflict")
            return False, self.idempotency[slot]
        self.idempotency_fingerprints[slot] = fingerprint
        self.idempotency[slot] = response
        return True, response

    async def replace_selection_idempotent(
        self,
        owner: tuple[str, str],
        calendar_ids: Sequence[UUID],
        *,
        expected_version: int,
        operation: str,
        key: str,
        fingerprint: str,
    ) -> CalendarSelection:
        claimed, receipt = await self.claim_idempotency(
            owner,
            operation,
            key,
            fingerprint,
            {
                "calendar_ids": tuple(calendar_ids),
                "version": self.selection_version + 1,
                "updated_at": NOW,
            },
        )
        if not claimed:
            if isinstance(receipt, CalendarSelection):
                return receipt
            if isinstance(receipt, dict):
                return CalendarSelection(
                    tuple(cast(Sequence[UUID], receipt["calendar_ids"])),
                    int(cast(int, receipt["version"])),
                    NOW,
                )
            raise CalendarVersionConflict("selection receipt is invalid")
        result = await self.replace_selection(
            owner, calendar_ids, expected_version=expected_version
        )
        self.idempotency[(operation, key)] = result
        return result

    async def selected_ids(self, owner: tuple[str, str]) -> tuple[UUID, ...]:
        del owner
        return self.selection

    async def connection_id(self, owner: tuple[str, str]) -> UUID | None:
        del owner
        return self.connection_id_value

    async def commit_connected(
        self,
        owner: tuple[str, str],
        credential: CalendarCredentialEnvelope,
        *,
        connected_at: datetime,
        expected_version: int | None = None,
    ) -> CalendarConnection:
        del owner, expected_version
        self.envelope = credential
        self.commits += 1
        self.connection = CalendarConnection(
            CalendarConnectionStatus.CONNECTED, True, 0, connected_at, connected_at
        )
        return self.connection

    async def commit_connected_idempotent(
        self,
        owner: tuple[str, str],
        credential: CalendarCredentialEnvelope,
        *,
        connected_at: datetime,
        operation: str,
        key: str,
        fingerprint: str,
        attempt_id: UUID,
    ) -> CalendarConnection:
        claimed, receipt = await self.claim_idempotency(
            owner,
            operation,
            key,
            fingerprint,
            {"status": "connected", "attempt_id": str(attempt_id)},
        )
        if not claimed:
            receipt_map = cast(dict[str, object], receipt) if isinstance(receipt, dict) else {}
            if receipt_map.get("status") == "pending":
                claimed = True
            elif receipt_map.get("status") != "connected":
                raise CalendarAuthorizationRejected("connection receipt is invalid")
            else:
                return self.connection
        result = await self.commit_connected(owner, credential, connected_at=connected_at)
        self.idempotency[(operation, key)] = {"status": "connected", "attempt_id": str(attempt_id)}
        return result

    async def load_credential(
        self, owner: tuple[str, str]
    ) -> tuple[UUID, CalendarCredentialEnvelope] | None:
        del owner
        if self.envelope is None:
            return None
        return self.connection_id_value, self.envelope

    async def compare_and_swap_credential(
        self,
        owner: tuple[str, str],
        connection_id: UUID,
        expected: CalendarCredentialEnvelope,
        replacement: CalendarCredentialEnvelope,
    ) -> bool:
        del owner, connection_id
        if self.envelope != expected:
            return False
        self.envelope = replacement
        return True

    async def destroy_credential(self, owner: tuple[str, str], connection_id: UUID) -> None:
        del owner, connection_id
        self.envelope = None
        self.destroyed += 1

    async def mark_revocation_uncertain(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> CalendarConnection:
        del owner, now, deadline
        self.connection = CalendarConnection(
            CalendarConnectionStatus.REVOCATION_UNCERTAIN, False, 0, None, NOW
        )
        return self.connection

    async def prepare_revocation(
        self, owner: tuple[str, str], *, now: datetime, deadline: datetime
    ) -> tuple[CalendarConnection, object]:
        del owner
        if self.revocation_deadline is None:
            self.revocation_deadline = deadline
        if self.connection.status is CalendarConnectionStatus.DISCONNECTED:
            return self.connection, CalendarRevocationState(0, self.revocation_deadline, None)
        if self.revocation_attempts == 0:
            return self.connection, None
        state = CalendarRevocationState(
            self.revocation_attempts,
            self.revocation_deadline,
            self.revocation_next_attempt_at,
        )
        return self.connection, state

    async def finalize_revocation_attempt(
        self,
        owner: tuple[str, str],
        *,
        now: datetime,
        success: bool,
        idempotency_key: str | None = None,
        fingerprint: str | None = None,
    ) -> CalendarConnection:
        del owner
        self.revocation_attempts += 1
        if success or self.revocation_attempts >= 6 or (
            self.revocation_deadline is not None and now >= self.revocation_deadline
        ):
            self.connection = CalendarConnection(
                CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
            )
            self.envelope = None
            self.destroyed += 1
            if idempotency_key is not None and fingerprint is not None:
                self.idempotency[("calendar.connection.disconnect", idempotency_key)] = {
                    "status": "disconnected"
                }
            return self.connection
        self.connection = CalendarConnection(
            CalendarConnectionStatus.REVOCATION_UNCERTAIN, False, 0, None, now
        )
        self.revocation_next_attempt_at = now + timedelta(hours=6)
        if idempotency_key is not None and fingerprint is not None:
            self.idempotency[("calendar.connection.disconnect", idempotency_key)] = {
                "status": "revocation_uncertain"
            }
        return self.connection

    async def mark_reauthorization_required(
        self, owner: tuple[str, str], *, now: datetime, category: str
    ) -> CalendarConnection:
        del owner, now, category
        self.connection = CalendarConnection(
            CalendarConnectionStatus.REAUTHORIZATION_REQUIRED,
            False,
            0,
            None,
            NOW,
        )
        return self.connection

    async def record_revocation_attempt(
        self, owner: tuple[str, str], *, now: datetime, success: bool
    ) -> CalendarConnection:
        del owner, now
        self.revocation_attempts += 1
        if success or self.revocation_attempts >= 6:
            self.connection = CalendarConnection(
                CalendarConnectionStatus.DISCONNECTED, False, 0, None, NOW
            )
        return self.connection

    async def upsert_discovered(
        self, owner: tuple[str, str], calendars: Sequence[object], *, observed_at: datetime
    ) -> SelectableCalendarPage:
        del owner, calendars, observed_at
        self.discovered_calls += 1
        return self.discovered_result

    async def provider_calendar_id(self, owner: tuple[str, str], calendar_id: UUID) -> str:
        del owner, calendar_id
        return "provider-calendar-id"

    async def record_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str, response: Any
    ) -> None:
        self.idempotency_fingerprints[(operation, key)] = fingerprint
        self.idempotency[(operation, key)] = response

    async def find_idempotency(
        self, owner: tuple[str, str], operation: str, key: str, fingerprint: str
    ) -> Any | None:
        del owner
        if (operation, key) in self.idempotency and self.idempotency_fingerprints.get(
            (operation, key), fingerprint
        ) != fingerprint:
            raise CalendarVersionConflict("idempotency key fingerprint conflict")
        return self.idempotency.get((operation, key))

    async def record_audit(self, owner: tuple[str, str], **kwargs: object) -> None:
        del owner, kwargs


class FakeCursorStore:
    def __init__(self) -> None:
        self.values: dict[str, object] = {}
        self.put_calls = 0

    async def put(self, owner: tuple[str, str], query: CalendarQuery, value: object) -> str:
        del owner, query
        self.put_calls += 1
        self.values["opaque-cursor"] = value
        return "opaque-cursor"

    async def consume(self, owner: tuple[str, str], query: CalendarQuery, cursor: str) -> object:
        del owner, query
        return self.values[cursor]


def _service() -> tuple[
    Any,
    FakeRepository,
    FakeAttemptStore,
    FakeProvider,
    FakeCodec,
    FakeClock,
    FakeCursorStore,
]:
    repository = FakeRepository()
    attempts = FakeAttemptStore()
    provider = FakeProvider()
    codec = FakeCodec()
    clock = FakeClock()
    cursor_store = FakeCursorStore()
    service_factory = cast(Any, CalendarApplicationService)
    service = service_factory(
        repository,
        provider,
        attempt_store=attempts,
        credential_codec=codec,
        cursor_store=cursor_store,
        clock=clock,
    )
    return service, repository, attempts, provider, codec, clock, cursor_store


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            if name in mapping:
                return mapping[name]
        object_value = cast(object, value)
        if hasattr(object_value, name):
            return getattr(object_value, name)
    raise AssertionError(f"missing expected field: {names}")


@pytest.mark.asyncio
async def test_start_authorize_is_single_use_session_bound_and_superseding() -> None:
    service, _, attempts, provider, _, _, _ = _service()
    first = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="one")
    second = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="two")
    first_handle = str(
        _field(first, "authorization_path", "authorizationPath")
    ).rsplit("/", 1)[-1]
    second_handle = str(
        _field(second, "authorization_path", "authorizationPath")
    ).rsplit("/", 1)[-1]
    assert first_handle != second_handle

    with pytest.raises(CalendarAuthorizationRejected):
        await service.authorize_navigation(OWNER, first_handle, session_id=SESSION)
    with pytest.raises(CalendarAuthorizationRejected):
        await service.authorize_navigation(FOREIGN, second_handle, session_id=SESSION)
    with pytest.raises(CalendarAuthorizationRejected):
        await service.authorize_navigation(OWNER, second_handle, session_id=FOREIGN_SESSION)

    target = await service.authorize_navigation(OWNER, second_handle, session_id=SESSION)
    assert target.startswith("https://accounts.google.com/")
    assert "nonce=" in target
    assert "scope=calendar.readonly" in target
    assert len(provider.exchange_calls) == 0
    with pytest.raises(CalendarAuthorizationRejected):
        await service.authorize_navigation(OWNER, second_handle, session_id=SESSION)
    assert attempts.consumed_handles


@pytest.mark.asyncio
async def test_interleaved_navigation_supersession_rejects_old_and_keeps_new_current() -> None:
    service, _, _, _, codec, clock, _ = _service()
    clock.now = datetime.now(UTC)
    transient = _CalendarTransientStore(None, cast(Any, codec), testing=True)
    service._attempt_store = transient
    entered = asyncio.Event()
    release = asyncio.Event()

    class InterleavedProvider(FakeProvider):
        async def authorization_url(self, attempt: CalendarAuthorizationAttempt) -> str:
            if attempt.handle == old_handle:
                entered.set()
                await release.wait()
            return f"https://accounts.google.com/auth?state={attempt.state}"

    provider = InterleavedProvider()
    service._provider = provider
    old = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="old")
    old_handle = str(_field(old, "authorization_path", "authorizationPath")).rsplit("/", 1)[-1]
    old_navigation = asyncio.create_task(
        service.authorize_navigation(OWNER, old_handle, session_id=SESSION)
    )
    await entered.wait()

    newer = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="new")
    newer_handle = str(
        _field(newer, "authorization_path", "authorizationPath")
    ).rsplit("/", 1)[-1]
    release.set()

    with pytest.raises(CalendarAuthorizationRejected):
        await old_navigation
    current = await service.authorize_navigation(OWNER, newer_handle, session_id=SESSION)
    assert current.startswith("https://accounts.google.com/auth?")
    assert "state=" in current


@pytest.mark.asyncio
async def test_authorization_prefetch_race_allows_one_navigation_and_rejects_replay() -> None:
    service, _, _, _, _, _, _ = _service()
    start = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="one")
    handle = str(_field(start, "authorization_path", "authorizationPath")).rsplit("/", 1)[-1]

    outcomes: list[object] = list(
        cast(
            Any,
            await asyncio.gather(
                service.authorize_navigation(OWNER, handle, session_id=SESSION),
                service.authorize_navigation(OWNER, handle, session_id=SESSION),
                return_exceptions=True,
            ),
        )
    )

    assert sum(isinstance(item, str) for item in outcomes) == 1
    rejected: list[BaseException] = [
        item for item in outcomes if isinstance(item, BaseException)
    ]
    assert len(rejected) == 1
    assert isinstance(rejected[0], CalendarAuthorizationRejected)


@pytest.mark.asyncio
async def test_callback_validates_state_and_completion_consumes_exact_attempt_once() -> None:
    service, repository, attempts, provider, codec, _, _ = _service()
    start = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="one")
    handle = str(_field(start, "authorization_path", "authorizationPath")).rsplit("/", 1)[-1]
    attempt = next(iter(attempts.attempts.values()))
    await service.authorize_navigation(OWNER, handle, session_id=SESSION)

    await service.provider_callback(
        code="provider-code",
        state=attempt.state,
        error=None,
        callback_origin="https://aura.example",
    )
    with pytest.raises(CalendarAuthorizationRejected):
        await service.complete_connection(
            FOREIGN, session_id=SESSION, idempotency_key="foreign-owner"
        )
    with pytest.raises(CalendarAuthorizationRejected):
        await service.complete_connection(
            OWNER, session_id=FOREIGN_SESSION, idempotency_key="foreign-session"
        )
    result = await service.complete_connection(
        OWNER, session_id=SESSION, idempotency_key="complete"
    )

    assert _field(result, "status") == CalendarConnectionStatus.CONNECTED
    assert repository.commits == 1
    assert codec.saved
    assert isinstance(codec.saved[0].key_id, str)
    assert codec.saved[0].key_id == "active-key"
    assert provider.exchange_calls
    with pytest.raises(CalendarAuthorizationRejected):
        await service.complete_connection(OWNER, session_id=SESSION, idempotency_key="replay")


@pytest.mark.asyncio
async def test_disconnect_disables_reads_before_revoke_and_exhausts_uncertain_state() -> None:
    service, repository, _, provider, _, clock, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 0, NOW, NOW
    )
    repository.envelope = CalendarCredentialEnvelope(
        b"ciphertext", b"n" * 12, "active-key", b"digest"
    )
    provider.fail_revocation = True

    result = await service.disconnect_connection(OWNER, idempotency_key="disconnect")
    assert _field(result, "status") == CalendarConnectionStatus.REVOCATION_UNCERTAIN
    assert _field(result, "reads_enabled", "readsEnabled") is False
    assert repository.envelope is not None
    assert provider.revocations == 1

    clock.now += timedelta(hours=24, seconds=1)
    result = repository.connection
    for attempt in range(5):
        result = await service.disconnect_connection(
            OWNER, idempotency_key=f"retry-{attempt}"
        )
    assert provider.revocations <= 6
    assert repository.envelope is None
    assert repository.destroyed >= 1
    assert _field(result, "status") is CalendarConnectionStatus.REVOCATION_UNCERTAIN
    assert _field(result, "available_actions") == ()


@pytest.mark.asyncio
async def test_confirmed_disconnect_revokes_before_destroying_ciphertext() -> None:
    service, repository, _, provider, _, _, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 0, NOW, NOW
    )
    repository.envelope = CalendarCredentialEnvelope(
        b"ciphertext", b"n" * 12, "active-key", b"digest"
    )

    result = await service.disconnect_connection(OWNER, idempotency_key="disconnect")

    assert _field(result, "status") == CalendarConnectionStatus.DISCONNECTED
    assert provider.revocations == 1
    assert repository.envelope is None
    assert repository.destroyed == 1


@pytest.mark.asyncio
async def test_valkey_loss_fails_closed_for_callback_and_completion() -> None:
    service, _, attempts, _, _, _, _ = _service()
    start = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="one")
    handle = str(_field(start, "authorization_path", "authorizationPath")).rsplit("/", 1)[-1]
    attempt = next(iter(attempts.attempts.values()))
    attempts.attempts.clear()
    with pytest.raises(CalendarAuthorizationRejected):
        await service.authorize_navigation(OWNER, handle, session_id=SESSION)
    with pytest.raises(CalendarAuthorizationRejected):
        await service.provider_callback(
            code="code",
            state=attempt.state,
            error=None,
            callback_origin="https://aura.example",
        )


@pytest.mark.asyncio
async def test_callback_state_mismatch_is_rejected_without_provider_exchange() -> None:
    service, _, _, provider, _, _, _ = _service()

    await service.start_connection(OWNER, session_id=SESSION, idempotency_key="one")
    with pytest.raises(CalendarAuthorizationRejected):
        await service.provider_callback(
            code="provider-code",
            state="foreign-state",
            error=None,
            callback_origin="https://aura.example",
        )
    assert provider.exchange_calls == []


@pytest.mark.asyncio
async def test_stale_callback_after_newer_attempt_is_rejected_without_exchange() -> None:
    service, _, attempts, provider, _, _, _ = _service()
    await service.start_connection(OWNER, session_id=SESSION, idempotency_key="first")
    first = next(iter(attempts.attempts.values()))
    await service.start_connection(OWNER, session_id=SESSION, idempotency_key="second")

    with pytest.raises(CalendarAuthorizationRejected):
        await service.provider_callback(
            code="stale-code",
            state=first.state,
            error=None,
            callback_origin="https://aura.example",
        )
    assert provider.exchange_calls == []


@pytest.mark.asyncio
async def test_in_flight_callback_is_rejected_when_a_newer_attempt_supersedes_it() -> None:
    service, _, _, _, codec, clock, _ = _service()
    # The production transient store uses wall-clock TTLs even when the
    # application service is supplied a deterministic clock.
    clock.now = datetime.now(UTC)
    transient = _CalendarTransientStore(None, cast(Any, codec), testing=True)
    service._attempt_store = transient
    entered = asyncio.Event()
    release = asyncio.Event()

    class PausingProvider(FakeProvider):
        async def exchange_code(
            self,
            *,
            code: str,
            redirect_uri: str,
            nonce: str | None = None,
            verifier: str | None = None,
            code_verifier: str | None = None,
            request: CalendarRequestContext | None = None,
        ) -> object:
            del nonce, verifier, code_verifier, request
            entered.set()
            await release.wait()
            self.exchange_calls.append((code, redirect_uri))
            return {"access_token": "access-token", "refresh_token": "refresh-token"}

    provider = PausingProvider()
    service._provider = provider
    started = await service.start_connection(OWNER, session_id=SESSION, idempotency_key="first")
    handle = str(_field(started, "authorization_path", "authorizationPath")).rsplit("/", 1)[-1]
    raw = await transient._get(
        f"aura:calendar:attempt:{transient._digest(handle)}", consume=False
    )
    assert raw is not None
    first = transient._attempt(raw)
    await service.authorize_navigation(OWNER, first.handle, session_id=SESSION)
    callback = asyncio.create_task(
        service.provider_callback(
            code="in-flight",
            state=first.state,
            error=None,
            callback_origin="https://aura.example",
        )
    )
    await entered.wait()
    await service.start_connection(OWNER, session_id=SESSION, idempotency_key="newer")
    release.set()

    with pytest.raises(CalendarAuthorizationRejected):
        await callback
    assert provider.exchange_calls == [("in-flight", "https://aura.example")]


@pytest.mark.asyncio
async def test_callback_rejects_at_the_original_expiry_boundary() -> None:
    service, _, attempts, provider, _, clock, _ = _service()
    await service.start_connection(OWNER, session_id=SESSION, idempotency_key="expiry")
    attempt = next(iter(attempts.attempts.values()))
    clock.now = attempt.expires_at

    with pytest.raises(CalendarAuthorizationRejected):
        await service.provider_callback(
            code="expired-code",
            state=attempt.state,
            error=None,
            callback_origin="https://aura.example",
        )
    assert provider.exchange_calls == []


@pytest.mark.asyncio
async def test_duplicate_callback_and_exact_attempt_mismatch_are_rejected() -> None:
    service, _, attempts, provider, _, _, _ = _service()
    await service.start_connection(OWNER, session_id=SESSION, idempotency_key="duplicate")
    attempt = next(iter(attempts.attempts.values()))
    await service.provider_callback(
        code="provider-code",
        state=attempt.state,
        error=None,
        callback_origin="https://aura.example",
    )
    with pytest.raises(CalendarAuthorizationRejected):
        await service.provider_callback(
            code="provider-code",
            state=attempt.state,
            error=None,
            callback_origin="https://aura.example",
        )

    await attempts.put_callback(
        CalendarCallbackResult(
            uuid4(), OWNER, SESSION, {"access_token": "wrong-attempt"}, attempt.expires_at
        )
    )
    with pytest.raises(CalendarAuthorizationRejected):
        await service.complete_connection(OWNER, session_id=SESSION, idempotency_key="mismatch")
    assert provider.exchange_calls == [("provider-code", "https://aura.example")]


@pytest.mark.asyncio
async def test_provider_auth_expiry_disables_reads_and_requires_reauthorization() -> None:
    service, repository, _, provider, _, _, _ = _service()
    calendar_id = uuid4()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 1, NOW, NOW
    )
    repository.selection = (calendar_id,)
    provider.fail_authentication = True
    query = CalendarQuery(NOW, NOW + timedelta(days=1), "UTC", (calendar_id,))

    with pytest.raises(CalendarNotConnected):
        await service.events(OWNER, query)
    assert repository.connection.status is CalendarConnectionStatus.REAUTHORIZATION_REQUIRED
    assert repository.connection.reads_enabled is False


@pytest.mark.asyncio
async def test_old_key_load_lazily_reencrypts_with_string_active_key_using_cas() -> None:
    service, repository, _, _, codec, _, _ = _service()
    old = CalendarCredentialEnvelope(b"old-ciphertext", b"o" * 12, "retained-key", b"digest")
    repository.envelope = old

    credential = await service.load_provider_credential(OWNER)

    assert credential == {"access_token": "access-token", "refresh_token": "refresh-token"}
    assert codec.opened == [old]
    assert repository.envelope is not old
    assert repository.envelope is not None
    assert repository.envelope.key_id == "active-key"


@pytest.mark.asyncio
async def test_credential_reseal_cas_loss_reloads_current_active_envelope() -> None:
    old = CalendarCredentialEnvelope(b"old-ciphertext", b"o" * 12, "retained-key", b"old-owner")
    current = CalendarCredentialEnvelope(b"current-ciphertext", b"c" * 12, "active-key", b"owner")

    class CurrentCodec(FakeCodec):
        active_key_id = "active-key"

        async def open(
            self,
            owner: tuple[str, str],
            connection_id: UUID,
            envelope: CalendarCredentialEnvelope,
        ) -> object:
            assert owner == OWNER
            return {"generation": "old" if envelope is old else "current"}

        async def seal(
            self, owner: tuple[str, str], connection_id: UUID, credential: object
        ) -> CalendarCredentialEnvelope:
            del owner, connection_id, credential
            return CalendarCredentialEnvelope(b"resealed", b"r" * 12, "active-key", b"owner")

    class LosingCasRepository(FakeRepository):
        async def compare_and_swap_credential(
            self,
            owner: tuple[str, str],
            connection_id: UUID,
            expected: CalendarCredentialEnvelope,
            replacement: CalendarCredentialEnvelope,
        ) -> bool:
            del connection_id, expected, replacement
            assert owner == OWNER
            self.envelope = current
            return False

    repository = LosingCasRepository()
    repository.envelope = old
    codec = CurrentCodec()
    service = CalendarApplicationService(
        cast(Any, repository),
        FakeProvider(),
        credential_codec=cast(Any, codec),
    )

    credential = await service.load_provider_credential(OWNER)

    assert credential == {"generation": "current"}
    assert repository.envelope is current


@pytest.mark.asyncio
async def test_application_credential_reseal_cas_loss_rejects_retained_or_unbound_reload() -> None:
    old = CalendarCredentialEnvelope(b"old-ciphertext", b"o" * 12, "retained-key", b"old-owner")
    retained = CalendarCredentialEnvelope(
        b"retained-ciphertext", b"t" * 12, "retained-key", b"owner"
    )

    class CurrentCodec(FakeCodec):
        active_key_id = "active-key"

        async def open(
            self,
            owner: tuple[str, str],
            connection_id: UUID,
            envelope: CalendarCredentialEnvelope,
        ) -> object:
            assert owner == OWNER
            return {"generation": "old" if envelope is old else "retained"}

        async def seal(
            self, owner: tuple[str, str], connection_id: UUID, credential: object
        ) -> CalendarCredentialEnvelope:
            del owner, connection_id, credential
            return CalendarCredentialEnvelope(b"resealed", b"r" * 12, "active-key", b"owner")

    class LosingCasRepository(FakeRepository):
        async def compare_and_swap_credential(
            self,
            owner: tuple[str, str],
            connection_id: UUID,
            expected: CalendarCredentialEnvelope,
            replacement: CalendarCredentialEnvelope,
        ) -> bool:
            del owner, connection_id, expected, replacement
            self.envelope = retained
            return False

    repository = LosingCasRepository()
    repository.envelope = old
    service = CalendarApplicationService(
        cast(Any, repository),
        FakeProvider(),
        credential_codec=cast(Any, CurrentCodec()),
    )

    with pytest.raises(CalendarNotConnected):
        await service.load_provider_credential(OWNER)


@pytest.mark.asyncio
async def test_discovery_upserts_provider_metadata_and_keeps_new_calendars_unselected() -> None:
    service, repository, _, provider, _, _, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 0, NOW, NOW
    )
    aura_calendar = uuid4()
    repository.discovered_result = SelectableCalendarPage(
        (
            SelectableCalendar(aura_calendar, "Primary", "UTC", False, NOW),
        ),
        None,
        1,
        NOW,
    )
    provider.discovery_items = (
        CalendarDiscoveryRecord("google-secret-id", "Primary", "UTC"),
    )

    page = await service.discover(OWNER, limit=100)

    assert repository.discovered_calls == 1
    assert page.items[0].selected is False
    assert aura_calendar in {item.id for item in page.items}


@pytest.mark.asyncio
async def test_selection_idempotency_replays_without_advancing_version_twice() -> None:
    service, repository, _, _, _, _, _ = _service()
    calendar_id = uuid4()

    first = await service.replace_selection(
        OWNER, (calendar_id,), expected_version=1, idempotency_key="same"
    )
    second = await service.replace_selection(
        OWNER, (calendar_id,), expected_version=1, idempotency_key="same"
    )

    assert first.version == second.version
    assert repository.selection_version == 2


@pytest.mark.asyncio
async def test_selection_concurrent_same_key_different_fingerprint_has_one_winner() -> None:
    service, repository, _, _, _, _, _ = _service()
    first_id = uuid4()
    second_id = uuid4()
    outcomes = cast(list[object], await asyncio.gather(
        service.replace_selection(
            OWNER, (first_id,), expected_version=1, idempotency_key="race"
        ),
        service.replace_selection(
            OWNER, (second_id,), expected_version=1, idempotency_key="race"
        ),
        return_exceptions=True,
    ))

    assert sum(isinstance(item, CalendarSelection) for item in outcomes) == 1
    assert sum(isinstance(item, CalendarVersionConflict) for item in outcomes) == 1
    assert repository.selection in {(first_id,), (second_id,)}


@pytest.mark.asyncio
async def test_revocation_due_boundary_does_not_retry_after_fixed_deadline() -> None:
    service, repository, _, provider, _, clock, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 0, NOW, NOW
    )
    repository.envelope = CalendarCredentialEnvelope(
        b"ciphertext", b"n" * 12, "active-key", b"digest"
    )
    provider.fail_revocation = True

    await service.disconnect_connection(OWNER, idempotency_key="initial")
    calls_after_initial = provider.revocations
    assert repository.revocation_deadline == NOW + timedelta(hours=24)
    clock.now = NOW + timedelta(hours=24)

    result = await service.disconnect_connection(OWNER, idempotency_key="due")

    assert provider.revocations == calls_after_initial
    assert _field(result, "status") is CalendarConnectionStatus.REVOCATION_UNCERTAIN
    assert _field(result, "revocation_terminal") is True
    assert repository.envelope is None


@pytest.mark.asyncio
async def test_terminal_revocation_uncertainty_remains_non_connectable_after_deletion() -> None:
    service, repository, _, provider, _, _, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.REVOCATION_UNCERTAIN, False, 0, None, NOW
    )
    repository.revocation_attempts = 6
    # Ciphertext has already been atomically deleted when the terminal
    # uncertain state is observed; the lifecycle status remains a hard guard.
    repository.envelope = None

    result = await service.disconnect_connection(OWNER, idempotency_key="guard")

    assert provider.revocations == 0
    assert _field(result, "status") is CalendarConnectionStatus.REVOCATION_UNCERTAIN
    assert _field(result, "reads_enabled") is False
    assert repository.envelope is None


@pytest.mark.asyncio
async def test_terminal_uncertain_connection_cannot_start_a_new_authorization() -> None:
    service, repository, _, _, _, _, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.REVOCATION_UNCERTAIN, False, 0, None, NOW
    )

    with pytest.raises(CalendarValidationError):
        await service.start_connection(OWNER, session_id=SESSION, idempotency_key="reconnect")


@pytest.mark.asyncio
async def test_concurrent_revocation_reservations_are_leased_and_bounded_to_six() -> None:
    service, repository, _, provider, _, _, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 0, NOW, NOW
    )
    repository.envelope = CalendarCredentialEnvelope(
        b"ciphertext", b"n" * 12, "active-key", b"digest"
    )

    class LeasedRepository(FakeRepository):
        def __init__(self) -> None:
            super().__init__()
            self.connection = repository.connection
            self.envelope = repository.envelope
            self.lock = asyncio.Lock()
            self.reservations = 0

        async def prepare_revocation(
            self, owner: tuple[str, str], *, now: datetime, deadline: datetime
        ) -> tuple[CalendarConnection, CalendarRevocationState, CalendarRevocationLease]:
            del owner
            async with self.lock:
                self.reservations += 1
                assert self.reservations <= 6
                return (
                    self.connection,
                    CalendarRevocationState(0, deadline, None),
                    CalendarRevocationLease(self.reservations, now + timedelta(minutes=5)),
                )

        async def finalize_revocation_attempt(
            self,
            owner: tuple[str, str],
            *,
            now: datetime,
            success: bool,
            reservation: CalendarRevocationLease,
            idempotency_key: str | None = None,
            fingerprint: str | None = None,
        ) -> CalendarConnection:
            del owner, success, idempotency_key, fingerprint
            assert 1 <= reservation.attempt <= 6
            self.connection = CalendarConnection(
                CalendarConnectionStatus.DISCONNECTED, False, 0, None, now
            )
            return self.connection

    leased = LeasedRepository()
    service = CalendarApplicationService(
        cast(Any, leased),
        provider,
        attempt_store=None,
        credential_codec=None,
        cursor_store=None,
        clock=FakeClock(),
    )
    results = await asyncio.gather(
        *(
            service.disconnect_connection(OWNER, idempotency_key=f"revocation-{index}")
            for index in range(6)
        ),
        return_exceptions=True,
    )

    assert leased.reservations == 6
    assert provider.revocations == 6
    assert all(isinstance(result, CalendarConnection) for result in results)


@pytest.mark.asyncio
async def test_event_page_uses_opaque_owner_query_bound_cursor_and_no_event_rows() -> None:
    service, repository, _, provider, _, _, cursors = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 1, NOW, NOW
    )
    calendar_id = uuid4()
    repository.selection = (calendar_id,)
    query = CalendarQuery(
        NOW,
        NOW + timedelta(days=1),
        "UTC",
        (calendar_id,),
    )
    provider.event_page = CalendarEventPage(
        NOW,
        NOW + timedelta(days=1),
        "UTC",
        (),
        "provider-secret-page-token",
        False,
        (),
        NOW,
    )

    page = await service.events(OWNER, query)

    assert page.next_cursor == "opaque-cursor"
    assert "provider-secret" not in page.next_cursor
    assert cursors.put_calls == 1
    assert not hasattr(repository, "events")


@pytest.mark.asyncio
async def test_conflict_cursor_is_opaque_and_dedupes_cross_page_cross_calendar_pairs() -> None:
    service, repository, _, provider, _, _, _ = _service()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 2, NOW, NOW
    )
    calendar_a = uuid4()
    calendar_b = uuid4()
    repository.selection = (calendar_a, calendar_b)
    start = NOW + timedelta(hours=1)

    def event(event_id: str, calendar_id: UUID) -> TimedCalendarEvent:
        return TimedCalendarEvent(
            event_id,
            calendar_id,
            "bounded title",
            None,
            None,
            CalendarEventAvailability.BUSY,
            start,
            start + timedelta(hours=1),
            "UTC",
            "UTC",
            False,
        )

    class ConflictPagingProvider(FakeProvider):
        async def retrieve(
            self, owner: tuple[str, str], query: Any, *, request: Any = None
        ) -> CalendarEventPage:
            del owner, request
            if query.cursor is None:
                return CalendarEventPage(
                    NOW,
                    NOW + timedelta(days=1),
                    "UTC",
                    (event("shared", calendar_a), event("shared", calendar_b)),
                    "provider-conflict-page-2",
                    False,
                    (),
                    NOW,
                )
            assert query.cursor == "provider-conflict-page-2"
            return CalendarEventPage(
                NOW,
                NOW + timedelta(days=1),
                "UTC",
                (event("shared", calendar_a), event("shared", calendar_b)),
                None,
                False,
                (),
                NOW,
            )

    provider = ConflictPagingProvider()
    service = CalendarApplicationService(
        cast(Any, repository),
        provider,
        cursor_store=cast(Any, FakeCursorStore()),
        clock=FakeClock(),
    )
    query = CalendarQuery(
        NOW,
        NOW + timedelta(days=1),
        "UTC",
        (calendar_a, calendar_b),
        limit=2,
    )

    first = await service.conflicts(OWNER, query)

    assert len(first.items) == 1
    assert first.next_cursor is not None
    assert "provider-conflict-page-2" not in first.next_cursor

    second = await service.conflicts(
        OWNER, CalendarQuery(
            NOW,
            NOW + timedelta(days=1),
            "UTC",
            (calendar_a, calendar_b),
            limit=2,
            cursor=first.next_cursor,
        )
    )
    assert second.items == ()
    assert second.next_cursor is None


@pytest.mark.asyncio
async def test_conflict_observer_duration_excludes_slow_provider_retrieval() -> None:
    service, repository, _, _, _, _, _ = _service()
    observed_durations: list[float] = []

    class Observer:
        async def observe_conflict_evaluation(
            self, *, duration_ms: float, outcome: str
        ) -> None:
            assert outcome == "ok"
            observed_durations.append(duration_ms)

    service._conflict_observer = Observer()
    repository.connection = CalendarConnection(
        CalendarConnectionStatus.CONNECTED, True, 2, NOW, NOW
    )
    calendar_id = uuid4()
    repository.selection = (calendar_id,)
    start = NOW + timedelta(hours=1)
    event = TimedCalendarEvent(
        "slow-provider-event",
        calendar_id,
        "bounded title",
        None,
        None,
        CalendarEventAvailability.BUSY,
        start,
        start + timedelta(hours=1),
        "UTC",
        "UTC",
        False,
    )

    class SlowProvider(FakeProvider):
        async def retrieve(
            self, owner: tuple[str, str], query: Any, *, request: Any = None
        ) -> CalendarEventPage:
            del owner, query, request
            await asyncio.sleep(0.05)
            return CalendarEventPage(
                NOW,
                NOW + timedelta(days=1),
                "UTC",
                (event,),
                None,
                False,
                (),
                NOW,
            )

    service._provider = SlowProvider()
    await service.conflicts(
        OWNER,
        CalendarQuery(NOW, NOW + timedelta(days=1), "UTC", (calendar_id,)),
    )

    assert len(observed_durations) == 1
    assert observed_durations[0] < 40
