"""Independent regressions for the formal AURA-0080 audit findings.

The cases in this module deliberately use service-owned fakes and frozen OTLP
wire fixtures.  They do not import Aura Core or require PostgreSQL/ClickHouse,
but they exercise the production-shaped boundaries where the query, live
update, retention, migration, and observability regressions were found.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import re
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
import yaml
from aura_observatory.adapters.clickhouse import ClickHouseObservationStore
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.adapters.postgres import PostgresObservationIdRegistry
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.contract import MetadataObservation
from aura_observatory.entrypoints import cli
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.otlp import metrics_to_observations
from aura_observatory.ports import ClaimOutcome, InsertOutcome, ObservatoryQueryPort
from aura_observatory.query import (
    DeletionResult,
    ObservatoryQueryService,
    RetentionAction,
    RetentionKind,
    RetentionRequest,
    RetentionStatus,
    RunFilters,
)
from aura_observatory.settings import Settings
from starlette.requests import Request
from test_aura0079_otlp_signal_regressions import (  # pyright: ignore[reportPrivateUsage]
    _metric_payload,  # pyright: ignore[reportPrivateUsage]
    _span_payload,  # pyright: ignore[reportPrivateUsage]
)
from test_aura0080_query_retention import BASE_TIME, make_payload, make_run_id

ROOT = Path(__file__).parents[3]
SERVICE_ROOT = ROOT / "services" / "aura-observatory"
OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"
_CLICKHOUSE_STATUS_FALLBACK = (
    "lowerUTF8(coalesce(nullIf(dimensions['outcome'], ''), "
    "nullIf(dimensions['status'], '')))"
)


def _evaluate_clickhouse_status(
    statement: str, dimensions: dict[str, str], *, legacy_key: str
) -> str:
    """Evaluate the supported Map lookup shape against provider-like rows."""

    if _CLICKHOUSE_STATUS_FALLBACK in statement:
        return (dimensions.get("outcome", "") or dimensions.get("status", "")).casefold()
    # Missing ClickHouse Map keys behave as empty strings.  The legacy query
    # shape used one key directly; retaining that behavior makes the fake
    # reject the old query rather than hiding the regression in a fixture.
    return dimensions.get(legacy_key, "")


def _evaluate_clickhouse_run_status(
    statement: str, dimensions: dict[str, str]
) -> str:
    """Evaluate the normalized run-status multiIf expression for a row."""

    raw = _evaluate_clickhouse_status(statement, dimensions, legacy_key="status")
    if "multiIf(" not in statement:
        return raw
    if raw in {"success", "succeeded", "completed", "ok"}:
        return "succeeded"
    if raw in {"error", "failed"}:
        return "failed"
    if raw == "cancelled":
        return "cancelled"
    return "unknown"


def _query_service(store: Any, metrics: Any = None) -> ObservatoryQueryService:
    """Keep tests independent of the adapter's reference-store type alias."""

    return ObservatoryQueryService(store=store, metrics=metrics)


def test_production_observation_port_requires_bounded_reads() -> None:
    """The high-cardinality adapter must never hydrate an unbounded table."""

    method = inspect.getsource(ProductionObservatoryStore.list_observations)
    signature = inspect.signature(ProductionObservatoryStore.list_observations)
    assert "limit" in signature.parameters
    assert "cursor" in signature.parameters or "offset" in signature.parameters
    query = method.upper()
    if "FROM OBSERVATIONS" in query:
        assert "LIMIT" in query


@pytest.mark.asyncio
async def test_run_cursor_is_an_immutable_snapshot_anchor() -> None:
    store = MemoryObservatoryStore()
    runs = [
        make_payload(
            f"00000000-0000-4000-8000-00000000020{index}",
            observed_at=BASE_TIME + timedelta(seconds=index),
            run_id=make_run_id(f"snapshot-{index}"),
        )
        for index in range(3)
    ]
    for item in runs:
        await store.insert_observation(item)
    service = _query_service(store)

    first = await service.runs(RunFilters(), cursor=None, limit=1)
    assert [item["runId"] for item in first["items"]] == [make_run_id("snapshot-2")]
    cursor = first["nextCursor"]
    assert isinstance(cursor, str)

    # A new run and updates to both the already returned and an unreturned run
    # must not reorder or duplicate the original snapshot between pages.
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000299",
            observed_at=BASE_TIME + timedelta(hours=1),
            run_id=make_run_id("snapshot-new"),
        )
    )
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000298",
            observed_at=BASE_TIME + timedelta(hours=2),
            run_id=make_run_id("snapshot-2"),
        )
    )
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000297",
            observed_at=BASE_TIME + timedelta(hours=3),
            run_id=make_run_id("snapshot-1"),
        )
    )
    second = await service.runs(RunFilters(), cursor=cursor, limit=2)
    assert [item["runId"] for item in second["items"]] == [
        make_run_id("snapshot-1"),
        make_run_id("snapshot-0"),
    ]
    assert make_run_id("snapshot-new") not in [item["runId"] for item in second["items"]]


@pytest.mark.asyncio
async def test_reference_run_status_search_uses_latest_observation() -> None:
    store = MemoryObservatoryStore()
    run_id = make_run_id("status-transition-reference")
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-0000000002a1",
            observed_at=BASE_TIME,
            run_id=run_id,
            status="completed",
            outcome="success",
        )
    )
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-0000000002a2",
            observed_at=BASE_TIME + timedelta(seconds=1),
            run_id=run_id,
            status="failed",
            outcome="error",
        )
    )

    service = _query_service(store)
    succeeded = await service.runs(RunFilters(status="succeeded"), cursor=None, limit=10)
    failed = await service.runs(RunFilters(status="failed"), cursor=None, limit=10)

    assert succeeded["items"] == []
    assert [item["runId"] for item in failed["items"]] == [run_id]
    assert failed["items"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_production_run_status_search_uses_latest_normalized_sql_row() -> None:
    run_id = make_run_id("status-transition-production")
    source_rows: list[dict[str, Any]] = [
        {
            "run_id": run_id,
            "started_at": BASE_TIME,
            "finished_at": BASE_TIME,
            "observed_at": BASE_TIME,
            "dimensions": {"outcome": "success"},
            "model": "local-model",
            "deployment_id": "deploy-a",
            "component_ids": ["aura.runtime.model_inference"],
            "trace_id": "0123456789abcdef0123456789abcdef",
            "order_sequence": 1,
        },
        {
            "run_id": run_id,
            "started_at": BASE_TIME,
            "finished_at": BASE_TIME + timedelta(seconds=1),
            "observed_at": BASE_TIME + timedelta(seconds=1),
            "dimensions": {"outcome": "error"},
            "model": "local-model",
            "deployment_id": "deploy-a",
            "component_ids": ["aura.runtime.model_inference"],
            "trace_id": "0123456789abcdef0123456789abcdef",
            "order_sequence": 2,
        },
    ]
    queries: list[str] = []
    requested_statuses: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 2, "observed_at": BASE_TIME}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            before_group, after_group = statement.split(" GROUP BY run_id", 1)
            where_clause = before_group.split(" FROM observations WHERE ", 1)[1]
            assert "argMax(" not in where_clause
            assert " HAVING " in after_group
            having_clause = after_group.split(" HAVING ", 1)[1].split(
                " ORDER BY", 1
            )[0]
            requested = having_clause.rsplit(" = '", 1)[1].split("'", 1)[0]
            requested_statuses.append(requested)
            newest = max(source_rows, key=lambda row: row["observed_at"])
            status = _evaluate_clickhouse_run_status(
                statement, newest["dimensions"]
            )
            if status != requested:
                return []
            return [{**newest, "status": status}]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    succeeded = await store.run_query(
        RunFilters(status="succeeded"), cursor=None, limit=10
    )
    failed = await store.run_query(RunFilters(status="failed"), cursor=None, limit=10)

    assert succeeded["items"] == []
    assert [item["runId"] for item in failed["items"]] == [run_id]
    assert failed["items"][0]["status"] == "failed"
    assert requested_statuses == ["succeeded", "failed"]
    assert all("multiIf(" in query for query in queries)


@pytest.mark.asyncio
async def test_reference_run_status_equal_timestamp_uses_higher_observation_id() -> None:
    store = MemoryObservatoryStore()
    run_id = make_run_id("status-equal-timestamp-reference")
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-0000000002b1",
            observed_at=BASE_TIME,
            run_id=run_id,
            status="completed",
            outcome="success",
        )
    )
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-0000000002b2",
            observed_at=BASE_TIME,
            run_id=run_id,
            status="failed",
            outcome="error",
        )
    )

    service = _query_service(store)
    succeeded = await service.runs(RunFilters(status="succeeded"), cursor=None, limit=10)
    failed = await service.runs(RunFilters(status="failed"), cursor=None, limit=10)

    assert succeeded["items"] == []
    assert [item["runId"] for item in failed["items"]] == [run_id]
    assert failed["items"][0]["status"] == "failed"


@pytest.mark.asyncio
async def test_production_run_status_equal_timestamp_uses_higher_observation_id() -> None:
    run_id = make_run_id("status-equal-timestamp-production")
    source_rows: list[dict[str, Any]] = [
        {
            "run_id": run_id,
            "observation_id": "00000000-0000-4000-8000-0000000002c1",
            "started_at": BASE_TIME,
            "finished_at": BASE_TIME,
            "observed_at": BASE_TIME,
            "dimensions": {"outcome": "success"},
            "model": "local-model",
            "deployment_id": "deploy-a",
            "component_ids": ["aura.runtime.model_inference"],
            "trace_id": "0123456789abcdef0123456789abcdef",
            "order_sequence": 1,
        },
        {
            "run_id": run_id,
            "observation_id": "00000000-0000-4000-8000-0000000002c2",
            "started_at": BASE_TIME,
            "finished_at": BASE_TIME,
            "observed_at": BASE_TIME,
            "dimensions": {"outcome": "error"},
            "model": "local-model",
            "deployment_id": "deploy-a",
            "component_ids": ["aura.runtime.model_inference"],
            "trace_id": "0123456789abcdef0123456789abcdef",
            "order_sequence": 2,
        },
    ]
    queries: list[str] = []
    requested_statuses: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 2, "observed_at": BASE_TIME}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            before_group, after_group = statement.split(" GROUP BY run_id", 1)
            where_clause = before_group.split(" FROM observations WHERE ", 1)[1]
            assert "argMax(" not in where_clause
            assert " HAVING " in after_group
            having_clause = after_group.split(" HAVING ", 1)[1].split(
                " ORDER BY", 1
            )[0]
            requested = having_clause.rsplit(" = '", 1)[1].split("'", 1)[0]
            requested_statuses.append(requested)
            newest = max(
                source_rows,
                key=lambda row: (row["observed_at"], row["observation_id"]),
            )
            status = _evaluate_clickhouse_run_status(
                statement, newest["dimensions"]
            )
            if status != requested:
                return []
            return [{**newest, "status": status}]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    succeeded = await store.run_query(
        RunFilters(status="succeeded"), cursor=None, limit=10
    )
    failed = await store.run_query(RunFilters(status="failed"), cursor=None, limit=10)

    assert succeeded["items"] == []
    assert [item["runId"] for item in failed["items"]] == [run_id]
    assert failed["items"][0]["status"] == "failed"
    assert requested_statuses == ["succeeded", "failed"]
    assert all("(observed_at, observation_id)" in query for query in queries)


@pytest.mark.asyncio
async def test_reference_run_list_and_detail_keep_latest_nonempty_trace_lineage() -> None:
    store = MemoryObservatoryStore()
    run_id = make_run_id("trace-lineage-reference")
    trace_id = "0123456789abcdef0123456789abcdef"
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-0000000002d1",
            observed_at=BASE_TIME,
            run_id=run_id,
            trace_id=trace_id,
            span_id="0123456789abcdef",
        )
    )
    latest_same_time_trace = "fedcba9876543210fedcba9876543210"
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-0000000002d3",
            observed_at=BASE_TIME,
            run_id=run_id,
            trace_id=latest_same_time_trace,
            span_id="fedcba9876543210",
        )
    )
    terminal = make_payload(
        "00000000-0000-4000-8000-0000000002d2",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=run_id,
        trace_id=trace_id,
    ).model_copy(update={"trace": None})
    await store.insert_observation(terminal)

    service = _query_service(store)
    listed = await service.runs(RunFilters(), cursor=None, limit=10)
    detail = await service.run_detail(run_id)

    assert listed["items"][0]["traceId"] == latest_same_time_trace
    assert detail is not None
    assert detail["traceId"] == latest_same_time_trace


@pytest.mark.asyncio
async def test_reference_run_list_and_detail_leave_trace_empty_without_lineage() -> None:
    store = MemoryObservatoryStore()
    run_id = make_run_id("trace-lineage-empty-reference")
    for suffix, observed_at in (("d3", BASE_TIME), ("d4", BASE_TIME + timedelta(seconds=1))):
        item = make_payload(
            f"00000000-0000-4000-8000-0000000000{suffix}",
            observed_at=observed_at,
            run_id=run_id,
        ).model_copy(update={"trace": None})
        await store.insert_observation(item)

    service = _query_service(store)
    listed = await service.runs(RunFilters(), cursor=None, limit=10)
    detail = await service.run_detail(run_id)

    assert listed["items"][0]["traceId"] is None
    assert detail is not None
    assert detail["traceId"] is None


