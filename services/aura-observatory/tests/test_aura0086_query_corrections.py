"""Focused AURA-0086 query, API, and migration regression coverage.

These tests deliberately use bounded adapter doubles.  The opt-in PostgreSQL
test owns the database lifecycle separately; this module is deterministic and
does not require a running Observatory stack.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aura_observatory.adapters.query as query_module
import httpx
import psycopg
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.query import ProductionObservatoryStore, StorageUnavailableError
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.entrypoints.cli import migrate
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.query import ObservatoryQueryService, bounded_component_health_window
from aura_observatory.settings import Settings

SERVICE_ROOT = Path(__file__).parents[1]
OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"


def _definition(
    component_id: str, version: str = "1.0.0", dependencies: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    return {
        "componentId": component_id,
        "componentVersion": version,
        "owner": "test-owner",
        "category": "test",
        "inputSchemaId": "input-v1",
        "outputSchemaId": "output-v1",
        "measurements": ["duration_ms"],
        "evaluators": [],
        "dependencies": dependencies or [],
        "createdAt": "2026-10-16T00:00:00Z",
    }


class FakeProductionStore(ProductionObservatoryStore):
    """Bounded, query-shape-aware ClickHouse/PostgreSQL adapter double."""

    def __init__(
        self,
        definitions: list[dict[str, Any]],
        observed: list[dict[str, Any]] | None = None,
        points: list[dict[str, Any]] | None = None,
        reverse: list[tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(
            lambda: None,
            "http://clickhouse.invalid",
            database="observatory",
            user="test",
            password="test",
        )
        self.definitions = definitions
        self.observed = observed or []
        self.points = points or []
        self.reverse = reverse or []
        self.queries: list[str] = []

    async def _component_definition_rows(self) -> list[dict[str, Any]]:
        return list(self.definitions)

    async def _component_definition_row(
        self, component_id: str, component_version: str
    ) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.definitions
                if item["componentId"] == component_id
                and item["componentVersion"] == component_version
            ),
            None,
        )

    async def _reverse_dependencies(self, component_id: str) -> list[tuple[str, str]]:
        del component_id
        return list(self.reverse)

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        del params
        self.queries.append(statement)
        if "groupUniqArray(64)" in statement:
            versions: dict[str, set[str]] = {}
            for row in self.observed:
                versions.setdefault(str(row["component_id"]), set()).add(
                    str(row["component_version"])
                )
            return [
                {"component_id": key, "versions": sorted(value)}
                for key, value in sorted(versions.items())
            ]
        if "GROUP BY start_at" in statement:
            return list(self.points)
        if (
            "FROM observations" in statement
            and "GROUP BY component_id, component_version" in statement
        ):
            return list(self.observed)
        if "FROM (SELECT" in statement:
            return [
                {
                    "sample_count": 4,
                    "p50": 10,
                    "p95": 20,
                    "failures": 1,
                    "successes": 2,
                    "cancelled": 0,
                }
            ]
        return []


class RawTrendStore(ProductionObservatoryStore):
    """Simulate ClickHouse's two-stage execution and trend aggregation."""

    def __init__(self) -> None:
        super().__init__(
            lambda: None,
            "http://clickhouse.invalid",
            database="observatory",
            user="test",
            password="test",
        )
        self.statements: list[str] = []
        self.observations = [
            {
                "span_id": "span-1",
                "observed_at": "2026-10-16T01:01:00Z",
                "measurement_value": 10,
                "measurement_unit": "milliseconds",
                "measurement_name": "duration_ms",
                "outcome": "succeeded",
            },
            {
                "span_id": "span-1",
                "observed_at": "2026-10-16T01:01:01Z",
                "measurement_value": 1,
                "measurement_unit": "count",
                "measurement_name": "tokens",
                "outcome": "succeeded",
            },
        ]

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        del params
        self.statements.append(statement)
        if "GROUP BY start_at" in statement:
            # The trend must aggregate canonical executions first.  Checking
            # the point query itself prevents the summary query from masking
            # a regression to raw-observation counting.
            assert "FROM (SELECT" in statement
            assert "argMax(observed_at" in statement
            assert statement.count("argMaxIf") >= 3
            assert "measurement_name = 'duration_ms'" in statement
            assert "GROUP BY if(span_id = ''" in statement
            return [
                {
                    "start_at": datetime(2026, 10, 16, 1, 0, tzinfo=UTC),
                    "sample_count": 1,
                    "p50": 10,
                    "p95": 10,
                    "failures": 0,
                    "successes": 1,
                    "cancelled": 0,
                }
            ]
        assert "GROUP BY execution_id" in statement
        executions = {row["span_id"] for row in self.observations}
        outcomes = {row["outcome"] for row in self.observations}
        return [
            {
                "sample_count": len(executions),
                "p50": 10,
                "p95": 10,
                "failures": int("failed" in outcomes),
                "successes": int("succeeded" in outcomes),
                "cancelled": 0,
            }
        ]


class _SourcePageResult:
    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[Any, ...]]:
        return self._rows


