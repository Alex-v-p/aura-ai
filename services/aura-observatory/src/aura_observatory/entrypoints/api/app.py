"""Thin FastAPI composition root for private metadata ingestion."""

from __future__ import annotations

import json

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse

from aura_observatory.auth import ObservatoryAuth
from aura_observatory.composition import build_auth, build_ingest_service
from aura_observatory.ingest import IngestPayloadError, InvalidCredential, TelemetryIngestService
from aura_observatory.otlp import OtlpPayloadError, metrics_to_observations, traces_to_observations
from aura_observatory.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    ingest_service: TelemetryIngestService | None = None,
    auth: ObservatoryAuth | None = None,
    testing: bool = False,
) -> FastAPI:
    """Build the API with an explicit service dependency for deterministic tests."""

    resolved = settings or Settings()
    if ingest_service is not None:
        service = ingest_service
    elif testing:
        from aura_observatory.adapters.memory import MemoryCatalog, MemoryObservationStore

        service = TelemetryIngestService(
            MemoryObservationStore(),
            MemoryCatalog(),
            credential=resolved.ingest_credential(),
            max_batch_size=resolved.ingest_max_batch_size,
        )
    else:
        service = build_ingest_service(resolved)
    auth_service = auth or build_auth(resolved, testing=testing)
    app = FastAPI(title="Aura Observatory API", version="1.0.0")
    app.state.ingest = service
    app.state.auth = auth_service

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "aura-observatory"}

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
        return JSONResponse(
            {
                "subject": session.principal.subject,
                "displayName": session.principal.display_name,
                "csrfToken": session.csrf_token,
            }
        )

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
