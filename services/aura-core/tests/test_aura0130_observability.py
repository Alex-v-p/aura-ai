"""Manifest-level assertions for the metadata-only calendar observability boundary."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any, cast

import pytest
import yaml
from aura_core.bootstrap.calendar_uow import _ConflictTelemetryObserver, _TelemetryAdapter
from aura_core.entrypoints.api.routes.calendar import _error_class, _observe
from aura_core.platform.telemetry import (
    CALENDAR_ERROR_CLASSES,
    CALENDAR_TERMINAL_OUTCOMES,
    MetadataMetrics,
    new_span_id,
)
from aura_core.providers.calendars.google_calendar import (
    CalendarProviderError,
    ProviderFailureCategory,
)

ROOT = Path(__file__).parents[1]

EXPECTED = {
    "calendar-connection.yaml": {
        "component": "aura.integration.calendar_connection",
        "metrics": {
            "calendar_connection_command_duration_ms",
            "calendar_connection_command_outcome",
            "calendar_connection_health",
        },
        "evaluators": {"calendar.credential-lifecycle.v1", "calendar.provider-boundary.v1"},
        "replayable": False,
    },
    "google-calendar-provider.yaml": {
        "component": "aura.provider.google_calendar",
        "metrics": {
            "google_calendar_provider_request_duration_ms",
            "google_calendar_provider_request_outcome",
            "google_calendar_provider_retry_count",
            "calendar_rate_limit_state",
        },
        "evaluators": {"calendar.provider-boundary.v1", "calendar.normalization.v1"},
        "replayable": False,
    },
    "calendar-retrieval.yaml": {
        "component": "aura.integration.calendar_retrieval",
        "metrics": {
            "calendar_retrieval_duration_ms",
            "calendar_retrieval_outcome",
            "calendar_returned_event_count_bucket",
        },
        "evaluators": {"calendar.normalization.v1", "calendar.provider-boundary.v1"},
        "replayable": False,
    },
    "calendar-conflict.yaml": {
        "component": "aura.automation.calendar_conflict",
        "metrics": {
            "calendar_conflict_evaluation_duration_ms",
            "calendar_conflict_evaluation_outcome",
        },
        "evaluators": {"calendar.conflict.v1", "calendar.normalization.v1"},
        "replayable": True,
    },
}


def test_calendar_components_are_versioned_metadata_only_and_cover_required_obligations() -> None:
    for filename, expected in EXPECTED.items():
        manifest = yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )
        component = manifest["component"]
        assert component["id"] == expected["component"]
        assert component["version"] == "1.0.0"
        assert manifest["capture_policy"] == "metadata_only"
        assert set(manifest["metrics"]) == expected["metrics"]
        assert set(manifest["evaluators"]) == expected["evaluators"]
        assert manifest["input"]["replayable"] is expected["replayable"]
        assert "content" in manifest["cardinality"]["prohibited"]
        assert "credential" in manifest["cardinality"]["prohibited"]
        assert "owner_identity" in manifest["cardinality"]["prohibited"]
        assert "token" in manifest["cardinality"]["prohibited"]


def test_calendar_manifest_dimensions_are_low_cardinality_and_content_free() -> None:
    for filename in EXPECTED:
        manifest = yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )
        dimensions = set(manifest["cardinality"]["metric_attributes"])
        assert dimensions <= {
            "operation",
            "outcome",
            "error_class",
            "dependency",
            "status",
            "retry_class",
            "event_count_bucket",
            "relationship_count_bucket",
            "connection_health",
            "rate_limit_state",
            "provider",
        }
        for value in manifest["cardinality"]["trace_only"]:
            assert "content" not in value.lower()
            assert "token" not in value.lower()
            assert "owner" not in value.lower()


def test_calendar_manifest_metric_ownership_and_dimensions_match_runtime_catalogue() -> None:
    from aura_core.platform.telemetry import _DIMENSION_KEYS, _METRICS

    for filename, expected in EXPECTED.items():
        manifest = yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )
        component = str(expected["component"])
        assert set(cast(set[str], expected["metrics"])) == set(_METRICS[component])
        assert set(manifest["cardinality"]["metric_attributes"]) <= _DIMENSION_KEYS


@pytest.mark.asyncio
async def test_calendar_manifest_ownership_matches_actual_production_snapshots() -> None:
    metrics = MetadataMetrics(capacity=128)
    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(aura=SimpleNamespace(metrics=metrics)),
        )
    )
    _observe(
        cast(Any, request),
        "aura.integration.calendar_connection",
        "calendar.connection.complete",
        monotonic() - 0.001,
        "ok",
        None,
        trace_id="a" * 32,
        span_id=new_span_id(),
        parent_span_id=None,
        result=SimpleNamespace(status=SimpleNamespace(value="connected")),
    )
    _TelemetryAdapter(metrics).record(
        "provider_rate_limit", retry_count=1, elapsed_ms=2.0
    )
    _observe(
        cast(Any, request),
        "aura.integration.calendar_retrieval",
        "calendar.events.query",
        monotonic() - 0.001,
        "ok",
        None,
        trace_id="b" * 32,
        span_id=new_span_id(),
        parent_span_id=None,
        result=SimpleNamespace(items=(object(),)),
    )
    await _ConflictTelemetryObserver(metrics).observe_conflict_evaluation(
        duration_ms=1.0, outcome="ok"
    )

    actual: dict[str, set[str]] = {}
    for item in metrics.snapshot():
        actual.setdefault(item.component_id, set()).add(item.metric)
    manifests: dict[str, dict[str, Any]] = {}
    for filename in EXPECTED:
        manifests[str(yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )["component"]["id"])] = yaml.safe_load(
            (ROOT / "resources/component-manifests" / filename).read_text(encoding="utf-8")
        )
    for _filename, expected in EXPECTED.items():
        component = str(expected["component"])
        assert actual[component] == set(cast(set[str], expected["metrics"]))

    spans = [item for item in metrics.snapshot() if item.kind == "span"]
    allowed_outcomes = set(CALENDAR_TERMINAL_OUTCOMES)
    allowed_errors = set(CALENDAR_ERROR_CLASSES)
    for item in metrics.snapshot():
        component_manifest = manifests[item.component_id]
        declared_dimensions = set(component_manifest["cardinality"]["metric_attributes"])
        assert set(dict(item.dimensions)) <= declared_dimensions
    for span in spans:
        dimensions = dict(span.dimensions)
        assert dimensions["outcome"] in allowed_outcomes
        if "error_class" in dimensions:
            assert dimensions["error_class"] in allowed_errors
        if "retry_class" in dimensions:
            assert dimensions["retry_class"] in {"none", "rate_limit", "transient", "unknown"}
            assert dimensions["retry_class"] not in dimensions["outcome"]


def test_calendar_closed_provider_outcomes_map_to_exact_api_error_classes() -> None:
    assert (
        _error_class(CalendarProviderError(ProviderFailureCategory.CANCELED, retryable=False))
        == "budget"
    )
    assert (
        _error_class(CalendarProviderError(ProviderFailureCategory.DEADLINE, retryable=True))
        == "deadline"
    )
    assert (
        _error_class(CalendarProviderError(ProviderFailureCategory.RATE_LIMIT, retryable=True))
        == "provider_rate_limit"
    )
    assert (
        _error_class(CalendarProviderError(ProviderFailureCategory.UNAVAILABLE, retryable=True))
        == "provider_unavailable"
    )


def test_calendar_runtime_spans_metrics_and_correlation_are_metadata_only() -> None:
    metrics = MetadataMetrics(capacity=128)
    trace_id = "a" * 32
    root = new_span_id()
    provider = new_span_id()
    retrieval = new_span_id()
    conflict = new_span_id()

    metrics.record_span(
        "aura.integration.calendar_connection",
        "calendar.connection.complete",
        4.0,
        trace_id=trace_id,
        span_id=root,
        parent_span_id=None,
        dependency="event_store",
        outcome="ok",
    )
    metrics.record_span(
        "aura.provider.google_calendar",
        "calendar.provider.request",
        8.0,
        trace_id=trace_id,
        span_id=provider,
        parent_span_id=root,
        dependency="event_store",
        outcome="ok",
        provider="google_calendar",
        retry_class="none",
    )
    metrics.record_span(
        "aura.integration.calendar_retrieval",
        "calendar.events.query",
        12.0,
        trace_id=trace_id,
        span_id=retrieval,
        parent_span_id=provider,
        dependency="event_store",
        outcome="ok",
        retrieval_stage="primary",
        event_count_bucket="1_10",
    )
    metrics.record_span(
        "aura.automation.calendar_conflict",
        "calendar.conflicts.query",
        3.0,
        trace_id=trace_id,
        span_id=conflict,
        parent_span_id=retrieval,
        dependency="event_store",
        outcome="ok",
    )

    required_metrics: dict[str, set[str]] = {
        str(expected["component"]): cast(set[str], expected["metrics"])
        for expected in EXPECTED.values()
    }
    for component, names in required_metrics.items():
        for name in names:
            metrics.increment(component, name, trace_id=trace_id)

    snapshot = metrics.snapshot()
    spans = [item for item in snapshot if item.kind == "span"]
    assert len(spans) == 4
    assert {item.trace_id for item in spans} == {trace_id}
    assert {(item.span_id, item.parent_span_id) for item in spans} == {
        (root, None),
        (provider, root),
        (retrieval, provider),
        (conflict, retrieval),
    }
    assert {item.metric for item in snapshot} >= set().union(*required_metrics.values())
    assert metrics.stats().rejected == 0

    before = len(snapshot)
    metrics.record_span(
        "aura.automation.calendar_conflict",
        "calendar.conflicts.unknown",
        1.0,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency="event_store",
        outcome="ok",
    )
    assert len(metrics.snapshot()) == before
    assert metrics.stats().rejected == 1