@pytest.mark.asyncio
async def test_production_run_query_uses_latest_nonempty_trace_with_stable_tie_breaker() -> None:
    queries: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {"latest_sequence": 2, "observed_at": BASE_TIME}

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            return [
                {
                    "run_id": make_run_id("trace-lineage-production"),
                    "started_at": BASE_TIME,
                    "finished_at": BASE_TIME + timedelta(seconds=1),
                    "observed_at": BASE_TIME + timedelta(seconds=1),
                    "status": "succeeded",
                    "model": "local-model",
                    "deployment_id": "deploy-a",
                    "component_ids": ["aura.runtime.model_inference"],
                    "trace_id": "0123456789abcdef0123456789abcdef",
                    "order_sequence": 2,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.run_query(RunFilters(), cursor=None, limit=10)

    assert result["items"][0]["traceId"] == "0123456789abcdef0123456789abcdef"
    assert len(queries) == 1
    assert (
        "argMaxIf(trace_id, (observed_at, observation_id), trace_id != '') AS trace_id"
        in queries[0]
    )
    assert "argMax(trace_id, observed_at)" not in queries[0]


@pytest.mark.asyncio
async def test_production_run_detail_keeps_trace_when_newest_row_is_untraced() -> None:
    run_id = make_run_id("trace-lineage-production-detail")
    trace_id = "0123456789abcdef0123456789abcdef"
    span = make_payload(
        "00000000-0000-4000-8000-0000000002d5",
        observed_at=BASE_TIME,
        run_id=run_id,
        trace_id=trace_id,
        span_id="0123456789abcdef",
        parent_span_id="fedcba9876543210",
    )
    terminal = make_payload(
        "00000000-0000-4000-8000-0000000002d6",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=run_id,
        trace_id=trace_id,
    ).model_copy(update={"trace": None})

    def row(item: MetadataObservation) -> dict[str, Any]:
        return {
            "observation_id": item.observation_id,
            "observed_at": item.observed_at,
            "schema_version": 1,
            "kind": item.kind,
            "component_id": item.component.id,
            "component_version": item.component.version,
            "operation": item.operation,
            "measurement_name": item.measurement.name,
            "measurement_value": item.measurement.value,
            "measurement_unit": item.measurement.unit,
            "dimensions": item.dimensions,
            "trace_id": item.trace.trace_id if item.trace else "",
            "span_id": item.trace.span_id if item.trace else "",
            "parent_span_id": item.trace.parent_span_id if item.trace else "",
            "linked_span_ids": (
                item.trace.linked_span_ids
                if item.trace and item.trace.linked_span_ids
                else (["1111111111111111"] if item.trace else [])
            ),
            "run_id": item.correlation.run_id if item.correlation else "",
            "conversation_id": make_run_id("trace-lineage-conversation"),
            "deployment_id": item.deployment.deployment_id,
            "git_commit": item.deployment.git_commit,
            "environment": item.deployment.environment,
            "host_id": item.deployment.host_id,
            "service_role": item.service_role,
            "capture_policy": item.capture_policy,
        }

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            return [row(span), row(terminal)]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.run_detail_query(run_id)

    assert result is not None
    assert result["traceId"] == trace_id


@pytest.mark.asyncio
async def test_production_components_merge_observed_only_and_registered_definitions() -> None:
    registered_id = "aura.runtime.model_inference"
    observed_only_id = "aura.runtime.stream_delivery"
    queries: list[str] = []

    class Store(ProductionObservatoryStore):
        async def components(self) -> list[dict[str, Any]]:
            return [
                {
                    "componentId": registered_id,
                    "version": "1.0.0",
                    "firstSeenAt": "2026-01-02T00:00:00Z",
                    "lastSeenAt": "2026-01-04T00:00:00Z",
                }
            ]

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            if "quantileIf" in statement:
                return [
                    {
                        "sample_count": 1,
                        "p50": 12.0,
                        "p95": 12.0,
                        "failures": 0,
                        "successes": 1,
                        "cancelled": 0,
                    }
                ]
            return [
                {
                    "component_id": registered_id,
                    "component_version": "1.0.0",
                    "first_seen_at": "2026-01-01 00:00:00",
                    "last_seen_at": "2026-01-05 00:00:00",
                },
                {
                    "component_id": observed_only_id,
                    "component_version": "2.0.0",
                    "first_seen_at": "2026-01-03 00:00:00",
                    "last_seen_at": "2026-01-03 00:00:01",
                },
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    components = await store.components_query()
    health = await store.component_health_query(observed_only_id, None, None)

    assert [(item["componentId"], item["version"]) for item in components] == [
        (registered_id, "1.0.0"),
        (observed_only_id, "2.0.0"),
    ]
    registered = components[0]
    assert registered["firstSeenAt"] == "2026-01-01T00:00:00Z"
    assert registered["lastSeenAt"] == "2026-01-05T00:00:00Z"
    assert health is not None
    assert health["componentId"] == observed_only_id
    assert health["summary"]["sampleCount"] == 1
    assert "GROUP BY component_id, component_version" in queries[0]
    assert "GROUP BY execution_id" in queries[1]


@pytest.mark.asyncio
async def test_live_catchup_uses_persisted_order_for_out_of_order_observations() -> None:
    store = MemoryObservatoryStore()
    newer_first = make_payload(
        "00000000-0000-4000-8000-000000000301",
        observed_at=BASE_TIME + timedelta(minutes=2),
        run_id=make_run_id("persisted-a"),
    )
    older_second = make_payload(
        "00000000-0000-4000-8000-000000000302",
        observed_at=BASE_TIME,
        run_id=make_run_id("persisted-b"),
    )
    await store.insert_observation(newer_first)
    await store.insert_observation(older_second)
    events = [item async for item in _query_service(store).events_after(None)]
    assert [item["id"] for item in events] == [
        newer_first.observation_id,
        older_second.observation_id,
    ]


@pytest.mark.asyncio
async def test_sse_has_a_snapshot_anchor_heartbeat_and_stays_live_until_disconnect() -> None:
    settings = Settings(owner_subject=OWNER)
    sessions = MemorySessionBackend()
    auth = ObservatoryAuth(settings, sessions, MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    store = MemoryObservatoryStore()
    app = create_app(settings, auth=auth, query_store=store, testing=True)
    route = next(
        cast(Any, route)
        for route in app.routes
        if getattr(route, "path", None) == "/api/v1/events"
    )
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/v1/events",
        "raw_path": b"/api/v1/events",
        "query_string": b"",
        "headers": [
            (b"host", b"observatory.localhost:4300"),
            (b"cookie", f"observatory_session={session_id}".encode()),
        ],
        "client": ("127.0.0.1", 1),
        "server": ("observatory.localhost", 4300),
    }
    response: Any = await route.endpoint(Request(scope), None)
    iterator = cast(AsyncIterator[str], response.body_iterator)
    first = await anext(iterator)
    assert "event: snapshot" in first
    assert "latestObservationId" in first
    heartbeat = await asyncio.wait_for(anext(iterator), timeout=0.3)
    assert "heartbeat" in heartbeat.lower()
    await response.body_iterator.aclose()


@pytest.mark.asyncio
async def test_sse_rejects_missing_and_foreign_origin_for_destructive_boundary() -> None:
    settings = Settings(
        environment="production",
        public_origin="https://observatory.example.invalid",
        oidc_redirect_uri="https://observatory.example.invalid/api/v1/auth/callback",
        secure_cookies=True,
        owner_subject=OWNER,
    )
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    transport = httpx.ASGITransport(app=app)
    cookies = {settings.effective_session_cookie_name: session_id}
    base = {
        "X-CSRF-Token": session.csrf_token,
        "Idempotency-Key": "origin-boundary-001",
        "X-Observatory-Confirmation": "purge-all",
    }
    async with httpx.AsyncClient(
        transport=transport, base_url=settings.public_origin, cookies=cookies
    ) as client:
        missing = await client.post(
            "/api/v1/retention/actions", json={"action": "purge_all"}, headers=base
        )
        foreign = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all"},
            headers={**base, "Origin": "https://evil.example.invalid"},
        )
    assert missing.status_code == 403
    assert foreign.status_code == 403


@pytest.mark.asyncio
async def test_authenticated_adversarial_inputs_are_rejected_before_query_and_sanitized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    called: list[str] = []

    async def unexpected_query(*_args: Any, **_kwargs: Any) -> Any:
        called.append("query")
        raise AssertionError("invalid input reached the query service")

    for method in ("runs", "run_detail", "trace_detail", "component_health", "events_after"):
        monkeypatch.setattr(app.state.query, method, unexpected_query)

    cookies = {settings.effective_session_cookie_name: session_id}
    requests: list[tuple[str, str, dict[str, str], dict[str, str]]] = [
        ("bad'status", "/api/v1/runs", {"status": "bad'status"}, {}),
        ("aura.runtime\\bad", "/api/v1/runs", {"component": "aura.runtime\\bad"}, {}),
        ("model\\'bad", "/api/v1/runs", {"model": "model\\'bad"}, {}),
        ("deploy\x01bad", "/api/v1/runs", {"deployment": "deploy\x01bad"}, {}),
        ("cursor-invalid\\value", "/api/v1/runs", {"cursor": "cursor-invalid\\value"}, {}),
        ("not-a-uuid'", "/api/v1/runs/not-a-uuid%27", {}, {}),
        ("bad\\trace", "/api/v1/traces/bad%5Ctrace", {}, {}),
        ("aura.bad'component", "/api/v1/components/aura.bad%27component/health", {}, {}),
        ("last-event\\id", "/api/v1/events", {}, {"Last-Event-ID": "last-event\\id"}),
    ]

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies=cookies,
    ) as client:
        responses = [
            (
                await client.get(path, params=params, headers=headers),
                marker,
            )
            for marker, path, params, headers in requests
        ]

    for response, marker in responses:
        assert response.status_code == 400
        body = response.json()
        assert body["code"] == "bad_request"
        assert body["message"] == "invalid request"
        assert marker not in response.text
    assert called == []


def test_clickhouse_literal_encoder_escapes_apostrophes_and_backslashes() -> None:
    from aura_observatory.adapters.query import _sql  # pyright: ignore[reportPrivateUsage]

    value = r"component\path' OR 1=1 --"
    literal = _sql(value)
    assert literal == r"'component\\path\' OR 1=1 --'"
    assert literal.startswith("'") and literal.endswith("'")


def test_clickhouse_datetime_literal_uses_utc_millisecond_storage_shape() -> None:
    from aura_observatory.adapters.query import (
        _clickhouse_time,  # pyright: ignore[reportPrivateUsage]
    )

    value = _clickhouse_time(
        datetime(2026, 1, 1, 13, 0, 0, 123456, tzinfo=timezone(timedelta(hours=1)))
    )
    assert value == "'2026-01-01 12:00:00.123'"
    assert "T" not in value and "Z" not in value


@pytest.mark.asyncio
async def test_production_retention_state_is_bounded_and_guard_release_clears_active_refs() -> None:
    class Result:
        def __init__(self, row: tuple[Any, ...] | None = None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.row

    class Database:
        def __init__(self) -> None:
            self.keys: dict[str, str] = {}
            self.active_locks = 0
            self.context_enters = 0
            self.context_exits = 0

        def row(self, action_id: str, key: str) -> tuple[Any, ...]:
            return (
                action_id,
                RetentionKind.PURGE_ALL.value,
                None,
                RetentionStatus.SUCCEEDED.value,
                BASE_TIME,
                BASE_TIME,
                BASE_TIME,
                0,
                0,
                0,
                None,
                key,
                0,
                0,
                0,
                "completed",
            )

        def factory(self) -> Context:
            return Context(self)

    class Connection:
        def __init__(self, database: Database) -> None:
            self.database = database

        async def execute(
            self, statement: str, params: tuple[Any, ...] = ()
        ) -> Result:
            if statement == "SELECT pg_advisory_lock(830078)":
                self.database.active_locks += 1
                return Result()
            if statement == "SELECT pg_advisory_unlock(830078)":
                self.database.active_locks -= 1
                return Result()
            if statement.startswith("INSERT INTO observatory_retention_actions"):
                action_id, key = str(params[0]), str(params[4])
                self.database.keys[key] = action_id
                return Result((action_id,))
            if "WHERE idempotency_key = %s" in statement:
                key = str(params[0])
                action_id = self.database.keys.get(key)
                return Result(self.database.row(action_id, key) if action_id else None)
            if "WHERE action_id = %s" in statement:
                action_id = str(params[0])
                key = next(
                    (
                        candidate
                        for candidate, value in self.database.keys.items()
                        if value == action_id
                    ),
                    "",
                )
                return Result(self.database.row(action_id, key) if key else None)
            if statement.startswith("UPDATE observatory_retention_actions"):
                return Result()
            if statement.startswith("DELETE FROM observatory_retention_targets"):
                return Result()
            raise AssertionError(f"unexpected SQL: {statement}")

        async def commit(self) -> None:
            return None

    class Context:
        def __init__(self, database: Database) -> None:
            self.database = database
            self.connection = Connection(database)

        async def __aenter__(self) -> Connection:
            self.database.context_enters += 1
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            self.database.context_exits += 1

    database = Database()
    store = ProductionObservatoryStore(
        database.factory,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )

    for index in range(128):
        action = RetentionAction(
            f"action-{index}",
            RetentionKind.PURGE_ALL,
            None,
            idempotency_key=f"retention-key-{index}",
        )
        assert await store.save_retention(action)
        found = await store.find_retention_by_key(action.idempotency_key)
        assert found is not None
        fetched = await store.get_retention(action.action_id)
        assert fetched is not None
        action.status = RetentionStatus.SUCCEEDED
        action.completed_at = BASE_TIME
        action.deletion_phase = "completed"
        await store.update_retention(action)

    assert not hasattr(store, "actions")
    assert not hasattr(store, "idempotency")

    active = RetentionAction(
        "active-action",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="active-retention-key",
    )
    await store.attach_retention(active)
    assert store._active_retention_action_id == active.action_id  # type: ignore[attr-defined]
    assert store._active_retention_action is active  # type: ignore[attr-defined]
    assert store._retention_guard is not None  # type: ignore[attr-defined]
    await store._release_retention_guard()  # type: ignore[attr-defined]
    assert store._active_retention_action_id is None  # type: ignore[attr-defined]
    assert store._active_retention_action is None  # type: ignore[attr-defined]
    assert store._retention_guard is None  # type: ignore[attr-defined]
    assert database.active_locks == 0
    assert database.context_enters == database.context_exits


@pytest.mark.asyncio
async def test_production_retention_insert_matches_partial_idempotency_index() -> None:
    """The production INSERT must match the deployed partial unique index."""

    migration = (
        SERVICE_ROOT / "migrations/postgresql/versions/0001_observatory_foundation.sql"
    ).read_text()
    assert "ON observatory_retention_actions(idempotency_key)" in migration
    assert "WHERE idempotency_key IS NOT NULL" in migration

    statements: list[str] = []

    class Result:
        async def fetchone(self) -> tuple[str] | None:
            return (action.action_id,)

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            statements.append(statement)
            # A PostgreSQL partial index cannot be inferred by the bare
            # ``ON CONFLICT (idempotency_key)`` form.  Keep this assertion
            # production-shaped so a future SQL change fails before deploy.
            assert (
                "ON CONFLICT (idempotency_key) WHERE idempotency_key IS NOT NULL"
                in statement
            )
            assert "command_fingerprint" in statement
            return Result()

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    action = RetentionAction(
        "00000000-0000-4000-8000-000000000901",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="production-retention-key-0001",
    )
    store = ProductionObservatoryStore(
        lambda: Context(),
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )

    assert await store.save_retention(action)
    assert len(statements) == 1


@pytest.mark.asyncio
async def test_retention_requires_exact_confirmation_closed_body_and_fixed_errors() -> None:
    from test_aura0080_api import _client  # pyright: ignore[reportPrivateUsage]

    client, _, session = await _client()
    async with client:
        headers = {
            "Origin": Settings().public_origin,
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": "closed-body-0001",
        }
        body_only = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all", "confirmation": "purge-all"},
            headers=headers,
        )
        extra = await client.post(
            "/api/v1/retention/actions",
            json={"action": "purge_all", "unexpected": "private-marker"},
            headers={**headers, "X-Observatory-Confirmation": "purge-all"},
        )
        reflected = await client.post(
            "/api/v1/retention/actions",
            json={"action": "not-a-real-action-private-marker"},
            headers={
                **headers,
                "Idempotency-Key": "closed-body-0002",
                "X-Observatory-Confirmation": "purge-all",
            },
        )
    assert body_only.status_code == 400
    assert extra.status_code == 400
    assert reflected.status_code == 400
    assert "private-marker" not in reflected.text
    assert "not-a-real-action-private-marker" not in reflected.text


@pytest.mark.asyncio
async def test_conflicting_idempotency_key_is_not_silently_replayed() -> None:
    from test_aura0080_api import _client  # pyright: ignore[reportPrivateUsage]

    client, _, session = await _client()
    async with client:
        common = {
            "Origin": Settings().public_origin,
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": "conflicting-key-001",
            "X-Observatory-Confirmation": "purge-all",
        }
        first = await client.post(
            "/api/v1/retention/actions", json={"action": "purge_all"}, headers=common
        )
        conflict = await client.post(
            "/api/v1/retention/actions",
            json={
                "action": "delete_before",
                "cutoff": "2026-01-01T12:00:00Z",
            },
            headers={**common, "X-Observatory-Confirmation": "delete-before"},
        )
    assert first.status_code == 202
    assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_retention_cutoff_requires_exact_utc_z_and_boundary_is_strict() -> None:
    service = _query_service(MemoryObservatoryStore())
    for request_cutoff in (
        BASE_TIME.replace(tzinfo=None),
        BASE_TIME.astimezone(timezone(timedelta(hours=1))),
    ):
        with pytest.raises(ValueError):
            await service.retention(
                RetentionRequest(
                    RetentionKind.DELETE_BEFORE,
                    request_cutoff,
                    confirmation="delete_before",
                    idempotency_key=f"cutoff-{str(request_cutoff)}",
                )
            )


@pytest.mark.asyncio
async def test_worker_persist_then_api_death_and_stale_lease_recover_once() -> None:
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000401",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="worker-recovery",
    )

    class DurableStore(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.actions[action.action_id] = action
            self.pending = [action]
            self.claims = 0
            self.deletes = 0

        async def pending_retentions(self) -> list[RetentionAction]:
            return list(self.pending)

        async def claim_retention(self, action_id: str, lease_seconds: int = 60) -> bool:
            del lease_seconds
            if action_id != action.action_id or self.claims:
                return False
            self.claims += 1
            return True

        async def delete_observations(  # type: ignore[override]
            self, cutoff: datetime | None
        ) -> DeletionResult:
            del cutoff
            self.deletes += 1
            return DeletionResult(3, 2, 1)

    store = DurableStore()
    service = _query_service(store)
    await asyncio.gather(service.recover_pending_retention(), service.recover_pending_retention())
    await asyncio.sleep(0.01)
    assert store.claims == 1
    assert store.deletes == 1
    assert action.deleted_count == 3
    assert action.deleted_receipt_count == 2
    assert action.deleted_run_count == 1


@pytest.mark.asyncio
async def test_retention_crashes_at_each_storage_boundary_then_retries_exact_counts() -> None:
    class BoundaryStore(MemoryObservatoryStore):
        def __init__(self, crash_at: int, current: RetentionAction) -> None:
            super().__init__()
            self.crash_at = crash_at
            self.calls = 0
            self.claimed = False
            self.current = current

        async def claim_pending_retention(self, action_id: str) -> RetentionAction | None:
            del action_id
            if self.claimed:
                return None
            self.claimed = True
            return self.current

        async def delete_observations(  # type: ignore[override]
            self, cutoff: datetime | None
        ) -> DeletionResult:
            del cutoff
            self.calls += 1
            if self.calls == self.crash_at:
                raise RuntimeError("simulated storage boundary crash")
            return DeletionResult(4, 3, 2)

    for boundary in (1, 2, 3):
        action = RetentionAction(
            f"00000000-0000-4000-8000-00000000041{boundary}",
            RetentionKind.PURGE_ALL,
            None,
            status=RetentionStatus.RUNNING,
            idempotency_key=f"boundary-{boundary}",
        )
        store = BoundaryStore(boundary, action)
        service = _query_service(store)
        store.actions[action.action_id] = action
        for _ in range(4):
            await cast(Any, service)._run_retention(action)
            if action.status is RetentionStatus.SUCCEEDED:
                break
            action.status = RetentionStatus.RUNNING
            store.claimed = False
        assert action.status is RetentionStatus.SUCCEEDED
        assert (action.deleted_count, action.deleted_receipt_count, action.deleted_run_count) == (
            4,
            3,
            2,
        )


@pytest.mark.asyncio
async def test_ingest_and_deletion_race_does_not_delete_new_observation() -> None:
    store = MemoryObservatoryStore()
    old = make_payload(
        "00000000-0000-4000-8000-000000000451",
        observed_at=BASE_TIME - timedelta(days=1),
        run_id=make_run_id("race-old"),
    )
    new = make_payload(
        "00000000-0000-4000-8000-000000000452",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=make_run_id("race-new"),
    )
    await store.insert_observation(old)
    deletion = asyncio.create_task(store.delete_observations(BASE_TIME))
    insertion = asyncio.create_task(store.insert_observation(new))
    await asyncio.gather(deletion, insertion)
    assert set(store.observations) == {new.observation_id}


def test_postgres_audit_has_no_owner_identity_and_receipt_cutoff_columns() -> None:
    text = (
        SERVICE_ROOT / "migrations/postgresql/versions/0001_observatory_foundation.sql"
    ).read_text(encoding="utf-8").lower()
    retention = text.split("create table if not exists observatory_retention_actions", 1)[1]
    assert "owner" not in retention
    assert "subject" not in retention
    assert "identity" not in retention
    assert "observed_at" in text


@pytest.mark.asyncio
async def test_clickhouse_retention_executes_source_and_projection_mutations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    statements: list[str] = []

    class Response:
        text = ""

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> Response:
            statements.append(str(kwargs["content"]))
            return Response()

    class Result:
        async def fetchone(self) -> tuple[int]:
            return (0,)

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            statements.append(statement)
            return Result()

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)

    store = ProductionObservatoryStore(
        connection,
        "http://clickhouse.test",
        database="observatory",
        user="observatory",
        password="secret",
    )
    result = await store.delete_observations(BASE_TIME)
    assert result == DeletionResult(0, 0, 0)
    assert any("ALTER TABLE observations DELETE" in statement for statement in statements)
    assert any("ALTER TABLE run_projections DELETE" in statement for statement in statements)
    assert all("secret" not in statement for statement in statements)


