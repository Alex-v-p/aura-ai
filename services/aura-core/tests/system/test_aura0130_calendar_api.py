"""System-level route and privacy regression checks for calendar API wiring."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from aura_core.bootstrap.calendar_uow import _TelemetryAdapter
from aura_core.entrypoints.api.app import create_app
from aura_core.providers.calendars.google_calendar import (
    CalendarProviderError,
    ProviderFailureCategory,
)
from fastapi import FastAPI

from conftest import owner_client

CALENDAR_ROUTES = {
    ("GET", "/api/v1/integrations/calendar/connection"),
    ("POST", "/api/v1/integrations/calendar/connection/start"),
    ("POST", "/api/v1/integrations/calendar/connection/complete"),
    ("POST", "/api/v1/integrations/calendar/connection/reauthorize"),
    ("POST", "/api/v1/integrations/calendar/connection/disconnect"),
    ("GET", "/api/v1/integrations/calendar/calendars"),
    ("PUT", "/api/v1/integrations/calendar/calendars/selection"),
    ("POST", "/api/v1/integrations/calendar/events/query"),
    ("POST", "/api/v1/integrations/calendar/conflicts/query"),
}


class _TimeoutCalendarService:
    def __init__(self, metrics: Any, *, provider_deadline: bool) -> None:
        self._metrics = metrics
        self._provider_deadline = provider_deadline

    async def events(
        self, owner: object, query: object, *, cancelled: object = None
    ) -> object:
        del owner, query, cancelled
        if self._provider_deadline:
            _TelemetryAdapter(self._metrics).record(
                "deadline", retry_count=0, elapsed_ms=1.0
            )
            raise CalendarProviderError(ProviderFailureCategory.DEADLINE, retryable=True)
        await asyncio.sleep(1.0)
        raise AssertionError("route timeout did not cancel the calendar operation")


def _calendar_request_body(*, timeout_ms: int) -> dict[str, object]:
    return {
        "rangeStart": "2026-01-01T00:00:00Z",
        "rangeEnd": "2026-01-02T00:00:00Z",
        "displayTimeZone": "UTC",
        "calendarIds": [str(uuid4())],
        "timeoutMs": timeout_ms,
    }


@pytest.mark.asyncio
async def test_live_calendar_api_timeout_emits_complete_closed_retrieval_telemetry(
    api_app: FastAPI,
) -> None:
    api_app.state.aura.calendar_service = _TimeoutCalendarService(
        api_app.state.aura.metrics, provider_deadline=False
    )
    client, _ = await owner_client(api_app)
    try:
        response = await client.post(
            "/api/v1/integrations/calendar/events/query",
            json=_calendar_request_body(timeout_ms=500),
        )
    finally:
        await client.aclose()

    assert response.status_code == 503
    assert response.json()["category"] == "deadline_exceeded"
    snapshots = api_app.state.aura.metrics.snapshot()
    spans = [
        item
        for item in snapshots
        if item.kind == "span" and item.component_id == "aura.integration.calendar_retrieval"
    ]
    assert len(spans) == 1
    dimensions = dict(spans[0].dimensions)
    assert dimensions["outcome"] == "timeout"
    assert dimensions["error_class"] == "deadline"
    emitted = {
        item.metric
        for item in snapshots
        if item.component_id == "aura.integration.calendar_retrieval"
    }
    assert {
        "calendar_retrieval_duration_ms",
        "calendar_retrieval_outcome",
    } <= emitted
    assert api_app.state.aura.metrics.stats().rejected == 0


@pytest.mark.asyncio
async def test_live_provider_deadline_emits_provider_and_retrieval_telemetry(
    api_app: FastAPI,
) -> None:
    api_app.state.aura.calendar_service = _TimeoutCalendarService(
        api_app.state.aura.metrics, provider_deadline=True
    )
    client, _ = await owner_client(api_app)
    try:
        response = await client.post(
            "/api/v1/integrations/calendar/events/query",
            json=_calendar_request_body(timeout_ms=5_000),
        )
    finally:
        await client.aclose()

    assert response.status_code == 503
    assert response.json()["category"] == "deadline_exceeded"
    snapshots = api_app.state.aura.metrics.snapshot()
    provider_spans = [
        item
        for item in snapshots
        if item.kind == "span" and item.component_id == "aura.provider.google_calendar"
    ]
    retrieval_spans = [
        item
        for item in snapshots
        if item.kind == "span" and item.component_id == "aura.integration.calendar_retrieval"
    ]
    assert len(provider_spans) == 1
    assert len(retrieval_spans) == 1
    provider_dimensions = dict(provider_spans[0].dimensions)
    retrieval_dimensions = dict(retrieval_spans[0].dimensions)
    assert provider_dimensions["outcome"] == "timeout"
    assert provider_dimensions["error_class"] == "deadline"
    assert provider_dimensions["provider"] == "google_calendar"
    assert provider_dimensions["retry_class"] == "none"
    assert retrieval_dimensions["outcome"] == "timeout"
    assert retrieval_dimensions["error_class"] == "deadline"
    emitted = {
        item.component_id: {
            metric.metric for metric in snapshots if metric.component_id == item.component_id
        }
        for item in (*provider_spans, *retrieval_spans)
    }
    assert {
        "google_calendar_provider_request_duration_ms",
        "google_calendar_provider_request_outcome",
        "google_calendar_provider_retry_count",
        "calendar_rate_limit_state",
    } <= emitted["aura.provider.google_calendar"]
    assert {
        "calendar_retrieval_duration_ms",
        "calendar_retrieval_outcome",
    } <= emitted["aura.integration.calendar_retrieval"]
    assert api_app.state.aura.metrics.stats().rejected == 0


def test_all_v119_calendar_operations_are_thin_registered_routes() -> None:
    app = create_app(testing=True)
    registered = {
        (method_name.upper(), path)
        for path, metadata in app.openapi()["paths"].items()
        if path.startswith("/api/v1/integrations/calendar")
        for method_name in metadata
        if method_name.upper() in {"GET", "POST", "PUT", "PATCH", "DELETE"}
    }

    assert CALENDAR_ROUTES <= registered
    assert not {"GET", "PUT", "PATCH", "DELETE"} & {
        method for method, path in registered if path.endswith("/events/query")
    }


def test_calendar_route_module_does_not_duplicate_provider_or_persistence_rules() -> None:
    route_path = Path(__file__).parents[2] / "src/aura_core/entrypoints/api/routes/calendar.py"
    source = route_path.read_text(encoding="utf-8")
    assert "sqlalchemy" not in source
    assert "GoogleCalendarAdapter" not in source
    assert "httpx" not in source
    assert "CalendarApplicationService" in source or "calendar_service" in source


@pytest.mark.parametrize("path", sorted({path for _, path in CALENDAR_ROUTES}))
def test_calendar_api_paths_are_no_store_middleware_targets(path: str) -> None:
    app_path = Path(__file__).parents[2] / "src/aura_core/entrypoints/api/app.py"
    source = app_path.read_text(encoding="utf-8")
    assert "no-store" in source
    assert "/api/v1/integrations/calendar" in source
    assert path.startswith("/api/v1/integrations/calendar/")


def test_calendar_api_never_places_private_inputs_in_path_or_query() -> None:
    route_path = Path(__file__).parents[2] / "src/aura_core/entrypoints/api/routes/calendar.py"
    source = route_path.read_text(encoding="utf-8")
    assert "calendar_id: UUID" not in source
    assert "event_id: str" not in source
    assert "request.query_params" not in source


@pytest.mark.asyncio
async def test_connection_response_is_exact_owner_safe_schema_and_no_store(
    api_app: FastAPI,
) -> None:
    client, _ = await owner_client(api_app)
    try:
        response = await client.get("/api/v1/integrations/calendar/connection")
    finally:
        await client.aclose()

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert "revocationTerminal" not in payload
    assert set(payload) == {
        "status",
        "readsEnabled",
        "selectedCalendarCount",
        "connectedAt",
        "updatedAt",
        "lastError",
        "availableActions",
    }
    assert payload["readsEnabled"] is False
    assert payload["selectedCalendarCount"] == 0


@pytest.mark.asyncio
async def test_disconnected_event_query_is_closed_no_store_error_without_request_echo(
    api_app: FastAPI,
) -> None:
    client, _ = await owner_client(api_app)
    try:
        response = await client.post(
            "/api/v1/integrations/calendar/events/query",
            json={
                "rangeStart": "2026-01-01T00:00:00Z",
                "rangeEnd": "2026-01-02T00:00:00Z",
                "displayTimeZone": "UTC",
                "calendarIds": [str(uuid4())],
            },
        )
    finally:
        await client.aclose()

    assert response.status_code == 409
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) == {
        "type",
        "title",
        "status",
        "category",
        "detail",
        "retryable",
        "recoveryAction",
        "traceId",
    }
    assert "calendarIds" not in response.text
    assert "2026-01-01" not in response.text


@pytest.mark.asyncio
async def test_oauth_callback_and_prefetch_rejection_have_exact_privacy_headers_and_errors(
    api_app: FastAPI,
) -> None:
    client, _ = await owner_client(api_app)
    try:
        callback = await client.get(
            "/api/v1/integrations/calendar/callback?code=private-code&state=private-state"
        )
        prefetch = await client.get(
            "/api/v1/integrations/calendar/authorize/opaque-handle",
            headers={"Sec-Fetch-Mode": "cors"},
        )
    finally:
        await client.aclose()

    for response in (callback, prefetch):
        assert response.status_code == 400
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["referrer-policy"] == "no-referrer"
        payload = response.json()
        assert set(payload) == {
            "type",
            "title",
            "status",
            "category",
            "detail",
            "retryable",
            "recoveryAction",
            "traceId",
        }
        assert "private-code" not in response.text
        assert "private-state" not in response.text
        assert "opaque-handle" not in response.text
