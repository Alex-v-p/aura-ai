# ruff: noqa: E501,E701,E702,F401,I001
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportPrivateUsage=false, reportUnusedImport=false, reportUnnecessaryIsInstance=false, reportAttributeAccessIssue=false
"""Closed, versioned saved-query definitions for Observatory operations.

This module intentionally contains no SQL and no storage knowledge.  The
transport and persistence layers call these small validators before a query
definition crosses the service boundary.  A saved query is a finite value
object: callers cannot smuggle SQL, expressions, arbitrary dimensions, or
provider-specific fields into the operations database.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any
from uuid import UUID, uuid4

_ID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
_SAFE_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT_ID = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_MODEL_ID = re.compile(r"^(?!.*(?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint))[a-z0-9][a-z0-9._+-]{0,127}(?::[a-z0-9._+-]+)?$")
_PRIVATE = re.compile(r"(?:prompt|response|memory|document|filename|credential|bearer|token|secret|password|endpoint|owner|identity|argument|result)", re.I)
_KINDS = {"analytics", "run_search", "trace_search", "run_comparison", "regression_search", "component_health"}
_FACETS = {"component", "component_version", "deployment", "environment", "host", "service_role", "operation", "model", "provider", "outcome", "status", "error_class", "retrieval_stage", "regression_outcome"}
_METRICS = {"operation_duration_ms", "run_duration_ms", "queue_wait_ms", "errors", "retries", "backlog", "backlog_depth", "inference_duration_ms", "time_to_first_token_ms", "output_tokens", "provider_errors", "model_routing_duration_ms", "structured_inference_duration_ms", "tool_selection_duration_ms", "tool_selection_outcome", "tool_policy_evaluation_duration_ms", "tool_policy_decision", "tool_argument_validation_outcome", "tool_execution_duration_ms", "tool_execution_outcome", "tool_timeout_count", "memory_extraction_duration_ms", "memory_candidate_decision", "memory_reviewable_candidate_count", "memory_operation_duration_ms", "memory_operation_outcome", "memory_retrieval_duration_ms", "memory_lexical_search_duration_ms", "memory_vector_search_duration_ms", "memory_rerank_duration_ms", "memory_processing_errors", "memory_retry_count", "memory_reindex_backlog", "document_retrieval_duration_ms", "document_retrieval_outcome", "document_retrieval_candidate_count", "reminder_scheduler_scan_duration_ms", "reminder_due_lag_ms", "reminder_series_due_lag_ms", "reminder_fired_count", "reminder_failure_count", "notification_failure_count", "notification_acknowledgement_outcome"}
_UNITS = {"count", "milliseconds", "bytes", "ratio", "score"}
_AGGREGATIONS = {"average", "sum", "minimum", "maximum", "p50", "p90", "p95", "p99", "rate", "count"}
_BUCKETS = {"auto", "one_minute", "five_minutes", "fifteen_minutes", "one_hour", "six_hours", "one_day"}
_GROUPINGS = {"none", "component", "component_version", "operation", "deployment", "environment", "host", "service_role", "provider", "outcome", "error_class", "status", "retrieval_stage"}
_FOCUSES = {"all", "model", "queue", "retrieval", "tool", "memory", "automation"}
_RUN_STATUSES = {"queued", "running", "succeeded", "failed", "cancelled", "unknown"}
_OUTCOMES = {"succeeded", "failed", "cancelled", "unknown"}
_REGRESSION_OUTCOMES = {"regressed", "unchanged", "inconclusive", "unavailable", "incompatible"}
_WINDOW_ALIASES = {
    "last_15_minutes": "fifteen_minutes",
    "last_hour": "one_hour",
    "last_6_hours": "six_hours",
    "last_24_hours": "twenty_four_hours",
    "last_7_days": "seven_days",
}
_WINDOWS = set(_WINDOW_ALIASES.values()) | {"thirty_one_days"}
_EXECUTION_CURSOR_PREFIX = "sq2_"
_EXECUTION_CURSOR_LEGACY_PREFIX = "sq1."
_PUBLIC_CURSOR_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class SavedQueryError(ValueError):
    """Closed-definition validation failure."""


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _closed(value: object, fields: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SavedQueryError(f"{name} is invalid")
    return dict(value)


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        raise SavedQueryError(f"{name} is invalid")
    try:
        parsed = str(UUID(value))
    except ValueError as exc:
        raise SavedQueryError(f"{name} is invalid") from exc
    if parsed != value:
        raise SavedQueryError(f"{name} is invalid")
    return value


def _label(value: object, name: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length or _LABEL.fullmatch(value) is None or _PRIVATE.search(value):
        raise SavedQueryError(f"{name} is invalid")
    return value


def _version(value: object, name: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None or _PRIVATE.search(value):
        raise SavedQueryError(f"{name} is invalid")
    return value


def _window(value: object) -> str:
    if isinstance(value, str):
        if value not in _WINDOWS:
            raise SavedQueryError("window is invalid")
        return value
    item = _closed(value, {"kind", "from", "to"}, "window")
    if not isinstance(item["kind"], str) or item["kind"] not in _WINDOW_ALIASES:
        raise SavedQueryError("window is invalid")
    if item["from"] is not None or item["to"] is not None:
        raise SavedQueryError("window bounds are invalid")
    return _WINDOW_ALIASES[item["kind"]]


def _values(value: object, name: str, *, limit: int, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list) or len(value) > limit or (not allow_empty and not value) or any(not isinstance(v, str) for v in value) or len(set(value)) != len(value):
        raise SavedQueryError(f"{name} is invalid")
    return [_label(v, name) for v in value]


def _enum_values(value: object, name: str, *, limit: int, allowed: set[str]) -> list[str]:
    values = _values(value, name, limit=limit)
    if any(item not in allowed for item in values):
        raise SavedQueryError(f"{name} is invalid")
    return values


def _safe(value: object, name: str, *, max_length: int = 128) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_length or _SAFE_LABEL.fullmatch(value) is None or _PRIVATE.search(value):
        raise SavedQueryError(f"{name} is invalid")
    return value


def _component(value: object, name: str) -> str:
    if not isinstance(value, str) or _COMPONENT_ID.fullmatch(value) is None or _PRIVATE.search(value):
        raise SavedQueryError(f"{name} is invalid")
    return value


def _model(value: object, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 193 or _MODEL_ID.fullmatch(value) is None:
        raise SavedQueryError(f"{name} is invalid")
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise SavedQueryError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SavedQueryError(f"{name} is invalid") from exc
    if parsed.tzinfo is None:
        raise SavedQueryError(f"{name} is invalid")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _facet(value: object) -> dict[str, str]:
    item = _closed(value, {"name", "value"}, "facet")
    name = item["name"]
    if name not in _FACETS or not isinstance(name, str):
        raise SavedQueryError("facet name is invalid")
    return {"name": name, "value": _safe(item["value"], "facet value")}


def normalize_definition(value: object) -> dict[str, object]:
    """Validate and canonicalize one observatory-v1 SavedQueryDefinition."""
    if not isinstance(value, Mapping) or not isinstance(value.get("kind"), str):
        raise SavedQueryError("definition is invalid")
    kind = str(value["kind"])
    if kind not in _KINDS:
        raise SavedQueryError("definition kind is invalid")
    if kind == "analytics":
        item = _closed(value, {"kind", "metric", "unit", "aggregation", "window", "bucket", "groupBy", "focus", "facets", "seriesLimit", "pointLimit"}, "analytics definition")
        result = dict(item)
        result["window"] = _window(item["window"])
        result["facets"] = [_facet(f) for f in (item["facets"] if isinstance(item["facets"], list) else [])]
        if len(result["facets"]) > 13 or not isinstance(item["seriesLimit"], int) or isinstance(item["seriesLimit"], bool) or not 1 <= item["seriesLimit"] <= 12 or not isinstance(item["pointLimit"], int) or isinstance(item["pointLimit"], bool) or not 1 <= item["pointLimit"] <= 200:
            raise SavedQueryError("analytics bounds are invalid")
        if item["metric"] not in _METRICS or item["unit"] not in _UNITS or item["aggregation"] not in _AGGREGATIONS or item["bucket"] not in _BUCKETS or item["groupBy"] not in _GROUPINGS or item["focus"] not in _FOCUSES:
            raise SavedQueryError("analytics vocabulary is invalid")
        return result
    if kind == "run_search":
        item = _closed(value, {"kind", "window", "statuses", "components", "deployments", "models", "pageSize"}, "run search definition")
        if not isinstance(item["pageSize"], int) or isinstance(item["pageSize"], bool) or not 1 <= item["pageSize"] <= 200:
            raise SavedQueryError("page size is invalid")
        result = dict(item); result["window"] = _window(item["window"])
        result["statuses"] = _enum_values(item["statuses"], "statuses", limit=6, allowed=_RUN_STATUSES)
        result["components"] = [_component(value, "component") for value in _values(item["components"], "components", limit=32)]
        result["deployments"] = [_safe(value, "deployment") for value in _values(item["deployments"], "deployments", limit=32)]
        result["models"] = [_model(value, "model") for value in _values(item["models"], "models", limit=32)]
        return result
    if kind == "trace_search":
        item = _closed(value, {"kind", "window", "components", "outcomes", "pageSize"}, "trace search definition")
        if not isinstance(item["pageSize"], int) or isinstance(item["pageSize"], bool) or not 1 <= item["pageSize"] <= 200:
            raise SavedQueryError("page size is invalid")
        result = dict(item); result["window"] = _window(item["window"])
        result["components"] = [_component(value, "component") for value in _values(item["components"], "components", limit=32)]
        result["outcomes"] = _enum_values(item["outcomes"], "outcomes", limit=4, allowed=_OUTCOMES)
        return result
    if kind == "run_comparison":
        item = _closed(value, {"kind", "baselineRunId", "candidateRunId"}, "run comparison definition")
        result = dict(item); result["baselineRunId"] = _uuid(item["baselineRunId"], "baselineRunId"); result["candidateRunId"] = _uuid(item["candidateRunId"], "candidateRunId")
        if result["baselineRunId"] == result["candidateRunId"]: raise SavedQueryError("comparison runs must differ")
        return result
    if kind == "regression_search":
        item = _closed(value, {"kind", "window", "outcomes", "experimentReferences", "pageSize"}, "regression search definition")
        if not isinstance(item["pageSize"], int) or isinstance(item["pageSize"], bool) or not 1 <= item["pageSize"] <= 200: raise SavedQueryError("page size is invalid")
        result = dict(item); result["window"] = _window(item["window"]); result["outcomes"] = _enum_values(item["outcomes"], "outcomes", limit=6, allowed=_REGRESSION_OUTCOMES)
        refs = item["experimentReferences"]
        if not isinstance(refs, list) or len(refs) > 32: raise SavedQueryError("experiment references are invalid")
        result["experimentReferences"] = [{"resourceId": _uuid(r.get("resourceId"), "reference id"), "resourceVersion": _version(r.get("resourceVersion"), "reference version")} for r in refs if isinstance(r, Mapping)]
        if len(result["experimentReferences"]) != len(refs): raise SavedQueryError("experiment references are invalid")
        return result
    item = _closed(value, {"kind", "componentId", "componentVersion", "window", "pointLimit"}, "component health definition")
    if not isinstance(item["pointLimit"], int) or isinstance(item["pointLimit"], bool) or not 1 <= item["pointLimit"] <= 200: raise SavedQueryError("point limit is invalid")
    return {**dict(item), "componentId": _component(item["componentId"], "componentId"), "componentVersion": _safe(item["componentVersion"], "componentVersion"), "window": _window(item["window"])}


def canonical_definition(value: object) -> dict[str, object]:
    normalized = normalize_definition(value)
    return json.loads(json.dumps(normalized, sort_keys=True, separators=(",", ":")))


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, item in pairs:
        if key in result:
            raise ValueError("duplicate cursor key")
        result[key] = item
    return result


def encode_cursor(
    created_at: str,
    resource_id: str,
    version: str,
    *,
    snapshot_at: str | None = None,
    filter_hash: str | None = None,
    snapshot_revision: int | None = None,
) -> str:
    payload = {"v": 1, "createdAt": created_at, "resourceId": resource_id, "version": version}
    if snapshot_at is not None:
        payload["snapshotAt"] = snapshot_at
    if filter_hash is not None:
        payload["filterHash"] = filter_hash
    if snapshot_revision is not None:
        payload["snapshotRevision"] = snapshot_revision
    return "op1." + base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()).decode().rstrip("=")


def decode_cursor(value: str | None) -> dict[str, str] | None:
    if value is None: return None
    if not isinstance(value, str) or not value.startswith("op1.") or len(value) > 512: raise SavedQueryError("cursor is invalid")
    encoded_value = value[4:]
    if not encoded_value or _PUBLIC_CURSOR_RE.fullmatch(encoded_value) is None:
        raise SavedQueryError("cursor is invalid")
    try:
        encoded = encoded_value + "=" * (-len(encoded_value) % 4)
        raw = base64.b64decode(encoded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded_value:
            raise ValueError("non-canonical cursor")
        item = json.loads(raw.decode(), object_pairs_hook=_strict_json_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise SavedQueryError("cursor is invalid") from exc
    allowed = {"v", "createdAt", "resourceId", "version", "snapshotAt", "filterHash", "snapshotRevision"}
    required = {"v", "createdAt", "resourceId", "version"}
    if not isinstance(item, Mapping) or not required <= set(item) or not set(item) <= allowed or item["v"] != 1: raise SavedQueryError("cursor is invalid")
    result = {key: str(item[key]) for key in ("createdAt", "resourceId", "version")} | {key: str(item[key]) for key in ("snapshotAt", "filterHash") if key in item}
    if "snapshotRevision" in item:
        if not isinstance(item["snapshotRevision"], int) or isinstance(item["snapshotRevision"], bool) or item["snapshotRevision"] < 0:
            raise SavedQueryError("cursor is invalid")
        result["snapshotRevision"] = str(item["snapshotRevision"])
    return result


def encode_execution_cursor(kind: str, query_id: str, query_version: str, provider_cursor: str) -> str:
    if (
        kind not in {"run_search", "trace_search", "regression_search"}
        or not isinstance(provider_cursor, str)
        or not provider_cursor
        or len(provider_cursor) > 1024
    ):
        raise SavedQueryError("execution cursor is invalid")
    payload = {
        "v": 2,
        "kind": kind,
        "queryId": query_id,
        "queryVersion": query_version,
        "cursor": provider_cursor,
    }
    encoded = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    ).decode().rstrip("=")
    result = _EXECUTION_CURSOR_PREFIX + encoded
    if len(result) > 1024 or _PUBLIC_CURSOR_RE.fullmatch(result) is None:
        raise SavedQueryError("execution cursor is invalid")
    return result


def decode_execution_cursor(value: str | None, *, kind: str, query_id: str, query_version: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or len(value) > 1024:
        raise SavedQueryError("execution cursor is invalid")
    if value.startswith(_EXECUTION_CURSOR_PREFIX):
        prefix = _EXECUTION_CURSOR_PREFIX
        expected_version = 2
    elif value.startswith(_EXECUTION_CURSOR_LEGACY_PREFIX):
        prefix = _EXECUTION_CURSOR_LEGACY_PREFIX
        expected_version = 1
    else:
        raise SavedQueryError("execution cursor is invalid")
    encoded = value[len(prefix):]
    if not encoded or _PUBLIC_CURSOR_RE.fullmatch(encoded) is None:
        raise SavedQueryError("execution cursor is invalid")
    try:
        padded = encoded + "=" * (-len(encoded) % 4)
        raw = base64.b64decode(padded, altchars=b"-_", validate=True)
        if base64.urlsafe_b64encode(raw).decode().rstrip("=") != encoded:
            raise ValueError("non-canonical cursor")
        item = json.loads(raw.decode(), object_pairs_hook=_strict_json_object)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, binascii.Error) as exc:
        raise SavedQueryError("execution cursor is invalid") from exc
    if (
        not isinstance(item, Mapping)
        or set(item) != {"v", "kind", "queryId", "queryVersion", "cursor"}
        or item.get("v") != expected_version
        or item.get("kind") != kind
        or item.get("queryId") != query_id
        or item.get("queryVersion") != query_version
        or not isinstance(item.get("kind"), str)
        or not isinstance(item.get("queryId"), str)
        or not isinstance(item.get("queryVersion"), str)
    ):
        raise SavedQueryError("execution cursor is invalid")
    provider = item.get("cursor")
    if not isinstance(provider, str) or not provider or len(provider) > 1024:
        raise SavedQueryError("execution cursor is invalid")
    return provider


_RESULT_KEYS = {
    "analytics": {"snapshotAt", "query", "percentileMethod", "series", "breakdowns", "freshness", "partial", "partialReasons", "unavailableSignals", "scan", "capturePolicy"},
    "run_search": {"items", "nextCursor", "snapshotAt", "latestObservationId"},
    "trace_search": {"items", "nextCursor", "snapshotAt", "latestObservationId"},
    "run_comparison": {"snapshotAt", "left", "right", "markerCorrelations", "components", "trace", "freshness", "partial", "partialReasons", "unavailableMarkerCount", "scan", "limits", "capturePolicy"},
    "regression_search": {"items", "nextCursor", "snapshotAt", "partial"},
    "component_health": {"componentId", "summary", "points"},
}
_RUN_ITEM_KEYS = {"runId", "status", "startedAt", "finishedAt", "durationMs", "model", "deploymentId", "componentIds", "traceId"}
_SERIES_KEYS = {"seriesKey", "facets", "summary", "points", "availability", "partial"}
_SUMMARY_KEYS = {"sampleCount", "minimum", "maximum", "average", "p50", "p90", "p95", "p99", "ratePerInterval", "rateIntervalSeconds"}
_POINT_KEYS = {"startAt", "endAt", "value", "sampleCount", "availability", "partial"}
_REGRESSION_ITEM_KEYS = {"schemaVersion", "resourceType", "regressionId", "regressionVersion", "experiment", "baseline", "variant", "cohortIds", "baselineResultIds", "candidateResultIds", "comparisons", "decision", "evidenceState", "evidence", "attribution", "capturePolicy", "createdAt"}
_FACET_KEYS = {"component", "componentVersion", "deployment", "environment", "host", "serviceRole", "operation", "model", "provider", "outcome", "status", "errorClass", "retrievalStage", "regressionOutcome"}
_AVAILABILITY = {"available", "no_data", "unavailable"}
_FRESHNESS = {"fresh", "stale", "unavailable"}
_COMPARISON_AVAILABILITY = {"available", "missing", "unavailable"}
_COMPARISON_STATE = {"equal", "changed", "missing", "unavailable"}
_COMPARISON_ALIGNMENT = {"matched", "left_only", "right_only", "incompatible"}
_TRACE_INTEGRITY = {"complete", "retained_partial", "truncated", "missing_parent", "cyclic", "duplicate"}
_PARTIAL_REASONS = {"observation_limit", "span_limit", "measurement_limit", "scan_limit", "execution_limit", "late_data", "retention_boundary", "incomplete_run", "missing_parent", "cyclic", "duplicate_span"}
_UNAVAILABLE_SIGNALS = {"model_identity", "retryability", "input_cohort", "gpu", "selected_evidence", "side_effect_detail", "quality", "saturation", "measurement", "unit", "dimension"}
_EVALUATION_EVIDENCE_KINDS = {"run", "trace", "result", "score", "artifact_reference"}
_EVALUATION_BINDING_KINDS = {"component", "model", "deployment", "prompt", "policy", "configuration"}
_EVALUATION_METRIC_CLASSES = {"operational", "quality"}
_EVALUATION_UNITS = {"ratio", "count", "milliseconds", "bytes", "tokens", "operations", "per_second", "score"}
_EVALUATION_AGGREGATIONS = {"minimum", "maximum", "mean", "p50", "p95", "p99", "total"}
_EVALUATION_DIRECTIONS = {"higher_is_better", "lower_is_better"}
_EVALUATION_COMPARISON_MODES = {"absolute", "relative"}
_MISSING_EVIDENCE_POLICIES = {"regressed", "inconclusive", "not_applicable"}
_PRIVATE_VALUE = re.compile(r"(?:prompt|response|memory|document|filename|tool|credential|bearer|token|secret|password|endpoint|owner|identity|content|argument)", re.I)
# RevisionIdentifier is a closed metadata identifier.  The OpenAPI grammar
# deliberately permits the semantic segment ``prompt`` (for example,
# ``prompt-v1``); only content-bearing/private segments remain prohibited.
_REVISION_PATTERN = re.compile(
    r"^(?!(?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)(?:$|[._:+-]))"
    r"(?!.*[._:+-](?:owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)(?:$|[._:+-]))"
    r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$",
    re.I,
)
_PRIVATE_KEY = re.compile(r"(?:prompt|response|memory|document|filename|tool|credential|bearer|token|secret|password|endpoint|owner|identity|content|argument|payload|stacktrace|exception)", re.I)
_TRACE_ID = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID = re.compile(r"^[0-9a-f]{16}$")


def _safe_result(value: object, *, kind: str, depth: int = 0, allow_prompt_bundle: bool = False) -> bool:
    """Apply only recursive size/privacy guards before the exact projector.

    ``result`` is intentionally not a prohibited key: evaluation regression
    resources contain public ``baselineResultIds`` and ``candidateResultIds``.
    Closed per-kind projection below remains responsible for rejecting unknown
    fields and validating every scalar.
    """
    del kind
    if depth > 12:
        return False
    if isinstance(value, Mapping):
        if len(value) > 64 or any(not isinstance(key, str) or (key not in {"promptRevision", "promptBundleRevision"} and _PRIVATE_KEY.search(key)) for key in value):
            return False
        attribution_candidate = set(value) == {
            "rank", "bindingKind", "componentId", "beforeRevision",
            "afterRevision", "affectedCohortIds", "unaffectedCohortIds", "evidence",
        }
        return all(
            _safe_result(
                item,
                kind="",
                depth=depth + 1,
                allow_prompt_bundle=allow_prompt_bundle
                or key in {"promptBundleRevision", "promptRevision", "bindingKind"}
                or (attribution_candidate and key in {"beforeRevision", "afterRevision"}),
            )
            for key, item in value.items()
        )
    if isinstance(value, list):
        if len(value) > 10000:
            return False
        return all(_safe_result(item, kind="", depth=depth + 1, allow_prompt_bundle=allow_prompt_bundle) for item in value)
    if isinstance(value, (bytes, bytearray)):
        return False
    if not isinstance(value, str) or len(value) > 2048:
        return not isinstance(value, str)
    if allow_prompt_bundle and value == "prompt":
        # The only non-revision prompt value admitted is the exact closed
        # EvaluationAttributionCandidate.bindingKind enum.
        return True
    if allow_prompt_bundle:
        return _valid_revision(value)
    return not _PRIVATE_VALUE.search(value)


def _finite_number(value: object, *, minimum: float | None = None, maximum: float | None = None) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    number = float(value)
    return math.isfinite(number) and (minimum is None or number >= minimum) and (maximum is None or number <= maximum)


def _exact_mapping(value: object, required: set[str], allowed: set[str] | None = None) -> bool:
    if not isinstance(value, Mapping):
        return False
    keys = set(value)
    return required <= keys and keys <= (allowed or required)


def _one_of(value: object, allowed: set[str]) -> bool:
    return isinstance(value, str) and value in allowed


def _valid_uuid(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or _ID.fullmatch(value) is None:
        return False
    try:
        return _uuid(value, "identifier") == value
    except SavedQueryError:
        return False


def _valid_revision(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and _REVISION_PATTERN.fullmatch(value) is not None


def _valid_timestamp(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or re.fullmatch(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,9})?Z$", value) is None:
        return False
    try:
        _timestamp(value, "timestamp")
    except SavedQueryError:
        return False
    return True


def _valid_label(value: object, *, component: bool = False, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    if not isinstance(value, str) or _PRIVATE_VALUE.search(value):
        return False
    return (_COMPONENT_ID.fullmatch(value) if component else _SAFE_LABEL.fullmatch(value)) is not None


def _valid_identifier(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and re.fullmatch(r"^[a-z][a-z0-9_.-]{0,127}$", value) is not None and not _PRIVATE_VALUE.search(value)


def _valid_safe_label(value: object) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and _SAFE_LABEL.fullmatch(value) is not None and not _PRIVATE_VALUE.search(value)


def _valid_digest(value: object) -> bool:
    return isinstance(value, str) and re.fullmatch(r"^[0-9a-f]{64}$", value) is not None


def _valid_string_list(value: object, *, limit: int, validator: Any, minimum: int = 0, unique: bool = True) -> bool:
    if not isinstance(value, list) or not minimum <= len(value) <= limit or (unique and len(set(value)) != len(value)):
        return False
    return all(validator(item) for item in value)


def _valid_marker(value: object, *, model: bool = False, prompt_bundle: bool = False) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"availability", "value"} or value["availability"] not in _COMPARISON_AVAILABILITY:
        return False
    marker_value = value["value"]
    if value["availability"] != "available":
        return marker_value is None
    if model:
        try:
            _model(marker_value, "model marker")
            return True
        except SavedQueryError:
            return False
    if prompt_bundle:
        # promptBundleRevision is still a RevisionIdentifier.  The exact
        # wrapper is the only place where the otherwise privacy-sensitive
        # word "prompt" is admitted; it does not widen the identifier grammar
        # (notably, ':' is not a valid RevisionIdentifier character).
        return _valid_revision(marker_value)
    return _valid_revision(marker_value)


def _valid_comparison_number(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {"availability", "value"} and value["availability"] in _COMPARISON_AVAILABILITY and (value["value"] is None or _finite_number(value["value"], minimum=-1_000_000_000_000_000, maximum=1_000_000_000_000_000)) and (value["availability"] != "available" or value["value"] is not None)


def _valid_evaluator_reference(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {"evaluatorId", "evaluatorVersion", "kind"} and _valid_uuid(value["evaluatorId"]) and isinstance(value["evaluatorVersion"], str) and _VERSION.fullmatch(value["evaluatorVersion"]) is not None and value["kind"] in {"deterministic", "local_judge", "human_review"}


def _valid_evidence_reference(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {"kind", "id", "digest", "version"} and value["kind"] in _EVALUATION_EVIDENCE_KINDS and _valid_safe_label(value["id"]) and _valid_digest(value["digest"]) and isinstance(value["version"], str) and _VERSION.fullmatch(value["version"]) is not None


def _valid_integrity_value(value: object) -> bool:
    return isinstance(value, str) and value in _TRACE_INTEGRITY


def _valid_facet(name: object, value: object) -> bool:
    if not isinstance(name, str) or not isinstance(value, str) or _PRIVATE_VALUE.search(value):
        return False
    if name == "component":
        return _COMPONENT_ID.fullmatch(value) is not None
    if name == "serviceRole":
        return value in {"api", "worker", "scheduler", "cli"}
    if name == "outcome":
        return value in _OUTCOMES
    if name == "regressionOutcome":
        return value in _REGRESSION_OUTCOMES
    if name == "operation":
        return re.fullmatch(r"^[a-z][a-z0-9_.-]{0,95}$", value) is not None
    if name == "environment":
        return re.fullmatch(r"^[a-z][a-z0-9_.-]{0,31}$", value) is not None
    if name == "model":
        try:
            _model(value, "model")
            return True
        except SavedQueryError:
            return False
    return _SAFE_LABEL.fullmatch(value) is not None


def _valid_cursor(value: object) -> bool:
    return value is None or (isinstance(value, str) and 16 <= len(value) <= 1024 and re.fullmatch(r"^[A-Za-z0-9_-]+$", value) is not None)


def _valid_run_item(value: object, *, require_trace: bool = False) -> bool:
    if not _exact_mapping(value, _RUN_ITEM_KEYS):
        return False
    assert isinstance(value, Mapping)
    duration_valid = value["durationMs"] is None or _finite_number(value["durationMs"], minimum=0)
    trace_valid = value["traceId"] is None or (isinstance(value["traceId"], str) and _TRACE_ID.fullmatch(value["traceId"]) is not None)
    components = value["componentIds"]
    components_valid = isinstance(components, list) and len(components) <= 256 and all(isinstance(item, str) and _valid_label(item, component=True) for item in components) and len(set(components)) == len(components)
    return (_valid_uuid(value["runId"]) and _one_of(value["status"], _RUN_STATUSES) and _valid_timestamp(value["startedAt"]) and _valid_timestamp(value["finishedAt"], nullable=True) and duration_valid and _valid_label(value["model"], nullable=True) and _valid_label(value["deploymentId"]) and components_valid and trace_valid and (not require_trace or isinstance(value["traceId"], str)))


def _valid_analytics(value: Mapping[str, object], definition: Mapping[str, object] | None = None) -> bool:
    required = _RESULT_KEYS["analytics"]
    if set(value) != required or not _valid_timestamp(value["snapshotAt"]) or value["percentileMethod"] != "clickhouse_quantile_exact" or value["capturePolicy"] != "metadata_only" or not isinstance(value["partial"], bool):
        return False
    query = value["query"]
    query_keys = {"metric", "unit", "aggregation", "focus", "from", "to", "bucket", "bucketSeconds", "groupBy", "filters", "seriesLimit", "pointLimit"}
    if not _exact_mapping(query, query_keys) or not isinstance(query, Mapping) or not _one_of(query["metric"], _METRICS) or not _one_of(query["unit"], _UNITS) or not _one_of(query["aggregation"], _AGGREGATIONS) or not _one_of(query["focus"], _FOCUSES) or not _one_of(query["bucket"], _BUCKETS) or not _one_of(query["groupBy"], _GROUPINGS) or not _valid_timestamp(query["from"]) or not _valid_timestamp(query["to"]) or not isinstance(query["bucketSeconds"], int) or isinstance(query["bucketSeconds"], bool) or not 60 <= query["bucketSeconds"] <= 86400 or not isinstance(query["seriesLimit"], int) or isinstance(query["seriesLimit"], bool) or not 1 <= query["seriesLimit"] <= 12 or not isinstance(query["pointLimit"], int) or isinstance(query["pointLimit"], bool) or not 1 <= query["pointLimit"] <= 200:
        return False
    filters = query["filters"]
    analytics_facet_keys = {"component", "componentVersion", "operation", "deployment", "environment", "host", "serviceRole", "provider", "outcome", "errorClass", "status", "retrievalStage"}
    if not isinstance(filters, Mapping) or set(filters) - analytics_facet_keys or any(not _valid_facet(name, item) for name, item in filters.items()):
        return False
    if isinstance(definition, Mapping):
        for key in ("metric", "unit", "aggregation", "bucket", "groupBy", "focus"):
            if query[key] != definition.get(key):
                return False
        facet_names = {
            "component_version": "componentVersion",
            "service_role": "serviceRole",
            "error_class": "errorClass",
            "retrieval_stage": "retrievalStage",
        }
        expected_filters = {
            facet_names.get(str(item["name"]), str(item["name"])): str(item["value"])
            for item in definition.get("facets", [])
            if isinstance(item, Mapping) and facet_names.get(str(item.get("name")), str(item.get("name"))) in analytics_facet_keys
        }
        if dict(filters) != expected_filters:
            return False
    effective_series_limit = int(definition["seriesLimit"]) if isinstance(definition, Mapping) and isinstance(definition.get("seriesLimit"), int) else 12
    effective_point_limit = int(definition["pointLimit"]) if isinstance(definition, Mapping) and isinstance(definition.get("pointLimit"), int) else 200
    if query["seriesLimit"] != effective_series_limit or query["pointLimit"] != effective_point_limit:
        return False
    series = value["series"]
    if not isinstance(series, list) or len(series) > effective_series_limit:
        return False
    for item in series:
        if not _exact_mapping(item, _SERIES_KEYS) or not isinstance(item, Mapping) or not isinstance(item["seriesKey"], str) or not 1 <= len(item["seriesKey"]) <= 256 or re.fullmatch(r"^[A-Za-z0-9._:+-]+$", item["seriesKey"]) is None or not _one_of(item["availability"], _AVAILABILITY) or not isinstance(item["partial"], bool):
            return False
        facets = item["facets"]
        if not isinstance(facets, Mapping) or set(facets) - analytics_facet_keys or any(not _valid_facet(name, item) for name, item in facets.items()):
            return False
        summary = item["summary"]
        if not _exact_mapping(summary, _SUMMARY_KEYS) or not isinstance(summary, Mapping) or not isinstance(summary["sampleCount"], int) or isinstance(summary["sampleCount"], bool) or not 0 <= summary["sampleCount"] <= 9007199254740991:
            return False
        if any(v is not None and not _finite_number(v, minimum=0 if k == "ratePerInterval" else None) for k, v in summary.items() if k not in {"sampleCount", "rateIntervalSeconds"}) or (summary["rateIntervalSeconds"] is not None and (not isinstance(summary["rateIntervalSeconds"], int) or isinstance(summary["rateIntervalSeconds"], bool) or not 60 <= summary["rateIntervalSeconds"] <= 86400)):
            return False
        points = item["points"]
        if not isinstance(points, list) or len(points) > effective_point_limit:
            return False
        for point in points:
            if not _exact_mapping(point, _POINT_KEYS) or not isinstance(point, Mapping) or not _valid_timestamp(point["startAt"]) or not _valid_timestamp(point["endAt"]) or not _one_of(point["availability"], _AVAILABILITY) or not isinstance(point["partial"], bool) or not isinstance(point["sampleCount"], int) or isinstance(point["sampleCount"], bool) or not 0 <= point["sampleCount"] <= 9007199254740991 or (point["value"] is not None and not _finite_number(point["value"], minimum=-1_000_000_000_000_000, maximum=1_000_000_000_000_000)):
                return False
    if not isinstance(value["breakdowns"], list) or len(value["breakdowns"]) > 50 or any(not isinstance(item, Mapping) or set(item) != {"groupBy", "value", "count", "partial"} or not _one_of(item["groupBy"], _GROUPINGS) or not _valid_label(item["value"]) or not isinstance(item["count"], int) or isinstance(item["count"], bool) or item["count"] < 0 or not isinstance(item["partial"], bool) for item in value["breakdowns"]):
        return False
    freshness = value["freshness"]
    if not isinstance(freshness, Mapping) or set(freshness) != {"latestObservedAt", "lagMs", "state"} or freshness["state"] not in _FRESHNESS or not _valid_timestamp(freshness["latestObservedAt"], nullable=True) or (freshness["lagMs"] is not None and not _finite_number(freshness["lagMs"], minimum=0, maximum=604800000)):
        return False
    if not isinstance(value["partialReasons"], list) or len(value["partialReasons"]) > 6 or any(not isinstance(item, str) or item not in {"scan_limit", "execution_limit", "series_limit", "point_limit", "late_data", "retention_boundary"} for item in value["partialReasons"]) or len(set(value["partialReasons"])) != len(value["partialReasons"]):
        return False
    if not isinstance(value["unavailableSignals"], list) or len(value["unavailableSignals"]) > 10 or any(item not in _UNAVAILABLE_SIGNALS for item in value["unavailableSignals"]) or len(set(value["unavailableSignals"])) != len(value["unavailableSignals"]):
        return False
    scan = value["scan"]
    return isinstance(scan, Mapping) and set(scan) == {"scannedRows", "scanLimit", "executionMs", "executionLimitMs"} and isinstance(scan["scannedRows"], int) and not isinstance(scan["scannedRows"], bool) and 0 <= scan["scannedRows"] <= 1000000 and scan["scanLimit"] == 1000000 and _finite_number(scan["executionMs"], minimum=0, maximum=5000) and scan["executionLimitMs"] == 5000


def _valid_regression(value: object) -> bool:
    if not _exact_mapping(value, _REGRESSION_ITEM_KEYS) or not isinstance(value, Mapping):
        return False
    if not isinstance(value["schemaVersion"], int) or isinstance(value["schemaVersion"], bool) or value["schemaVersion"] != 1 or value["resourceType"] != "regression_decision" or not _valid_uuid(value["regressionId"]) or not isinstance(value["regressionVersion"], str) or _VERSION.fullmatch(value["regressionVersion"]) is None or not _one_of(value["decision"], _REGRESSION_OUTCOMES) or not _one_of(value["evidenceState"], {"complete", "partial", "insufficient", "missing", "incompatible", "late"}) or value["capturePolicy"] != "metadata_only" or not _valid_timestamp(value["createdAt"]):
        return False
    for name in ("experiment", "baseline"):
        ref = value[name]
        if not isinstance(ref, Mapping) or set(ref) != {"resourceId", "resourceVersion"} or not _valid_uuid(ref["resourceId"]) or not isinstance(ref["resourceVersion"], str) or _VERSION.fullmatch(ref["resourceVersion"]) is None:
            return False
    variant = value["variant"]
    if not isinstance(variant, Mapping) or set(variant) != {"variantId", "components", "model", "configurationRevision", "promptRevision", "policyRevision", "deploymentRevision"} or not _valid_identifier(variant["variantId"]):
        return False
    if not all(_valid_revision(variant[name]) for name in ("configurationRevision", "promptRevision", "policyRevision", "deploymentRevision")):
        return False
    components = variant["components"]
    if not isinstance(components, list) or not 1 <= len(components) <= 32:
        return False
    for component in components:
        if not isinstance(component, Mapping) or set(component) != {"componentId", "componentVersion", "codeRevision", "imageDigest", "configurationRevision"} or not isinstance(component["componentId"], str) or _COMPONENT_ID.fullmatch(component["componentId"]) is None or not isinstance(component["componentVersion"], str) or _VERSION.fullmatch(component["componentVersion"]) is None or not _valid_revision(component["codeRevision"]) or not _valid_digest(component["imageDigest"]) or not _valid_revision(component["configurationRevision"]):
            return False
    model = variant["model"]
    if not isinstance(model, Mapping) or set(model) != {"modelId", "modelVersion", "modelDigest"} or not _valid_identifier(model["modelId"]) or not isinstance(model["modelVersion"], str) or _VERSION.fullmatch(model["modelVersion"]) is None or not _valid_digest(model["modelDigest"]):
        return False
    if not _valid_string_list(value["cohortIds"], limit=64, minimum=1, validator=_valid_identifier):
        return False
    for name in ("baselineResultIds", "candidateResultIds"):
        if not isinstance(value[name], list) or not 1 <= len(value[name]) <= 100000 or len(set(value[name])) != len(value[name]) or any(not _valid_uuid(item) for item in value[name]):
            return False
    comparisons = value["comparisons"]
    comparison_keys = {"metric", "metricClass", "evaluator", "unit", "aggregation", "direction", "comparisonMode", "baselineValue", "candidateValue", "difference", "relativeChange", "thresholdValue", "minimumSampleSize", "baselineSampleSize", "candidateSampleSize", "missingEvidencePolicy", "outcome"}
    if not isinstance(comparisons, list) or not 1 <= len(comparisons) <= 128:
        return False
    for comparison in comparisons:
        if not isinstance(comparison, Mapping) or set(comparison) != comparison_keys or not _valid_identifier(comparison["metric"]) or comparison["metricClass"] not in _EVALUATION_METRIC_CLASSES or not _valid_evaluator_reference(comparison["evaluator"]) or comparison["unit"] not in _EVALUATION_UNITS or comparison["aggregation"] not in _EVALUATION_AGGREGATIONS or comparison["direction"] not in _EVALUATION_DIRECTIONS or comparison["comparisonMode"] not in _EVALUATION_COMPARISON_MODES or not _finite_number(comparison["baselineValue"], minimum=-1_000_000_000_000_000, maximum=1_000_000_000_000_000) or not _finite_number(comparison["candidateValue"], minimum=-1_000_000_000_000_000, maximum=1_000_000_000_000_000) or (comparison["difference"] is not None and not _finite_number(comparison["difference"], minimum=-1_000_000_000_000_000, maximum=1_000_000_000_000_000)) or (comparison["relativeChange"] is not None and not _finite_number(comparison["relativeChange"], minimum=-1_000_000, maximum=1_000_000)) or not _finite_number(comparison["thresholdValue"], minimum=0, maximum=1_000_000_000_000_000) or not isinstance(comparison["minimumSampleSize"], int) or isinstance(comparison["minimumSampleSize"], bool) or not 1 <= comparison["minimumSampleSize"] <= 100000 or not isinstance(comparison["baselineSampleSize"], int) or isinstance(comparison["baselineSampleSize"], bool) or not 0 <= comparison["baselineSampleSize"] <= 100000 or not isinstance(comparison["candidateSampleSize"], int) or isinstance(comparison["candidateSampleSize"], bool) or not 0 <= comparison["candidateSampleSize"] <= 100000 or comparison["missingEvidencePolicy"] not in _MISSING_EVIDENCE_POLICIES or comparison["outcome"] not in _REGRESSION_OUTCOMES:
            return False
    if not isinstance(value["evidence"], list) or len(value["evidence"]) > 512 or any(not _valid_evidence_reference(item) for item in value["evidence"]):
        return False
    attribution = value["attribution"]
    attribution_keys = {"attributionId", "attributionVersion", "regression", "availability", "candidates", "capturePolicy", "createdAt"}
    if not isinstance(attribution, Mapping) or set(attribution) != attribution_keys or not _valid_uuid(attribution["attributionId"]) or not isinstance(attribution["attributionVersion"], str) or _VERSION.fullmatch(attribution["attributionVersion"]) is None or not isinstance(attribution["regression"], Mapping) or set(attribution["regression"]) != {"resourceId", "resourceVersion"} or not _valid_uuid(attribution["regression"]["resourceId"]) or not isinstance(attribution["regression"]["resourceVersion"], str) or _VERSION.fullmatch(attribution["regression"]["resourceVersion"]) is None or attribution["availability"] not in {"available", "unavailable", "insufficient_evidence", "incompatible"} or not isinstance(attribution["candidates"], list) or len(attribution["candidates"]) > 64 or attribution["capturePolicy"] != "metadata_only" or not _valid_timestamp(attribution["createdAt"]):
        return False
    candidate_keys = {"rank", "bindingKind", "componentId", "beforeRevision", "afterRevision", "affectedCohortIds", "unaffectedCohortIds", "evidence"}
    for candidate in attribution["candidates"]:
        if not isinstance(candidate, Mapping) or set(candidate) != candidate_keys or not isinstance(candidate["rank"], int) or isinstance(candidate["rank"], bool) or not 1 <= candidate["rank"] <= 64 or candidate["bindingKind"] not in _EVALUATION_BINDING_KINDS or (candidate["componentId"] is not None and (not isinstance(candidate["componentId"], str) or _COMPONENT_ID.fullmatch(candidate["componentId"]) is None)) or not _valid_revision(candidate["beforeRevision"]) or not _valid_revision(candidate["afterRevision"]) or not _valid_string_list(candidate["affectedCohortIds"], limit=64, minimum=1, validator=_valid_identifier) or not _valid_string_list(candidate["unaffectedCohortIds"], limit=64, validator=_valid_identifier) or not isinstance(candidate["evidence"], list) or not 1 <= len(candidate["evidence"]) <= 128 or any(not _valid_evidence_reference(item) for item in candidate["evidence"]):
            return False
    return True


def _valid_health_summary(value: object) -> bool:
    required = {"sampleCount", "latencyP50Ms", "latencyP95Ms", "errorRate", "outcomes"}
    if not _exact_mapping(value, required) or not isinstance(value, Mapping) or not isinstance(value["sampleCount"], int) or isinstance(value["sampleCount"], bool) or not 0 <= value["sampleCount"] <= 9007199254740991:
        return False
    if any(item is not None and not _finite_number(item, minimum=0) for item in (value["latencyP50Ms"], value["latencyP95Ms"])):
        return False
    if value["errorRate"] is not None and not _finite_number(value["errorRate"], minimum=0, maximum=1):
        return False
    outcomes = value["outcomes"]
    return isinstance(outcomes, Mapping) and set(outcomes) == {"succeeded", "failed", "cancelled", "unknown"} and all(isinstance(item, int) and not isinstance(item, bool) and item >= 0 for item in outcomes.values())


def _valid_component_health(value: Mapping[str, object], definition: Mapping[str, object] | None = None) -> bool:
    component_id = value.get("componentId")
    if not isinstance(component_id, str) or _COMPONENT_ID.fullmatch(component_id) is None or not _valid_health_summary(value.get("summary")):
        return False
    points = value.get("points")
    point_limit = int(definition["pointLimit"]) if isinstance(definition, Mapping) and isinstance(definition.get("pointLimit"), int) else 200
    if not isinstance(points, list) or len(points) > point_limit:
        return False
    for point in points:
        if not _exact_mapping(point, {"startAt", "endAt", "summary"}) or not isinstance(point, Mapping) or not _valid_timestamp(point["startAt"]) or not _valid_timestamp(point["endAt"]) or not _valid_health_summary(point["summary"]):
            return False
    return True


def _valid_marker_comparison(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"marker", "state", "left", "right"} or value["state"] not in _COMPARISON_STATE:
        return False
    marker = value["marker"]
    if marker == "model_id":
        return _valid_marker(value["left"], model=True) and _valid_marker(value["right"], model=True)
    return marker in {"deployment_id", "git_commit", "image_version", "configuration_version", "model_version", "agent_revision", "prompt_bundle_revision", "policy_revision"} and _valid_marker(value["left"], prompt_bundle=marker == "prompt_bundle_revision") and _valid_marker(value["right"], prompt_bundle=marker == "prompt_bundle_revision")


def _valid_comparable_span(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {"spanId", "parentSpanId", "componentId", "componentVersion", "operation", "startedAt", "durationMs", "outcome", "partial"} and isinstance(value["spanId"], str) and _SPAN_ID.fullmatch(value["spanId"]) is not None and (value["parentSpanId"] is None or (isinstance(value["parentSpanId"], str) and _SPAN_ID.fullmatch(value["parentSpanId"]) is not None)) and isinstance(value["componentId"], str) and _COMPONENT_ID.fullmatch(value["componentId"]) is not None and _valid_safe_label(value["componentVersion"]) and isinstance(value["operation"], str) and re.fullmatch(r"^[a-z][a-z0-9_.-]{0,95}$", value["operation"]) is not None and _valid_timestamp(value["startedAt"]) and _finite_number(value["durationMs"], minimum=0, maximum=604800000) and value["outcome"] in _OUTCOMES and isinstance(value["partial"], bool)


def _valid_aligned_span(value: object) -> bool:
    return isinstance(value, Mapping) and set(value) == {"alignmentKey", "alignment", "left", "right", "leftIntegrity", "rightIntegrity"} and isinstance(value["alignmentKey"], str) and 1 <= len(value["alignmentKey"]) <= 512 and re.fullmatch(r"^[A-Za-z0-9._:+-]+$", value["alignmentKey"]) is not None and value["alignment"] in _COMPARISON_ALIGNMENT and (value["left"] is None or _valid_comparable_span(value["left"])) and (value["right"] is None or _valid_comparable_span(value["right"])) and _valid_string_list(value["leftIntegrity"], limit=6, minimum=1, validator=_valid_integrity_value) and _valid_string_list(value["rightIntegrity"], limit=6, minimum=1, validator=_valid_integrity_value)


def _valid_component_comparison(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != {"componentId", "operation", "alignment", "left", "right", "measurements"} or not isinstance(value["componentId"], str) or _COMPONENT_ID.fullmatch(value["componentId"]) is None or not isinstance(value["operation"], str) or re.fullmatch(r"^[a-z][a-z0-9_.-]{0,95}$", value["operation"]) is None or value["alignment"] not in _COMPARISON_ALIGNMENT or not isinstance(value["measurements"], list) or len(value["measurements"]) > 128:
        return False
    for side in (value["left"], value["right"]):
        if side is not None and (not isinstance(side, Mapping) or set(side) != {"componentVersion", "durationMs", "outcome", "spanCount", "partial"} or not _valid_marker(side["componentVersion"]) or (side["durationMs"] is not None and not _finite_number(side["durationMs"], minimum=0, maximum=604800000)) or side["outcome"] not in _OUTCOMES or not isinstance(side["spanCount"], int) or isinstance(side["spanCount"], bool) or not 0 <= side["spanCount"] <= 5000 or not isinstance(side["partial"], bool)):
            return False
    for measurement in value["measurements"]:
        keys = {"name", "unit", "state", "left", "right", "absoluteDelta"}
        if not isinstance(measurement, Mapping) or set(measurement) != keys or not isinstance(measurement["name"], str) or re.fullmatch(r"^[a-z][a-z0-9_.-]{0,95}$", measurement["name"]) is None or measurement["unit"] not in _UNITS or measurement["state"] not in _COMPARISON_STATE or not _valid_comparison_number(measurement["left"]) or not _valid_comparison_number(measurement["right"]) or (measurement["absoluteDelta"] is not None and not _finite_number(measurement["absoluteDelta"], minimum=0, maximum=2_000_000_000_000_000)):
            return False
    return True


def _valid_comparison(value: Mapping[str, object]) -> bool:
    if not _valid_timestamp(value.get("snapshotAt")) or not isinstance(value.get("partial"), bool) or value.get("capturePolicy") != "metadata_only":
        return False
    side_keys = {"role", "runId", "evidence", "status", "outcome", "startedAt", "finishedAt", "durationMs", "traceId", "modelId", "modelVersion", "deploymentId", "gitCommit", "imageVersion", "configurationVersion", "agentRevision", "promptBundleRevision", "policyRevision", "componentVersions", "partial"}
    for side, role in ((value.get("left"), "left"), (value.get("right"), "right")):
        if not isinstance(side, Mapping) or set(side) != side_keys or side["role"] != role or not _valid_uuid(side["runId"]) or side["evidence"] not in _COMPARISON_AVAILABILITY or side["status"] not in _RUN_STATUSES or side["outcome"] not in _OUTCOMES or not _valid_timestamp(side["startedAt"], nullable=True) or not _valid_timestamp(side["finishedAt"], nullable=True) or (side["durationMs"] is not None and not _finite_number(side["durationMs"], minimum=0, maximum=604800000)) or (side["traceId"] is not None and (not isinstance(side["traceId"], str) or _TRACE_ID.fullmatch(side["traceId"]) is None)) or not _valid_marker(side["modelId"], model=True) or not _valid_marker(side["modelVersion"]) or not _valid_marker(side["deploymentId"]) or not _valid_marker(side["gitCommit"]) or not _valid_marker(side["imageVersion"]) or not _valid_marker(side["configurationVersion"]) or not _valid_marker(side["agentRevision"]) or not _valid_marker(side["promptBundleRevision"], prompt_bundle=True) or not _valid_marker(side["policyRevision"]) or not isinstance(side["componentVersions"], list) or len(side["componentVersions"]) > 256 or any(not isinstance(component, Mapping) or set(component) != {"componentId", "version"} or not isinstance(component["componentId"], str) or _COMPONENT_ID.fullmatch(component["componentId"]) is None or not _valid_marker(component["version"]) for component in side["componentVersions"]) or not isinstance(side["partial"], bool):
            return False
    trace = value["trace"]
    trace_keys = {"leftTraceId", "rightTraceId", "leftEvidence", "rightEvidence", "leftIntegrity", "rightIntegrity", "alignedSpans", "partial"}
    if not isinstance(trace, Mapping) or set(trace) != trace_keys or any(item is not None and (not isinstance(item, str) or _TRACE_ID.fullmatch(item) is None) for item in (trace["leftTraceId"], trace["rightTraceId"])) or trace["leftEvidence"] not in _COMPARISON_AVAILABILITY or trace["rightEvidence"] not in _COMPARISON_AVAILABILITY or not _valid_string_list(trace["leftIntegrity"], limit=6, minimum=1, validator=_valid_integrity_value) or not _valid_string_list(trace["rightIntegrity"], limit=6, minimum=1, validator=_valid_integrity_value) or not isinstance(trace["alignedSpans"], list) or len(trace["alignedSpans"]) > 10000 or any(not _valid_aligned_span(span) for span in trace["alignedSpans"]) or not isinstance(trace["partial"], bool):
        return False
    if not isinstance(value["markerCorrelations"], list) or len(value["markerCorrelations"]) > 9 or any(not _valid_marker_comparison(item) for item in value["markerCorrelations"]):
        return False
    if not isinstance(value["components"], list) or len(value["components"]) > 512 or any(not _valid_component_comparison(item) for item in value["components"]):
        return False
    limits = value["limits"]
    expected_limits = {"runCount", "maximumObservationsPerRun", "maximumSpansPerRun", "maximumMeasurements", "maximumScannedRows", "executionTimeoutMs"}
    if not isinstance(limits, Mapping) or set(limits) != expected_limits or limits["runCount"] != 2 or limits["maximumObservationsPerRun"] != 10000 or limits["maximumSpansPerRun"] != 5000 or limits["maximumMeasurements"] != 2000 or limits["maximumScannedRows"] != 1000000 or limits["executionTimeoutMs"] != 5000:
        return False
    scan = value["scan"]
    if not isinstance(scan, Mapping) or set(scan) != {"scannedRows", "scanLimit", "executionMs", "executionLimitMs"} or scan["scanLimit"] != 1000000 or scan["executionLimitMs"] != 5000 or not isinstance(scan["scannedRows"], int) or isinstance(scan["scannedRows"], bool) or not 0 <= scan["scannedRows"] <= 1000000 or not _finite_number(scan["executionMs"], minimum=0, maximum=5000):
        return False
    freshness = value["freshness"]
    return isinstance(freshness, Mapping) and set(freshness) == {"latestObservedAt", "lagMs", "state"} and _one_of(freshness["state"], _FRESHNESS) and _valid_timestamp(freshness["latestObservedAt"], nullable=True) and (freshness["lagMs"] is None or _finite_number(freshness["lagMs"], minimum=0, maximum=604800000)) and isinstance(value["partialReasons"], list) and len(value["partialReasons"]) <= 11 and len(set(value["partialReasons"])) == len(value["partialReasons"]) and all(item in _PARTIAL_REASONS for item in value["partialReasons"])


def validate_execution_result(kind: str, result: object, definition: Mapping[str, object] | None = None) -> dict[str, object] | None:
    """Project provider output through the exact closed metadata schemas."""
    if kind not in _RESULT_KEYS or not isinstance(result, Mapping) or not _safe_result(result, kind=kind) or set(result) != _RESULT_KEYS[kind]:
        return None
    if kind in {"run_search", "trace_search"}:
        max_items = int(definition["pageSize"]) if isinstance(definition, Mapping) and isinstance(definition.get("pageSize"), int) else 200
        if not _valid_timestamp(result["snapshotAt"]) or not _valid_cursor(result["nextCursor"]) or not _valid_uuid(result["latestObservationId"], nullable=True) or not isinstance(result["items"], list) or len(result["items"]) > max_items:
            return None
        if any(not _valid_run_item(item, require_trace=kind == "trace_search") for item in result["items"]):
            return None
    elif kind == "analytics":
        if not _valid_analytics(result, definition):
            return None
    elif kind == "regression_search":
        max_items = int(definition["pageSize"]) if isinstance(definition, Mapping) and isinstance(definition.get("pageSize"), int) else 200
        if not _valid_timestamp(result["snapshotAt"]) or not _valid_cursor(result["nextCursor"]) or not isinstance(result["partial"], bool) or not isinstance(result["items"], list) or len(result["items"]) > max_items or any(not _valid_regression(item) for item in result["items"]):
            return None
    elif kind == "component_health":
        if not _valid_component_health(result, definition):
            return None
    elif kind == "run_comparison":
        if not _valid_comparison(result) or not isinstance(result["components"], list) or len(result["components"]) > 512 or not isinstance(result["markerCorrelations"], list) or len(result["markerCorrelations"]) > 9 or not isinstance(result["partialReasons"], list) or len(result["partialReasons"]) > 11 or any(not isinstance(item, str) for item in result["partialReasons"]) or not isinstance(result["unavailableMarkerCount"], int) or isinstance(result["unavailableMarkerCount"], bool) or not 0 <= result["unavailableMarkerCount"] <= 18:
            return None
    return dict(result)


class SavedQueryService:
    def __init__(self, repository: Any, metrics: Any | None = None) -> None:
        self.repository = repository
        self.metrics = metrics

    def _record(self, operation: str, started: float, outcome: str, dependency: str | None = "postgresql", error_code: str | None = None) -> None:
        record = getattr(self.metrics, "record", None)
        if callable(record): record("observatory_saved_query_operation_count", 1.0, {"operation": operation, "outcome": outcome})
        span = getattr(self.metrics, "record_component_span", None)
        if callable(span): span(component_id="aura.observatory.saved_queries", component_version="1.0.0", deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")), operation=operation, duration_ms=max(0.001, (monotonic() - started) * 1000.0), outcome=outcome, dependency=dependency, error_code=error_code or ("database_failure" if outcome == "failure" and dependency else None))

    async def create(self, request: Mapping[str, object], *, command_id: str | None = None) -> dict[str, object]:
        started = monotonic()
        if set(request) != {"name", "definition", "createdAt"} or not isinstance(request["name"], str) or not 1 <= len(request["name"]) <= 120:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise SavedQueryError("saved query request is invalid")
        try:
            definition = canonical_definition(request["definition"])
        except Exception:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise
        created = _timestamp(request["createdAt"], "createdAt")
        _version("1.0.0", "query version")
        query = {"queryId": str(uuid4()), "queryVersion": "1.0.0", "name": request["name"], "definition": definition, "createdAt": created}
        started = monotonic()
        try:
            result = await self.repository.create_resource("saved_query", query, command_id=command_id)
        except RuntimeError:
            self._record("write", started, "failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("write", started, "failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("write", started, "failure", error_code="timeout")
            raise
        except Exception:
            self._record("write", started, "failure")
            raise
        self._record("write", started, "success")
        return result

    async def list(self, *, cursor: str | None = None, limit: int = 50, kind: str | None = None) -> dict[str, object]:
        started = monotonic()
        if not 1 <= limit <= 200:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise SavedQueryError("page size is invalid")
        try:
            decode_cursor(cursor)
        except SavedQueryError:
            self._record("validate", started, "failure", dependency=None, error_code="validation")
            raise
        try:
            result = await self.repository.list_resources("saved_query", cursor=cursor, limit=limit, kind=kind)
        except RuntimeError:
            self._record("read", started, "failure", error_code="conflict")
            raise
        except ConnectionError:
            self._record("read", started, "failure", error_code="transport_unavailable")
            raise
        except TimeoutError:
            self._record("read", started, "failure", error_code="timeout")
            raise
        except Exception:
            self._record("read", started, "failure")
            raise
        self._record("read", started, "success")
        return result


class SavedQueryExecutionService:
    """Execute one persisted query through the narrow Observatory query ports.

    This service deliberately accepts no caller supplied query structure.  The
    exact version is loaded from the operations facade and is the sole source
    of execution parameters; provider adapters remain behind their public
    query/evaluation ports.
    """

    def __init__(
        self,
        repository: Any,
        *,
        query_port: Any | None = None,
        evaluation_port: Any | None = None,
        metrics: Any | None = None,
    ) -> None:
        self.repository = repository
        self.query_port = query_port
        self.evaluation_port = evaluation_port
        self.metrics = metrics

    @staticmethod
    def _window_bounds(window: str) -> tuple[datetime, datetime]:
        durations = {
            "fifteen_minutes": timedelta(minutes=15),
            "one_hour": timedelta(hours=1),
            "six_hours": timedelta(hours=6),
            "twenty_four_hours": timedelta(hours=24),
            "seven_days": timedelta(days=7),
            "thirty_one_days": timedelta(days=31),
        }
        to_time = datetime.now(UTC)
        return to_time - durations[window], to_time

    @staticmethod
    def _freshness(result: Mapping[str, object] | None, snapshot_at: str) -> dict[str, object]:
        source = result.get("freshness") if isinstance(result, Mapping) else None
        if not isinstance(source, Mapping):
            return {"state": "fresh", "measuredAt": snapshot_at, "lagMs": 0}
        state = source.get("state")
        if state not in {"fresh", "stale", "unavailable"}:
            state = "unavailable"
        measured = source.get("measuredAt")
        lag = source.get("lagMs")
        if state == "unavailable":
            return {"state": state, "measuredAt": None, "lagMs": None}
        if not isinstance(measured, str):
            measured = snapshot_at
        if not isinstance(lag, (int, float)) or lag < 0 or lag > 604_800_000:
            lag = 0
        return {"state": state, "measuredAt": measured, "lagMs": lag}

    @staticmethod
    def _state(result: Mapping[str, object] | None) -> tuple[str, bool]:
        partial = bool(result.get("partial")) if isinstance(result, Mapping) else False
        return ("partial" if partial else "complete"), partial

    async def _run_search(self, definition: Mapping[str, object], cursor: str | None) -> dict[str, object]:
        from aura_observatory.query import RunFilters

        if self.query_port is None:
            raise RuntimeError("query provider unavailable")
        statuses = list(definition["statuses"])
        components = list(definition["components"])
        deployments = list(definition["deployments"])
        models = list(definition["models"])
        from_time, to_time = self._window_bounds(str(definition["window"]))
        # RunFilters is deliberately scalar.  Do not fan out the caller's
        # bounded lists into a Cartesian product: request one bounded provider
        # page, carry its opaque continuation, and apply the closed finite
        # disjunction locally.  The provider's run snapshot keeps page-two
        # replay stable and the call count is independent of list cardinality.
        filters = RunFilters(
            status=statuses[0] if len(statuses) == 1 else None,
            component=components[0] if len(components) == 1 else None,
            deployment=deployments[0] if len(deployments) == 1 else None,
            model=models[0] if len(models) == 1 else None,
            from_time=from_time,
            to_time=to_time,
        )
        if callable(getattr(self.query_port, "runs", None)):
            page = await self.query_port.runs(filters, cursor=cursor, limit=int(definition["pageSize"]))
        else:
            page = await self.query_port.run_query(filters, cursor=cursor, limit=int(definition["pageSize"]))
        items = page.get("items", []) if isinstance(page, Mapping) else []
        if isinstance(items, list):
            def matches(item: object) -> bool:
                if not isinstance(item, Mapping):
                    return False
                if statuses and item.get("status") not in statuses:
                    return False
                if deployments and item.get("deploymentId") not in deployments:
                    return False
                if models and item.get("model") not in models:
                    return False
                item_components = item.get("componentIds")
                if components and (not isinstance(item_components, list) or not any(component in components for component in item_components)):
                    return False
                started = item.get("startedAt")
                if isinstance(started, str):
                    try:
                        started_at = datetime.fromisoformat(started.replace("Z", "+00:00"))
                        if started_at < from_time or started_at > to_time:
                            return False
                    except ValueError:
                        return False
                return True
            return {**dict(page), "items": [item for item in items if matches(item)]}
        return dict(page) if isinstance(page, Mapping) else {"items": [], "nextCursor": None, "snapshotAt": utc_now(), "latestObservationId": None}

    async def _execute_definition(self, definition: Mapping[str, object], cursor: str | None) -> dict[str, object] | None:
        kind = str(definition["kind"])
        if kind == "analytics":
            if self.query_port is None:
                raise RuntimeError("query provider unavailable")
            from aura_observatory.ports import AnalyticsRequest
            from_time, to_time = self._window_bounds(str(definition["window"]))
            request = AnalyticsRequest(
                metric=str(definition["metric"]), unit=str(definition["unit"]),
                aggregation=str(definition["aggregation"]), bucket=str(definition["bucket"]),
                group_by=str(definition["groupBy"]), focus=str(definition["focus"]),
                from_time=from_time, to_time=to_time,
                filters={str(item["name"]): str(item["value"]) for item in definition.get("facets", []) if isinstance(item, Mapping)},
                series_limit=int(definition["seriesLimit"]), point_limit=int(definition["pointLimit"]),
            )
            if callable(getattr(self.query_port, "analytics", None)):
                return await self.query_port.analytics(request)
            return await self.query_port.operational_analytics_query(request)
        if kind == "run_search":
            return await self._run_search(definition, cursor)
        if kind == "trace_search":
            if self.query_port is None:
                raise RuntimeError("query provider unavailable")
            from aura_observatory.query import RunFilters
            from_time, to_time = self._window_bounds(str(definition["window"]))
            component_values = list(definition["components"])
            outcome_values = list(definition["outcomes"])
            trace_filters = RunFilters(
                component=component_values[0] if len(component_values) == 1 else None,
                from_time=from_time,
                to_time=to_time,
            )
            if callable(getattr(self.query_port, "runs", None)):
                page = await self.query_port.runs(trace_filters, cursor=cursor, limit=int(definition["pageSize"]))
            else:
                page = await self.query_port.run_query(trace_filters, cursor=cursor, limit=int(definition["pageSize"]))
            items = page.get("items", []) if isinstance(page, Mapping) else []
            if not isinstance(items, list):
                return dict(page) if isinstance(page, Mapping) else {"items": [], "nextCursor": None, "snapshotAt": utc_now(), "latestObservationId": None}
            filtered = [
                item for item in items
                if isinstance(item, Mapping)
                and isinstance(item.get("traceId"), str)
                and (not component_values or any(component in component_values for component in item.get("componentIds", []) if isinstance(item.get("componentIds"), list)))
                and (not outcome_values or item.get("status") in outcome_values or item.get("outcome") in outcome_values)
            ]
            return {**dict(page), "items": filtered}
        if kind == "run_comparison":
            if self.query_port is None:
                raise RuntimeError("query provider unavailable")
            if callable(getattr(self.query_port, "compare_runs", None)):
                return await self.query_port.compare_runs(str(definition["baselineRunId"]), str(definition["candidateRunId"]))
            return await self.query_port.run_comparison_query(str(definition["baselineRunId"]), str(definition["candidateRunId"]))
        if kind == "component_health":
            if self.query_port is None:
                raise RuntimeError("query provider unavailable")
            from_time, to_time = self._window_bounds(str(definition["window"]))
            if callable(getattr(self.query_port, "component_health", None)):
                return await self.query_port.component_health(str(definition["componentId"]), from_time=from_time, to_time=to_time, component_version=str(definition["componentVersion"]), point_limit=int(definition["pointLimit"]))
            return await self.query_port.component_health_query(str(definition["componentId"]), from_time, to_time, component_version=str(definition["componentVersion"]), point_limit=int(definition["pointLimit"]))
        if self.evaluation_port is None:
            raise RuntimeError("evaluation provider unavailable")
        filters: dict[str, object] = {}
        outcomes = list(definition["outcomes"])
        if len(outcomes) == 1:
            filters["decision"] = outcomes[0]
        else:
            # Keep the complete closed outcome set at the public provider
            # boundary so production pagination is filtered before rows are
            # fetched.  The local projection check remains as a defensive
            # guard for providers that return partial pages.
            filters["outcomes"] = outcomes
        from_time, to_time = self._window_bounds(str(definition["window"]))
        filters["from"] = from_time.isoformat().replace("+00:00", "Z")
        filters["to"] = to_time.isoformat().replace("+00:00", "Z")
        filters["experimentReferences"] = [
            {"resourceId": str(item["resourceId"]), "resourceVersion": str(item["resourceVersion"])}
            for item in definition["experimentReferences"]
            if isinstance(item, Mapping)
        ]
        page = await self.evaluation_port.collection_async("regressions", cursor=cursor, limit=int(definition["pageSize"]), filters=filters)
        references = {
            (str(item.get("resourceId")), str(item.get("resourceVersion")))
            for item in definition["experimentReferences"] if isinstance(item, Mapping)
        }
        if references and isinstance(page, Mapping) and isinstance(page.get("items"), list):
            def matches(item: object) -> bool:
                if not isinstance(item, Mapping):
                    return False
                candidates: set[tuple[str, str]] = set()
                # ``experimentReferences`` are intentionally scoped to the
                # experiment side of a regression.  A baseline is a related
                # resource, not an acceptable experiment reference, even
                # though both are represented as versioned references.
                for key in ("experiment",):
                    nested = item.get(key)
                    if isinstance(nested, Mapping) and nested.get("resourceId") is not None and nested.get("resourceVersion") is not None:
                        candidates.add((str(nested["resourceId"]), str(nested["resourceVersion"])))
                for key in ("experimentId",):
                    if item.get(key) is not None:
                        candidates.add((str(item[key]), str(item.get("experimentVersion") or "")))
                return bool(candidates & references)
            page = {**page, "items": [item for item in page["items"] if matches(item)]}
        if len(outcomes) > 1 and isinstance(page, Mapping) and isinstance(page.get("items"), list):
            page = {**page, "items": [item for item in page["items"] if isinstance(item, Mapping) and (item.get("outcome") in outcomes or item.get("decision") in outcomes)]}
        return dict(page) if isinstance(page, Mapping) else None

    async def execute(self, query_id: str, query_version: str, *, cursor: str | None = None) -> dict[str, object]:
        started = monotonic()
        definition_started = monotonic()
        try:
            query = await self.repository.get_resource("saved_query", query_id, query_version)
        except (ConnectionError, TimeoutError) as exc:
            self._record("definition_get", definition_started, "unavailable", dependency="postgresql", error_code="timeout" if isinstance(exc, TimeoutError) else "transport_unavailable")
            raise
        if query is None:
            self._record("definition_get", definition_started, "failure", dependency=None, error_code="validation")
            raise KeyError("saved query not found")
        self._record("definition_get", definition_started, "success", dependency="postgresql")
        definition = canonical_definition(query.get("definition")) if isinstance(query, Mapping) else None
        if not isinstance(definition, Mapping):
            raise SavedQueryError("saved query definition is invalid")
        kind = str(definition["kind"])
        if kind in {"run_search", "trace_search", "regression_search"}:
            provider_cursor = decode_execution_cursor(cursor, kind=kind, query_id=query_id, query_version=query_version)
        elif cursor is not None:
            raise SavedQueryError("cursor is invalid")
        else:
            provider_cursor = None
        provider_dependency = "evaluation_definition_state" if kind == "regression_search" else "clickhouse"
        snapshot_at = utc_now()
        base: dict[str, object] = {"kind": kind, "query": {"queryId": query_id, "queryVersion": query_version}, "state": "complete", "partial": False, "snapshotAt": snapshot_at, "freshness": {"state": "fresh", "measuredAt": snapshot_at, "lagMs": 0}, "result": None}
        if kind in {"run_search", "trace_search", "regression_search"}:
            base["nextCursor"] = None
        try:
            result = await self._execute_definition(definition, provider_cursor)
        except SavedQueryError:
            base["state"] = "incompatible"
            self._record("query", started, "failure", dependency=None, error_code="validation")
            return base
        except ValueError as exc:
            if exc.__class__.__name__ == "EvaluationUnavailable":
                base["state"] = "unavailable"
                base["freshness"] = {"state": "unavailable", "measuredAt": None, "lagMs": None}
                self._record("query", started, "unavailable", dependency=provider_dependency, error_code="provider_unavailable")
                return base
            base["state"] = "incompatible"
            self._record("query", started, "failure", dependency=None, error_code="validation")
            return base
        except TimeoutError:
            base["state"] = "unavailable"
            base["freshness"] = {"state": "unavailable", "measuredAt": None, "lagMs": None}
            self._record("query", started, "unavailable", dependency=provider_dependency, error_code="provider_timeout")
            return base
        except (ConnectionError, RuntimeError):
            base["state"] = "unavailable"
            base["freshness"] = {"state": "unavailable", "measuredAt": None, "lagMs": None}
            self._record("query", started, "unavailable", dependency=provider_dependency, error_code="provider_unavailable")
            return base
        except Exception:
            base["state"] = "unavailable"
            base["freshness"] = {"state": "unavailable", "measuredAt": None, "lagMs": None}
            self._record("query", started, "unavailable", dependency=provider_dependency, error_code="provider_failure")
            return base
        if isinstance(result, Mapping):
            result_map = validate_execution_result(kind, result, definition)
            if result_map is None:
                base["state"] = "incompatible"
                self._record("validate", started, "incompatible", dependency=None, error_code="validation")
                return base
            result_snapshot = result_map.get("snapshotAt")
            if isinstance(result_snapshot, str):
                base["snapshotAt"] = result_snapshot
                snapshot_at = result_snapshot
            state, partial = self._state(result_map)
            base["state"], base["partial"] = state, partial
            base["freshness"] = self._freshness(result_map, snapshot_at)
            base["result"] = result_map
            if "nextCursor" in base:
                provider_next = result_map.get("nextCursor")
                if isinstance(provider_next, str) and provider_next:
                    try:
                        base["nextCursor"] = encode_execution_cursor(
                            kind, query_id, query_version, provider_next
                        )
                    except SavedQueryError:
                        base["state"] = "incompatible"
                        base["partial"] = False
                        base["result"] = None
                        self._record("validate", started, "incompatible", dependency=None, error_code="validation")
                        return base
                else:
                    base["nextCursor"] = None
        elif result is not None:
            base["state"] = "incompatible"
            self._record("validate", started, "incompatible", dependency=None, error_code="validation")
            return base
        freshness_state = base.get("freshness", {}).get("state") if isinstance(base.get("freshness"), Mapping) else None
        execution_outcome = "stale" if freshness_state == "stale" else "partial" if base["partial"] else "complete"
        self._record("query", started, execution_outcome, dependency=provider_dependency)
        return base

    def _record(self, operation: str, started: float, outcome: str, *, dependency: str | None = "postgresql", error_code: str | None = None) -> None:
        bounded_outcome = outcome if outcome in {"success", "complete", "partial", "stale", "failure", "unavailable", "incompatible"} else "failure"
        metric = getattr(self.metrics, "record", None)
        if callable(metric):
            try:
                metric("observatory_saved_query_operation_count", 1.0, {"operation": "query" if operation == "execute" else operation, "outcome": bounded_outcome})
            except (ValueError, TypeError):
                pass
        recorder = getattr(self.metrics, "record_component_span", None)
        if callable(recorder):
            try:
                recorder(component_id="aura.observatory.saved_queries", component_version="1.0.0", deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")), operation="query" if operation == "execute" else operation, duration_ms=max(0.001, (monotonic() - started) * 1000.0), outcome=bounded_outcome, dependency=dependency, error_code=error_code)
            except (ValueError, TypeError):
                # Diagnostics are strictly best-effort and never alter query
                # behavior or provider result classification.
                pass


__all__ = ["SavedQueryError", "SavedQueryExecutionService", "SavedQueryService", "canonical_definition", "decode_cursor", "decode_execution_cursor", "encode_cursor", "encode_execution_cursor", "normalize_definition", "utc_now"]
