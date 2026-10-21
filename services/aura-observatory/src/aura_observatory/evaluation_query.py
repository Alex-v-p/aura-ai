"""Bounded read models for the Observatory evaluation API.

The evaluation domain owns admission and persistence.  This module is only a
transport-neutral query boundary: it applies deterministic ordering/cursors and
projects domain records to the content-free observatory-v1 shapes.  The small
reference store is deliberately useful for isolated API tests and local
development; production composition may provide repositories exposing the
same optional list methods.
"""

# SQL projections are kept visually aligned with the service-owned schema;
# line-length checking is not useful for those provider statements.
# ruff: noqa: E501

# The query boundary accepts closed JSON projections.  Runtime validation below
# narrows the dynamic values once; avoid duplicating that decoder in types.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportPrivateUsage=false, reportUnusedFunction=false, reportUnnecessaryIsInstance=false

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol, cast
from uuid import UUID, uuid4

MAX_PAGE_SIZE = 200
MAX_QUERY_OFFSET = 1_000_000
_CURSOR_PREFIX = "ev1."
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_COMPONENT_ID = re.compile(r"^aura\.[a-z][a-z0-9_.-]{0,90}$")
_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$")
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_CAPTURE_CLASSES = {
    "metadata_only",
    "hashed",
    "redacted",
    "sampled_content",
    "full_content",
}
_SOURCE_KINDS = {"synthetic", "owner_supplied", "captured"}


class EvaluationQueryError(ValueError):
    """A bounded query or projection failure safe to expose as a 4xx."""


class EvaluationNotFound(EvaluationQueryError):
    pass


class EvaluationConflict(EvaluationQueryError):
    pass


class EvaluationUnavailable(EvaluationQueryError):
    """A bounded provider/storage outage safe to expose as retryable 503."""

    pass


class EvaluationQueryBackend(Protocol):
    """Durable, content-free query port used by production composition."""

    async def collection(
        self,
        kind: str,
        *,
        cursor: str | None,
        limit: int,
        filters: Mapping[str, object] | None,
    ) -> dict[str, object]: ...

    async def get(
        self, kind: str, identifier: str, version: str | None
    ) -> dict[str, object] | None: ...

    async def history_page(
        self,
        *,
        cursor: str | None,
        limit: int,
        resource_type: str | None,
        resource_id: str | None,
    ) -> dict[str, object]: ...


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _safe_uuid(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise EvaluationQueryError(f"{name} is invalid")
    try:
        parsed = UUID(value)
    except ValueError as exc:
        raise EvaluationQueryError(f"{name} is invalid") from exc
    if str(parsed) != value:
        raise EvaluationQueryError(f"{name} is invalid")
    return value


def _required_uuid(value: object, name: str) -> str:
    """Validate a required stored UUID instead of inventing a placeholder."""

    if value is None:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    return _safe_uuid(str(value), name)


def _safe_identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise EvaluationQueryError(f"{name} is invalid")
    lowered = value.casefold()
    if any(marker in lowered for marker in ("token", "secret", "password", "endpoint", "owner")):
        raise EvaluationQueryError(f"{name} is invalid")
    return value


def encode_cursor(index: int) -> str:
    if index < 0:
        raise EvaluationQueryError("cursor is invalid")
    raw = json.dumps({"v": 1, "i": index}, separators=(",", ":")).encode()
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(value: str | None) -> int:
    if value is None:
        return 0
    if not isinstance(value, str) or not value.startswith(_CURSOR_PREFIX) or len(value) > 256:
        raise EvaluationQueryError("cursor is invalid")
    try:
        padded = value[len(_CURSOR_PREFIX) :] + "=" * (-len(value[len(_CURSOR_PREFIX) :]) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded).decode())
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvaluationQueryError("cursor is invalid") from exc
    if not isinstance(decoded, dict) or decoded.get("v") != 1 or type(decoded.get("i")) is not int:
        raise EvaluationQueryError("cursor is invalid")
    index = int(decoded["i"])
    if index < 0 or index > 1_000_000:
        raise EvaluationQueryError("cursor is invalid")
    return index