class _SourcePageConnection:
    """Small async PostgreSQL seam that returns only the requested source page."""

    def __init__(self, store: SourceBackedCatalogueStore) -> None:
        self.store = store

    async def __aenter__(self) -> _SourcePageConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(
        self, statement: str, parameters: tuple[object, ...] | None = None
    ) -> _SourcePageResult:
        self.store.pg_statements.append(statement)
        assert parameters is not None
        fence = str(parameters[0])
        match = re.search(r"LIMIT (\d+)", statement)
        assert match is not None
        bound = int(match.group(1))
        component_ids = sorted(
            {
                str(row[0])
                for row in self.store.definition_rows
                if str(row[0]) > fence
            }
        )[:bound]
        self.store.pg_component_batches.append(component_ids)
        rows = [
            row
            for row in self.store.definition_rows
            if str(row[0]) in component_ids
        ]
        rows.sort(key=lambda row: (str(row[0]), str(row[1])))
        self.store.pg_row_counts.append(len(rows))
        return _SourcePageResult(rows)


class SourceBackedCatalogueStore(ProductionObservatoryStore):
    """Exercise the production pagination seams without compatibility overrides."""

    def __init__(
        self,
        definition_rows: list[tuple[object, ...]],
        observed_components: list[tuple[str, list[str]]],
    ) -> None:
        super().__init__(
            self._connection,
            "http://clickhouse.invalid",
            database="observatory",
            user="test",
            password="test",
        )
        self.definition_rows = definition_rows
        self.observed_components = observed_components
        self.pg_statements: list[str] = []
        self.pg_row_counts: list[int] = []
        self.pg_component_batches: list[list[str]] = []
        self.clickhouse_statements: list[str] = []
        self.clickhouse_row_counts: list[int] = []
        self.clickhouse_component_batches: list[list[str]] = []

    def _connection(self) -> _SourcePageConnection:
        return _SourcePageConnection(self)

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        del params
        self.clickhouse_statements.append(statement)
        if "groupUniqArray(64)" not in statement:
            return []
        cursor_match = re.search(r"component_id > '([^']*)'", statement)
        assert cursor_match is not None
        fence = cursor_match.group(1)
        bound_match = re.search(r"LIMIT (\d+)", statement)
        assert bound_match is not None
        bound = int(bound_match.group(1))
        component_ids = [
            component_id
            for component_id, _versions in self.observed_components
            if component_id > fence
        ][:bound]
        self.clickhouse_component_batches.append(component_ids)
        rows = [
            {
                "component_id": component_id,
                "versions": versions,
                "first_seen_at": "2026-10-16T00:00:00Z",
                "last_seen_at": "2026-10-16T01:00:00Z",
            }
            for component_id, versions in self.observed_components
            if component_id in component_ids
        ][:bound]
        self.clickhouse_row_counts.append(len(rows))
        return rows


class _TopologySourceConnection:
    def __init__(self, store: TopologySourceStore) -> None:
        self.store = store

    async def __aenter__(self) -> _TopologySourceConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(
        self, statement: str, _parameters: tuple[object, ...] | None = None
    ) -> _SourcePageResult:
        self.store.source_statements.append(statement)
        if "observatory_component_definitions" in statement:
            return _SourcePageResult(self.store.definition_rows)
        if "observatory_component_dependencies" in statement:
            return _SourcePageResult(self.store.reverse_rows)
        raise AssertionError(f"unexpected topology source statement: {statement}")


class TopologySourceStore(ProductionObservatoryStore):
    """Production topology seam with explicit source limit+one pages."""

    def __init__(self) -> None:
        self.target_id = "aura.topology.target"
        self.root = _definition(
            "aura.topology.root",
            dependencies=[
                {
                    "dependencyId": self.target_id,
                    "kind": "component",
                    "requiredVersion": "1.0.0",
                }
            ],
        )
        self.target = _definition(self.target_id)
        self.definition_rows: list[tuple[Any, ...]] = [
            (
                self.target_id,
                "1.0.0",
                "test-owner",
                "test",
                "input-v1",
                "output-v1",
                ["duration_ms"],
                [],
                datetime(2026, 10, 16, tzinfo=UTC),
            )
        ] + [
            (
                f"aura.topology.unrelated.{index:04d}",
                "1.0.0",
                "test-owner",
                "test",
                "input-v1",
                "output-v1",
                ["duration_ms"],
                [],
                datetime(2026, 10, 16, tzinfo=UTC),
            )
            for index in range(2000)
        ]
        self.reverse_rows = [
            (f"aura.topology.reverse.{index:04d}", "1.0.0") for index in range(201)
        ]
        self.observed_rows = [
            {"component_id": self.target_id, "versions": ["1.0.0"]}
        ] + [
            {
                "component_id": f"aura.topology.observed.{index:04d}",
                "versions": ["1.0.0"],
            }
            for index in range(200)
        ]
        self.source_statements: list[str] = []
        self.clickhouse_statements: list[str] = []
        super().__init__(
            self._connection,
            "http://clickhouse.invalid",
            database="observatory",
            user="test",
            password="test",
        )

    def _connection(self) -> _TopologySourceConnection:
        return _TopologySourceConnection(self)

    async def _component_definition_row(
        self, component_id: str, component_version: str
    ) -> dict[str, Any] | None:
        if (component_id, component_version) == (
            self.root["componentId"],
            self.root["componentVersion"],
        ):
            return self.root
        if (component_id, component_version) == (
            self.target["componentId"],
            self.target["componentVersion"],
        ):
            return self.target
        return None

    async def _query_rows(
        self, statement: str, *, params: dict[str, object] | None = None
    ) -> list[dict[str, Any]]:
        del params
        self.clickhouse_statements.append(statement)
        if "groupUniqArray(64)" in statement:
            return list(self.observed_rows)
        if "FROM (SELECT" in statement:
            return [
                {
                    "sample_count": 1,
                    "p50": 10,
                    "p95": 20,
                    "failures": 0,
                    "successes": 1,
                    "cancelled": 0,
                }
            ]
        return []


