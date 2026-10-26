"""Focused AURA-0123 checks for bounded query-stream invalidation identity."""

from __future__ import annotations

import asyncio
import re
from typing import Any

import pytest
from aura_observatory.adapters.query import ProductionObservatoryStore
from aura_observatory.entrypoints.api.app import _query_live_event_payload


RUN_ID = "11111111-1111-4111-8111-111111111111"
OBSERVATION_ID = "22222222-2222-4222-8222-222222222222"
EVENT_ID = "33333333-3333-4333-8333-333333333333"
LIVE_EVENT_KEYS = {
    "schemaVersion",
    "eventId",
    "eventType",
    "occurredAt",
    "resourceType",
    "resourceId",
    "capturePolicy",
}


def test_run_correlated_observation_invalidates_the_run() -> None:
    payload = _query_live_event_payload(
        "observation.changed",
        {"observationId": OBSERVATION_ID, "runId": RUN_ID, "traceId": "ignored"},
        event_id=EVENT_ID,
    )

    assert payload["resourceType"] == "run"
    assert payload["resourceId"] == RUN_ID
    assert payload["eventId"] == EVENT_ID


def test_missing_or_invalid_run_correlation_keeps_observation_identity() -> None:
    for data in (
        {"observationId": OBSERVATION_ID},
        {"observationId": OBSERVATION_ID, "runId": "not-a-uuid"},
        {"observationId": OBSERVATION_ID, "runId": None},
    ):
        payload = _query_live_event_payload("observation.changed", data, event_id=EVENT_ID)

        assert payload["resourceType"] == "observation"
        assert payload["resourceId"] == OBSERVATION_ID


def test_query_live_event_shape_remains_closed_and_content_free() -> None:
    payload = _query_live_event_payload(
        "observation.changed",
        {
            "observationId": OBSERVATION_ID,
            "runId": RUN_ID,
            "traceId": "0123456789abcdef0123456789abcdef",
            "prompt": "must not cross the live-event boundary",
            "response": "must not cross the live-event boundary",
        },
        event_id=EVENT_ID,
    )

    assert set(payload) == LIVE_EVENT_KEYS
    assert payload["capturePolicy"] == "metadata_only"
    rendered = str(payload)
    assert "prompt" not in rendered
    assert "response" not in rendered


def test_health_events_keep_system_identity() -> None:
    payload = _query_live_event_payload("health.changed", {}, event_id=EVENT_ID)

    assert payload["resourceType"] == "system"
    assert payload["resourceId"] is None
    assert set(payload) == LIVE_EVENT_KEYS


@pytest.mark.asyncio
async def test_production_query_projection_excludes_self_observation_from_run_refresh() -> None:
    """Exercise the adapter projection before the closed SSE renderer."""

    store = object.__new__(ProductionObservatoryStore)
    rows = [
        {
            "observation_id": OBSERVATION_ID,
            "run_id": RUN_ID,
            "trace_id": "0123456789abcdef0123456789abcdef",
            "component_id": "aura.runtime.model_inference",
            "ingest_sequence": 1,
        },
        {
            "observation_id": "44444444-4444-4444-8444-444444444444",
            "run_id": RUN_ID,
            "trace_id": "0123456789abcdef0123456789abcdef",
            "component_id": "aura.observatory.run_inspection_query",
            "ingest_sequence": 2,
        },
    ]

    async def counts() -> dict[str, int]:
        return {"latest_sequence": len(rows)}

    async def query(statement: str) -> list[dict[str, Any]]:
        assert "component_id" in statement
        match = re.search(r"ingest_sequence > (\d+) AND ingest_sequence <= (\d+)", statement)
        assert match is not None
        lower, upper = int(match.group(1)), int(match.group(2))
        return [row for row in rows if lower < row["ingest_sequence"] <= upper]

    class HealthEvents:
        async def latest_health_event_sequence(self) -> int:
            return 0

        async def health_event_sequence(self, _event_id: str) -> None:
            return None

        async def health_events_after(
            self, _event_id: str | None, _upper: int
        ) -> list[dict[str, object]]:
            return []

    store._overview_counts = counts  # type: ignore[attr-defined]
    store._query_rows = query  # type: ignore[attr-defined]
    store._health_port = HealthEvents()  # type: ignore[attr-defined]

    stream = store.events_after_query(None, snapshot_sequence=0)
    events = [await asyncio.wait_for(anext(stream), timeout=1.0) for _ in rows]
    await stream.aclose()

    external = _query_live_event_payload(
        events[0]["type"], events[0]["data"], event_id=events[0]["id"]
    )
    self_observation = _query_live_event_payload(
        events[1]["type"], events[1]["data"], event_id=events[1]["id"]
    )
    assert external["resourceType"] == "run"
    assert external["resourceId"] == RUN_ID
    assert self_observation["resourceType"] == "observation"
    assert self_observation["resourceId"] == rows[1]["observation_id"]
    assert set(external) == LIVE_EVENT_KEYS
    assert set(self_observation) == LIVE_EVENT_KEYS
    assert "componentId" not in external
    assert "componentId" not in self_observation
