"""OIDC BFF route translations."""

# FastAPI dependency markers are intentionally declared at the transport edge.
# ruff: noqa: B008

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import RedirectResponse

from aura_core.domains.governance.identity.application import (
    IdentityConfigurationError,
    IdentityValidationError,
    InvalidLoginState,
)
from aura_core.entrypoints.api.routes.conversations import state as app_state
from aura_core.entrypoints.api.routes.dependencies import require_csrf, require_session
from aura_core.platform.auth import Session

router = APIRouter(prefix="/api/v1/auth", tags=["Authentication"])


@router.get("/session")
async def get_session(session: Session = Depends(require_session)) -> dict[str, object]:
    return {
        "principal": {
            "issuer": session.principal.issuer,
            "subject": session.principal.subject,
            "displayName": session.principal.display_name,
        },
        "csrfToken": session.csrf_token,
        "idleExpiresAt": session.idle_expires_at.isoformat(),
        "absoluteExpiresAt": session.absolute_expires_at.isoformat(),
    }


@router.get("/login")
async def start_login(request: Request, return_to: str = "/") -> Response:
    try:
        result = await app_state(request).login.start(return_to)
    except IdentityValidationError as exc:
        raise HTTPException(status_code=503, detail="identity provider unavailable") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IdentityConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return RedirectResponse(result.authorization_url, status_code=302)


@router.get("/callback")
async def complete_login(request: Request, code: str, state: str) -> Response:
    settings = app_state(request).settings
    cookie = settings.session_cookie
    try:
        result = await app_state(request).login.complete(
            code,
            state,
            request.cookies.get(cookie.name),
        )
    except InvalidLoginState as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IdentityConfigurationError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except IdentityValidationError as exc:
        raise HTTPException(status_code=403, detail="identity validation failed") from exc
    response = RedirectResponse(result.return_to, status_code=302)
    response.set_cookie(
        cookie.name,
        result.session_id,
        secure=cookie.secure,
        httponly=cookie.httponly,
        samesite=cookie.samesite,
        path=cookie.path,
    )
    return response


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request, response: Response, session: Session = Depends(require_csrf)
) -> None:
    del session
    settings = app_state(request).settings
    cookie = settings.session_cookie
    await app_state(request).login.logout(request.cookies.get(cookie.name))
    response.delete_cookie(
        cookie.name,
        path=cookie.path,
        secure=cookie.secure,
        httponly=cookie.httponly,
        samesite=cookie.samesite,
    )