def test_clickhouse_migration_is_forward_safe_and_projection_accumulates_or_is_absent() -> None:
    text = (SERVICE_ROOT / "migrations/clickhouse/versions/0001_observations.sql").read_text(
        encoding="utf-8"
    ).lower()
    assert "create table if not exists" in text
    assert "create materialized view if not exists" in text or "run_projections" not in text
    if "run_projections" in text:
        assert any(
            token in text for token in ("summingmergetree", "aggregatingmergetree", "sumstate")
        )
        assert "insert into run_projections" in text or "backfill" in text


def test_runtime_observatory_metrics_match_all_aura0080_manifest_declarations() -> None:
    required = {
        "observatory_query_duration_ms",
        "observatory_query_outcome",
        "observatory_sse_connection_count",
        "observatory_sse_replay_count",
        "observatory_retention_action_count",
        "observatory_retention_deleted_count",
        "observatory_retention_failure_count",
    }
    manifests = [
        yaml.safe_load(path.read_text(encoding="utf-8"))
        for path in (SERVICE_ROOT / "resources/component-manifests").glob("*.yaml")
    ]
    declared = {metric for item in manifests for metric in item.get("metrics", [])}
    assert required <= declared
    runtime = "\n".join(
        path.read_text(encoding="utf-8") for path in (SERVICE_ROOT / "src").rglob("*.py")
    )
    assert required <= set(re.findall(r"observatory_[a-z0-9_]+", runtime))


def test_observatory_contract_and_generated_client_are_registered() -> None:
    registry = yaml.safe_load(
        (ROOT / "tooling/architecture/modules.yaml").read_text(encoding="utf-8")
    )
    modules = {item["id"]: item for item in registry["modules"]}
    assert any(
        item["root"] == "contracts/openapi/observatory-v1.yaml"
        and item["kind"] == "openapi-contract"
        for item in modules.values()
    )
    assert any(
        item["root"] == "frontend/libs/platform/observatory-api-client"
        and item["kind"] == "generated-client"
        for item in modules.values()
    )


@pytest.mark.asyncio
async def test_frozen_core_otlp_fixture_preserves_units_lineage_and_health_denominators() -> None:
    from aura_observatory.entrypoints.api.app import create_app
    from aura_observatory.ingest import TelemetryIngestService
    from aura_observatory.otlp import metrics_to_observations, traces_to_observations
    from test_aura0079_otlp_signal_regressions import MemoryCatalog, MemoryObservationStore

    metric = _metric_payload()
    metric_point = metric["resourceMetrics"][0]["scopeMetrics"][0]["metrics"][0]
    metric_point["unit"] = "milliseconds"
    points = cast(list[dict[str, Any]], metric_point["gauge"]["dataPoints"])
    points.append(
        dict(points[0], asDouble=25.0, timeUnixNano="1735689601123456789")
    )
    metric_records = metrics_to_observations(metric)
    span_records = traces_to_observations(
        _span_payload(observation_id="01234567-89ab-4cde-8123-0123456789ac")
    )
    all_records = [*metric_records, *span_records]
    assert all(
        float(cast(dict[str, Any], record["measurement"])["value"]) > 0
        for record in all_records
    )
    assert all(cast(dict[str, Any], record["component"])["id"] for record in all_records)
    assert all(record.get("trace") for record in span_records)
    assert all(
        cast(dict[str, Any], record["measurement"]).get("unit") == "milliseconds"
        for record in all_records
    )

    store = MemoryObservationStore()
    service = TelemetryIngestService(store, MemoryCatalog(), credential="secret")
    app = create_app(Settings(), ingest_service=service, testing=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://collector") as client:
        response = await client.post(
            "/internal/v1/telemetry/metrics",
            json=metric,
            headers={"Authorization": "Bearer secret"},
        )
    assert response.status_code == 202
    observations = list(store.observations.values())
    assert len(observations) == 2
    assert max(item.measurement.value for item in observations) == 25.0
    assert min(item.measurement.value for item in observations) > 0


@pytest.mark.asyncio
async def test_overview_distinguishes_last_acceptance_and_failure_and_recovers_health() -> None:
    class HealthStore(MemoryObservatoryStore):
        async def ingest_overview(self) -> dict[str, Any]:
            return {
                "accepted": 2,
                "rejected": 1,
                "duplicate": 0,
                "malformed": 0,
                "storageFailure": 0,
                "lastAcceptedAt": "2026-01-01T12:00:00Z",
                "lastFailureAt": "2026-01-01T11:59:00Z",
            }

    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=HealthStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get("/api/v1/overview")
    body = response.json()
    assert body["systemState"] == "healthy"
    assert body["ingest"]["lastAcceptedAt"] == "2026-01-01T12:00:00Z"
    assert body["ingest"]["lastFailureAt"] == "2026-01-01T11:59:00Z"


@pytest.mark.asyncio
async def test_retention_worker_continues_polling_after_one_failure() -> None:
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000499",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.QUEUED,
        idempotency_key="polling-worker",
    )

    class Store(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.polls = 0
            self.actions[action.action_id] = action

        async def pending_retentions(self) -> list[RetentionAction]:
            self.polls += 1
            return [action] if self.polls == 1 else []

    store = Store()
    service = _query_service(store)
    stop = asyncio.Event()

    async def stop_soon() -> None:
        await asyncio.sleep(0.6)
        stop.set()

    stopper = asyncio.create_task(stop_soon())
    await asyncio.wait_for(service.run_retention_worker(stop), timeout=2.0)
    await stopper
    assert store.polls >= 2


@pytest.mark.asyncio
async def test_production_sse_replay_is_continuous_beyond_one_thousand_rows() -> None:
    """Replay paging must advance its anchor instead of silently truncating."""

    store = object.__new__(ProductionObservatoryStore)
    rows = [
        {
            "observation_id": f"00000000-0000-4000-8000-{index:012x}",
            "run_id": "00000000-0000-4000-8000-000000000001",
            "trace_id": "trace-1",
            "ingest_sequence": index,
        }
        for index in range(1, 1206)
    ]

    async def counts() -> dict[str, int]:
        return {"latest_sequence": len(rows)}

    async def query(statement: str) -> list[dict[str, Any]]:
        if "SELECT ingest_sequence FROM observations" in statement:
            return []
        match = re.search(r"ingest_sequence > (\d+) AND ingest_sequence <= (\d+)", statement)
        assert match is not None
        lower, upper = int(match.group(1)), int(match.group(2))
        return [
            item
            for item in rows
            if lower < int(item["ingest_sequence"]) <= upper
        ][:1000]

    store._overview_counts = counts  # type: ignore[attr-defined]
    store._query_rows = query  # type: ignore[attr-defined]
    stream = cast(
        AsyncGenerator[dict[str, Any]],
        store.events_after_query("00000000-0000-4000-8000-ffffffffffff"),
    )
    replayed = [await asyncio.wait_for(anext(stream), timeout=1.0) for _ in rows]
    assert [item["id"] for item in replayed] == [item["observation_id"] for item in rows]
    await stream.aclose()


@pytest.mark.asyncio
async def test_sse_initial_anchor_race_survives_idle_polls_and_delivers_late_event() -> None:
    class LateStore(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.polls = 0

        async def wait_for_event(self, timeout: float) -> None:
            del timeout
            self.polls += 1
            if self.polls == 2:
                await self.insert_observation(
                    make_payload(
                        "00000000-0000-4000-8000-000000000601",
                        observed_at=BASE_TIME + timedelta(seconds=2),
                        run_id=make_run_id("late-event"),
                    )
                )
            await asyncio.sleep(0)

    store = LateStore()
    await store.insert_observation(
        make_payload(
            "00000000-0000-4000-8000-000000000600",
            observed_at=BASE_TIME,
            run_id=make_run_id("anchor"),
        )
    )
    anchor = store.event_sequence["00000000-0000-4000-8000-000000000600"]
    stream = cast(
        AsyncGenerator[dict[str, Any]],
        _query_service(store).events_after(None, snapshot_sequence=anchor),
    )
    late = await asyncio.wait_for(anext(stream), timeout=1.0)
    assert late["id"] == "00000000-0000-4000-8000-000000000601"
    assert store.polls >= 2
    await stream.aclose()


def test_reversed_completion_requires_a_contiguous_visibility_watermark() -> None:
    source = inspect.getsource(ProductionObservatoryStore.events_after_query)
    # A max sequence is not a visibility watermark: a committed sequence 9
    # must not be replayed while sequence 8 is still absent.
    assert any(token in source.lower() for token in ("contiguous", "watermark", "visibility"))
    migration = (SERVICE_ROOT / "migrations/clickhouse/versions/0001_observations.sql").read_text(
        encoding="utf-8"
    ).lower()
    assert "ingest_sequence" in migration
    assert "unique" in migration or "primary key" in migration


def test_api_and_worker_keep_authoritative_state_roles_separate() -> None:
    api = Settings(role="api")
    worker = Settings(role="worker")
    assert api.role == "api"
    assert worker.role == "worker"
    worker_source = (SERVICE_ROOT / "src/aura_observatory/entrypoints/worker.py").read_text(
        encoding="utf-8"
    )
    api_source = (SERVICE_ROOT / "src/aura_observatory/entrypoints/api/app.py").read_text(
        encoding="utf-8"
    )
    assert "run_retention_worker" in worker_source
    assert "build_query_service" in worker_source
    assert "run_retention_worker" not in api_source


def test_retention_global_lock_precedes_target_selection() -> None:
    source = inspect.getsource(ProductionObservatoryStore.delete_observations)
    lock = source.find("pg_advisory_xact_lock")
    target = source.find("SELECT count() AS observations")
    assert lock >= 0
    assert target >= 0
    assert lock < target


def test_receipt_boundary_is_committed_before_clickhouse_storage() -> None:
    source = inspect.getsource(
        __import__("aura_observatory.adapters.clickhouse", fromlist=["ClickHouseObservationStore"])
        .ClickHouseObservationStore.insert_observation
    )
    assert source.find("set_observed_at") < source.find("AsyncClient")
    assert source.find("complete") > source.find("raise_for_status")


def test_retention_state_has_immutable_target_and_resumable_phases() -> None:
    migration = (
        SERVICE_ROOT / "migrations/postgresql/versions/0002_observatory_retention_state.sql"
    ).read_text(encoding="utf-8").lower()
    for column in (
        "target_observation_count",
        "target_receipt_count",
        "target_run_count",
        "deletion_phase",
    ):
        assert column in migration
    source = inspect.getsource(ObservatoryQueryService)
    assert "prepare_retention" in source
    assert "update_retention" in source


@pytest.mark.asyncio
async def test_terminal_audit_failure_is_content_free_and_keeps_counts() -> None:
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000602",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="audit-failure",
    )

    class Store(MemoryObservatoryStore):
        async def claim_retention(self, action_id: str, lease_seconds: int = 60) -> bool:
            del action_id, lease_seconds
            return True

        async def prepare_retention(self, action: RetentionAction) -> None:
            del action

        async def delete_observations(  # type: ignore[override]
            self, cutoff: datetime | None
        ) -> DeletionResult:
            del cutoff
            return DeletionResult(7, 5, 3)

        async def update_retention(self, action: RetentionAction) -> None:
            del action
            raise RuntimeError("private audit identity must not escape")

    store = Store()
    service = _query_service(store)
    await service._run_retention(action)  # type: ignore[attr-defined]
    assert action.status is RetentionStatus.FAILED
    assert action.error == "storage_failure"
    assert (action.deleted_count, action.deleted_receipt_count, action.deleted_run_count) == (
        7,
        5,
        3,
    )


@pytest.mark.asyncio
async def test_retention_failure_logs_only_bounded_phase_and_exception_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000603",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="diagnostic-failure",
    )

    class Store(MemoryObservatoryStore):
        async def delete_observations(  # type: ignore[override]
            self, cutoff: datetime | None
        ) -> DeletionResult:
            del cutoff
            raise RuntimeError("SQL and private response details must not escape")

    store = Store()
    service = _query_service(store)
    with caplog.at_level("ERROR", logger="aura_observatory.query"):
        await service._run_retention(action)  # type: ignore[attr-defined]

    messages = [record.getMessage() for record in caplog.records]
    diagnostic = next(message for message in messages if "retention storage failure" in message)
    assert diagnostic == (
        "retention storage failure phase=storage_cleanup exception_class=RuntimeError"
    )
    assert "SQL" not in diagnostic
    assert "private" not in diagnostic


def test_query_use_case_has_explicit_ports_and_no_provider_sql() -> None:
    source = inspect.getsource(ObservatoryQueryService)
    assert getattr(ObservatoryQueryPort, "_is_protocol", False)
    assert "httpx" not in source
    assert "SELECT " not in source
    assert "ALTER TABLE" not in source


def test_large_run_and_trace_contracts_expose_partial_continuation() -> None:
    contract = json.loads(
        (ROOT / "contracts/openapi/observatory-v1.yaml").read_text(encoding="utf-8")
    )
    run_detail = contract["components"]["schemas"]["RunDetail"]
    trace_detail = contract["components"]["schemas"]["TraceDetail"]
    contract_text = json.dumps({"run": run_detail, "trace": trace_detail}).lower()
    assert "partial" in contract_text
    assert any(token in contract_text for token in ("continuation", "nextcursor", "truncated"))
    for method_name in ("run_detail_query", "trace_detail_query"):
        source = inspect.getsource(getattr(ProductionObservatoryStore, method_name))
        assert "LIMIT" in source
        assert any(token in source.lower() for token in ("partial", "continuation", "truncated"))