@pytest.mark.asyncio
async def test_component_pagination_is_complete_at_component_boundary() -> None:
    definitions = [_definition(f"aura.test.{letter}") for letter in ("a", "b", "c")]
    store = FakeProductionStore(definitions)

    first = await store.component_collection_query(limit=2)
    second = await store.component_collection_query(limit=2, cursor=first["nextCursor"])

    assert [item["componentId"] for item in first["items"]] == ["aura.test.a", "aura.test.b"]
    assert [item["componentId"] for item in second["items"]] == ["aura.test.c"]
    assert all(
        "definitionVersions" in item and "versions" in item
        for item in first["items"] + second["items"]
    )


@pytest.mark.asyncio
async def test_source_backed_postgres_cursor_pages_all_versions_without_global_cap() -> None:
    component_count = 231
    definition_rows: list[tuple[object, ...]] = [
        (
            f"aura.source.{index:04d}",
            version,
            "test-owner",
            "test",
            "input-v1",
            "output-v1",
            ["duration_ms"],
            [],
            datetime(2026, 10, 16, tzinfo=UTC),
        )
        for index in range(component_count)
        for version in ("1.0.0", "2.0.0")
    ]
    store = SourceBackedCatalogueStore(definition_rows, [])
    limit = 11
    cursor: str | None = None
    seen: list[str] = []

    for _ in range(component_count):
        page = await store.component_collection_query(limit=limit, cursor=cursor)
        items = page["items"]
        assert 0 < len(items) <= limit
        for item in items:
            assert item["definitionVersions"] == ["1.0.0", "2.0.0"]
            assert item["versions"] == []
        seen.extend(str(item["componentId"]) for item in items)
        next_cursor = page["nextCursor"]
        if next_cursor is None:
            break
        cursor = str(next_cursor)
    else:
        pytest.fail("source-backed cursor did not terminate")

    assert seen == sorted(seen)
    assert len(seen) == component_count
    assert len(set(seen)) == component_count
    assert all("LIMIT 2000" not in statement for statement in store.pg_statements)
    assert all("LIMIT 12" in statement for statement in store.pg_statements)
    assert all(row_count <= (limit + 1) * 2 for row_count in store.pg_row_counts)


@pytest.mark.asyncio
async def test_source_backed_clickhouse_cursor_pages_complete_version_groups() -> None:
    component_count = 231
    observed = [
        (f"aura.observed.{index:04d}", ["1.0.0", "2.0.0", "3.0.0"])
        for index in range(component_count)
    ]
    store = SourceBackedCatalogueStore([], observed)
    limit = 11
    cursor: str | None = None
    seen: list[str] = []

    for _ in range(component_count):
        page = await store.component_collection_query(limit=limit, cursor=cursor)
        items = page["items"]
        assert 0 < len(items) <= limit
        for item in items:
            assert item["definitionState"] == "observed_only"
            assert item["definitionVersions"] == []
            assert item["versions"] == ["1.0.0", "2.0.0", "3.0.0"]
        seen.extend(str(item["componentId"]) for item in items)
        next_cursor = page["nextCursor"]
        if next_cursor is None:
            break
        cursor = str(next_cursor)
    else:
        pytest.fail("source-backed cursor did not terminate")

    assert seen == sorted(seen)
    assert len(seen) == component_count
    assert len(set(seen)) == component_count
    assert all("LIMIT 1000" not in statement for statement in store.clickhouse_statements)
    assert all("LIMIT 12" in statement for statement in store.clickhouse_statements)
    assert all(row_count <= limit + 1 for row_count in store.clickhouse_row_counts)


def _health_sql_bounds(statement: str) -> tuple[str, str]:
    lower = re.findall(r"observed_at >= toDateTime64\('([^']+)'", statement)
    upper = re.findall(r"observed_at < toDateTime64\('([^']+)'", statement)
    assert lower, statement
    assert upper, statement
    return lower[0], upper[0]


@pytest.mark.asyncio
async def test_component_versions_are_deterministically_capped_at_64() -> None:
    versions = [f"2026.10.{index:03d}" for index in range(80)]
    definition_rows: list[tuple[object, ...]] = [
        (
            "aura.version.capped",
            version,
            "test-owner",
            "test",
            "input-v1",
            "output-v1",
            ["duration_ms"],
            [],
            datetime(2026, 10, 16, tzinfo=UTC),
        )
        for version in versions
    ]
    store = SourceBackedCatalogueStore(
        definition_rows, [("aura.version.capped", versions)]
    )

    raw_observed, _ = await store._observed_component_page("", 1)  # pyright: ignore[reportPrivateUsage]
    assert len(raw_observed) == 1
    assert len(raw_observed[0]["versions"]) == 80
    collection = await store.component_collection_query(limit=1)
    item = collection["items"][0]
    assert item["definitionVersions"] == sorted(versions)[:64]
    assert item["versions"] == sorted(versions)[:64]
    assert len(item["definitionVersions"]) == 64
    assert len(item["versions"]) == 64
    assert any(
        "groupUniqArray(64)(component_version)" in statement
        for statement in store.clickhouse_statements
    )


