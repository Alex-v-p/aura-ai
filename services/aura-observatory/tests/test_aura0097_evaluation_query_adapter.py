"""Production evaluation-query adapter coverage for regression searches."""
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import base64
import json
import re
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

import pytest
from aura_observatory.adapters.evaluation_query import PostgresEvaluationQueryBackend
from aura_observatory.alerting import AlertError, _condition
from aura_observatory.dashboards import DashboardError, _panel
from aura_observatory.evaluation_query import EvaluationQueryError
from aura_observatory.saved_queries import (
    SavedQueryError,
    SavedQueryExecutionService,
    canonical_definition,
    decode_cursor,
    decode_execution_cursor,
    encode_cursor,
    encode_execution_cursor,
    normalize_definition,
)


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self._rows


class _Connection:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, params: tuple[object, ...]) -> _Result:
        self.calls.append((sql, params))
        return _Result(self.rows)


def _resource(experiment_id: str, experiment_version: str) -> dict[str, object]:
    return {
        "regressionId": str(uuid4()),
        "regressionVersion": experiment_version,
        "experiment": {"resourceId": experiment_id, "resourceVersion": experiment_version},
        "baseline": {"resourceId": str(uuid4()), "resourceVersion": "1.0.0"},
        "decision": "unchanged",
        "createdAt": "2026-10-21T10:00:00Z",
    }


@pytest.mark.asyncio
async def test_regression_collection_filters_window_outcome_and_exact_experiment_reference(
) -> None:
    experiment_id = str(uuid4())
    baseline_id = str(uuid4())
    regression = _resource(experiment_id, "2.0.0")
    connection = _Connection(
        [
            (
                regression,
                regression["regressionId"],
                regression["regressionVersion"],
                regression["createdAt"],
            )
        ]
    )

    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[Any]:
        yield connection

    backend = PostgresEvaluationQueryBackend(connection_factory)
    page = await backend.collection(
        "regressions",
        cursor=None,
        limit=20,
        filters={
            "from": "2026-10-21T09:00:00Z",
            "to": "2026-10-21T11:00:00Z",
            "outcomes": ["regressed", "unchanged"],
            # A baseline identity is deliberately supplied as a separate
            # value; the provider must never use it as an experiment match.
            "experimentReferences": [
                {"resourceId": experiment_id, "resourceVersion": "2.0.0"},
                {"resourceId": baseline_id, "resourceVersion": "1.0.0"},
            ],
        },
    )

    assert page["items"] == [
        {key: value for key, value in regression.items() if not key.startswith("_")}
    ]
    sql, params = connection.calls[0]
    assert "regression.created_at >= %s" in sql
    assert "regression.created_at <= %s" in sql
    assert "regression.decision = ANY(%s::text[])" in sql
    assert "regression.experiment_id = %s" in sql
    assert "regression.experiment_version = %s" in sql
    assert "regression.baseline_id" not in sql
    assert params[0] != params[1]
    assert params[2] == ["regressed", "unchanged"]
    assert experiment_id in params
    assert baseline_id in params


@pytest.mark.asyncio
async def test_regression_collection_requires_utc_window_before_storage_access() -> None:
    calls = 0

    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[Any]:
        nonlocal calls
        calls += 1
        yield _Connection([])

    backend = PostgresEvaluationQueryBackend(connection_factory)
    with pytest.raises(EvaluationQueryError, match="window is required"):
        await backend.collection(
            "regressions",
            cursor=None,
            limit=20,
            filters={"from": "2026-10-21T09:00:00Z"},
        )
    assert calls == 0


