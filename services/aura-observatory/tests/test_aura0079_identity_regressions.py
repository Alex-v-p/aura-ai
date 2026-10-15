"""BFF identity-boundary regressions independent of the platform test file."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
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


def test_only_the_exact_disposable_local_origin_may_use_http() -> None:
    local = Settings()
    assert local.environment == "disposable_local"
    assert local.public_origin == "http://observatory.localhost:4300"
    assert local.secure_cookies is False

    with pytest.raises(ValueError):
        Settings(
            environment="disposable_local",
            public_origin="http://other.localhost:4300",
            oidc_redirect_uri="http://other.localhost:4300/api/v1/auth/callback",
            secure_cookies=False,
        )
    with pytest.raises(ValueError):
        Settings(
            environment="staging",
            public_origin="http://observatory.localhost:4300",
            oidc_redirect_uri="http://observatory.localhost:4300/api/v1/auth/callback",
            secure_cookies=False,
        )


def test_non_disposable_configuration_requires_matching_https_and_secure_cookie() -> None:
    valid = _settings(
        environment="production",
        public_origin="https://observatory.example.invalid",
        oidc_redirect_uri="https://observatory.example.invalid/api/v1/auth/callback",
        secure_cookies=True,
    )
    assert valid.effective_session_cookie_name.startswith("__Host-")
    for overrides in (
        {"public_origin": "http://observatory.example.invalid"},
        {"secure_cookies": False},
        {"owner_subject": ""},
        {"oidc_redirect_uri": "https://other.example.invalid/api/v1/auth/callback"},
    ):
        values: dict[str, Any] = {
            "environment": "production",
            "public_origin": "https://observatory.example.invalid",
            "oidc_redirect_uri": "https://observatory.example.invalid/api/v1/auth/callback",
            "secure_cookies": True,
            "owner_subject": "owner-subject",
        }
        values.update(overrides)
        with pytest.raises(ValueError):
            _settings(**values)


def test_rendered_worker_and_migrate_settings_skip_api_only_bff_validation() -> None:
    compose = yaml.safe_load(Path("compose.yaml").read_text(encoding="utf-8"))
    services = compose["services"]
    for service_name in ("observatory-worker", "observatory-migrate"):
        environment = services[service_name]["environment"]
        settings = Settings(
            role=environment["AURA_OBSERVATORY_ROLE"],
            environment=environment["AURA_OBSERVATORY_ENVIRONMENT"],
            database_url="postgresql://observatory",
            clickhouse_password="runtime-only-test-secret",
        )
        assert settings.role in {"worker", "migrate"}

    api_environment = services["observatory-api"]["environment"]
    api = Settings(
        role=api_environment["AURA_OBSERVATORY_ROLE"],
        environment=api_environment["AURA_OBSERVATORY_ENVIRONMENT"],
        public_origin="https://observatory.example.invalid",
        oidc_issuer="https://identity.example.invalid/application/o/aura-observatory/",
        oidc_redirect_uri="https://observatory.example.invalid/api/v1/auth/callback",
        owner_subject="owner-subject",
        secure_cookies=True,
        database_url="postgresql://observatory",
        clickhouse_password="runtime-only-test-secret",
    )
    assert api.role == "api"


def test_multi_audience_tokens_require_the_observatory_client_as_authorized_party() -> None:
    auth = ObservatoryAuth(_settings(), MemorySessionBackend(), MemoryLoginStateBackend())
    base = {
        "iss": "https://authentik.example.invalid/application/o/aura-observatory/",
        "sub": "owner-subject",
        "aud": ["aura-observatory", "aura-web"],
    }
    with pytest.raises(PermissionError):
        auth.authorize_claims(base)

    accepted = dict(base)
    accepted["azp"] = "aura-observatory"
    assert auth.authorize_claims(accepted).subject == "owner-subject"


@pytest.mark.asyncio
async def test_production_bff_reads_effective_host_cookie_and_rejects_foreign_origin_csrf() -> None:
    settings = _settings(
        environment="production",
        public_origin="https://observatory.example.invalid",
        oidc_redirect_uri="https://observatory.example.invalid/api/v1/auth/callback",
        secure_cookies=True,
    )
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(settings.oidc_issuer, settings.owner_subject)
    )
    app = create_app(settings, auth=auth, testing=True)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="https://observatory.example.invalid",
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        me = await client.get("/api/v1/auth/me")
        assert me.status_code == 200, me.text
        denied = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": "https://evil.example.invalid",
                "X-Observatory-CSRF": session.csrf_token,
            },
        )
        assert denied.status_code == 403
        assert await auth.get_session(session_id) is not None

        allowed = await client.post(
            "/api/v1/auth/logout",
            headers={
                "Origin": settings.public_origin,
                "X-Observatory-CSRF": session.csrf_token,
            },
        )
        assert allowed.status_code == 204
        assert await auth.get_session(session_id) is None
        assert f"{settings.effective_session_cookie_name}=;" in allowed.headers.get(
            "set-cookie", ""
        )
