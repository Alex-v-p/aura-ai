"""AURA-0092 deployment isolation and process-boundary checks."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import yaml

ROOT = Path(__file__).parents[2]


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


def test_evaluation_worker_and_scheduler_use_distinct_image_targets() -> None:
    services = _services()
    worker = services["observatory-evaluation-worker"]
    scheduler = services["observatory-evaluation-scheduler"]

    assert worker["profiles"] == ["observatory"]
    assert scheduler["profiles"] == ["observatory"]
    assert worker["build"]["target"] == "evaluation-worker"
    assert scheduler["build"]["target"] == "evaluation-scheduler"
    assert "ports" not in worker
    assert "ports" not in scheduler
    assert worker["restart"] == "unless-stopped"
    assert scheduler["restart"] == "unless-stopped"
    assert worker["healthcheck"]["test"][0] == "CMD"
    assert scheduler["healthcheck"]["test"][0] == "CMD"


def test_worker_has_dispatch_bus_but_scheduler_is_database_only() -> None:
    services = _services()
    worker = services["observatory-evaluation-worker"]
    scheduler = services["observatory-evaluation-scheduler"]
    worker_networks = set(worker["networks"])
    scheduler_networks = set(scheduler["networks"])

    assert worker_networks == {
        "observatory-db",
        "aura-evaluation-bus",
        "observatory-ingest",
        "observatory-object-storage",
        "observatory-judge-egress",
    }
    assert scheduler_networks == {"observatory-db", "observatory-ingest"}
    assert set(worker["secrets"]) == {
        "observatory-database-password",
        "aura-nats-dispatch-password",
        "observatory-ingest-credential",
        "observatory-judge-garage-access-key",
        "observatory-judge-garage-secret-key",
    }
    assert scheduler["secrets"] == [
        "observatory-database-password",
        "observatory-ingest-credential",
    ]
    assert "AURA_OBSERVATORY_EVALUATION_NATS_PASSWORD_FILE" in worker["environment"]
    assert not any("NATS" in str(key) for key in scheduler["environment"])
    assert not any("OBJECT" in str(key) for key in scheduler["environment"])


def test_evaluation_processes_export_bounded_spans_with_stable_identity() -> None:
    services = _services()
    for name in ("observatory-evaluation-worker", "observatory-evaluation-scheduler"):
        environment = services[name]["environment"]
        assert environment["AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED"] == (
            "${AURA_OBSERVATORY_TELEMETRY_OTLP_ENABLED:-true}"
        )
        assert environment["AURA_OBSERVATORY_TELEMETRY_OTLP_ENDPOINT"] == "${AURA_OBSERVATORY_TELEMETRY_OTLP_ENDPOINT:-http://observatory-collector:4318/v1/traces}"
        assert environment["AURA_OBSERVATORY_INGEST_CREDENTIAL_FILE"] == (
            "/run/secrets/observatory-ingest-credential"
        )
        assert environment["AURA_OBSERVATORY_DEPLOYMENT_ID"] == (
            "${AURA_OBSERVATORY_DEPLOYMENT_ID:-local}"
        )
        assert environment["AURA_OBSERVATORY_DEPLOYMENT_COMMIT"] == (
            "${AURA_OBSERVATORY_DEPLOYMENT_COMMIT:-0000000}"
        )
        assert environment["AURA_OBSERVATORY_DEPLOYMENT_HOST"] == (
            "${AURA_OBSERVATORY_DEPLOYMENT_HOST:-local}"
        )


def test_scheduler_keeps_nats_isolation_while_reaching_only_collector_ingest() -> None:
    scheduler = _services()["observatory-evaluation-scheduler"]
    assert "aura-evaluation-bus" not in scheduler["networks"]
    assert "aura-nats-dispatch-password" not in scheduler["secrets"]
    assert scheduler["networks"] == ["observatory-db", "observatory-ingest"]


def test_scheduler_manifest_declares_every_emitted_metric() -> None:
    manifest = yaml.safe_load(
        (
            ROOT
            / "services/aura-observatory/resources/component-manifests/evaluation-scheduler.yaml"
        ).read_text(encoding="utf-8")
    )
    assert isinstance(manifest, dict)
    manifest = cast(dict[str, Any], manifest)
    declared = {str(name) for name in cast(list[Any], manifest.get("metrics", []))}
    definitions = {
        str(cast(dict[str, Any], item)["name"])
        for item in cast(list[Any], manifest.get("metric_definitions", []))
        if isinstance(item, dict) and "name" in cast(dict[str, Any], item)
    }
    emitted_by_scheduler = {
        "observatory_evaluation_dependency_outcome_count",
        "observatory_evaluation_scheduler_occurrence_count",
    }
    assert emitted_by_scheduler <= declared
    assert declared <= definitions
    assert emitted_by_scheduler <= definitions


def test_regular_observatory_worker_no_longer_receives_dispatch_identity() -> None:
    worker = _services()["observatory-worker"]
    assert "aura-evaluation-bus" not in worker["networks"]
    assert "aura-nats-dispatch-password" not in worker["secrets"]
    assert {
        key
        for key in worker["environment"]
        if "NATS" in str(key)
    } == {
        "AURA_OBSERVATORY_RUN_EVIDENCE_NATS_URL",
        "AURA_OBSERVATORY_RUN_EVIDENCE_NATS_USER",
        "AURA_OBSERVATORY_RUN_EVIDENCE_NATS_PASSWORD_FILE",
        "AURA_OBSERVATORY_RUN_EVIDENCE_NATS_DURABLE",
    }


def test_new_processes_have_bounded_read_only_runtime_controls() -> None:
    for service in (
        _services()["observatory-evaluation-worker"],
        _services()["observatory-evaluation-scheduler"],
    ):
        assert service["read_only"] is True
        assert service["security_opt"] == ["no-new-privileges:true"]
        assert service["cap_drop"] == ["ALL"]
        assert service["pids_limit"] > 0
        assert service["cpus"]
        assert service["mem_limit"]
        assert service["tmpfs"]
        assert service["ulimits"]["nofile"]["soft"] <= 256


def test_observatory_dockerfile_declares_dedicated_targets() -> None:
    dockerfile = (ROOT / "deploy/docker/aura-observatory.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "FROM runtime AS evaluation-worker" in dockerfile
    assert "aura_observatory.entrypoints.evaluation_worker" in dockerfile
    assert "FROM runtime AS evaluation-scheduler" in dockerfile
    assert "aura_observatory.entrypoints.evaluation_scheduler" in dockerfile


def test_observatory_image_contains_the_authoritative_transport_schema() -> None:
    dockerfile = (ROOT / "deploy/docker/aura-observatory.Dockerfile").read_text(
        encoding="utf-8"
    )
    source_dir = ROOT / "contracts/evaluation/v1"
    transport = source_dir / "evaluation-runner-transport.schema.json"

    assert transport.is_file()
    assert "COPY contracts/evaluation/v1/ ./contracts/evaluation/v1/" in dockerfile
    assert "COPY services/aura-observatory/" in dockerfile
    assert "schema generated" not in dockerfile.lower()

    def local_refs(value: Any) -> list[str]:
        refs: list[str] = []
        if isinstance(value, dict):
            mapping = cast(dict[str, Any], value)
            for key, nested in mapping.items():
                if key == "$ref" and isinstance(nested, str) and not nested.startswith("#"):
                    refs.append(nested.split("#", 1)[0])
                refs.extend(local_refs(nested))
        elif isinstance(value, list):
            for nested in cast(list[Any], value):
                refs.extend(local_refs(nested))
        return refs

    for schema_path in sorted(source_dir.glob("*.json")):
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        for reference in local_refs(schema):
            assert (schema_path.parent / reference).is_file(), (
                f"missing authoritative local schema dependency: {schema_path.name} -> {reference}"
            )

    assert {path.name for path in source_dir.glob("*.json")} == {
        "evaluation-runner-transport.schema.json",
        "evaluation.schema.json",
        "failure-codes.schema.json",
    }


def test_local_http_overlay_keeps_new_services_private() -> None:
    local_http = (ROOT / "deploy/compose/local-http.yaml").read_text(encoding="utf-8")
    assert "  observatory-evaluation-worker:\n" in local_http
    assert "  observatory-evaluation-scheduler:\n" in local_http
    assert (
        "networks: [observatory-db, aura-evaluation-bus, observatory-ingest, "
        "observatory-object-storage, observatory-judge-egress]"
    ) in local_http
    assert "networks: [observatory-db, observatory-ingest]" in local_http


def test_collector_diagnostics_keep_failures_but_hide_paths_and_endpoints() -> None:
    collector = _services()["observatory-collector"]
    command = collector["command"]
    assert command[0] == "--config=/etc/otelcol-contrib/config.yaml"
    assert "--set=service.telemetry.logs.level=warn" in command
    assert "--set=service.telemetry.logs.disable_caller=true" in command
    assert "--set=service.telemetry.logs.disable_stacktrace=true" in command
    assert collector["expose"] == ["4317", "4318", "8888"]
    config = (ROOT / "deploy/compose/observatory-collector.yaml").read_text(
        encoding="utf-8"
    )
    assert "level: normal" in config
    assert "level: detailed" not in config
    assert "server_address" not in config
    assert "server_port" not in config
    assert "target_info{" not in config
    assert "service_instance_id" not in config
    assert "resource:" in config
    assert "service.instance.id: null" in config
    assert (
        "endpoint: 0.0.0.0:8888" in config
        or ("host: 0.0.0.0" in config and "port: 8888" in config)
    )
    assert "output_paths: [/dev/null]" in config
    assert "error_output_paths: [/dev/null]" in config
