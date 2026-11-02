"""HTTP authentication and state dependencies."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from fastapi import Depends, Header, HTTPException, Request, status

from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session


def app_state(request: Request) -> AppState:
    return request.app.state.aura


async def require_session(request: Request) -> Session:
    state = app_state(request)
    if not state.settings.oidc_issuer or not state.settings.owner_subject:
        raise HTTPException(status_code=503, detail="identity authorization is not configured")
    session = await state.sessions.get(request.cookies.get(state.settings.session_cookie.name))
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="authentication required"
        )
    if (
        session.principal.issuer != state.settings.oidc_issuer
        or session.principal.subject != state.settings.owner_subject
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="owner authorization required"
        )
    return session


async def require_csrf(
    request: Request,
    session: Session = Depends(require_session),
    x_csrf_token: str | None = Header(default=None),
) -> Session:
    if x_csrf_token is None or not secrets_equal(x_csrf_token, session.csrf_token):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf validation failed")
    origin = request.headers.get("origin")
    if origin is None or origin.rstrip("/") != app_state(request).settings.public_origin.rstrip(
        "/"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="origin validation failed"
        )
    return session


def secrets_equal(left: str, right: str) -> bool:
    import hmac

    return hmac.compare_digest(left.encode(), right.encode())
