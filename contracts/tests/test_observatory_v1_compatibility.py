"""Backward-compatibility checks for additive AURA-0095 OpenAPI changes."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parents[2]
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"


def _document() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_pre_aura0095_operations_and_version_remain_stable() -> None:
    document = _document()
    assert document["info"]["version"] == "1.0.0"
    expected = {
        "/api/v1/auth/login": {"get": "startOwnerLogin"},
        "/api/v1/auth/me": {"get": "getOwnerSession"},
        "/api/v1/auth/logout": {"post": "logoutOwnerSession"},
        "/api/v1/overview": {"get": "getOverview"},
        "/api/v1/health/history": {"get": "listHealthHistory"},
        "/api/v1/runs": {"get": "listRuns"},
        "/api/v1/runs/{run_id}": {"get": "getRun"},
        "/api/v1/traces/{trace_id}": {"get": "getTrace"},
        "/api/v1/components": {"get": "listComponents"},
        "/api/v1/analytics/metrics": {"get": "listAnalyticsMetrics"},
        "/api/v1/analytics/query": {"get": "queryOperationalAnalytics"},
        "/api/v1/run-comparisons/query": {"post": "compareRuns"},
        "/api/v1/deployments": {"get": "listDeployments"},
        "/api/v1/events": {"get": "streamEvents"},
        "/api/v1/retention/actions": {"post": "createRetentionAction"},
        "/api/v1/retention/actions/{action_id}": {"get": "getRetentionAction"},
    }
    for path, methods in expected.items():
        assert path in document["paths"]
        for method, operation_id in methods.items():
            assert document["paths"][path][method]["operationId"] == operation_id


def test_existing_sse_event_values_are_preserved() -> None:
    document = _document()
    event_types = document["components"]["schemas"]["LiveEventType"]["enum"]
    assert event_types == [
        "observation.changed",
        "retention.changed",
        "health.changed",
        "heartbeat",
    ]
    assert document["components"]["schemas"]["SsePayload"]["oneOf"] == [
        {"$ref": "#/components/schemas/Overview"},
        {"$ref": "#/components/schemas/LiveEvent"},
    ]
    operational = document["paths"]["/api/v1/events"]["get"]
    assert operational["operationId"] == "streamEvents"
    assert operational["responses"]["200"]["content"]["text/event-stream"]["schema"] == {
        "$ref": "#/components/schemas/SsePayload"
    }

    evaluation = document["components"]["schemas"]["EvaluationLiveEvent"]
    assert evaluation["properties"]["eventType"]["const"] == "evaluation.changed"
    evaluation_stream = document["paths"]["/api/v1/evaluation/events"]["get"]
    assert evaluation_stream["operationId"] == "streamEvaluationEvents"
    assert evaluation_stream["parameters"] == [
        {"$ref": "#/components/parameters/LastEventId"}
    ]
    assert evaluation_stream["responses"]["200"]["content"]["text/event-stream"][
        "schema"
    ] == {"$ref": "#/components/schemas/EvaluationLiveEvent"}


def test_new_paths_are_additive_under_evaluation_namespace() -> None:
    paths = _document()["paths"]
    new_paths = [path for path in paths if path.startswith("/api/v1/evaluation/")]
    assert new_paths
    assert all(path.startswith("/api/v1/evaluation/") for path in new_paths)
    operation_ids = [
        operation["operationId"]
        for path_item in paths.values()
        for method, operation in path_item.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]
    assert len(operation_ids) == len(set(operation_ids))