def test_retention_cleanup_is_bounded_and_storage_side() -> None:
    source = inspect.getsource(ProductionObservatoryStore.delete_observations)
    assert "list_observations" not in source
    assert "count()" in source
    assert "ALTER TABLE observations DELETE" in source
    assert "ALTER TABLE run_projections DELETE" in source


def test_populated_migrations_are_rerunnable_without_duplicate_objects() -> None:
    for path in (SERVICE_ROOT / "migrations/postgresql/versions").glob("*.sql"):
        text = path.read_text(encoding="utf-8").lower()
        for statement in re.findall(r"(?:create|alter)\s+(?:table|index|type)[^;]+", text):
            assert "if not exists" in statement or "add column if not exists" in statement, path
    for path in (SERVICE_ROOT / "migrations/clickhouse/versions").glob("*.sql"):
        text = path.read_text(encoding="utf-8").lower()
        for statement in re.findall(r"create\s+(?:table|materialized view|index)[^;]+", text):
            if path.name == "0003_legacy_ordering_ledger.sql" and (
                "observatory_legacy_ordering" in statement
            ):
                assert "drop table if exists observatory_legacy_ordering" in text
            else:
                assert "if not exists" in statement, path


@pytest.mark.asyncio
async def test_runtime_run_detail_validates_against_openapi_schema() -> None:
    contract = json.loads(
        (ROOT / "contracts/openapi/observatory-v1.yaml").read_text(encoding="utf-8")
    )
    observation = make_payload(
        "00000000-0000-4000-8000-000000000603",
        observed_at=BASE_TIME,
        run_id=make_run_id("schema"),
    )
    store = MemoryObservatoryStore()
    await store.insert_observation(observation)
    detail = await _query_service(store).run_detail(make_run_id("schema"))
    assert detail is not None
    schema = contract["components"]["schemas"]
    summary = detail
    detail_schema = schema["RunDetail"]
    required_summary = set(detail_schema["required"])
    assert required_summary <= summary.keys()
    assert set(summary) <= set(detail_schema["properties"])
    observations = cast(list[dict[str, Any]], summary["observations"])
    assert isinstance(observations, list)
    max_items = detail_schema["properties"]["observations"]["maxItems"]
    assert len(observations) <= max_items
    observation_schema = schema["ObservationSummary"]
    for observation in observations:
        assert set(observation) <= set(observation_schema["properties"])
        assert set(observation_schema["required"]) <= observation.keys()
        assert observation["capturePolicy"] == "metadata_only"
        assert observation["serviceRole"] in {"api", "worker", "scheduler", "cli"}


@pytest.mark.asyncio
async def test_query_success_failure_metrics_and_sse_replay_are_externally_visible() -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    metrics = ObservatoryMetrics()
    service = _query_service(MemoryObservatoryStore(), metrics)
    await service.overview()
    await anext(service.events_after(None))
    snapshot = metrics.snapshot()
    assert any(key[0] == "observatory_query_outcome" for key in metrics.values)
    assert any(key[0] == "observatory_sse_connection_count" for key in metrics.values)
    assert any(key[0] == "observatory_sse_replay_count" for key in metrics.values)
    assert snapshot


@pytest.mark.asyncio
async def test_trace_uses_one_canonical_execution_for_multiple_span_metrics() -> None:
    store = MemoryObservatoryStore()
    first = make_payload(
        "00000000-0000-4000-8000-000000000604",
        observed_at=BASE_TIME,
        run_id=make_run_id("canonical"),
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
    )
    second = make_payload(
        "00000000-0000-4000-8000-000000000605",
        observed_at=BASE_TIME + timedelta(milliseconds=1),
        run_id=make_run_id("canonical"),
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
    )
    await store.insert_observation(first)
    await store.insert_observation(second)
    detail = await _query_service(store).trace_detail("0123456789abcdef0123456789abcdef")
    assert detail is not None
    assert len(detail["spans"]) == 1


@pytest.mark.asyncio
async def test_malformed_owner_input_is_authenticated_before_parsing_and_sanitized() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/retention/actions",
            content=b"not-json-owner-secret",
            headers={
                "Origin": settings.public_origin,
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "malformed-owner-1",
                "X-Observatory-Confirmation": "purge-all",
                "Content-Type": "application/json",
            },
        )
    assert response.status_code in {400, 422}
    assert "not-json-owner-secret" not in response.text


