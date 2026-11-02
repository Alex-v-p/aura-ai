from datetime import UTC, datetime, timedelta

import httpx
import pytest
from aura_core.domains.governance.identity.models import (
    OidcLoginState,
    ValidatedIdentity,
)
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import Principal, Settings
from pydantic import ValidationError


class IdentityProvider:
    async def authorization_url(self, *, state: str, nonce: str) -> str:
        return f"https://identity.example/authorize?state={state}&nonce={nonce}"

    async def exchange(self, *, code: str, expected_nonce: str) -> ValidatedIdentity:
        assert code == "authorization-code"
        assert expected_nonce == "nonce"
        return ValidatedIdentity("https://identity.example", "owner", "Owner")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("secure", "origin", "cookie_name"),
    [
        (True, "https://aura.example", "__Host-aura_session"),
        (False, "http://aura.example", "aura_session"),
    ],
)
async def test_cookie_policy_rotates_reads_and_revokes_session(
    secure: bool, origin: str, cookie_name: str
) -> None:
    settings = Settings(
        public_origin=origin,
        oidc_issuer="https://identity.example",
        oidc_client_id="aura-web",
        oidc_client_secret="secret",
        oidc_audience="aura-web",
        oidc_redirect_uri=f"{origin}/api/v1/auth/callback",
        owner_subject="owner",
        secure_cookies=secure,
    )
    app = create_app(settings, testing=True)
    app.state.aura.login.provider = IdentityProvider()
    previous_id, _ = await app.state.aura.sessions.create(
        Principal("https://identity.example", "owner")
    )
    await app.state.aura.oidc_states.put(
        "login-state",
        OidcLoginState(
            "/",
            "nonce",
            datetime.now(UTC) + timedelta(minutes=5),
        ),
        300,
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=origin,
        headers={"Origin": origin},
    )
    client.cookies.set(cookie_name, previous_id)
    try:
        callback = await client.get(
            "/api/v1/auth/callback",
            params={"code": "authorization-code", "state": "login-state"},
        )
        assert callback.status_code == 302
        set_cookie = callback.headers["set-cookie"]
        assert set_cookie.startswith(f"{cookie_name}=")
        assert "HttpOnly" in set_cookie
        assert "SameSite=lax" in set_cookie
        assert "Path=/" in set_cookie
        assert "Domain=" not in set_cookie
        assert ("Secure" in set_cookie) is secure

        rotated_id = callback.cookies.get(cookie_name)
        assert rotated_id is not None and rotated_id != previous_id
        assert await app.state.aura.sessions.get(previous_id) is None
        rotated = await app.state.aura.sessions.get(rotated_id)
        assert rotated is not None
        assert (await client.get("/api/v1/auth/session")).status_code == 200

        logout = await client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": rotated.csrf_token},
        )
        assert logout.status_code == 204
        deleted_cookie = logout.headers["set-cookie"]
        assert deleted_cookie.startswith(f"{cookie_name}=")
        assert "Max-Age=0" in deleted_cookie
        assert "HttpOnly" in deleted_cookie
        assert "SameSite=lax" in deleted_cookie
        assert "Path=/" in deleted_cookie
        assert "Domain=" not in deleted_cookie
        assert ("Secure" in deleted_cookie) is secure
        assert await app.state.aura.sessions.get(rotated_id) is None
    finally:
        await client.aclose()


def test_production_cannot_disable_secure_host_cookie() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            role="api",
            public_origin="https://aura.example",
            oidc_issuer="https://identity.example",
            oidc_redirect_uri="https://aura.example/api/v1/auth/callback",
            owner_subject="owner",
            secure_cookies=False,
        )
