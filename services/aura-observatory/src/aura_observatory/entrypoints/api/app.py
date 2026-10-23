"""Thin FastAPI composition root for private metadata ingestion."""
# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false

from __future__ import annotations

import asyncio
import json
import logging
import re
import zlib
from collections.abc import AsyncGenerator, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from time import monotonic
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import StorageUnavailableError
from aura_observatory.auth import ObservatoryAuth, sanitize_return_to
from aura_observatory.composition import (
    build_auth,
    build_evaluation_api_service,
    build_health_projector,
    build_ingest_service,
)
from aura_observatory.evaluation_api import register_evaluation_routes
from aura_observatory.ingest import IngestPayloadError, InvalidCredential, TelemetryIngestService
from aura_observatory.otlp import OtlpPayloadError, metrics_to_observations, traces_to_observations
from aura_observatory.query import (
    ANALYTICS_AGGREGATIONS,
    ANALYTICS_BUCKETS,
    ANALYTICS_FOCUSES,
    ANALYTICS_GROUPINGS,
    ANALYTICS_METRICS,
    ANALYTICS_UNITS,
    AnalyticsRequest,
    ObservatoryHealthProjector,
    ObservatoryQueryService,
    RetentionConflictError,
    RetentionKind,
    RetentionRequest,
    RunFilters,
    analytics_metric_catalogue,
    decode_health_cursor,
)
from aura_observatory.settings import Settings
from aura_observatory.alerting import AlertError
from aura_observatory.annotations import AnnotationError, AnnotationTargetNotFound, AnnotationTargetUnavailable
from aura_observatory.dashboards import DashboardError
from aura_observatory.incidents import IncidentError
from aura_observatory.saved_queries import SavedQueryError

_LOGGER = logging.getLogger(__name__)

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT_ID_RE = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{16,1024}$")
_EXECUTION_CURSOR_RE = re.compile(r"^(?:sq2_[A-Za-z0-9_-]+|sq1\.[A-Za-z0-9_-]+)$")
_RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "unknown"})
_AUTH_RECOVERY_QUERY = "auth_recovery"
_AUTH_RECOVERY_MARKERS = frozenset(
    {"state_missing", "state_invalid", "code_missing", "provider_denied", "callback_failed"}
)


def _request_trace_id(request: Request) -> str:
    """Return the bounded request trace propagated to Problems and spans."""

    existing = getattr(request.state, "trace_id", None)
    if isinstance(existing, str) and _TRACE_ID_RE.fullmatch(existing):
        return existing

    candidate = request.headers.get("x-trace-id")
    if candidate is None:
        traceparent = request.headers.get("traceparent", "")
        parts = traceparent.split("-")
        candidate = parts[1] if len(parts) == 4 else None
    if candidate is not None and _TRACE_ID_RE.fullmatch(candidate):
        request.state.trace_id = candidate
        return candidate
    request.state.trace_id = uuid4().hex
    return request.state.trace_id


def _otlp_rejection_class(exc: BaseException) -> str:
    """Classify OTLP failures without putting submitted values in logs."""

    if isinstance(exc, UnicodeDecodeError):
        return "encoding"
    if isinstance(exc, json.JSONDecodeError):
        return "json"
    if isinstance(exc, OtlpPayloadError):
        # The exception text is inspected only in memory.  Emit a finite class,
        # never the submitted field name, value, payload, or exception text.
        message = str(exc).casefold()
        if "contains unsupported fields" in message:
            return "unsupported_fields"
        if "dropped" in message and "must be zero" in message:
            return "dropped_nonzero"
        if "status code is unsupported" in message:
            return "status_code"
        if "status message is not accepted" in message:
            return "status_message"
        if message.endswith("must be an object"):
            return "shape_object"
        if message.endswith("must be an array"):
            return "shape_array"
        if message.endswith("must be a non-empty string"):
            static_string_shapes = {
                "traceid must be a non-empty string": "shape_trace_id",
                "spanid must be a non-empty string": "shape_span_id",
                "parentspanid must be a non-empty string": "shape_parent_span_id",
                "link traceid must be a non-empty string": "shape_link_trace_id",
                "link spanid must be a non-empty string": "shape_link_span_id",
                "attribute key must be a non-empty string": "shape_attribute_key",
            }
            return static_string_shapes.get(message, "shape_attribute_value")
        if "scope" in message:
            return "scope"
        if "link" in message:
            return "link"
        if "event" in message:
            return "event"
        if "attribute" in message:
            return "attribute"
        if any(marker in message for marker in ("must", "required", "array", "object")):
            return "shape"
        return "contract_value"
    return "malformed"


