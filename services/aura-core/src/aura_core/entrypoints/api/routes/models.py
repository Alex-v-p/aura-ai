"""Model inventory route."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from fastapi import APIRouter, Depends, Request

from aura_core.entrypoints.api.routes.conversations import state
from aura_core.entrypoints.api.routes.dependencies import require_session
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/models", tags=["Models"])


@router.get("")
async def list_models(
    request: Request, session: Session = Depends(require_session)
) -> dict[str, object]:
    del session
    models, default = await state(request).models()
    return {
        "models": [
            {
                "id": model.id,
                "displayName": model.display_name,
                "provider": model.provider,
                "capabilities": list(model.capabilities),
                "availability": model.availability,
                "selectable": model.selectable,
                "disabledReason": model.disabled_reason,
            }
            for model in models
        ],
        "defaultModelId": default,
        "observedAt": __import__("datetime")
        .datetime.now(__import__("datetime").timezone.utc)
        .isoformat(),
    }
