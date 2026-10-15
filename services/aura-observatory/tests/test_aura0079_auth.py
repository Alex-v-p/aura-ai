"""AURA-0079 owner-only Observatory authentication and BFF session tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
    RedisLoginStateBackend,
    RedisSessionBackend,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.settings import Settings


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "oidc_issuer": "https://authentik.example.invalid/application/o/aura-observatory/",
        "oidc_audience": "aura-observatory",
        "oidc_client_id": "aura-observatory",
        "owner_subject": "owner-subject",
    }
    values.update(overrides)
    return Settings(**cast(Any, values))


@pytest.mark.asyncio
async def test_login_state_is_single_use_and_return_to_is_local_only() -> None:
    auth = ObservatoryAuth(_settings(), MemorySessionBackend(), MemoryLoginStateBackend())

    state, nonce = await auth.begin_login("https://evil.invalid/exfiltrate")
    stored = await auth.login_states.consume(state)
    assert stored is not None
    assert stored.return_to == "/"
    assert stored.nonce == nonce
    assert await auth.login_states.consume(state) is None


def test_authorize_claims_requires_exact_observatory_issuer_audience_and_owner() -> None:
    auth = ObservatoryAuth(_settings(), MemorySessionBackend(), MemoryLoginStateBackend())
    base = {
        "iss": "https://authentik.example.invalid/application/o/aura-observatory/",
        "sub": "owner-subject",
        "aud": ["aura-observatory"],
    }

    principal = auth.authorize_claims(base)
    assert principal == Principal(cast(str, base["iss"]), "owner-subject")

    for key, value in (
        ("iss", "https://authentik.example.invalid/application/o/aura-web/"),
        ("sub", "another-subject"),
        ("aud", ["aura-web"]),
    ):
        claims = dict(base)
        claims[key] = value
        with pytest.raises(PermissionError):
            auth.authorize_claims(claims)


@pytest.mark.asyncio
async def test_sessions_keep_provider_tokens_server_side_and_rotate_on_login() -> None:
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(_settings(), sessions, MemoryLoginStateBackend())
    principal = Principal(
        "https://authentik.example.invalid/application/o/aura-observatory",
        "owner-subject",
    )

    first_id, first = await auth.create_session(principal, {"access_token": "private"})
    second_id, second = await auth.create_session(principal, {"access_token": "private"})
    assert first_id != second_id
    assert first.csrf_token != second.csrf_token
    loaded = await auth.get_session(first_id)
    assert loaded is not None
    assert loaded.provider_tokens == {"access_token": "private"}

    # Tokens are available only through the backend, never as part of the
    # opaque browser identifier or a login redirect.
    assert "private" not in first_id
    assert "private" not in second_id
    assert "access_token" not in first_id


@pytest.mark.asyncio
async def test_csrf_is_required_for_state_changes_and_logout_revokes_session() -> None:
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(_settings(), sessions, MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal("issuer", "owner-subject"))

    with pytest.raises(PermissionError):
        auth.require_csrf(session, None)
    with pytest.raises(PermissionError):
        auth.require_csrf(session, "wrong")
    auth.require_csrf(session, session.csrf_token)

    await auth.revoke(session_id)
    assert await auth.get_session(session_id) is None


def test_production_settings_require_stable_https_origin_secure_cookie_and_owner() -> None:
    valid = _settings(
        environment="production",
        public_origin="https://observatory.example.invalid",
        oidc_redirect_uri="https://observatory.example.invalid/api/v1/auth/callback",
        secure_cookies=True,
    )
    assert valid.effective_session_cookie_name.startswith("__Host-")

    for overrides in (
        {"public_origin": "http://observatory.example.invalid"},
        {"public_origin": "https://observatory.localhost"},
        {"public_origin": "https://127.0.0.1"},
        {"secure_cookies": False},
        {"owner_subject": ""},
        {"oidc_redirect_uri": "https://different.example.invalid/callback"},
    ):
        with pytest.raises(ValueError):
            production_values: dict[str, object] = {
                "environment": "production",
                "public_origin": "https://observatory.example.invalid",
                "oidc_redirect_uri": "https://observatory.example.invalid/api/v1/auth/callback",
                "secure_cookies": True,
            }
            production_values.update(overrides)
            _settings(
                **production_values,
            )


def test_session_and_login_state_settings_have_safe_bounds() -> None:
    for field, values in (
        ("session_ttl_seconds", (299, 2_592_001)),
        ("login_state_ttl_seconds", (59, 901)),
    ):
        for value in values:
            with pytest.raises(ValueError):
                _settings(**{field: value})


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    async def setex(self, key: str, ttl: int, value: str) -> None:
        self.values[key] = value
        self.ttls[key] = ttl

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)


@pytest.mark.asyncio
async def test_redis_session_and_oidc_state_use_a_dedicated_namespace() -> None:
    redis = FakeRedis()
    sessions = RedisSessionBackend(redis, namespace="aura:observatory-test")
    login_states = RedisLoginStateBackend(redis, namespace="aura:observatory-test")
    principal = Principal("issuer", "owner-subject")
    now = datetime.now(UTC)
    from aura_observatory.auth import LoginState, Session

    await sessions.put(
        "opaque-session",
        Session(principal, "csrf", now + timedelta(hours=1), now + timedelta(hours=2)),
        3600,
    )
    await login_states.put(
        "opaque-state",
        LoginState("nonce", "/", now + timedelta(minutes=5)),
        300,
    )
    assert set(redis.values) == {
        key for key in redis.values if key.startswith("aura:observatory-test:")
    }
    assert all("opaque-session" not in key and "opaque-state" not in key for key in redis.values)


@pytest.mark.asyncio
async def test_bff_logout_requires_csrf_and_clears_the_rotated_session_cookie() -> None:
    settings = _settings()
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject)
    )
    app = create_app(settings, auth=auth, testing=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport, base_url="http://observatory.localhost:4300"
    ) as client:
        unauthorized = await client.get("/api/v1/auth/me")
        assert unauthorized.status_code == 401

        client.cookies.set(settings.effective_session_cookie_name, session_id)
        denied = await client.post("/api/v1/auth/logout")
        assert denied.status_code == 403

        allowed = await client.post(
            "/api/v1/auth/logout", headers={"X-Observatory-CSRF": session.csrf_token}
        )
        assert allowed.status_code == 204
        assert await auth.get_session(session_id) is None
        assert "observatory_session=;" in allowed.headers.get("set-cookie", "")


def test_bff_cookie_settings_never_put_bearer_credentials_in_urls() -> None:
    source = (Path(__file__).parents[1] / "src/aura_observatory/entrypoints/api/app.py").read_text(
        encoding="utf-8"
    )
    assert "access_token" not in source.split("def _set_session_cookie", 1)[1]
    assert "refresh_token" not in source.split("def _set_session_cookie", 1)[1]
    assert 'samesite="lax"' in source
    assert "httponly=True" in source
