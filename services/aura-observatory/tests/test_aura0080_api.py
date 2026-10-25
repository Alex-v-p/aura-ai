"""AURA-0080 owner-only REST/SSE boundary checks."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any, cast

import httpx
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.query import decode_cursor, encode_cursor
from aura_observatory.settings import Settings
from fastapi import HTTPException
from fastapi.routing import APIRoute
from starlette.requests import Request
from starlette.responses import StreamingResponse
from test_aura0080_query_retention import BASE_TIME, make_payload, make_run_id

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"
CORE_CANONICAL_ID = "01234567-89ab-0cde-7123-0123456789ab"


async def _client() -> tuple[httpx.AsyncClient, MemoryObservatoryStore, Any]:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    store = MemoryObservatoryStore()
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000101",
            observed_at=BASE_TIME,
            run_id=make_run_id("api-run"),
        )
    )
    app = create_app(settings, auth=auth, query_store=store, testing=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    )
    return client, store, session


@pytest.mark.asyncio
async def test_query_resources_require_owner_session_and_return_metadata_only() -> None:
    settings = Settings(owner_subject=OWNER)
    app = create_app(settings, query_store=MemoryObservatoryStore(), testing=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=settings.public_origin) as client:
        assert (await client.get("/api/v1/overview")).status_code == 401

    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, "not-owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        assert (await client.get("/api/v1/overview")).status_code == 403

    client, _, _ = await _client()
    async with client:
        overview = await client.get("/api/v1/overview")
        assert overview.status_code == 200
        assert overview.json()["runCount"] == 1
        assert "owner-subject" not in overview.text
        assert not any(
            secret in overview.text for secret in ("prompt", "response", "credential", "bearer")
        )


@pytest.mark.asyncio
async def test_run_search_cursor_and_trace_resources_are_owner_scoped() -> None:
    client, store, _ = await _client()
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000102",
            observed_at=BASE_TIME,
            run_id=make_run_id("api-run-2"),
            component="aura.runtime.tool",
        )
    )
    async with client:
        page = await client.get("/api/v1/runs", params={"limit": 1})
        assert page.status_code == 200
        assert len(page.json()["items"]) == 1
        cursor = page.json()["nextCursor"]
        assert isinstance(cursor, str) and cursor
        next_page = await client.get("/api/v1/runs", params={"cursor": cursor, "limit": 1})
        assert next_page.status_code == 200
        assert len(next_page.json()["items"]) == 1

        filtered = await client.get("/api/v1/runs", params={"component": "aura.runtime.tool"})
        assert [item["deploymentId"] for item in filtered.json()["items"]] == ["deploy-a"]

        run_id = make_run_id("api-run")
        detail = await client.get(f"/api/v1/runs/{run_id}")
        assert detail.status_code == 200
        assert detail.json()["runId"] == run_id
        assert detail.json()["status"] == "succeeded"
        assert detail.json()["observations"][0]["outcome"] == "succeeded"
        missing = await client.get("/api/v1/runs/00000000-0000-4000-8000-000000000999")
        assert missing.status_code == 404

        traces = await client.get("/api/v1/traces/0123456789abcdef0123456789abcdef")
        assert traces.status_code == 200
        assert "prompt" not in traces.text


@pytest.mark.asyncio
async def test_run_detail_accepts_canonical_core_id_without_uuid_marker_bits() -> None:
    client, store, _ = await _client()
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000103",
            observed_at=BASE_TIME,
            run_id=CORE_CANONICAL_ID,
        )
    )

    async with client:
        detail = await client.get(f"/api/v1/runs/{CORE_CANONICAL_ID}")

    assert detail.status_code == 200
    assert detail.json()["runId"] == CORE_CANONICAL_ID


def test_cursor_accepts_canonical_identifier_without_uuid_marker_bits() -> None:
    cursor = encode_cursor(BASE_TIME, CORE_CANONICAL_ID)

    observed_at, identifier = decode_cursor(cursor)

    assert observed_at == BASE_TIME
    assert identifier == CORE_CANONICAL_ID


@pytest.mark.asyncio
async def test_component_health_filters_time_and_uses_true_percentiles() -> None:
    client, store, _ = await _client()
    store.observations.clear()
    store.events.clear()
    for index, value in enumerate((10, 20, 30, 40)):
        await store.insert_observation(
            make_payload(
                f"00000000-0000-4000-8000-00000000011{index}",
                    observed_at=BASE_TIME + timedelta(seconds=index),
                    run_id=make_run_id(f"health-{index}"),
                    span_id=f"0123456789abcde{index}",
                    value=value,
                outcome="success",
                status="completed",
            )
        )
    async with client:
        all_time = await client.get("/api/v1/components/aura.runtime.model_inference/health")
        assert all_time.status_code == 200
        summary = all_time.json()["summary"]
        assert summary["sampleCount"] == 4
        assert abs(float(summary["latencyP50Ms"]) - 25.0) < 0.001
        assert abs(float(summary["latencyP95Ms"]) - 38.5) < 0.001

        bounded = await client.get(
            "/api/v1/components/aura.runtime.model_inference/health",
            params={
                "from": (BASE_TIME + timedelta(seconds=1)).isoformat().replace("+00:00", "Z"),
                "to": (BASE_TIME + timedelta(seconds=3)).isoformat().replace("+00:00", "Z"),
            },
        )
        assert bounded.status_code == 200
        bounded_summary = bounded.json()["summary"]
        assert bounded_summary["sampleCount"] == 2
        assert abs(float(bounded_summary["latencyP50Ms"]) - 25.0) < 0.001
        assert abs(float(bounded_summary["latencyP95Ms"]) - 29.5) < 0.001


@pytest.mark.asyncio
async def test_deployment_catalogue_reports_all_service_roles() -> None:
    client, store, _ = await _client()
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000119",
            observed_at=BASE_TIME + timedelta(seconds=1),
            run_id=make_run_id("scheduler-run"),
            service_role="scheduler",
        )
    )
    async with client:
        response = await client.get("/api/v1/deployments")
        assert response.status_code == 200
        assert response.json()["items"][0]["serviceRoles"] == ["scheduler", "worker"]


@pytest.mark.asyncio
async def test_sse_snapshot_and_last_event_id_catch_up_without_url_credentials() -> None:
    client, store, _ = await _client()
    async with client:
        first = await client.get("/api/v1/events")
        assert first.status_code == 200
        assert first.headers["content-type"].startswith("text/event-stream")
        assert "event: snapshot" in first.text
        assert "00000000-0000-4000-8000-000000000101" in first.text
        assert "?" not in str(first.request.url)

        await store.insert_observation(
            make_payload(
                "00000000-0000-4000-8000-000000000103",
                observed_at=BASE_TIME,
                run_id=make_run_id("api-run-3"),
            )
        )
        replay = await client.get(
            "/api/v1/events",
            headers={"Last-Event-ID": "00000000-0000-4000-8000-000000000101"},
        )
        assert replay.status_code == 200
        assert "id: 00000000-0000-4000-8000-000000000103" in replay.text
        assert "id: 00000000-0000-4000-8000-000000000101" not in replay.text
        events = [line for line in replay.text.splitlines() if line.startswith("data: ")]
        assert events
        for line in events:
            payload = json.loads(line.removeprefix("data: "))
            assert not any(
                secret in json.dumps(payload).lower()
                for secret in ("prompt", "response", "token", "credential")
            )


def _sse_request(
    settings: Settings,
    session_id: str,
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
    host: str = "observatory.localhost:4300",
    scheme: str = "http",
) -> Request:
    request_headers = [
        (b"host", host.encode()),
        (b"cookie", f"{settings.effective_session_cookie_name}={session_id}".encode()),
    ]
    if headers:
        request_headers.extend(headers)
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": scheme,
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": b"",
            "headers": request_headers,
            "client": ("127.0.0.1", 1),
            "server": (host.split(":", 1)[0], 443 if scheme == "https" else 80),
        }
    )


@pytest.mark.asyncio
async def test_sse_accepts_real_browser_originless_eventsource_headers() -> None:
    settings = Settings(owner_subject=OWNER)
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    store = MemoryObservatoryStore()
    app = create_app(settings, auth=auth, query_store=store, testing=False)
    route = cast(
        APIRoute,
        next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/events"),
    )

    response = cast(
        StreamingResponse,
        await route.endpoint(
            _sse_request(
                settings,
                session_id,
                headers=[(b"sec-fetch-site", b"same-origin")],
            ),
            None,
        ),
    )
    assert "event: snapshot" in await anext(cast(AsyncIterator[str], response.body_iterator))
    await cast(Any, response.body_iterator).aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"headers": []},
        {"headers": [(b"sec-fetch-site", b"same-site")]},
        {
            "headers": [(b"sec-fetch-site", b"same-origin")],
            "host": "evil.example.invalid:4300",
        },
        {
            "headers": [(b"sec-fetch-site", b"same-origin")],
            "host": "observatory.localhost:bad",
        },
        {
            "headers": [
                (b"sec-fetch-site", b"same-origin"),
                (b"host", b"observatory.localhost:4300"),
            ]
        },
        {
            "headers": [
                (b"sec-fetch-site", b"same-origin"),
                (b"x-forwarded-proto", b"https,http"),
                (b"x-forwarded-host", b"observatory.localhost:4300"),
            ]
        },
        {
            "headers": [
                (b"sec-fetch-site", b"same-origin"),
                (b"forwarded", b"proto=http;host=observatory.localhost:4300,proto=http"),
            ]
        },
        {
            "headers": [
                (b"sec-fetch-site", b"same-origin"),
                (b"forwarded", b"proto=javascript;host=observatory.localhost:4300"),
            ]
        },
    ],
)
async def test_sse_rejects_ambiguous_or_mismatched_originless_headers(
    request_kwargs: dict[str, Any],
) -> None:
    settings = Settings(owner_subject=OWNER)
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=False)
    route = cast(
        APIRoute,
        next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/events"),
    )

    with pytest.raises(HTTPException) as error:
        await route.endpoint(_sse_request(settings, session_id, **request_kwargs), None)
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_http_run_and_trace_details_expose_partial_continuations() -> None:
    client, store, _ = await _client()
    store.observations.clear()
    store.events.clear()
    trace_id = "fedcba9876543210fedcba9876543210"
    run_id = make_run_id("http-partial")
    for index in range(2):
        await store.insert_observation(
            make_payload(
                f"00000000-0000-4000-8000-00000000018{index}",
                observed_at=BASE_TIME + timedelta(seconds=index),
                run_id=run_id,
                trace_id=trace_id,
                span_id=f"{index + 1:016x}",
            )
        )
    async with client:
        run = await client.get(f"/api/v1/runs/{run_id}", params={"page_size": 1})
        assert run.status_code == 200
        assert run.json()["partial"] is True
        assert run.json()["nextCursor"]
        trace = await client.get(f"/api/v1/traces/{trace_id}", params={"page_size": 1})
        assert trace.status_code == 200
        assert trace.json()["partial"] is True
        assert trace.json()["nextCursor"]


@pytest.mark.asyncio
async def test_sse_terminates_after_session_revocation() -> None:
    settings = Settings(owner_subject=OWNER)
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    route = cast(
        APIRoute,
        next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/events"),
    )
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/events",
            "raw_path": b"/api/v1/events",
            "query_string": b"",
            "headers": [
                (b"cookie", f"{settings.effective_session_cookie_name}={session_id}".encode()),
                (b"origin", settings.public_origin.encode()),
            ],
            "scheme": "http",
            "server": ("observatory.localhost", 4300),
            "client": ("127.0.0.1", 1),
        }
    )
    response = cast(StreamingResponse, await route.endpoint(request, None))
    iterator: AsyncIterator[str] = cast(AsyncIterator[str], response.body_iterator)
    assert "event: snapshot" in await anext(iterator)
    await auth.revoke(session_id)
    with pytest.raises(StopAsyncIteration):
        await asyncio.wait_for(anext(iterator), timeout=0.5)


@pytest.mark.asyncio
async def test_retention_endpoint_requires_csrf_confirmation_and_idempotency() -> None:
    client, store, session = await _client()
    async with client:
        base_headers = {"Origin": Settings().public_origin, "X-CSRF-Token": session.csrf_token}
        missing_csrf = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all"},
            headers={"Idempotency-Key": "missing-csrf", "X-Observatory-Confirmation": "purge-all"},
        )
        assert missing_csrf.status_code == 403

        invalid_confirmation = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all"},
            headers={
                **base_headers,
                "Idempotency-Key": "bad-confirm",
                "X-Observatory-Confirmation": "delete-before",
            },
        )
        assert invalid_confirmation.status_code == 400

        created = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all"},
            headers={
                **base_headers,
                "Idempotency-Key": "purge-idempotent",
                "X-Observatory-Confirmation": "purge-all",
            },
        )
        assert created.status_code == 202
        action = created.json()
        repeated = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all"},
            headers={
                **base_headers,
                "Idempotency-Key": "purge-idempotent",
                "X-Observatory-Confirmation": "purge-all",
            },
        )
        assert repeated.status_code == 202
        assert repeated.json()["actionId"] == action["actionId"]

        status_response = await client.get(f"/api/v1/retention/actions/{action['actionId']}")
        assert status_response.status_code == 200
        for _ in range(100):
            status_response = await client.get(f"/api/v1/retention/actions/{action['actionId']}")
            if status_response.json()["status"] in {"succeeded", "failed"}:
                break
            await asyncio.sleep(0)
        assert status_response.json()["status"] == "succeeded"
        assert await store.list_observations() == []


@pytest.mark.asyncio
async def test_retention_storage_failures_return_content_free_retryable_error() -> None:
    class FailingStore(MemoryObservatoryStore):
        async def save_retention(self, action: Any) -> bool:
            del action
            raise RuntimeError("private database host and SQL details")

    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(
        settings,
        auth=auth,
        query_store=FailingStore(),
        testing=True,
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all"},
            headers={
                "Origin": settings.public_origin,
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "storage-failure-key-0001",
                "X-Observatory-Confirmation": "purge-all",
            },
        )

    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "service_unavailable"
    assert body["message"] == "service unavailable"
    assert body["retryable"] is True
    assert set(body) <= {"code", "message", "retryable", "traceId"}
    assert "private database" not in response.text


def test_openapi_declares_versioned_owner_surface_and_sse() -> None:
    app = create_app(
        Settings(owner_subject=OWNER), query_store=MemoryObservatoryStore(), testing=True
    )
    schema = app.openapi()
    assert schema["info"]["version"] == "1.0.0"
    paths = set(schema["paths"])
    assert {
        "/api/v1/overview",
        "/api/v1/runs",
        "/api/v1/events",
        "/api/v1/retention/actions",
    } <= paths
    assert any(path.startswith("/api/v1/runs/") for path in paths)
    assert any(path.startswith("/api/v1/retention/actions/") for path in paths)
