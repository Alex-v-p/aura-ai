"""Static topology and opt-in checks for the governed research providers."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]
OVERLAY = ROOT / "deploy/compose/research-providers.yaml"
PRIVATE_NETWORKS = {
    "aura-research-searxng": "aura-research-searxng-private",
    "aura-research-static-fetcher": "aura-research-static-private",
    "aura-research-browser-fetcher": "aura-research-browser-private",
}


def _rendered() -> dict[str, Any]:
    env = os.environ.copy()
    env.update(
        {
            "AURA_STATE_ROOT": "/tmp/aura0144-state",
            "AURA_OIDC_ISSUER": "https://identity.example",
            "AURA_OIDC_REDIRECT_URI": "https://aura.example/api/v1/auth/callback",
            "AURA_OWNER_SUBJECT": "aura0144-owner",
            "AURA_PUBLIC_ORIGIN": "https://aura.example",
            "COMPOSE_PROFILES": "research-providers",
        }
    )
    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(OVERLAY),
            "config",
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_provider_services_are_profile_gated_and_unpublished() -> None:
    source = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    assert isinstance(source, dict)
    services = cast(dict[str, Any], source["services"])
    provider_names = (
        "aura-research-searxng",
        "aura-research-static-fetcher",
        "aura-research-browser-fetcher",
    )
    for name in provider_names:
        definition = cast(dict[str, Any], services[name])
        assert "research-providers" in definition["profiles"]
        assert "ports" not in definition
        assert definition["read_only"] is True
        assert "no-new-privileges:true" in definition["security_opt"]
        assert definition["cap_drop"] == ["ALL"]


def test_rendered_overlay_has_only_private_provider_inbound_path() -> None:
    services = cast(dict[str, Any], _rendered()["services"])
    core_networks = set(cast(dict[str, Any], services["aura-core-worker"]["networks"]))
    assert core_networks.issuperset(PRIVATE_NETWORKS.values())
    for name, private_network in PRIVATE_NETWORKS.items():
        service = cast(dict[str, Any], services[name])
        assert not service.get("ports")
        assert private_network in service["networks"]
        assert private_network in core_networks
        assert "aura-core-db" not in service["networks"]
        assert "aura-core-bus" not in service["networks"]
        assert "aura-core-cache" not in service["networks"]
        assert "aura-object-storage" not in service["networks"]
        assert "observatory-ingest" not in service["networks"]
    assert "aura-research-public-egress" in services["aura-research-searxng"]["networks"]


def test_provider_private_networks_are_pairwise_isolated() -> None:
    services = cast(dict[str, Any], _rendered()["services"])
    for provider, own_network in PRIVATE_NETWORKS.items():
        networks = set(cast(dict[str, Any], services[provider]["networks"]))
        assert own_network in networks
        for other_provider, other_network in PRIVATE_NETWORKS.items():
            if other_provider != provider:
                assert other_network not in networks
    assert set(PRIVATE_NETWORKS.values()).isdisjoint(
        {"aura-research-public-egress"}
    )


def test_provider_defaults_are_disabled_and_no_aura_credentials_are_declared() -> None:
    rendered = _rendered()
    services = cast(dict[str, Any], rendered["services"])
    for name, switch in (
        ("aura-research-static-fetcher", "AURA_RESEARCH_STATIC_ENABLED"),
        ("aura-research-browser-fetcher", "AURA_RESEARCH_BROWSER_ENABLED"),
    ):
        environment = cast(dict[str, Any], services[name]["environment"])
        assert environment[switch] == "false"
        assert not services[name].get("secrets")
        assert not any("SECRET" in str(key) for key in environment)
        forbidden = ("AURA_SESSION", "DATABASE", "NATS", "VALKEY", "GARAGE", "OIDC")
        assert not any(token in repr(services[name]) for token in forbidden)


def test_core_provider_names_match_overlay_dns_names() -> None:
    settings = (
        ROOT / "services/aura-core/src/aura_core/platform/research_provider_settings.py"
    ).read_text(encoding="utf-8")
    assert 'search_endpoint: str = "http://aura-research-searxng:8080"' in settings
    assert 'static_fetch_endpoint: str = "http://aura-research-static-fetcher:8080"' in settings
    assert 'browser_fetch_endpoint: str = "http://aura-research-browser-fetcher:8080"' in settings


def test_versioned_search_adapter_is_folded_into_the_single_searxng_deployable() -> None:
    source = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    assert isinstance(source, dict)
    services = cast(dict[str, Any], source["services"])
    adapter = cast(dict[str, Any], services["aura-research-searxng"])
    assert "research-providers" in adapter["profiles"]
    assert "ports" not in adapter
    assert PRIVATE_NETWORKS["aura-research-searxng"] in adapter["networks"]
    assert "deploy/docker/research-searxng.Dockerfile" in repr(adapter)
    assert "aura-research-search-adapter" not in services

    adapter_source = (ROOT / "deploy/research/searxng_adapter.py").read_text(encoding="utf-8")
    assert "research-provider.v1" in adapter_source
    assert "/internal/v1/search" in adapter_source


def test_public_egress_is_explicitly_controlled_and_not_an_unrestricted_default_bridge() -> None:
    source = yaml.safe_load(OVERLAY.read_text(encoding="utf-8"))
    assert isinstance(source, dict)
    networks = cast(dict[str, Any], source["networks"])
    egress = cast(dict[str, Any], networks["aura-research-public-egress"])
    assert egress.get("external") is True
    assert egress.get("name") == "${AURA_RESEARCH_EGRESS_NETWORK:-aura-research-egress}"
    assert "driver" not in egress
    assert "driver_opts" not in egress
    assert "default" not in networks
    for service in cast(dict[str, Any], source["services"]).values():
        assert "default" not in cast(dict[str, Any], service).get("networks", {})


def test_provider_roots_are_registered_with_architecture_ownership() -> None:
    registry = yaml.safe_load(
        (ROOT / "tooling/architecture/modules.yaml").read_text(encoding="utf-8")
    )
    assert isinstance(registry, dict)
    modules = cast(list[dict[str, Any]], registry["modules"])
    roots = {str(item.get("root")) for item in modules}
    assert "services/aura-research-static-fetcher" in roots
    assert "services/aura-research-browser-fetcher" in roots


def test_provider_only_dependencies_are_not_added_to_shared_production_project() -> None:
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8").lower()
    assert '"trafilatura' not in project
    assert '"playwright' not in project