def test_component_health_window_defaults_to_24h_utc_and_enforces_31d_bounds() -> None:
    local_now = datetime(2026, 10, 16, 14, 30, tzinfo=timezone(timedelta(hours=2)))
    lower, upper = bounded_component_health_window(None, None, now=local_now)
    assert upper == local_now.astimezone(UTC)
    assert lower == upper - timedelta(hours=24)
    accepted_lower, accepted_upper = bounded_component_health_window(
        upper - timedelta(days=31), upper, now=local_now
    )
    assert (accepted_lower, accepted_upper) == (
        upper - timedelta(days=31),
        upper,
    )

    with pytest.raises(ValueError):
        bounded_component_health_window(
            upper - timedelta(days=31, microseconds=1), upper, now=local_now
        )
    with pytest.raises(ValueError):
        bounded_component_health_window(upper, upper, now=local_now)
    with pytest.raises(ValueError):
        bounded_component_health_window(datetime(2026, 10, 15), upper, now=local_now)
    with pytest.raises(ValueError):
        bounded_component_health_window(
            upper - timedelta(hours=1), datetime(2026, 10, 16), now=local_now
        )


@pytest.mark.asyncio
async def test_direct_and_catalogue_health_sql_always_has_both_time_bounds() -> None:
    store = FakeProductionStore([_definition("aura.health.catalogue")])
    direct = await store.component_health_query("aura.health.catalogue", None, None)
    assert direct is not None
    direct_queries = [query for query in store.queries if "selected_measurement_value" in query]
    assert len(direct_queries) == 1
    direct_lower, direct_upper = _health_sql_bounds(direct_queries[0])
    assert direct_lower < direct_upper

    class CataloguePort:
        async def component_collection_query(self, **_kwargs: Any) -> dict[str, Any]:
            return {
                "items": [
                    {
                        "componentId": "aura.health.catalogue",
                        "versions": ["1.0.0"],
                        "definitionVersions": ["1.0.0"],
                        "definitionState": "declared",
                        "firstSeenAt": "2026-10-16T00:00:00Z",
                        "lastSeenAt": "2026-10-16T00:00:00Z",
                    }
                ],
                "nextCursor": None,
            }

        async def component_health_query(
            self,
            component_id: str,
            from_time: datetime,
            to_time: datetime,
            *,
            component_version: str | None = None,
            point_limit: int = 200,
        ) -> dict[str, Any] | None:
            return await store.component_health_query(
                component_id,
                from_time,
                to_time,
                component_version=component_version,
                point_limit=point_limit,
            )

    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    app.state.query = ObservatoryQueryService(
        MemoryObservatoryStore(), query_port=CataloguePort()  # type: ignore[arg-type]
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get("/api/v1/components")
    assert response.status_code == 200, response.text
    catalogue_queries = [
        query for query in store.queries if "selected_measurement_value" in query
    ]
    assert len(catalogue_queries) == 2
    catalogue_lower, catalogue_upper = _health_sql_bounds(catalogue_queries[-1])
    assert catalogue_lower < catalogue_upper


@pytest.mark.asyncio
async def test_topology_health_reuses_one_bounded_window_for_all_nodes() -> None:
    targets = [_definition(f"aura.health.target{i}") for i in range(2)]
    root = _definition(
        "aura.health.root",
        dependencies=[
            {
                "dependencyId": target["componentId"],
                "kind": "component",
                "requiredVersion": "1.0.0",
            }
            for target in targets
        ],
    )
    observed = [
        {
            "component_id": target["componentId"],
            "component_version": "1.0.0",
            "first_seen_at": "2026-10-16T00:00:00Z",
            "last_seen_at": "2026-10-16T01:00:00Z",
        }
        for target in targets
    ]
    store = FakeProductionStore([root, *targets], observed=observed)
    topology = await store.component_dependencies_query(
        "aura.health.root", "1.0.0", depth=1, max_nodes=10
    )
    assert topology is not None
    health_queries = [
        query for query in store.queries if "selected_measurement_value" in query
    ]
    assert len(health_queries) == 2
    bounds = [_health_sql_bounds(query) for query in health_queries]
    assert all(lower < upper for lower, upper in bounds)
    assert len(set(bounds)) == 1


def _sparse_filtered_store() -> SourceBackedCatalogueStore:
    declared_ids = [f"aura.sparse.{letter}" for letter in ("a", "b", "c")]
    definition_rows: list[tuple[object, ...]] = [
        (
            component_id,
            version,
            "test-owner",
            "test",
            "input-v1",
            "output-v1",
            ["duration_ms"],
            [],
            datetime(2026, 10, 16, tzinfo=UTC),
        )
        for component_id in declared_ids
        for version in ("1.0.0", "2.0.0")
    ]
    observed_components = [
        (f"aura.sparse.{letter}", ["1.0.0"])
        for letter in ("a", "b", "c", "d", "e", "f")
    ]
    return SourceBackedCatalogueStore(definition_rows, observed_components)


@pytest.mark.asyncio
async def test_sparse_observed_only_filter_continues_past_empty_overlap_batches() -> None:
    store = _sparse_filtered_store()
    limit = 2

    first = await store.component_collection_query(
        definition_state="observed_only", limit=limit
    )
    second = await store.component_collection_query(
        definition_state="observed_only", limit=limit, cursor=first["nextCursor"]
    )

    assert [item["componentId"] for item in first["items"]] == [
        "aura.sparse.d",
        "aura.sparse.e",
    ]
    assert first["nextCursor"] is not None
    assert [item["componentId"] for item in second["items"]] == ["aura.sparse.f"]
    assert second["nextCursor"] is None
    # The first bounded source pages overlap on only declared components; the
    # later ClickHouse page supplies the first observed-only matches.
    assert store.pg_component_batches[0] == [
        "aura.sparse.a",
        "aura.sparse.b",
        "aura.sparse.c",
    ]
    assert store.clickhouse_component_batches[0] == [
        "aura.sparse.a",
        "aura.sparse.b",
        "aura.sparse.c",
    ]
    assert all(row_count <= (limit + 1) * 2 for row_count in store.pg_row_counts)
    assert all(row_count <= limit + 1 for row_count in store.clickhouse_row_counts)
    assert all("LIMIT 1000" not in statement for statement in store.clickhouse_statements)

    replay = _sparse_filtered_store()
    replay_first = await replay.component_collection_query(
        definition_state="observed_only", limit=limit
    )
    assert [item["componentId"] for item in replay_first["items"]] == [
        item["componentId"] for item in first["items"]
    ]
    assert replay_first["nextCursor"] == first["nextCursor"]


@pytest.mark.asyncio
async def test_sparse_declared_filter_has_symmetric_cursor_continuation() -> None:
    store = _sparse_filtered_store()
    limit = 2

    first = await store.component_collection_query(
        definition_state="declared", limit=limit
    )
    second = await store.component_collection_query(
        definition_state="declared", limit=limit, cursor=first["nextCursor"]
    )

    assert [item["componentId"] for item in first["items"]] == [
        "aura.sparse.a",
        "aura.sparse.b",
    ]
    assert first["nextCursor"] is not None
    assert [item["componentId"] for item in second["items"]] == ["aura.sparse.c"]
    assert second["nextCursor"] is None
    assert all(
        str(item["definitionState"]) == "declared"
        for item in first["items"] + second["items"]
    )
    assert all(row_count <= (limit + 1) * 2 for row_count in store.pg_row_counts)
    assert all(row_count <= limit + 1 for row_count in store.clickhouse_row_counts)
    assert all("LIMIT 1000" not in statement for statement in store.clickhouse_statements)


@pytest.mark.asyncio
async def test_topology_exact_depth_fanout_cap_and_reverse_cap() -> None:
    leaves = [_definition(f"aura.test.leaf{i}") for i in range(64)]
    fanout = [
        _definition(
            f"aura.test.branch{i}",
            dependencies=[
                {
                    "dependencyId": leaf["componentId"],
                    "kind": "component",
                    "requiredVersion": "1.0.0",
                }
                for leaf in leaves
            ],
        )
        for i in range(32)
    ]
    root = _definition(
        "aura.test.root",
        dependencies=[
            {"dependencyId": item["componentId"], "kind": "component", "requiredVersion": "1.0.0"}
            for item in fanout
        ],
    )
    reverse = [(f"aura.test.reverse{i}", "1.0.0") for i in range(250)]
    store = FakeProductionStore([root, *fanout, *leaves], reverse=reverse)
    assert len(root["dependencies"]) == 32

    depth_one = await store.component_dependencies_query(
        "aura.test.root", "1.0.0", depth=1, max_nodes=200
    )
    assert depth_one is not None
    assert all(edge["sourceId"] == "aura.test.root" for edge in depth_one["edges"])

    depth_two = await store.component_dependencies_query(
        "aura.test.root", "1.0.0", depth=2, max_nodes=200
    )
    assert depth_two is not None
    assert any(edge["sourceId"].startswith("aura.test.branch") for edge in depth_two["edges"]), (
        depth_two["edges"][:3]
    )
    assert len(depth_two["edges"]) <= 1000
    assert len(depth_two["reverseDependencyIds"]) == 200
    assert depth_two["partial"] is True


@pytest.mark.asyncio
async def test_topology_max_nodes_is_deterministic_and_edges_never_escape_nodes() -> None:
    root_id = "aura.test.bounded-root"
    dependencies = [
        _definition(f"aura.test.bounded-{index}") for index in range(8)
    ]
    root = _definition(
        root_id,
        dependencies=[
            {
                "dependencyId": item["componentId"],
                "kind": "component",
                "requiredVersion": "1.0.0",
            }
            for item in dependencies
        ],
    )
    store = FakeProductionStore([root, *dependencies])

    first = await store.component_dependencies_query(
        root_id, "1.0.0", depth=1, max_nodes=3
    )
    second = await store.component_dependencies_query(
        root_id, "1.0.0", depth=1, max_nodes=3
    )
    assert first is not None and second is not None
    assert first["nodes"] == second["nodes"]
    assert first["edges"] == second["edges"]
    assert first["partial"] is True
    assert len(first["nodes"]) == 3
    assert len(first["edges"]) == 3

    node_keys = {
        (str(node["dependencyId"]), str(node["componentVersion"]))
        for node in first["nodes"]
    }
    root_key = (root_id, "1.0.0")
    # The contract carries the root in rootComponentId/rootComponentVersion;
    # every non-root endpoint must be represented by a bounded node.  This
    # catches an edge emitted after a target was rejected by max_nodes.
    represented_keys = node_keys | {root_key}
    for edge in first["edges"]:
        source_key = (str(edge["sourceId"]), str(edge["sourceVersion"]))
        target_key = (str(edge["targetId"]), str(edge["targetVersion"]))
        assert source_key in represented_keys
        assert target_key in node_keys


@pytest.mark.asyncio
async def test_topology_source_limit_plus_one_marks_partial_without_false_missing() -> None:
    store = TopologySourceStore()
    topology = await store.component_dependencies_query(
        "aura.topology.root", "1.0.0", depth=1, max_nodes=10
    )

    assert topology is not None
    assert len(store.definition_rows) == 2001
    assert len(store.observed_rows) == 201
    assert len(store.reverse_rows) == 201
    assert any(
        "observatory_component_definitions" in statement and "LIMIT 2001" in statement
        for statement in store.source_statements
    )
    assert any(
        "groupUniqArray(64)" in statement and "LIMIT 201" in statement
        for statement in store.clickhouse_statements
    )
    assert any(
        "observatory_component_dependencies" in statement and "LIMIT 201" in statement
        for statement in store.source_statements
    )

    assert topology["partial"] is True
    assert topology["rootComponentId"] == "aura.topology.root"
    assert topology["rootComponentVersion"] == "1.0.0"
    edge = next(edge for edge in topology["edges"] if edge["targetId"] == store.target_id)
    assert edge["resolution"] == "resolved"
    target_node = next(
        node for node in topology["nodes"] if node["dependencyId"] == store.target_id
    )
    assert target_node["definitionState"] == "declared"
    assert target_node["availability"] == "observed"
    assert len(topology["reverseDependencyIds"]) == 200


@pytest.mark.asyncio
async def test_unpinned_dependency_versions_and_retention_removes_observed_only() -> None:
    root = _definition(
        "aura.test.root",
        dependencies=[
            {"dependencyId": "aura.test.target", "kind": "component", "requiredVersion": None}
        ],
    )
    target_v1 = _definition("aura.test.target", "1.0.0")
    target_v2 = _definition("aura.test.target", "2.0.0")
    store = FakeProductionStore(
        [root, target_v1, target_v2],
        observed=[
            {
                "component_id": "aura.test.observed",
                "component_version": "9.0.0",
                "first_seen_at": "2026-10-16T00:00:00Z",
                "last_seen_at": "2026-10-16T00:00:00Z",
            }
        ],
    )
    topology = await store.component_dependencies_query("aura.test.root", "1.0.0", depth=1)
    assert topology is not None
    assert {edge["targetVersion"] for edge in topology["edges"]} == {"1.0.0", "2.0.0"}
    before = await store.component_collection_query()
    assert any(item["componentId"] == "aura.test.observed" for item in before["items"])
    store.observed = []
    after = await store.component_collection_query()
    assert not any(item["componentId"] == "aura.test.observed" for item in after["items"])
    assert {item["componentId"] for item in after["items"]} == {
        "aura.test.root",
        "aura.test.target",
    }


@pytest.mark.asyncio
async def test_component_health_bounds_time_version_and_point_order() -> None:
    points = [
        {
            "start_at": datetime(2026, 10, 16, 1, 0, tzinfo=UTC),
            "sample_count": 1,
            "p50": 5,
            "p95": 6,
            "failures": 0,
            "successes": 1,
            "cancelled": 0,
        },
        {
            "start_at": datetime(2026, 10, 16, 2, 0, tzinfo=UTC),
            "sample_count": 1,
            "p50": 7,
            "p95": 8,
            "failures": 0,
            "successes": 1,
            "cancelled": 0,
        },
    ]
    store = FakeProductionStore([_definition("aura.test.health")], points=points)
    result = await store.component_health_query(
        "aura.test.health",
        datetime(2026, 10, 16, tzinfo=UTC),
        datetime(2026, 10, 17, tzinfo=UTC),
        component_version="1.0.0",
        point_limit=2,
    )
    assert result is not None
    assert [point["startAt"] for point in result["points"]] == [
        "2026-10-16T01:00:00Z",
        "2026-10-16T02:00:00Z",
    ]
    query = "\n".join(store.queries)
    assert "component_version = '1.0.0'" in query
    assert "observed_at >= toDateTime64" in query and "observed_at < toDateTime64" in query
    assert "LIMIT 2" in query


@pytest.mark.asyncio
async def test_component_health_counts_multiple_measurements_once_per_execution() -> None:
    store = RawTrendStore()
    result = await store.component_health_query(
        "aura.test.health",
        datetime(2026, 10, 16, tzinfo=UTC),
        datetime(2026, 10, 17, tzinfo=UTC),
        component_version="1.0.0",
        point_limit=1,
    )

    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["outcomes"] == {
        "succeeded": 1,
        "failed": 0,
        "cancelled": 0,
        "unknown": 0,
    }
    assert result["points"][0]["summary"]["sampleCount"] == result["summary"]["sampleCount"]
    assert result["points"][0]["summary"]["outcomes"] == result["summary"]["outcomes"]
    assert any("GROUP BY execution_id" in statement for statement in store.statements)


@pytest.mark.asyncio
async def test_component_definition_distinguishes_not_found_from_storage_failure() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))

    class Port:
        def __init__(self, error: Exception | None) -> None:
            self.error = error

        async def component_definition_query(
            self, _component_id: str, _version: str
        ) -> dict[str, Any] | None:
            if self.error:
                raise self.error
            return None

    app = create_app(settings, auth=auth, query_store=object(), testing=True)
    service = ObservatoryQueryService(MemoryObservatoryStore(), query_port=Port(None))  # type: ignore[arg-type]
    app.state.query = service
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        missing = await client.get("/api/v1/components/aura.test.missing/definitions/1.0.0")
        assert missing.status_code == 404
        assert missing.json()["code"] == "not_found"
        app.state.query = ObservatoryQueryService(
            MemoryObservatoryStore(),
            query_port=Port(StorageUnavailableError("backend unavailable")),  # type: ignore[arg-type]
        )
        unavailable = await client.get(
            "/api/v1/components/aura.test.missing/definitions/1.0.0?retry=1"
        )
        assert unavailable.status_code == 503, unavailable.text
        assert unavailable.json() == {
            "code": "service_unavailable",
            "message": "service unavailable",
            "retryable": True,
            "traceId": unavailable.json()["traceId"],
        }