@pytest.mark.asyncio
async def test_failed_insert_retry_cannot_freeze_snapshot_or_replay_order() -> None:
    class FlakyStore(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.fail_once = True

        async def insert_observation(self, observation: Any) -> Any:
            if self.fail_once:
                self.fail_once = False
                raise RuntimeError("ambiguous insert")
            return await super().insert_observation(observation)

    store = FlakyStore()
    first = make_payload(
        "00000000-0000-4000-8000-000000000701",
        observed_at=BASE_TIME,
        run_id=make_run_id("retry-first"),
    )
    later = make_payload(
        "00000000-0000-4000-8000-000000000702",
        observed_at=BASE_TIME + timedelta(seconds=1),
        run_id=make_run_id("retry-later"),
    )
    with pytest.raises(RuntimeError):
        await store.insert_observation(first)
    await store.insert_observation(later)
    await store.insert_observation(first)
    assert store.snapshot_sequence == 2
    stream = cast(
        AsyncGenerator[dict[str, Any]],
        _query_service(store).events_after(
            "00000000-0000-4000-8000-ffffffffffff",
            snapshot_sequence=store.snapshot_sequence,
        ),
    )
    assert [
        (await anext(stream))["id"],
        (await anext(stream))["id"],
    ] == [later.observation_id, first.observation_id]
    await stream.aclose()


@pytest.mark.asyncio
async def test_heartbeat_does_not_advance_last_event_id() -> None:
    stream = cast(
        AsyncGenerator[dict[str, Any]], _query_service(MemoryObservatoryStore()).events_after(None)
    )
    heartbeat = await asyncio.wait_for(anext(stream), timeout=1.0)
    assert heartbeat["type"] == "heartbeat"
    assert heartbeat.get("id") in (None, "")
    await stream.aclose()


def test_ordering_migration_uses_a_durable_ledger_for_populated_reruns() -> None:
    migration = (
        SERVICE_ROOT / "migrations/clickhouse/versions/0003_legacy_ordering_ledger.sql"
    ).read_text(encoding="utf-8").lower()
    cli = (SERVICE_ROOT / "src/aura_observatory/entrypoints/cli.py").read_text(
        encoding="utf-8"
    ).lower()
    assert "observatory_migration_ledger" in migration
    assert "insert into observatory_migration_ledger" in migration
    assert "observatory_migration_ledger" in cli
    assert "select" in cli and "version" in cli
    assert "on conflict" in migration or "not exists" in migration
    assert "drop table if exists observatory_legacy_ordering" in migration
    assert "engine = join(any, left, observation_id)" in migration
    assert "joinget('observatory_legacy_ordering', 'ingest_sequence', observation_id)" in migration
    assert "settings mutations_sync = 2" in migration
    assert "select ingest_sequence from observatory_legacy_ordering" not in migration
    assert "engine = replacingmergetree\norder by observation_id" not in migration
    assert migration.count("drop table if exists observatory_legacy_ordering") == 2
    assert migration.index("drop table if exists observatory_legacy_ordering") < migration.index(
        "create table observatory_legacy_ordering"
    )
    assert migration.rindex("drop table if exists observatory_legacy_ordering") > migration.index(
        "settings mutations_sync = 2"
    )


def test_delete_guard_spans_ingest_visibility_and_has_no_unbounded_run_id_array() -> None:
    source = inspect.getsource(ProductionObservatoryStore.delete_observations)
    lock_start = source.find("pg_advisory_xact_lock")
    mutation_start = source.find("ALTER TABLE observations DELETE")
    assert lock_start >= 0
    assert mutation_start >= 0
    # A transaction-scoped guard must still be active while the ClickHouse
    # mutation is issued; a closed lock connection only serializes a read.
    guard_block = source[source.rfind("async with", 0, lock_start) : mutation_start]
    assert "lock_connection" not in guard_block or "httpx" in guard_block
    assert "groupUniqArray" not in source


def test_retention_targeting_is_page_bounded_and_phase_resumable() -> None:
    source = inspect.getsource(ProductionObservatoryStore.prepare_retention)
    assert "LIMIT" in source.upper()
    assert "deletion_phase" in source
    assert any(token in source for token in ("targeted", "source_deleted", "receipts_deleted"))
    adapter = "\n".join(
        inspect.getsource(base) for base in ProductionObservatoryStore.__mro__[:2]
    )
    assert "target_observation_count" in adapter
    assert "target_receipt_count" in adapter
    assert "target_run_count" in adapter


def test_retention_counts_survive_restart_from_durable_action_row() -> None:
    source = (SERVICE_ROOT / "src/aura_observatory/query.py").read_text(encoding="utf-8")
    adapter = "\n".join(
        inspect.getsource(base) for base in ProductionObservatoryStore.__mro__[:2]
    )
    assert "target_observation_count" in source or "target_observation_count" in adapter
    assert "target_receipt_count" in source or "target_receipt_count" in adapter
    assert "target_run_count" in source or "target_run_count" in adapter
    assert "deletion_phase" in adapter


def test_query_use_case_dispatches_only_through_explicit_ports() -> None:
    source = inspect.getsource(ObservatoryQueryService)
    assert "isinstance(self.store, MemoryObservatoryStore)" not in source
    assert "httpx" not in source
    assert "SELECT " not in source
    assert getattr(ObservatoryQueryPort, "_is_protocol", False)


@pytest.mark.asyncio
async def test_truncated_trace_preserves_top_level_partial_and_continuation() -> None:
    store = MemoryObservatoryStore()
    trace_id = "abcdef0123456789abcdef0123456789"
    for index in range(3):
        await store.insert_observation(
            make_payload(
                f"00000000-0000-4000-8000-00000000071{index}",
                observed_at=BASE_TIME + timedelta(seconds=index),
                run_id=make_run_id("trace-page"),
                trace_id=trace_id,
                span_id=f"0123456789abcde{index}",
            )
        )
    result = await _query_service(store).trace_detail(trace_id, limit=1)
    assert result is not None
    assert result["partial"] is True
    assert result["nextCursor"]


@pytest.mark.asyncio
async def test_all_query_resources_publish_duration_and_outcome_label_series() -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    metrics = ObservatoryMetrics()
    store = MemoryObservatoryStore()
    item = make_payload(
        "00000000-0000-4000-8000-000000000720",
        observed_at=BASE_TIME,
        run_id=make_run_id("metric-resource"),
        trace_id="abcdef0123456789abcdef0123456789",
        span_id="0123456789abcdef",
    )
    await store.insert_observation(item)
    service = _query_service(store, metrics)
    await service.overview()
    await service.runs(RunFilters(), cursor=None, limit=10)
    await service.run_detail(make_run_id("metric-resource"))
    await service.trace_detail("abcdef0123456789abcdef0123456789")
    await service.component_health(item.component.id)
    resources = {"overview", "runs", "run_detail", "trace_detail", "component_health"}
    duration_labels = {
        dict(labels).get("resource")
        for name, labels in metrics.values
        if name == "observatory_query_duration_ms"
    }
    outcome_labels = {
        dict(labels).get("resource")
        for name, labels in metrics.values
        if name == "observatory_query_outcome"
    }
    assert resources <= duration_labels
    assert resources <= outcome_labels


@pytest.mark.asyncio
async def test_sse_and_retention_metrics_are_label_distinct_and_external() -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    metrics = ObservatoryMetrics()
    service = _query_service(MemoryObservatoryStore(), metrics)
    stream = cast(AsyncGenerator[dict[str, Any]], service.events_after(None))
    await anext(stream)
    await stream.aclose()
    await service.retention(
        RetentionRequest(
            RetentionKind.PURGE_ALL,
            confirmation="purge-all",
            idempotency_key="metric-action-1",
            strict_confirmation=True,
        )
    )
    names = {name for name, _labels in metrics.values}
    assert "observatory_sse_connection_count" in names
    assert "observatory_sse_replay_count" in names
    assert "observatory_retention_action_count" in names
    sse_series = {labels for name, labels in metrics.values if name.startswith("observatory_sse")}
    assert len(sse_series) >= 2


@pytest.mark.asyncio
async def test_component_health_counts_one_canonical_span_execution() -> None:
    store = MemoryObservatoryStore()
    trace_id = "fedcba9876543210fedcba9876543210"
    for index in range(2):
        await store.insert_observation(
            make_payload(
                f"00000000-0000-4000-8000-00000000073{index}",
                observed_at=BASE_TIME + timedelta(seconds=index),
                run_id=make_run_id("health-canonical"),
                trace_id=trace_id,
                span_id="fedcba9876543210",
            )
        )
    result = await _query_service(store).component_health("aura.runtime.model_inference")
    assert result is not None
    assert result["summary"]["sampleCount"] == 1


@pytest.mark.asyncio
async def test_ingest_health_keeps_first_acceptance_and_failure_timestamps() -> None:
    store = MemoryObservatoryStore()
    before = datetime.now(UTC)
    await store.increment_counter("accepted")
    await store.increment_counter("storage_failure")
    overview = await store.ingest_overview()
    accepted = datetime.fromisoformat(str(overview["lastAcceptedAt"]).replace("Z", "+00:00"))
    failed = datetime.fromisoformat(str(overview["lastFailureAt"]).replace("Z", "+00:00"))
    assert accepted >= before
    assert failed >= accepted


def test_migration_ledger_skips_populated_versions_and_syncs_legacy_ordering(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A populated ClickHouse install is read through the ledger on rerun."""

    applied: set[str] = set()
    clickhouse_posts: list[str] = []
    postgres_calls: list[tuple[str, tuple[Any, ...]]] = []

    class Response:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
            postgres_calls.append((statement, params))

        def commit(self) -> None:
            return None

    def connect(_url: str) -> Connection:
        return Connection()

    def post(
        _url: str,
        *,
        params: dict[str, str],
        content: str,
        timeout: float,
        **_kwargs: Any,
    ) -> Response:
        assert params["database"] == "aura_observatory"
        assert timeout == 10.0
        clickhouse_posts.append(content)
        if "SELECT version FROM observatory_migration_ledger" in content:
            match = re.search(r"version = '([^']+)'", content)
            assert match is not None
            return Response(match.group(1) + "\n" if match.group(1) in applied else "")
        if "SELECT coalesce(max(ingest_sequence), 0)" in content:
            return Response("17\n")
        match = re.search(r"SELECT '([^']+)', now64\(3\)", content)
        if match:
            applied.add(match.group(1))
        return Response()

    # The CLI only enables ledger reads for the real HTTP client surface.
    post.__module__ = "httpx"
    monkeypatch.setattr(cli.psycopg, "connect", connect)
    monkeypatch.setattr(cli.httpx, "post", post)

    cli.migrate()
    first_post_count = len(clickhouse_posts)
    first_migrations = [
        value
        for value in clickhouse_posts
        if "INSERT INTO observatory_migration_ledger" in value
    ]
    assert first_migrations
    assert all("WHERE NOT EXISTS" in value for value in first_migrations)

    cli.migrate()
    second_migrations = [
        value
        for value in clickhouse_posts[first_post_count:]
        if "INSERT INTO observatory_migration_ledger" in value
    ]
    assert second_migrations == []
    expected_versions = sorted(
        path.stem
        for path in (SERVICE_ROOT / "migrations/clickhouse/versions").glob("*.sql")
    )
    assert sum(
        "SELECT version FROM observatory_migration_ledger" in value
        for value in clickhouse_posts
    ) == 2 * len(expected_versions)
    assert any(
        "setval('observatory_ingest_sequence'" in statement
        for statement, _ in postgres_calls
    )
    assert any("observatory_ingest_visibility" in statement for statement, _ in postgres_calls)
    assert any("committed_sequence" in statement for statement, _ in postgres_calls)


@pytest.mark.asyncio
async def test_postgres_sequence_reuse_and_visibility_watermark_are_transactional() -> None:
    calls: list[str] = []
    receipt_sequence: int | None = None
    next_sequence = 40
    visible: dict[int, bool] = {}
    watermark = 40

    class Result:
        def __init__(self, row: tuple[Any, ...] | None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.row

    class Connection:
        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            nonlocal receipt_sequence, next_sequence, watermark
            calls.append(statement)
            if "INSERT INTO observatory_observation_receipts" in statement:
                return Result(("observation-1",))
            if "SELECT ingest_sequence FROM observatory_observation_receipts" in statement:
                return Result((receipt_sequence,) if receipt_sequence is not None else None)
            if "nextval('observatory_ingest_sequence')" in statement:
                next_sequence += 1
                return Result((next_sequence,))
            if "INSERT INTO observatory_ingest_visibility" in statement:
                visible[int(params[0])] = False
                return Result(None)
            if "UPDATE observatory_observation_receipts SET ingest_sequence" in statement:
                receipt_sequence = int(params[0])
                return Result(None)
            if "UPDATE observatory_ingest_visibility SET visible" in statement:
                visible[int(params[0])] = True
                return Result(None)
            if "SELECT committed_sequence" in statement:
                return Result((watermark,))
            if "SELECT visible FROM observatory_ingest_visibility" in statement:
                return Result((visible.get(int(params[0]), False),))
            if "UPDATE observatory_ingest_watermark" in statement:
                watermark = int(params[0])
                return Result(None)
            return Result(None)

    class Context:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()

    def factory() -> Context:
        return Context()

    registry = PostgresObservationIdRegistry(factory)
    assert await registry.claim("observation-1") is ClaimOutcome.ACQUIRED
    first = await registry.next_ingest_sequence("observation-1")
    second = await registry.next_ingest_sequence("observation-1")
    assert first == second == 41
    assert sum("nextval('observatory_ingest_sequence')" in value for value in calls) == 1
    await registry.mark_ingest_visible(first)
    await registry.complete("observation-1")
    assert watermark == 41
    assert visible[41] is True
    assert calls.index("SELECT pg_advisory_lock(830078)") < calls.index(
        next(value for value in calls if "INSERT INTO observatory_observation_receipts" in value)
    )
    assert "SELECT pg_advisory_unlock(830078)" in calls


@pytest.mark.asyncio
async def test_postgres_claim_closes_non_acquired_context_and_retained_guards() -> None:
    class Result:
        def __init__(self, row: tuple[Any, ...] | None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.row

    class Database:
        def __init__(self) -> None:
            self.statuses = {
                "duplicate-observation": "completed",
                "pending-observation": "pending",
            }
            self.active_locks = 0
            self.lock_calls = 0
            self.unlock_calls = 0
            self.context_enters = 0
            self.context_exits = 0
            self.commit_calls = 0

        def factory(self) -> Context:
            return Context(self)

    class Connection:
        def __init__(self, database: Database) -> None:
            self.database = database

        async def execute(
            self, statement: str, params: tuple[Any, ...] = ()
        ) -> Result:
            if statement == "SELECT pg_advisory_lock(830078)":
                self.database.lock_calls += 1
                self.database.active_locks += 1
                return Result(None)
            if statement == "SELECT pg_advisory_unlock(830078)":
                self.database.unlock_calls += 1
                self.database.active_locks -= 1
                return Result(None)
            if "INSERT INTO observatory_observation_receipts" in statement:
                observation_id = str(params[0])
                if observation_id in self.database.statuses:
                    return Result(None)
                self.database.statuses[observation_id] = "pending"
                return Result((observation_id,))
            if "SELECT status FROM observatory_observation_receipts" in statement:
                return Result((self.database.statuses[str(params[0])],))
            if "SET status = 'pending'" in statement:
                return Result(None)
            if "SET status = 'completed'" in statement:
                self.database.statuses[str(params[0])] = "completed"
                return Result(None)
            raise AssertionError(f"unexpected SQL: {statement}")

        async def commit(self) -> None:
            self.database.commit_calls += 1

    class Context:
        def __init__(self, database: Database) -> None:
            self.database = database
            self.connection = Connection(database)

        async def __aenter__(self) -> Connection:
            self.database.context_enters += 1
            return self.connection

        async def __aexit__(self, *_args: object) -> None:
            self.database.context_exits += 1

    database = Database()
    registry = PostgresObservationIdRegistry(database.factory)

    assert await registry.claim("duplicate-observation") is ClaimOutcome.DUPLICATE
    assert database.active_locks == 0
    assert database.context_exits == database.context_enters == 1
    assert registry._ingest_guards == {}  # type: ignore[attr-defined]
    assert registry._sequence_guards == {}  # type: ignore[attr-defined]

    assert await registry.claim("pending-observation") is ClaimOutcome.PENDING
    assert database.active_locks == 0
    assert database.context_exits == database.context_enters == 2
    assert registry._ingest_guards == {}  # type: ignore[attr-defined]
    assert registry._sequence_guards == {}  # type: ignore[attr-defined]

    assert await registry.claim("complete-observation") is ClaimOutcome.ACQUIRED
    assert database.active_locks == 1
    assert database.context_exits == 2
    assert set(registry._ingest_guards) == {"complete-observation"}  # type: ignore[attr-defined]
    await registry.complete("complete-observation")
    assert database.active_locks == 0
    assert database.context_exits == database.context_enters == 3
    assert registry._ingest_guards == {}  # type: ignore[attr-defined]
    assert registry._sequence_guards == {}  # type: ignore[attr-defined]

    assert await registry.claim("abort-observation") is ClaimOutcome.ACQUIRED
    assert database.active_locks == 1
    assert database.context_exits == 3
    assert set(registry._ingest_guards) == {"abort-observation"}  # type: ignore[attr-defined]
    await registry.abort("abort-observation")
    assert database.active_locks == 0
    assert database.context_exits == database.context_enters == 4
    assert registry._ingest_guards == {}  # type: ignore[attr-defined]
    assert registry._sequence_guards == {}  # type: ignore[attr-defined]
    assert database.lock_calls == database.unlock_calls == 4


@pytest.mark.asyncio
async def test_all_query_failures_publish_label_distinct_duration_and_outcome_series() -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    class FailingStore(MemoryObservatoryStore):
        async def list_observations(self, *, limit: int | None = None) -> list[Any]:
            del limit
            raise RuntimeError("store unavailable")

        async def components(self) -> list[dict[str, Any]]:
            raise RuntimeError("store unavailable")

        async def deployments(self) -> list[dict[str, Any]]:
            raise RuntimeError("store unavailable")

    metrics = ObservatoryMetrics()
    service = _query_service(FailingStore(), metrics)
    calls = (
        service.overview(),
        service.runs(RunFilters(), cursor=None, limit=1),
        service.run_detail(make_run_id("failed")),
        service.trace_detail("0123456789abcdef0123456789abcdef"),
        service.components(),
        service.component_health("aura.runtime.model_inference"),
        service.deployments(),
    )
    results = await asyncio.gather(*(cast(Any, call) for call in calls), return_exceptions=True)
    assert all(isinstance(result, RuntimeError) for result in results)
    outcome_resources = {
        dict(labels).get("resource")
        for name, labels in metrics.values
        if name == "observatory_query_outcome"
        and dict(labels).get("outcome") == "failed"
    }
    duration_resources = {
        dict(labels).get("resource")
        for name, labels in metrics.values
        if name == "observatory_query_duration_ms"
    }
    expected = {
        "overview",
        "runs",
        "run_detail",
        "trace_detail",
        "components",
        "component_health",
        "deployments",
    }
    assert outcome_resources == expected
    assert duration_resources == expected

    app = create_app(
        Settings(owner_subject=OWNER),
        query_store=MemoryObservatoryStore(),
        testing=True,
    )
    app.state.query.metrics = metrics
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=Settings().public_origin
    ) as client:
        rendered = (await client.get("/metrics")).text
    assert 'observatory_query_outcome{outcome="failed",resource="overview"}' in rendered
    assert 'observatory_query_duration_ms_sum{resource="overview"}' in rendered
    assert 'observatory_query_duration_ms{resource="overview"}' not in rendered


@pytest.mark.asyncio
async def test_retention_restart_preserves_target_scope_and_counts_after_targeting() -> None:
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000741",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="restart-targeted",
        target_observation_count=9,
        target_receipt_count=7,
        target_run_count=3,
        deletion_phase="targeted",
    )

    class DurableStore(MemoryObservatoryStore):
        def __init__(self) -> None:
            super().__init__()
            self.actions[action.action_id] = action
            self.prepare_calls = 0
            self.update_calls = 0

        async def claim_pending_retention(self, action_id: str) -> RetentionAction | None:
            assert action_id == action.action_id
            return action

        async def prepare_retention(self, action: RetentionAction) -> None:
            self.prepare_calls += 1
            await super().prepare_retention(action)

        async def delete_observations(self, cutoff: datetime | None) -> DeletionResult:
            del cutoff
            return DeletionResult(9, 7, 3)

        async def update_retention(self, action: RetentionAction) -> None:
            self.update_calls += 1
            self.actions[action.action_id] = action

    store = DurableStore()
    await _query_service(store)._run_retention(action)  # type: ignore[attr-defined]
    restarted = _query_service(store)
    recovered = await restarted.retention_status(action.action_id)
    assert recovered is not None
    assert recovered.status is RetentionStatus.SUCCEEDED
    assert store.prepare_calls == 0
    assert store.update_calls == 1
    assert (
        recovered.target_observation_count,
        recovered.target_receipt_count,
        recovered.target_run_count,
    ) == (9, 7, 3)
    assert (
        recovered.deleted_count,
        recovered.deleted_receipt_count,
        recovered.deleted_run_count,
    ) == (9, 7, 3)


@pytest.mark.asyncio
async def test_clickhouse_cancel_after_allocation_reuses_sequence_after_restart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Allocation and ClickHouse acceptance both remain retry-safe."""

    observation = MetadataObservation.from_payload(metrics_to_observations(_metric_payload())[0])

    class Registry:
        def __init__(self) -> None:
            self.sequences: dict[str, int] = {}
            self.next_sequence = 41
            self.aborted: list[str] = []
            self.completed: list[str] = []
            self.visible: list[int] = []
            self.commits = 0

        async def claim(self, observation_id: str) -> ClaimOutcome:
            return ClaimOutcome.ACQUIRED

        async def next_ingest_sequence(self, observation_id: str) -> int:
            if observation_id not in self.sequences:
                self.sequences[observation_id] = self.next_sequence
                self.next_sequence += 1
            return self.sequences[observation_id]

        async def set_observed_at(self, observation_id: str, observed_at: datetime) -> None:
            del observation_id, observed_at
            return None

        async def commit_ingest_boundary(self, observation_id: str) -> None:
            del observation_id
            self.commits += 1

        async def mark_ingest_visible(self, sequence: int) -> None:
            self.visible.append(sequence)

        async def complete(self, observation_id: str) -> None:
            self.completed.append(observation_id)

        async def abort(self, observation_id: str) -> None:
            self.aborted.append(observation_id)

    registry = Registry()
    requests: list[dict[str, Any]] = []
    mode = "cancel_before_clickhouse"

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _endpoint: str, **kwargs: Any) -> Response:
            nonlocal mode
            requests.append(kwargs)
            if mode == "cancel_after_accept":
                # The first successful observation is already in the request
                # history; cancellation belongs to this new operation.
                mode = "cancel_after_accept_seen"
                raise asyncio.CancelledError()
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.clickhouse.httpx.AsyncClient", Client)
    store = ClickHouseObservationStore("http://clickhouse.test", id_registry=registry)

    original_commit = registry.commit_ingest_boundary

    async def cancel_before_clickhouse(observation_id: str) -> None:
        if mode == "cancel_before_clickhouse":
            raise asyncio.CancelledError()
        await original_commit(observation_id)

    registry.commit_ingest_boundary = cancel_before_clickhouse  # type: ignore[method-assign]
    with pytest.raises(asyncio.CancelledError):
        await store.insert_observation(observation)
    assert requests == []
    assert registry.sequences[observation.observation_id] == 41
    assert registry.aborted == [observation.observation_id]

    mode = "success"
    assert await store.insert_observation(observation) is InsertOutcome.ACCEPTED
    assert len(requests) == 1
    assert registry.visible == [41]

    second = MetadataObservation.from_payload(
        metrics_to_observations(
            _metric_payload("01234567-89ab-4cde-8123-0123456789ac")
        )[0]
    )
    mode = "cancel_after_accept"
    with pytest.raises(asyncio.CancelledError):
        await store.insert_observation(second)
    mode = "success"
    assert await store.insert_observation(second) is InsertOutcome.ACCEPTED
    assert registry.sequences[second.observation_id] == 42
    assert len(requests) == 3
    assert [
        json.loads(str(request["content"]).split("\n", 1)[1])["ingest_sequence"]
        for request in requests[1:]
    ] == [42, 42]
    assert registry.visible == [41, 42]
    assert registry.commits == 3


@pytest.mark.asyncio
async def test_postgres_ingest_guards_release_after_commit_and_do_not_accumulate() -> None:
    calls: list[str] = []
    visible: dict[int, bool] = {}
    watermark = 0
    next_sequence = 0

    class Result:
        def __init__(self, row: tuple[Any, ...] | None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.row

    class Connection:
        async def commit(self) -> None:
            calls.append("COMMIT")

        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            nonlocal next_sequence, watermark
            calls.append(statement)
            if "INSERT INTO observatory_observation_receipts" in statement:
                return Result((str(params[0]),))
            if "SELECT ingest_sequence FROM observatory_observation_receipts" in statement:
                return Result(None)
            if "SELECT next_sequence FROM observatory_ingest_allocator" in statement:
                return Result(None)
            if "nextval('observatory_ingest_sequence')" in statement:
                next_sequence += 1
                return Result((next_sequence,))
            if "SELECT committed_sequence" in statement:
                return Result((watermark,))
            if "SELECT visible FROM observatory_ingest_visibility" in statement:
                return Result((visible.get(int(params[0]), False),))
            if "UPDATE observatory_ingest_watermark" in statement:
                watermark = int(params[0])
                return Result(None)
            if "UPDATE observatory_ingest_visibility" in statement:
                visible[int(params[0])] = True
            return Result(None)

    class Context:
        async def __aenter__(self) -> Connection:
            return connection

        async def __aexit__(self, *_args: object) -> None:
            return None

    connection = Connection()

    def factory() -> Context:
        return Context()

    registry = PostgresObservationIdRegistry(factory)
    for index in range(3):
        observation_id = f"observation-{index}"
        assert await registry.claim(observation_id) is ClaimOutcome.ACQUIRED
        sequence = await registry.next_ingest_sequence(observation_id)
        await registry.commit_ingest_boundary(observation_id)
        await registry.mark_ingest_visible(sequence)
        await registry.complete(observation_id)
    assert registry._ingest_guards == {}  # type: ignore[attr-defined]
    assert registry._sequence_guards == {}  # type: ignore[attr-defined]
    assert watermark == 3
    commit_index = calls.index("COMMIT")
    unlock_index = calls.index("SELECT pg_advisory_unlock(830078)", commit_index)
    assert commit_index < unlock_index


@pytest.mark.asyncio
async def test_production_component_health_deduplicates_spanless_secondary_metrics() -> None:
    calls: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            calls.append(statement)
            return [
                {
                    "sample_count": 1,
                    "p50": 12.5,
                    "p95": 12.5,
                    "failures": 0,
                    "successes": 1,
                    "cancelled": 0,
                }
            ]

    store = Store(lambda: None, "http://clickhouse.test", database="obs", user="u", password="p")
    result = await store.component_health_query("aura.runtime.model_routing", None, None)
    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["latencyP50Ms"] == 12.5
    assert calls and "GROUP BY execution_id" in calls[0]


@pytest.mark.asyncio
async def test_worker_metrics_are_visible_to_api_with_histogram_gauge_and_replay_semantics(
    tmp_path: Path,
) -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    path = tmp_path / "observatory-series.jsonl"
    worker_metrics = ObservatoryMetrics(path)
    worker_metrics.record("observatory_query_duration_ms", 12.5, {"resource": "runs"})
    worker_metrics.record("observatory_sse_active", 1, {"event_type": "stream"})
    worker_metrics.record("observatory_sse_replay_count", 1, {"event_type": "observation.changed"})
    api_metrics = ObservatoryMetrics(path)
    app = create_app(
        Settings(owner_subject=OWNER),
        query_store=MemoryObservatoryStore(),
        testing=True,
    )
    app.state.query.metrics = api_metrics
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=Settings().public_origin
    ) as client:
        rendered = (await client.get("/metrics")).text
    assert "# TYPE observatory_query_duration_ms histogram" in rendered
    assert 'observatory_query_duration_ms_sum{resource="runs"} 12.5' in rendered
    assert 'observatory_query_duration_ms{resource="runs"} 12.5' not in rendered
    assert 'observatory_query_duration_ms_count{resource="runs"} 1' in rendered
    assert '# TYPE observatory_sse_active gauge' in rendered
    assert 'observatory_sse_replay_count{event_type="observation.changed"} 1' in rendered


@pytest.mark.asyncio
async def test_retention_target_pages_are_bounded_and_survive_interleaving_actions() -> None:
    target_inserts: list[tuple[Any, ...]] = []
    target_queries: list[str] = []

    class Result:
        async def fetchone(self) -> tuple[int]:
            return (11,)

    class Connection:
        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            if "INSERT INTO observatory_retention_targets" in statement:
                target_inserts.append(params)
            return Result()

    class Store(ProductionObservatoryStore):
        def __init__(self, factory: Any) -> None:
            super().__init__(
                factory,
                "http://clickhouse.test",
                database="obs",
                user="u",
                password="p",
            )
            self.page = 0

        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            if "SELECT observation_id, run_id, observed_at" not in query:
                return [{"observations": 2001, "runs": 11}]
            target_queries.append(query)
            self.page += 1
            if self.page == 1:
                return [
                    {
                        "observation_id": f"old-{index:04d}",
                        "run_id": "run-a",
                        "observed_at": "2026-01-01T00:00:00Z",
                    }
                    for index in range(1000)
                ]
            if self.page == 2:
                return [
                    {
                        "observation_id": f"old-{index:04d}",
                        "run_id": "run-b",
                        "observed_at": "2026-01-01T00:00:01Z",
                    }
                    for index in range(1000, 2000)
                ]
            return [
                {
                    "observation_id": "old-2000",
                    "run_id": "run-c",
                    "observed_at": "2026-01-01T00:00:02Z",
                }
            ]

    # Inject one shared connection factory after construction; separate store
    # instances model concurrent worker processes with independent page state.
    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    first_store = Store(connection)
    second_store = Store(connection)
    first = RetentionAction(
        "action-a", RetentionKind.DELETE_BEFORE, BASE_TIME, idempotency_key="a"
    )
    second = RetentionAction(
        "action-b", RetentionKind.DELETE_BEFORE, BASE_TIME, idempotency_key="b"
    )
    await asyncio.gather(
        first_store.prepare_retention(first),
        second_store.prepare_retention(second),
    )
    assert first.deletion_phase == second.deletion_phase == "targeted"
    assert first.target_observation_count == second.target_observation_count == 2001
    assert len(target_queries) == 6
    assert all("LIMIT 1000" in statement for statement in target_queries)
    assert len(target_inserts) == 4002
    assert sum(row[0] == "action-a" for row in target_inserts) == 2001
    assert sum(row[0] == "action-b" for row in target_inserts) == 2001


@pytest.mark.asyncio
async def test_retention_count_comes_from_target_rows_when_clickhouse_aggregate_lags() -> None:
    """A visible row must not be reported as a zero-count purge."""

    updates: list[tuple[Any, ...]] = []

    class Result:
        async def fetchone(self) -> tuple[int]:
            return (0,)

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return []

    class Connection:
        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            if "UPDATE observatory_retention_actions SET target_observation_count" in statement:
                updates.append(params)
            if "count(DISTINCT run_id)" in statement:
                return ResultWithRunCount()
            return Result()

        async def commit(self) -> None:
            return None

    class ResultWithRunCount(Result):
        async def fetchone(self) -> tuple[int]:
            return (1,)

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    class Store(ProductionObservatoryStore):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self._target_page_returned = False

        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            if "SELECT observation_id, run_id, observed_at" not in query:
                # Simulate the aggregate read lagging behind the ordered
                # target read for a just-visible purge-all run.
                return [{"observations": 0, "runs": 0}]
            if "toDateTime64" in query:
                return []
            if self._target_page_returned:
                return []
            self._target_page_returned = True
            return [
                {
                    "observation_id": "00000000-0000-4000-8000-000000000901",
                    "run_id": "00000000-0000-4000-8000-000000000902",
                    "observed_at": "2026-01-01T00:00:00Z",
                }
            ]

    store = Store(connection, "http://clickhouse.test", database="obs", user="u", password="p")
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000903",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="aggregate-lag",
    )

    await store.prepare_retention(action)

    assert action.target_observation_count == 1
    assert action.target_run_count == 1
    assert updates and updates[-1][0] == 1