def create_app(
    settings: Settings | None = None,
    *,
    ingest_service: TelemetryIngestService | None = None,
    auth: ObservatoryAuth | None = None,
    query_store: object | None = None,
    evaluation_service: object | None = None,
    testing: bool = False,
) -> FastAPI:
    """Build the API with an explicit service dependency for deterministic tests."""

    resolved = settings or Settings()
    memory_store: MemoryObservatoryStore | None = None
    if ingest_service is not None:
        service = ingest_service
    elif testing:
        memory_store = MemoryObservatoryStore()
        service = TelemetryIngestService(
            memory_store,
            memory_store,
            credential=resolved.ingest_credential(),
            max_batch_size=resolved.ingest_max_batch_size,
        )
    else:
        service = build_ingest_service(resolved)
    auth_service = auth or build_auth(resolved, testing=testing)

    @asynccontextmanager
    async def lifespan(live_app: FastAPI) -> AsyncGenerator[None]:
        if live_app.state.background_tasks_enabled:
            if live_app.state.health_projector_task is None:
                stop = asyncio.Event()
                live_app.state.health_projector_stop = stop
                live_app.state.health_projector_task = asyncio.create_task(
                    live_app.state.health_projector.run(stop)
                )
        try:
            yield
        finally:
            stop = live_app.state.health_projector_stop
            task = live_app.state.health_projector_task
            if stop is not None and task is not None:
                stop.set()
                try:
                    await asyncio.wait_for(task, timeout=5.0)
                except TimeoutError, asyncio.CancelledError:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                finally:
                    live_app.state.health_projector_stop = None
                    live_app.state.health_projector_task = None

    app = FastAPI(title="Aura Observatory API", version="1.0.0", lifespan=lifespan)
    app.state.ingest = service
    app.state.auth = auth_service

    @app.middleware("http")
    async def request_trace_context(request: Request, call_next: Any) -> Response:
        metrics = getattr(getattr(request.app.state, "query", None), "metrics", None)
        trace_context = getattr(metrics, "trace_context", None)
        if callable(trace_context):
            context_manager = cast(Any, trace_context)(_request_trace_id(request))
            with context_manager:
                return await call_next(request)
        return await call_next(request)

    @app.exception_handler(HTTPException)
    async def problem_response(request: Request, exc: HTTPException) -> JSONResponse:
        if request.url.path.startswith("/api/v1/"):
            code = {
                400: "bad_request",
                401: "unauthorized",
                403: "forbidden",
                404: "not_found",
                409: "conflict",
                503: "service_unavailable",
            }.get(exc.status_code, "request_failed")
            detail = {
                400: "invalid request",
                401: "authentication required",
                403: "request not permitted",
                404: "resource not found",
                409: "request conflicts with existing state",
                503: "service unavailable",
            }.get(exc.status_code, "request failed")
            return JSONResponse(
                status_code=exc.status_code,
                content={
                    "code": code,
                    "message": detail,
                    "retryable": exc.status_code >= 500,
                    "traceId": _request_trace_id(request),
                },
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

    @app.exception_handler(StorageUnavailableError)
    async def storage_unavailable_response(
        request: Request, _exc: StorageUnavailableError
    ) -> JSONResponse:
        if request.url.path.startswith("/api/v1/"):
            return JSONResponse(
                status_code=503,
                content={
                    "code": "service_unavailable",
                    "message": "service unavailable",
                    "retryable": True,
                    "traceId": _request_trace_id(request),
                },
            )
        return JSONResponse(status_code=503, content={"detail": "service unavailable"})

    @app.exception_handler(RequestValidationError)
    async def request_validation_problem(
        request: Request, _exc: RequestValidationError
    ) -> JSONResponse:
        """Translate framework validation without serializing submitted values."""

        if request.url.path.startswith("/api/v1/"):
            protected = (
                request.url.path == "/api/v1/overview"
                or request.url.path.startswith("/api/v1/runs")
                or request.url.path.startswith("/api/v1/traces")
                or request.url.path.startswith("/api/v1/components")
                or request.url.path.startswith("/api/v1/deployments")
                or request.url.path.startswith("/api/v1/analytics")
                or request.url.path == "/api/v1/run-comparisons/query"
                or request.url.path == "/api/v1/events"
                or request.url.path.startswith("/api/v1/retention")
            )
            if protected:
                session_id = request.cookies.get(resolved.effective_session_cookie_name)
                session = await auth_service.get_session(session_id)
                if session is None:
                    return JSONResponse(
                        status_code=401,
                        content={
                            "code": "unauthorized",
                            "message": "authentication required",
                            "retryable": False,
                            "traceId": _request_trace_id(request),
                        },
                    )
                if resolved.owner_subject and session.principal.subject != resolved.owner_subject:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "code": "forbidden",
                            "message": "owner authorization required",
                            "retryable": False,
                            "traceId": _request_trace_id(request),
                        },
                    )
            return JSONResponse(
                status_code=422,
                content={
                    "code": "invalid_request",
                    "message": "request validation failed",
                    "retryable": False,
                    "traceId": _request_trace_id(request),
                },
            )
        return JSONResponse(status_code=422, content={"detail": "request validation failed"})

    if query_store is None:
        if testing:
            query_store = memory_store or MemoryObservatoryStore()
            app.state.query = ObservatoryQueryService(query_store)
        else:
            from aura_observatory.composition import build_query_service

            app.state.query = build_query_service(resolved)
    else:
        app.state.query = ObservatoryQueryService(query_store)  # type: ignore[arg-type]

    # Health projection belongs to the API lifecycle, independently of the
    # ingest worker.  Testing compositions opt out by default so unit tests
    # never leak a background task; lifecycle tests can enable it explicitly.
    app.state.health_projector = (
        ObservatoryHealthProjector(app.state.query.health_port)
        if testing or query_store is not None
        else build_health_projector(resolved)
    )
    app.state.background_tasks_enabled = not testing
    app.state.testing = testing
    app.state.health_projector_stop = None
    app.state.health_projector_task = None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "aura-observatory"}

    @app.get("/metrics", include_in_schema=False)
    async def metrics() -> PlainTextResponse:
        """Supported scrape surface; values contain metadata-only labels."""

        local = app.state.query.metrics.prometheus(
            exclude_names={
                "observatory_retention_action_count",
                "observatory_retention_deleted_count",
                "observatory_retention_failure_count",
            }
        )
        persisted = await app.state.query.persisted_metric_series()
        return PlainTextResponse(local + app.state.query.metrics.prometheus_for_series(persisted))

    @app.get("/api/v1/auth/login")
    async def login(return_to: str = "/") -> RedirectResponse:
        return RedirectResponse(await auth_service.authorization_url(return_to), status_code=307)

    @app.get("/api/v1/auth/callback", include_in_schema=False)
    async def callback(
        request: Request,
        code: str | None = Query(default=None),
        state: str | None = Query(default=None),
        error: str | None = Query(default=None),
    ) -> RedirectResponse:
        started = monotonic()

        def recover(
            marker: str,
            return_to: str | None = None,
            *,
            stage: str,
            outcome: str,
        ) -> RedirectResponse:
            _record_auth_callback(app, started, stage, outcome)
            return _callback_recovery_response(resolved, marker, return_to)

        login_state = None
        if state:
            try:
                login_state = await auth_service.login_states.consume(state)
            except Exception:
                # Provider/state backend details never cross the callback
                # boundary. Treat malformed or unavailable state as a local
                # recovery, with no return path because it cannot be trusted.
                return recover(
                    "state_invalid",
                    stage="login_state_store",
                    outcome="unavailable",
                )
        if login_state is None:
            marker = "state_missing" if not state else "state_invalid"
            return recover(
                marker,
                stage="login_state_store",
                outcome="missing" if not state else "invalid",
            )
        return_to = sanitize_return_to(login_state.return_to)
        if error is not None:
            return recover(
                "provider_denied",
                return_to,
                stage="provider",
                outcome="denied",
            )
        if not code:
            return recover(
                "code_missing",
                return_to,
                stage="provider",
                outcome="code_missing",
            )
        try:
            tokens, claims = await auth_service.authenticate_code(code, login_state.nonce)
        except PermissionError:
            return recover(
                "callback_failed",
                return_to,
                stage="authorization",
                outcome="rejected",
            )
        except Exception:
            return recover(
                "callback_failed",
                return_to,
                stage="provider",
                outcome="exchange_failed",
            )
        try:
            principal = auth_service.authorize_claims(claims)
        except Exception:
            return recover(
                "callback_failed",
                return_to,
                stage="authorization",
                outcome="rejected",
            )
        try:
            prior_session = request.cookies.get(resolved.effective_session_cookie_name)
            session_id, _session = await auth_service.create_session(
                principal, tokens, replace_session_id=prior_session
            )
        except Exception:
            return recover(
                "callback_failed",
                return_to,
                stage="session_store",
                outcome="failed",
            )
        _record_auth_callback(app, started, "callback", "success")
        redirect = RedirectResponse(return_to, status_code=303)
        _set_session_cookie(redirect, resolved, session_id)
        return redirect

    @app.get("/api/v1/auth/me")
    async def me(request: Request) -> JSONResponse:
        session_id = request.cookies.get(resolved.effective_session_cookie_name)
        session = await auth_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        # Keep the browser-facing contract closed: owner identity is an
        # authentication concern, not Observatory telemetry.  The browser
        # only needs this ephemeral value for same-origin state changes.
        return JSONResponse({"csrfToken": session.csrf_token})

    @app.post("/api/v1/auth/logout", status_code=status.HTTP_204_NO_CONTENT)
    async def logout(
        request: Request,
        csrf: str | None = Header(default=None, alias="X-Observatory-CSRF"),
    ) -> Response:
        session_id = request.cookies.get(resolved.effective_session_cookie_name)
        session = await auth_service.get_session(session_id)
        if session is None:
            response = Response(status_code=status.HTTP_204_NO_CONTENT)
            _clear_session_cookie(response, resolved)
            return response
        try:
            auth_service.require_csrf(session, csrf, origin=request.headers.get("origin"))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="invalid CSRF token") from exc
        await auth_service.revoke(session_id)
        response = Response(status_code=status.HTTP_204_NO_CONTENT)
        _clear_session_cookie(response, resolved)
        return response

    async def owner_session(request: Request):
        session_id = request.cookies.get(resolved.effective_session_cookie_name)
        session = await auth_service.get_session(session_id)
        if session is None:
            raise HTTPException(status_code=401, detail="authentication required")
        if resolved.owner_subject and session.principal.subject != resolved.owner_subject:
            raise HTTPException(status_code=403, detail="owner authorization required")
        return session

    # Evaluation resources are registered after the owner-session closure is
    # available so every route reuses the same BFF authorization and CSRF
    # boundary.  The application service remains replaceable for production
    # composition and deterministic test fixtures.
    # Production always uses the durable PostgreSQL/ClickHouse query port.
    # Missing optional object-store credentials disable artifact commands at
    # the application boundary; they must never select an in-memory read model.
    selected_evaluation_service = (
        evaluation_service
        if evaluation_service is not None
        else build_evaluation_api_service(resolved) if not testing else None
    )
    register_evaluation_routes(
        app,
        owner_session=owner_session,
        auth_service=auth_service,
        service=cast(Any, selected_evaluation_service),
        resolved=resolved,
    )
    _register_operations_routes(app, owner_session, auth_service, resolved, testing=testing)

    @app.get("/api/v1/overview")
    async def overview(request: Request) -> JSONResponse:
        await owner_session(request)
        return JSONResponse(_overview_payload(await app.state.query.overview()))

    @app.get("/api/v1/analytics/metrics")
    async def analytics_metrics(
        request: Request, focus: str | None = Query(default=None)
    ) -> JSONResponse:
        await owner_session(request)
        if focus is not None and focus not in ANALYTICS_FOCUSES:
            raise HTTPException(status_code=400, detail="invalid request")
        return JSONResponse(analytics_metric_catalogue(focus=focus))

    @app.get("/api/v1/analytics/query")
    async def analytics_query(
        request: Request,
        metric: str,
        unit: str,
        aggregation: str = "average",
        focus: str = "all",
        from_time: str | None = Query(default=None, alias="from"),
        to_time: str | None = Query(default=None, alias="to"),
        bucket: str = "auto",
        group_by: str = Query(default="none", alias="group_by"),
        component: str | None = None,
        component_version: str | None = None,
        operation: str | None = None,
        deployment: str | None = None,
        environment: str | None = None,
        host: str | None = None,
        service_role: str | None = Query(default=None, alias="service_role"),
        provider: str | None = None,
        outcome: str | None = None,
        error_class: str | None = Query(default=None, alias="error_class"),
        status: str | None = None,
        retrieval_stage: str | None = Query(default=None, alias="retrieval_stage"),
        series_limit: str | None = Query(default=None, alias="series_limit"),
        point_limit: str | None = Query(default=None, alias="point_limit"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_analytics_inputs(
                metric,
                unit,
                aggregation,
                focus,
                bucket,
                group_by,
                component,
                component_version,
                operation,
                deployment,
                environment,
                host,
                service_role,
                provider,
                outcome,
                error_class,
                status,
                retrieval_stage,
            )
            filters = {
                key: value
                for key, value in {
                    "component": component,
                    "component_version": component_version,
                    "operation": operation,
                    "deployment": deployment,
                    "environment": environment,
                    "host": host,
                    "service_role": service_role,
                    "provider": provider,
                    "outcome": outcome,
                    "error_class": error_class,
                    "status": status,
                    "retrieval_stage": retrieval_stage,
                }.items()
                if value is not None
            }
            result = await app.state.query.analytics(
                AnalyticsRequest(
                    metric=metric,
                    unit=unit,
                    aggregation=aggregation,
                    focus=focus,
                    from_time=_parse_utc_z(from_time),
                    to_time=_parse_utc_z(to_time),
                    bucket=bucket,
                    group_by=group_by,
                    filters=filters,
                    series_limit=_parse_bounded_int(
                        series_limit, default=12, minimum=1, maximum=12
                    ),
                    point_limit=_parse_bounded_int(
                        point_limit, default=200, minimum=1, maximum=200
                    ),
                )
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid analytics query") from exc
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/health/history")
    async def health_history(
        request: Request,
        cursor: str | None = None,
        from_time: str | None = Query(default=None, alias="from"),
        to_time: str | None = Query(default=None, alias="to"),
        limit: str | None = None,
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_health_cursor(cursor)
            requested_limit = _parse_history_limit(limit)
            result = await app.state.query.health_history(
                cursor=cursor,
                from_time=_parse_time(from_time),
                to_time=_parse_time(to_time),
                limit=requested_limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        return JSONResponse(_health_history_payload(result))

    @app.get("/api/v1/runs")
    async def runs(
        request: Request,
        status_filter: str | None = Query(default=None, alias="status"),
        component: str | None = None,
        model: str | None = None,
        deployment: str | None = None,
        from_time: str | None = Query(default=None, alias="from"),
        to_time: str | None = Query(default=None, alias="to"),
        cursor: str | None = None,
        limit: str | None = Query(default=None),
        page_size: str | None = Query(default=None, alias="page_size"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_run_filter_inputs(status_filter, component, model, deployment)
            filters = RunFilters(
                status=status_filter,
                component=component,
                model=model,
                deployment=deployment,
                from_time=_parse_time(from_time),
                to_time=_parse_time(to_time),
            )
            raw_limit = page_size if page_size is not None else limit
            requested_limit = _parse_limit(raw_limit)
            _validate_cursor(cursor)
            result = await app.state.query.runs(filters, cursor=cursor, limit=requested_limit)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        return JSONResponse(_run_page_payload(result))

    @app.get("/api/v1/runs/{run_id}")
    async def run_detail(
        run_id: str,
        request: Request,
        cursor: str | None = None,
        page_size: str | None = Query(default=None, alias="page_size"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_uuid(run_id, "run identifier")
            _validate_cursor(cursor)
            result = await app.state.query.run_detail(
                run_id, cursor=cursor, limit=_parse_detail_limit(page_size)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="run not found")
        return JSONResponse(_run_detail_payload(result))

    @app.get("/api/v1/traces/{trace_id}")
    async def trace_detail(
        trace_id: str,
        request: Request,
        cursor: str | None = None,
        page_size: str | None = Query(default=None, alias="page_size"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_trace_id(trace_id)
            _validate_cursor(cursor)
            result = await app.state.query.trace_detail(
                trace_id, cursor=cursor, limit=_parse_detail_limit(page_size)
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if result is None:
            raise HTTPException(status_code=404, detail="trace not found")
        return JSONResponse(_trace_payload(result))

    @app.get("/api/v1/components")
    async def components(
        request: Request,
        cursor: str | None = None,
        page_size: str | None = Query(default=None, alias="page_size"),
        definition_state: str | None = Query(default=None, alias="definition_state"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_cursor(cursor)
            if definition_state is not None and definition_state not in {
                "declared",
                "observed_only",
                "missing",
            }:
                raise ValueError("invalid definition state")
            collection = await app.state.query.component_collection(
                definition_state=definition_state,
                cursor=cursor,
                limit=_parse_limit(page_size),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        values = cast(list[dict[str, Any]], collection.get("items", []))
        health = {
            str(value["componentId"]): await app.state.query.component_health(
                str(value["componentId"])
            )
            for value in values
        }
        payload = _component_collection_payload(values, health)
        payload["nextCursor"] = collection.get("nextCursor")
        payload["snapshotAt"] = collection.get("snapshotAt") or _now_iso()
        return JSONResponse(payload)

    @app.get("/api/v1/components/{component_id:path}/definitions/{component_version}")
    async def component_definition(
        component_id: str, component_version: str, request: Request
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_component_id(component_id)
            _validate_safe_label(component_version, "component version")
            definition = await app.state.query.component_definition(component_id, component_version)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if definition is None:
            raise HTTPException(status_code=404, detail="component definition not found")
        return JSONResponse(definition)

    @app.get("/api/v1/components/{component_id:path}/definitions/{component_version}/dependencies")
    async def component_dependencies(
        component_id: str,
        component_version: str,
        request: Request,
        depth: str | None = None,
        max_nodes: str | None = Query(default=None, alias="max_nodes"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_component_id(component_id)
            _validate_safe_label(component_version, "component version")
            requested_depth = _parse_bounded_int(depth, default=1, minimum=1, maximum=4)
            requested_nodes = _parse_bounded_int(max_nodes, default=100, minimum=1, maximum=200)
            topology = await app.state.query.component_dependencies(
                component_id,
                component_version,
                depth=requested_depth,
                max_nodes=requested_nodes,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if topology is None:
            raise HTTPException(status_code=404, detail="component definition not found")
        return JSONResponse(topology)

    @app.get("/api/v1/components/{component_id:path}/health")
    async def component_health(
        component_id: str,
        request: Request,
        component_version: str | None = Query(default=None, alias="component_version"),
        from_time: str | None = Query(default=None, alias="from"),
        to_time: str | None = Query(default=None, alias="to"),
        point_limit: str | None = Query(default=None, alias="point_limit"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_component_id(component_id)
            _validate_safe_label(component_version, "component version")
            health = await app.state.query.component_health(
                component_id,
                from_time=_parse_time(from_time),
                to_time=_parse_time(to_time),
                component_version=component_version,
                point_limit=_parse_bounded_int(point_limit, default=200, minimum=1, maximum=1000),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if health is None:
            raise HTTPException(status_code=404, detail="component not found")
        return JSONResponse(health)

    @app.post("/api/v1/run-comparisons/query")
    async def compare_runs(request: Request) -> JSONResponse:
        """Return an ephemeral, bounded owner-only comparison snapshot."""

        await owner_session(request)
        try:
            body = await request.json()
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if not isinstance(body, dict):
            raise HTTPException(status_code=422, detail="invalid request")
        comparison_body = cast(dict[str, object], body)
        if set(comparison_body) != {"leftRunId", "rightRunId"}:
            raise HTTPException(status_code=422, detail="invalid request")
        left_run_id = comparison_body.get("leftRunId")
        right_run_id = comparison_body.get("rightRunId")
        if not isinstance(left_run_id, str) or not isinstance(right_run_id, str):
            raise HTTPException(status_code=422, detail="invalid request")
        try:
            _validate_uuid(left_run_id, "left run identifier")
            _validate_uuid(right_run_id, "right run identifier")
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="invalid request") from exc
        if left_run_id == right_run_id:
            raise HTTPException(status_code=422, detail="invalid request")
        result = await app.state.query.compare_runs(left_run_id, right_run_id)
        if result is None:
            raise HTTPException(status_code=404, detail="run not found")
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @app.get("/api/v1/deployments")
    async def deployments(request: Request) -> JSONResponse:
        await owner_session(request)
        return JSONResponse(_deployment_collection_payload(await app.state.query.deployments()))

    @app.get("/api/v1/events")
    async def events(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        origin_values = request.headers.getlist("origin")
        origin = origin_values[0] if len(origin_values) == 1 else None
        browser_origin_allowed = _browser_sse_origin_allowed(request, resolved.public_origin)
        if (
            origin != resolved.public_origin
            and not (not origin_values and browser_origin_allowed)
            and not (testing and not origin_values)
        ):
            raise HTTPException(status_code=403, detail="same-origin request required")
        await owner_session(request)
        try:
            _validate_uuid(last_event_id, "last event identifier")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        stream_session_id = request.cookies.get(resolved.effective_session_cookie_name)

        async def stream() -> AsyncIterator[str]:
            async def session_is_authorized() -> bool:
                # A stream outlives its initial request. Re-fetch the server
                # session before every snapshot/change/heartbeat so logout,
                # expiry, and owner revocation terminate the stream silently.
                session = await auth_service.get_session(stream_session_id)
                return session is not None and (
                    not resolved.owner_subject
                    or session.principal.subject == resolved.owner_subject
                )

            async def disconnected() -> bool:
                try:
                    return await request.is_disconnected()
                except RuntimeError:
                    # Unit/in-process ASGI callers may not provide a receive
                    # channel; the server still owns disconnect detection.
                    return False

            if await disconnected() or not await session_is_authorized():
                return
            snapshot = await app.state.query.overview()
            if await disconnected() or not await session_is_authorized():
                return
            yield (
                "event: snapshot\ndata: "
                + json.dumps(_overview_payload(snapshot), separators=(",", ":"))
                + "\n\n"
            )
            async for event in app.state.query.events_after(
                last_event_id, snapshot.get("snapshotSequence")
            ):
                if await disconnected() or not await session_is_authorized():
                    return
                data = event["data"]
                event_id = event.get("id")
                event_payload = {
                    "schemaVersion": 1,
                    "eventId": event_id or str(uuid4()),
                    "eventType": event["type"],
                    "occurredAt": _now_iso(),
                    "resourceType": "system"
                    if event["type"] == "health.changed"
                    else "observation",
                    "resourceId": data.get("observationId")
                    if event["type"] != "health.changed"
                    else None,
                    "capturePolicy": "metadata_only",
                }
                id_line = f"id: {event_id}\n" if event_id else ""
                yield (
                    f"{id_line}event: {event['type']}\n"
                    f"data: {json.dumps(event_payload, separators=(',', ':'))}\n\n"
                )

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/v1/retention/actions", status_code=status.HTTP_202_ACCEPTED)
    async def create_retention_action(
        request: Request,
        csrf: str | None = Header(default=None, alias="X-CSRF-Token"),
        legacy_csrf: str | None = Header(default=None, alias="X-Observatory-CSRF"),
        idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
        confirmation: str | None = Header(default=None, alias="X-Observatory-Confirmation"),
    ) -> JSONResponse:
        session = await owner_session(request)
        try:
            auth_service.require_csrf(
                session, csrf or legacy_csrf, origin=request.headers.get("origin")
            )
            if (
                idempotency_key is None
                or not 16 <= len(idempotency_key) <= 128
                or re.fullmatch(r"[A-Za-z0-9._:-]+", idempotency_key) is None
            ):
                raise ValueError("a valid idempotency key is required")
            raw_body: Any = await request.json()
            if not isinstance(raw_body, dict):
                raise ValueError("retention body contains unsupported fields")
            body = cast(dict[str, Any], raw_body)
            if set(body) - {"action", "cutoff"}:
                raise ValueError("retention body contains unsupported fields")
            action_value = body.get("action")
            if action_value not in {"delete_before", "purge_all"}:
                raise ValueError("retention action is invalid")
            kind = RetentionKind(action_value)
            expected_confirmation = (
                "delete-before" if kind is RetentionKind.DELETE_BEFORE else "purge-all"
            )
            if confirmation is None or confirmation != expected_confirmation:
                raise ValueError("retention confirmation is invalid")
            if kind is RetentionKind.DELETE_BEFORE and "cutoff" not in body:
                raise ValueError("retention cutoff is required")
            if kind is RetentionKind.PURGE_ALL and "cutoff" in body:
                raise ValueError("retention body contains unsupported fields")
            cutoff = _parse_utc_z(body.get("cutoff"))
            action = await app.state.query.retention(
                RetentionRequest(
                    kind,
                    cutoff,
                    confirmation,
                    idempotency_key or "",
                    strict_confirmation=True,
                )
            )
        except RetentionConflictError as exc:
            raise HTTPException(
                status_code=409, detail="idempotency key conflicts with command"
            ) from exc
        except (ValueError, TypeError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail="invalid CSRF token") from exc
        except Exception as exc:
            # Storage/provider exceptions may contain SQL, host, or schema
            # details. Keep the browser response fixed and content-free.
            _LOGGER.error("retention action storage failure")
            raise HTTPException(status_code=503, detail="retention storage unavailable") from exc
        return JSONResponse(action.to_dict(), status_code=status.HTTP_202_ACCEPTED)

    @app.get("/api/v1/retention/actions/{action_id}")
    async def retention_status(action_id: str, request: Request) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_uuid(action_id, "retention action identifier")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        action = await app.state.query.retention_status(action_id)
        if action is None:
            raise HTTPException(status_code=404, detail="retention action not found")
        return JSONResponse(action.to_dict())

    @app.post("/internal/v1/telemetry/observations", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_observations(
        request: Request,
        response: Response,
        authorization: str | None = Header(default=None),
        x_observatory_service_credential: str | None = Header(default=None),
    ) -> JSONResponse:
        del response
        presented = _credential_from_headers(authorization, x_observatory_service_credential)
        try:
            service.authenticate(presented)
        except InvalidCredential as exc:
            raise HTTPException(status_code=401, detail="invalid service credential") from exc
        try:
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > resolved.ingest_max_payload_bytes:
                    await service.record_malformed(credential=presented, reason="payload_too_large")
                    raise HTTPException(status_code=413, detail="telemetry payload too large")
                body.extend(chunk)
            payload = json.loads(body)
        except HTTPException:
            raise
        except (UnicodeDecodeError, ValueError) as exc:
            await service.record_malformed(credential=presented, reason="json")
            raise HTTPException(status_code=400, detail="malformed telemetry payload") from exc
        try:
            summary = await service.ingest(payload, credential=presented)
        except InvalidCredential as exc:
            raise HTTPException(status_code=401, detail="invalid service credential") from exc
        except IngestPayloadError as exc:
            raise HTTPException(status_code=400, detail="malformed telemetry payload") from exc
        if summary["storage_failure"]:
            return JSONResponse(
                status_code=503,
                content=summary,
                headers={"Retry-After": "1"},
            )
        return JSONResponse(status_code=202, content=summary)

    @app.post("/internal/v1/telemetry/v1/metrics", status_code=status.HTTP_202_ACCEPTED)
    @app.post("/internal/v1/telemetry/metrics", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_otlp_metrics(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        return await _ingest_otlp_signal(request, authorization, "metrics", service, resolved)

    @app.post("/internal/v1/telemetry/v1/traces", status_code=status.HTTP_202_ACCEPTED)
    @app.post("/internal/v1/telemetry/traces", status_code=status.HTTP_202_ACCEPTED)
    async def ingest_otlp_traces(
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        return await _ingest_otlp_signal(request, authorization, "traces", service, resolved)

    return app


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _overview_payload(value: dict[str, Any]) -> dict[str, Any]:
    ingest = value.get("ingest")
    raw = cast(dict[str, Any], ingest) if isinstance(ingest, dict) else {}
    counts = {
        "accepted": int(raw.get("accepted", 0)),
        "rejected": int(raw.get("rejected", 0)),
        "duplicate": int(raw.get("duplicate", 0)),
        "malformed": int(raw.get("malformed", 0)),
        "storageFailure": int(raw.get("storageFailure", 0)),
    }
    # An ingest attempt is not an accepted observation.  Keep these health
    # timestamps semantically distinct, including on the first failed batch.
    last_accepted = raw.get("lastAcceptedAt")
    last_failure = raw.get("lastFailureAt")
    current_failure = bool(
        last_failure and (not last_accepted or str(last_failure) > str(last_accepted))
    )
    health_value = value.get("health")
    health = cast(dict[str, Any] | None, health_value) if isinstance(health_value, dict) else None
    health_state = health.get("state") if isinstance(health, dict) else None
    service_state = (
        "unavailable"
        if health_state == "unavailable"
        else "degraded"
        if current_failure or health_state in {"delayed", "partial", "degraded"}
        else "healthy"
    )
    return {
        "observedAt": _now_iso(),
        "systemState": service_state,
        "ingest": {
            "state": service_state,
            "counts": counts,
            "lastAcceptedAt": last_accepted,
            "lastFailureAt": last_failure,
        },
        "observationCount": int(value.get("observationCount", 0)),
        "runCount": int(value.get("runCount", 0)),
        "latestObservationId": value.get("latestObservationId"),
        **({"health": health} if isinstance(health, dict) else {}),
    }


def _health_history_payload(value: dict[str, Any]) -> dict[str, Any]:
    items = value.get("items", [])
    return {
        "items": items if isinstance(items, list) else [],
        "nextCursor": value.get("nextCursor"),
        "snapshotAt": value.get("snapshotAt") or _now_iso(),
    }


def _run_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "runId": value.get("runId"),
        "status": value.get("status", "unknown"),
        "startedAt": value.get("startedAt"),
        "finishedAt": value.get("finishedAt"),
        "durationMs": value.get("durationMs"),
        "model": value.get("model"),
        "deploymentId": value.get("deploymentId", "unknown"),
        "componentIds": value.get("componentIds", []),
        "traceId": value.get("traceId"),
    }


def _run_page_payload(value: dict[str, Any]) -> dict[str, Any]:
    items = cast(list[Any], value.get("items", []))
    return {
        "items": [
            _run_summary(cast(dict[str, Any], item)) for item in items if isinstance(item, dict)
        ],
        "nextCursor": value.get("nextCursor"),
        "snapshotAt": value.get("snapshotAt") or _now_iso(),
        "latestObservationId": value.get("latestObservationId"),
    }


def _run_detail_payload(value: dict[str, Any]) -> dict[str, Any]:
    result = _run_summary(value)
    result["observations"] = value.get("observations", [])
    result["partial"] = bool(value.get("partial", False))
    result["nextCursor"] = value.get("nextCursor")
    return result


def _trace_payload(value: dict[str, Any]) -> dict[str, Any]:
    spans: list[dict[str, Any]] = []

    def flatten(nodes: object, depth: int = 0) -> None:
        if not isinstance(nodes, list):
            return
        for node in cast(list[Any], nodes):
            if not isinstance(node, dict):
                continue
            node_value = cast(dict[str, Any], node)
            spans.append(
                {
                    "spanId": node_value.get("spanId"),
                    "parentSpanId": node_value.get("parentSpanId"),
                    "componentId": node_value.get("componentId"),
                    "operation": node_value.get("operation"),
                    "startedAt": node_value.get("startedAt"),
                    "durationMs": node_value.get("durationMs") or 0,
                    "outcome": node_value.get("outcome") or "unknown",
                    "depth": depth,
                    "partial": bool(node_value.get("partial", False)),
                }
            )
            flatten(node_value.get("children"), depth + 1)

    flatten(value.get("spans"))
    partial = (
        any(bool(span.get("partial", False)) for span in spans)
        or (value.get("nextCursor") is not None)
        or bool(value.get("partial", False))
    )
    return {
        "traceId": value.get("traceId"),
        "runId": value.get("runId"),
        "partial": partial,
        "spans": spans,
        "nextCursor": value.get("nextCursor"),
    }


def _component_collection_payload(
    values: list[dict[str, Any]], health: dict[str, dict[str, Any] | None]
) -> dict[str, Any]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        grouped.setdefault(str(value["componentId"]), []).append(value)
    items: list[dict[str, Any]] = []
    for component_id, versions in sorted(grouped.items()):
        if any("versions" in item for item in versions):
            value = next(item for item in versions if "versions" in item)
            health_value = health.get(component_id)
            items.append(
                {
                    "componentId": component_id,
                    "versions": sorted({str(version) for version in value.get("versions", [])})[
                        :64
                    ],
                    "firstSeenAt": str(value.get("firstSeenAt") or _now_iso()),
                    "lastSeenAt": str(
                        value.get("lastSeenAt") or value.get("firstSeenAt") or _now_iso()
                    ),
                    "health": (
                        health_value.get("summary")
                        if health_value is not None
                        else _health_summary(0, 0, [])
                    ),
                    "definitionState": str(value.get("definitionState", "observed_only")),
                    "definitionVersions": sorted(
                        {str(version) for version in value.get("definitionVersions", [])}
                    )[:64],
                }
            )
            continue
        observations = sum(int(item.get("observations", 0)) for item in versions)
        errors = sum(int(item.get("errorCount", 0)) for item in versions)
        latencies = [
            float(item["latencyMs"]) for item in versions if item.get("latencyMs") is not None
        ]
        latencies.sort()
        health_value = health.get(component_id)
        summary = (
            health_value.get("summary")
            if health_value is not None
            else _health_summary(observations, errors, latencies)
        )
        items.append(
            {
                "componentId": component_id,
                "versions": sorted(
                    str(item["version"])
                    for item in versions
                    if bool(item.get("observed", "observations" in item))
                ),
                "firstSeenAt": min(str(item["firstSeenAt"]) for item in versions),
                "lastSeenAt": max(str(item["lastSeenAt"]) for item in versions),
                "health": summary,
                "definitionState": (
                    "declared"
                    if any(item.get("definitionState") == "declared" for item in versions)
                    else "observed_only"
                ),
                "definitionVersions": sorted(
                    str(item["version"])
                    for item in versions
                    if item.get("definitionState") == "declared"
                ),
            }
        )
    return {"items": items, "snapshotAt": _now_iso()}


def _health_summary(observations: int, errors: int, latencies: list[float]) -> dict[str, Any]:
    return {
        "sampleCount": observations,
        "latencyP50Ms": _percentile(latencies, 0.50),
        "latencyP95Ms": _percentile(latencies, 0.95),
        "errorRate": errors / observations if observations else None,
        "outcomes": {
            "succeeded": observations - errors,
            "failed": errors,
            "cancelled": 0,
            "unknown": 0,
        },
    }


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    upper = min(lower + 1, len(values) - 1)
    return values[lower] + (values[upper] - values[lower]) * (index - lower)


def _deployment_collection_payload(values: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "items": [
            {
                **value,
                "serviceRoles": value.get("serviceRoles", []),
                "lastSeenAt": value.get("lastSeenAt", value.get("firstSeenAt")),
            }
            for value in values
        ],
        "snapshotAt": _now_iso(),
    }


def _validate_uuid(value: str | None, label: str) -> None:
    if value is None:
        return
    if _UUID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")


def _validate_trace_id(value: str, label: str = "trace identifier") -> None:
    if _TRACE_ID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")


def _validate_safe_label(value: str | None, label: str) -> None:
    if value is not None and _SAFE_LABEL_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")


def _validate_component_id(value: str, label: str = "component identifier") -> None:
    if not 6 <= len(value) <= 96 or _COMPONENT_ID_RE.fullmatch(value) is None:
        raise ValueError(f"invalid {label}")


def _validate_run_filter_inputs(
    status_filter: str | None,
    component: str | None,
    model: str | None,
    deployment: str | None,
) -> None:
    if status_filter is not None and status_filter not in _RUN_STATUSES:
        raise ValueError("invalid run status")
    if component is not None:
        _validate_component_id(component)
    _validate_safe_label(model, "model")
    _validate_safe_label(deployment, "deployment")


def _validate_analytics_inputs(
    metric: str,
    unit: str,
    aggregation: str,
    focus: str,
    bucket: str,
    group_by: str,
    component: str | None,
    component_version: str | None,
    operation: str | None,
    deployment: str | None,
    environment: str | None,
    host: str | None,
    service_role: str | None,
    provider: str | None,
    outcome: str | None,
    error_class: str | None,
    status: str | None,
    retrieval_stage: str | None,
) -> None:
    if metric not in ANALYTICS_METRICS or unit not in ANALYTICS_UNITS:
        raise ValueError("invalid analytics metric or unit")
    if aggregation not in ANALYTICS_AGGREGATIONS or focus not in ANALYTICS_FOCUSES:
        raise ValueError("invalid analytics aggregation or focus")
    if bucket != "auto" and bucket not in ANALYTICS_BUCKETS:
        raise ValueError("invalid analytics bucket")
    if group_by not in ANALYTICS_GROUPINGS:
        raise ValueError("invalid analytics grouping")
    if component is not None:
        _validate_component_id(component)
    for value, label in (
        (component_version, "component version"),
        (deployment, "deployment"),
        (host, "host"),
        (provider, "provider"),
        (error_class, "error class"),
        (status, "status"),
        (retrieval_stage, "retrieval stage"),
    ):
        _validate_safe_label(value, label)
    if operation is not None and re.fullmatch(r"[a-z][a-z0-9_.-]{0,95}", operation) is None:
        raise ValueError("invalid operation")
    if environment is not None and re.fullmatch(r"[a-z][a-z0-9_.-]{0,31}", environment) is None:
        raise ValueError("invalid environment")
    if service_role is not None and service_role not in {"api", "worker", "scheduler", "cli"}:
        raise ValueError("invalid service role")
    if outcome is not None and outcome not in {"succeeded", "failed", "cancelled", "unknown"}:
        raise ValueError("invalid outcome")


def _validate_cursor(value: str | None) -> None:
    if value is not None and _CURSOR_RE.fullmatch(value) is None:
        raise ValueError("invalid cursor")


def _validate_execution_cursor(value: str | None) -> None:
    if value is not None and (len(value) > 1024 or _EXECUTION_CURSOR_RE.fullmatch(value) is None):
        raise ValueError("invalid cursor")


def _validate_health_cursor(value: str | None) -> None:
    if value is not None:
        decode_health_cursor(value)


def _parse_history_limit(value: str | None) -> int:
    return _parse_limit(value)


def _parse_time(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("timestamps must be strings")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamps must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ValueError("timestamps must include a timezone")
    return parsed.astimezone(UTC)


def _parse_limit(value: str | None) -> int:
    if value is None:
        return 50
    if not re.fullmatch(r"[0-9]{1,3}", value):
        raise ValueError("page size must be an integer between 1 and 200")
    parsed = int(value)
    if not 1 <= parsed <= 200:
        raise ValueError("page size must be between 1 and 200")
    return parsed


def _parse_bounded_int(value: str | None, *, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError("integer is outside the allowed bound")
    return parsed


def _parse_detail_limit(value: str | None) -> int:
    if value is None:
        return 10000
    if not re.fullmatch(r"[0-9]{1,5}", value):
        raise ValueError("page size must be an integer between 1 and 10000")
    parsed = int(value)
    if not 1 <= parsed <= 10000:
        raise ValueError("page size must be between 1 and 10000")
    return parsed


def _parse_utc_z(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str) or not re.fullmatch(
        r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z",
        value,
    ):
        raise ValueError("retention cutoff must be a UTC timestamp ending in Z")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("retention cutoff is invalid") from exc


async def _ingest_otlp_signal(
    request: Request,
    authorization: str | None,
    signal: str,
    service: TelemetryIngestService,
    settings: Settings,
) -> JSONResponse:
    presented = _credential_from_headers(authorization, None)
    try:
        service.authenticate(presented)
    except InvalidCredential as exc:
        raise HTTPException(status_code=401, detail="invalid service credential") from exc
    try:
        body = await _read_otlp_body(request, settings)
        payload: object = json.loads(body)
        records = (
            metrics_to_observations(payload)
            if signal == "metrics"
            else traces_to_observations(payload)
        )
        summary = await service.ingest({"observations": records}, credential=presented)
    except HTTPException as exc:
        if exc.status_code == 413:
            await service.record_malformed(credential=presented, reason="payload_too_large")
        raise
    except (UnicodeDecodeError, ValueError, OtlpPayloadError) as exc:
        await service.record_malformed(credential=presented, reason=_otlp_rejection_class(exc))
        _LOGGER.warning("OTLP %s rejected class=%s", signal, _otlp_rejection_class(exc))
        raise HTTPException(status_code=400, detail="malformed OTLP telemetry payload") from exc
    if summary["storage_failure"]:
        return JSONResponse(status_code=503, content=summary, headers={"Retry-After": "1"})
    return JSONResponse(status_code=202, content=summary)


def _content_encoding(value: str | None) -> str | None:
    """Accept only one identity or gzip content encoding at the ingest edge."""

    if value is None or not value.strip():
        return None
    encodings = [item.strip().casefold() for item in value.split(",")]
    if len(encodings) != 1 or encodings[0] not in {"identity", "gzip"}:
        raise ValueError("unsupported content encoding")
    return None if encodings[0] == "identity" else "gzip"


async def _read_otlp_body(request: Request, settings: Settings) -> bytes:
    """Read identity/gzip OTLP JSON with independent compressed/decompressed caps."""

    encoding = _content_encoding(request.headers.get("content-encoding"))
    compressed_size = 0
    body = bytearray()
    if encoding is None:
        async for chunk in request.stream():
            compressed_size += len(chunk)
            if compressed_size > settings.ingest_max_payload_bytes:
                raise HTTPException(status_code=413, detail="telemetry payload too large")
            body.extend(chunk)
        return bytes(body)

    decompressor = zlib.decompressobj(wbits=16 + zlib.MAX_WBITS)
    async for chunk in request.stream():
        if not chunk:
            continue
        compressed_size += len(chunk)
        if compressed_size > settings.ingest_max_payload_bytes:
            raise HTTPException(status_code=413, detail="telemetry payload too large")
        if decompressor.eof:
            raise ValueError("trailing gzip data")
        remaining = settings.ingest_max_payload_bytes - len(body)
        try:
            decoded = decompressor.decompress(chunk, remaining + 1)
        except zlib.error as exc:
            raise ValueError("malformed gzip payload") from exc
        if len(body) + len(decoded) > settings.ingest_max_payload_bytes:
            raise HTTPException(status_code=413, detail="telemetry payload too large")
        body.extend(decoded)
        if decompressor.unused_data or decompressor.unconsumed_tail:
            raise ValueError("trailing or oversized gzip data")
    if not decompressor.eof:
        raise ValueError("truncated gzip payload")
    flushed = decompressor.flush()
    if len(body) + len(flushed) > settings.ingest_max_payload_bytes:
        raise HTTPException(status_code=413, detail="telemetry payload too large")
    body.extend(flushed)
    return bytes(body)


def _set_session_cookie(response: Response, settings: Settings, session_id: str) -> None:
    response.set_cookie(
        settings.effective_session_cookie_name,
        session_id,
        max_age=settings.session_ttl_seconds,
        httponly=True,
        secure=settings.secure_cookies,
        samesite="lax",
        path="/",
    )


def _record_auth_callback(app: FastAPI, started: float, stage: str, outcome: str) -> None:
    """Record only bounded callback stage/outcome metadata in the metrics sink."""

    duration = max(0.0, (monotonic() - started) * 1000.0)
    labels = {"stage": stage, "outcome": outcome}
    app.state.query.metrics.record("observatory_auth_callback_duration_ms", duration, labels)
    app.state.query.metrics.record("observatory_auth_callback_outcome", labels=labels)


def _callback_recovery_response(
    settings: Settings, marker: str, return_to: str | None = None
) -> RedirectResponse:
    safe_marker = marker if marker in _AUTH_RECOVERY_MARKERS else "callback_failed"
    target = urlsplit(sanitize_return_to(return_to))
    query = [
        (key, value)
        for key, value in parse_qsl(target.query, keep_blank_values=True)
        if key != _AUTH_RECOVERY_QUERY
    ]
    query.append((_AUTH_RECOVERY_QUERY, safe_marker))
    location = urlunsplit(("", "", target.path or "/", urlencode(query), ""))
    response = RedirectResponse(location, status_code=303)
    _clear_session_cookie(response, settings)
    return response


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Clear the opaque session without serializing an empty quoted value."""

    attributes = "Max-Age=0; Path=/; HttpOnly; SameSite=lax"
    if settings.secure_cookies:
        attributes += "; Secure"
    response.headers.append(
        "set-cookie", f"{settings.effective_session_cookie_name}=; {attributes}"
    )


def _browser_sse_origin_allowed(request: Request, public_origin: str) -> bool:
    """Validate an Origin-less browser EventSource request conservatively."""

    fetch_site = request.headers.getlist("sec-fetch-site")
    if len(fetch_site) != 1 or fetch_site[0] != "same-origin":
        return False
    request_origin = _request_origin_from_trusted_headers(request)
    return request_origin is not None and _origins_match(request_origin, public_origin)


def _request_origin_from_trusted_headers(request: Request) -> str | None:
    """Reconstruct the external origin from one unambiguous proxy/header form."""

    host_values = request.headers.getlist("host")
    if len(host_values) != 1:
        return None
    direct_host = _validate_origin_host(host_values[0])
    if direct_host is None:
        return None

    forwarded_values = request.headers.getlist("forwarded")
    proto_values = request.headers.getlist("x-forwarded-proto")
    forwarded_host_values = request.headers.getlist("x-forwarded-host")
    if len(forwarded_values) > 1 or len(proto_values) > 1 or len(forwarded_host_values) > 1:
        return None
    if forwarded_values and (proto_values or forwarded_host_values):
        return None

    scheme: str | None
    host = direct_host
    if forwarded_values:
        forwarded = forwarded_values[0]
        if not forwarded or "," in forwarded:
            return None
        fields: dict[str, str] = {}
        for item in forwarded.split(";"):
            key, separator, value = item.partition("=")
            key = key.strip().casefold()
            value = value.strip()
            if not separator or not key or not value or key in fields:
                return None
            if any(character in value for character in ('"', "'", "\\")):
                return None
            fields[key] = value
        scheme = _validate_origin_scheme(fields.get("proto"))
        forwarded_host = _validate_origin_host(fields.get("host"))
        if scheme is None or forwarded_host is None:
            return None
        host = forwarded_host
    elif proto_values or forwarded_host_values:
        if len(proto_values) != 1 or len(forwarded_host_values) != 1:
            return None
        scheme = _validate_origin_scheme(proto_values[0])
        forwarded_host = _validate_origin_host(forwarded_host_values[0])
        if scheme is None or forwarded_host is None:
            return None
        host = forwarded_host
    else:
        scheme = _validate_origin_scheme(request.scope.get("scheme"))
        if scheme is None:
            return None
    port = _origin_port(scheme, host)
    if port is None:
        return None
    return f"{scheme}://{host}"


def _validate_origin_scheme(value: object) -> str | None:
    if not isinstance(value, str) or value not in {"http", "https"}:
        return None
    return value


def _validate_origin_host(value: object) -> str | None:
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    if any(character in value for character in (",", "/", "\\", "?", "#", "@")):
        return None
    try:
        parsed = urlsplit("//" + value)
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.netloc != value
        or parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
        or parsed.hostname is None
    ):
        return None
    if port is not None and not 1 <= port <= 65535:
        return None
    hostname = parsed.hostname.casefold()
    if ":" in hostname:
        hostname = f"[{hostname}]"
    return hostname if port is None else f"{hostname}:{port}"


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def _origin_port(scheme: str, host: str) -> int | None:
    try:
        parsed = urlsplit("//" + host)
        port = parsed.port
    except ValueError:
        return None
    return port if port is not None else _default_port(scheme)


def _origins_match(actual: str, expected: str) -> bool:
    actual_parts = urlsplit(actual)
    expected_parts = urlsplit(expected)
    if (
        actual_parts.scheme not in {"http", "https"}
        or expected_parts.scheme not in {"http", "https"}
        or expected_parts.path
        or expected_parts.query
        or expected_parts.fragment
        or expected_parts.username is not None
        or expected_parts.password is not None
        or expected_parts.hostname is None
    ):
        return False
    try:
        expected_port = expected_parts.port or _default_port(expected_parts.scheme)
    except ValueError:
        return False
    return (
        actual_parts.scheme == expected_parts.scheme
        and actual_parts.hostname is not None
        and actual_parts.hostname.casefold() == expected_parts.hostname.casefold()
        and _origin_port(actual_parts.scheme, actual_parts.netloc) == expected_port
    )


def _credential_from_headers(authorization: str | None, explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    return token if separator and scheme.lower() == "bearer" and token else None


def _register_operations_routes(
    app: FastAPI,
    owner_session: Any,
    auth_service: Any,
    resolved: Settings,
    *,
    testing: bool,
) -> None:
    """Register additive owner-only operations without widening legacy routes."""
    from aura_observatory.composition import build_operations_services

    services = build_operations_services(resolved, testing=testing)
    app.state.operations = services

    async def read_owner(request: Request) -> Any:
        return await owner_session(request)

    async def payload(request: Request) -> dict[str, object]:
        try:
            raw = await request.body()
            if len(raw) > 262_144: raise HTTPException(status_code=413, detail="request too large")
            value = json.loads(raw)
        except HTTPException: raise
        except Exception as exc: raise HTTPException(status_code=400, detail="invalid request") from exc
        if not isinstance(value, dict): raise HTTPException(status_code=422, detail="invalid request")
        return cast(dict[str, object], value)

    def page_limit(value: int) -> int:
        if not 1 <= value <= 200:
            raise HTTPException(status_code=422, detail="invalid page size")
        return value

    async def command(request: Request, *, expected_confirmation: str | None = None) -> str:
        session = await owner_session(request)
        csrf = request.headers.get("X-CSRF-Token") or request.headers.get("X-Observatory-CSRF")
        try: auth_service.require_csrf(session, csrf, origin=request.headers.get("origin"))
        except PermissionError as exc: raise HTTPException(status_code=403, detail="invalid CSRF token") from exc
        key = request.headers.get("Idempotency-Key")
        if key is None or not 16 <= len(key) <= 128 or re.fullmatch(r"[A-Za-z0-9._:-]+", key) is None: raise HTTPException(status_code=400, detail="invalid request")
        if expected_confirmation is not None and request.headers.get("X-Observatory-Confirmation") != expected_confirmation: raise HTTPException(status_code=400, detail="invalid request")
        return key

    def no_store(value: object, code: int = 200) -> JSONResponse: return JSONResponse(value, status_code=code, headers={"Cache-Control": "no-store"})

    def translate(error: Exception) -> HTTPException:
        if isinstance(error, KeyError): return HTTPException(status_code=404, detail="resource not found")
        if isinstance(error, RuntimeError): return HTTPException(status_code=409, detail="request conflicts with existing state")
        if isinstance(error, AnnotationTargetNotFound): return HTTPException(status_code=404, detail="resource not found")
        if isinstance(error, AnnotationTargetUnavailable): return HTTPException(status_code=503, detail="service unavailable")
        if isinstance(error, (DashboardError, SavedQueryError, AlertError, IncidentError, AnnotationError, ValueError)): return HTTPException(status_code=422, detail="invalid request")
        return HTTPException(status_code=503, detail="service unavailable")

    @app.get("/api/v1/dashboard-presets")
    async def dashboard_presets(request: Request, page_size: int = 50, cursor: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["dashboard"].presets())

    @app.get("/api/v1/dashboards")
    async def dashboards(request: Request, page_size: int = 50, cursor: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["dashboard"].list(cursor=cursor, limit=page_limit(page_size)))

    @app.post("/api/v1/dashboards", status_code=201)
    async def create_dashboard(request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["dashboard"].create(await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/dashboards/{dashboard_id}/versions/{resource_version}")
    async def get_dashboard(dashboard_id: str, resource_version: str, request: Request) -> JSONResponse:
        await read_owner(request); value = await services["operations"].get_resource("dashboard", dashboard_id, resource_version)
        if value is None: raise HTTPException(status_code=404, detail="resource not found")
        return no_store(value)

    @app.post("/api/v1/dashboards/{dashboard_id}/versions/{resource_version}/delete", status_code=204)
    async def delete_dashboard(dashboard_id: str, resource_version: str, request: Request) -> Response:
        key = await command(request, expected_confirmation="delete-dashboard")
        body = await payload(request)
        if set(body) != {"expectedVersion", "requestedAt"} or body["expectedVersion"] != resource_version: raise HTTPException(status_code=422, detail="invalid request")
        try: _parse_time(body["requestedAt"])
        except ValueError as exc: raise HTTPException(status_code=422, detail="invalid request") from exc
        try: await services["operations"].delete_resource("dashboard", dashboard_id, resource_version, command_id=key)
        except Exception as exc: raise translate(exc) from exc
        return Response(status_code=204)

    @app.get("/api/v1/saved-queries")
    async def saved_queries(request: Request, page_size: int = 50, cursor: str | None = None, kind: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["saved_query"].list(cursor=cursor, limit=page_limit(page_size), kind=kind))

    @app.post("/api/v1/saved-queries", status_code=201)
    async def create_saved_query(request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["saved_query"].create(await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/saved-queries/{query_id}/versions/{resource_version}")
    async def get_saved_query(query_id: str, resource_version: str, request: Request) -> JSONResponse:
        await read_owner(request); value = await services["operations"].get_resource("saved_query", query_id, resource_version)
        if value is None: raise HTTPException(status_code=404, detail="resource not found")
        return no_store(value)

    @app.get("/api/v1/saved-queries/{query_id}/versions/{resource_version}/execute")
    async def execute_saved_query(query_id: str, resource_version: str, request: Request, cursor: str | None = None) -> JSONResponse:
        await read_owner(request)
        try:
            _validate_execution_cursor(cursor)
            value = await services["saved_query_execution"].execute(query_id, resource_version, cursor=cursor)
        except Exception as exc:
            raise translate(exc) from exc
        return no_store(value)

    @app.post("/api/v1/saved-queries/{query_id}/versions/{resource_version}/delete", status_code=204)
    async def delete_saved_query(query_id: str, resource_version: str, request: Request) -> Response:
        key = await command(request, expected_confirmation="delete-saved-query")
        body = await payload(request)
        if set(body) != {"expectedVersion", "requestedAt"} or body["expectedVersion"] != resource_version: raise HTTPException(status_code=422, detail="invalid request")
        try: _parse_time(body["requestedAt"])
        except ValueError as exc: raise HTTPException(status_code=422, detail="invalid request") from exc
        try: await services["operations"].delete_resource("saved_query", query_id, resource_version, command_id=key)
        except Exception as exc: raise translate(exc) from exc
        return Response(status_code=204)

    @app.get("/api/v1/alert-rules")
    async def alert_rules(request: Request, page_size: int = 50, cursor: str | None = None, state: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["alert"].list_rules(cursor=cursor, limit=page_limit(page_size), state=state))

    @app.post("/api/v1/alert-rules", status_code=201)
    async def create_alert_rule(request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["alert"].create_rule(await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/alert-rules/{rule_id}/versions/{resource_version}")
    async def get_alert_rule(rule_id: str, resource_version: str, request: Request) -> JSONResponse:
        await read_owner(request); value = await services["operations"].get_resource("alert_rule", rule_id, resource_version)
        if value is None: raise HTTPException(status_code=404, detail="resource not found")
        return no_store(value)

    @app.post("/api/v1/alert-rules/{rule_id}/versions/{resource_version}/state")
    async def change_alert_rule(rule_id: str, resource_version: str, request: Request) -> JSONResponse:
        key = await command(request, expected_confirmation="change-alert-rule-state")
        body = await payload(request)
        if set(body) != {"expectedVersion", "state", "requestedAt"} or body["expectedVersion"] != resource_version: raise HTTPException(status_code=422, detail="invalid request")
        try: _parse_time(body["requestedAt"])
        except ValueError as exc: raise HTTPException(status_code=422, detail="invalid request") from exc
        try: return no_store(await services["alert"].set_rule_state(rule_id, resource_version, str(body["state"]), command_id=key, expected_version=str(body["expectedVersion"])))
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/alert-instances")
    async def alert_instances(request: Request, page_size: int = 50, cursor: str | None = None, state: str | None = None, severity: str | None = None, rule_id: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["operations"].list_resources("alert_instance", cursor=cursor, limit=page_limit(page_size), state=state, severity=severity, rule_id=rule_id))

    @app.get("/api/v1/alert-instances/{instance_id}")
    async def get_alert_instance(instance_id: str, request: Request) -> JSONResponse:
        await read_owner(request); value = await services["operations"].get_resource("alert_instance", instance_id)
        if value is None: raise HTTPException(status_code=404, detail="resource not found")
        return no_store(value)

    @app.post("/api/v1/alert-instances/{instance_id}/acknowledgements")
    async def acknowledge_alert(instance_id: str, request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["alert"].acknowledge(instance_id, await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/alert-suppressions")
    async def suppressions(request: Request, page_size: int = 50, cursor: str | None = None, state: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["operations"].list_resources("alert_suppression", cursor=cursor, limit=page_limit(page_size), state=state))

    @app.post("/api/v1/alert-suppressions", status_code=201)
    async def create_suppression(request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["alert"].create_suppression(await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.post("/api/v1/alert-suppressions/{suppression_id}/clear")
    async def clear_suppression(suppression_id: str, request: Request) -> JSONResponse:
        key = await command(request, expected_confirmation="clear-suppression")
        body = await payload(request)
        if set(body) != {"expectedVersion", "clearedAt"}: raise HTTPException(status_code=422, detail="invalid request")
        try: _parse_time(body["clearedAt"])
        except ValueError as exc: raise HTTPException(status_code=422, detail="invalid request") from exc
        try: return no_store(await services["alert"].clear_suppression(suppression_id, expected_version=str(body["expectedVersion"]), cleared_at=str(body["clearedAt"]), command_id=key))
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/incidents")
    async def incidents(request: Request, page_size: int = 50, cursor: str | None = None, state: str | None = None, severity: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["operations"].list_resources("incident", cursor=cursor, limit=page_limit(page_size), state=state, severity=severity))

    @app.post("/api/v1/incidents", status_code=201)
    async def create_incident(request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["incident"].create(await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/incidents/{incident_id}")
    async def get_incident(incident_id: str, request: Request) -> JSONResponse:
        await read_owner(request); value = await services["operations"].get_resource("incident", incident_id)
        if value is None: raise HTTPException(status_code=404, detail="resource not found")
        return no_store(value)

    @app.post("/api/v1/incidents/{incident_id}/transitions")
    async def transition_incident(incident_id: str, request: Request) -> JSONResponse:
        body = await payload(request)
        key = await command(request, expected_confirmation="transition-incident")
        try: return no_store(await services["incident"].transition(incident_id, body, command_id=key))
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/annotations")
    async def annotations(request: Request, page_size: int = 50, cursor: str | None = None, target_type: str | None = None, target_id: str | None = None, target_version: str | None = None) -> JSONResponse:
        await read_owner(request); return no_store(await services["annotation"].list(target_type=target_type, target_id=target_id, target_version=target_version, cursor=cursor, limit=page_limit(page_size)))

    @app.post("/api/v1/annotations", status_code=201)
    async def create_annotation(request: Request) -> JSONResponse:
        key = await command(request)
        try: return no_store(await services["annotation"].create(await payload(request), command_id=key), 201)
        except Exception as exc: raise translate(exc) from exc

    @app.get("/api/v1/annotations/{annotation_id}/versions/{resource_version}")
    async def get_annotation(annotation_id: str, resource_version: str, request: Request) -> JSONResponse:
        await read_owner(request); value = await services["operations"].get_resource("annotation", annotation_id, resource_version)
        if value is None: raise HTTPException(status_code=404, detail="resource not found")
        return no_store(value)

    @app.post("/api/v1/annotations/{annotation_id}/versions/{resource_version}/delete", status_code=204)
    async def delete_annotation(annotation_id: str, resource_version: str, request: Request) -> Response:
        key = await command(request, expected_confirmation="delete-annotation")
        body = await payload(request)
        if set(body) != {"expectedVersion", "requestedAt"} or body["expectedVersion"] != resource_version: raise HTTPException(status_code=422, detail="invalid request")
        try: _parse_time(body["requestedAt"])
        except ValueError as exc: raise HTTPException(status_code=422, detail="invalid request") from exc
        try: await services["operations"].delete_resource("annotation", annotation_id, resource_version, command_id=key)
        except Exception as exc: raise translate(exc) from exc
        return Response(status_code=204)

    @app.get("/api/v1/operations/events")
    async def operation_events(request: Request, last_event_id: str | None = Header(default=None, alias="Last-Event-ID")) -> StreamingResponse:
        origin = request.headers.get("origin")
        if origin is not None and origin != resolved.public_origin: raise HTTPException(status_code=403, detail="same-origin request required")
        await read_owner(request)
        async def stream() -> AsyncIterator[str]:
            cursor = last_event_id
            last_reauth = monotonic()
            record = getattr(services.get("metrics"), "record", None)
            setter = getattr(services.get("metrics"), "set", None)
            span = getattr(services.get("metrics"), "record_component_span", None)
            connected_at = monotonic()
            terminal_event = "disconnect"
            terminal_outcome = "success"
            terminal_error: str | None = None
            if callable(record):
                record("observatory_sse_connection_count", 1.0, {"event_type": "connection"})
            if callable(setter):
                setter("observatory_sse_active", 1.0, {"event_type": "stream"})
            elif callable(record):
                record("observatory_sse_active", 1.0, {"event_type": "stream"})
            try:
                while not await request.is_disconnected():
                    if monotonic() - last_reauth >= 15.0:
                        try:
                            await read_owner(request)
                        except Exception:
                            terminal_event = "reauth"
                            terminal_outcome = "failure"
                            terminal_error = "authorization_required"
                            break
                        last_reauth = monotonic()
                    try:
                        events = await services["live"].after(cursor, limit=256)
                    except Exception:
                        terminal_event = "failure"
                        terminal_outcome = "failure"
                        terminal_error = "transport_unavailable"
                        break
                    if events:
                        if callable(record):
                            record("observatory_sse_replay_count", float(len(events)), {"event_type": "operations.changed"})
                            oldest = events[0].get("occurredAt") if isinstance(events[0], Mapping) else None
                            if isinstance(oldest, str):
                                try:
                                    parsed_oldest = _parse_time(oldest)
                                    if parsed_oldest is not None:
                                        lag = max(0.0, (datetime.now(UTC) - parsed_oldest).total_seconds() * 1000.0)
                                        if callable(setter):
                                            setter("observatory_sse_replay_lag_ms", lag, {"event_type": "replay_lag"})
                                        else:
                                            record("observatory_sse_replay_lag_ms", lag, {"event_type": "replay_lag"})
                                except ValueError:
                                    terminal_event = "failure"
                                    terminal_outcome = "failure"
                                    terminal_error = "validation"
                        for event in events:
                            cursor = str(event["eventId"])
                            yield f"id: {event['eventId']}\nevent: operations.changed\ndata: {json.dumps(event, separators=(',', ':'))}\n\n"
                    else:
                        yield ": heartbeat\n\n"
                    if testing:
                        break
                    await asyncio.sleep(1.0)
            except Exception:
                terminal_event = "failure"
                terminal_outcome = "failure"
                terminal_error = "transport_unavailable"
                raise
            finally:
                if callable(record):
                    if terminal_outcome == "failure":
                        record("observatory_sse_failure_count", 1.0, {"event_type": terminal_event})
                    record("observatory_sse_disconnect_count", 1.0, {"event_type": terminal_event})
                if callable(setter):
                    setter("observatory_sse_active", 0.0, {"event_type": "stream"})
                elif callable(record):
                    record("observatory_sse_active", -1.0, {"event_type": "stream"})
                if callable(span):
                    span(component_id="aura.observatory.live_updates", component_version="1.0.0", deployment_id=str(getattr(services.get("metrics"), "deployment_id", "observatory")), operation="live_disconnect", duration_ms=max(0.001, (monotonic() - connected_at) * 1000.0), outcome=terminal_outcome, dependency="postgresql", error_code=terminal_error)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @app.get("/api/v1/operations/history")
    async def operations_history(request: Request, page_size: int = 50, cursor: str | None = None, resource_type: str | None = None, resource_id: str | None = None, resource_version: str | None = None) -> JSONResponse:
        await read_owner(request)
        try:
            return no_store(await services["operations"].list_history(cursor=cursor, limit=page_limit(page_size), resource_type=resource_type, resource_id=resource_id, resource_version=resource_version))
        except Exception as exc:
            raise translate(exc) from exc


app = create_app()

__all__ = ["app", "create_app"]
