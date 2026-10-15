"""Thin FastAPI composition root for private metadata ingestion."""

from __future__ import annotations

import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import httpx
from fastapi import FastAPI, Header, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, StreamingResponse

from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.auth import ObservatoryAuth
from aura_observatory.composition import build_auth, build_ingest_service
from aura_observatory.ingest import IngestPayloadError, InvalidCredential, TelemetryIngestService
from aura_observatory.otlp import OtlpPayloadError, metrics_to_observations, traces_to_observations
from aura_observatory.query import (
    ObservatoryQueryService,
    RetentionConflictError,
    RetentionKind,
    RetentionRequest,
    RunFilters,
)
from aura_observatory.settings import Settings

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_SAFE_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT_ID_RE = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]{16,1024}$")
_RUN_STATUSES = frozenset({"queued", "running", "succeeded", "failed", "cancelled", "unknown"})


def create_app(
    settings: Settings | None = None,
    *,
    ingest_service: TelemetryIngestService | None = None,
    auth: ObservatoryAuth | None = None,
    query_store: object | None = None,
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
    app = FastAPI(title="Aura Observatory API", version="1.0.0")
    app.state.ingest = service
    app.state.auth = auth_service

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
                    "traceId": uuid4().hex,
                },
            )
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})

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
                            "traceId": uuid4().hex,
                        },
                    )
                if resolved.owner_subject and session.principal.subject != resolved.owner_subject:
                    return JSONResponse(
                        status_code=403,
                        content={
                            "code": "forbidden",
                            "message": "owner authorization required",
                            "retryable": False,
                            "traceId": uuid4().hex,
                        },
                    )
            return JSONResponse(
                status_code=422,
                content={
                    "code": "invalid_request",
                    "message": "request validation failed",
                    "retryable": False,
                    "traceId": uuid4().hex,
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

    @app.get("/api/v1/auth/callback")
    async def callback(code: str, state: str, request: Request) -> RedirectResponse:
        login_state = await auth_service.login_states.consume(state)
        if login_state is None:
            raise HTTPException(status_code=400, detail="invalid or expired login state")
        try:
            tokens, claims = await auth_service.authenticate_code(code, login_state.nonce)
            principal = auth_service.authorize_claims(claims)
            prior_session = request.cookies.get(resolved.effective_session_cookie_name)
            session_id, _session = await auth_service.create_session(
                principal, tokens, replace_session_id=prior_session
            )
        except (httpx.HTTPError, KeyError, TypeError, ValueError, PermissionError) as exc:
            raise HTTPException(status_code=401, detail="OIDC authentication failed") from exc
        redirect = RedirectResponse(login_state.return_to, status_code=303)
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

    @app.get("/api/v1/overview")
    async def overview(request: Request) -> JSONResponse:
        await owner_session(request)
        return JSONResponse(_overview_payload(await app.state.query.overview()))

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
    async def components(request: Request) -> JSONResponse:
        await owner_session(request)
        values = await app.state.query.components()
        health = {
            str(value["componentId"]): await app.state.query.component_health(
                str(value["componentId"])
            )
            for value in values
        }
        return JSONResponse(_component_collection_payload(values, health))

    @app.get("/api/v1/components/{component_id:path}/health")
    async def component_health(
        component_id: str,
        request: Request,
        from_time: str | None = Query(default=None, alias="from"),
        to_time: str | None = Query(default=None, alias="to"),
    ) -> JSONResponse:
        await owner_session(request)
        try:
            _validate_component_id(component_id)
            health = await app.state.query.component_health(
                component_id,
                from_time=_parse_time(from_time),
                to_time=_parse_time(to_time),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid request") from exc
        if health is None:
            raise HTTPException(status_code=404, detail="component not found")
        return JSONResponse(health)

    @app.get("/api/v1/deployments")
    async def deployments(request: Request) -> JSONResponse:
        await owner_session(request)
        return JSONResponse(_deployment_collection_payload(await app.state.query.deployments()))

    @app.get("/api/v1/events")
    async def events(
        request: Request,
        last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    ) -> StreamingResponse:
        origin = request.headers.get("origin")
        if origin != resolved.public_origin and not (testing and origin is None):
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
                    "resourceType": "observation",
                    "resourceId": data.get("observationId"),
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
                    await service.record_malformed(credential=presented)
                    raise HTTPException(status_code=413, detail="telemetry payload too large")
                body.extend(chunk)
            payload = json.loads(body)
        except HTTPException:
            raise
        except (UnicodeDecodeError, ValueError) as exc:
            await service.record_malformed(credential=presented)
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
    return {
        "observedAt": _now_iso(),
        "systemState": "degraded" if current_failure else "healthy",
        "ingest": {
            "state": "degraded" if current_failure else "healthy",
            "counts": counts,
            "lastAcceptedAt": last_accepted,
            "lastFailureAt": last_failure,
        },
        "observationCount": int(value.get("observationCount", 0)),
        "runCount": int(value.get("runCount", 0)),
        "latestObservationId": value.get("latestObservationId"),
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
    partial = any(bool(span.get("partial", False)) for span in spans) or (
        value.get("nextCursor") is not None
    ) or bool(value.get("partial", False))
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
                "versions": sorted(str(item["version"]) for item in versions),
                "firstSeenAt": min(str(item["firstSeenAt"]) for item in versions),
                "lastSeenAt": max(str(item["lastSeenAt"]) for item in versions),
                "health": summary,
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


def _validate_cursor(value: str | None) -> None:
    if value is not None and _CURSOR_RE.fullmatch(value) is None:
        raise ValueError("invalid cursor")


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
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > settings.ingest_max_payload_bytes:
                await service.record_malformed(credential=presented)
                raise HTTPException(status_code=413, detail="telemetry payload too large")
            body.extend(chunk)
        payload: object = json.loads(body)
        records = (
            metrics_to_observations(payload)
            if signal == "metrics"
            else traces_to_observations(payload)
        )
        summary = await service.ingest({"observations": records}, credential=presented)
    except HTTPException:
        raise
    except (UnicodeDecodeError, ValueError, OtlpPayloadError) as exc:
        await service.record_malformed(credential=presented)
        raise HTTPException(status_code=400, detail="malformed OTLP telemetry payload") from exc
    if summary["storage_failure"]:
        return JSONResponse(status_code=503, content=summary, headers={"Retry-After": "1"})
    return JSONResponse(status_code=202, content=summary)


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


def _clear_session_cookie(response: Response, settings: Settings) -> None:
    """Clear the opaque session without serializing an empty quoted value."""

    attributes = "Max-Age=0; Path=/; HttpOnly; SameSite=lax"
    if settings.secure_cookies:
        attributes += "; Secure"
    response.headers.append(
        "set-cookie", f"{settings.effective_session_cookie_name}=; {attributes}"
    )


def _credential_from_headers(authorization: str | None, explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    return token if separator and scheme.lower() == "bearer" and token else None


app = create_app()

__all__ = ["app", "create_app"]