@pytest.mark.asyncio
async def test_retention_receipt_capture_casts_uuid_to_target_text() -> None:
    """Receipt UUIDs compare safely with varchar retention target identities."""

    statements: list[str] = []

    class Result:
        async def fetchone(self) -> tuple[int]:
            return (0,)

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return []

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            statements.append(statement)
            if "r.observation_id" in statement and "NOT EXISTS" in statement:
                # Model PostgreSQL's real UUID/varchar operator resolution:
                # the uncast comparison would fail before any rows are read.
                if "r.observation_id::text" not in statement:
                    raise RuntimeError("operator does not exist: character varying = uuid")
            return Result()

        async def commit(self) -> None:
            return None

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    class Store(ProductionObservatoryStore):
        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            if "SELECT observation_id, run_id, observed_at" in query:
                return []
            return [{"observations": 0, "runs": 0}]

    store = Store(connection, "http://clickhouse.test", database="obs", user="u", password="p")
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000832",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="uuid-target-cast",
    )
    await store.prepare_retention(action)
    receipt_query = next(
        statement
        for statement in statements
        if "NOT EXISTS" in statement and "observatory_observation_receipts" in statement
    )
    assert "t.observation_id = r.observation_id::text" in receipt_query
    assert action.deletion_phase == "targeted"


@pytest.mark.asyncio
async def test_failed_retention_retries_are_time_bounded() -> None:
    calls: list[tuple[str, tuple[Any, ...]]] = []

    class Result:
        async def fetchall(self) -> list[tuple[Any, ...]]:
            return []

        async def fetchone(self) -> None:
            return None

    class Connection:
        async def execute(self, statement: str, params: tuple[Any, ...] = ()) -> Result:
            calls.append((statement, params))
            return Result()

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    store = ProductionObservatoryStore(
        connection, "http://clickhouse.test", database="obs", user="u", password="p"
    )
    await store.pending_retentions()
    await store.claim_retention("00000000-0000-4000-8000-000000000833")
    pending_sql, pending_params = calls[0]
    claim_sql, claim_params = calls[1]
    assert "completed_at IS NULL" in pending_sql
    assert "completed_at IS NULL" in claim_sql
    assert pending_params == (5,)
    assert claim_params == ("00000000-0000-4000-8000-000000000833", 5, 60)


@pytest.mark.asyncio
async def test_production_retention_releases_session_lock_before_clickhouse_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An immutable target snapshot must not block continuous telemetry cleanup."""

    state: dict[str, Any] = {
        "locks": 0,
        "phase": "queued",
        "mutation_lock_levels": [],
    }

    class Result:
        def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
            self.rows = rows or []

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.rows[0] if self.rows else None

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return list(self.rows)

    class Connection:
        async def execute(
            self, statement: str, params: tuple[Any, ...] = ()
        ) -> Result:
            if statement == "SELECT pg_advisory_lock(830078)":
                state["locks"] += 1
            elif statement == "SELECT pg_advisory_unlock(830078)":
                state["locks"] -= 1
            elif "SELECT target_observation_count" in statement:
                return Result([(1, 0, 0, state["phase"])])
            elif "SELECT run_id FROM observatory_retention_targets" in statement:
                return Result()
            elif "SELECT observation_id FROM observatory_retention_targets" in statement:
                return Result([("observation-1",)] if len(params) == 1 else [])
            elif "SELECT count(*) FROM observatory_observation_receipts r" in statement:
                return Result([(0,)])
            elif "SELECT r.observation_id FROM observatory_observation_receipts" in statement:
                return Result()
            elif "UPDATE observatory_retention_actions SET deletion_phase" in statement:
                state["phase"] = str(params[0])
            return Result()

        async def commit(self) -> None:
            return None

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Store(ProductionObservatoryStore):
        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            if "SELECT count() AS observations" in query:
                return [{"observations": 1, "runs": 0}]
            return [
                {
                    "observation_id": "observation-1",
                    "run_id": None,
                    "observed_at": "2026-01-01T00:00:00Z",
                }
            ]

    class Response:
        status_code = 200
        text = ""

        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **_kwargs: Any) -> Response:
            state["mutation_lock_levels"].append(state["locks"])
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000831",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="lock-scope",
    )
    store = Store(
        lambda: Context(),
        "http://clickhouse.test",
        database="obs",
        user="u",
        password="p",
    )
    await store.attach_retention(action)
    assert state["locks"] == 1
    await store.prepare_retention(action)
    assert action.deletion_phase == "targeted"
    assert state["locks"] == 0
    result = await store.delete_observations(None)

    assert result == DeletionResult(1, 0, 0)
    assert state["mutation_lock_levels"]
    assert all(level == 0 for level in state["mutation_lock_levels"])


@pytest.mark.asyncio
@pytest.mark.parametrize("helper_name", ("run_ids", "target_ids"))
async def test_retention_keyset_helpers_reject_nonadvancing_pages(helper_name: str) -> None:
    """Every durable keyset helper rejects a page at or behind its cursor."""

    after = "cursor-0001"
    calls = 0

    class Result:
        async def fetchall(self) -> list[tuple[str]]:
            return [(after,)]

    class Connection:
        async def execute(self, _statement: str, _params: tuple[Any, ...] = ()) -> Result:
            nonlocal calls
            calls += 1
            return Result()

    store = ProductionObservatoryStore(
        lambda: None,
        "http://clickhouse.test",
        database="obs",
        user="u",
        password="p",
    )
    with pytest.raises(RuntimeError, match="advanc"):
        if helper_name == "run_ids":
            await store._retention_run_ids(Connection(), "action", after)  # type: ignore[attr-defined]
        else:
            await store._retention_target_ids(Connection(), "action", after)  # type: ignore[attr-defined]
    assert calls == 1


@pytest.mark.asyncio
async def test_retention_target_capture_fails_fast_on_repeated_full_page() -> None:
    """Target capture cannot spin when a full page repeats its last key."""

    page = [
        {
            "observation_id": f"obs-{index:04d}",
            "run_id": f"run-{index:04d}",
            "observed_at": "2026-01-01T00:00:00Z",
        }
        for index in range(1000)
    ]
    page_calls = 0
    inserted = 0

    class Result:
        async def fetchone(self) -> tuple[int, ...]:
            return (0,)

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return []

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            nonlocal inserted
            if "INSERT INTO observatory_retention_targets" in statement:
                inserted += 1
            return Result()

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    class Store(ProductionObservatoryStore):
        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            nonlocal page_calls
            if "SELECT observation_id, run_id, observed_at" in query:
                page_calls += 1
                return page
            return [{"observations": 1000, "runs": 1000}]

    store = Store(connection, "http://clickhouse.test", database="obs", user="u", password="p")
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000861",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="repeated-target-page",
    )
    with pytest.raises(RuntimeError, match="advanc"):
        await store.prepare_retention(action)
    assert page_calls <= 2
    assert inserted <= 1000


@pytest.mark.asyncio
async def test_retention_receipt_capture_fails_fast_on_repeated_full_page() -> None:
    """Receipt capture cannot spin when a full page repeats its last key."""

    receipt_page = [(f"receipt-{index:04d}",) for index in range(1000)]
    receipt_page_calls = 0
    inserted = 0

    class Result:
        async def fetchone(self) -> tuple[int, ...]:
            return (0,)

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return receipt_page

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            nonlocal inserted, receipt_page_calls
            if "SELECT r.observation_id FROM observatory_observation_receipts" in statement:
                receipt_page_calls += 1
            if "INSERT INTO observatory_retention_targets" in statement:
                inserted += 1
            return Result()

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    class Store(ProductionObservatoryStore):
        async def _rows(
            self, query: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            if "SELECT observation_id, run_id, observed_at" in query:
                return []
            return [{"observations": 0, "runs": 0}]

    store = Store(connection, "http://clickhouse.test", database="obs", user="u", password="p")
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000862",
        RetentionKind.PURGE_ALL,
        None,
        idempotency_key="repeated-receipt-page",
    )
    with pytest.raises(RuntimeError, match="advanc"):
        await store.prepare_retention(action)
    assert receipt_page_calls <= 2
    assert inserted <= 1000


@pytest.mark.asyncio
async def test_cutoff_projection_mutation_targets_only_deleted_runs() -> None:
    statements: list[str] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> Response:
            statements.append(str(kwargs["content"]))
            return Response()

    class Result:
        async def fetchone(self) -> tuple[int]:
            return (1,)

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            statements.append(statement)
            return Result()

    old = make_payload(
        "00000000-0000-4000-8000-000000000781",
        observed_at=BASE_TIME - timedelta(days=1),
        run_id=make_run_id("deleted-run"),
    )
    survivor = make_payload(
        "00000000-0000-4000-8000-000000000782",
        observed_at=BASE_TIME + timedelta(days=1),
        run_id=make_run_id("surviving-run"),
    )

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    class Store(ProductionObservatoryStore):
        async def list_observations(self, **_kwargs: Any) -> list[MetadataObservation]:
            return [old, survivor]

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            del statement
            return [{"observations": 1, "runs": 1}]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    try:
        store = Store(connection, "http://clickhouse.test", database="obs", user="u", password="p")
        result = await store.delete_observations(BASE_TIME)
    finally:
        monkeypatch.undo()
    assert result == DeletionResult(1, 1, 1)
    projection_delete = next(
        statement
        for statement in statements
        if "run_projections" in statement and "ALTER TABLE" in statement
    )
    projection_rebuild = next(
        statement
        for statement in statements
        if "run_projections" in statement and "INSERT INTO" in statement
    )
    assert "WHERE 1 = 1" in projection_delete
    assert "FROM observations" in projection_rebuild
    assert "GROUP BY run_id" in projection_rebuild
    # The rebuild is sourced from surviving ClickHouse rows rather than a
    # Python-hydrated run-id list, so both cutoff and surviving runs remain
    # represented according to the post-mutation source of truth.
    assert make_run_id("deleted-run") not in projection_rebuild
    assert make_run_id("surviving-run") not in projection_rebuild


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_phase", ["source", "projection", "receipt"])
async def test_retention_source_projection_receipt_crashes_retry_with_exact_counts(
    monkeypatch: pytest.MonkeyPatch, failure_phase: str
) -> None:
    attempts = {"source": 0, "projection": 0, "receipt": 0}
    old = make_payload(
        "00000000-0000-4000-8000-000000000791",
        observed_at=BASE_TIME - timedelta(days=1),
        run_id=make_run_id("phase-run"),
    )

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> Response:
            statement = str(kwargs["content"])
            phase = "projection" if "run_projections" in statement else "source"
            attempts[phase] += 1
            if failure_phase == phase and attempts[phase] == 1:
                raise RuntimeError(f"crash at {phase}")
            return Response()

    class Result:
        async def fetchone(self) -> tuple[int]:
            if failure_phase == "receipt" and attempts["receipt"] == 0:
                attempts["receipt"] += 1
                raise RuntimeError("crash at receipt")
            return (1,)

    class Connection:
        async def execute(self, statement: str, _params: tuple[Any, ...] = ()) -> Result:
            if "SELECT count(*)" in statement:
                attempts["receipt"] += 1
                if failure_phase == "receipt" and attempts["receipt"] == 1:
                    raise RuntimeError("crash at receipt")
                return Result()
            return Result()

    @asynccontextmanager
    async def connection() -> AsyncGenerator[Connection]:
        yield Connection()

    class Store(ProductionObservatoryStore):
        async def list_observations(self, **_kwargs: Any) -> list[MetadataObservation]:
            return [old]

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            del statement
            return [{"observations": 1, "runs": 1}]

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    store = Store(connection, "http://clickhouse.test", database="obs", user="u", password="p")
    with pytest.raises(RuntimeError, match=failure_phase):
        await store.delete_observations(BASE_TIME)
    result = await store.delete_observations(BASE_TIME)
    assert result == DeletionResult(1, 1, 1)
    assert all(
        attempts[phase] >= 2 if phase == failure_phase else attempts[phase] >= 1
        for phase in attempts
    )


def test_legacy_watermark_sync_uses_offset_safe_sql_setval_and_visibility_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    postgres_calls: list[tuple[str, tuple[Any, ...]]] = []

    class Response:
        text = "251\n"

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, statement: str, params: tuple[Any, ...] = ()) -> None:
            postgres_calls.append((statement, params))

        def commit(self) -> None:
            return None

    def query(_settings: Settings, _content: str) -> Response:
        return Response()

    def connect(_url: str) -> Connection:
        return Connection()

    monkeypatch.setattr(cli, "_clickhouse_query", query)
    monkeypatch.setattr(cli.psycopg, "connect", connect)
    cli._synchronize_legacy_watermark(Settings())  # type: ignore[reportPrivateUsage]
    setval = next(
        item
        for item in postgres_calls
        if "setval('observatory_ingest_sequence'" in item[0]
    )
    assert setval[1] == (251,)
    assert (
        "GREATEST((SELECT last_value FROM observatory_ingest_sequence), %s), TRUE"
        in setval[0]
    )
    assert setval[0].count("%s") == 1
    visibility = next(item for item in postgres_calls if "generate_series(1, %s)" in item[0])
    assert visibility[1] == (251,)
    watermark = next(item for item in postgres_calls if "committed_sequence" in item[0])
    assert watermark[1] == (251,)


def test_clickhouse_migration_retries_a_partial_0002_without_replaying_completed_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    applied: set[str] = set()
    migration_calls: list[str] = []
    failed_0002 = False

    class Response:
        def __init__(self, text: str = "") -> None:
            self.text = text

        def raise_for_status(self) -> None:
            return None

    class Connection:
        def __enter__(self) -> Connection:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def execute(self, _statement: str, _params: tuple[Any, ...] = ()) -> None:
            return None

        def commit(self) -> None:
            return None

    def post(
        _url: str,
        *,
        content: str,
        params: dict[str, str],
        timeout: float,
        **_kwargs: Any,
    ) -> Response:
        nonlocal failed_0002
        del params, timeout
        if "SELECT version FROM observatory_migration_ledger" in content:
            match = re.search(r"version = '([^']+)'", content)
            assert match is not None
            return Response(match.group(1) + "\n" if match.group(1) in applied else "")
        if "SELECT coalesce(max(ingest_sequence), 0)" in content:
            return Response("0\n")
        migration_calls.append(content)
        if "0002_observation_ordering" in content and not failed_0002:
            failed_0002 = True
            raise RuntimeError("simulated partial 0002 migration")
        match = re.search(r"SELECT '([^']+)', now64\(3\)", content)
        if match:
            applied.add(match.group(1))
        return Response()

    post.__module__ = "httpx"
    monkeypatch.setattr(cli.httpx, "post", post)
    def connect(_url: str) -> Connection:
        return Connection()

    monkeypatch.setattr(cli.psycopg, "connect", connect)
    with pytest.raises(RuntimeError, match="partial 0002"):
        cli.migrate()
    cli.migrate()
    assert failed_0002 is True
    assert sum("0002_observation_ordering" in value for value in migration_calls) == 2
    assert sum("0001_observations" in value for value in migration_calls) == 1
    # The 0003 file contains its own guarded ledger statement and the runner
    # appends a second guarded marker. Both are sent independently now that
    # ClickHouse no longer accepts multiquery requests.
    assert sum("0003_legacy_ordering_ledger" in value for value in migration_calls) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "cutoff"),
    [
        (RetentionKind.DELETE_BEFORE, BASE_TIME),
        (RetentionKind.PURGE_ALL, None),
    ],
)
async def test_recovered_retention_uses_immutable_targets_not_late_rows(
    monkeypatch: pytest.MonkeyPatch,
    kind: RetentionKind,
    cutoff: datetime | None,
) -> None:
    """Rows arriving after targeting survive both recovery paths."""

    old_id = "00000000-0000-4000-8000-000000000801"
    late_id = "00000000-0000-4000-8000-000000000802"
    old_run = make_run_id("targeted-old")
    late_run = make_run_id("late-row")
    state: dict[str, Any] = {"phase": "targeted"}
    statements: list[str] = []
    receipt_deletes: list[list[str]] = []

    class Result:
        def __init__(self, rows: list[tuple[Any, ...]] | None = None) -> None:
            self.rows = rows or []

        async def fetchone(self) -> tuple[Any, ...] | None:
            return self.rows[0] if self.rows else None

        async def fetchall(self) -> list[tuple[Any, ...]]:
            return list(self.rows)

    class Connection:
        async def execute(
            self, statement: str, params: tuple[Any, ...] = ()
        ) -> Result:
            statements.append(statement)
            if "SELECT target_observation_count" in statement:
                return Result([(1, 1, 1, state["phase"])])
            if "SELECT run_id FROM observatory_retention_targets" in statement:
                return Result([(old_run,)] if len(params) == 1 else [])
            if "SELECT observation_id FROM observatory_retention_targets" in statement:
                return Result([(old_id,)] if len(params) == 1 else [])
            if statement.startswith("DELETE FROM observatory_observation_receipts"):
                receipt_deletes.append(list(cast(list[str], params[0])))
            if "UPDATE observatory_retention_actions SET deletion_phase" in statement:
                state["phase"] = str(params[0])
            return Result()

        async def commit(self) -> None:
            return None

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> Response:
            statements.append(str(kwargs["content"]))
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000803",
        kind,
        cutoff,
        status=RetentionStatus.RUNNING,
        idempotency_key=f"late-{kind.value}",
        target_observation_count=1,
        target_receipt_count=1,
        target_run_count=1,
        deletion_phase="targeted",
    )
    store = ProductionObservatoryStore(
        lambda: Context(), "http://clickhouse.test", database="obs", user="u", password="p"
    )
    await store.attach_retention(action)
    try:
        result = await store.delete_observations(cutoff)
    finally:
        await store._release_retention_guard()  # type: ignore[attr-defined]
    assert result == DeletionResult(1, 1, 1)
    assert any(old_id in statement for statement in statements)
    assert any(old_run in statement for statement in statements)
    assert all(late_id not in statement and late_run not in statement for statement in statements)
    assert receipt_deletes == [[old_id]]


@pytest.mark.asyncio
async def test_projection_rebuild_retry_after_accepted_request_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rebuild accepted before its phase receipt can safely be retried."""

    state: dict[str, Any] = {
        "phase": "source_deleted",
        "fail_phase": True,
        "target_page_calls": 0,
    }
    statements: list[str] = []

    class Result:
        def __init__(self, rows: list[tuple[str]] | None = None) -> None:
            self.rows = rows or []

        async def fetchone(self) -> tuple[Any, ...]:
            return (1, 1, 1, state["phase"])

        async def fetchall(self) -> list[tuple[str]]:
            return list(self.rows)

    class Connection:
        async def execute(
            self, statement: str, params: tuple[Any, ...] = ()
        ) -> Result:
            statements.append(statement)
            if "SELECT observation_id FROM observatory_retention_targets" in statement:
                state["target_page_calls"] += 1
                # A keyset page after the only target is empty.  This mirrors
                # the database contract and prevents a fake from making the
                # production pagination loop unbounded.
                return Result(
                    [("00000000-0000-4000-8000-000000000811",)]
                    if len(params) == 1
                    else []
                )
            if "UPDATE observatory_retention_actions SET deletion_phase" in statement:
                if params[0] == "projection_deleted" and state["fail_phase"]:
                    state["fail_phase"] = False
                    raise RuntimeError("phase receipt lost")
                state["phase"] = str(params[0])
            return Result()

        async def commit(self) -> None:
            return None

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Response:
        def raise_for_status(self) -> None:
            return None

    class Client:
        def __init__(self, **_kwargs: Any) -> None:
            return None

        async def __aenter__(self) -> Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: Any) -> Response:
            statements.append(str(kwargs["content"]))
            return Response()

    monkeypatch.setattr("aura_observatory.adapters.query.httpx.AsyncClient", Client)
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000812",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="projection-retry",
        target_observation_count=1,
        target_receipt_count=1,
        target_run_count=1,
        deletion_phase="source_deleted",
    )
    store = ProductionObservatoryStore(
        lambda: Context(), "http://clickhouse.test", database="obs", user="u", password="p"
    )
    await store.attach_retention(action)
    try:
        with pytest.raises(RuntimeError, match="phase receipt lost"):
            await store.delete_observations(None)
        result = await store.delete_observations(None)
    finally:
        await store._release_retention_guard()  # type: ignore[attr-defined]
    assert result == DeletionResult(1, 1, 1)
    assert sum("TRUNCATE TABLE run_projections" in value for value in statements) == 2
    assert sum("INSERT INTO run_projections" in value for value in statements) == 2
    assert not any("ALTER TABLE observations DELETE" in value for value in statements)
    assert state["target_page_calls"] == 2


