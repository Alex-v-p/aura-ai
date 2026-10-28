"""Adversarial tests for the production calendar transient/cursor boundary."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from aura_core.bootstrap.calendar_uow import _CalendarTransientStore
from aura_core.domains.integrations.calendar.internal import (
    CalendarConflictCursorState,
    CalendarConflictQuery,
)
from aura_core.domains.integrations.calendar.public import (
    CalendarAuthorizationAttempt,
    CalendarQuery,
    CalendarValidationError,
)

OWNER = ("https://identity.example", "owner-a")
FOREIGN = ("https://identity.example", "owner-b")
CALENDAR = UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
NOW = datetime(2026, 1, 1, tzinfo=UTC)


class FakeValkey:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.ttls[key] = ttl

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)


class _CrossClientPipeline:
    def __init__(self, backend: _CrossClientValkey, client_name: str) -> None:
        self._backend = backend
        self._client_name = client_name
        self._watched: dict[str, int] = {}
        self._commands: list[tuple[str, str, int, str | None]] = []
        self._in_multi = False

    async def watch(self, *keys: str) -> None:
        self._watched = {key: self._backend.versions.get(key, 0) for key in keys}
        self._backend.watchers += 1
        if self._backend.watchers >= 2:
            self._backend.watchers_ready.set()
        await self._backend.watchers_ready.wait()

    def multi(self) -> None:
        self._in_multi = True

    def get(self, key: str) -> object:
        if self._in_multi:
            self._commands.append(("get", key, 0, None))
            return None
        return _ImmediateValue(self._backend.values.get(key))

    def setex(self, key: str, ttl: int, value: str) -> None:
        self._commands.append(("setex", key, ttl, value))

    def delete(self, *keys: str) -> None:
        for key in keys:
            self._commands.append(("delete", key, 0, None))

    async def execute(self) -> list[object] | None:
        if self._client_name == "newest":
            await self._backend.first_commit.wait()
        if any(
            self._backend.versions.get(key, 0) != version
            for key, version in self._watched.items()
        ):
            return None
        results: list[object] = []
        for command, key, ttl, value in self._commands:
            if command == "get":
                results.append(self._backend.values.get(key))
            elif command == "setex":
                assert value is not None
                self._backend.values[key] = value
                self._backend.ttls[key] = ttl
                self._backend.versions[key] = self._backend.versions.get(key, 0) + 1
            else:
                self._backend.values.pop(key, None)
                self._backend.ttls.pop(key, None)
                self._backend.versions[key] = self._backend.versions.get(key, 0) + 1
        if self._client_name == "current":
            self._backend.first_commit.set()
        self._backend.pipeline_executes += 1
        return results

    async def reset(self) -> None:
        return None


class _ImmediateValue:
    def __init__(self, value: str | None) -> None:
        self.value = value

    def __await__(self) -> Any:
        async def resolve() -> str | None:
            return self.value

        return resolve().__await__()


class _CrossClient:
    def __init__(self, backend: _CrossClientValkey, name: str) -> None:
        self._backend = backend
        self._name = name

    def pipeline(self, *, transaction: bool) -> _CrossClientPipeline:
        assert transaction is True
        return _CrossClientPipeline(self._backend, self._name)

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self._backend.values[key] = value
        self._backend.ttls[key] = ttl
        self._backend.versions[key] = self._backend.versions.get(key, 0) + 1

    async def get(self, key: str) -> str | None:
        return self._backend.values.get(key)

    async def getdel(self, key: str) -> str | None:
        value = self._backend.values.pop(key, None)
        self._backend.ttls.pop(key, None)
        self._backend.versions[key] = self._backend.versions.get(key, 0) + 1
        return value


class _CrossClientValkey:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}
        self.versions: dict[str, int] = {}
        self.watchers = 0
        self.watchers_ready = asyncio.Event()
        self.first_commit = asyncio.Event()
        self.pipeline_executes = 0

    def client(self, name: str) -> _CrossClient:
        return _CrossClient(self, name)


def _attempt(handle: str, state: str) -> CalendarAuthorizationAttempt:
    expires_at = datetime.now(UTC) + timedelta(minutes=10)
    return CalendarAuthorizationAttempt(
        uuid4(), OWNER, "session-a", state, f"nonce-{handle}", handle,
        expires_at, "v" * 43,
    )


def _query(*, limit: int = 50) -> CalendarQuery:
    return CalendarQuery(
        NOW,
        NOW + timedelta(days=1),
        "UTC",
        (CALENDAR,),
        limit=limit,
    )


def _binding(
    *, conflict_limit: int = 10, query: CalendarQuery | None = None
) -> CalendarConflictQuery:
    return CalendarConflictQuery(query or _query(), conflict_limit)


def _state() -> CalendarConflictCursorState:
    return CalendarConflictCursorState(
        "provider-page-2",
        2,
        ("left|right|overlaps",),
    )


@pytest.mark.asyncio
async def test_valkey_clients_interleave_attempt_creation_without_mixed_supersession_markers(
) -> None:
    backend = _CrossClientValkey()
    current = _CalendarTransientStore(backend.client("current"), None, testing=False)
    newest = _CalendarTransientStore(backend.client("newest"), None, testing=False)
    first = _attempt("old-handle", "old-state")
    second = _attempt("new-handle", "new-state")

    results = await asyncio.gather(
        current.create(first), newest.create(second), return_exceptions=True
    )

    assert backend.pipeline_executes >= 2
    assert all(result is None for result in results)
    current_markers = [
        json.loads(value)
        for key, value in backend.values.items()
        if key.startswith("aura:calendar:current:")
    ]
    active_markers = [
        json.loads(value)
        for key, value in backend.values.items()
        if key.startswith("aura:calendar:active:")
    ]
    assert current_markers == [{"handle": "new-handle"}]
    assert active_markers == [{"handle": "new-handle", "attemptId": str(second.attempt_id)}]
    payloads = [json.loads(value) for value in backend.values.values()]
    assert all(payload.get("handle") != "old-handle" for payload in payloads)
    assert all(payload.get("state") != "old-state" for payload in payloads)
    assert any(payload.get("handle") == "new-handle" for payload in payloads)


@pytest.mark.asyncio
async def test_conflict_cursor_round_trip_reconstructs_typed_state_and_binds_owner_query_and_limit(
) -> None:
    client = FakeValkey()
    store = _CalendarTransientStore(client, None)
    binding = _binding()

    cursor = await store.put(OWNER, binding, _state())
    assert client.ttls[next(iter(client.ttls))] <= 600
    restored = await store.consume(OWNER, binding, cursor)

    assert isinstance(restored, CalendarConflictCursorState)
    assert restored == _state()

    for owner, wrong_binding in (
        (FOREIGN, binding),
        (OWNER, _binding(conflict_limit=11)),
        (OWNER, _binding(query=_query(limit=49))),
    ):
        fresh = await store.put(OWNER, binding, _state())
        with pytest.raises(RuntimeError, match="cursor is invalid"):
            await store.consume(owner, wrong_binding, fresh)
        assert await store.consume(OWNER, binding, fresh) == _state()


@pytest.mark.asyncio
async def test_conflict_cursor_is_one_shot_and_expiry_is_fail_closed() -> None:
    # The in-memory backend is an explicitly testing-only composition; using
    # it here keeps this test independent of Valkey while exercising the same
    # one-shot/expiry semantics.
    store = _CalendarTransientStore(None, None, testing=True)
    binding = _binding()
    cursor = await store.put(OWNER, binding, _state())

    assert await store.consume(OWNER, binding, cursor) == _state()
    with pytest.raises(RuntimeError, match="cursor is invalid"):
        await store.consume(OWNER, binding, cursor)

    expired = await store.put(OWNER, binding, _state())
    key = f"aura:calendar:cursor:{store._digest(expired)}"
    value = store._memory[key][1]
    store._memory[key] = (0.0, value)
    with pytest.raises(RuntimeError, match="cursor is invalid"):
        await store.consume(OWNER, binding, expired)


@pytest.mark.asyncio
async def test_conflict_cursor_malformed_typed_payload_is_rejected_without_leaking_value() -> None:
    client = FakeValkey()
    store = _CalendarTransientStore(client, None)
    cursor = await store.put(OWNER, _binding(), _state())
    key = f"aura:calendar:cursor:{store._digest(cursor)}"
    raw = json.loads(client.values[key])
    raw["value"] = "{malformed-json"
    client.values[key] = json.dumps(raw)

    with pytest.raises((RuntimeError, CalendarValidationError)) as raised:
        await store.consume(OWNER, _binding(), cursor)
    assert "provider-page-2" not in str(raised.value)


def _exception_graph_text(error: BaseException) -> str:
    seen: set[int] = set()
    values: list[str] = []

    def visit(current: BaseException | None) -> None:
        if current is None or id(current) in seen:
            return
        seen.add(id(current))
        values.extend((str(current), repr(current)))
        values.extend(str(item) for item in current.args)
        visit(current.__cause__)
        visit(current.__context__)

    visit(error)
    return "\n".join(values)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("raw", "secret"),
    (
        (b"\xffprivate-oauth-state", "private-oauth-state"),
        (b'{"value":"private-oauth-state"', "private-oauth-state"),
    ),
)
async def test_malformed_valkey_bytes_and_json_do_not_leak_oauth_state_in_exception_graph(
    raw: bytes, secret: str
) -> None:
    client = FakeValkey()
    store = _CalendarTransientStore(client, None)
    cursor = await store.put(OWNER, _binding(), _state())
    key = f"aura:calendar:cursor:{store._digest(cursor)}"
    client.values[key] = cast(Any, raw)

    with pytest.raises(Exception) as raised:
        await store.consume(OWNER, _binding(), cursor)

    assert secret not in _exception_graph_text(raised.value)


def test_production_composition_requires_valkey_for_non_testing_calendar_service() -> None:
    from aura_core.bootstrap import calendar_uow

    class Settings:
        google_calendar_enabled = False

    with pytest.raises((RuntimeError, ValueError)):
        calendar_uow.calendar_service(
            cast(Any, object()),
            settings=Settings(),
            testing=False,
            transient_client=None,
        )
