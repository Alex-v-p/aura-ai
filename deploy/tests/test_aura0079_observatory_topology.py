"""Acceptance checks for the isolated, opt-in Observatory Compose profile.

These checks intentionally inspect the rendered source configuration rather than
starting containers.  They keep the default Aura stack usable on hosts that do
not enable Observatory and make the service/data/credential boundaries explicit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


def _compose() -> dict[str, Any]:
    value: Any = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _services(document: dict[str, Any]) -> dict[str, dict[str, Any]]:
    services = document.get("services")
    assert isinstance(services, dict)
    raw_services = cast(dict[Any, Any], services)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in raw_services.items()
        if isinstance(definition, dict)
    }


def _observatory_services() -> dict[str, dict[str, Any]]:
    return {
        name: definition
        for name, definition in _services(_compose()).items()
        if "observatory" in cast(list[Any], definition.get("profiles", []))
    }


def test_observatory_is_an_opt_in_profile_and_default_services_do_not_depend_on_it() -> None:
    services = _services(_compose())
    observatory = _observatory_services()

    expected = {
        "observatory-api",
        "observatory-worker",
        "observatory-migrate",
        "observatory-web",
        "observatory-postgres",
        "observatory-clickhouse",
        "observatory-session",
        "observatory-collector",
    }
    assert expected <= observatory.keys()

    for name, definition in observatory.items():
        assert definition["profiles"] == ["observatory"], name

    base_services = {
        name: definition for name, definition in services.items() if name not in observatory
    }
    assert all(
        "observatory" not in repr(definition.get("depends_on", {})).casefold()
        for definition in base_services.values()
    )


def test_observatory_state_is_below_encrypted_state_root_and_credentials_are_file_backed() -> None:
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    observatory = _observatory_services()
    assert len(observatory) >= 8

    for name, definition in observatory.items():
        body = repr(definition)
        if name.endswith(("-postgres", "-clickhouse", "-session")):
            assert "AURA_STATE_ROOT" in body, name

        environment = definition.get("environment", {})
        environment_text = repr(environment)
        assert "PASSWORD" not in environment_text or "_FILE" in environment_text
        assert "SECRET" not in environment_text or "_FILE" in environment_text

    # The profile must define Compose secrets through file paths; inline
    # credential values would leak into rendered configuration and logs.
    secrets = _compose().get("secrets")
    assert isinstance(secrets, dict)
    secret_names = {str(name) for name in cast(dict[Any, Any], secrets)}
    for fragment in ("observatory", "clickhouse", "session", "ingest"):
        assert any(fragment in name.casefold() for name in secret_names), fragment
    assert "file:" in compose_text


def test_only_observatory_web_has_a_loopback_browser_port() -> None:
    services = _services(_compose())
    observatory = _observatory_services()
    web_names = {name for name in observatory if name.endswith("-web")}
    assert web_names == {"observatory-web"}

    web_ports = services["observatory-web"].get("ports")
    assert web_ports == ["127.0.0.1:${AURA_OBSERVATORY_PORT:-4300}:80"]

    for name, definition in observatory.items():
        if name != "observatory-web":
            assert "ports" not in definition, name


def test_observatory_database_and_ingest_networks_are_private_and_separate() -> None:
    document = _compose()
    services = _observatory_services()
    networks = document.get("networks")
    assert isinstance(networks, dict)

    raw_networks = cast(dict[Any, Any], networks)
    private_networks: dict[str, dict[str, Any]] = {
        str(name): cast(dict[str, Any], definition)
        for name, definition in raw_networks.items()
        if str(name).startswith("observatory-") and isinstance(definition, dict)
    }
    assert private_networks
    assert all(
        definition.get("internal") is True
        for name, definition in private_networks.items()
        if name not in {"observatory-edge", "observatory-identity-egress"}
    )

    memberships: dict[str, set[str]] = {
        network: {
            name
            for name, definition in services.items()
            if network in (definition.get("networks") or [])
            or network in (definition.get("networks") or {})
        }
        for network in private_networks
    }
    assert any(
        any(name.endswith("-postgres") for name in members) for members in memberships.values()
    )
    assert any(
        any(name.endswith("-clickhouse") for name in members) for members in memberships.values()
    )
    assert any(
        any(name.endswith("-collector") for name in members) for members in memberships.values()
    )

    # Browser and database paths must not collapse into one shared network.
    database_networks = {
        network
        for network, members in memberships.items()
        if any(name.endswith("-postgres") for name in members)
    }
    browser_networks = {
        network for network, members in memberships.items() if "observatory-web" in members
    }
    assert database_networks.isdisjoint(browser_networks)


def test_observatory_auth_configuration_has_separate_audience_owner_and_https_defaults() -> None:
    env = (ROOT / ".env.example").read_text(encoding="utf-8")
    compose_text = (ROOT / "compose.yaml").read_text(encoding="utf-8")
    identity_files = [
        path
        for path in (ROOT / "deploy/authentik").rglob("*")
        if path.is_file() and path.suffix in {".yaml", ".yml", ".py", ".sh"}
    ]
    identity_text = "\n".join(path.read_text(encoding="utf-8") for path in identity_files)

    for variable in (
        "AURA_OBSERVATORY_OIDC_ISSUER",
        "AURA_OBSERVATORY_OIDC_AUDIENCE",
        "AURA_OBSERVATORY_OIDC_CLIENT_ID",
        "AURA_OBSERVATORY_OIDC_REDIRECT_URI",
        "AURA_OBSERVATORY_OWNER_SUBJECT",
        "AURA_OBSERVATORY_PUBLIC_ORIGIN",
    ):
        assert variable in env or variable in compose_text

    assert "observatory" in identity_text.casefold()
    assert "aura-observatory" in identity_text.casefold()
    assert "audience" in identity_text.casefold()

    observatory = _observatory_services()
    api = observatory["observatory-api"]
    environment = repr(api.get("environment", {}))
    assert "OIDC" in environment
    assert "OWNER_SUBJECT" in environment
    assert "SECURE_COOKIES" in environment


def test_collector_is_authenticated_buffered_and_metadata_only() -> None:
    collector = (ROOT / "deploy/compose/observatory-collector.yaml").read_text(encoding="utf-8")

    assert "capture_policy" in collector
    assert "metadata_only" in collector
    assert "Authorization: Bearer" in collector
    assert "OBSERVATORY_INGEST_CREDENTIAL" in collector
    assert "retry_on_failure:" in collector
    assert "sending_queue:" in collector
    assert "queue_size:" in collector
    assert "http://observatory-api:8000/internal/v1/telemetry" in collector
    assert "?" not in collector


def test_collector_and_observatory_api_are_private_without_host_ports() -> None:
    services = _observatory_services()
    collector = services["observatory-collector"]
    api = services["observatory-api"]

    assert "ports" not in collector
    assert "ports" not in api
    assert "expose" in collector
    assert "expose" in api
    assert "observatory-ingest" in (collector.get("networks") or [])
    assert "observatory-ingest" in (api.get("networks") or [])


def test_default_compose_declares_no_observatory_profile_services_without_profile() -> None:
    """The base file remains valid even when the profile is not selected."""

    document = _compose()
    services = _services(document)
    for name, definition in services.items():
        if "profiles" not in definition:
            assert "observatory" not in name.casefold()
            assert "observatory" not in repr(definition.get("depends_on", {})).casefold()

    # The opt-in declaration must be at the service level, never a default
    # network, volume, or dependency that changes the base topology.
    assert all(
        definition.get("profiles") == ["observatory"]
        for definition in services.values()
        if "observatory" in repr(definition).casefold() and "profiles" in definition
    )
