"""Liveness and dependency-readiness HTTP translations."""

import json

from fastapi import APIRouter, Request, Response, status

from aura_core.entrypoints.api.routes.conversations import state

router = APIRouter(tags=["Health"])


@router.get("/health/live")
async def liveness() -> dict[str, str]:
    return {"status": "alive"}


@router.get("/health/ready")
async def readiness(request: Request) -> Response:
    report = await state(request).readiness.check()
    payload = {
        "status": report.status,
        "checks": {
            name: {"status": check.status, "detail": check.detail}
            for name, check in report.checks.items()
        },
        "checkedAt": report.checked_at.isoformat(),
    }
    return Response(
        content=json.dumps(payload),
        media_type="application/json",
        status_code=200 if report.status == "ready" else status.HTTP_503_SERVICE_UNAVAILABLE,
    )