def page(
    items: Sequence[Mapping[str, object]], *, cursor: str | None, limit: int
) -> dict[str, object]:
    if not 1 <= limit <= MAX_PAGE_SIZE:
        raise EvaluationQueryError("page size is invalid")
    start = decode_cursor(cursor)
    bounded = list(items[start : start + limit])
    next_cursor = encode_cursor(start + limit) if start + limit < len(items) else None
    return {
        "items": bounded,
        "nextCursor": next_cursor,
        "snapshotAt": iso(utc_now()),
        "partial": False,
    }


def _digest(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode()).hexdigest()


def provenance(
    *,
    source: Mapping[str, object],
    component: str = "aura.observatory.evaluation_api",
    version: str = "1.0.0",
) -> dict[str, str]:
    return {
        "createdByComponent": component,
        "createdByVersion": version,
        "sourceDigest": _digest(source),
    }


def resource_ref(resource_id: str, version: str) -> dict[str, str]:
    return {"resourceId": resource_id, "resourceVersion": version}


def _project_provenance(
    payload: Mapping[str, object], resource_type: str
) -> dict[str, str]:
    """Project only the three fields allowed by EvaluationProvenance.

    Stored definitions may contain legacy or provider-specific keys.  They are
    intentionally ignored; an invalid or incomplete stored provenance makes
    the projection unavailable.  Query adapters must never invent provenance
    or a digest from the resource identity.
    """

    raw = payload.get("provenance")
    if isinstance(raw, Mapping):
        component = raw.get("createdByComponent")
        version = raw.get("createdByVersion")
        digest = raw.get("sourceDigest")
        if (
            isinstance(component, str)
            and _COMPONENT_ID.fullmatch(component) is not None
            and isinstance(version, str)
            and _VERSION.fullmatch(version) is not None
            and isinstance(digest, str)
            and _DIGEST.fullmatch(digest) is not None
        ):
            return {
                "createdByComponent": component,
                "createdByVersion": version,
                "sourceDigest": digest,
            }
    raise EvaluationUnavailable(f"stored {resource_type} provenance is unavailable")


def _project_strings(value: object, name: str, *, maximum: int) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    values = list(value)
    if len(values) > maximum or any(not isinstance(item, str) for item in values):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    return cast(list[str], values)


def _project_text(value: object, name: str, *, default: str | None = None) -> str:
    if value is None and default is not None:
        return default
    if not isinstance(value, str):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    return value


def _project_integer(value: object, name: str, *, default: int | None = None) -> int:
    if value is None and default is not None:
        return default
    if type(value) is not int:
        raise EvaluationQueryError(f"stored {name} is unavailable")
    return int(value)


def _project_version(value: object, name: str) -> str:
    result = _project_text(value, name)
    if _VERSION.fullmatch(result) is None:
        raise EvaluationQueryError(f"stored {name} is unavailable")
    return result


def _project_timestamp(value: object, name: str) -> str:
    if value is None:
        raise EvaluationUnavailable(f"stored {name} is unavailable")
    if not isinstance(value, str):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationQueryError(f"stored {name} is unavailable") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    return parsed.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _project_enum(
    value: object, name: str, allowed: set[str], *, default: str | None = None
) -> str:
    result = _project_text(value, name, default=default)
    if result not in allowed:
        raise EvaluationQueryError(f"stored {name} is unavailable")
    return result


