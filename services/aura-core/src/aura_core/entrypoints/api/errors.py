"""Central RFC 7807-compatible API error envelope."""

import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


def _envelope(
    request: Request,
    status: int,
    code: str,
    detail: str,
    *,
    retryable: bool = False,
    field_errors: list[dict[str, str]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"urn:aura:problem:{code.lower()}",
        "title": code.replace("_", " ").title(),
        "status": status,
        "code": code,
        "detail": detail[:1000],
        "retryable": retryable,
        "traceId": secrets.token_hex(16),
        "resource": str(request.url.path)[:255],
    }
    if field_errors:
        body["fieldErrors"] = field_errors
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    mapping = {
        400: "BAD_REQUEST",
        401: "UNAUTHENTICATED",
        403: "FORBIDDEN",
        404: "NOT_FOUND",
        409: "CONFLICT",
        410: "GONE",
        422: "UNPROCESSABLE_ENTITY",
        503: "DEPENDENCY_UNAVAILABLE",
    }
    return _envelope(
        request, exc.status_code, mapping.get(exc.status_code, "REQUEST_FAILED"), str(exc.detail)
    )


async def starlette_exception_handler(
    request: Request, exc: StarletteHTTPException
) -> JSONResponse:
    compatible = HTTPException(status_code=exc.status_code, detail=exc.detail)
    return await http_exception_handler(request, compatible)


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    del exc
    return _envelope(
        request,
        500,
        "INTERNAL_ERROR",
        "The request could not be completed.",
        retryable=True,
    )


async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    fields = [
        {
            "field": ".".join(str(part) for part in error.get("loc", [])),
            "code": "INVALID_VALUE",
            "message": str(error.get("msg", "invalid value")),
        }
        for error in exc.errors()
    ]
    return _envelope(
        request, 422, "VALIDATION_FAILED", "Request validation failed.", field_errors=fields
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, starlette_exception_handler)  # type: ignore[arg-type]
    handler: Any = validation_exception_handler
    app.add_exception_handler(RequestValidationError, handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