@pytest.mark.asyncio
async def test_saved_regression_reference_filter_never_matches_baseline_side() -> None:
    experiment_id = str(uuid4())

    class _EvaluationPort:
        async def collection_async(
            self,
            kind: str,
            *,
            cursor: str | None,
            limit: int,
            filters: dict[str, object],
        ) -> dict[str, object]:
            assert kind == "regressions"
            assert cursor is None
            assert limit == 10
            assert filters["experimentReferences"] == [
                {"resourceId": experiment_id, "resourceVersion": "1.0.0"}
            ]
            return {
                "items": [
                    {
                        "baseline": {
                            "resourceId": experiment_id,
                            "resourceVersion": "1.0.0",
                        }
                    },
                    {
                        "experiment": {
                            "resourceId": experiment_id,
                            "resourceVersion": "1.0.0",
                        }
                    },
                ],
                "nextCursor": None,
                "snapshotAt": "2026-10-21T10:00:00Z",
                "partial": False,
            }

    service = SavedQueryExecutionService(object(), evaluation_port=_EvaluationPort())
    definition = canonical_definition(
        {
            "kind": "regression_search",
            "window": {"kind": "last_hour", "from": None, "to": None},
            "outcomes": [],
            "experimentReferences": [
                {"resourceId": experiment_id, "resourceVersion": "1.0.0"}
            ],
            "pageSize": 10,
        }
    )
    result = await service._execute_definition(definition, None)
    assert isinstance(result, dict)
    assert result["items"] == [
        {
            "experiment": {
                "resourceId": experiment_id,
                "resourceVersion": "1.0.0",
            }
        }
    ]