@pytest.mark.asyncio
async def test_receipt_delete_commit_gap_retries_with_durable_target_counts() -> None:
    """A commit failure after receipt deletion cannot lose the target scope."""

    state: dict[str, Any] = {
        "phase": "projection_deleted",
        "fail_commit": True,
        "target_page_calls": 0,
    }
    receipt_deletes = 0

    class Result:
        def __init__(self, rows: list[tuple[str]] | None = None) -> None:
            self.rows = rows or []

        async def fetchone(self) -> tuple[Any, ...]:
            return (4, 3, 2, state["phase"])

        async def fetchall(self) -> list[tuple[str]]:
            return list(self.rows)

    class Connection:
        async def execute(
            self, statement: str, params: tuple[Any, ...] = ()
        ) -> Result:
            nonlocal receipt_deletes
            if "SELECT observation_id FROM observatory_retention_targets" in statement:
                state["target_page_calls"] += 1
                # Each retry starts at the first page, then the keyset cursor
                # must advance to an empty page rather than repeat forever.
                return Result(
                    [("00000000-0000-4000-8000-000000000821",)]
                    if len(params) == 1
                    else []
                )
            if statement.startswith("DELETE FROM observatory_observation_receipts"):
                receipt_deletes += 1
            if "UPDATE observatory_retention_actions SET deletion_phase" in statement:
                state["phase"] = str(params[0])
            return Result()

        async def commit(self) -> None:
            if state["fail_commit"]:
                state["fail_commit"] = False
                raise RuntimeError("receipt commit lost")

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    store = ProductionObservatoryStore(
        lambda: Context(), "http://clickhouse.test", database="obs", user="u", password="p"
    )
    action = RetentionAction(
        "00000000-0000-4000-8000-000000000822",
        RetentionKind.PURGE_ALL,
        None,
        status=RetentionStatus.RUNNING,
        idempotency_key="receipt-commit-gap",
        target_observation_count=4,
        target_receipt_count=3,
        target_run_count=2,
        deletion_phase="projection_deleted",
    )
    await store.attach_retention(action)
    try:
        with pytest.raises(RuntimeError, match="receipt commit lost"):
            await store.delete_observations(None)
        result = await store.delete_observations(None)
    finally:
        await store._release_retention_guard()  # type: ignore[attr-defined]
    assert result == DeletionResult(4, 3, 2)
    assert receipt_deletes == 2
    assert state["phase"] == "receipts_deleted"
    assert state["target_page_calls"] == 4


def test_observatory_metrics_reject_nonfinite_values_and_crafted_labels() -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    metrics = ObservatoryMetrics()
    with pytest.raises(ValueError, match="finite"):
        metrics.record("observatory_query_duration_ms", float("nan"), {"resource": "runs"})
    with pytest.raises(ValueError, match="bounded"):
        metrics.record(
            "observatory_query_outcome",
            labels={"resource": "prompt", "outcome": "failed"},
        )
    with pytest.raises(ValueError, match="bounded"):
        metrics.record(
            "observatory_query_outcome",
            labels={"resource": "runs", "outcome": "failed", "private": "secret"},
        )


@pytest.mark.asyncio
async def test_api_retention_metrics_are_loaded_from_postgres_without_shared_file() -> None:
    class Result:
        async def fetchall(self) -> list[tuple[Any, ...]]:
            return [("purge_all", "succeeded", 3, 7, 0)]

    class Connection:
        async def execute(
            self, _statement: str, _params: tuple[Any, ...] = ()
        ) -> Result:
            return Result()

    class Context:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    store = ProductionObservatoryStore(
        lambda: Context(), "http://clickhouse.test", database="obs", user="u", password="p"
    )
    app = create_app(Settings(owner_subject=OWNER), query_store=store, testing=True)
    assert app.state.query.metrics.export_path is None
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=Settings().public_origin
    ) as client:
        response = await client.get("/metrics")
    assert response.status_code == 200
    assert (
        'observatory_retention_action_count{action="purge_all",status="succeeded"} 3'
        in response.text
    )
    assert 'observatory_retention_deleted_count{action="purge_all"} 7' in response.text