class OperationalFailureConnection:
    async def __aenter__(self) -> OperationalFailureConnection:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def execute(self, *_args: object) -> None:
        raise psycopg.OperationalError("database is unavailable")


class FailedClickHouseClient:
    async def __aenter__(self) -> FailedClickHouseClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: object, **_kwargs: object) -> httpx.Response:
        request = httpx.Request("POST", "http://clickhouse.invalid")
        return httpx.Response(503, request=request, text="temporary backend failure")


@pytest.mark.asyncio
async def test_concrete_postgres_and_clickhouse_failures_reach_typed_api_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pg_store = ProductionObservatoryStore(
        lambda: OperationalFailureConnection(),
        "http://clickhouse.invalid",
        database="observatory",
        user="test",
        password="test",
    )
    with pytest.raises(StorageUnavailableError) as pg_error:
        await pg_store.component_definition_query("aura.test.missing", "1.0.0")
    assert isinstance(pg_error.value.__cause__, psycopg.OperationalError)

    def failed_client(**_kwargs: object) -> FailedClickHouseClient:
        return FailedClickHouseClient()

    clickhouse_store = ProductionObservatoryStore(
        lambda: OperationalFailureConnection(),
        "http://clickhouse.invalid",
        database="observatory",
        user="test",
        password="test",
    )
    with monkeypatch.context() as isolated_patch:
        isolated_patch.setattr(query_module.httpx, "AsyncClient", failed_client)
        with pytest.raises(StorageUnavailableError) as clickhouse_error:
            await clickhouse_store._query_rows("SELECT 1")  # pyright: ignore[reportPrivateUsage]
    assert isinstance(clickhouse_error.value.__cause__, httpx.HTTPStatusError)

    concrete_clickhouse_failure = clickhouse_error.value

    async def failed_rows(_statement: str) -> list[dict[str, Any]]:
        raise concrete_clickhouse_failure

    clickhouse_store._query_rows = failed_rows  # pyright: ignore[reportAttributeAccessIssue, reportPrivateUsage]

    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    app.state.query = ObservatoryQueryService(MemoryObservatoryStore(), query_port=pg_store)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        pg_response = await client.get("/api/v1/components/aura.test.missing/definitions/1.0.0")
        assert pg_response.status_code == 503
        assert pg_response.json()["code"] == "service_unavailable"

        app.state.query = ObservatoryQueryService(
            MemoryObservatoryStore(), query_port=clickhouse_store
        )
        clickhouse_response = await client.get("/api/v1/components/aura.test.health/health")
        assert clickhouse_response.status_code == 503
        assert clickhouse_response.json()["code"] == "service_unavailable"


