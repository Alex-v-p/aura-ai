"""Acceptance coverage for the disabled-by-default Calendar OAuth boundary."""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import pickle
from pathlib import Path
from types import SimpleNamespace

import aura_core.platform.calendar_oauth as calendar_oauth
import pytest
from aura_core.platform.calendar_oauth import (
    CALENDAR_API_ENDPOINT,
    CALENDAR_CALLBACK_PATH,
    CALENDAR_SCOPE,
    CREDENTIAL_ENCRYPTION_POLICY,
    GOOGLE_AUTHORIZATION_ENDPOINT,
    GOOGLE_CALENDAR_API_ENDPOINT,
    GOOGLE_CALENDAR_SCOPE,
    GOOGLE_TOKEN_ENDPOINT,
    GOOGLE_TOKEN_REVOCATION_ENDPOINT,
    CalendarOAuthConfigurationError,
    CredentialKeyRing,
    GoogleCalendarOAuthConfig,
    KeyMaterial,
    SecretValue,
    build_callback_uri,
    calendar_oauth_readiness,
    fixed_endpoints,
    load_key_ring,
    validate_calendar_callback,
    validate_callback_uri,
)


def _key(value: int = 1) -> str:
    return base64.urlsafe_b64encode(bytes([value]) * 32).decode()


def _write_ring(
    path: Path,
    *,
    active: str = "key-1",
    keys: dict[str, dict[str, str]] | None = None,
) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "active_key_id": active,
                "keys": keys or {active: {"key": _key()}},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)


def _write_secret(path: Path, value: str = "client-secret-value") -> None:
    path.write_text(value + "\n", encoding="utf-8")
    path.chmod(0o600)


