"""AURA-0085 bounded ingest health and self-monitoring checks."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from aura_observatory.adapters.memory import MemoryObservatoryStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.metrics import (
    BoundedLatencyWindow,
    HealthState,
    IngestHealth,
    bounded_lag_ms,
)
from aura_observatory.settings import Settings

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"


def test_health_bounds_future_lag_and_latency_reservoir() -> None:
    assert bounded_lag_ms(-5) == 0
    assert bounded_lag_ms(float("inf")) is None
    assert bounded_lag_ms(999_999_999_999) == 604_800_000
    window = BoundedLatencyWindow(max_samples=2)
    window.add(1)
    window.add(2)
    window.add(3)
    assert window.summary().sample_count == 2
    assert window.summary().p50_ms == 2.5


def test_latency_summary_latest_is_last_inserted_retained_sample() -> None:
    window = BoundedLatencyWindow(max_samples=3)
    for value in (90, 20, 10):
        window.add(value)
    summary = window.summary()
    assert summary.latest_ms == 10
    assert summary.p50_ms == 20
    assert summary.p95_ms == 83

    rollover = BoundedLatencyWindow()
    rollover.add(90)
    for value in range(1, 128):
        rollover.add(value)
    rollover.add(10)
    rolled = rollover.summary()
    assert rolled.sample_count == 128
    assert rolled.latest_ms == 10
    assert rolled.p95_ms == 120.65


def test_health_snapshot_uses_explicit_states_and_finite_reasons() -> None:
    health = IngestHealth()
    health.heartbeat(datetime.now(UTC))
    health.rejection("not-an-accepted-reason")
    snapshot = health.snapshot()
    assert snapshot.state is HealthState.DEGRADED
    assert snapshot.as_dict()["rejectionReasons"] == [{"reason": "malformed", "count": 1}]
    assert "payload" not in str(snapshot.as_dict()).lower()


@pytest.mark.asyncio
async def test_owner_health_history_is_bounded_and_metadata_only() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    store = MemoryObservatoryStore()
    app = create_app(settings, auth=auth, query_store=store, testing=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    )
    async with client:
        overview = await client.get("/api/v1/overview")
        assert overview.status_code == 200
        assert overview.json()["health"]["state"] in {
            "healthy",
            "degraded",
            "partial",
            "delayed",
            "unavailable",
        }
        history = await client.get("/api/v1/health/history", params={"limit": "1"})
        assert history.status_code == 200
        assert len(history.json()["items"]) <= 1
        assert not any(
            forbidden in history.text.lower()
            for forbidden in ("prompt", "response", "credential", "bearer", "exception")
        )


@pytest.mark.asyncio
async def test_latency_latest_round_trips_through_overview_and_history() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    store = MemoryObservatoryStore()
    for value in (90, 20, 10):
        await store.record_latency("storage", value)
    snapshot = await store.health_snapshot()
    await store.save_health_snapshot(snapshot)
    app = create_app(settings, auth=auth, query_store=store, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        overview = await client.get("/api/v1/overview")
        history = await client.get("/api/v1/health/history", params={"limit": "1"})
    assert overview.status_code == 200
    assert overview.json()["health"]["storageLatency"]["latestMs"] == 10
    assert history.status_code == 200
    assert history.json()["items"][0]["storageLatency"]["latestMs"] == 10


@pytest.mark.asyncio
async def test_health_history_rejects_unbounded_page_sizes() -> None:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _ = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, query_store=MemoryObservatoryStore(), testing=True)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://observatory.localhost:4300",
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.get("/api/v1/health/history", params={"limit": "201"})
    assert response.status_code == 400
