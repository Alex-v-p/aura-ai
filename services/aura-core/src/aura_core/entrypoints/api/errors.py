"""Central RFC 7807-compatible API error envelope."""

import secrets
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class CalendarApiError(Exception):
    """Closed, owner-safe calendar problem translated at the API boundary."""

    def __init__(
        self,
        status: int,
        category: str,
        recovery_action: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.status = status
        self.category = category
        self.recovery_action = recovery_action
        self.retryable = retryable
        super().__init__(category)


def _calendar_title(category: str) -> str:
    return {
        "authorization": "Calendar authorization required",
        "validation": "Invalid calendar request",
        "callback_rejected": "Calendar authorization was rejected",
        "provider_auth": "Calendar authorization required",
        "provider_rate_limit": "Calendar provider is rate limited",
        "provider_unavailable": "Calendar provider unavailable",
        "calendar_unavailable": "Calendar is unavailable",
        "dependency_unavailable": "Calendar dependency unavailable",
        "deadline_exceeded": "Calendar request timed out",
        "revocation_uncertain": "Calendar disconnect is pending",
    }.get(category, "Calendar request failed")


async def calendar_exception_handler(request: Request, exc: CalendarApiError) -> JSONResponse:
    del request
    body = {
        "type": f"urn:aura:problem:calendar_{exc.category}",
        "title": _calendar_title(exc.category),
        "status": exc.status,
        "category": exc.category,
        "detail": {
            "authorization": "Reconnect the calendar to continue.",
            "validation": "Review the calendar request and try again.",
            "callback_rejected": "Restart calendar connection and try again.",
            "provider_auth": "Reconnect the calendar to continue.",
            "provider_rate_limit": "Wait briefly and try again.",
            "provider_unavailable": "Try again later.",
            "calendar_unavailable": "Check the calendar connection and try again.",
            "dependency_unavailable": "Try again later.",
            "deadline_exceeded": "Narrow the request and try again.",
            "revocation_uncertain": "Retry disconnect later.",
        }.get(exc.category, "Try again later."),
        "retryable": exc.retryable,
        "recoveryAction": exc.recovery_action,
        "traceId": secrets.token_hex(16),
    }
    return JSONResponse(
        body,
        status_code=exc.status,
        media_type="application/problem+json",
        headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
    )


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
    if request.url.path.startswith("/api/v1/integrations/calendar"):
        category = (
            "authorization"
            if exc.status_code in {401, 403}
            else "dependency_unavailable"
            if exc.status_code >= 500
            else "validation"
        )
        action = "connect" if category == "authorization" else "retry"
        return await calendar_exception_handler(
            request,
            CalendarApiError(
                exc.status_code,
                category,
                action,
                retryable=exc.status_code >= 500,
            ),
        )
    if exc.status_code == 409 and exc.detail == "model_vision_required":
        return _envelope(
            request,
            409,
            "model_vision_required",
            "The selected model requires verified vision capability.",
        )
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
    if request.url.path.startswith("/api/v1/integrations/calendar"):
        return await calendar_exception_handler(
            request,
            CalendarApiError(500, "dependency_unavailable", "retry", retryable=True),
        )
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
    if request.url.path.startswith("/api/v1/integrations/calendar"):
        return await calendar_exception_handler(
            request,
            CalendarApiError(422, "validation", "retry", retryable=False),
        )
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
    app.add_exception_handler(CalendarApiError, calendar_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(HTTPException, http_exception_handler)  # type: ignore[arg-type]
    app.add_exception_handler(StarletteHTTPException, starlette_exception_handler)  # type: ignore[arg-type]
    handler: Any = validation_exception_handler
    app.add_exception_handler(RequestValidationError, handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