def _settings(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "google_calendar_enabled": False,
        "google_calendar_client_id": "",
        "google_calendar_client_secret": "",
        "google_calendar_client_secret_file": "",
        "provider_credential_key_ring_file": "",
        "google_calendar_key_ring_file": "",
        "google_calendar_callback_uri": "",
        "public_origin": "https://calendar.example",
        "environment": "production",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_google_calendar_policy_is_fixed_and_read_only() -> None:
    assert GOOGLE_AUTHORIZATION_ENDPOINT == "https://accounts.google.com/o/oauth2/v2/auth"
    assert GOOGLE_TOKEN_ENDPOINT == "https://oauth2.googleapis.com/token"
    assert GOOGLE_TOKEN_REVOCATION_ENDPOINT == "https://oauth2.googleapis.com/revoke"
    assert GOOGLE_CALENDAR_API_ENDPOINT == "https://www.googleapis.com/calendar/v3"
    assert GOOGLE_CALENDAR_SCOPE == "https://www.googleapis.com/auth/calendar.readonly"
    assert CALENDAR_API_ENDPOINT == GOOGLE_CALENDAR_API_ENDPOINT
    assert CALENDAR_SCOPE == GOOGLE_CALENDAR_SCOPE
    assert fixed_endpoints() == {
        "authorization": GOOGLE_AUTHORIZATION_ENDPOINT,
        "token": GOOGLE_TOKEN_ENDPOINT,
        "revocation": GOOGLE_TOKEN_REVOCATION_ENDPOINT,
        "calendar_api": GOOGLE_CALENDAR_API_ENDPOINT,
    }
    with pytest.raises(TypeError):
        fixed_endpoints()["calendar_api"] = "https://attacker.invalid"  # type: ignore[index]


def test_credential_encryption_policy_is_the_approved_future_boundary() -> None:
    assert CREDENTIAL_ENCRYPTION_POLICY.algorithm == "AES-256-GCM"
    assert CREDENTIAL_ENCRYPTION_POLICY.nonce_bytes == 12
    assert CREDENTIAL_ENCRYPTION_POLICY.associated_data_fields == ("owner_id", "connection_id")
    assert CREDENTIAL_ENCRYPTION_POLICY.stores_key_version is True


def test_disabled_configuration_needs_no_calendar_secret_material(tmp_path: Path) -> None:
    config = GoogleCalendarOAuthConfig.from_settings(_settings())
    assert config.readiness().state == "disabled"
    assert calendar_oauth_readiness(_settings()).state == "disabled"
    assert not (tmp_path / "client-secret").exists()


def test_enabled_configuration_is_ready_only_with_valid_file_secrets(tmp_path: Path) -> None:
    secret = tmp_path / "client-secret"
    ring = tmp_path / "key-ring.json"
    _write_secret(secret)
    _write_ring(ring)
    settings = _settings(
        google_calendar_enabled=True,
        google_calendar_client_id="calendar-client-id",
        google_calendar_client_secret_file=str(secret),
        provider_credential_key_ring_file=str(ring),
    )

    config = GoogleCalendarOAuthConfig.from_settings(settings)
    assert config.readiness().state == "ready"
    assert isinstance(config.client_secret, SecretValue)
    assert config.client_secret.value == "client-secret-value"
    assert config.key_ring is not None


@pytest.mark.parametrize(
    "field",
    ["google_calendar_client_secret_file", "provider_credential_key_ring_file"],
)
def test_enabled_configuration_missing_file_fails_closed_without_detail(
    tmp_path: Path, field: str
) -> None:
    values = {
        "google_calendar_enabled": True,
        "google_calendar_client_id": "calendar-client-id",
        "google_calendar_client_secret_file": str(tmp_path / "missing-secret"),
        "provider_credential_key_ring_file": str(tmp_path / "missing-ring"),
    }
    values[field] = str(tmp_path / f"missing-{field}")
    readiness = calendar_oauth_readiness(_settings(**values))
    assert readiness.state == "missing_configuration"
    assert str(tmp_path) not in repr(readiness)


def test_inline_secret_is_rejected_even_when_disabled() -> None:
    with pytest.raises(CalendarOAuthConfigurationError, match="file-backed"):
        GoogleCalendarOAuthConfig.from_settings(_settings(google_calendar_client_secret="inline"))


def test_repr_and_errors_never_expose_secret_or_key_bytes(tmp_path: Path) -> None:
    secret = SecretValue("super-secret-marker")
    material = KeyMaterial("key-1", bytes.fromhex("ab" * 32), "active")
    ring_path = tmp_path / "ring.json"
    _write_ring(ring_path)
    config = _settings(
        google_calendar_enabled=True,
        google_calendar_client_id="public-client-id",
        google_calendar_client_secret_file=str(tmp_path / "missing-secret-super-secret-marker"),
        provider_credential_key_ring_file=str(ring_path),
    )

    assert "super-secret-marker" not in repr(secret)
    assert "super-secret-marker" not in str(secret)
    assert "ab" * 32 not in repr(material)
    assert "missing-secret-super-secret-marker" not in repr(
        GoogleCalendarOAuthConfig.from_settings(_settings())
    )
    readiness = calendar_oauth_readiness(config)
    assert readiness.state in {"missing_configuration", "invalid_configuration"}
    assert "super-secret-marker" not in repr(readiness)


@pytest.mark.parametrize(
    "value",
    [SecretValue("pickle-secret"), KeyMaterial("key-1", bytes(32), "active")],
)
def test_secret_values_reject_structural_and_pickle_serialization(value: object) -> None:
    with pytest.raises((TypeError, ValueError)):
        dataclasses.asdict(value)  # type: ignore[arg-type]
    with pytest.raises((TypeError, ValueError)):
        pickle.dumps(value)
    assert "pickle-secret" not in repr(value)
    assert "b'" not in repr(value)


def test_secret_file_reads_use_descriptor_no_follow_and_regular_file_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    _write_secret(secret, "descriptor-safe")
    seen_flags: list[int] = []
    real_open = calendar_oauth.os.open

    def checked_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        seen_flags.append(flags)
        return real_open(path, flags, mode)

    monkeypatch.setattr(calendar_oauth.os, "open", checked_open)
    assert (
        calendar_oauth._read_secret_file(secret)  # pyright: ignore[reportPrivateUsage]
        == "descriptor-safe"
    )
    assert seen_flags
    assert seen_flags[0] & os.O_NOFOLLOW
    assert seen_flags[0] & os.O_RDONLY == os.O_RDONLY


def test_secret_file_read_rejects_path_swap_to_symlink_after_metadata_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = tmp_path / "secret"
    original = tmp_path / "secret-original"
    _write_secret(secret, "swap-marker")
    real_open = calendar_oauth.os.open
    swapped = False

    def swap_then_open(path: str | os.PathLike[str], flags: int, mode: int = 0o777) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            secret.rename(original)
            secret.symlink_to(original)
        return real_open(path, flags, mode)

    monkeypatch.setattr(calendar_oauth.os, "open", swap_then_open)
    with pytest.raises(CalendarOAuthConfigurationError):
        calendar_oauth._read_secret_file(secret)  # pyright: ignore[reportPrivateUsage]
    secret.unlink()
    original.rename(secret)


@pytest.mark.parametrize("mode", [0o444, 0o644])
def test_compose_compatible_secret_modes_are_accepted(tmp_path: Path, mode: int) -> None:
    secret = tmp_path / "secret"
    ring = tmp_path / "ring.json"
    _write_secret(secret)
    _write_ring(ring)
    secret.chmod(mode)
    ring.chmod(mode)
    settings = _settings(
        google_calendar_enabled=True,
        google_calendar_client_id="calendar-client-id",
        google_calendar_client_secret_file=str(secret),
        provider_credential_key_ring_file=str(ring),
    )
    assert calendar_oauth_readiness(settings).state == "ready"


@pytest.mark.parametrize("mode", [0o602, 0o664, 0o666])
def test_secret_modes_with_group_or_other_write_are_rejected(tmp_path: Path, mode: int) -> None:
    secret = tmp_path / "secret"
    ring = tmp_path / "ring.json"
    _write_secret(secret)
    _write_ring(ring)
    secret.chmod(mode)
    ring.chmod(0o600)
    settings = _settings(
        google_calendar_enabled=True,
        google_calendar_client_id="calendar-client-id",
        google_calendar_client_secret_file=str(secret),
        provider_credential_key_ring_file=str(ring),
    )
    assert calendar_oauth_readiness(settings).state == "invalid_configuration"


@pytest.mark.parametrize(
    "origin",
    [
        "https://calendar.example\r",
        "https://calendar.example\n",
        "https://calendar.example\t",
        "https://calendar.example ",
        "https:// calendar.example",
        "https://calendar.example\u00a0",
    ],
)
def test_callback_origin_rejects_control_and_other_whitespace(origin: str) -> None:
    with pytest.raises(CalendarOAuthConfigurationError):
        build_callback_uri(origin, "production")


@pytest.mark.parametrize("version", [True, False])
def test_key_ring_rejects_boolean_version_values(tmp_path: Path, version: bool) -> None:
    path = tmp_path / "ring.json"
    path.write_text(
        json.dumps(
            {
                "version": version,
                "active_key_id": "key-1",
                "keys": {"key-1": {"key": _key()}},
            }
        ),
        encoding="utf-8",
    )
    path.chmod(0o600)
    with pytest.raises(CalendarOAuthConfigurationError):
        load_key_ring(path)


def test_key_ring_accepts_one_active_and_bounded_decrypt_only_keys(tmp_path: Path) -> None:
    path = tmp_path / "ring.json"
    keys = {"key-1": {"key": _key(1), "status": "active"}}
    keys.update(
        {
            f"old-{index}": {"key": _key(index + 1), "status": "decrypt_only"}
            for index in range(1, 8)
        }
    )
    _write_ring(path, keys=keys)

    ring = load_key_ring(path)
    assert isinstance(ring, CredentialKeyRing)
    assert ring.active_key_id == "key-1"
    assert ring.version == 1
    assert ring.active.status == "active"
    assert ring.key_for_decryption("old-7") is not None
    assert ring.key_for_decryption("unknown") is None
    assert len(ring.keys) == 8


@pytest.mark.parametrize(
    "document",
    [
        {"active_key_id": "key-1", "keys": {}},
        {
            "active_key_id": "key-1",
            "keys": {
                "key-1": {"key": _key(), "status": "active"},
                "key-2": {"key": _key(2), "status": "active"},
            },
        },
        {"active_key_id": "key-1", "keys": {"key-1": {"key": _key(), "status": "decrypt_only"}}},
        {"active_key_id": "bad/id", "keys": {"bad/id": {"key": _key()}}},
        {"active_key_id": "key-1", "keys": {"key-1": {"key": _key()[:-4]}}},
        {"version": 2, "active_key_id": "key-1", "keys": {"key-1": {"key": _key()}}},
    ],
)
def test_key_ring_rejects_unsafe_or_malformed_material(
    tmp_path: Path, document: dict[str, object]
) -> None:
    path = tmp_path / "ring.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    path.chmod(0o600)
    with pytest.raises(CalendarOAuthConfigurationError):
        load_key_ring(path)


def test_key_ring_rejects_duplicate_json_members_and_more_than_eight_keys(tmp_path: Path) -> None:
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(
        '{"active_key_id":"key-1","keys":{"key-1":{"key":"'
        + _key()
        + '"},"key-1":{"key":"'
        + _key(2)
        + '"}}}',
        encoding="utf-8",
    )
    duplicate.chmod(0o600)
    with pytest.raises(CalendarOAuthConfigurationError):
        load_key_ring(duplicate)

    oversized = tmp_path / "oversized.json"
    keys = {f"key-{index}": {"key": _key(index + 1)} for index in range(9)}
    _write_ring(oversized, keys=keys)
    with pytest.raises(CalendarOAuthConfigurationError):
        load_key_ring(oversized)

    too_large = tmp_path / "too-large.json"
    too_large.write_text("{" + " " * (64 * 1024) + "}", encoding="utf-8")
    too_large.chmod(0o600)
    with pytest.raises(CalendarOAuthConfigurationError):
        load_key_ring(too_large)


@pytest.mark.parametrize(
    ("origin", "environment"),
    [
        ("http://calendar.example", "production"),
        ("https://calendar.example:8443", "production"),
        ("https://calendar.example/path", "production"),
        ("https://calendar.example?private=1", "production"),
        ("https://user:pass@calendar.example", "production"),
        ("https://calendar.example#fragment", "production"),
        ("http://aura.localhost:4200", "production"),
        ("https://aura.localhost:4200", "development"),
        ("https://other.localhost:4200", "development"),
    ],
)
def test_callback_origin_rules_fail_closed(origin: str, environment: str) -> None:
    with pytest.raises(CalendarOAuthConfigurationError):
        build_callback_uri(origin, environment)


def test_production_callback_is_exact_and_development_overlay_is_explicit() -> None:
    production = build_callback_uri("https://calendar.example", "production")
    assert production == f"https://calendar.example{CALENDAR_CALLBACK_PATH}"
    assert validate_callback_uri(production, "https://calendar.example", "production")
    for variant in (
        production + "?code=secret",
        production + "/",
        production.replace("calendar.example", "CALENDAR.EXAMPLE"),
        production.replace(CALENDAR_CALLBACK_PATH, "/api/v1/integrations/calendar/%63allback"),
        production.replace("https://calendar.example", "https://calendar.example:443"),
    ):
        assert not validate_callback_uri(variant, "https://calendar.example")
    with pytest.raises(CalendarOAuthConfigurationError):
        validate_calendar_callback(production + "#fragment", "https://calendar.example")

    local = build_callback_uri("http://aura.localhost:4200", "development")
    assert local == f"http://aura.localhost:4200{CALENDAR_CALLBACK_PATH}"
    assert validate_callback_uri(local, "http://aura.localhost:4200", "development")


def test_secret_files_require_owner_read_only_regular_files(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    ring = tmp_path / "ring.json"
    _write_secret(secret)
    _write_ring(ring)
    secret.chmod(0o664)
    settings = _settings(
        google_calendar_enabled=True,
        google_calendar_client_id="calendar-client-id",
        google_calendar_client_secret_file=str(secret),
        provider_credential_key_ring_file=str(ring),
    )
    assert calendar_oauth_readiness(settings).state == "invalid_configuration"

    secret.chmod(0o600)
    link = tmp_path / "secret-link"
    link.symlink_to(secret)
    settings.google_calendar_client_secret_file = str(link)
    assert calendar_oauth_readiness(settings).state == "invalid_configuration"


def test_secret_file_size_is_bounded(tmp_path: Path) -> None:
    secret = tmp_path / "secret"
    ring = tmp_path / "ring.json"
    secret.write_text("x" * (16 * 1024 + 1), encoding="utf-8")
    ring.write_text(
        json.dumps(
            {
                "version": 1,
                "active_key_id": "key-1",
                "keys": {"key-1": {"key": _key()}},
            }
        ),
        encoding="utf-8",
    )
    secret.chmod(0o600)
    ring.chmod(0o600)
    settings = _settings(
        google_calendar_enabled=True,
        google_calendar_client_id="calendar-client-id",
        google_calendar_client_secret_file=str(secret),
        provider_credential_key_ring_file=str(ring),
    )
    assert calendar_oauth_readiness(settings).state == "invalid_configuration"


def test_key_material_is_narrow_and_repr_safe() -> None:
    material = KeyMaterial("active-1", bytes(32), "active")
    assert material.key_bytes == bytes(32)
    assert "active-1" in repr(material)
    assert "\x00" not in repr(material)
