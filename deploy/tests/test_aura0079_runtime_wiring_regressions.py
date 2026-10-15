"""Executable deployment regressions added after the AURA-0079 review."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


def _compose() -> dict[str, Any]:
    document: Any = yaml.safe_load((ROOT / "compose.yaml").read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return cast(dict[str, Any], document)


def _services() -> dict[str, dict[str, Any]]:
    raw = _compose()["services"]
    assert isinstance(raw, dict)
    return {
        str(name): cast(dict[str, Any], definition)
        for name, definition in cast(dict[Any, Any], raw).items()
        if isinstance(definition, dict)
    }


def _observatory_services() -> dict[str, dict[str, Any]]:
    return {
        name: definition
        for name, definition in _services().items()
        if "observatory" in cast(list[Any], definition.get("profiles", []))
    }


def test_scheduler_has_optional_telemetry_export_without_an_observatory_startup_dependency() -> (
    None
):
    scheduler = _services()["aura-core-scheduler"]
    environment = cast(dict[Any, Any], scheduler.get("environment", {}))
    for key in (
        "AURA_TELEMETRY_OTLP_ENABLED",
        "AURA_TELEMETRY_OTLP_ENDPOINT",
        "AURA_TELEMETRY_OTLP_QUEUE_CAPACITY",
        "AURA_TELEMETRY_OTLP_MAX_ATTEMPTS",
        "AURA_TELEMETRY_OTLP_TIMEOUT_SECONDS",
        "AURA_TELEMETRY_DEPLOYMENT_ID",
        "AURA_TELEMETRY_GIT_COMMIT",
        "AURA_TELEMETRY_HOST_ID",
    ):
        assert key in environment
    assert "observatory-ingest" in (scheduler.get("networks") or [])
    assert "observatory" not in repr(scheduler.get("depends_on", {})).casefold()


def test_core_api_worker_and_scheduler_render_production_deployment_environment() -> None:
    services = _services()
    for name in ("aura-core-api", "aura-core-worker", "aura-core-scheduler"):
        environment = cast(dict[Any, Any], services[name].get("environment", {}))
        assert environment.get("AURA_ENVIRONMENT") == "production", name


def test_all_clickhouse_clients_mount_the_same_file_backed_credential() -> None:
    services = _observatory_services()
    expected_clients = {"observatory-api", "observatory-worker", "observatory-migrate"}
    for name in expected_clients:
        definition = services[name]
        secrets = set(cast(list[Any], definition.get("secrets", [])))
        environment = repr(definition.get("environment", {}))
        assert "observatory-clickhouse-password" in secrets, name
        assert "CLICKHOUSE_USER" in environment
        assert "CLICKHOUSE_PASSWORD_FILE" in environment


def test_observatory_web_is_a_same_origin_bff_gateway_and_api_stays_private() -> None:
    services = _observatory_services()
    web = services["observatory-web"]
    api = services["observatory-api"]
    assert "ports" in web
    assert "ports" not in api
    assert "observatory-api" in repr(web.get("depends_on", {}))

    # The gateway is intentionally checked through its shipped configuration,
    # not just through a Compose comment: a stock nginx image cannot proxy the
    # private BFF without a mounted/build-time config.
    candidate_paths = [
        ROOT / "deploy/docker/aura-observatory.nginx.conf",
        ROOT / "deploy/docker/aura-observatory-web.nginx.conf",
        ROOT / "deploy/docker/observatory-web.nginx.conf",
    ]
    configured_paths = " ".join(str(value) for value in web.get("volumes", []))
    config = next((path for path in candidate_paths if path.exists()), None)
    assert config is not None, configured_paths
    assert str(config.relative_to(ROOT)) in configured_paths
    text = config.read_text(encoding="utf-8")
    assert "location /api/" in text
    assert "proxy_pass http://observatory-api" in text


def test_collector_uses_standard_signal_paths_and_explicit_metadata_allowlist() -> None:
    collector_path = ROOT / "deploy/compose/observatory-collector.yaml"
    text = collector_path.read_text(encoding="utf-8")
    config = yaml.safe_load(text)
    assert isinstance(config, dict)
    raw_config = cast(dict[Any, Any], config)
    exporters = cast(dict[Any, Any], raw_config.get("exporters", {}))
    exporter_text = repr(exporters)
    assert "observatory-api" in exporter_text
    assert "/internal" in exporter_text
    assert "Authorization: Bearer" in text
    assert "OBSERVATORY_INGEST_CREDENTIAL" in text
    observatory_exporter = cast(dict[Any, Any], exporters["otlphttp/observatory"])
    assert observatory_exporter.get("encoding") == "json"

    processors = cast(dict[Any, Any], raw_config.get("processors", {}))
    processor_text = repr(processors).casefold()
    assert any(
        marker in processor_text
        for marker in ("delete_key", "include", "allowlist", "transform", "filter")
    )
    # Sensitive OTLP envelope sections must not be forwarded by a stock
    # Collector when an untrusted sender adds them.
    assert "events" in processor_text
    assert "links" in processor_text


def test_default_compose_has_no_profile_only_service_in_the_base_start_graph() -> None:
    for name, definition in _services().items():
        if "profiles" not in definition:
            assert "observatory" not in name.casefold()
            assert "observatory" not in repr(definition.get("depends_on", {})).casefold()