def test_migrate_no_argument_path_runs_catalogue_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, object | None]] = []

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, parameters: object | None = None) -> None:
            calls.append((statement, parameters))

        def commit(self) -> None:
            return None

    class Psycopg:
        @staticmethod
        def connect(_url: str) -> Connection:
            return Connection()

    import aura_observatory.entrypoints.cli as cli

    monkeypatch.setattr(cli, "psycopg", Psycopg)
    monkeypatch.setattr(
        cli,
        "Settings",
        lambda: SimpleNamespace(
            database_url="postgresql://test",
            clickhouse_url="http://clickhouse.invalid",
            clickhouse_database="observatory",
            clickhouse_user="test",
            clickhouse_password="test",
        ),
    )

    def no_clickhouse_ledger(*_args: object) -> bool:
        return False

    def no_clickhouse_post(*_args: object) -> None:
        return None

    def no_watermark(*_args: object) -> None:
        return None

    monkeypatch.setattr(cli, "_clickhouse_applied", no_clickhouse_ledger)
    monkeypatch.setattr(cli, "_clickhouse_post", no_clickhouse_post)
    monkeypatch.setattr(cli, "_synchronize_legacy_watermark", no_watermark)
    migrate()
    assert any("observatory_component_definitions" in statement for statement, _ in calls)
    assert any("observatory_component_dependencies" in statement for statement, _ in calls)


