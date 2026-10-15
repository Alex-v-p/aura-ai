"""Thin FastAPI composition root for private metadata ingestion."""

from __future__ import annotations

import json

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse

from aura_observatory.composition import build_ingest_service
from aura_observatory.ingest import IngestPayloadError, InvalidCredential, TelemetryIngestService
from aura_observatory.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    ingest_service: TelemetryIngestService | None = None,
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
    app = FastAPI(title="Aura Observatory API", version="1.0.0")
    app.state.ingest = service

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok", "service": "aura-observatory"}

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

    return app


def _credential_from_headers(authorization: str | None, explicit: str | None) -> str | None:
    if explicit is not None:
        return explicit
    if authorization is None:
        return None
    scheme, separator, token = authorization.partition(" ")
    return token if separator and scheme.lower() == "bearer" and token else None


app = create_app()

__all__ = ["app", "create_app"]
