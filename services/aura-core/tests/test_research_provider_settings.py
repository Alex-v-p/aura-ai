"""Configuration tests for independent research provider switches."""

from __future__ import annotations

import base64

import pytest
from aura_core.platform.research_provider_settings import (
    ResearchProviderSettings,
    capability_enabled,
)
from pydantic import SecretStr


def _key() -> str:
    return base64.urlsafe_b64encode(b"k" * 32).decode().rstrip("=")


def test_research_capabilities_are_disabled_independently_by_default() -> None:
    settings = ResearchProviderSettings()
    assert settings.any_provider_enabled is False
    assert capability_enabled(settings, "admission") is False
    assert capability_enabled(settings, "search") is False
    assert capability_enabled(settings, "static_fetch") is False
    assert capability_enabled(settings, "browser_fetch") is False


def test_each_provider_switch_can_be_enabled_with_a_valid_signing_key() -> None:
    settings = ResearchProviderSettings(
        admission_enabled=True,
        search_enabled=True,
        signing_key=SecretStr(_key()),
    )
    assert settings.admission_enabled is True
    assert settings.search_enabled is True
    assert settings.static_fetch_enabled is False
    assert settings.key_bytes == b"k" * 32


def test_enabled_provider_without_key_fails_closed() -> None:
    with pytest.raises(ValueError, match="signing key"):
        ResearchProviderSettings(search_enabled=True)


@pytest.mark.parametrize(
    "field,value",
    [
        ("search_deadline_seconds", 15.1),
        ("static_fetch_deadline_seconds", 20.1),
        ("browser_fetch_deadline_seconds", 45.1),
        ("search_concurrency", 9),
        ("static_fetch_concurrency", 5),
        ("browser_fetch_concurrency", 3),
        ("max_redirects", 6),
    ],
)
def test_operator_limits_cannot_exceed_adr_ceiling(field: str, value: object) -> None:
    with pytest.raises(ValueError):
        ResearchProviderSettings(**{field: value})  # type: ignore[reportArgumentType]


def test_endpoint_rejects_credentials_and_query_state() -> None:
    with pytest.raises(ValueError):
        ResearchProviderSettings(search_endpoint="http://user:secret@provider:8080")
    with pytest.raises(ValueError):
        ResearchProviderSettings(search_endpoint="http://provider:8080/?debug=true")
