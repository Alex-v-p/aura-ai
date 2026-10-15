"""Closed standard OTLP/HTTP JSON adapter for the Observatory boundary.

The Collector speaks the standard ``/v1/metrics`` and ``/v1/traces`` routes.
This adapter intentionally understands only the metadata attributes emitted by
Core; arbitrary OTLP attributes, events, and malformed links are rejected
before they reach the versioned Observatory contract.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import cast
from uuid import NAMESPACE_URL, uuid5


class OtlpPayloadError(ValueError):
    """The payload is not the bounded OTLP metadata profile."""


_RESOURCE_KEYS = frozenset(
    {
        "service.name",
        "service.role",
        "deployment.id",
        "deployment.environment",
        "deployment.git_commit",
        "host.id",
        "capture.policy",
    }
)
_OBSERVATION_KEYS = frozenset(
    {
        "aura.observation_id",
        "aura.component_id",
        "aura.component_version",
        "aura.operation",
        "aura.metric_name",
        "aura.capture_policy",
        "aura.run_id",
        "aura.conversation_id",
    }
)
_DIMENSION_MAP = {
    "archive_state": "archiveState",
    "error_class": "errorClass",
    "token_estimator": "tokenEstimator",
    "scope_type": "scopeType",
    "retrieval_stage": "retrievalStage",
}
_DIMENSION_KEYS = frozenset(
    {
        "archiveState",
        "dependency",
        "errorClass",
        "outcome",
        "provider",
        "status",
        "tokenEstimator",
        "scopeType",
        "retrievalStage",
        "degradation",
        "operation",
    }
)


def metrics_to_observations(payload: object) -> list[dict[str, object]]:
    """Normalize an OTLP metrics export into telemetry-v1 observations."""

    root = _object(payload, "metrics payload")
    _only_keys(root, {"resourceMetrics"}, "metrics payload")
    resources = _list(root.get("resourceMetrics"), "resourceMetrics")
    result: list[dict[str, object]] = []
    seen_ids: set[str] = set()
    for resource_metric in resources:
        resource = _object(resource_metric, "resource metric")
        _only_keys(resource, {"resource", "scopeMetrics", "schemaUrl"}, "resource metric")
        deployment = _deployment(resource.get("resource"))
        scopes = _list(resource.get("scopeMetrics"), "scopeMetrics")
        for scope in scopes:
            scope_object = _object(scope, "scope metric")
            _only_keys(scope_object, {"scope", "metrics", "schemaUrl"}, "scope metric")
            for metric in _list(scope_object.get("metrics"), "metrics"):
                metric_object = _object(metric, "metric")
                _only_keys(metric_object, {"name", "description", "unit", "gauge", "sum"}, "metric")
                if not isinstance(metric_object.get("name"), str):
                    raise OtlpPayloadError("metric name is required")
                value_container = metric_object.get("gauge") or metric_object.get("sum")
                if value_container is None or (
                    metric_object.get("gauge") is not None and metric_object.get("sum") is not None
                ):
                    raise OtlpPayloadError("metric must contain exactly one gauge or sum")
                values = _object(value_container, "metric value")
                _only_keys(
                    values, {"dataPoints", "aggregationTemporality", "isMonotonic"}, "metric value"
                )
                if values.get("exemplars"):
                    raise OtlpPayloadError("metric exemplars are not accepted")
                for point in _list(values.get("dataPoints"), "dataPoints"):
                    point_object = _object(point, "data point")
                    _only_keys(
                        point_object,
                        {
                            "attributes",
                            "timeUnixNano",
                            "startTimeUnixNano",
                            "asDouble",
                            "asInt",
                            "exemplars",
                            "flags",
                        },
                        "data point",
                    )
                    attrs = _attributes(point_object.get("attributes"), _OBSERVATION_KEYS)
                    original_id = attrs.get("aura.observation_id")
                    observed_at = _timestamp(point_object.get("timeUnixNano"))
                    if original_id in seen_ids:
                        attrs["aura.observation_id"] = str(
                            uuid5(NAMESPACE_URL, f"{original_id}:{observed_at.isoformat()}")
                        )
                    seen_ids.add(attrs["aura.observation_id"])
                    result.append(
                        _observation(
                            deployment,
                            attrs,
                            kind="metric",
                            value=_number(point_object),
                            observed_at=observed_at,
                            trace=None,
                            unit=_metric_unit(metric_object.get("unit")),
                        )
                    )
    if not result:
        raise OtlpPayloadError("OTLP metrics payload contains no data points")
    return result


def traces_to_observations(payload: object) -> list[dict[str, object]]:
    """Normalize an OTLP traces export into telemetry-v1 span observations."""

    root = _object(payload, "traces payload")
    _only_keys(root, {"resourceSpans"}, "traces payload")
    resources = _list(root.get("resourceSpans"), "resourceSpans")
    result: list[dict[str, object]] = []
    for resource_span in resources:
        resource = _object(resource_span, "resource span")
        _only_keys(resource, {"resource", "scopeSpans", "schemaUrl"}, "resource span")
        deployment = _deployment(resource.get("resource"))
        scopes = _list(resource.get("scopeSpans"), "scopeSpans")
        for scope in scopes:
            scope_object = _object(scope, "scope span")
            _only_keys(scope_object, {"scope", "spans", "schemaUrl"}, "scope span")
            for span in _list(scope_object.get("spans"), "spans"):
                span_object = _object(span, "span")
                _only_keys(
                    span_object,
                    {
                        "traceId",
                        "spanId",
                        "parentSpanId",
                        "name",
                        "startTimeUnixNano",
                        "endTimeUnixNano",
                        "attributes",
                        "status",
                        "kind",
                        "traceState",
                        "flags",
                        "links",
                    },
                    "span",
                )
                if span_object.get("events"):
                    raise OtlpPayloadError("span events are not accepted")
                links = span_object.get("links")
                if links:
                    link_ids = _links(links, span_object.get("traceId"))
                else:
                    link_ids = None
                trace_id = _string(span_object.get("traceId"), "traceId")
                span_id = _string(span_object.get("spanId"), "spanId")
                parent = span_object.get("parentSpanId")
                attrs = _attributes(span_object.get("attributes"), _OBSERVATION_KEYS)
                end = _timestamp(span_object.get("endTimeUnixNano"))
                start = _timestamp(span_object.get("startTimeUnixNano"))
                duration = max(0.0, (end - start).total_seconds() * 1000.0)
                result.append(
                    _observation(
                        deployment,
                        attrs,
                        kind="span",
                        value=duration,
                        observed_at=end,
                        trace={
                            "traceId": trace_id,
                            "spanId": span_id,
                            **({"parentSpanId": _string(parent, "parentSpanId")} if parent else {}),
                            **({"linkedSpanIds": link_ids} if link_ids else {}),
                        },
                        unit="milliseconds",
                    )
                )
    if not result:
        raise OtlpPayloadError("OTLP traces payload contains no spans")
    return result


def _observation(
    deployment: dict[str, str],
    attrs: dict[str, str],
    *,
    kind: str,
    value: float,
    observed_at: datetime,
    trace: dict[str, object] | None,
    unit: str | None = None,
) -> dict[str, object]:
    required = (
        "aura.observation_id",
        "aura.component_id",
        "aura.component_version",
        "aura.operation",
        "aura.metric_name",
        "aura.capture_policy",
    )
    if any(key not in attrs for key in required):
        raise OtlpPayloadError("metadata observation attributes are incomplete")
    dimensions: dict[str, str] = {}
    for key in (*_DIMENSION_KEYS, *_DIMENSION_MAP):
        value_for_key = attrs.pop(f"aura.dimension.{key}", None)
        if value_for_key is not None:
            dimensions[_DIMENSION_MAP.get(key, key)] = value_for_key
    # Build correlation explicitly to avoid ever passing arbitrary attributes.
    correlation = {}
    if "aura.run_id" in attrs:
        correlation["runId"] = attrs["aura.run_id"]
    if "aura.conversation_id" in attrs:
        correlation["conversationId"] = attrs["aura.conversation_id"]
    result: dict[str, object] = {
        "schemaVersion": 1,
        "observationId": attrs["aura.observation_id"],
        "observedAt": observed_at.isoformat().replace("+00:00", "Z"),
        "kind": kind,
        "component": {"id": attrs["aura.component_id"], "version": attrs["aura.component_version"]},
        "operation": attrs["aura.operation"],
        "measurement": {
            "name": attrs["aura.metric_name"],
            "value": value,
            **({"unit": unit} if unit is not None else {}),
        },
        "dimensions": dimensions,
        "deployment": {
            "deploymentId": deployment["deploymentId"],
            "gitCommit": deployment["gitCommit"],
            "environment": deployment["environment"],
            "hostId": deployment["hostId"],
        },
        "serviceRole": deployment["serviceRole"],
        "capturePolicy": attrs["aura.capture_policy"],
    }
    if trace is not None:
        result["trace"] = trace
    if correlation:
        result["correlation"] = correlation
    return result


def _deployment(value: object) -> dict[str, str]:
    resource = _object(value, "resource")
    attrs = _attributes(resource.get("attributes"), _RESOURCE_KEYS)
    required = {
        "service.role": "serviceRole",
        "deployment.id": "deploymentId",
        "deployment.environment": "environment",
        "deployment.git_commit": "gitCommit",
        "host.id": "hostId",
        "capture.policy": "capturePolicy",
    }
    if any(key not in attrs for key in required):
        raise OtlpPayloadError("deployment resource attributes are incomplete")
    if "service.name" in attrs and attrs["service.name"] != "aura-core":
        raise OtlpPayloadError("unexpected OTLP service name")
    return {target: attrs[source] for source, target in required.items()}


def _metric_unit(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise OtlpPayloadError("metric unit must be a string")
    if value not in {"count", "milliseconds", "bytes", "ratio", "score"}:
        raise OtlpPayloadError("metric unit is outside the metadata allowlist")
    return value


def _attributes(value: object, allowed: frozenset[str]) -> dict[str, str]:
    entries = _list(value, "attributes")
    result: dict[str, str] = {}
    for entry in entries:
        item = _object(entry, "attribute")
        _only_keys(item, {"key", "value"}, "attribute")
        key = _string(item.get("key"), "attribute key")
        if key.startswith("aura.dimension."):
            dimension = key.removeprefix("aura.dimension.")
            if dimension not in _DIMENSION_KEYS and dimension not in _DIMENSION_MAP:
                raise OtlpPayloadError("unknown dimension attribute")
        elif key not in allowed:
            raise OtlpPayloadError("attribute is outside the metadata allowlist")
        if key in result:
            raise OtlpPayloadError("duplicate OTLP attribute")
        value_object = _object(item.get("value"), "attribute value")
        if set(value_object) != {"stringValue"}:
            raise OtlpPayloadError("metadata attributes must be string values")
        result[key] = _string(value_object["stringValue"], key)
    return result


def _links(value: object, trace_id: object) -> list[str]:
    if not isinstance(value, list):
        raise OtlpPayloadError("span links are bounded")
    links = cast(list[object], value)
    if len(links) > 8:
        raise OtlpPayloadError("span links are bounded")
    trace = _string(trace_id, "traceId")
    result: list[str] = []
    for item in links:
        link = _object(item, "span link")
        _only_keys(link, {"traceId", "spanId", "attributes", "traceState"}, "span link")
        if link.get("attributes"):
            raise OtlpPayloadError("span link attributes are not accepted")
        if _string(link.get("traceId"), "link traceId") != trace:
            raise OtlpPayloadError("cross-trace links are not accepted")
        span_id = _string(link.get("spanId"), "link spanId")
        if span_id in result:
            raise OtlpPayloadError("duplicate span link")
        result.append(span_id)
    return result


def _number(value: Mapping[str, object]) -> float:
    present = [key for key in ("asDouble", "asInt") if key in value]
    if len(present) != 1:
        raise OtlpPayloadError("data point must contain one numeric value")
    raw = value[present[0]]
    if not isinstance(raw, (int, float, str)) or isinstance(raw, bool):
        raise OtlpPayloadError("data point value is not numeric")
    try:
        result = float(raw)
    except (TypeError, ValueError) as exc:
        raise OtlpPayloadError("data point value is not numeric") from exc
    if not math.isfinite(result):
        raise OtlpPayloadError("data point value must be finite")
    return result


def _timestamp(value: object) -> datetime:
    if not isinstance(value, (int, float, str)) or isinstance(value, bool):
        raise OtlpPayloadError("OTLP timestamp is invalid")
    try:
        nanos = int(value)  # OTLP JSON encodes uint64 timestamps as strings.
    except (TypeError, ValueError) as exc:
        raise OtlpPayloadError("OTLP timestamp is invalid") from exc
    if nanos <= 0:
        raise OtlpPayloadError("OTLP timestamp is invalid")
    return datetime.fromtimestamp(nanos / 1_000_000_000, UTC)


def _object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise OtlpPayloadError(f"{label} must be an object")
    raw = cast(dict[object, object], value)
    return {str(key): item for key, item in raw.items()}


def _list(value: object, label: str) -> list[object]:
    if not isinstance(value, list):
        raise OtlpPayloadError(f"{label} must be an array")
    return cast(list[object], value)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value:
        raise OtlpPayloadError(f"{label} must be a non-empty string")
    return value


def _only_keys(value: Mapping[str, object], allowed: set[str], label: str) -> None:
    extra = set(value) - allowed
    if extra:
        raise OtlpPayloadError(f"{label} contains unsupported fields")


__all__ = ["OtlpPayloadError", "metrics_to_observations", "traces_to_observations"]
