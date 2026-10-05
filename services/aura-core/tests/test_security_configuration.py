from pathlib import Path

import pytest
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import (
    MemorySessionBackend,
    Principal,
    ProviderTokens,
    SessionService,
    Settings,
)
from pydantic import ValidationError


def test_runtime_secret_files_are_encoded_into_transport_urls(tmp_path: Path) -> None:
    nats_secret = tmp_path / "nats"
    valkey_secret = tmp_path / "valkey"
    nats_secret.write_text("nats p@ss/word\n", encoding="utf-8")
    valkey_secret.write_text("valkey:p@ss\n", encoding="utf-8")

    settings = Settings(
        nats_url="nats://bus.internal:4222",
        nats_user="aura",
        nats_password_file=str(nats_secret),
        valkey_url="redis://cache.internal:6379/0",
        valkey_password_file=str(valkey_secret),
    )

    assert settings.nats_url == "nats://aura:nats%20p%40ss%2Fword@bus.internal:4222"
    assert settings.valkey_url == "redis://:valkey%3Ap%40ss@cache.internal:6379/0"


def test_context_budget_has_validated_default_and_override() -> None:
    assert Settings().context_token_budget == 8192
    overridden = Settings(context_token_budget=4096)
    assert overridden.context_token_budget == 4096
    assert AppState(overridden, testing=True).coordinator.context_token_budget == 4096
    with pytest.raises(ValidationError):
        Settings(context_token_budget=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oidc_issuer", "https://localhost/oidc"),
        ("public_origin", "http://aura.example"),
        ("public_origin", "https://aura.example/unexpected-path"),
        ("owner_subject", ""),
    ],
)
def test_production_api_identity_configuration_fails_closed(field: str, value: str) -> None:
    values = {
        "environment": "production",
        "role": "api",
        "oidc_issuer": "https://identity.example",
        "public_origin": "https://aura.example",
        "oidc_redirect_uri": "https://aura.example/api/v1/auth/callback",
        "owner_subject": "owner-123",
        field: value,
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("oidc_redirect_uri", "https://other.example/api/v1/auth/callback"),
        ("secure_cookies", False),
    ],
)
def test_production_api_requires_exact_callback_and_secure_cookie(
    field: str, value: object
) -> None:
    values: dict[str, object] = {
        "environment": "production",
        "role": "api",
        "oidc_issuer": "https://identity.example",
        "public_origin": "https://aura.example",
        "oidc_redirect_uri": "https://aura.example/api/v1/auth/callback",
        "owner_subject": "owner-123",
        "secure_cookies": True,
        field: value,
    }
    with pytest.raises(ValidationError):
        Settings.model_validate(values)


@pytest.mark.asyncio
async def test_provider_tokens_remain_in_server_side_session() -> None:
    service = SessionService(MemorySessionBackend(), Settings())
    session_id, created = await service.create(
        Principal("https://identity.example", "owner"),
        ProviderTokens("access-secret", "refresh-secret", "Bearer"),
    )

    loaded = await service.get(session_id)

    assert loaded is not None
    assert loaded.principal == created.principal
    assert loaded.provider_tokens is not None
    assert loaded.provider_tokens.refresh_token == "refresh-secret"
