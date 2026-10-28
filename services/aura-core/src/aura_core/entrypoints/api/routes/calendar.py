"""Thin owner-authenticated HTTP translation for read-only calendars."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from enum import Enum
from time import monotonic
from typing import cast
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field

from aura_core.domains.integrations.calendar.public import (
    CalendarAuthorizationRejected,
    CalendarNotConnected,
    CalendarQuery,
    CalendarRequestContext,
    CalendarValidationError,
    CalendarVersionConflict,
)
from aura_core.entrypoints.api.errors import CalendarApiError
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.calendar_oauth import (
    GOOGLE_CALENDAR_CALLBACK_PATH,
)
from aura_core.platform.telemetry import (
    CalendarTraceContext,
    calendar_trace_context,
    classify_calendar_terminal,
    current_calendar_trace_context,
    new_span_id,
)

router = APIRouter(tags=["Calendar"])


class ReplaceCalendarSelectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    calendar_ids: list[UUID] = Field(alias="calendarIds", max_length=50)
    expected_version: int = Field(alias="expectedVersion", ge=1)


class CalendarQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    range_start: datetime = Field(alias="rangeStart")
    range_end: datetime = Field(alias="rangeEnd")
    display_time_zone: str = Field(alias="displayTimeZone", min_length=1, max_length=128)
    calendar_ids: list[UUID] = Field(alias="calendarIds", min_length=1, max_length=50)
    limit: int = Field(default=50, ge=1, le=100)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    timeout_ms: int = Field(default=5000, alias="timeoutMs", ge=500, le=10_000)


class CalendarConflictQueryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)
    range_start: datetime = Field(alias="rangeStart")
    range_end: datetime = Field(alias="rangeEnd")
    display_time_zone: str = Field(alias="displayTimeZone", min_length=1, max_length=128)
    calendar_ids: list[UUID] = Field(alias="calendarIds", min_length=1, max_length=50)
    cursor: str | None = Field(default=None, min_length=1, max_length=1024)
    timeout_ms: int = Field(default=5000, alias="timeoutMs", ge=500, le=10_000)
    conflict_limit: int = Field(default=100, alias="conflictLimit", ge=1, le=200)


def _state(request: Request) -> AppState:
    return request.app.state.aura


def _owner(session: Session) -> tuple[str, str]:
    return session.principal.issuer, session.principal.subject


def _session_id(request: Request) -> str:
    return request.cookies.get(_state(request).settings.session_cookie.name, "")


def _intentional_top_level_navigation(request: Request) -> bool:
    """Require browser navigation evidence before consuming an OAuth handle."""

    headers = request.headers
    purpose = " ".join(
        value.lower()
        for value in (headers.get("purpose", ""), headers.get("sec-purpose", ""))
    )
    if "prefetch" in purpose or headers.get("x-moz", "").lower() == "prefetch":
        return False
    return (
        headers.get("sec-fetch-mode", "").lower() == "navigate"
        and headers.get("sec-fetch-dest", "").lower() == "document"
        and headers.get("sec-fetch-user", "") == "?1"
        and headers.get("sec-fetch-site", "").lower() in {"same-origin", "same-site"}
    )


def _query(body: CalendarQueryRequest | CalendarConflictQueryRequest) -> CalendarQuery:
    return CalendarQuery(
        body.range_start,
        body.range_end,
        body.display_time_zone,
        tuple(body.calendar_ids),
        int(getattr(body, "limit", 50)),
        body.cursor,
        body.timeout_ms,
    )


def _camel(name: str) -> str:
    parts = name.split("_")
    return parts[0] + "".join(item.title() for item in parts[1:])


def _payload(value: object) -> object:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        result = {
            _camel(field.name): _payload(getattr(value, field.name))
            for field in dataclasses.fields(value)
            if field.name
            not in {
                "credential",
                "token",
                "provider_id",
                "provider_calendar_id",
                # Internal revocation exhaustion state is not part of the
                # closed v1.19 public connection response.
                "revocation_terminal",
            }
        }
        actions = getattr(value, "available_actions", None)
        if actions is not None:
            result["availableActions"] = _payload(actions)
        return result
    if isinstance(value, dict):
        values = cast(Mapping[object, object], value)
        return {_camel(str(key)): _payload(item) for key, item in values.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        values = cast(Sequence[object], value)
        return [_payload(item) for item in values]
    return value


def _request_cancelled(request: Request) -> bool:
    event = getattr(request.state, "calendar_disconnect_event", None)
    return isinstance(event, asyncio.Event) and event.is_set()


def _calendar_request(request: Request) -> CalendarRequestContext:
    context = current_calendar_trace_context()
    if context is None or context.deadline is None:
        return CalendarRequestContext(monotonic() + 5.0, lambda: _request_cancelled(request))
    return CalendarRequestContext(context.deadline, lambda: _request_cancelled(request))


async def _watch_disconnect(request: Request, event: asyncio.Event) -> None:
    while not event.is_set():
        if await request.is_disconnected():
            event.set()
            return
        await asyncio.sleep(0.05)


async def _invoke(
    request: Request,
    component: str,
    operation: str,
    call: Callable[[], Awaitable[object]],
    *,
    timeout_ms: int = 5_000,
) -> object:
    if not hasattr(request.state, "calendar_trace_id"):
        request.state.calendar_trace_id = uuid4().hex
        request.state.calendar_root_span_id = new_span_id()
    trace_id = request.state.calendar_trace_id
    parent_span_id = request.state.calendar_root_span_id
    span_id = new_span_id()
    deadline = monotonic() + max(1, timeout_ms) / 1000
    started = monotonic()
    disconnect_event = asyncio.Event()
    request.state.calendar_disconnect_event = disconnect_event
    context = CalendarTraceContext(
        trace_id, span_id, deadline, lambda: disconnect_event.is_set()
    )
    disconnect_watcher = asyncio.create_task(_watch_disconnect(request, disconnect_event))

    async def run_call() -> object:
        return await call()

    with calendar_trace_context(context):
        call_task: asyncio.Task[object] = asyncio.create_task(run_call())
        disconnect_waiter = asyncio.create_task(disconnect_event.wait())
        try:
            async with asyncio.timeout(max(1, timeout_ms) / 1000):
                done, _ = await asyncio.wait(
                    (call_task, disconnect_waiter),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if disconnect_waiter in done and disconnect_event.is_set():
                    call_task.cancel()
                    await asyncio.gather(call_task, return_exceptions=True)
                    raise asyncio.CancelledError
                result: object = call_task.result()
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            _observe(
                request,
                component,
                operation,
                started,
                *_calendar_terminal(exc),
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
            raise
        finally:
            if not disconnect_waiter.done():
                disconnect_waiter.cancel()
            await asyncio.gather(disconnect_waiter, return_exceptions=True)
            if not call_task.done():
                call_task.cancel()
                await asyncio.gather(call_task, return_exceptions=True)
            disconnect_event.set()
            disconnect_watcher.cancel()
            await asyncio.gather(disconnect_watcher, return_exceptions=True)
    success_outcome = "partial" if bool(getattr(result, "partial", False)) else "ok"
    _observe(
        request,
        component,
        operation,
        started,
        success_outcome,
        None,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=parent_span_id,
        result=result,
    )
    return result


def _error_class(exc: BaseException) -> str:
    if isinstance(exc, asyncio.CancelledError):
        return "budget"
    category = getattr(getattr(exc, "category", None), "value", "")
    if category == "canceled":
        return "budget"
    if category in {
        "provider_auth",
        "provider_rate_limit",
        "provider_unavailable",
        "deadline",
        "deadline_exceeded",
    }:
        return "deadline" if category in {"deadline", "deadline_exceeded"} else category
    if category == "validation":
        return "validation"
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return "deadline"
    if isinstance(exc, CalendarAuthorizationRejected):
        return "authorization"
    if isinstance(exc, CalendarValidationError):
        return "validation"
    if isinstance(exc, CalendarVersionConflict):
        return "conflict"
    if isinstance(exc, CalendarNotConnected):
        return "dependency_unavailable"
    if isinstance(exc, (ValueError, TypeError)):
        return "validation"
    return "internal"


def _calendar_terminal(exc: BaseException) -> tuple[str, str | None]:
    error_class = _error_class(exc)
    if error_class == "budget":
        return classify_calendar_terminal("canceled", error_class=error_class)
    if error_class == "deadline":
        return classify_calendar_terminal("timeout", error_class=error_class)
    if error_class in {
        "authorization",
        "validation",
        "conflict",
        "provider_auth",
        "provider_rate_limit",
        "provider_unavailable",
        "dependency_unavailable",
    }:
        return "rejected", error_class
    return "error", "internal"


def _observe(
    request: Request,
    component: str,
    operation: str,
    started: float,
    outcome: str,
    error_class: str | None,
    *,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    result: object | None = None,
) -> None:
    state = _state(request)
    if component != "aura.automation.calendar_conflict":
        state.metrics.record_span(
            component,
            operation,
            (monotonic() - started) * 1000,
            trace_id=trace_id,
            span_id=span_id,
            parent_span_id=parent_span_id,
            dependency="application_validation",
            outcome=outcome,
            error_class=error_class,
        )
    metric = {
        "aura.integration.calendar_connection": "calendar_connection_command_outcome",
        "aura.integration.calendar_retrieval": "calendar_retrieval_outcome",
    }.get(component)
    if metric is not None:
        outcome_attributes: dict[str, str] = {"outcome": outcome}
        if error_class is not None:
            outcome_attributes["error_class"] = error_class
        state.metrics.observe(
            component,
            metric,
            1.0,
            trace_id=trace_id,
            dependency="application_validation",
            **outcome_attributes,
        )
    if component == "aura.integration.calendar_connection":
        raw_status = getattr(getattr(result, "status", None), "value", "")
        health = raw_status if raw_status in {"connected", "disconnected"} else "degraded"
        state.metrics.observe(
            component,
            "calendar_connection_health",
            1.0,
            trace_id=trace_id,
            dependency="application_validation",
            outcome=outcome,
            connection_health=health,
        )
    if component == "aura.integration.calendar_retrieval" and result is not None:
        count = len(getattr(result, "items", ()))
        bucket = (
            "0" if count == 0 else "1_10" if count <= 10 else "11_50" if count <= 50 else "51_100"
        )
        state.metrics.observe(
            component,
            "calendar_returned_event_count_bucket",
            1.0,
            trace_id=trace_id,
            dependency="application_validation",
            outcome=outcome,
            event_count_bucket=bucket,
        )


def _calendar_error(exc: Exception) -> CalendarApiError:
    if isinstance(exc, CalendarAuthorizationRejected):
        return CalendarApiError(400, "callback_rejected", "connect", retryable=False)
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)):
        return CalendarApiError(503, "deadline_exceeded", "retry", retryable=True)
    if isinstance(exc, CalendarValidationError):
        return CalendarApiError(422, "validation", "retry", retryable=False)
    if isinstance(exc, CalendarVersionConflict):
        return CalendarApiError(409, "validation", "retry", retryable=True)
    if isinstance(exc, CalendarNotConnected):
        return CalendarApiError(409, "calendar_unavailable", "connect", retryable=False)
    category = getattr(exc, "category", None)
    category_value = getattr(category, "value", category)
    if category_value == "validation":
        return CalendarApiError(422, "validation", "retry", retryable=False)
    if category_value in {"canceled", "deadline", "deadline_exceeded", "timeout"}:
        return CalendarApiError(503, "deadline_exceeded", "retry", retryable=True)
    if category_value in {
        "provider_auth",
        "provider_rate_limit",
        "provider_unavailable",
        "deadline_exceeded",
        "deadline",
        "timeout",
        "canceled",
    }:
        normalized = "deadline_exceeded" if category_value == "deadline" else str(category_value)
        action = "reauthorize" if normalized == "provider_auth" else "retry"
        return CalendarApiError(503, normalized, action, retryable=True)
    if isinstance(exc, (ValueError, TypeError)):
        return CalendarApiError(422, "validation", "retry", retryable=False)
    return CalendarApiError(503, "dependency_unavailable", "retry", retryable=True)


@router.get("/api/v1/integrations/calendar/connection")
async def get_calendar_connection(
    request: Request, session: Session = Depends(require_session)
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.get",
            lambda: _state(request).calendar_service.connection(_owner(session)),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.post("/api/v1/integrations/calendar/connection/start", status_code=status.HTTP_202_ACCEPTED)
async def start_calendar_connection(
    request: Request,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.start",
            lambda: _state(request).calendar_service.start_connection(
                _owner(session),
                session_id=_session_id(request),
                idempotency_key=str(idempotency_key),
            ),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.post("/api/v1/integrations/calendar/connection/complete")
async def complete_calendar_connection(
    request: Request,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.complete",
            lambda: _state(request).calendar_service.complete_connection(
                _owner(session),
                session_id=_session_id(request),
                idempotency_key=str(idempotency_key),
            ),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.post(
    "/api/v1/integrations/calendar/connection/reauthorize",
    status_code=status.HTTP_202_ACCEPTED,
)
async def reauthorize_calendar_connection(
    request: Request,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.reauthorize",
            lambda: _state(request).calendar_service.reauthorize_connection(
                _owner(session),
                session_id=_session_id(request),
                idempotency_key=str(idempotency_key),
            ),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.post("/api/v1/integrations/calendar/connection/disconnect")
async def disconnect_calendar_connection(
    request: Request,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.disconnect",
            lambda: _state(request).calendar_service.disconnect_connection(
                _owner(session),
                idempotency_key=str(idempotency_key),
                request=_calendar_request(request),
            ),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.get("/api/v1/integrations/calendar/calendars")
async def list_selectable_calendars(
    request: Request,
    cursor: str | None = Query(default=None, min_length=1, max_length=1024),
    limit: int = Query(default=50, ge=1, le=100),
    session: Session = Depends(require_session),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_retrieval",
            "calendar.discovery.list",
            lambda: _state(request).calendar_service.discover(
                _owner(session),
                cursor=cursor,
                limit=limit,
                request=_calendar_request(request),
            ),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.put("/api/v1/integrations/calendar/calendars/selection")
async def replace_calendar_selection(
    request: Request,
    body: ReplaceCalendarSelectionRequest,
    session: Session = Depends(require_csrf),
    idempotency_key: UUID = Header(alias="Idempotency-Key"),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.selection",
            lambda: _state(request).calendar_service.replace_selection(
                _owner(session),
                tuple(body.calendar_ids),
                expected_version=body.expected_version,
                idempotency_key=str(idempotency_key),
            ),
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.post("/api/v1/integrations/calendar/events/query")
async def query_calendar_events(
    request: Request,
    body: CalendarQueryRequest,
    session: Session = Depends(require_session),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.integration.calendar_retrieval",
            "calendar.events.query",
            lambda: _state(request).calendar_service.events(
                _owner(session),
                _query(body),
                cancelled=lambda: _request_cancelled(request),
            ),
            timeout_ms=body.timeout_ms,
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.post("/api/v1/integrations/calendar/conflicts/query")
async def query_calendar_conflicts(
    request: Request,
    body: CalendarConflictQueryRequest,
    session: Session = Depends(require_session),
) -> object:
    try:
        result = await _invoke(
            request,
            "aura.automation.calendar_conflict",
            "calendar.conflicts.query",
            lambda: _state(request).calendar_service.conflicts(
                _owner(session),
                _query(body),
                conflict_limit=body.conflict_limit,
                cancelled=lambda: _request_cancelled(request),
            ),
            timeout_ms=body.timeout_ms,
        )
        return _payload(result)
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.get("/api/v1/integrations/calendar/authorize/{handle}")
async def authorize_calendar(
    request: Request, handle: str, session: Session = Depends(require_session)
) -> Response:
    if not _intentional_top_level_navigation(request):
        raise CalendarApiError(400, "authorization", "connect", retryable=False)
    try:
        target = await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.authorize",
            lambda: _state(request).calendar_service.authorize_navigation(
                _owner(session),
                handle,
                session_id=_session_id(request),
                request=_calendar_request(request),
            ),
        )
        if not isinstance(target, str) or not target.startswith("https://"):
            raise ValueError("invalid authorization target")
        return RedirectResponse(
            target,
            status_code=status.HTTP_302_FOUND,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
    except Exception as exc:
        raise _calendar_error(exc) from exc


@router.get("/api/v1/integrations/calendar/callback")
async def calendar_callback(
    request: Request,
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
) -> Response:
    try:
        oauth = _state(request).settings.calendar_oauth_config()
        callback_origin = oauth.callback_uri or (
            f"{oauth.public_origin.rstrip('/')}{GOOGLE_CALENDAR_CALLBACK_PATH}"
        )
        await _invoke(
            request,
            "aura.integration.calendar_connection",
            "calendar.connection.callback",
            lambda: _state(request).calendar_service.provider_callback(
                code=code,
                state=state,
                error=error,
                callback_origin=callback_origin,
                request=_calendar_request(request),
            ),
        )
    except Exception as exc:
        raise _calendar_error(exc) from exc
    return RedirectResponse(
        f"{_state(request).settings.public_origin.rstrip('/')}/integrations/calendar",
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )
