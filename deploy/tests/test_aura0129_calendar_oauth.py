"""AURA-0129 deployment and secret-isolation acceptance checks."""

from __future__ import annotations

import base64
import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml
from aura_core.platform.calendar_oauth import calendar_oauth_readiness

ROOT = Path(__file__).parents[2]
CALENDAR_SECRET_NAMES = {
    "aura-google-calendar-client-secret",
    "aura-provider-credential-key-ring",
}


def _compose() -> dict[str, Any]:
    value = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services() -> dict[str, dict[str, Any]]:
    value = _compose()["services"]
    assert isinstance(value, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[object, object], value).items()
    }


def _rendered_compose(*overlays: str) -> dict[str, Any]:
    environment = os.environ.copy()
    environment.update(
        {
            "AURA_STATE_ROOT": "/tmp/aura-aura0129-test-state",
            "AURA_OIDC_ISSUER": "https://identity.example",
            "AURA_OIDC_REDIRECT_URI": "https://aura.example/api/v1/auth/callback",
            "AURA_OWNER_SUBJECT": "aura0129-test-owner",
            "AURA_PUBLIC_ORIGIN": "https://aura.example",
            "AURA_GOOGLE_CALENDAR_CLIENT_ID": "aura0129-client",
            "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE": "/tmp/aura0129-missing-client-secret",
            "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE": "/tmp/aura0129-missing-key-ring",
        }
    )
    command = ["docker", "compose"]
    command.extend(flag for overlay in overlays for flag in ("-f", str(ROOT / overlay)))
    command.extend(("config", "--format", "json"))
    result = subprocess.run(command, check=False, capture_output=True, text=True, env=environment)
    assert result.returncode == 0, result.stderr
    rendered = json.loads(result.stdout)
    assert isinstance(rendered, dict)
    return cast(dict[str, Any], rendered)


def _calendar_fields(service: dict[str, Any]) -> tuple[set[str], set[str]]:
    raw_secrets = cast(list[Any], service.get("secrets", []))
    secrets = {
        value if isinstance(value, str) else str(value.get("source", ""))
        for value in raw_secrets
        if isinstance(value, (str, dict))
    }
    environment = cast(dict[str, Any], service.get("environment", {}))
    return secrets, set(str(key) for key in environment)


def test_calendar_is_disabled_by_default_and_documented_without_usable_values() -> None:
    environment = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose = (ROOT / "compose.yaml").read_text(encoding="utf-8")

    assert "AURA_GOOGLE_CALENDAR_ENABLED=false" in environment
    assert "AURA_GOOGLE_CALENDAR_CLIENT_ID=" in environment
    assert (
        "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE=./.secrets/"
        "aura-google-calendar-client-secret"
    ) in environment
    assert (
        "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE=./.secrets/"
        "aura-provider-credential-key-ring.json"
    ) in environment
    assert "AURA_GOOGLE_CALENDAR_SCOPE=" not in compose
    assert "AURA_GOOGLE_CALENDAR_CALLBACK_URI:" not in compose
    assert "client-secret-value" not in compose
    assert "BEGIN PRIVATE KEY" not in environment


def test_only_core_api_receives_calendar_secret_mounts_and_configuration() -> None:
    services = _services()
    api = services["aura-core-api"]

    assert not CALENDAR_SECRET_NAMES.intersection(set(api["secrets"]))
    api_environment = cast(dict[str, Any], api["environment"])
    assert api_environment.get("AURA_GOOGLE_CALENDAR_ENABLED", "false") == "false"
    assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE" not in api_environment
    assert "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE" not in api_environment

    for name, service in services.items():
        if name == "aura-core-api":
            continue
        rendered = repr(service)
        assert not CALENDAR_SECRET_NAMES.intersection(
            set(cast(list[str], service.get("secrets", [])))
        )
        assert "/run/secrets/aura-google-calendar-client-secret" not in rendered
        assert "/run/secrets/aura-provider-credential-key-ring" not in rendered
        assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET" not in rendered
        assert "AURA_PROVIDER_CREDENTIAL_KEY_RING" not in rendered


def test_api_only_calendar_secrets_are_not_shared_with_worker_scheduler_migration_or_web() -> None:
    services = _services()
    for name in (
        "aura-core-worker",
        "aura-core-scheduler",
        "aura-core-migrate",
        "aura-web",
        "frontend-e2e",
        "aura-evaluation-runner",
        "observatory-api",
        "observatory-worker",
        "observatory-scheduler",
    ):
        if name not in services:
            continue
        service = services[name]
        assert not CALENDAR_SECRET_NAMES.intersection(set(service.get("secrets", []))), name
        # Existing Core worker egress is unrelated Ollama/provider wiring; this
        # slice must not add Calendar secret access or a new network path.
        assert "/run/secrets/aura-google-calendar-client-secret" not in repr(service), name
        assert "/run/secrets/aura-provider-credential-key-ring" not in repr(service), name


def test_api_calendar_secret_mounts_are_not_plaintext_environment_values() -> None:
    api = _services()["aura-core-api"]
    environment = cast(dict[str, Any], api["environment"])
    for key, value in environment.items():
        assert "SECRET" not in key or "_FILE" in key or key in {
            "AURA_GOOGLE_CALENDAR_ENABLED",
        }
        assert "super-secret" not in str(value)
        assert "BEGIN " not in str(value)
    assert "AURA_GOOGLE_CALENDAR_SCOPE" not in environment
    assert "AURA_GOOGLE_CALENDAR_CALLBACK_URI" not in environment


def test_local_http_does_not_create_or_mount_calendar_placeholders() -> None:
    launcher = (ROOT / "deploy/compose/local-http.sh").read_text(encoding="utf-8")

    assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE" not in launcher
    assert "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE" not in launcher


def test_local_http_overlay_does_not_grant_calendar_secrets_to_other_processes() -> None:
    overlay = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
    assert "aura-core-api:" in overlay
    assert "aura-google-calendar-client-secret" not in overlay
    assert "aura-provider-credential-key-ring" not in overlay
    assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET" not in overlay
    assert "AURA_PROVIDER_CREDENTIAL_KEY_RING" not in overlay


def test_top_level_secret_definitions_are_file_only_and_have_no_inline_values() -> None:
    base_secrets = cast(dict[str, Any], _compose()["secrets"])
    assert not CALENDAR_SECRET_NAMES.intersection(base_secrets)
    overlay = yaml.safe_load(
        (ROOT / "deploy/compose/google-calendar.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(overlay, dict)
    secrets = cast(dict[str, Any], overlay["secrets"])
    for name in CALENDAR_SECRET_NAMES:
        definition = cast(dict[str, Any], secrets[name])
        assert set(definition) == {"file"}
        assert "value" not in definition
        assert "environment" not in definition
        assert "content" not in definition


def test_api_keeps_the_existing_core_egress_boundary_without_expanding_other_services() -> None:
    services = _services()
    api = services["aura-core-api"]
    assert "aura-core-egress" in api["networks"]
    for name, service in services.items():
        if name != "aura-core-api":
            assert not (
                "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE" in repr(service)
                and "aura-core-egress" in service.get("networks", [])
            )


def test_base_and_local_http_render_without_calendar_files_or_calendar_mounts() -> None:
    for rendered in (
        _rendered_compose("compose.yaml"),
        _rendered_compose(
            "compose.yaml", "deploy/compose/local-identity.yaml", "deploy/compose/local-http.yaml"
        ),
    ):
        services = cast(dict[str, Any], rendered["services"])
        for name, service_value in services.items():
            service = cast(dict[str, Any], service_value)
            secret_names, environment_keys = _calendar_fields(service)
            assert not CALENDAR_SECRET_NAMES.intersection(secret_names), name
            assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE" not in environment_keys, name
            assert "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE" not in environment_keys, name


def test_explicit_google_calendar_overlay_enables_and_mounts_both_secrets_only_in_api() -> None:
    overlay = ROOT / "deploy/compose/google-calendar.yaml"
    assert overlay.is_file()
    rendered = _rendered_compose("compose.yaml", "deploy/compose/google-calendar.yaml")
    services = cast(dict[str, Any], rendered["services"])
    api = cast(dict[str, Any], services["aura-core-api"])
    api_secrets, api_environment = _calendar_fields(api)
    assert CALENDAR_SECRET_NAMES <= api_secrets
    assert "AURA_GOOGLE_CALENDAR_ENABLED" in api_environment
    assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE" in api_environment
    assert "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE" in api_environment
    for name, service_value in services.items():
        if name == "aura-core-api":
            continue
        service = cast(dict[str, Any], service_value)
        secret_names, environment_keys = _calendar_fields(service)
        assert not CALENDAR_SECRET_NAMES.intersection(secret_names), name
        assert "AURA_GOOGLE_CALENDAR_CLIENT_SECRET_FILE" not in environment_keys, name
        assert "AURA_PROVIDER_CREDENTIAL_KEY_RING_FILE" not in environment_keys, name


def test_enabled_configuration_is_missing_invalid_then_ready_with_safe_files(
    tmp_path: Path,
) -> None:
    secret = tmp_path / "client-secret"
    ring = tmp_path / "key-ring.json"
    settings = type(
        "SettingsFixture",
        (),
        {
            "google_calendar_enabled": True,
            "google_calendar_client_id": "aura0129-client",
            "google_calendar_client_secret": "",
            "google_calendar_client_secret_file": str(secret),
            "provider_credential_key_ring_file": str(ring),
            "google_calendar_key_ring_file": "",
            "google_calendar_callback_uri": "",
            "public_origin": "https://aura.example",
            "environment": "production",
        },
    )()
    assert calendar_oauth_readiness(settings).state == "missing_configuration"
    secret.write_text("malformed\n", encoding="utf-8")
    ring.write_text("{\"version\": true}", encoding="utf-8")
    secret.chmod(0o644)
    ring.chmod(0o644)
    assert calendar_oauth_readiness(settings).state == "invalid_configuration"
    secret.write_text("safe-client-secret\n", encoding="utf-8")
    ring.write_text(
        '{"version":1,"active_key_id":"key-1","keys":{"key-1":{"key":"'
        + base64.urlsafe_b64encode(bytes([1]) * 32).decode()
        + '"}}}',
        encoding="utf-8",
    )
    assert calendar_oauth_readiness(settings).state == "ready"


def test_rendered_calendar_overlay_does_not_create_writable_secret_mounts() -> None:
    rendered = _rendered_compose("compose.yaml", "deploy/compose/google-calendar.yaml")
    api = cast(dict[str, Any], rendered["services"]["aura-core-api"])
    mounts = cast(list[dict[str, Any]], api["secrets"])
    calendar_mounts = [mount for mount in mounts if mount.get("source") in CALENDAR_SECRET_NAMES]
    assert {mount["source"] for mount in calendar_mounts} == CALENDAR_SECRET_NAMES
    assert all(int(mount.get("mode", 0o444)) & 0o022 == 0 for mount in calendar_mounts)
