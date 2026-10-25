"""AURA-0086 service-boundary, query, and recovery acceptance coverage."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.entrypoints.cli import (
    _sync_component_manifest_documents,  # pyright: ignore[reportPrivateUsage]
    load_component_manifests,
)
from aura_observatory.query import ObservatoryQueryService
from aura_observatory.settings import Settings

SERVICE_ROOT = Path(__file__).parents[1]
OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"


def _manifest(component_id: str = "aura.test.component") -> dict[str, object]:
    return {
        "component": {
            "id": component_id,
            "version": "1.0.0",
            "owner": "observatory.test",
            "category": "test",
        },
        "input": {"schema": "test-input-v1"},
        "output": {"schema": "test-output-v1"},
        "dependencies": [],
        "metrics": ["duration_ms"],
        "evaluators": ["test.evaluator"],
    }


class RecordingConnection:
    """Tiny DB-API double that verifies the sync path is idempotent."""

    def __init__(self) -> None:
        self.statements: list[tuple[str, tuple[object, ...] | None]] = []

    def execute(self, statement: str, parameters: tuple[object, ...] | None = None) -> None:
        self.statements.append((statement, parameters))


class CatalogueQueryPort:
    def __init__(self) -> None:
        self.component_calls: list[tuple[str | None, str | None, int]] = []
        self.health_calls: list[tuple[str, str | None, int]] = []
        self.definition: dict[str, object] = {
            "componentId": "aura.observatory.query_api",
            "componentVersion": "1.0.0",
            "owner": "observatory",
            "category": "api",
            "inputSchemaId": "metadata-request-v1",
            "outputSchemaId": "metadata-response-v1",
            "measurements": ["duration_ms"],
            "evaluators": ["observatory_self_monitoring"],
            "dependencies": [
                {
                    "dependencyId": "aura.observatory.telemetry_storage",
                    "kind": "component",
                    "requiredVersion": "1.0.0",
                },
                {
                    "dependencyId": "clickhouse",
                    "kind": "provider",
                    "requiredVersion": None,
                },
            ],
        }
        self.topology: dict[str, object] = {
            "rootComponentId": "aura.observatory.query_api",
            "rootComponentVersion": "1.0.0",
            "nodes": [
                {
                    "dependencyId": "aura.observatory.telemetry_storage",
                    "kind": "component",
                    "componentVersion": "1.0.0",
                    "definitionState": "declared",
                    "deployedVersions": ["1.0.0"],
                    "availability": "observed",
                    "health": None,
                    "partialHealth": True,
                },
                {
                    "dependencyId": "clickhouse",
                    "kind": "provider",
                    "componentVersion": None,
                    "definitionState": "missing",
                    "deployedVersions": [],
                    "availability": "unavailable",
                    "health": None,
                    "partialHealth": True,
                },
            ],
            "edges": [
                {
                    "sourceId": "aura.observatory.query_api",
                    "sourceVersion": "1.0.0",
                    "targetId": "aura.observatory.telemetry_storage",
                    "targetVersion": "1.0.0",
                    "targetKind": "component",
                    "resolution": "resolved",
                },
                {
                    "sourceId": "aura.observatory.query_api",
                    "sourceVersion": "1.0.0",
                    "targetId": "clickhouse",
                    "targetVersion": None,
                    "targetKind": "provider",
                    "resolution": "missing_definition",
                },
            ],
            "directDependencyIds": ["aura.observatory.telemetry_storage", "clickhouse"],
            "reverseDependencyIds": [],
            "partial": True,
            "snapshotAt": "2026-10-16T12:00:00Z",
        }

    async def overview_query(self) -> dict[str, object]:
        return {"health": None}

    async def components_query(self) -> list[dict[str, object]]:
        return (await self.component_collection_query())["items"]  # type: ignore[return-value]

    async def component_collection_query(
        self,
        *,
        definition_state: str | None = None,
        cursor: str | None = None,
        limit: int = 200,
    ) -> dict[str, object]:
        self.component_calls.append((definition_state, cursor, limit))
        items: list[dict[str, object]] = [
            {
                "componentId": "aura.observatory.query_api",
                "version": "1.0.0",
                "observed": True,
                "definitionState": "declared",
                "firstSeenAt": "2026-10-16T11:00:00Z",
                "lastSeenAt": "2026-10-16T12:00:00Z",
            },
            {
                "componentId": "aura.core.scheduler",
                "version": "9.9.9",
                "observed": True,
                "definitionState": "observed_only",
                "firstSeenAt": "2026-10-16T11:00:00Z",
                "lastSeenAt": "2026-10-16T12:00:00Z",
            },
        ]
        if definition_state is not None:
            items = [item for item in items if item["definitionState"] == definition_state]
        return {"items": items[:limit], "nextCursor": None, "snapshotAt": "2026-10-16T12:00:00Z"}

    async def component_definition_query(
        self, component_id: str, component_version: str
    ) -> dict[str, object] | None:
        if (component_id, component_version) == (
            "aura.observatory.query_api",
            "1.0.0",
        ):
            return self.definition
        return None

    async def component_dependencies_query(
        self,
        component_id: str,
        component_version: str,
        *,
        depth: int = 1,
        max_nodes: int = 100,
    ) -> dict[str, object] | None:
        assert 1 <= depth <= 4
        assert 1 <= max_nodes <= 200
        return self.topology if (component_id, component_version) == (
            "aura.observatory.query_api",
            "1.0.0",
        ) else None

    async def deployments_query(self) -> list[dict[str, object]]:
        return []

    async def run_query(self, filters: Any, *, cursor: str | None, limit: int) -> dict[str, Any]:
        del filters, cursor, limit
        return {"items": [], "nextCursor": None, "snapshotAt": "2026-10-16T12:00:00Z"}

    async def run_detail_query(
        self, run_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, object] | None:
        del run_id, cursor, limit
        return None

    async def trace_detail_query(
        self, trace_id: str, *, cursor: str | None = None, limit: int = 10000
    ) -> dict[str, object] | None:
        del trace_id, cursor, limit
        return None

    async def events_after_query(
        self, last_event_id: str | None, snapshot_sequence: int | None = None
    ) -> AsyncIterator[dict[str, object]]:
        del last_event_id, snapshot_sequence
        if False:
            yield {}

    async def component_health_query(
        self,
        component_id: str,
        from_time: Any,
        to_time: Any,
        *,
        component_version: str | None = None,
        point_limit: int = 200,
    ) -> dict[str, object] | None:
        del from_time, to_time
        self.health_calls.append((component_id, component_version, point_limit))
        return {
            "componentId": component_id,
            "summary": {
                "sampleCount": 1,
                "latencyP50Ms": 10,
                "latencyP95Ms": 10,
                "errorRate": 0,
                "outcomes": {"succeeded": 1, "failed": 0, "cancelled": 0, "unknown": 0},
            },
            "points": [],
        }

    async def health_snapshot_query(self) -> dict[str, object] | None:
        return None

    async def health_history_query(
        self, *, cursor: str | None, from_time: Any, to_time: Any, limit: int
    ) -> dict[str, object]:
        del cursor, from_time, to_time, limit
        return {"items": [], "nextCursor": None, "snapshotAt": None}


def test_manifest_sync_is_idempotent_and_replaces_only_owned_edges() -> None:
    connection = RecordingConnection()
    definition = load_component_manifests()[0]
    _sync_component_manifest_documents(connection, [definition])
    first_count = len(connection.statements)
    _sync_component_manifest_documents(connection, [definition])

    assert first_count > 0
    assert len(connection.statements) == first_count * 2
    statements = "\n".join(statement for statement, _ in connection.statements).lower()
    assert "on conflict (component_id, component_version) do update" in statements
    assert "delete from observatory_component_dependencies" in statements
    serialized = json.dumps(connection.statements, default=str).lower()
    assert not any(value in serialized for value in ("prompt", "response", "credential", "token"))


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("unexpected", True, "unsupported fields"),
        ("metrics", ["x"] * 129, "bounded"),
        ("dependencies", ["aura.test.dep"] * 65, "bounded"),
    ],
)
def test_manifest_loader_rejects_unsupported_or_unbounded_metadata(
    tmp_path: Path, field: str, value: object, message: str
) -> None:
    document = _manifest()
    document[field] = value
    (tmp_path / "manifest.yaml").write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_component_manifests(tmp_path)


@pytest.mark.asyncio
async def test_catalogue_api_is_owner_scoped_bounded_and_explicit_about_partial_topology() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    port = CatalogueQueryPort()
    app.state.query = ObservatoryQueryService(
        MemoryObservatoryStore(), query_port=cast(Any, port)
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        catalogue = await client.get("/api/v1/components", params={"page_size": 1})
        assert catalogue.status_code == 200
        payload = catalogue.json()
        assert payload["items"][0]["definitionState"] == "declared"
        assert payload["items"][0]["definitionVersions"] == ["1.0.0"]
        assert port.component_calls[-1] == (None, None, 1)

        observed_only = await client.get(
            "/api/v1/components", params={"definition_state": "observed_only"}
        )
        assert observed_only.status_code == 200
        assert observed_only.json()["items"][0]["definitionState"] == "observed_only"
        assert observed_only.json()["items"][0]["definitionVersions"] == []

        definition = await client.get(
            "/api/v1/components/aura.observatory.query_api/definitions/1.0.0"
        )
        assert definition.status_code == 200
        assert "inputSchemaId" in definition.json()
        serialized_definition = definition.text.lower()
        assert not any(
            f'"{field}"' in serialized_definition
            for field in ("prompt", "response", "credential", "token", "endpoint")
        )

        topology = await client.get(
            "/api/v1/components/aura.observatory.query_api/definitions/1.0.0/dependencies",
            params={"depth": 2, "max_nodes": 2},
        )
        assert topology.status_code == 200
        topology_payload = topology.json()
        assert topology_payload["partial"] is True
        assert {edge["resolution"] for edge in topology_payload["edges"]} == {
            "resolved",
            "missing_definition",
        }

        assert (await client.get("/api/v1/components", params={"page_size": 0})).status_code == 400
        assert (
            await client.get(
                "/api/v1/components", params={"definition_state": "invalid"}
            )
        ).status_code == 400
        assert (
            await client.get(
                "/api/v1/components/aura.observatory.query_api/definitions/1.0.0/dependencies",
                params={"max_nodes": 201},
            )
        ).status_code == 400

    unauthenticated = create_app(settings, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unauthenticated), base_url=settings.public_origin
    ) as client:
        assert (await client.get("/api/v1/components")).status_code == 401


@pytest.mark.asyncio
async def test_retention_removes_observations_but_preserves_observed_component_catalogue() -> None:
    store = MemoryObservatoryStore()
    component_id = "aura.observatory.query_api"
    await store.record_component(component_id, "1.0.0")
    deleted = await store.delete_observations(None)
    assert deleted.observations == 0
    assert (component_id, "1.0.0") in store.components_seen


def test_observatory_component_code_does_not_import_core_or_read_core_tables() -> None:
    source_root = SERVICE_ROOT / "src" / "aura_observatory"
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_root.rglob("*.py"))
    assert "import aura_core" not in source
    assert "from aura_core" not in source
    assert "aura_core." not in source
    assert "core_observations" not in source
