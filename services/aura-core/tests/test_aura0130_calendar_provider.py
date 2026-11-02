"""Adversarial Google Calendar adapter and credential-boundary tests."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import pickle
from datetime import UTC, datetime, timedelta
from time import monotonic
from uuid import uuid4

import httpx
import pytest
from aura_core.bootstrap.calendar_uow import _TelemetryAdapter
from aura_core.domains.integrations.calendar.public import CalendarRequestContext
from aura_core.platform.calendar_oauth import (
    GOOGLE_TOKEN_REVOCATION_ENDPOINT,
    CredentialKeyRing,
    KeyMaterial,
)
from aura_core.platform.telemetry import MetadataMetrics
from aura_core.providers.calendars.google_calendar import (
    GOOGLE_CALENDAR_SCOPE,
    GOOGLE_TOKEN_ENDPOINT,
    CalendarProviderError,
    CredentialMaterial,
    GoogleCalendarAdapter,
    GoogleCalendarCredential,
    ProviderFailureCategory,
    SealedCredential,
    open_credential,
    open_for_use,
    seal_credential,
)


def _credential() -> GoogleCalendarCredential:
    return GoogleCalendarCredential("access-secret", "refresh-secret")


def _expiring_credential() -> GoogleCalendarCredential:
    return GoogleCalendarCredential(
        "access-secret",
        "refresh-secret",
        datetime.now(UTC) + timedelta(hours=1),
    )


def _key_ring(active: str, *, old: bool = False) -> CredentialKeyRing:
    keys = {
        active: KeyMaterial(active, b"n" * 32, "active"),
    }
    if old:
        keys["retained"] = KeyMaterial("retained", b"o" * 32, "decrypt_only")
    return CredentialKeyRing(active, keys)


def test_credential_ciphertext_is_authenticated_owner_bound_and_key_rotatable() -> None:
    owner = str(uuid4())
    connection = str(uuid4())
    old = CredentialKeyRing(
        "retained", {"retained": KeyMaterial("retained", b"o" * 32, "active")}
    )
    rotated = _key_ring("active", old=True)
    envelope = seal_credential(owner, connection, b'{"access_token":"secret"}', old)

    assert envelope.key_id == "retained"
    assert (
        open_credential(owner, connection, envelope, rotated).as_bytes()
        == b'{"access_token":"secret"}'
    )
    with pytest.raises(ValueError, match="authentication"):
        open_credential("foreign-owner", connection, envelope, rotated)
    with pytest.raises(ValueError, match="authentication"):
        open_credential(owner, "foreign-connection", envelope, rotated)
    with pytest.raises(ValueError, match="authentication"):
        open_credential(owner, connection, envelope, _key_ring("active"))


def test_credential_material_cannot_be_serialized_or_leak_repr() -> None:
    material = CredentialMaterial(b"top-secret-token")
    assert "top-secret-token" not in repr(material)
    assert "top-secret-token" not in str(material)
    with pytest.raises(TypeError):
        pickle.dumps(material)
    assert material.as_bytes() == b"top-secret-token"


def test_credential_payload_parsing_rejects_malformed_and_bool_expiry() -> None:
    with pytest.raises(ValueError):
        GoogleCalendarCredential.from_material(CredentialMaterial(b"[]"))
    with pytest.raises(ValueError):
        GoogleCalendarCredential.from_material(
            CredentialMaterial(b'{"access_token":"a","expires_in":true}')
        )


@pytest.mark.asyncio
async def test_provider_uses_fixed_endpoint_scope_and_redirect_free_requests() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url == httpx.URL(GOOGLE_TOKEN_ENDPOINT):
            return httpx.Response(
                200,
                json={
                    "access_token": "access",
                    "refresh_token": "refresh",
                    "expires_in": 3600,
                    "scope": GOOGLE_CALENDAR_SCOPE,
                },
            )
        assert request.url.path == "/calendar/v3/users/me/calendarList"
        assert request.headers["authorization"] == "Bearer access"
        return httpx.Response(
            200,
            json={"items": [{"id": "primary", "summary": "Primary", "accessRole": "owner"}]},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        material = await adapter.exchange_code(
            code="authorization-code",
            redirect_uri="https://calendar.example/api/v1/integrations/calendar/callback",
            code_verifier="A" * 43,
        )
        credential = GoogleCalendarCredential.from_material(material)
        page = await adapter.list_calendars(credential)

    assert page.items[0].provider_id == "primary"
    assert seen[0].url == httpx.URL(GOOGLE_TOKEN_ENDPOINT)
    assert seen[1].url.scheme == "https"


@pytest.mark.asyncio
async def test_successful_204_revocation_emits_complete_provider_telemetry() -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    metrics = MetadataMetrics(capacity=64)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id",
            client_secret="secret",
            http_client=client,
            telemetry=_TelemetryAdapter(metrics),
        )
        await adapter.revoke_credential(_credential())

    assert len(requests) == 1
    assert requests[0].method == "POST"
    assert requests[0].url == httpx.URL(GOOGLE_TOKEN_REVOCATION_ENDPOINT)
    snapshots = metrics.snapshot()
    spans = [
        item
        for item in snapshots
        if item.kind == "span" and item.component_id == "aura.provider.google_calendar"
    ]
    assert len(spans) == 1
    dimensions = dict(spans[0].dimensions)
    assert dimensions["outcome"] == "ok"
    assert dimensions["provider"] == "google_calendar"
    assert dimensions["retry_class"] == "none"
    assert dimensions["rate_limit_state"] == "not_limited"
    assert {
        "google_calendar_provider_request_duration_ms",
        "google_calendar_provider_request_outcome",
        "google_calendar_provider_retry_count",
        "calendar_rate_limit_state",
    } <= {
        item.metric
        for item in snapshots
        if item.component_id == "aura.provider.google_calendar"
    }
    assert metrics.stats().rejected == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["https://www.googleapis.com/auth/calendar", "profile", None])
async def test_token_exchange_rejects_non_read_only_scope(scope: str | None) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": scope,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id", client_secret="secret", http_client=client
        )
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.exchange_code(
                code="authorization-code",
                redirect_uri="https://calendar.example/api/v1/integrations/calendar/callback",
                code_verifier="A" * 43,
            )
        assert raised.value.category is ProviderFailureCategory.AUTHENTICATION


@pytest.mark.asyncio
async def test_provider_retries_only_transient_reads_and_stops_at_bound() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        del request
        if calls < 3:
            return httpx.Response(503, text="provider-private-diagnostics")
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id", client_secret="secret", http_client=client, max_attempts=3
        )
        page = await adapter.list_calendars(_credential())
    assert page.items == ()
    assert calls == 3


@pytest.mark.asyncio
async def test_provider_rate_limit_exhaustion_is_closed_and_redacted() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(429, text="private provider response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id", client_secret="secret", http_client=client, max_attempts=2
        )
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.list_calendars(_credential())

    assert raised.value.category is ProviderFailureCategory.RATE_LIMIT
    assert "private" not in str(raised.value)
    assert "access-secret" not in repr(raised.value)


@pytest.mark.asyncio
async def test_provider_unavailable_exhaustion_has_exact_closed_category() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, text="upstream-secret")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id", client_secret="secret", http_client=client, max_attempts=1
        )
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.list_calendars(_credential())

    assert raised.value.category is ProviderFailureCategory.UNAVAILABLE
    assert raised.value.retryable is True
    assert "upstream-secret" not in repr(raised.value)


@pytest.mark.asyncio
async def test_provider_rejects_oversized_malformed_pages_and_tokens() -> None:
    cases: tuple[dict[str, object], ...] = (
        {"items": [{}]},
        {"items": [{"id": "x", "summary": "x", "accessRole": "reader"}] * 101},
        {"items": [], "nextPageToken": "x" * 1025},
    )
    for payload in cases:
        async def handler(request: httpx.Request, payload: object = payload) -> httpx.Response:
            del request
            return httpx.Response(200, json=payload)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            adapter = GoogleCalendarAdapter(
                client_id="id", client_secret="secret", http_client=client
            )
            with pytest.raises(CalendarProviderError) as raised:
                await adapter.list_calendars(_credential())
        assert raised.value.category is ProviderFailureCategory.MALFORMED


@pytest.mark.asyncio
async def test_provider_honors_cancellation_before_dispatch() -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        del request
        return httpx.Response(200, json={"items": []})

    canceled = asyncio.Event()
    canceled.set()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.list_calendars(_credential(), cancel_event=canceled)
    assert raised.value.category is ProviderFailureCategory.CANCELED
    assert called is False


@pytest.mark.asyncio
async def test_provider_event_query_is_bounded_and_provider_ids_are_path_encoded() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "event-1",
                        "summary": "untrusted",
                        "start": {"dateTime": "2026-01-01T09:00:00Z"},
                        "end": {"dateTime": "2026-01-01T10:00:00Z"},
                    }
                ]
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        page = await adapter.list_events(
            _credential(),
            "private/calendar id",
            time_min=datetime(2026, 1, 1, tzinfo=UTC),
            time_max=datetime(2026, 1, 2, tzinfo=UTC),
            page_token="opaque-page-token",
        )
    assert page.items[0].provider_id == "event-1"
    assert "/calendars/private%2Fcalendar%20id/events" in str(seen[0].url)
    assert "access-secret" not in repr(page)


class _OversizedStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"{" + b"x" * (2 * 1024 * 1024 + 1)


@pytest.mark.asyncio
async def test_provider_rejects_streaming_response_that_exceeds_body_limit() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, stream=_OversizedStream())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.list_calendars(_credential())
    assert raised.value.category is ProviderFailureCategory.MALFORMED


@pytest.mark.asyncio
async def test_provider_deadline_covers_a_delayed_multi_calendar_read() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.list_calendars(
                _credential(),
                deadline=asyncio.get_running_loop().time() + 0.001,
            )
    assert raised.value.category is ProviderFailureCategory.DEADLINE


@pytest.mark.asyncio
async def test_provider_cancellation_after_dispatch_is_a_closed_outcome() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    canceled = asyncio.Event()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        started.set()
        await release.wait()
        return httpx.Response(200, json={"items": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        task = asyncio.create_task(
            adapter.list_calendars(_credential(), cancel_event=canceled)
        )
        await started.wait()
        canceled.set()
        release.set()
        with pytest.raises(CalendarProviderError) as raised:
            await task
    assert raised.value.category is ProviderFailureCategory.CANCELED


@pytest.mark.asyncio
async def test_in_flight_cancellation_closes_the_provider_stream_promptly() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    canceled = asyncio.Event()

    class ClosingStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            started.set()
            await release.wait()
            yield b"{}"

        async def aclose(self) -> None:
            self.closed = True

    stream = ClosingStream()

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        task = asyncio.create_task(adapter.list_calendars(_credential(), cancel_event=canceled))
        await started.wait()
        canceled.set()
        release.set()
        with pytest.raises(CalendarProviderError) as raised:
            await task

    assert raised.value.category is ProviderFailureCategory.CANCELED
    assert stream.closed is True


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["discover", "exchange", "revoke"])
async def test_discovery_exchange_and_revocation_honor_disconnect_before_dispatch(
    operation: str,
) -> None:
    called = False
    canceled = asyncio.Event()
    canceled.set()

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        del request
        return httpx.Response(200, json={"items": []})

    async def loader(owner: tuple[str, str]) -> GoogleCalendarCredential:
        del owner
        return _expiring_credential()

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id",
            client_secret="secret",
            http_client=client,
            credential_loader=loader,
        )
        with pytest.raises(CalendarProviderError) as raised:
            if operation == "discover":
                await adapter.discover(
                    ("issuer", "owner"), cursor=None, limit=1, cancel_event=canceled
                )
            elif operation == "exchange":
                await adapter.exchange_code(
                    code="authorization-code",
                    redirect_uri="https://calendar.example/api/v1/integrations/calendar/callback",
                    code_verifier="A" * 43,
                    cancel_event=canceled,
                )
            else:
                await adapter.revoke_credential(_credential(), cancel_event=canceled)

    assert raised.value.category is ProviderFailureCategory.CANCELED
    assert called is False


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["discover", "exchange", "revoke"])
async def test_discovery_exchange_and_revocation_honor_expired_deadline_before_dispatch(
    operation: str,
) -> None:
    called = False

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        del request
        return httpx.Response(200, json={"items": []})

    async def loader(owner: tuple[str, str]) -> GoogleCalendarCredential:
        del owner
        return _expiring_credential()

    request = CalendarRequestContext(monotonic() - 1.0, None)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id",
            client_secret="secret",
            http_client=client,
            credential_loader=loader,
        )
        with pytest.raises(CalendarProviderError) as raised:
            if operation == "discover":
                await adapter.discover(
                    ("issuer", "owner"), cursor=None, limit=1, request=request
                )
            elif operation == "exchange":
                await adapter.exchange_code(
                    code="authorization-code",
                    redirect_uri="https://calendar.example/api/v1/integrations/calendar/callback",
                    code_verifier="A" * 43,
                    request=request,
                )
            else:
                await adapter.revoke_credential(_credential(), request=request)

    assert raised.value.category is ProviderFailureCategory.DEADLINE
    assert called is False


@pytest.mark.asyncio
async def test_token_exchange_requires_a_pkce_verifier_and_rejects_mismatch() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "access_token": "access",
                "refresh_token": "refresh",
                "expires_in": 3600,
                "scope": GOOGLE_CALENDAR_SCOPE,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(client_id="id", client_secret="secret", http_client=client)
        with pytest.raises(CalendarProviderError):
            await adapter.exchange_code(
                code="authorization-code",
                redirect_uri="https://calendar.example/api/v1/integrations/calendar/callback",
                code_verifier="wrong-verifier",  # type: ignore[call-arg]
            )


@pytest.mark.asyncio
async def test_absolute_expiry_without_refresh_token_disables_provider_reads() -> None:
    async def expired(owner: tuple[str, str]) -> GoogleCalendarCredential:
        del owner
        return GoogleCalendarCredential("access", expires_at=datetime.now(UTC))

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        pytest.fail("expired credentials must fail before provider dispatch")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="id",
            client_secret="secret",
            http_client=client,
            credential_loader=expired,
        )
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.discover(("issuer", "subject"), cursor=None, limit=100)
    assert raised.value.category is ProviderFailureCategory.AUTHENTICATION


@pytest.mark.asyncio
async def test_retained_key_rotation_reloads_newer_ciphertext_after_cas_conflict() -> None:
    owner = str(uuid4())
    connection = str(uuid4())
    ring = _key_ring("active", old=True)
    original = seal_credential(owner, connection, b"old-material", ring)
    newer = seal_credential(owner, connection, b"new-material", ring)

    class ConflictingRewrite:
        async def compare_and_swap(
            self,
            owner_id: str,
            connection_id: str,
            expected: SealedCredential,
            replacement: SealedCredential,
        ) -> bool:
            del owner_id, connection_id, expected, replacement
            return False

        async def reload(self, owner_id: str, connection_id: str) -> SealedCredential:
            del owner_id, connection_id
            return newer

    material = await open_for_use(
        owner,
        connection,
        original,
        ring,
        rewrite=ConflictingRewrite(),
    )

    assert material.as_bytes() == b"new-material"


@pytest.mark.asyncio
async def test_retained_key_rotation_fails_when_cas_reload_is_still_retained() -> None:
    owner = str(uuid4())
    connection = str(uuid4())
    ring = _key_ring("active", old=True)
    original = seal_credential(owner, connection, b"old-material", ring)

    class LostRace:
        async def compare_and_swap(
            self,
            owner_id: str,
            connection_id: str,
            expected: SealedCredential,
            replacement: SealedCredential,
        ) -> bool:
            del owner_id, connection_id, expected, replacement
            return False

        async def reload(self, owner_id: str, connection_id: str) -> SealedCredential:
            del owner_id, connection_id
            return original

    with pytest.raises(ValueError, match="credential rotation conflict"):
        await open_for_use(owner, connection, original, ring, rewrite=LostRace())


@pytest.mark.asyncio
async def test_provider_exception_graph_never_retains_malformed_provider_or_credential_secrets(
) -> None:
    secret = "provider-credential-secret"

    async def handler(request: httpx.Request) -> httpx.Response:
        del request
        raise httpx.ReadError(secret)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = GoogleCalendarAdapter(
            client_id="client-secret", client_secret=secret, http_client=client
        )
        with pytest.raises(CalendarProviderError) as raised:
            await adapter.list_calendars(_credential())

    pending: list[BaseException | None] = [raised.value]
    seen: set[int] = set()
    rendered: list[str] = []
    while pending:
        current = pending.pop()
        if current is None or id(current) in seen:
            continue
        seen.add(id(current))
        rendered.extend((str(current), repr(current)))
        pending.extend((current.__cause__, current.__context__))
    assert all(secret not in value for value in rendered)
