"""Authenticated ClickHouse wiring regressions for AURA-0079."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.memory import MemoryObservationIdRegistry
from aura_observatory.adapters.query import ProductionObservatoryStore
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
    assert "user" not in request.get("params", {})


@pytest.mark.asyncio
async def test_query_adapter_uses_basic_auth_without_duplicate_user_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, endpoint: str, **kwargs: Any) -> _Response:
            requests.append({"endpoint": endpoint, **kwargs})
            class Response(_Response):
                text = ""

            return Response()

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = ProductionObservatoryStore(
        lambda: None,
        "http://observatory-clickhouse:8123",
        database="aura_observatory",
        user="observatory",
        password="clickhouse-private",
    )

    assert await store._query_rows("SELECT 1") == []  # pyright: ignore[reportPrivateUsage]
    assert len(requests) == 1
    request = requests[0]
    assert request["params"] == {"database": "aura_observatory"}
    assert request["auth"] == ("observatory", "clickhouse-private")


def test_clickhouse_migration_splitter_preserves_quoted_and_commented_semicolons() -> None:
    statements = cli._split_clickhouse_statements(  # pyright: ignore[reportPrivateUsage]
        """
        -- Header ; semicolon is part of a comment.
        INSERT INTO observations VALUES ('one;two', 'it''s fine');
        -- Trailing ; comment stays with the next statement boundary.
        /* Block ; comment. */ INSERT INTO observations VALUES ('backslash\\;still');
        """
    )

    assert len(statements) == 2
    assert "'one;two'" in statements[0]
    assert "'it''s fine'" in statements[0]
    assert "Block ; comment" in statements[1]
    assert "'backslash\\;still'" in statements[1]


def test_clickhouse_migration_posts_each_statement_without_multiquery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    def post(url: str, **kwargs: Any) -> Response:
        requests.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(cli.httpx, "post", post)
    settings = Settings(
        clickhouse_url="http://observatory-clickhouse:8123",
        clickhouse_user="observatory",
        clickhouse_password="clickhouse-private",
    )

    cli._clickhouse_post(  # pyright: ignore[reportPrivateUsage]
        settings,
        "CREATE TABLE observations (id String) ENGINE = MergeTree ORDER BY id;"
        "INSERT INTO observations VALUES ('a;b');",
    )

    assert [request["content"] for request in requests] == [
        "CREATE TABLE observations (id String) ENGINE = MergeTree ORDER BY id",
        "INSERT INTO observations VALUES ('a;b')",
    ]
    assert all(request["params"] == {"database": "aura_observatory"} for request in requests)
    assert all("multiquery" not in request["params"] for request in requests)
    assert all(request["auth"] == ("observatory", "clickhouse-private") for request in requests)


def test_cli_query_results_use_sql_format_without_pseudo_http_parameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[dict[str, Any]] = []

    class Response:
        text = "0001_observations\n"

        def raise_for_status(self) -> None:
            return None

    def post(url: str, **kwargs: Any) -> Response:
        requests.append({"url": url, **kwargs})
        return Response()

    monkeypatch.setattr(cli.httpx, "post", post)
    settings = Settings(
        clickhouse_url="http://observatory-clickhouse:8123",
        clickhouse_user="observatory",
        clickhouse_password="clickhouse-private",
    )

    assert cli._clickhouse_applied(  # pyright: ignore[reportPrivateUsage]
        settings, "0001_observations"
    )
    cli._clickhouse_query(settings, "SELECT 1")  # pyright: ignore[reportPrivateUsage]

    assert len(requests) == 2
    assert all(request["params"] == {"database": "aura_observatory"} for request in requests)
    assert all("format" not in request["params"] for request in requests)
    assert requests[0]["content"].endswith("LIMIT 1 FORMAT TabSeparatedRaw")
    assert requests[1]["content"] == "SELECT 1 FORMAT TabSeparatedRaw"



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
        assert "user" not in request.get("params", {})
        assert "multiquery" not in request.get("params", {})
        assert "format" not in request.get("params", {})
        assert "clickhouse-private" not in request["url"]