def test_no_argument_cli_main_dispatches_migrate_without_typer_help() -> None:
    script = """
import sys
import aura_observatory.entrypoints.cli as cli

def migrate():
    print('migrate-dispatched')

def fail_help():
    raise AssertionError('Typer help was invoked')

cli.migrate = migrate
cli.app = fail_help
sys.argv = ['aura-observatory-migrate']
cli.main()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SERVICE_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout.strip() == "migrate-dispatched"
    assert "Usage" not in completed.stdout


def test_subcommand_cli_main_delegates_sync_manifests_to_typer_app() -> None:
    script = """
import sys
import aura_observatory.entrypoints.cli as cli

def fail_migrate():
    raise AssertionError('migration default was selected for an explicit subcommand')

def app():
    print(sys.argv[1])

cli.migrate = fail_migrate
cli.app = app
sys.argv = ['aura-observatory', 'sync-manifests']
cli.main()
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SERVICE_ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.stdout.strip() == "sync-manifests"


def test_real_manifests_declare_provider_dependencies() -> None:
    documents = {
        path.stem: __import__("yaml").safe_load(path.read_text(encoding="utf-8"))
        for path in (SERVICE_ROOT / "resources/component-manifests").glob("*.yaml")
    }
    assert documents
    for document in documents.values():
        component = document["component"]
        if component["id"] in {"aura.observatory.telemetry_storage", "aura.observatory.query_api"}:
            assert document.get("dependencies"), component["id"]


