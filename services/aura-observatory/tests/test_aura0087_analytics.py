"""AURA-0087 bounded analytics semantics and privacy regressions."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import httpx
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.contract import MetadataObservation
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.query import (
    AnalyticsRequest,
    ObservatoryQueryService,
    analytics_metric_catalogue,
    bounded_analytics_window,
    choose_analytics_bucket,
)
from aura_observatory.settings import Settings

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"


def _observation(
    index: int, value: float, at: datetime, *, component: str = "aura.test.component"
) -> MetadataObservation:
    payload = {
        "schemaVersion": 1,
        "observationId": f"00000000-0000-0000-0000-{index:012d}",
        "observedAt": at.isoformat().replace("+00:00", "Z"),
        "kind": "metric",
        "component": {"id": component, "version": "1.0.0"},
        "operation": "observe",
        "measurement": {"name": "duration_ms", "value": value, "unit": "milliseconds"},
        "dimensions": {"provider": "local", "outcome": "success"},
        "correlation": {"runId": "00000000-0000-0000-0000-000000000001"},
        "deployment": {
            "deploymentId": "dev",
            "gitCommit": "abcdef1",
            "environment": "test",
            "hostId": "host",
        },
        "serviceRole": "api",
        "capturePolicy": "metadata_only",
    }
    return MetadataObservation.from_payload(payload)


@pytest.mark.asyncio
async def test_reference_analytics_has_stable_percentiles_units_and_group_facets() -> None:
    store = MemoryObservatoryStore()
    base = datetime.now(UTC) - timedelta(minutes=5)
    for index, value in enumerate((10.0, 20.0, 30.0), start=1):
        await store.insert_observation(_observation(index, value, base + timedelta(seconds=index)))
    result = await ObservatoryQueryService(store).analytics(
        AnalyticsRequest(
            metric="operation_duration_ms",
            unit="milliseconds",
            aggregation="p95",
            group_by="provider",
            from_time=base - timedelta(seconds=1),
            to_time=base + timedelta(minutes=1),
        )
    )
    series = cast(list[dict[str, Any]], result["series"])
    assert series[0]["facets"] == {"provider": "local"}
    assert series[0]["summary"]["sampleCount"] == 3
    assert series[0]["summary"]["p50"] == 20.0
    assert series[0]["summary"]["p95"] == 30.0
    assert result["percentileMethod"] == "clickhouse_quantile_exact"
    assert result["capturePolicy"] == "metadata_only"


@pytest.mark.asyncio
async def test_analytics_rejects_mixed_units_and_arbitrary_filters() -> None:
    service = ObservatoryQueryService(MemoryObservatoryStore())
    with pytest.raises(ValueError, match="unit"):
        await service.analytics(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="count",
            )
        )
    with pytest.raises(ValueError, match="filter"):
        await service.analytics(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                filters={"prompt": "never"},
            )
        )
    with pytest.raises(ValueError, match="filter"):
        await service.analytics(
            AnalyticsRequest(
                metric="operation_duration_ms",
                unit="milliseconds",
                filters={"component": "aura.test' OR 1=1"},
            )
        )


def test_analytics_windows_and_buckets_are_finite_and_deterministic() -> None:
    now = datetime(2026, 10, 16, 12, tzinfo=UTC)
    lower, upper = bounded_analytics_window(None, None, now=now)
    assert upper - lower == timedelta(hours=24)
    assert choose_analytics_bucket("auto", lower, upper) == ("fifteen_minutes", 900)
    with pytest.raises(ValueError, match="too wide|invalid"):
        bounded_analytics_window(now - timedelta(days=32), now)
    with pytest.raises(ValueError, match="point"):
        choose_analytics_bucket("one_minute", now - timedelta(days=1), now)


def test_metric_catalogue_is_closed_and_exposes_bounds() -> None:
    catalogue = analytics_metric_catalogue(now=datetime(2026, 10, 16, tzinfo=UTC))
    items = cast(list[dict[str, Any]], catalogue["items"])
    limits = cast(dict[str, Any], catalogue["limits"])
    assert len(items) >= 40
    assert catalogue["capturePolicy"] == "metadata_only"
    assert limits["maximumScannedRows"] == 1_000_000
    assert all(
        set(item)
        == {
            "metric",
            "unit",
            "focusAreas",
            "supportedAggregations",
            "supportedGroupings",
            "availability",
            "unavailableSignals",
        }
        for item in items
    )


@pytest.mark.asyncio
async def test_analytics_api_is_owner_scoped_no_store_and_bounded() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get(
            "/api/v1/analytics/query",
            params={
                "metric": "operation_duration_ms",
                "unit": "milliseconds",
                "series_limit": "13",
            },
        )
        assert response.status_code == 422
        assert response.headers.get("cache-control") is None
        response = await client.get(
            "/api/v1/analytics/query",
            params={"metric": "operation_duration_ms", "unit": "milliseconds"},
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert "prompt" not in response.text
        assert "response" not in response.text
    unauthenticated = create_app(settings, query_store=MemoryObservatoryStore(), testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=unauthenticated), base_url=settings.public_origin
    ) as client:
        assert (await client.get("/api/v1/analytics/metrics")).status_code == 401


@pytest.mark.asyncio
async def test_analytics_api_preserves_scan_limit_partial_snapshot() -> None:
    class ScanLimitedStore(MemoryObservatoryStore):
        async def operational_analytics_query(
            self, request: AnalyticsRequest
        ) -> dict[str, object]:
            del request
            return {
                "snapshotAt": "2026-10-16T12:00:00Z",
                "series": [],
                "breakdowns": [],
                "freshness": {"state": "unavailable"},
                "partial": True,
                "partialReasons": ["scan_limit"],
                "unavailableSignals": ["storage"],
                "scan": {"scannedRows": 1_000_000, "scanLimit": 1_000_000},
                "capturePolicy": "metadata_only",
            }

    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(
        settings,
        auth=auth,
        query_store=ScanLimitedStore(),
        testing=True,
    )
    app.state.query.query_port = app.state.query.store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get(
            "/api/v1/analytics/query",
            params={"metric": "operation_duration_ms", "unit": "milliseconds"},
        )
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert payload["partial"] is True
    assert payload["partialReasons"] == ["scan_limit"]
    assert payload["scan"]["scannedRows"] == 1_000_000
    assert "prompt" not in response.text
    assert "response" not in response.text