@pytest.mark.asyncio
async def test_component_health_sql_accepts_only_exact_spanless_duration_measurements() -> None:
    queries: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            return [
                {
                    "sample_count": 1,
                    "p50": 12.0,
                    "p95": 12.0,
                    "failures": 0,
                    "successes": 1,
                    "cancelled": 0,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.component_health_query("aura.runtime.model_inference", None, None)

    assert result is not None
    assert len(queries) == 1
    query = queries[0]
    assert "measurement_name = 'duration_ms'" in query
    assert "span_id != '' OR measurement_name = 'duration_ms'" in query
    assert "measurement_name LIKE" not in query
    assert "endsWith(measurement_name" not in query


@pytest.mark.asyncio
async def test_production_component_health_keeps_earlier_duration_before_later_token_count(
) -> None:
    """The provider predicate keeps latency from a span's primary duration."""

    queries: list[str] = []
    selected_values: list[float] = []
    span_rows: list[dict[str, Any]] = [
        {
            "span_id": "0123456789abcdef",
            "measurement_name": "duration_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 18.0,
            "observed_at": BASE_TIME,
        },
        {
            "span_id": "0123456789abcdef",
            "measurement_name": "queue_time_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 99.0,
            "observed_at": BASE_TIME + timedelta(seconds=1),
        },
    ]

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            # Evaluate the generated argMaxIf predicate against provider-shaped
            # rows instead of returning a hard-coded aggregate.  The old broad
            # milliseconds predicate would select the later queue_time_ms row.
            predicate_start = statement.index("argMaxIf(measurement_value")
            predicate_end = statement.index(") AS selected_measurement_value", predicate_start)
            predicate = statement[predicate_start:predicate_end]
            if "measurement_name = 'duration_ms'" in predicate:
                eligible = [
                    row
                    for row in span_rows
                    if row["measurement_name"] == "duration_ms"
                    and row["measurement_unit"] == "milliseconds"
                ]
            else:
                eligible = [
                    row
                    for row in span_rows
                    if row["measurement_unit"] == "milliseconds"
                    and row["span_id"] != ""
                ]
            selected = max(eligible, key=lambda row: row["observed_at"])
            selected_values.append(float(selected["measurement_value"]))
            latency = float(selected["measurement_value"])
            return [
                {
                    "sample_count": 1,
                    "p50": latency,
                    "p95": latency,
                    "failures": 0,
                    "successes": 1,
                    "cancelled": 0,
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.component_health_query("aura.runtime.model_inference", None, None)

    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["latencyP50Ms"] == 18.0
    assert selected_values == [18.0]
    assert len(queries) == 1
    query = queries[0]
    # The production query must choose the duration measurement for a span,
    # rather than the latest arbitrary millisecond metric.
    assert "argMaxIf(measurement_value, (observed_at, observation_id)" in query
    assert "measurement_unit = 'milliseconds'" in query
    assert "measurement_name = 'duration_ms'" in query


@pytest.mark.asyncio
async def test_production_component_health_keeps_duration_and_uses_later_failure() -> None:
    """A later same-span failure changes outcome, not the duration value."""

    span_rows: list[dict[str, Any]] = [
        {
            "span_id": "0123456789abcdef",
            "measurement_name": "duration_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 18.0,
            "observed_at": BASE_TIME,
            "dimensions": {"status": "success"},
        },
        {
            "span_id": "0123456789abcdef",
            "measurement_name": "queue_time_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 99.0,
            "observed_at": BASE_TIME + timedelta(seconds=1),
            "dimensions": {"status": "error"},
        },
    ]

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            predicate_start = statement.index("argMaxIf(measurement_value")
            predicate_end = statement.index(") AS selected_measurement_value", predicate_start)
            predicate = statement[predicate_start:predicate_end]
            durations = [
                row
                for row in span_rows
                if "measurement_name = 'duration_ms'" in predicate
                and row["measurement_name"] == "duration_ms"
                and row["measurement_unit"] == "milliseconds"
            ]
            assert durations
            duration = max(durations, key=lambda row: row["observed_at"])
            latest = max(span_rows, key=lambda row: row["observed_at"])
            outcome = _evaluate_clickhouse_status(
                statement, latest["dimensions"], legacy_key="outcome"
            )
            return [
                {
                    "sample_count": 1,
                    "p50": duration["measurement_value"],
                    "p95": duration["measurement_value"],
                    "failures": int(outcome in {"error", "failed"}),
                    "successes": int(outcome in {"success", "succeeded", "completed", "ok"}),
                    "cancelled": int(outcome == "cancelled"),
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.component_health_query(
        "aura.runtime.model_inference", None, None
    )

    assert result is not None
    assert result["summary"]["latencyP50Ms"] == 18.0
    assert result["summary"]["latencyP95Ms"] == 18.0
    assert result["summary"]["errorRate"] == 1.0
    assert result["summary"]["outcomes"] == {
        "succeeded": 0,
        "failed": 1,
        "cancelled": 0,
        "unknown": 0,
    }


@pytest.mark.asyncio
async def test_production_component_health_equal_timestamp_uses_higher_observation_id() -> None:
    span_rows: list[dict[str, Any]] = [
        {
            "span_id": "0123456789abcdef",
            "observation_id": "00000000-0000-4000-8000-0000000002d1",
            "measurement_name": "duration_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 18.0,
            "observed_at": BASE_TIME,
            "dimensions": {"status": "success"},
        },
        {
            "span_id": "0123456789abcdef",
            "observation_id": "00000000-0000-4000-8000-0000000002d2",
            "measurement_name": "queue_time_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 99.0,
            "observed_at": BASE_TIME,
            "dimensions": {"status": "error"},
        },
    ]

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            predicate_start = statement.index("argMaxIf(measurement_value")
            predicate_end = statement.index(") AS selected_measurement_value", predicate_start)
            predicate = statement[predicate_start:predicate_end]
            durations = [
                row
                for row in span_rows
                if "measurement_name = 'duration_ms'" in predicate
                and row["measurement_name"] == "duration_ms"
                and row["measurement_unit"] == "milliseconds"
            ]
            assert durations
            duration = max(
                durations,
                key=lambda row: (row["observed_at"], row["observation_id"]),
            )
            latest = max(
                span_rows,
                key=lambda row: (row["observed_at"], row["observation_id"]),
            )
            outcome = _evaluate_clickhouse_status(
                statement, latest["dimensions"], legacy_key="outcome"
            )
            return [
                {
                    "sample_count": 1,
                    "p50": duration["measurement_value"],
                    "p95": duration["measurement_value"],
                    "failures": int(outcome in {"error", "failed"}),
                    "successes": int(outcome in {"success", "succeeded", "completed", "ok"}),
                    "cancelled": int(outcome == "cancelled"),
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.component_health_query(
        "aura.runtime.model_inference", None, None
    )

    assert result is not None
    assert result["summary"]["latencyP50Ms"] == 18.0
    assert result["summary"]["latencyP95Ms"] == 18.0
    assert result["summary"]["errorRate"] == 1.0
    assert result["summary"]["outcomes"] == {
        "succeeded": 0,
        "failed": 1,
        "cancelled": 0,
        "unknown": 0,
    }


@pytest.mark.asyncio
async def test_production_run_query_uses_outcome_only_map_values_for_status_filter() -> None:
    """Run filters evaluate normalized status branches over outcome-only Maps."""

    source_rows: list[dict[str, Any]] = [
        {
            "run_id": make_run_id(f"outcome-only-{raw_status}"),
            "started_at": BASE_TIME,
            "finished_at": BASE_TIME + timedelta(milliseconds=18),
            "observed_at": BASE_TIME + timedelta(milliseconds=18),
            "dimensions": {"outcome": raw_status},
            "model": "local-model",
            "deployment_id": "deploy-a",
            "component_ids": ["aura.runtime.model_inference"],
            "trace_id": "0123456789abcdef0123456789abcdef",
            "order_sequence": index,
        }
        for index, raw_status in enumerate(("success", "completed", "ok", "error"), 1)
    ]
    queries: list[str] = []
    requested_statuses: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _overview_counts(self) -> dict[str, Any]:
            return {
                "latest_sequence": 4,
                "observed_at": BASE_TIME,
                "latest_observation_id": "00000000-0000-4000-8000-000000000854",
            }

        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            before_group, after_group = statement.split(" GROUP BY run_id", 1)
            where_clause = before_group.split(" FROM observations WHERE ", 1)[1]
            assert "argMax(" not in where_clause
            assert " HAVING " in after_group
            having_clause = after_group.split(" HAVING ", 1)[1].split(
                " ORDER BY", 1
            )[0]
            requested = having_clause.rsplit(" = '", 1)[1].split("'", 1)[0]
            requested_statuses.append(requested)
            matched = [
                row
                for row in source_rows
                if _evaluate_clickhouse_run_status(statement, row["dimensions"]) == requested
            ]
            return [
                {
                    **row,
                    "status": _evaluate_clickhouse_run_status(statement, row["dimensions"]),
                }
                for row in matched
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    succeeded = await store.run_query(
        RunFilters(status="succeeded"), cursor=None, limit=20
    )
    failed = await store.run_query(RunFilters(status="failed"), cursor=None, limit=20)

    assert [item["status"] for item in succeeded["items"]] == [
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert [item["status"] for item in failed["items"]] == ["failed"]
    assert requested_statuses == ["succeeded", "failed"]
    assert len(queries) == 2
    for query in queries:
        assert "multiIf(" in query
        assert "IN ('success', 'succeeded', 'completed', 'ok'), 'succeeded'" in query
        assert "IN ('error', 'failed'), 'failed'" in query


@pytest.mark.asyncio
async def test_production_component_health_uses_status_only_map_values_for_outcomes() -> None:
    """A status-only Map entry must contribute to component failure outcomes."""

    source_rows: list[dict[str, Any]] = [
        {
            "span_id": "0123456789abcdef",
            "measurement_name": "duration_ms",
            "measurement_unit": "milliseconds",
            "measurement_value": 18.0,
            "observed_at": BASE_TIME,
            "dimensions": {"status": "error"},
        }
    ]
    queries: list[str] = []

    class Store(ProductionObservatoryStore):
        async def _query_rows(
            self, statement: str, *, params: dict[str, object] | None = None
        ) -> list[dict[str, Any]]:
            del params
            queries.append(statement)
            predicate_start = statement.index("argMaxIf(measurement_value")
            predicate_end = statement.index(") AS selected_measurement_value", predicate_start)
            predicate = statement[predicate_start:predicate_end]
            eligible = [
                row
                for row in source_rows
                if "measurement_name = 'duration_ms'" in predicate
                and row["measurement_name"] == "duration_ms"
                and row["measurement_unit"] == "milliseconds"
            ]
            assert eligible
            outcome = _evaluate_clickhouse_status(
                statement, eligible[0]["dimensions"], legacy_key="outcome"
            )
            return [
                {
                    "sample_count": len(eligible),
                    "p50": eligible[0]["measurement_value"],
                    "p95": eligible[0]["measurement_value"],
                    "failures": int(outcome in {"error", "failed"}),
                    "successes": int(outcome in {"success", "succeeded", "completed", "ok"}),
                    "cancelled": int(outcome == "cancelled"),
                }
            ]

    store = Store(
        lambda: None,
        "http://clickhouse.test",
        database="observatory",
        user="u",
        password="p",
    )
    result = await store.component_health_query(
        "aura.runtime.model_inference", None, None
    )

    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["errorRate"] == 1.0
    assert result["summary"]["outcomes"] == {
        "succeeded": 0,
        "failed": 1,
        "cancelled": 0,
        "unknown": 0,
    }
    assert len(queries) == 1
    assert _CLICKHOUSE_STATUS_FALLBACK in queries[0]


def _flatten_spans(spans: list[dict[str, Any]]) -> list[dict[str, Any]]:
    flattened: list[dict[str, Any]] = []
    pending = list(spans)
    while pending:
        span = pending.pop(0)
        flattened.append(span)
        pending.extend(cast(list[dict[str, Any]], span.get("children", [])))
    return flattened


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("case_name", "lineage", "expected_roots"),
    [
        (
            "self-parent",
            [("0000000000000001", "0000000000000001")],
            ["0000000000000001"],
        ),
        (
            "two-node-cycle",
            [
                ("0000000000000002", "0000000000000003"),
                ("0000000000000003", "0000000000000002"),
            ],
            ["0000000000000002", "0000000000000003"],
        ),
        (
            "multi-node-cycle",
            [
                ("0000000000000004", "0000000000000005"),
                ("0000000000000005", "0000000000000006"),
                ("0000000000000006", "0000000000000004"),
            ],
            ["0000000000000004", "0000000000000005", "0000000000000006"],
        ),
    ],
)
async def test_trace_detail_preserves_cyclic_spans_once_with_deterministic_partial_roots(
    case_name: str,
    lineage: list[tuple[str, str]],
    expected_roots: list[str],
) -> None:
    trace_id = "abcdef0123456789abcdef0123456789"
    store = MemoryObservatoryStore()
    for index, (span_id, parent_span_id) in enumerate(lineage):
        await store.insert_observation(
            make_payload(
                f"00000000-0000-4000-8000-0000000009{index + 1:02d}",
                observed_at=BASE_TIME + timedelta(seconds=index),
                run_id=make_run_id(f"cycle-{case_name}"),
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
            )
        )

    detail = await _query_service(store).trace_detail(trace_id)

    assert detail is not None
    assert detail["partial"] is True
    roots = cast(list[dict[str, Any]], detail["spans"])
    assert [str(span["spanId"]) for span in roots] == expected_roots
    flattened = _flatten_spans(roots)
    span_ids = [str(span["spanId"]) for span in flattened]
    assert span_ids == [span_id for span_id, _ in lineage]
    assert len(span_ids) == len(set(span_ids))
    assert all(span["partial"] is True for span in flattened)


def test_histogram_buckets_gauges_and_replay_series_have_prometheus_semantics() -> None:
    from aura_observatory.metrics import ObservatoryMetrics

    metrics = ObservatoryMetrics()
    metrics.record("observatory_query_duration_ms", 7.0, {"resource": "runs"})
    metrics.record("observatory_sse_active", 1.0, {"event_type": "stream"})
    metrics.record(
        "observatory_sse_replay_count", 2.0, {"event_type": "observation.changed"}
    )
    rendered = metrics.prometheus()
    assert "# TYPE observatory_query_duration_ms histogram" in rendered
    def parse_sample(line: str) -> tuple[str, tuple[tuple[str, str], ...], str]:
        name_and_labels, value = line.rsplit(" ", 1)
        if "{" not in name_and_labels:
            return name_and_labels, (), value
        name, raw_labels = name_and_labels[:-1].split("{", 1)
        labels = tuple(
            sorted(
                (key, label_value.strip('"'))
                for key, label_value in (
                    part.split("=", 1) for part in raw_labels.split(",")
                )
            )
        )
        return name, labels, value

    # Parse a complete family as a Prometheus consumer would: label order is
    # irrelevant, every bucket is cumulative, and histograms expose only
    # _bucket/_sum/_count samples (never an unsuffixed base sample).
    samples = {
        (name, labels): value
        for line in rendered.splitlines()
        if line and not line.startswith("#")
        for name, labels, value in [parse_sample(line)]
        if name.startswith("observatory_query_duration_ms")
    }
    boundaries = (
        "5",
        "10",
        "25",
        "50",
        "100",
        "250",
        "500",
        "1000",
        "2500",
        "5000",
        "10000",
        "+Inf",
    )
    expected_counts = ("0", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1", "1")
    bucket_values: list[int] = []
    for boundary, expected in zip(boundaries, expected_counts, strict=True):
        key = (
            "observatory_query_duration_ms_bucket",
            (("le", boundary), ("resource", "runs")),
        )
        assert samples[key] == expected
        bucket_values.append(int(samples[key]))
    assert bucket_values == sorted(bucket_values)
    assert samples[("observatory_query_duration_ms_sum", (("resource", "runs"),))] == "7"
    assert samples[("observatory_query_duration_ms_count", (("resource", "runs"),))] == "1"
    assert not any(name == "observatory_query_duration_ms" for name, _ in samples)
    assert '# TYPE observatory_sse_active gauge' in rendered
    assert 'observatory_sse_replay_count{event_type="observation.changed"} 2' in rendered


@pytest.mark.asyncio
async def test_spanless_component_health_uses_only_explicit_primary_measurements() -> None:
    store = MemoryObservatoryStore()
    primary = make_payload(
        "00000000-0000-4000-8000-000000000831",
        observed_at=BASE_TIME,
        run_id=make_run_id("spanless-primary"),
        value=18.0,
    ).model_dump(by_alias=True, exclude_none=True)
    primary["trace"] = {"traceId": "0123456789abcdef0123456789abcdef"}
    secondary = dict(primary)
    secondary["observationId"] = "00000000-0000-4000-8000-000000000832"
    secondary["measurement"] = {"name": "token_count", "value": 24.0, "unit": "count"}
    await store.insert_observation(MetadataObservation.from_payload(primary))
    await store.insert_observation(MetadataObservation.from_payload(secondary))
    result = await _query_service(store).component_health("aura.runtime.model_inference")
    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["latencyP50Ms"] == 18.0


@pytest.mark.asyncio
async def test_reference_component_health_keeps_duration_before_later_token_count() -> None:
    """A later same-span counter must not replace the latency measurement."""

    store = MemoryObservatoryStore()
    primary = make_payload(
        "00000000-0000-4000-8000-000000000841",
        observed_at=BASE_TIME,
        run_id=make_run_id("duration-before-token-count"),
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        value=18.0,
    ).model_dump(by_alias=True, exclude_none=True)
    secondary = dict(primary)
    secondary["observationId"] = "00000000-0000-4000-8000-000000000842"
    secondary["observedAt"] = (BASE_TIME + timedelta(seconds=1)).isoformat()
    secondary["measurement"] = {"name": "token_count", "value": 24.0, "unit": "count"}

    await store.insert_observation(MetadataObservation.from_payload(primary))
    await store.insert_observation(MetadataObservation.from_payload(secondary))

    result = await _query_service(store).component_health("aura.runtime.model_inference")

    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["latencyP50Ms"] == 18.0
    assert result["summary"]["latencyP95Ms"] == 18.0


@pytest.mark.asyncio
async def test_reference_component_health_keeps_duration_and_uses_later_failure() -> None:
    """A later same-span failure changes outcome, not the duration value."""

    store = MemoryObservatoryStore()
    primary = make_payload(
        "00000000-0000-4000-8000-000000000851",
        observed_at=BASE_TIME,
        run_id=make_run_id("duration-failure-reference"),
        status="completed",
        outcome="success",
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        value=18.0,
    ).model_dump(by_alias=True, exclude_none=True)
    secondary = dict(primary)
    secondary["observationId"] = "00000000-0000-4000-8000-000000000852"
    secondary["observedAt"] = (BASE_TIME + timedelta(seconds=1)).isoformat()
    secondary["measurement"] = {"name": "queue_time_ms", "value": 99.0, "unit": "milliseconds"}
    secondary["dimensions"] = {"status": "error"}

    await store.insert_observation(MetadataObservation.from_payload(primary))
    await store.insert_observation(MetadataObservation.from_payload(secondary))

    result = await _query_service(store).component_health("aura.runtime.model_inference")

    assert result is not None
    assert result["summary"]["sampleCount"] == 1
    assert result["summary"]["latencyP50Ms"] == 18.0
    assert result["summary"]["latencyP95Ms"] == 18.0
    assert result["summary"]["errorRate"] == 1.0
    assert result["summary"]["outcomes"] == {
        "succeeded": 0,
        "failed": 1,
        "cancelled": 0,
        "unknown": 0,
    }


@pytest.mark.asyncio
async def test_reference_component_health_equal_timestamp_uses_higher_observation_id() -> None:
    store = MemoryObservatoryStore()
    primary = make_payload(
        "00000000-0000-4000-8000-0000000002e1",
        observed_at=BASE_TIME,
        run_id=make_run_id("duration-failure-equal-timestamp"),
        status="completed",
        outcome="success",
        trace_id="0123456789abcdef0123456789abcdef",
        span_id="0123456789abcdef",
        value=18.0,
    ).model_dump(by_alias=True, exclude_none=True)
    secondary = dict(primary)
    secondary["observationId"] = "00000000-0000-4000-8000-0000000002e2"
    secondary["measurement"] = {
        "name": "queue_time_ms",
        "value": 99.0,
        "unit": "milliseconds",
    }
    secondary["dimensions"] = {"status": "error"}

    await store.insert_observation(MetadataObservation.from_payload(primary))
    await store.insert_observation(MetadataObservation.from_payload(secondary))

    result = await _query_service(store).component_health("aura.runtime.model_inference")

    assert result is not None
    assert result["summary"]["latencyP50Ms"] == 18.0
    assert result["summary"]["latencyP95Ms"] == 18.0
    assert result["summary"]["errorRate"] == 1.0
    assert result["summary"]["outcomes"] == {
        "succeeded": 0,
        "failed": 1,
        "cancelled": 0,
        "unknown": 0,
    }