@pytest.mark.asyncio
async def test_regression_cursor_replays_same_timestamp_and_id_by_version() -> None:
    experiment_id = str(uuid4())
    first = _resource(experiment_id, "2.0.0")
    second = dict(first)
    second["regressionVersion"] = "1.0.0"
    rows = [
        (
            first,
            first["regressionId"],
            first["regressionVersion"],
            first["createdAt"],
        )
    ]
    connection = _Connection(rows)

    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[Any]:
        yield connection

    backend = PostgresEvaluationQueryBackend(connection_factory)
    filters = {
        "from": "2026-10-21T09:00:00Z",
        "to": "2026-10-21T11:00:00Z",
        "outcomes": ["unchanged"],
    }
    first_page = await backend.collection("regressions", cursor=None, limit=1, filters=filters)
    cursor = first_page["nextCursor"]
    assert isinstance(cursor, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
    assert len(cursor) <= 1024
    assert cursor.startswith("ev2_")
    payload = json.loads(base64.urlsafe_b64decode(cursor[4:] + "===").decode())
    assert payload["kind"] == "regressions"
    assert payload["identifier"] == first["regressionId"]
    assert payload["version"] == "2.0.0"
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection("jobs", cursor=cursor, limit=1, filters=None)

    connection.rows = [
        (
            second,
            second["regressionId"],
            second["regressionVersion"],
            second["createdAt"],
        )
    ]
    await backend.collection("regressions", cursor=cursor, limit=1, filters=filters)
    continuation_sql, continuation_params = connection.calls[-1]
    assert "regression.regression_version < %s" in continuation_sql
    assert continuation_params[-2] == "2.0.0"


@pytest.mark.asyncio
async def test_regression_cursor_replays_max_length_version_and_accepts_legacy_prefix() -> None:
    experiment_id = str(uuid4())
    max_version = "v" + "x" * 63
    older_version = "w" + "y" * 63
    first = _resource(experiment_id, max_version)
    second = dict(first)
    second["regressionVersion"] = older_version
    rows = [
        (
            first,
            first["regressionId"],
            first["regressionVersion"],
            first["createdAt"],
        )
    ]
    connection = _Connection(rows)

    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[Any]:
        yield connection

    backend = PostgresEvaluationQueryBackend(connection_factory)
    filters = {
        "from": "2026-10-21T09:00:00Z",
        "to": "2026-10-21T11:00:00Z",
        "outcomes": ["unchanged"],
    }
    first_page = await backend.collection("regressions", cursor=None, limit=1, filters=filters)
    cursor = first_page["nextCursor"]
    assert isinstance(cursor, str)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
    assert len(cursor) <= 1024
    payload = json.loads(base64.urlsafe_b64decode(cursor[4:] + "===").decode())
    assert payload["version"] == max_version

    # Legacy cursors remain readable for clients that already received one.
    legacy_payload = {
        "v": 2,
        "snapshotAt": payload["snapshotAt"],
        "timestamp": payload["timestamp"],
        "identifier": payload["identifier"],
    }
    legacy_encoded = base64.urlsafe_b64encode(
        json.dumps(legacy_payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    legacy_cursor = "ev1." + legacy_encoded
    # The legacy shape predates regression's version tie-breaker, so it is
    # accepted for the older non-versioned history traversal only.
    connection.rows = []
    await backend.history_page(cursor=legacy_cursor, limit=1, resource_type=None, resource_id=None)
    connection.rows = [
        (
            second,
            second["regressionId"],
            second["regressionVersion"],
            second["createdAt"],
        )
    ]
    await backend.collection("regressions", cursor=cursor, limit=1, filters=filters)
    continuation_sql, continuation_params = connection.calls[-1]
    assert "regression.regression_version < %s" in continuation_sql
    assert continuation_params[-2] == max_version


@pytest.mark.asyncio
async def test_regression_cursor_rejects_malformed_and_overlong_values() -> None:
    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[Any]:
        yield _Connection([])

    backend = PostgresEvaluationQueryBackend(connection_factory)
    filters = {
        "from": "2026-10-21T09:00:00Z",
        "to": "2026-10-21T11:00:00Z",
    }
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection(
            "regressions", cursor="ev2_not-base64", limit=1, filters=filters
        )
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection(
            "regressions", cursor="ev2_" + "A" * 1024, limit=1, filters=filters
        )


@pytest.mark.asyncio
async def test_storage_cursor_requires_exact_shape_kind_and_canonical_encoding() -> None:
    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[Any]:
        yield _Connection([])

    backend = PostgresEvaluationQueryBackend(connection_factory)
    payload: dict[str, object] = {
        "v": 2,
        "kind": "jobs",
        "snapshotAt": "2026-10-21T10:00:00Z",
        "timestamp": None,
        "identifier": None,
        "version": None,
    }

    def cursor_for(value: dict[str, object], prefix: str = "ev2_") -> str:
        encoded = base64.urlsafe_b64encode(
            json.dumps(value, separators=(",", ":")).encode()
        ).decode().rstrip("=")
        return prefix + encoded

    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection(
            "jobs",
            cursor=cursor_for({key: item for key, item in payload.items() if key != "kind"}),
            limit=1,
            filters=None,
        )
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection(
            "jobs", cursor=cursor_for({**payload, "extra": 1}), limit=1, filters=None
        )
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection(
            "jobs", cursor=cursor_for({**payload, "kind": "history"}), limit=1, filters=None
        )
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.history_page(
            cursor=cursor_for(payload), limit=1, resource_type=None, resource_id=None
        )
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection("jobs", cursor=cursor_for(payload) + ".", limit=1, filters=None)
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection("jobs", cursor="ev2_e31", limit=1, filters=None)

    legacy_payload = {key: item for key, item in payload.items() if key not in {"kind", "version"}}
    await backend.history_page(
        cursor=cursor_for(legacy_payload, prefix="ev1."),
        limit=1,
        resource_type=None,
        resource_id=None,
    )
    legacy_cursor = cursor_for(legacy_payload, prefix="ev1.")
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection("jobs", cursor=legacy_cursor, limit=1, filters=None)
    with pytest.raises(EvaluationQueryError, match="cursor is invalid"):
        await backend.collection(
            "regressions",
            cursor=legacy_cursor,
            limit=1,
            filters={
                "from": "2026-10-21T09:00:00Z",
                "to": "2026-10-21T11:00:00Z",
            },
        )


def test_saved_query_execution_cursor_is_public_safe_and_binds_query() -> None:
    query_id = str(uuid4())
    provider_cursor = "ev2_" + "A" * 80
    cursor = encode_execution_cursor("regression_search", query_id, "1.0.0", provider_cursor)
    assert re.fullmatch(r"[A-Za-z0-9_-]+", cursor)
    assert len(cursor) <= 1024
    assert cursor.startswith("sq2_")
    assert decode_execution_cursor(
        cursor,
        kind="regression_search",
        query_id=query_id,
        query_version="1.0.0",
    ) == provider_cursor
    with pytest.raises(SavedQueryError, match="execution cursor is invalid"):
        decode_execution_cursor(
            cursor + "....",
            kind="regression_search",
            query_id=query_id,
            query_version="1.0.0",
        )
    with pytest.raises(SavedQueryError, match="execution cursor is invalid"):
        decode_execution_cursor(
            cursor,
            kind="run_search",
            query_id=query_id,
            query_version="1.0.0",
        )
    with pytest.raises(SavedQueryError, match="execution cursor is invalid"):
        encode_execution_cursor("regression_search", query_id, "1.0.0", "x" * 1024)


def test_saved_query_execution_cursor_accepts_only_bounded_legacy_shape() -> None:
    query_id = str(uuid4())
    payload = {
        "v": 1,
        "kind": "run_search",
        "queryId": query_id,
        "queryVersion": "1.0.0",
        "cursor": "ev2_legacy-provider",
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    legacy = "sq1." + encoded
    assert decode_execution_cursor(
        legacy,
        kind="run_search",
        query_id=query_id,
        query_version="1.0.0",
    ) == "ev2_legacy-provider"
    with pytest.raises(SavedQueryError, match="execution cursor is invalid"):
        decode_execution_cursor(
            legacy + ".",
            kind="run_search",
            query_id=query_id,
            query_version="1.0.0",
        )
    malformed = base64.urlsafe_b64encode(b"{}").decode().rstrip("=")
    with pytest.raises(SavedQueryError, match="execution cursor is invalid"):
        decode_execution_cursor(
            "sq2_" + malformed,
            kind="run_search",
            query_id=query_id,
            query_version="1.0.0",
        )


def test_saved_query_listing_cursor_rejects_trailing_characters() -> None:
    cursor = encode_cursor("2026-10-21T10:00:00Z", str(uuid4()), "1.0.0")
    assert decode_cursor(cursor) is not None
    with pytest.raises(SavedQueryError, match="cursor is invalid"):
        decode_cursor(cursor + "....")


@pytest.mark.parametrize("value", [True, False])
def test_closed_integer_admission_rejects_boolean_limits(value: bool) -> None:
    run_query = {
        "kind": "run_search",
        "window": {"kind": "last_hour", "from": None, "to": None},
        "statuses": [], "components": [], "deployments": [], "models": [],
        "pageSize": value,
    }
    with pytest.raises(SavedQueryError):
        normalize_definition(run_query)

    analytics = {
        "kind": "analytics", "metric": "errors", "unit": "count",
        "aggregation": "average", "window": "one_hour", "bucket": "auto",
        "groupBy": "none", "focus": "all", "facets": [],
        "seriesLimit": value, "pointLimit": 1,
    }
    with pytest.raises(SavedQueryError):
        normalize_definition(analytics)

    panel = {
        "panelId": str(uuid4()), "title": "Runs", "query": {
            "queryId": str(uuid4()), "queryVersion": "1.0.0"
        }, "visualization": "table", "size": "medium", "position": value,
    }
    with pytest.raises(DashboardError):
        _panel(panel)


def test_alert_condition_rejects_boolean_minimum_and_nonfinite_threshold() -> None:
    condition = {
        "signal": "latency", "query": {"queryId": str(uuid4()), "queryVersion": "1.0.0"},
        "operator": "greater_than", "threshold": 1, "unit": "milliseconds",
        "window": "one_hour", "minimumSampleSize": True,
        "missingEvidencePolicy": "inconclusive", "staleAfterSeconds": 60, "groupBy": [],
    }
    with pytest.raises(AlertError):
        _condition(condition)
    for nonfinite in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(AlertError):
            _condition({**condition, "minimumSampleSize": 1, "threshold": nonfinite})
