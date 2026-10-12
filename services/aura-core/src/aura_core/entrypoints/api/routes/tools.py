"""Authenticated, provider-neutral tool catalog translation."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from fastapi import APIRouter, Depends, Request

from aura_core.entrypoints.api.routes.dependencies import require_session
from aura_core.platform.auth import Session
from aura_core.runtime.tools import ToolRegistry

router = APIRouter(tags=["Tools"])


def _tool_registry(request: Request) -> ToolRegistry:
    """Resolve the composition-owned registry without constructing adapters."""

    return request.app.state.aura.tool_registry


@router.get("/api/v1/tools")
async def list_tools(
    request: Request, _: Session = Depends(require_session)
) -> dict[str, object]:
    # Keep provider payloads, schemas, handlers, and private result details out
    # of the configuration surface.  The runtime registry owns execution;
    # this summary is the stable policy-selection catalog.
    return {"items": _tool_registry(request).summaries()}
