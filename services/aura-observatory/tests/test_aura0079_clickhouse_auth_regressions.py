"""Authenticated ClickHouse wiring regressions for AURA-0079."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.memory import MemoryObservationIdRegistry
from aura_observatory.composition import build_ingest_service
from aura_observatory.contract import MetadataObservation
from aura_observatory.entrypoints import cli
from aura_observatory.settings import Settings


def _observation() -> MetadataObservation:
    return MetadataObservation.from_payload(
        {
            "schemaVersion": 1,
            "observationId": "01234567-89ab-4cde-8123-0123456789ab",
            "observedAt": datetime.now(UTC).isoformat(),
            "kind": "metric",
            "component": {"id": "aura.runtime.model_routing", "version": "1.2.0"},
            "operation": "model_routed",
            "measurement": {"name": "model_routed", "value": 1.0, "unit": "count"},
            "dimensions": {"outcome": "ok"},
            "deployment": {
                "deploymentId": "deploy-test",
                "gitCommit": "0123456789abcdef01234567",
                "environment": "test",
                "hostId": "host-test",
            },
            "serviceRole": "worker",
            "capturePolicy": "metadata_only",
        }
    )


class _Response:
    def raise_for_status(self) -> None:
        return None


@pytest.mark.asyncio
async def test_clickhouse_insert_uses_configured_basic_auth_without_url_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[dict[str, Any], dict[str, Any]]] = []

    class Client:
        def __init__(self, **kwargs: Any) -> None:
            self.options = kwargs

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, endpoint: str, **kwargs: Any) -> _Response:
            calls.append(({"endpoint": endpoint, **self.options}, kwargs))
            return _Response()

    monkeypatch.setattr(
        "aura_observatory.adapters.clickhouse.httpx.AsyncClient",
        Client,
    )
    store = ClickHouseObservationStore(
        "http://observatory-clickhouse:8123",
        database="aura_observatory",
        user="observatory",
        password="clickhouse-private",
        id_registry=MemoryObservationIdRegistry(),
    )

    assert await store.insert_observation(_observation())
    assert len(calls) == 1
    client_options, request = calls[0]
    auth = client_options.get("auth") or request.get("auth")
    assert auth is not None, "ClickHouse must receive explicit HTTP authentication"
    if isinstance(auth, tuple):
        assert auth == ("observatory", "clickhouse-private")
    else:
        assert getattr(auth, "username", None) == "observatory"
        assert getattr(auth, "password", None) == "clickhouse-private"
    assert "clickhouse-private" not in client_options["endpoint"]
    assert "clickhouse-private" not in repr(request.get("params", {}))


def test_clickhouse_password_is_loaded_only_from_the_configured_secret_file(tmp_path: Path) -> None:
    password_file = tmp_path / "clickhouse-password"
    password_file.write_text("clickhouse-private\n", encoding="utf-8")

    settings = Settings(
        clickhouse_user="observatory",
        clickhouse_password_file=password_file,
    )

    assert settings.clickhouse_user == "observatory"
    assert settings.clickhouse_password == "clickhouse-private"


def test_production_composition_passes_clickhouse_credentials_to_the_store(
    tmp_path: Path,
) -> None:
    password_file = tmp_path / "clickhouse-password"
    password_file.write_text("clickhouse-private", encoding="utf-8")
    settings = Settings(
        database_url="postgresql://observatory",
        clickhouse_user="observatory",
        clickhouse_password_file=password_file,
    )

    store = build_ingest_service(settings).observation_store

    assert getattr(store, "user", None) == "observatory"
    assert getattr(store, "password", None) == "clickhouse-private"


def test_migration_clickhouse_requests_are_authenticated_and_keep_secret_out_of_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str) -> None:
            return None

        def commit(self) -> None:
            return None

    class Response:
        def raise_for_status(self) -> None:
            return None

    settings = Settings(
        database_url="postgresql://observatory",
        clickhouse_url="http://observatory-clickhouse:8123",
        clickhouse_user="observatory",
        clickhouse_password="clickhouse-private",
    )
    monkeypatch.setattr(cli, "Settings", lambda: settings)

    def connect(_url: str) -> Connection:
        return Connection()

    monkeypatch.setattr(cli.psycopg, "connect", connect)

    def post(url: str, **kwargs: Any) -> Response:
        requests.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(cli.httpx, "post", post)
    cli.migrate()

    assert requests
    for request in requests:
        assert request["url"] == "http://observatory-clickhouse:8123"
        auth = request.get("auth")
        assert auth is not None
        if isinstance(auth, tuple):
            assert auth == ("observatory", "clickhouse-private")
        else:
            assert getattr(auth, "username", None) == "observatory"
            assert getattr(auth, "password", None) == "clickhouse-private"
        assert "clickhouse-private" not in request["url"]