def _project_reference(value: object, name: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    resource_id = value.get("resourceId")
    version = value.get("resourceVersion")
    if not isinstance(resource_id, str) or not isinstance(version, str):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    try:
        checked_id = _safe_uuid(resource_id, f"{name}.resourceId")
        checked_version = _safe_identifier(version, f"{name}.resourceVersion")
    except EvaluationQueryError as exc:
        raise EvaluationQueryError(f"stored {name} is unavailable") from exc
    return resource_ref(checked_id, checked_version)


def project_definition(
    payload: Mapping[str, object], *, state: str = "active"
) -> dict[str, object]:
    """Project a domain payload into one of the closed API definitions."""

    resource_type = str(payload.get("resourceType", ""))
    created_at = _project_timestamp(payload.get("createdAt"), "createdAt")
    prov = _project_provenance(payload, resource_type)
    if resource_type == "dataset":
        dataset_id = _required_uuid(payload.get("datasetId"), "datasetId")
        version = _project_version(payload.get("datasetVersion"), "datasetVersion")
        return {
            "schemaVersion": 1,
            "resourceType": "dataset",
            "datasetId": dataset_id,
            "datasetVersion": version,
            # These are persisted definition facts.  A legacy row that does
            # not contain them is unavailable; the query layer must never
            # silently classify it as synthetic/private metadata.
            "sourceKind": _project_enum(
                payload.get("sourceKind"), "sourceKind", _SOURCE_KINDS
            ),
            "scopeIds": _project_strings(
                payload.get("scopes", payload.get("scopeIds")), "scopeIds", maximum=4
            ),
            "captureClassification": _capture(payload),
            "caseCount": _project_integer(payload.get("caseCount"), "caseCount"),
            "retentionMode": "manual_only",
            "provenance": prov,
            "state": state,
            "createdAt": created_at,
        }
    if resource_type == "case":
        case_id = _required_uuid(payload.get("caseId"), "caseId")
        version = _project_version(payload.get("caseVersion"), "caseVersion")
        dataset = payload.get("dataset", {})
        if not isinstance(dataset, Mapping):
            raise EvaluationUnavailable("stored case dataset reference is unavailable")
        dataset_ref = _project_reference(dataset, "dataset")
        return {
            "schemaVersion": 1,
            "resourceType": "case",
            "caseId": case_id,
            "caseVersion": version,
            "dataset": dataset_ref,
            "sourceKind": _project_enum(
                payload.get("sourceKind"), "sourceKind", _SOURCE_KINDS
            ),
            "scopeIds": _project_strings(
                payload.get("scopes", payload.get("scopeIds")), "scopeIds", maximum=4
            ),
            "captureClassification": _capture(payload),
            "artifactIds": _project_strings(
                payload.get("inputArtifactIds", payload.get("artifactIds", [])),
                "artifactIds",
                maximum=32,
            ),
            "retentionMode": "manual_only",
            "provenance": prov,
            "createdAt": created_at,
        }
    if resource_type == "evaluator":
        result: dict[str, object] = {
            "schemaVersion": 1,
            "resourceType": "evaluator",
            "evaluatorId": _required_uuid(payload.get("evaluatorId"), "evaluatorId"),
            "evaluatorVersion": _project_version(
                payload.get("evaluatorVersion"), "evaluatorVersion"
            ),
            "kind": _project_text(payload.get("kind"), "kind"),
            "metric": _project_text(payload.get("metric"), "metric"),
            "componentIds": _project_strings(
                payload.get("componentIds", []), "componentIds", maximum=32
            ),
            "retentionMode": "manual_only",
            "provenance": prov,
            "createdAt": created_at,
        }
        if "rubricVersion" in payload:
            result["rubricVersion"] = _project_version(
                payload["rubricVersion"], "rubricVersion"
            )
        if "calibrationVersion" in payload:
            result["calibrationVersion"] = _project_version(
                payload["calibrationVersion"], "calibrationVersion"
            )
        return result
    if resource_type == "suite":
        dataset = cast(Mapping[str, object], payload.get("dataset", {}))
        if not dataset:
            raise EvaluationUnavailable("stored suite dataset reference is unavailable")
        evaluators = payload.get("evaluators")
        evaluator_items: list[dict[str, object]] = []
        if isinstance(evaluators, Sequence) and not isinstance(evaluators, (str, bytes)):
            for item in evaluators:
                if not isinstance(item, Mapping):
                    raise EvaluationQueryError("stored suite evaluators are unavailable")
                evaluator_items.append(
                    {
                        "evaluatorId": _project_text(
                            item.get("evaluatorId"), "evaluatorId"
                        ),
                        "evaluatorVersion": _project_version(
                            item.get("evaluatorVersion"), "evaluatorVersion"
                        ),
                        "kind": _project_text(item.get("kind"), "kind"),
                    }
                )
        limits = payload.get("limits")
        if not isinstance(limits, Mapping):
            raise EvaluationUnavailable("stored suite limits are unavailable")
        limits_map = cast(Mapping[str, object], limits)
        components = payload.get("components")
        component_ids = payload.get("componentIds")
        if isinstance(components, Sequence) and not isinstance(components, (str, bytes)):
            component_ids = [
                cast(Mapping[str, object], item).get("componentId")
                for item in components
                if isinstance(item, Mapping)
            ]
        return {
            "schemaVersion": 1,
            "resourceType": "suite",
            "suiteId": _required_uuid(payload.get("suiteId"), "suiteId"),
            "suiteVersion": _project_version(payload.get("suiteVersion"), "suiteVersion"),
            "dataset": _project_reference(dataset, "dataset"),
            "evaluators": evaluator_items,
            "componentIds": _project_strings(component_ids, "componentIds", maximum=32),
            "caseBudget": _project_integer(
                payload.get("caseBudget", limits_map.get("maxCases")), "caseBudget"
            ),
            "timeoutMs": _project_integer(
                payload.get("timeoutMs", limits_map.get("timeoutMs")), "timeoutMs"
            ),
            "retentionMode": "manual_only",
            "provenance": prov,
            "state": state,
            "createdAt": created_at,
        }
    raise EvaluationQueryError("stored definition type is unavailable")


def _capture(payload: Mapping[str, object]) -> str:
    value = payload.get("captureClassification")
    if value is None and isinstance(payload.get("captureAuthorization"), Mapping):
        value = cast(Mapping[str, object], payload["captureAuthorization"]).get(
            "captureClassification"
        )
    return _project_enum(value, "captureClassification", _CAPTURE_CLASSES)


def _id_key(kind: str) -> str:
    return {
        "datasets": "datasetId",
        "cases": "caseId",
        "suites": "suiteId",
        "evaluators": "evaluatorId",
        "jobs": "jobId",
        "attempts": "attemptId",
        "results": "resultId",
        "scores": "scoreId",
        "artifacts": "artifactId",
        "reviews": "reviewId",
        "calibrations": "calibrationId",
        "qualifications": "qualificationId",
        "deletions": "actionId",
    }.get(kind, "id")


def _version_key(kind: str) -> str:
    return {
        "datasets": "datasetVersion",
        "cases": "caseVersion",
        "suites": "suiteVersion",
        "evaluators": "evaluatorVersion",
        "calibrations": "calibrationVersion",
        "qualifications": "qualificationVersion",
    }.get(kind, "version")


def _history_type(kind: str) -> str:
    return {
        "datasets": "dataset",
        "cases": "case",
        "suites": "suite",
        "evaluators": "evaluator",
        "jobs": "job",
        "attempts": "attempt",
        "results": "result",
        "scores": "score",
        "artifacts": "artifact",
        "reviews": "review",
        "calibrations": "calibration",
        "qualifications": "qualification",
        "deletions": "deletion",
    }.get(kind, "deletion")


@dataclass(slots=True)
class EvaluationQueryStore:
    """Small bounded reference read model; no content-bearing fields."""

    resources: dict[str, list[dict[str, object]]] = field(default_factory=dict)
    history: list[dict[str, object]] = field(default_factory=list)
    idempotency: dict[str, tuple[str, str, dict[str, object]]] = field(default_factory=dict)

    def _items(self, kind: str) -> list[dict[str, object]]:
        return self.resources.setdefault(kind, [])

    def put(
        self, kind: str, item: Mapping[str, object], *, change: str = "created"
    ) -> dict[str, object]:
        value = dict(item)
        identifier = str(value.get(_id_key(kind), value.get("id", "")))
        version = str(value.get(_version_key(kind), value.get("version", "")))
        items = self._items(kind)
        for existing in items:
            if (
                str(existing.get(_id_key(kind), existing.get("id", ""))) == identifier
                and str(existing.get(_version_key(kind), existing.get("version", ""))) == version
            ):
                if _digest(existing) != _digest(value):
                    raise EvaluationConflict("immutable resource conflicts with existing state")
                return dict(existing)
        items.append(value)
        items.sort(key=lambda row: (str(row.get("createdAt", "")), identifier), reverse=True)
        self.history.insert(
            0,
            {
                "historyId": str(uuid4()),
                "resourceType": _history_type(kind),
                "resourceId": identifier,
                "resourceVersion": version or None,
                "change": change,
                "state": str(value.get("state", "created")),
                "occurredAt": iso(utc_now()),
            },
        )
        self.history[:] = self.history[:10_000]
        return dict(value)

    def get(
        self, kind: str, identifier: str, version: str | None = None
    ) -> dict[str, object] | None:
        for item in self._items(kind):
            if str(item.get(_id_key(kind), item.get("id", ""))) == identifier and (
                version is None
                or str(item.get(_version_key(kind), item.get("version", ""))) == version
            ):
                return dict(item)
        return None


def _json_mapping(value: object) -> dict[str, object]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return {}
    return dict(cast(Mapping[str, object], value)) if isinstance(value, Mapping) else {}


def _date(value: object) -> str | None:
    if isinstance(value, datetime):
        return iso(value.astimezone(UTC))
    return str(value) if value is not None else None


def _instant_filter(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise EvaluationQueryError(f"{name} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise EvaluationQueryError(f"{name} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise EvaluationQueryError(f"{name} is invalid")
    return parsed.astimezone(UTC)


class EvaluationQueryService:
    """Read-only projection service with explicit bounds and filters."""

    def __init__(
        self,
        store: EvaluationQueryStore | None = None,
        *,
        backend: EvaluationQueryBackend | None = None,
    ) -> None:
        self.store = store or EvaluationQueryStore()
        self.backend = backend

    async def collection_async(
        self,
        kind: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        if self.backend is not None:
            return await self.backend.collection(kind, cursor=cursor, limit=limit, filters=filters)
        return self.collection(kind, cursor=cursor, limit=limit, filters=filters)

    async def get_async(
        self, kind: str, identifier: str, version: str | None = None
    ) -> dict[str, object]:
        if self.backend is not None:
            found = await self.backend.get(kind, identifier, version)
            if found is None:
                raise EvaluationNotFound("evaluation resource not found")
            return found
        return self.get(kind, identifier, version)

    async def history_page_async(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, object]:
        if self.backend is not None:
            return await self.backend.history_page(
                cursor=cursor,
                limit=limit,
                resource_type=resource_type,
                resource_id=resource_id,
            )
        return self.history_page(
            cursor=cursor, limit=limit, resource_type=resource_type, resource_id=resource_id
        )

    def collection(
        self,
        kind: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        filters: Mapping[str, object] | None = None,
    ) -> dict[str, object]:
        items = list(self.store._items(kind))
        for key, expected in (filters or {}).items():
            if expected is not None:
                items = [
                    item
                    for item in items
                    if item.get(key) == expected
                    or (
                        isinstance(item.get(key), Mapping)
                        and cast(Mapping[str, object], item[key]).get("resourceId") == expected
                    )
                ]
        return page(items, cursor=cursor, limit=limit)

    def get(self, kind: str, identifier: str, version: str | None = None) -> dict[str, object]:
        _safe_identifier(identifier, "resource identifier")
        found = self.store.get(kind, identifier, version)
        if found is None:
            raise EvaluationNotFound("evaluation resource not found")
        return dict(found)

    def history_page(
        self,
        *,
        cursor: str | None = None,
        limit: int = 50,
        resource_type: str | None = None,
        resource_id: str | None = None,
    ) -> dict[str, object]:
        values = self.store.history
        if resource_type is not None:
            values = [item for item in values if item.get("resourceType") == resource_type]
        if resource_id is not None:
            values = [item for item in values if item.get("resourceId") == resource_id]
        return page(values, cursor=cursor, limit=limit)


__all__ = [
    "EvaluationConflict",
    "EvaluationNotFound",
    "EvaluationQueryError",
    "EvaluationUnavailable",
    "EvaluationQueryBackend",
    "EvaluationQueryService",
    "EvaluationQueryStore",
    "MAX_PAGE_SIZE",
    "decode_cursor",
    "encode_cursor",
    "project_definition",
]
