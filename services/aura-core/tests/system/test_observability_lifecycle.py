"""Lifecycle semantics for metadata-only Core observations."""

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from conftest import owner_client


def _assert_parented_stream_failure(records: tuple[Any, ...], operation: str) -> None:
    measurements = list(records)
    spans = [item for item in measurements if item.kind == "span"]
    failures = [
        item
        for item in spans
        if dict(item.trace_attributes).get("operation") == operation
        and dict(item.dimensions).get("outcome") == "error"
    ]
    assert len(failures) == 1
    failure = failures[0]
    roots = [
        item
        for item in spans
        if item.trace_id == failure.trace_id
        and dict(item.trace_attributes).get("operation") == "sse.connect"
        and item.parent_span_id is None
    ]
    assert len(roots) == 1
    assert failure.parent_span_id == roots[0].span_id
    assert failure.value > 0
    assert "private" not in repr(failure)


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
        second_response = await client.get(
            f"/api/v1/runs/{run_id}/events",
            headers={"Last-Event-ID": str(history[0].event_id)},
        )
        assert second_response.status_code == 200

        records = api_app.state.aura.metrics.snapshot()
        reconnects = [item for item in records if item.metric == "sse_reconnects"]
        deliveries = [item for item in records if item.metric == "events_delivered"]
        spans = [
            item
            for item in records
            if item.kind == "span"
            and item.component_id == "aura.runtime.stream_delivery"
        ]
        assert len(reconnects) == 2
        assert deliveries
        assert any(dict(item.trace_attributes)["operation"] == "sse.reconnect" for item in spans)
        assert any(dict(item.trace_attributes)["operation"] == "sse.deliver" for item in spans)
        trace_ids = {item.trace_id for item in spans}
        assert len(trace_ids) == 2
        assert all(request_trace_id != run_id.hex for request_trace_id in trace_ids)
        assert all(
            len(request_trace_id) == 32
            and all(character in "0123456789abcdef" for character in request_trace_id)
            for request_trace_id in trace_ids
        )
        connections = [
            item
            for item in spans
            if dict(item.trace_attributes)["operation"] == "sse.connect"
        ]
        assert len(connections) == 2
        assert all(connection.parent_span_id is None for connection in connections)
        assert len({connection.trace_id for connection in connections}) == 2
        for connection in connections:
            assert all(
                item.parent_span_id == connection.span_id
                for item in spans
                if item.trace_id == connection.trace_id and item is not connection
            )
        assert all(
            dict(item.trace_attributes)["conversation_id"] == str(conversation_id)
            for item in spans
            if dict(item.trace_attributes)["operation"] != "sse.connect"
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


@pytest.mark.asyncio
async def test_reconciliation_failure_has_authorized_request_root_and_no_content(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "observe reconcile failure", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])

        async def fail_reconciliation(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError("private reconciliation details")

        api_app.state.aura.store.get_run_memory_activity = fail_reconciliation  # type: ignore[attr-defined]
        with pytest.raises(RuntimeError, match="private reconciliation details"):
            await client.get(f"/api/v1/runs/{run_id}/events")

        _assert_parented_stream_failure(
            api_app.state.aura.metrics.snapshot(), "memory.activity.reconcile"
        )
        assert api_app.state.aura.metrics.stats().rejected == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_event_history_failure_has_parented_replay_error_distinct_from_reconcile(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "observe replay failure", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])

        async def fail_event_history(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError("private event-history details")

        api_app.state.aura.sql_store = SimpleNamespace(event_history=fail_event_history)
        with pytest.raises(RuntimeError, match="private event-history details"):
            await client.get(f"/api/v1/runs/{run_id}/events")

        records = api_app.state.aura.metrics.snapshot()
        _assert_parented_stream_failure(records, "memory.activity.replay")
        assert any(
            item.kind == "span"
            and dict(item.trace_attributes).get("operation") == "memory.activity.reconcile"
            for item in records
        )
        assert api_app.state.aura.metrics.stats().rejected == 0
        assert "private event-history details" not in repr(records)
    finally:
        await client.aclose()