@pytest.mark.asyncio
async def test_query_metrics_record_success_and_failure() -> None:
    class Port:
        async def components_query(self) -> list[dict[str, Any]]:
            return []

        async def component_collection_query(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("query failed")

    metrics = ObservatoryMetrics()
    service = ObservatoryQueryService(object(), metrics=metrics, query_port=Port())  # type: ignore[arg-type]
    assert await service.components() == []
    with pytest.raises(RuntimeError):
        await service.component_collection()
    rendered = metrics.prometheus()
    assert 'observatory_query_outcome{outcome="succeeded",resource="components"} 1' in rendered
    assert 'observatory_query_outcome{outcome="failed",resource="components"} 1' in rendered


@pytest.mark.asyncio
async def test_component_resource_queries_emit_one_success_sample_each() -> None:
    class Port:
        async def component_collection_query(self, **_kwargs: Any) -> dict[str, Any]:
            return {"items": [], "nextCursor": None}

        async def component_definition_query(
            self, _component_id: str, _component_version: str
        ) -> dict[str, Any] | None:
            return None

        async def component_dependencies_query(
            self,
            _component_id: str,
            _component_version: str,
            *,
            depth: int,
            max_nodes: int,
        ) -> dict[str, Any] | None:
            assert depth == 1
            assert max_nodes == 100
            return None

    metrics = ObservatoryMetrics()
    service = ObservatoryQueryService(object(), metrics=metrics, query_port=Port())  # type: ignore[arg-type]
    calls = (
        lambda: service.component_collection(limit=1),
        lambda: service.component_definition("aura.test.missing", "1.0.0"),
        lambda: service.component_dependencies("aura.test.root", "1.0.0"),
    )
    for call in calls:
        before_values = dict(metrics.values)
        before_histograms = dict(metrics._histogram_counts)  # pyright: ignore[reportPrivateUsage]
        await call()
        duration_key = (
            "observatory_query_duration_ms",
            (("resource", "components"),),
        )
        outcome_key = (
            "observatory_query_outcome",
            (("outcome", "succeeded"), ("resource", "components")),
        )
        assert metrics._histogram_counts[duration_key] - before_histograms.get(  # pyright: ignore[reportPrivateUsage]
            duration_key, 0
        ) == 1
        assert metrics.values[outcome_key] - before_values.get(outcome_key, 0) == 1


@pytest.mark.asyncio
async def test_component_resource_query_failures_emit_one_sample_each() -> None:
    class FailingPort:
        async def component_collection_query(self, **_kwargs: Any) -> dict[str, Any]:
            raise RuntimeError("collection failure")

        async def component_definition_query(
            self, _component_id: str, _component_version: str
        ) -> dict[str, Any] | None:
            raise RuntimeError("definition failure")

        async def component_dependencies_query(
            self,
            _component_id: str,
            _component_version: str,
            *,
            depth: int,
            max_nodes: int,
        ) -> dict[str, Any] | None:
            del depth, max_nodes
            raise RuntimeError("dependencies failure")

    metrics = ObservatoryMetrics()
    service = ObservatoryQueryService(
        object(), metrics=metrics, query_port=FailingPort()  # type: ignore[arg-type]
    )
    calls = (
        lambda: service.component_collection(limit=1),
        lambda: service.component_definition("aura.test.missing", "1.0.0"),
        lambda: service.component_dependencies("aura.test.root", "1.0.0"),
    )
    for call in calls:
        before_values = dict(metrics.values)
        before_histograms = dict(metrics._histogram_counts)  # pyright: ignore[reportPrivateUsage]
        with pytest.raises(RuntimeError):
            await call()
        duration_key = (
            "observatory_query_duration_ms",
            (("resource", "components"),),
        )
        outcome_key = (
            "observatory_query_outcome",
            (("outcome", "failed"), ("resource", "components")),
        )
        assert metrics._histogram_counts[duration_key] - before_histograms.get(  # pyright: ignore[reportPrivateUsage]
            duration_key, 0
        ) == 1
        assert metrics.values[outcome_key] - before_values.get(outcome_key, 0) == 1


@pytest.mark.asyncio
async def test_legacy_component_collection_does_not_double_count_metrics() -> None:
    class LegacyStore:
        async def components(self) -> list[dict[str, Any]]:
            return []

    metrics = ObservatoryMetrics()
    service = ObservatoryQueryService(LegacyStore(), metrics=metrics)  # type: ignore[arg-type]
    before_histograms = dict(metrics._histogram_counts)  # pyright: ignore[reportPrivateUsage]
    before_values = dict(metrics.values)
    result = await service.component_collection(limit=1)
    assert result["items"] == []
    duration_key = (
        "observatory_query_duration_ms",
        (("resource", "components"),),
    )
    success_key = (
        "observatory_query_outcome",
        (("outcome", "succeeded"), ("resource", "components")),
    )
    assert metrics._histogram_counts[duration_key] - before_histograms.get(  # pyright: ignore[reportPrivateUsage]
        duration_key, 0
    ) == 1
    assert metrics.values[success_key] - before_values.get(success_key, 0) == 1
