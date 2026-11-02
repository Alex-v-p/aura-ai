# ruff: noqa: E501
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportOperatorIssue=false, reportReturnType=false, reportAssignmentType=false, reportUnnecessaryIsInstance=false, reportUnnecessaryCast=false, reportPrivateUsage=false

"""Immutable experiments, baselines, and deterministic regressions.

The experiment boundary is intentionally independent from the evaluation
runner.  It stores only references and bounded metadata; metric values are
accepted from a narrow evidence port and are never accepted from HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import wraps
from time import monotonic
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_COMPONENT = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PRIVATE = re.compile(
    r"(?:owner|identity|credential|bearer|token|secret|password|private|content|endpoint|filename|path)",
    re.IGNORECASE,
)
_CAPTURE = {"metadata_only", "hashed", "redacted", "sampled_content", "full_content"}
_OUTCOMES = {"regressed", "unchanged", "inconclusive", "unavailable", "incompatible"}
_EVIDENCE_STATES = {
    "complete",
    "missing",
    "partial",
    "insufficient",
    "incompatible",
    "late",
}
_COMMAND_KEY = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_AUTHORIZATION_PURPOSES = {
    "synthetic_benchmark",
    "owner_supplied_evaluation",
    "controlled_replay",
    "human_review",
}


def _utc_now() -> datetime:
    """Return UTC without importing the general query module.

    Experiments are a separate ownership zone; keeping this tiny clock helper
    local prevents an accidental dependency on the run-query implementation.
    """

    return datetime.now(UTC)


class ExperimentError(ValueError):
    """Malformed, incompatible, or unsafe experiment operation."""


class ExperimentConflict(ExperimentError):
    """An immutable identity or idempotency key conflicts with stored state."""


class ExperimentNotFound(ExperimentError):
    """A requested experiment-owned resource does not exist."""


class ExperimentUnavailable(RuntimeError):
    """The backing metadata/evidence provider is unavailable."""


def _public_failure_boundary(
    metric_name: str, operation: str, component_id: str
) -> Callable[[Callable[..., Awaitable[Any]]], Callable[..., Awaitable[Any]]]:
    """Instrument failures that occur before a command reaches its core path."""

    def decorate(function: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
        @wraps(function)
        async def wrapped(self: object, *args: object, **kwargs: object) -> object:
            started = monotonic()
            try:
                return await function(self, *args, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                service = cast("EvaluationExperimentService", self)
                service._metric(metric_name, outcome="failed", operation=operation)
                if isinstance(error, ExperimentConflict):
                    error_code = "validation"
                elif isinstance(error, ExperimentNotFound):
                    error_code = "validation"
                elif isinstance(error, ExperimentUnavailable):
                    error_code = "provider_unavailable"
                elif isinstance(error, ExperimentError):
                    error_code = "validation"
                else:
                    error_code = "provider_failure"
                service._span(
                    operation,
                    started,
                    "failure",
                    error_code,
                    component_id=component_id,
                )
                raise

        return wrapped

    return decorate


@dataclass(frozen=True, slots=True)
class EvidenceAssessment:
    """Finite, content-free evidence returned by an Observatory provider."""

    state: str
    baseline_values: tuple[float, ...] = ()
    candidate_values: tuple[float, ...] = ()
    references: tuple[dict[str, str], ...] = ()
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.state not in _EVIDENCE_STATES:
            raise ExperimentError("evidence state is invalid")
        for values in (self.baseline_values, self.candidate_values):
            if len(values) > 100_000 or any(
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(float(value))
                or abs(float(value)) > 1.0e15
                for value in values
            ):
                raise ExperimentError("evidence values are invalid")


def _uuid(value: object, name: str) -> str:
    if not isinstance(value, str) or _UUID.fullmatch(value) is None:
        raise ExperimentError(f"{name} is invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise ExperimentError(f"{name} is invalid") from exc
    if str(parsed) != value:
        raise ExperimentError(f"{name} is invalid")
    return value


def _version(value: object, name: str) -> str:
    if not isinstance(value, str) or _VERSION.fullmatch(value) is None or _PRIVATE.search(value):
        raise ExperimentError(f"{name} is invalid")
    return value


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None or _PRIVATE.search(value):
        raise ExperimentError(f"{name} is invalid")
    return value


def _timestamp(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ExperimentError(f"{name} is invalid")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ExperimentError(f"{name} is invalid") from exc
    if instant.tzinfo is None or instant.utcoffset() != UTC.utcoffset(instant):
        raise ExperimentError(f"{name} is invalid")
    return instant.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _digest(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _encode_experiment_cursor(
    *, snapshot_at: str, created_at: str, resource_id: str, version: str
) -> str:
    payload = {
        "v": 3,
        "snapshotAt": snapshot_at,
        "createdAt": created_at,
        "resourceId": resource_id,
        "version": version,
    }
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "ex3." + base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _decode_experiment_cursor(value: str | None) -> dict[str, str | None]:
    if value is None:
        now = _utc_now().isoformat().replace("+00:00", "Z")
        return {"snapshotAt": now, "createdAt": None, "resourceId": None, "version": None}
    if not isinstance(value, str) or not value.startswith("ex3.") or len(value) > 512:
        raise ExperimentError("cursor is invalid")
    try:
        encoded = value[4:]
        encoded += "=" * (-len(encoded) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(encoded).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExperimentError("cursor is invalid") from exc
    if not isinstance(decoded, Mapping) or decoded.get("v") != 3:
        raise ExperimentError("cursor is invalid")
    fields = {"v", "snapshotAt", "createdAt", "resourceId", "version"}
    if set(decoded) != fields or decoded.get("v") != 3 or any(
        item is not None and not isinstance(item, str)
        for key, item in decoded.items()
        if key != "v"
    ):
        raise ExperimentError("cursor is invalid")
    return {
        key: cast(str | None, decoded[key])
        for key in fields
        if key != "v"
    }


def _closed(value: object, allowed: set[str], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or any(not isinstance(key, str) for key in value):
        raise ExperimentError(f"{name} is invalid")
    result = dict(value)
    if set(result) != allowed:
        raise ExperimentError(f"{name} is invalid")
    return result


def _ref(value: object, name: str) -> dict[str, str]:
    item = _closed(value, {"id", "version"}, name)
    return {"id": _uuid(item["id"], f"{name}.id"), "version": _version(item["version"], f"{name}.version")}


def _provenance(value: object, name: str = "provenance") -> dict[str, str]:
    item = _closed(value, {"createdByComponent", "createdByVersion", "sourceDigest"}, name)
    component = item["createdByComponent"]
    if not isinstance(component, str) or _COMPONENT.fullmatch(component) is None:
        raise ExperimentError(f"{name} is invalid")
    digest = item["sourceDigest"]
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ExperimentError(f"{name} is invalid")
    return {
        "createdByComponent": component,
        "createdByVersion": _version(item["createdByVersion"], f"{name}.createdByVersion"),
        "sourceDigest": digest,
    }


def _component(value: object, name: str) -> dict[str, object]:
    item = _closed(value, {"componentId", "componentVersion", "codeRevision", "imageDigest", "configurationRevision"}, name)
    component = item["componentId"]
    digest = item["imageDigest"]
    for key in ("codeRevision", "configurationRevision"):
        _version(item[key], f"{name}.{key}")
    if not isinstance(component, str) or _COMPONENT.fullmatch(component) is None:
        raise ExperimentError(f"{name}.componentId is invalid")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ExperimentError(f"{name}.imageDigest is invalid")
    return {**item, "componentId": component, "componentVersion": _version(item["componentVersion"], f"{name}.componentVersion")}


def _model(value: object, name: str) -> dict[str, str]:
    item = _closed(value, {"modelId", "modelVersion", "modelDigest"}, name)
    model = item["modelId"]
    digest = item["modelDigest"]
    if not isinstance(model, str) or not _IDENTIFIER.fullmatch(model) or _PRIVATE.search(model):
        raise ExperimentError(f"{name}.modelId is invalid")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ExperimentError(f"{name}.modelDigest is invalid")
    return {"modelId": model, "modelVersion": _version(item["modelVersion"], f"{name}.modelVersion"), "modelDigest": digest}


def _variant(value: object, index: int) -> dict[str, object]:
    item = _closed(value, {"variantId", "components", "model", "configurationRevision", "promptRevision", "policyRevision", "deploymentRevision"}, f"variants[{index}]")
    variant = {"variantId": _identifier(item["variantId"], f"variants[{index}].variantId")}
    components = item["components"]
    if not isinstance(components, list) or not 1 <= len(components) <= 32:
        raise ExperimentError(f"variants[{index}].components is invalid")
    variant["components"] = [_component(entry, f"variants[{index}].components[{position}]") for position, entry in enumerate(components)]
    variant["model"] = _model(item["model"], f"variants[{index}].model")
    for key in ("configurationRevision", "promptRevision", "policyRevision", "deploymentRevision"):
        variant[key] = _version(item[key], f"variants[{index}].{key}")
    return variant


def _cohort(value: object, index: int) -> dict[str, object]:
    item = _closed(value, {"cohortId", "caseIds"}, f"cohorts[{index}]")
    cohort_id = _identifier(item["cohortId"], f"cohorts[{index}].cohortId")
    case_ids = item["caseIds"]
    if not isinstance(case_ids, list) or not 1 <= len(case_ids) <= 1000 or len(set(case_ids)) != len(case_ids):
        raise ExperimentError(f"cohorts[{index}].caseIds is invalid")
    return {"cohortId": cohort_id, "caseIds": [_uuid(case_id, f"cohorts[{index}].caseIds") for case_id in case_ids]}


def _limits(value: object) -> dict[str, object]:
    allowed = {"timeoutMs", "maxConcurrency", "maxAttempts", "maxCases", "maxInputBytes", "maxOutputBytes", "maxTokens"}
    item = _closed(value, allowed, "limits")
    upper = {"timeoutMs": 86_400_000, "maxConcurrency": 64, "maxAttempts": 10, "maxCases": 100_000, "maxInputBytes": 1_073_741_824, "maxOutputBytes": 1_073_741_824, "maxTokens": 100_000_000}
    minimum = {"timeoutMs": 100, "maxConcurrency": 1, "maxAttempts": 1, "maxCases": 1}
    for key, limit in upper.items():
        if type(item[key]) is not int or not minimum.get(key, 0) <= item[key] <= limit:
            raise ExperimentError(f"limits.{key} is invalid")
    return item


def _threshold(value: object, index: int) -> dict[str, object]:
    allowed = {"metric", "metricClass", "evaluator", "unit", "aggregation", "direction", "comparisonMode", "thresholdValue", "minimumSampleSize", "missingEvidencePolicy"}
    item = _closed(value, allowed, f"thresholds[{index}]")
    _identifier(item["metric"], f"thresholds[{index}].metric")
    if item["metricClass"] not in {"operational", "quality"} or item["unit"] not in {"ratio", "count", "milliseconds", "bytes", "tokens", "operations", "per_second", "score"} or item["aggregation"] not in {"minimum", "maximum", "mean", "p50", "p95", "p99", "total"} or item["direction"] not in {"higher_is_better", "lower_is_better"} or item["comparisonMode"] not in {"absolute", "relative"} or item["missingEvidencePolicy"] not in {"regressed", "inconclusive", "not_applicable"}:
        raise ExperimentError(f"thresholds[{index}] is invalid")
    evaluator = _closed(item["evaluator"], {"evaluatorId", "evaluatorVersion", "kind"}, f"thresholds[{index}].evaluator")
    _uuid(evaluator["evaluatorId"], "threshold evaluator id")
    _version(evaluator["evaluatorVersion"], "threshold evaluator version")
    if evaluator["kind"] not in {"deterministic", "local_judge", "human_review"}:
        raise ExperimentError(f"thresholds[{index}].evaluator is invalid")
    number = item["thresholdValue"]
    if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(float(number)) or not 0 <= float(number) <= 1.0e15:
        raise ExperimentError(f"thresholds[{index}].thresholdValue is invalid")
    if type(item["minimumSampleSize"]) is not int or not 1 <= item["minimumSampleSize"] <= 100_000:
        raise ExperimentError(f"thresholds[{index}].minimumSampleSize is invalid")
    return {**item, "evaluator": {"evaluatorId": evaluator["evaluatorId"], "evaluatorVersion": evaluator["evaluatorVersion"], "kind": evaluator["kind"]}}


def _reproducibility(value: object) -> dict[str, object]:
    item = _closed(value, {"seed", "clockMode", "timeZone", "locale", "fixtureRevision", "promptRevision", "policyRevision"}, "reproducibility")
    if type(item["seed"]) is not int or not 0 <= item["seed"] <= 2_147_483_647 or item["clockMode"] not in {"fixed", "recorded"} or item["timeZone"] != "UTC" or not isinstance(item["locale"], str) or len(item["locale"]) > 32:
        raise ExperimentError("reproducibility is invalid")
    for key in ("fixtureRevision", "promptRevision", "policyRevision"):
        _version(item[key], f"reproducibility.{key}")
    return item


class ExperimentEvidencePort(Protocol):
    async def metric_values(
        self,
        result_ids: Sequence[str],
        metric: str,
        cohort_ids: Sequence[str],
        *,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[float, ...] | None: ...

    async def evidence(self, result_ids: Sequence[str], *, version: str) -> list[dict[str, str]]: ...

    async def assess(
        self,
        baseline_result_ids: Sequence[str],
        candidate_result_ids: Sequence[str],
        metric: str,
        cohort_ids: Sequence[str],
        *,
        exact_version: str,
        expected_evaluator: Mapping[str, object] | None = None,
        baseline_variant: Mapping[str, object] | None = None,
        candidate_variant: Mapping[str, object] | None = None,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> EvidenceAssessment: ...

    async def persist_facts(
        self,
        regression_id: str,
        regression_version: str,
        metric: str,
        metric_class: str,
        cohort_ids: Sequence[str],
        baseline_values: Sequence[float],
        candidate_values: Sequence[float],
        baseline_result_ids: Sequence[str],
        candidate_result_ids: Sequence[str],
        evidence_state: str,
        *,
        threshold_index: int = 0,
    ) -> None: ...


class ExperimentReferencePort(Protocol):
    """Closed reference validation over Observatory-owned definitions/results."""

    async def validate_experiment(self, experiment: Mapping[str, object]) -> None: ...

    async def validate_baseline(
        self, baseline: Mapping[str, object], experiment: Mapping[str, object]
    ) -> None: ...

    async def validate_regression(
        self,
        regression: Mapping[str, object],
        experiment: Mapping[str, object],
        baseline: Mapping[str, object],
    ) -> None: ...


class ExperimentRepository(Protocol):
    @property
    def history_rows(self) -> list[dict[str, object]]: ...

    async def put(self, resource_type: str, resource: Mapping[str, object]) -> None: ...

    async def get(self, resource_type: str, resource_id: str, version: str | None = None) -> Mapping[str, object] | None: ...

    async def list(self, resource_type: str) -> list[Mapping[str, object]]: ...

    async def list_page(
        self,
        resource_type: str,
        *,
        cursor: str | None,
        limit: int,
        filters: Mapping[str, object] | None,
    ) -> dict[str, object]: ...

    async def idempotent(self, operation: str, key: str, fingerprint: str, response: Mapping[str, object]) -> Mapping[str, object] | None: ...

    async def complete_idempotent(self, operation: str, key: str, response: Mapping[str, object]) -> None: ...

    async def delete_before(self, cutoff: str | None) -> int: ...

    async def begin_command(
        self, operation: str, key: str, fingerprint: str, resource_id: str | None = None,
        resource_version: str | None = None,
    ) -> MemoryCommandClaim: ...

    async def complete_command(
        self, operation: str, key: str, fingerprint: str, response: Mapping[str, object], *, claim_token: str | None = None
    ) -> None: ...

    async def renew_command(
        self, operation: str, key: str, fingerprint: str, claim_token: str | None
    ) -> bool: ...

    async def promote_baseline(
        self, baseline_id: str, version: str, authorization_record_id: str, promoted_at: str, *,
        idempotency_key: str | None = None, request_fingerprint: str | None = None,
        claim_token: str | None = None,
    ) -> Mapping[str, object]: ...

    async def reconcile_projection_outbox(self, *, limit: int = 16) -> int: ...

    async def reconcile_command_history(self, *, limit: int = 16) -> int: ...

    async def record_history(self, value: Mapping[str, object]) -> None: ...


@dataclass(frozen=True, slots=True)
class MemoryCommandClaim:
    replay: Mapping[str, object] | None
    claim_token: str | None = None


class MemoryExperimentRepository:
    """Bounded reference repository used by isolated API tests and local mode."""

    def __init__(self) -> None:
        self.resources: dict[str, dict[tuple[str, str], dict[str, object]]] = {}
        self.commands: dict[tuple[str, str], tuple[str, dict[str, object]]] = {}
        self.history_rows: list[dict[str, object]] = []
        self._command_lock = asyncio.Lock()
        self._promotion_lock = asyncio.Lock()

    async def put(self, resource_type: str, resource: Mapping[str, object]) -> None:
        # Projection facts are an internal, numeric-only persistence hint.  A
        # repository must never expose this transport-private field in the
        # immutable resource projection.
        clean_resource = dict(resource)
        clean_resource.pop("__projectionFacts", None)
        identifier = str(resource.get("experimentId") or resource.get("baselineId") or resource.get("regressionId"))
        version = str(resource.get("experimentVersion") or resource.get("baselineVersion") or resource.get("regressionVersion"))
        bucket = self.resources.setdefault(resource_type, {})
        prior = bucket.get((identifier, version))
        if prior is not None and prior != clean_resource:
            raise ExperimentConflict("immutable resource conflicts with existing state")
        bucket[(identifier, version)] = clean_resource

    async def get(self, resource_type: str, resource_id: str, version: str | None = None) -> Mapping[str, object] | None:
        values = self.resources.get(resource_type, {})
        for (identifier, resource_version), resource in values.items():
            if identifier == resource_id and (version is None or resource_version == version):
                return dict(resource)
        return None

    async def list(self, resource_type: str) -> list[Mapping[str, object]]:
        values = self.resources.get(resource_type, {}).values()
        return [dict(value) for value in sorted(values, key=lambda item: str(item.get("createdAt", "")), reverse=True)]

    async def idempotent(self, operation: str, key: str, fingerprint: str, response: Mapping[str, object]) -> Mapping[str, object] | None:
        async with self._command_lock:
            prior = self.commands.get((operation, key))
            if prior is not None:
                if prior[0] != fingerprint:
                    raise ExperimentConflict("idempotency key conflicts with existing state")
                return dict(prior[1])
            self.commands[(operation, key)] = (fingerprint, dict(response))
            return None

    async def begin_command(
        self, operation: str, key: str, fingerprint: str, resource_id: str | None = None,
        resource_version: str | None = None,
    ) -> MemoryCommandClaim:
        del resource_id, resource_version
        async with self._command_lock:
            prior = self.commands.get((operation, key))
            if prior is not None:
                if prior[0] != fingerprint:
                    raise ExperimentConflict("idempotency key conflicts with existing state")
                return MemoryCommandClaim(dict(prior[1]))
            self.commands[(operation, key)] = (fingerprint, {})
            return MemoryCommandClaim(None)

    async def complete_idempotent(self, operation: str, key: str, response: Mapping[str, object]) -> None:
        async with self._command_lock:
            if (operation, key) in self.commands:
                fingerprint, _prior = self.commands[(operation, key)]
                self.commands[(operation, key)] = (fingerprint, dict(response))

    async def complete_command(
        self, operation: str, key: str, fingerprint: str, response: Mapping[str, object], *, claim_token: str | None = None
    ) -> None:
        del claim_token
        async with self._command_lock:
            prior = self.commands.get((operation, key))
            if prior is None or prior[0] != fingerprint:
                raise ExperimentConflict("idempotency command claim is invalid")
            self.commands[(operation, key)] = (fingerprint, dict(response))

    async def renew_command(
        self, operation: str, key: str, fingerprint: str, claim_token: str | None
    ) -> bool:
        del claim_token
        prior = self.commands.get((operation, key))
        if prior is None or prior[0] != fingerprint:
            raise ExperimentUnavailable("experiment command claim was lost")
        return True

    async def reconcile_projection_outbox(self, *, limit: int = 16) -> int:
        del limit
        return 0

    async def reconcile_command_history(self, *, limit: int = 16) -> int:
        del limit
        return 0

    async def promote_baseline(
        self,
        baseline_id: str,
        version: str,
        authorization_record_id: str,
        promoted_at: str,
        *,
        idempotency_key: str | None = None,
        request_fingerprint: str | None = None,
        claim_token: str | None = None,
    ) -> Mapping[str, object]:
        del idempotency_key, request_fingerprint, claim_token
        async with self._promotion_lock:
            current = await self.get("baselines", baseline_id, version)
            if current is None:
                raise ExperimentNotFound("baseline candidate not found")
            if current.get("state") != "candidate":
                if current.get("manualPromotionRecordId") == authorization_record_id:
                    return dict(current)
                raise ExperimentConflict("baseline is already immutable")
            promoted = dict(current)
            promoted.update(
                {
                    "state": "promoted",
                    "manualPromotionRecordId": authorization_record_id,
                    "promotedAt": promoted_at,
                }
            )
            # Promotion is the only fenced state transition.  It intentionally
            # bypasses the generic insert-only ``put`` path.
            self.resources.setdefault("baselines", {})[(baseline_id, version)] = promoted
            return promoted

    async def record_history(self, value: Mapping[str, object]) -> None:
        if any(row.get("historyId") == value.get("historyId") for row in self.history_rows):
            return
        self.history_rows.insert(0, dict(value))
        del self.history_rows[10_000:]

    async def history_page(
        self,
        *,
        cursor: str | None,
        limit: int,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, object]:
        values = self.history_rows
        if resource_type is not None:
            values = [item for item in values if item.get("resourceType") == resource_type]
        if resource_id is not None:
            values = [item for item in values if item.get("resourceId") == resource_id]
        return _page_experiment_values(values, cursor=cursor, limit=limit)

    async def list_page(
        self,
        resource_type: str,
        *,
        cursor: str | None,
        limit: int,
        filters: Mapping[str, object] | None,
    ) -> dict[str, object]:
        values = await self.list(resource_type)
        for key, expected in (filters or {}).items():
            if expected is None:
                continue
            values = [
                value
                for value in values
                if value.get(key) == expected
                or (
                    isinstance(value.get(key), Mapping)
                    and cast(Mapping[str, object], value[key]).get("resourceId") == expected
                )
            ]
        return _page_experiment_values(values, cursor=cursor, limit=limit)

    async def delete_before(self, cutoff: str | None) -> int:
        removed = 0
        for bucket in self.resources.values():
            keys = [
                key
                for key, value in bucket.items()
                if cutoff is None or str(value.get("createdAt", "")) < cutoff
            ]
            for key in keys:
                del bucket[key]
                removed += 1
        return removed


def _page_experiment_values(
    values: Sequence[Mapping[str, object]], *, cursor: str | None, limit: int
) -> dict[str, object]:
    if not 1 <= limit <= 200:
        raise ExperimentError("query is invalid")
    state = _decode_experiment_cursor(cursor)
    snapshot = str(state["snapshotAt"])
    bounded = [
        dict(value)
        for value in values
        if str(value.get("createdAt", "")) <= snapshot
    ]
    bounded.sort(
        key=lambda value: (
            str(value.get("createdAt", "")),
            str(value.get("experimentId") or value.get("baselineId") or value.get("regressionId") or value.get("historyId")),
            str(value.get("experimentVersion") or value.get("baselineVersion") or value.get("regressionVersion") or ""),
        ),
        reverse=True,
    )
    last_created = state.get("createdAt")
    last_id = state.get("resourceId")
    last_version = state.get("version")
    if last_created is not None and last_id is not None:
        bounded = [
            value
            for value in bounded
            if (
                str(value.get("createdAt", "")),
                str(value.get("experimentId") or value.get("baselineId") or value.get("regressionId") or value.get("historyId")),
                str(value.get("experimentVersion") or value.get("baselineVersion") or value.get("regressionVersion") or ""),
            )
            < (str(last_created), str(last_id), str(last_version or ""))
        ]
    items = bounded[:limit]
    next_cursor = None
    if len(bounded) > limit and items:
        item = items[-1]
        identifier = str(item.get("experimentId") or item.get("baselineId") or item.get("regressionId") or item.get("historyId"))
        version = str(item.get("experimentVersion") or item.get("baselineVersion") or item.get("regressionVersion") or "")
        next_cursor = _encode_experiment_cursor(
            snapshot_at=snapshot,
            created_at=str(item.get("createdAt") or item.get("occurredAt")),
            resource_id=identifier,
            version=version,
        )
    return {"items": items, "nextCursor": next_cursor, "snapshotAt": snapshot, "partial": False}


@dataclass(slots=True)
class MemoryMetricEvidence:
    values: dict[tuple[str, str, str], tuple[float, ...]] = field(default_factory=dict)

    async def metric_values(
        self,
        result_ids: Sequence[str],
        metric: str,
        cohort_ids: Sequence[str],
        *,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> tuple[float, ...] | None:
        del result_ids
        values: list[float] = []
        for cohort_id in cohort_ids:
            values.extend(self.values.get((cohort_id, metric, "candidate"), ()))
        return tuple(values) if values else None

    async def evidence(self, result_ids: Sequence[str], *, version: str) -> list[dict[str, str]]:
        return [{"kind": "result", "id": result_id, "digest": hashlib.sha256(result_id.encode()).hexdigest(), "version": version} for result_id in result_ids[:512]]

    async def assess(
        self,
        baseline_result_ids: Sequence[str],
        candidate_result_ids: Sequence[str],
        metric: str,
        cohort_ids: Sequence[str],
        *,
        exact_version: str,
        expected_evaluator: Mapping[str, object] | None = None,
        baseline_variant: Mapping[str, object] | None = None,
        candidate_variant: Mapping[str, object] | None = None,
        cohort_case_ids: Mapping[str, Sequence[str]] | None = None,
    ) -> EvidenceAssessment:
        del exact_version, expected_evaluator, baseline_variant, candidate_variant, cohort_case_ids
        baseline = await self.metric_values(baseline_result_ids, metric, cohort_ids)
        candidate = await self.metric_values(candidate_result_ids, metric, cohort_ids)
        if baseline is None or candidate is None:
            return EvidenceAssessment(
                "missing", baseline or (), candidate or (), tuple(await self.evidence(baseline_result_ids + tuple(candidate_result_ids), version="1.0.0"))
            )
        return EvidenceAssessment(
            "complete", baseline, candidate,
            tuple(await self.evidence(baseline_result_ids + tuple(candidate_result_ids), version="1.0.0")),
        )

    async def persist_facts(
        self,
        regression_id: str,
        regression_version: str,
        metric: str,
        metric_class: str,
        cohort_ids: Sequence[str],
        baseline_values: Sequence[float],
        candidate_values: Sequence[float],
        baseline_result_ids: Sequence[str],
        candidate_result_ids: Sequence[str],
        evidence_state: str,
        *,
        threshold_index: int = 0,
    ) -> None:
        del (
            regression_id,
            regression_version,
            metric,
            metric_class,
            cohort_ids,
            baseline_values,
            candidate_values,
            baseline_result_ids,
            candidate_result_ids,
            evidence_state,
            threshold_index,
        )


class NoopExperimentReferencePort:
    """Explicit isolated reference boundary for memory-only tests."""

    async def validate_experiment(self, experiment: Mapping[str, object]) -> None:
        del experiment

    async def validate_baseline(
        self, baseline: Mapping[str, object], experiment: Mapping[str, object]
    ) -> None:
        del baseline, experiment

    async def validate_regression(
        self,
        regression: Mapping[str, object],
        experiment: Mapping[str, object],
        baseline: Mapping[str, object],
    ) -> None:
        del regression, experiment, baseline


class EvaluationExperimentService:
    """Owner-agnostic application service; transport owns auth/CSRF."""

    def __init__(
        self,
        repository: ExperimentRepository | None = None,
        evidence: ExperimentEvidencePort | None = None,
        references: ExperimentReferencePort | None = None,
        metrics: object | None = None,
    ) -> None:
        self.repository = repository or MemoryExperimentRepository()
        self.evidence = evidence or MemoryMetricEvidence()
        self.references = references or NoopExperimentReferencePort()
        self.metrics = metrics

    @property
    def history(self) -> list[dict[str, object]]:
        """Compatibility view; durable repositories own lifecycle history."""

        return self.repository.history_rows

    def _metric(self, name: str, *, outcome: str, operation: str = "write") -> None:
        recorder = getattr(self.metrics, "record", None)
        if not callable(recorder):
            return
        try:
            labels = {"outcome": outcome}
            if name in {
                "observatory_evaluation_experiment_operation_count",
                "observatory_evaluation_baseline_operation_count",
                "observatory_evaluation_regression_operation_count",
            }:
                labels["operation"] = operation
            recorder(name, 1.0, labels)
        except Exception:
            return

    async def recover_projection_outbox(self, *, limit: int = 16) -> int:
        """Bounded command/worker recovery drain for pending CH projections."""
        try:
            bounded = max(1, min(16, int(limit)))
            projections = await self.repository.reconcile_projection_outbox(limit=bounded)
            history = int(await self.repository.reconcile_command_history(limit=bounded))
            return int(projections) + history
        except Exception:
            # Recovery is retried by the next bounded command/worker pass;
            # provider diagnostics never cross this application boundary.
            return 0

    def _span(
        self,
        operation: str,
        started: float,
        outcome: str = "success",
        error_code: str | None = None,
        component_id: str = "aura.observatory.evaluation_experiments",
        dependency: str = "postgresql",
    ) -> None:
        recorder = getattr(self.metrics, "record_component_span", None)
        if not callable(recorder):
            return
        try:
            recorder(
                component_id=component_id,
                component_version="1.0.0",
                deployment_id=str(getattr(self.metrics, "deployment_id", "observatory")),
                operation=operation,
                duration_ms=max(0.0, (monotonic() - started) * 1000.0),
                outcome=outcome,
                dependency=dependency,
                error_code=error_code,
            )
        except Exception:
            return

    @staticmethod
    def _fingerprint(value: Mapping[str, object]) -> str:
        return _digest(value)

    @staticmethod
    def _cohort_case_map(
        experiment: Mapping[str, object], cohort_ids: Sequence[object]
    ) -> dict[str, tuple[str, ...]]:
        selected = {str(value) for value in cohort_ids}
        cohorts = experiment.get("cohorts")
        if not isinstance(cohorts, list):
            raise ExperimentError("experiment cohorts are unavailable")
        result: dict[str, tuple[str, ...]] = {}
        for cohort in cohorts:
            if not isinstance(cohort, Mapping):
                continue
            cohort_id = str(cohort.get("cohortId"))
            if cohort_id not in selected:
                continue
            case_ids = cohort.get("caseIds")
            if not isinstance(case_ids, list) or not case_ids:
                raise ExperimentError("cohort cases are unavailable")
            result[cohort_id] = tuple(str(case_id) for case_id in case_ids)
        if set(result) != selected:
            raise ExperimentError("cohort cases are unavailable")
        return result

    async def _complete_idempotent(
        self, operation: str, key: str | None, response: Mapping[str, object]
    ) -> None:
        if key is not None:
            await self.repository.complete_idempotent(operation, key, response)

    async def _record_history(
        self, resource_type: str, resource_id: str, version: str | None, change: str, state: str
    ) -> None:
        value = self._history(resource_type, resource_id, version, change, state)
        await self.repository.record_history(value)

    async def _claim_command(
        self,
        operation: str,
        key: str | None,
        fingerprint: str,
        response: Mapping[str, object],
        resource_id: str | None = None,
        resource_version: str | None = None,
    ) -> tuple[Mapping[str, object] | None, str | None]:
        if key is None:
            return None, None
        _uuid(key, "idempotencyKey")
        claim = await self.repository.begin_command(
            operation, key, fingerprint, resource_id, resource_version
        )
        return claim.replay, claim.claim_token

    async def _complete_command(
        self, operation: str, key: str | None, fingerprint: str, response: Mapping[str, object],
        claim_token: str | None = None,
    ) -> None:
        if key is None:
            return
        await self.repository.complete_command(
            operation, key, fingerprint, response, claim_token=claim_token
        )

    @_public_failure_boundary(
        "observatory_evaluation_experiment_operation_count", "write",
        "aura.observatory.evaluation_experiments",
    )
    async def create_experiment(self, body: Mapping[str, object], *, idempotency_key: str | None = None) -> dict[str, object]:
        started = monotonic()
        await self.recover_projection_outbox()
        allowed = {"experimentId", "experimentVersion", "suite", "variants", "cohorts", "captureAuthorization", "limits", "retentionMode", "provenance", "createdAt"}
        item = _closed(body, allowed, "experiment")
        experiment_id = _uuid(item["experimentId"], "experimentId")
        version = _version(item["experimentVersion"], "experimentVersion")
        suite = _ref(item["suite"], "suite")
        variants = item["variants"]
        if not isinstance(variants, list) or not 2 <= len(variants) <= 16:
            raise ExperimentError("variants are invalid")
        normalized_variants = [_variant(value, index) for index, value in enumerate(variants)]
        if len({str(value["variantId"]) for value in normalized_variants}) != len(normalized_variants):
            raise ExperimentError("variant identifiers must be unique")
        cohorts = item["cohorts"]
        if not isinstance(cohorts, list) or not 1 <= len(cohorts) <= 64:
            raise ExperimentError("cohorts are invalid")
        normalized_cohorts = [_cohort(value, index) for index, value in enumerate(cohorts)]
        if len({str(value["cohortId"]) for value in normalized_cohorts}) != len(normalized_cohorts):
            raise ExperimentError("cohort identifiers must be unique")
        capture = _closed(item["captureAuthorization"], {"captureClassification", "authorizationPurpose", "authorizationContext", "retentionMode"}, "captureAuthorization")
        if capture["captureClassification"] != "metadata_only" or capture["authorizationPurpose"] not in _AUTHORIZATION_PURPOSES or capture["authorizationContext"] != "controlled_evaluation" or capture["retentionMode"] != "manual_only":
            raise ExperimentError("captureAuthorization is invalid")
        if item["retentionMode"] != "manual_only":
            raise ExperimentError("retentionMode is invalid")
        limits = _limits(item["limits"])
        normalized = {
            "schemaVersion": 1, "resourceType": "experiment", "experimentId": experiment_id,
            "experimentVersion": version, "state": "active", "suite": {"resourceId": suite["id"], "resourceVersion": suite["version"]},
            "variants": normalized_variants, "cohorts": normalized_cohorts,
            "captureAuthorization": dict(capture), "limits": limits,
            "captureClassification": capture["captureClassification"], "retentionMode": "manual_only",
            "provenance": _provenance(item["provenance"]), "createdAt": _timestamp(item["createdAt"], "createdAt"),
        }
        try:
            await self.references.validate_experiment(normalized)
        except ExperimentError:
            self._span("write", started, "failure", "validation")
            raise
        except Exception:
            self._span("write", started, "failure", "provider_unavailable")
            raise
        if idempotency_key is not None:
            prior, claim_token = await self._claim_command(
                "create_experiment", idempotency_key, self._fingerprint(body), normalized,
                resource_id=experiment_id, resource_version=version,
            )
            if prior is not None:
                self._span("write", monotonic(), "duplicate", "duplicate")
                return dict(prior)
        else:
            claim_token = None
        try:
            await self.repository.put("experiments", normalized)
        except Exception:
            self._span("write", started, "failure", "database_failure")
            raise
        self._metric("observatory_evaluation_experiment_operation_count", outcome="accepted")
        self._span("write", started, "success")
        await self._complete_command("create_experiment", idempotency_key, self._fingerprint(body), normalized, claim_token)
        await self._record_history("experiment", experiment_id, version, "created", "active")
        return normalized

    @_public_failure_boundary(
        "observatory_evaluation_baseline_operation_count", "write",
        "aura.observatory.evaluation_baselines",
    )
    async def create_baseline(self, body: Mapping[str, object], *, idempotency_key: str | None = None) -> dict[str, object]:
        started = monotonic()
        await self.recover_projection_outbox()
        allowed = {"baselineId", "baselineVersion", "experiment", "suite", "variantId", "cohortIds", "sourceResultIds", "sourceResultFrom", "sourceResultTo", "thresholds", "captureClassification", "reproducibility", "retentionMode", "provenance", "createdAt"}
        item = _closed(body, allowed, "baseline")
        baseline_id = _uuid(item["baselineId"], "baselineId")
        baseline_version = _version(item["baselineVersion"], "baselineVersion")
        experiment = _ref(item["experiment"], "experiment")
        suite = _ref(item["suite"], "suite")
        variant_id = _identifier(item["variantId"], "variantId")
        cohort_ids = item["cohortIds"]
        result_ids = item["sourceResultIds"]
        if not isinstance(cohort_ids, list) or not 1 <= len(cohort_ids) <= 64 or len(set(cohort_ids)) != len(cohort_ids):
            raise ExperimentError("cohortIds are invalid")
        if not isinstance(result_ids, list) or not 1 <= len(result_ids) <= 100_000 or len(set(result_ids)) != len(result_ids):
            raise ExperimentError("sourceResultIds are invalid")
        cohort_ids_normalized = [_identifier(value, "cohortId") for value in cohort_ids]
        result_ids_normalized = [_uuid(value, "sourceResultId") for value in result_ids]
        experiment_resource = await self.repository.get("experiments", experiment["id"], experiment["version"])
        if experiment_resource is None:
            raise ExperimentNotFound("experiment not found")
        if experiment_resource.get("suite") != {"resourceId": suite["id"], "resourceVersion": suite["version"]}:
            raise ExperimentError("suite is not bound to experiment")
        canonical_capture = experiment_resource.get("captureAuthorization")
        if (
            not isinstance(canonical_capture, Mapping)
            or canonical_capture.get("captureClassification") != item["captureClassification"]
            or canonical_capture.get("authorizationPurpose") not in _AUTHORIZATION_PURPOSES
            or canonical_capture.get("authorizationContext") != "controlled_evaluation"
            or canonical_capture.get("retentionMode") != "manual_only"
        ):
            raise ExperimentError("baseline capture authorization is incompatible")
        variants = experiment_resource.get("variants")
        cohorts = experiment_resource.get("cohorts")
        if not isinstance(variants, list) or not any(isinstance(value, Mapping) and value.get("variantId") == variant_id for value in variants):
            raise ExperimentError("variantId is not declared by experiment")
        declared_cohorts = {value.get("cohortId") for value in cohorts} if isinstance(cohorts, list) else set()
        if not set(cohort_ids_normalized) <= declared_cohorts:
            raise ExperimentError("cohortIds are not declared by experiment")
        source_from = _timestamp(item["sourceResultFrom"], "sourceResultFrom")
        source_to = _timestamp(item["sourceResultTo"], "sourceResultTo")
        if source_from > source_to:
            raise ExperimentError("source result range is invalid")
        thresholds = item["thresholds"]
        if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 128:
            raise ExperimentError("thresholds are invalid")
        if item["captureClassification"] != "metadata_only" or item["retentionMode"] != "manual_only":
            raise ExperimentError("baseline retention/capture is invalid")
        normalized = {
            "schemaVersion": 1, "resourceType": "baseline", "baselineId": baseline_id, "baselineVersion": baseline_version, "state": "candidate",
            "experiment": {"resourceId": experiment["id"], "resourceVersion": experiment["version"]}, "suite": {"resourceId": suite["id"], "resourceVersion": suite["version"]}, "variantId": variant_id,
            "cohortIds": cohort_ids_normalized, "sourceResultIds": result_ids_normalized,
            "sourceResultFrom": source_from, "sourceResultTo": source_to,
            "thresholds": [_threshold(value, index) for index, value in enumerate(thresholds)], "captureClassification": item["captureClassification"],
            "reproducibility": _reproducibility(item["reproducibility"]), "immutable": True, "promotionMode": "manual_owner_action", "manualPromotionRecordId": None, "promotedAt": None,
            "retentionMode": "manual_only", "provenance": _provenance(item["provenance"]), "createdAt": _timestamp(item["createdAt"], "createdAt"),
        }
        try:
            await self.references.validate_baseline(normalized, cast(Mapping[str, object], experiment_resource))
        except ExperimentError:
            self._span("write", started, "failure", "validation", component_id="aura.observatory.evaluation_baselines")
            raise
        except Exception:
            self._span("write", started, "failure", "provider_unavailable", component_id="aura.observatory.evaluation_baselines")
            raise
        if idempotency_key is not None:
            prior, claim_token = await self._claim_command(
                "create_baseline", idempotency_key, self._fingerprint(body), normalized,
                resource_id=baseline_id, resource_version=baseline_version,
            )
            if prior is not None:
                self._span("write", started, "duplicate", "duplicate", component_id="aura.observatory.evaluation_baselines")
                return dict(prior)
        else:
            claim_token = None
        try:
            await self.repository.put("baselines", normalized)
        except Exception:
            self._span("write", started, "failure", "database_failure", component_id="aura.observatory.evaluation_baselines")
            raise
        self._metric("observatory_evaluation_baseline_operation_count", outcome="candidate_created")
        self._span("write", started, "success", component_id="aura.observatory.evaluation_baselines")
        await self._complete_command("create_baseline", idempotency_key, self._fingerprint(body), normalized, claim_token)
        await self._record_history("baseline", baseline_id, baseline_version, "candidate_created", "candidate")
        return normalized

    @_public_failure_boundary(
        "observatory_evaluation_baseline_operation_count", "write",
        "aura.observatory.evaluation_baselines",
    )
    async def promote_baseline(self, baseline_id: str, body: Mapping[str, object], *, idempotency_key: str | None = None) -> dict[str, object]:
        started = monotonic()
        await self.recover_projection_outbox()
        baseline_id = _uuid(baseline_id, "baselineId")
        item = _closed(body, {"expectedCandidateVersion", "authorizationRecordId", "promotedAt"}, "promotion")
        expected = _version(item["expectedCandidateVersion"], "expectedCandidateVersion")
        authorization = _uuid(item["authorizationRecordId"], "authorizationRecordId")
        promoted_at = _timestamp(item["promotedAt"], "promotedAt")
        current = await self.repository.get("baselines", baseline_id, expected)
        if current is None:
            raise ExperimentNotFound("baseline candidate not found")
        promoted = dict(current)
        promoted.update({"state": "promoted", "manualPromotionRecordId": authorization, "promotedAt": promoted_at})
        fingerprint = self._fingerprint(body)
        if idempotency_key is not None:
            prior, claim_token = await self._claim_command(
                "promote_baseline", idempotency_key, fingerprint, promoted,
                resource_id=baseline_id, resource_version=expected,
            )
            if prior is not None:
                self._span("write", started, "duplicate", "duplicate", component_id="aura.observatory.evaluation_baselines")
                return dict(prior)
        else:
            claim_token = None
        if current.get("state") != "candidate":
            if current.get("manualPromotionRecordId") == authorization:
                await self._complete_command(
                    "promote_baseline", idempotency_key, fingerprint, current, claim_token
                )
                return dict(current)
            raise ExperimentConflict("baseline is already immutable")
        promoted = dict(
            await self.repository.promote_baseline(
                baseline_id,
                expected,
                authorization,
                promoted_at,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                claim_token=claim_token,
            )
        )
        persisted = await self.repository.get("baselines", baseline_id, expected)
        if persisted is None or persisted.get("manualPromotionRecordId") != authorization:
            raise ExperimentConflict("baseline promotion lost an immutable race")
        self._metric("observatory_evaluation_baseline_operation_count", outcome="promoted")
        self._span("write", started, "success", component_id="aura.observatory.evaluation_baselines")
        await self._record_history("baseline", baseline_id, expected, "promoted", "promoted")
        await self._complete_command("promote_baseline", idempotency_key, fingerprint, promoted, claim_token)
        return promoted

    @_public_failure_boundary(
        "observatory_evaluation_regression_operation_count", "evaluate",
        "aura.observatory.evaluation_regressions",
    )
    async def create_regression(self, body: Mapping[str, object], *, idempotency_key: str | None = None) -> dict[str, object]:
        started = monotonic()
        await self.recover_projection_outbox()
        allowed = {"regressionId", "regressionVersion", "experiment", "baseline", "variantId", "cohortIds", "baselineResultIds", "candidateResultIds", "thresholds", "createdAt"}
        item = _closed(body, allowed, "regression")
        regression_id = _uuid(item["regressionId"], "regressionId")
        regression_version = _version(item["regressionVersion"], "regressionVersion")
        experiment = _ref(item["experiment"], "experiment")
        baseline = _ref(item["baseline"], "baseline")
        variant_id = _identifier(item["variantId"], "variantId")
        experiment_resource = await self.repository.get("experiments", experiment["id"], experiment["version"])
        baseline_resource = await self.repository.get("baselines", baseline["id"], baseline["version"])
        if experiment_resource is None or baseline_resource is None:
            raise ExperimentNotFound("experiment or baseline not found")
        if baseline_resource.get("experiment") != {
            "resourceId": experiment["id"],
            "resourceVersion": experiment["version"],
        }:
            raise ExperimentError("baseline is not bound to experiment")
        if baseline_resource.get("state") != "promoted":
            raise ExperimentConflict("baseline must be promoted before comparison")
        variants = experiment_resource.get("variants")
        if not isinstance(variants, list):
            raise ExperimentUnavailable("experiment variants are unavailable")
        selected_variant = next(
            (candidate for candidate in variants if isinstance(candidate, Mapping) and candidate.get("variantId") == variant_id),
            None,
        )
        if not isinstance(selected_variant, Mapping):
            raise ExperimentError("variantId is not declared by experiment")
        cohort_ids = item["cohortIds"]
        left = item["baselineResultIds"]
        right = item["candidateResultIds"]
        if not isinstance(cohort_ids, list) or not 1 <= len(cohort_ids) <= 64 or len(set(cohort_ids)) != len(cohort_ids):
            raise ExperimentError("cohortIds are invalid")
        cohort_case_ids = self._cohort_case_map(experiment_resource, cohort_ids)
        if not isinstance(left, list) or not 1 <= len(left) <= 100_000 or not isinstance(right, list) or not 1 <= len(right) <= 100_000:
            raise ExperimentError("result ids are invalid")
        left_ids = [_uuid(value, "baselineResultId") for value in left]
        right_ids = [_uuid(value, "candidateResultId") for value in right]
        source_ids = baseline_resource.get("sourceResultIds")
        if not isinstance(source_ids, list) or set(left_ids) != set(source_ids):
            raise ExperimentError("baseline result set is incompatible")
        thresholds = item["thresholds"]
        if not isinstance(thresholds, list) or not 1 <= len(thresholds) <= 128:
            raise ExperimentError("thresholds are invalid")
        normalized_thresholds = [_threshold(value, index) for index, value in enumerate(thresholds)]
        await self.references.validate_regression(
            {
                "experiment": {"resourceId": experiment["id"], "resourceVersion": experiment["version"]},
                "baseline": {"resourceId": baseline["id"], "resourceVersion": baseline["version"]},
                "variantId": variant_id,
                "cohortIds": cohort_ids,
                "baselineResultIds": left_ids,
                "candidateResultIds": right_ids,
                "thresholds": normalized_thresholds,
            },
            cast(Mapping[str, object], experiment_resource),
            cast(Mapping[str, object], baseline_resource),
        )
        comparisons: list[dict[str, object]] = []
        projection_assessments: list[tuple[int, Mapping[str, object], EvidenceAssessment]] = []
        cohort_outcomes: dict[str, str] = {}
        evidence_state = "complete"
        overall = "unchanged"
        for threshold_index, threshold in enumerate(normalized_thresholds):
            metric = str(threshold["metric"])
            assessments: list[tuple[str, EvidenceAssessment]] = []
            for cohort_id in cohort_ids:
                selected_cohort = [cohort_id]
                assessment = await self.evidence.assess(
                    left_ids, right_ids, metric, selected_cohort,
                    exact_version=regression_version,
                    expected_evaluator=cast(Mapping[str, object], threshold["evaluator"]),
                    baseline_variant=cast(Mapping[str, object], baseline_resource.get("variant") or next((v for v in variants if isinstance(v, Mapping) and v.get("variantId") == baseline_resource.get("variantId")), {})),
                    candidate_variant=cast(Mapping[str, object], selected_variant),
                    cohort_case_ids={cohort_id: cohort_case_ids[cohort_id]},
                )
                assessments.append((str(cohort_id), assessment))
            baseline_values = tuple(value for _cohort, cohort_assessment in assessments for value in cohort_assessment.baseline_values) or None
            candidate_values = tuple(value for _cohort, cohort_assessment in assessments for value in cohort_assessment.candidate_values) or None
            assessment_state = "complete"
            for cohort_id, cohort_assessment in assessments:
                assessment_state = self._combine_evidence_state(assessment_state, cohort_assessment.state)
                if cohort_assessment.state == "complete" and len(cohort_assessment.baseline_values) >= int(threshold["minimumSampleSize"]) and len(cohort_assessment.candidate_values) >= int(threshold["minimumSampleSize"]):
                    outcome = self._decide(
                        threshold,
                        self._aggregate(cohort_assessment.baseline_values, str(threshold["aggregation"])),
                        self._aggregate(cohort_assessment.candidate_values, str(threshold["aggregation"])),
                    )
                else:
                    outcome = "inconclusive"
                cohort_outcomes[cohort_id] = self._combine_outcome(
                    cohort_outcomes.get(cohort_id, "unchanged"), outcome
                )
            projection_state = assessment_state
            if projection_state == "complete":
                if baseline_values is None or candidate_values is None:
                    projection_state = "missing"
                elif (
                    len(baseline_values) < int(threshold["minimumSampleSize"])
                    or len(candidate_values) < int(threshold["minimumSampleSize"])
                ):
                    projection_state = "insufficient"
            aggregate_assessment = EvidenceAssessment(
                projection_state, baseline_values or (), candidate_values or ()
            )
            # Persist one bounded outbox fact for every threshold, including
            # incomplete evidence.  This preserves the terminal state for a
            # restart/reconciliation path instead of defaulting missing rows.
            projection_assessments.append((threshold_index, threshold, aggregate_assessment))
            if assessment_state != "complete":
                evidence_state = self._combine_evidence_state(evidence_state, assessment_state)
                outcome = str(threshold["missingEvidencePolicy"])
                if assessment_state in {"incompatible", "late", "partial"}:
                    outcome = "inconclusive"
                if outcome == "not_applicable":
                    outcome = "inconclusive"
                overall = self._combine_outcome(overall, outcome)
                comparisons.append(self._comparison(threshold, baseline_values, candidate_values, outcome))
                continue
            if baseline_values is None or candidate_values is None:
                evidence_state = self._combine_evidence_state(evidence_state, "missing")
                outcome = str(threshold["missingEvidencePolicy"])
                if outcome == "not_applicable":
                    outcome = "inconclusive"
                overall = self._combine_outcome(overall, outcome)
                comparisons.append(self._comparison(threshold, None, None, outcome))
                continue
            if len(baseline_values) < int(threshold["minimumSampleSize"]) or len(candidate_values) < int(threshold["minimumSampleSize"]):
                evidence_state = self._combine_evidence_state(evidence_state, "insufficient")
                comparisons.append(self._comparison(threshold, baseline_values, candidate_values, "inconclusive"))
                overall = self._combine_outcome(overall, "inconclusive")
                continue
            baseline_value = self._aggregate(baseline_values, str(threshold["aggregation"]))
            candidate_value = self._aggregate(candidate_values, str(threshold["aggregation"]))
            outcome = self._decide(threshold, baseline_value, candidate_value)
            comparisons.append(self._comparison(threshold, baseline_values, candidate_values, outcome))
            overall = self._combine_outcome(overall, outcome)
        if not comparisons:
            overall = "unavailable"
            evidence_state = "missing"
        evidence = await self.evidence.evidence(left_ids + right_ids, version=regression_version)
        baseline_variant_id = baseline_resource.get("variantId")
        attribution_candidates: list[dict[str, object]] = []
        if evidence_state == "complete" and isinstance(baseline_variant_id, str):
            baseline_variant = next(
                (candidate for candidate in variants if isinstance(candidate, Mapping) and candidate.get("variantId") == baseline_variant_id),
                None,
            )
            if isinstance(baseline_variant, Mapping):
                attribution_candidates = self._attribution_candidates(
                    baseline_variant,
                    selected_variant,
                    cohort_ids,
                    evidence,
                    cohort_outcomes=cohort_outcomes,
                )
        attribution = {
            "attributionId": str(uuid5(NAMESPACE_URL, f"aura.observatory.attribution:{regression_id}:{regression_version}")), "attributionVersion": "1.0.0", "regression": {"resourceId": regression_id, "resourceVersion": regression_version},
            "availability": "available" if attribution_candidates else ("unavailable" if evidence_state != "complete" else "insufficient_evidence"), "candidates": attribution_candidates, "capturePolicy": "metadata_only", "createdAt": _timestamp(item["createdAt"], "createdAt"),
        }
        normalized = {
            "schemaVersion": 1, "resourceType": "regression_decision", "regressionId": regression_id, "regressionVersion": regression_version,
            "experiment": {"resourceId": experiment["id"], "resourceVersion": experiment["version"]}, "baseline": {"resourceId": baseline["id"], "resourceVersion": baseline["version"]}, "variant": dict(cast(Mapping[str, object], selected_variant)),
            "cohortIds": [_identifier(value, "cohortId") for value in cohort_ids], "baselineResultIds": left_ids, "candidateResultIds": right_ids, "comparisons": comparisons,
            "decision": overall if overall in _OUTCOMES else "inconclusive", "evidenceState": evidence_state, "evidence": evidence, "attribution": attribution, "capturePolicy": "metadata_only", "createdAt": _timestamp(item["createdAt"], "createdAt"),
        }
        normalized["__projectionFacts"] = [
            {
                "thresholdIndex": threshold_index,
                "metric": threshold["metric"],
                "metricClass": threshold["metricClass"],
                "evidenceState": assessment.state,
                "baselineValues": list(assessment.baseline_values),
                "candidateValues": list(assessment.candidate_values),
            }
            for threshold_index, threshold, assessment in projection_assessments
        ]
        try:
            await self.references.validate_regression(
                normalized,
                cast(Mapping[str, object], experiment_resource),
                cast(Mapping[str, object], baseline_resource),
            )
        except ExperimentError:
            self._span("evaluate", started, "failure", "validation", component_id="aura.observatory.evaluation_regressions")
            raise
        except Exception:
            self._span("evaluate", started, "failure", "provider_unavailable", component_id="aura.observatory.evaluation_regressions")
            raise
        if idempotency_key is not None:
            prior, claim_token = await self._claim_command(
                "create_regression", idempotency_key, self._fingerprint(body), normalized,
                resource_id=regression_id, resource_version=regression_version,
            )
            if prior is not None:
                await self.repository.reconcile_projection_outbox(limit=16)
                self._span("evaluate", monotonic(), "duplicate", "duplicate")
                return dict(prior)
        else:
            claim_token = None
        try:
            await self.repository.put("regressions", normalized)
        except Exception:
            self._span("evaluate", started, "failure", "database_failure", component_id="aura.observatory.evaluation_regressions")
            raise
        normalized.pop("__projectionFacts", None)
        # Close the durable command immediately after the immutable resource
        # and projection outbox commit.  ClickHouse facts and deterministic
        # history are replayable side effects; leaving the command pending
        # until after those writes creates a crash window with no owner.
        await self._complete_command(
            "create_regression", idempotency_key, self._fingerprint(body), normalized, claim_token
        )
        if projection_assessments:
            try:
                for threshold_index, threshold, assessment in projection_assessments:
                    await self.evidence.persist_facts(
                        regression_id,
                        regression_version,
                        str(threshold["metric"]),
                        str(threshold["metricClass"]),
                        cohort_ids,
                        assessment.baseline_values,
                        assessment.candidate_values,
                        left_ids,
                        right_ids,
                        assessment.state,
                        threshold_index=threshold_index,
                    )
            except Exception:
                self._span("fact_projection_write", started, "failure", "provider_unavailable", component_id="aura.observatory.evaluation_regressions", dependency="clickhouse")
                raise
        self._metric("observatory_evaluation_regression_operation_count", outcome="completed", operation="evaluate")
        self._span("evaluate", started, "success", component_id="aura.observatory.evaluation_regressions")
        recorder = getattr(self.metrics, "record", None)
        if callable(recorder):
            try:
                recorder("observatory_evaluation_regression_duration_ms", (monotonic() - started) * 1000.0, {})
            except Exception:
                pass
        self._metric("observatory_evaluation_regression_outcome_count", outcome=str(normalized["decision"]), operation="evaluate")
        self._metric("observatory_evaluation_attribution_outcome_count", outcome=str(attribution["availability"]), operation="evaluate")
        await self._record_history("regression", regression_id, regression_version, "comparison_completed", str(normalized["decision"]))
        await self._record_history("attribution", str(attribution["attributionId"]), "1.0.0", "attribution_completed", str(attribution["availability"]))
        await self._complete_command(
            "create_regression", idempotency_key, self._fingerprint(body), normalized, claim_token
        )
        return normalized

    @staticmethod
    def _combine_outcome(current: str, candidate: str) -> str:
        """Regressed is sticky; later unavailable evidence cannot erase it."""

        if current == "regressed" or candidate == "regressed":
            return "regressed"
        rank = {"unchanged": 0, "unavailable": 1, "inconclusive": 2, "incompatible": 3}
        return current if rank.get(current, 0) >= rank.get(candidate, 0) else candidate

    @staticmethod
    def _combine_evidence_state(current: str, candidate: str) -> str:
        rank = {"complete": 0, "missing": 1, "insufficient": 2, "partial": 3, "late": 4, "incompatible": 5}
        return current if rank.get(current, 0) >= rank.get(candidate, 0) else candidate

    @staticmethod
    def _aggregate(values: Sequence[float], aggregation: str) -> float:
        ordered = sorted(values)
        if aggregation == "minimum":
            return ordered[0]
        if aggregation == "maximum":
            return ordered[-1]
        if aggregation == "total":
            return float(sum(values))
        if aggregation == "p50":
            return ordered[(len(ordered) - 1) // 2]
        if aggregation == "p95":
            return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.95) - 1)]
        if aggregation == "p99":
            return ordered[min(len(ordered) - 1, math.ceil(len(ordered) * 0.99) - 1)]
        return float(sum(values) / len(values))

    @staticmethod
    def _attribution_candidates(
        before: Mapping[str, object],
        after: Mapping[str, object],
        cohort_ids: Sequence[object],
        evidence: Sequence[Mapping[str, str]],
        *,
        cohort_outcomes: Mapping[str, str] | None = None,
    ) -> list[dict[str, object]]:
        changed: list[tuple[str, str | None, str, str]] = []
        for kind, before_key, after_key in (
            ("model", "model", "model"),
            ("configuration", "configurationRevision", "configurationRevision"),
            ("prompt", "promptRevision", "promptRevision"),
            ("policy", "policyRevision", "policyRevision"),
            ("deployment", "deploymentRevision", "deploymentRevision"),
        ):
            left = before.get(before_key)
            right = after.get(after_key)
            if kind == "model" and isinstance(left, Mapping) and isinstance(right, Mapping):
                before_revision = str(left.get("modelVersion", ""))
                after_revision = str(right.get("modelVersion", ""))
            else:
                before_revision = str(left or "")
                after_revision = str(right or "")
            if before_revision and after_revision and before_revision != after_revision:
                changed.append((kind, None, before_revision, after_revision))
        components_before = before.get("components")
        components_after = after.get("components")
        if isinstance(components_before, list) and isinstance(components_after, list):
            for left, right in zip(components_before, components_after, strict=False):
                if isinstance(left, Mapping) and isinstance(right, Mapping) and left.get("componentVersion") != right.get("componentVersion"):
                    component_id = str(right.get("componentId"))
                    changed.append(("component", component_id, str(left.get("componentVersion")), str(right.get("componentVersion"))))
        candidates: list[dict[str, object]] = []
        affected_cohorts = {
            str(value)
            for value in cohort_ids
            if (cohort_outcomes or {}).get(str(value)) == "regressed"
        }
        if not affected_cohorts:
            return candidates
        for rank, (kind, component_id, before_revision, after_revision) in enumerate(changed[:64], start=1):
            candidates.append({
                "rank": rank,
                "bindingKind": kind,
                "componentId": component_id,
                "beforeRevision": before_revision,
                "afterRevision": after_revision,
                "affectedCohortIds": [
                    str(value) for value in cohort_ids
                    if (cohort_outcomes or {}).get(str(value)) == "regressed"
                ],
                "unaffectedCohortIds": [
                    str(value) for value in cohort_ids
                    if (cohort_outcomes or {}).get(str(value)) == "unchanged"
                ],
                "evidence": list(evidence[:128]),
            })
        return candidates

    @staticmethod
    def _decide(threshold: Mapping[str, object], baseline: float, candidate: float) -> str:
        difference = candidate - baseline
        if threshold["comparisonMode"] == "relative":
            if baseline == 0:
                return "inconclusive"
            magnitude = abs(difference / baseline)
        else:
            magnitude = abs(difference)
        threshold_value = float(threshold["thresholdValue"])
        violated = magnitude >= threshold_value
        if not violated:
            return "unchanged"
        worse = difference < 0 if threshold["direction"] == "higher_is_better" else difference > 0
        return "regressed" if worse else "unchanged"

    @staticmethod
    def _comparison(threshold: Mapping[str, object], left: Sequence[float] | None, right: Sequence[float] | None, outcome: str) -> dict[str, object]:
        baseline = None if left is None else EvaluationExperimentService._aggregate(left, str(threshold["aggregation"]))
        candidate = None if right is None else EvaluationExperimentService._aggregate(right, str(threshold["aggregation"]))
        difference = None if baseline is None or candidate is None else candidate - baseline
        relative = None if baseline in (None, 0) or difference is None else difference / baseline
        return {"metric": threshold["metric"], "metricClass": threshold["metricClass"], "evaluator": threshold["evaluator"], "unit": threshold["unit"], "aggregation": threshold["aggregation"], "direction": threshold["direction"], "comparisonMode": threshold["comparisonMode"], "baselineValue": 0.0 if baseline is None else baseline, "candidateValue": 0.0 if candidate is None else candidate, "difference": difference, "relativeChange": relative, "thresholdValue": threshold["thresholdValue"], "minimumSampleSize": threshold["minimumSampleSize"], "baselineSampleSize": len(left or ()), "candidateSampleSize": len(right or ()), "missingEvidencePolicy": threshold["missingEvidencePolicy"], "outcome": outcome}

    @staticmethod
    def _history(resource_type: str, resource_id: str, version: str | None, change: str, state: str) -> dict[str, object]:
        stable_key = f"aura-0096:{resource_type}:{resource_id}:{version or ''}:{change}:{state}"
        return {"historyId": str(uuid5(NAMESPACE_URL, stable_key)), "resourceType": resource_type, "resourceId": resource_id, "resourceVersion": version, "change": change, "state": state, "occurredAt": _utc_now().isoformat().replace("+00:00", "Z")}

    async def list_resources(
        self,
        resource_type: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if resource_type not in {"experiments", "baselines", "regressions"} or not 1 <= limit <= 200:
            raise ExperimentError("query is invalid")
        allowed_filters = {
            "experiments": {"suite", "state"},
            "baselines": {"suite", "experiment", "state"},
            "regressions": {"experiment", "baseline", "decision"},
        }[resource_type]
        supplied = filters or {}
        if set(supplied) - allowed_filters:
            raise ExperimentError("query filter is invalid")
        for key, expected in supplied.items():
            if expected is None:
                continue
            if key in {"suite", "experiment", "baseline"}:
                _uuid(expected, f"{key} filter")
            elif key == "state":
                valid = {"active", "superseded", "deleted"} if resource_type == "experiments" else {"candidate", "promoted"}
                if expected not in valid:
                    raise ExperimentError("state filter is invalid")
            elif key == "decision" and expected not in _OUTCOMES:
                raise ExperimentError("decision filter is invalid")
        return await self.repository.list_page(
            resource_type,
            cursor=cursor,
            limit=limit,
            filters=filters,
        )

    async def get_resource(self, resource_type: str, identifier: str, version: str | None = None) -> dict[str, object]:
        found = await self.repository.get(resource_type, _uuid(identifier, "resourceId"), version)
        if found is None:
            raise ExperimentNotFound("resource not found")
        return dict(found)

    async def attribution(self, regression_id: str, version: str) -> dict[str, object]:
        value = await self.repository.get(
            "regressions", _uuid(regression_id, "regressionId"), _version(version, "version")
        )
        if value is None:
            raise ExperimentNotFound("regression not found")
        attribution = value.get("attribution")
        if not isinstance(attribution, Mapping):
            raise ExperimentUnavailable("attribution is unavailable")
        return dict(attribution)

    async def delete_for_retention(
        self, *, cutoff: str | None = None, purge_all: bool = False
    ) -> int:
        """Remove only experiment projections; promotion command evidence remains."""

        if not purge_all and cutoff is None:
            raise ExperimentError("retention cutoff is required")
        return int(
            await self.repository.delete_before(
                None if purge_all else _timestamp(cutoff, "cutoff")
            )
        )


__all__ = ["EvaluationExperimentService", "ExperimentConflict", "ExperimentError", "ExperimentEvidencePort", "ExperimentNotFound", "ExperimentRepository", "ExperimentUnavailable", "MemoryExperimentRepository", "MemoryMetricEvidence"]
