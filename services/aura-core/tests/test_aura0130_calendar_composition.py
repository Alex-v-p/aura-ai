"""Enabled/disabled calendar composition tests using provider fakes."""

# Private composition inspection and flexible fake constructors are deliberate
# in this boundary test; production modules remain untouched.
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownLambdaType=false

from __future__ import annotations

from typing import Any

import pytest
from aura_core.bootstrap import calendar_uow
from aura_core.platform.calendar_oauth import (
    CredentialKeyRing,
    GoogleCalendarOAuthConfig,
    KeyMaterial,
    SecretValue,
)

SESSION_FACTORY: Any = object()


class FakeSettings:
    google_calendar_enabled = True

    def calendar_oauth_config(self) -> GoogleCalendarOAuthConfig:
        return GoogleCalendarOAuthConfig(
            enabled=True,
            client_id="configured-client",
            client_secret=SecretValue("configured-secret"),
            key_ring=CredentialKeyRing(
                "active",
                {"active": KeyMaterial("active", b"k" * 32, "active")},
            ),
        )


class FakeEnabledProvider:
    pass


def test_enabled_composition_uses_configured_provider_instead_of_unavailable_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = FakeEnabledProvider()
    monkeypatch.setattr(
        calendar_uow,
        "GoogleCalendarAdapter",
        lambda **kwargs: provider,
    )

    service = calendar_uow.calendar_service(
        SESSION_FACTORY,
        settings=FakeSettings(),
        testing=True,
    )

    assert service._provider is provider
    assert type(service._provider).__name__ != "_UnavailableCalendarProvider"


def test_enabled_composition_requires_a_real_provider_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(calendar_uow, "GoogleCalendarAdapter", lambda **kwargs: None)
    service = calendar_uow.calendar_service(
        SESSION_FACTORY,
        settings=FakeSettings(),
        testing=True,
    )
    provider: Any = service._provider
    assert provider is not None
    assert type(provider).__name__ != "_UnavailableCalendarProvider"
