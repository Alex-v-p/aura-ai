"""Lifecycle semantics for metadata-only Core observations."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from conftest import owner_client


@pytest.mark.asyncio
async def test_sse_reconnect_and_delivery_emit_parented_component_spans(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "observe stream", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])
        conversation_id = UUID(created.json()["conversation"]["id"])
        await api_app.state.aura.coordinator.execute(run_id, api_app.state.aura.provider)
        history = await api_app.state.aura.publisher.history(run_id)
        assert history

        response = await client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(history[0].event_id)},
        )
        assert response.status_code == 200

        records = api_app.state.aura.metrics.snapshot()
        reconnects = [item for item in records if item.metric == "sse_reconnects"]
        deliveries = [item for item in records if item.metric == "events_delivered"]
        spans = [
            item
            for item in records
            if item.kind == "span"
            and item.component_id == "aura.runtime.stream_delivery"
        ]
        assert len(reconnects) == 1
        assert deliveries
        assert any(dict(item.trace_attributes)["operation"] == "sse.reconnect" for item in spans)
        assert any(dict(item.trace_attributes)["operation"] == "sse.deliver" for item in spans)
        assert all(item.trace_id == run_id.hex for item in spans)
        connections = [
            item
            for item in spans
            if dict(item.trace_attributes)["operation"] == "sse.connect"
        ]
        assert len(connections) == 1
        assert connections[0].parent_span_id is None
        assert all(
            item.parent_span_id == connections[0].span_id
            for item in spans
            if item is not connections[0]
        )
        assert all(
            dict(item.trace_attributes)["conversation_id"] == str(conversation_id)
            for item in spans
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_and_retry_idempotency_keep_trace_linked_observations(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "observe commands", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])
        cancel_headers = {
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": str(uuid4()),
        }
        first_cancel = await client.post(f"/api/v1/runs/{run_id}/cancel", headers=cancel_headers)
        replayed_cancel = await client.post(
            f"/api/v1/runs/{run_id}/cancel", headers=cancel_headers
        )
        assert first_cancel.status_code == replayed_cancel.status_code == 202
        assert first_cancel.json()["id"] == replayed_cancel.json()["id"] == str(run_id)

        await api_app.state.aura.coordinator.execute(run_id, api_app.state.aura.provider)
        retry_headers = {
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": str(uuid4()),
        }
        first_retry = await client.post(f"/api/v1/runs/{run_id}/retry", headers=retry_headers)
        replayed_retry = await client.post(
            f"/api/v1/runs/{run_id}/retry", headers=retry_headers
        )
        assert first_retry.status_code == replayed_retry.status_code == 202
        retry_id = first_retry.json()["run"]["id"]
        assert replayed_retry.json()["run"]["id"] == retry_id

        spans = [item for item in api_app.state.aura.metrics.snapshot() if item.kind == "span"]
        cancel_spans = [
            item for item in spans if dict(item.trace_attributes).get("operation") == "run.cancel"
        ]
        retry_spans = [
            item for item in spans if dict(item.trace_attributes).get("operation") == "run.retry"
        ]
        assert len(cancel_spans) == 2
        assert len(retry_spans) == 2
        assert len({item.span_id for item in cancel_spans}) == 2
        assert len({item.span_id for item in retry_spans}) == 2
        assert all(item.trace_id == run_id.hex for item in cancel_spans)
        assert all(item.trace_id == UUID(retry_id).hex for item in retry_spans)
        assert all(item.parent_span_id is None for item in cancel_spans + retry_spans)
    finally:
        await client.aclose()
