"""AURA-0111 canonical UTC instants at the production query boundary."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import base64
import json
from collections.abc import AsyncGenerator, Mapping
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import pytest
from aura_observatory.adapters import evaluation_query as evaluation_query_adapter
from aura_observatory.adapters.evaluation_query import (
    PostgresEvaluationQueryBackend,
    _canonical_cursor_instant,
    _decode_storage_cursor,
    _encode_storage_cursor,
)
from aura_observatory.evaluation_query import EvaluationQueryError


def _payload(cursor: str) -> dict[str, object]:
    return json.loads(base64.urlsafe_b64decode(cursor[4:] + "===").decode())


def _cursor(payload: Mapping[str, object], *, prefix: str = "ev2_") -> str:
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    return prefix + encoded


def test_cursor_instants_normalize_offsets_and_preserve_fractional_precision() -> None:
    value = datetime(2026, 10, 24, 12, 34, 56, 123456, tzinfo=timezone(timedelta(hours=2)))
    assert _canonical_cursor_instant(value) == "2026-10-24T10:34:56.123456Z"
    assert _canonical_cursor_instant("2026-10-24 12:34:56.123456+02:00") == (
        "2026-10-24T10:34:56.123456Z"
    )


@pytest.mark.parametrize(
    "value",
    [
        datetime(2026, 10, 24, 12, 34, 56),
        "2026-10-24T12:34:56",
        "not-a-timestamp",
        123,
    ],
)
def test_cursor_instants_reject_naive_malformed_and_non_string_values(value: object) -> None:
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        _canonical_cursor_instant(value)


@pytest.mark.parametrize("kind", ["jobs", "history", "scores"])
def test_new_collection_history_and_score_cursors_are_canonical(kind: str) -> None:
    snapshot = datetime(2026, 10, 24, 12, 0, tzinfo=timezone(timedelta(hours=2)))
    timestamp = "2026-10-24 11:59:59.987654+02:00"
    identifiers = {
        "jobs": "job-1",
        "history": "history-1",
        "scores": "00000000-0000-4000-8000-000000000001",
    }
    row_key = {"jobs": "createdAt", "history": "occurredAt", "scores": "createdAt"}[kind]
    cursor = _encode_storage_cursor(
        {"snapshotAt": snapshot},
        {
            row_key: timestamp,
            "jobId": identifiers["jobs"],
            "historyId": identifiers["history"],
            "scoreId": identifiers["scores"],
        },
        kind,
    )
    payload = _payload(cursor)
    assert payload["snapshotAt"] == "2026-10-24T10:00:00Z"
    assert payload["timestamp"] == "2026-10-24T09:59:59.987654Z"
    assert " " not in str(payload["snapshotAt"])
    assert " " not in str(payload["timestamp"])


def test_legacy_history_cursor_decodes_and_successor_is_canonical() -> None:
    legacy = _cursor(
        {
            "v": 2,
            "snapshotAt": "2026-10-24 12:00:00+02:00",
            "timestamp": "2026-10-24 11:59:59.100000+02:00",
            "identifier": "history-1",
        },
        prefix="ev1.",
    )
    state = _decode_storage_cursor(legacy, expected_kind="history")
    assert state["snapshotAt"] == datetime(2026, 10, 24, 10, 0, tzinfo=UTC)
    assert state["timestamp"] == "2026-10-24T09:59:59.100000Z"
    successor = _encode_storage_cursor(
        state,
        {"occurredAt": "2026-10-24 11:58:00+02:00", "historyId": "history-2"},
        "history",
    )
    assert _payload(successor)["snapshotAt"] == "2026-10-24T10:00:00Z"
    assert _payload(successor)["timestamp"] == "2026-10-24T09:58:00Z"


@pytest.mark.asyncio
async def test_invalid_cursor_timestamp_is_rejected_before_provider_access() -> None:
    calls = 0

    class _Connection:
        async def execute(self, _sql: str, _params: tuple[object, ...]) -> Any:
            raise AssertionError("provider access must not occur")

    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[_Connection]:
        nonlocal calls
        calls += 1
        yield _Connection()

    backend = PostgresEvaluationQueryBackend(connection_factory)
    payload = {
        "v": 2,
        "kind": "jobs",
        "snapshotAt": "2026-10-24T10:00:00Z",
        "timestamp": "2026-10-24T09:59:59",
        "identifier": "job-1",
        "version": None,
    }
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection("jobs", cursor=_cursor(payload), limit=1, filters=None)
    assert calls == 0


def _score_row(score_id: str, observed_at: str) -> dict[str, object]:
    dataset_id = "00000000-0000-4000-8000-000000000101"
    suite_id = "00000000-0000-4000-8000-000000000102"
    evaluator_id = "00000000-0000-4000-8000-000000000103"
    component_id = "aura.test.evaluator"
    digest = "a" * 64
    provenance = {
        "dataset": {"id": dataset_id, "version": "1.0.0"},
        "suite": {"id": suite_id, "version": "1.0.0"},
        "evaluators": [{
            "evaluatorId": evaluator_id,
            "evaluatorVersion": "1.0.0",
            "kind": "deterministic",
        }],
        "components": [{
            "componentId": component_id,
            "componentVersion": "1.0.0",
            "codeRevision": "revision-1",
            "imageDigest": digest,
            "configurationRevision": "configuration-1",
        }],
        "model": {"modelId": "model-1", "modelVersion": "1.0.0", "modelDigest": digest},
        "deploymentRevision": "deployment-1",
        "reproducibility": {
            "seed": 1,
            "clockMode": "fixed",
            "timeZone": "UTC",
            "locale": "en-US",
            "fixtureRevision": "fixture-1",
            "promptRevision": "prompt-1",
            "policyRevision": "policy-1",
        },
    }
    return {
        "score_id": score_id,
        "result_id": str(uuid4()),
        "case_id": str(uuid4()),
        "evaluator_id": evaluator_id,
        "evaluator_version": "1.0.0",
        "evaluator_kind": "deterministic",
        "component_id": component_id,
        "component_version": "1.0.0",
        "code_revision": "revision-1",
        "image_digest": digest,
        "configuration_revision": "configuration-1",
        "provenance_component": component_id,
        "provenance_version": "1.0.0",
        "provenance_digest": digest,
        "execution_provenance_json": json.dumps(provenance),
        "metric": "accuracy",
        "score_value": 1.0,
        "outcome": "unchanged",
        "evidence_status": "complete",
        "uncertainty": None,
        "score_disposition": "accepted",
        "observed_at": observed_at,
        "reproducibility": "fixed",
    }


@pytest.mark.asyncio
async def test_clickhouse_score_pages_normalize_provider_time_and_continue_without_skips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_id = str(uuid4())
    second_id = str(uuid4())
    responses = [
        [_score_row(first_id, "2026-10-18 12:00:00.123")],
        [_score_row(second_id, "2026-10-18 11:59:59.999")],
    ]
    requests: list[dict[str, Any]] = []

    class _Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict[str, object]:
            return {"data": responses[len(requests) - 1]}

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            pass

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str, **kwargs: object) -> _Response:
            requests.append(kwargs)
            return _Response()

    monkeypatch.setattr(evaluation_query_adapter.httpx, "AsyncClient", _Client)
    backend = PostgresEvaluationQueryBackend(
        lambda: None,
        clickhouse_url="http://clickhouse.invalid",
    )

    first_page = await backend.collection("scores", cursor=None, limit=1, filters=None)
    first_item = first_page["items"][0]
    assert isinstance(first_item, dict)
    assert first_item["scoreId"] == first_id
    assert first_item["createdAt"] == "2026-10-18T12:00:00.123000Z"
    first_cursor = first_page["nextCursor"]
    assert isinstance(first_cursor, str)
    first_cursor_payload = _payload(first_cursor)
    assert first_cursor_payload["timestamp"] == "2026-10-18T12:00:00.123000Z"

    second_page = await backend.collection("scores", cursor=first_cursor, limit=1, filters=None)
    second_item = second_page["items"][0]
    assert isinstance(second_item, dict)
    assert second_item["scoreId"] == second_id
    assert second_item["scoreId"] != first_item["scoreId"]
    assert [first_item["scoreId"], second_item["scoreId"]] == [first_id, second_id]
    assert requests[1]["params"]["param_cursor_timestamp"] == "2026-10-18 12:00:00.123000"
    assert requests[1]["params"]["param_cursor_id"] == first_id
