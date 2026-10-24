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
# Keep this delimiter-bounded exclusion aligned with the authoritative
# evaluation-definition admission policy.  It prevents sensitive segments
# without rejecting benign values that merely contain the same characters.
_PRIVATE_SEGMENT = re.compile(
    r"(?:^|[._:+-])(owner|identity|credential|bearer|token|secret|password|private|content|endpoint|url|uri|filename|path)(?:$|[._:+-])",
    re.IGNORECASE,
)
_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_REVISION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_EVALUATION_ID = re.compile(r"^[a-z][a-z0-9_.-]{0,127}$")
_REVISION_PRIVATE_SEGMENT = re.compile(
    r"(?:^|[._:+-])(owner|identity|credential|bearer|token|secret|password|private|content|hash|digest|endpoint)(?:$|[._:+-])",
    re.IGNORECASE,
)
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


def _project_reference_version(value: object, name: str) -> str:
    """Validate a resource-reference version and its content exclusion."""

    result = _project_version(value, name)
    if _PRIVATE_SEGMENT.search(result) is not None:
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

    # Definitions are persisted with the domain spelling while an older
    # observatory-v1 read model may already contain the API spelling.  Keep
    # this compatibility boundary deliberately closed: accepting a partial
    # or hybrid map would make it impossible to tell which representation was
    # actually stored and could leak arbitrary fields through a projection.
    keys = set(value)
    if keys == {"id", "version"}:
        resource_id = value.get("id")
        version = value.get("version")
    elif keys == {"resourceId", "resourceVersion"}:
        resource_id = value.get("resourceId")
        version = value.get("resourceVersion")
    else:
        raise EvaluationQueryError(f"stored {name} is unavailable")

    if not isinstance(resource_id, str) or not isinstance(version, str):
        raise EvaluationQueryError(f"stored {name} is unavailable")
    try:
        checked_id = _safe_uuid(resource_id, f"{name}.id")
        checked_version = _project_reference_version(version, f"{name}.version")
    except EvaluationQueryError as exc:
        raise EvaluationQueryError(f"stored {name} is unavailable") from exc
    return resource_ref(checked_id, checked_version)


_SUITE_COMPONENT_FIELDS = frozenset(
    {"componentId", "componentVersion", "codeRevision", "imageDigest", "configurationRevision"}
)
_SUITE_VARIANT_FIELDS = frozenset(
    {
        "variantId",
        "components",
        "model",
        "configurationRevision",
        "promptRevision",
        "policyRevision",
        "deploymentRevision",
    }
)
_SUITE_MODEL_FIELDS = frozenset({"modelId", "modelVersion", "modelDigest"})
_SUITE_LIMIT_FIELDS = frozenset(
    {
        "timeoutMs",
        "maxConcurrency",
        "maxAttempts",
        "maxCases",
        "maxInputBytes",
        "maxOutputBytes",
        "maxTokens",
    }
)
_SUITE_LIMIT_BOUNDS: dict[str, tuple[int, int]] = {
    "timeoutMs": (100, 86_400_000),
    "maxConcurrency": (1, 64),
    "maxAttempts": (1, 10),
    "maxCases": (1, 100_000),
    "maxInputBytes": (0, 1_073_741_824),
    "maxOutputBytes": (0, 1_073_741_824),
    "maxTokens": (0, 100_000_000),
}


def _suite_unavailable(name: str) -> EvaluationUnavailable:
    return EvaluationUnavailable(f"stored suite {name} is unavailable")


def _suite_closed(value: object, fields: frozenset[str], name: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or set(value) != set(fields):
        raise _suite_unavailable(name)
    return dict(cast(Mapping[str, object], value))


def _project_suite_component(value: object, name: str) -> dict[str, str]:
    item = _suite_closed(value, _SUITE_COMPONENT_FIELDS, name)
    component_id = item.get("componentId")
    if not isinstance(component_id, str) or _COMPONENT_ID.fullmatch(component_id) is None:
        raise _suite_unavailable(name)
    try:
        component_version = _project_version(item.get("componentVersion"), f"{name}.componentVersion")
    except EvaluationQueryError as exc:
        raise _suite_unavailable(name) from exc
    revisions: dict[str, str] = {}
    for field_name in ("codeRevision", "configurationRevision"):
        value = item.get(field_name)
        if (
            not isinstance(value, str)
            or _REVISION_ID.fullmatch(value) is None
            or _REVISION_PRIVATE_SEGMENT.search(value) is not None
        ):
            raise _suite_unavailable(name)
        revisions[field_name] = value
    digest = item.get("imageDigest")
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise _suite_unavailable(name)
    return {
        "componentId": component_id,
        "componentVersion": component_version,
        "codeRevision": revisions["codeRevision"],
        "imageDigest": digest,
        "configurationRevision": revisions["configurationRevision"],
    }


def _project_suite_components(value: object, name: str) -> tuple[list[dict[str, str]], list[str]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise _suite_unavailable(name)
    components = [_project_suite_component(item, f"{name}[{index}]") for index, item in enumerate(value)]
    component_ids = [item["componentId"] for item in components]
    if len(set(component_ids)) != len(component_ids):
        raise _suite_unavailable(name)
    return components, component_ids


def _project_suite_model(value: object, name: str) -> dict[str, str]:
    item = _suite_closed(value, _SUITE_MODEL_FIELDS, name)
    model_id = item.get("modelId")
    if (
        not isinstance(model_id, str)
        or len(model_id) > 193
        or _EVALUATION_ID.fullmatch(model_id) is None
        or _PRIVATE_SEGMENT.search(model_id) is not None
    ):
        raise _suite_unavailable(name)
    try:
        model_version = _project_version(item.get("modelVersion"), f"{name}.modelVersion")
    except EvaluationQueryError as exc:
        raise _suite_unavailable(name) from exc
    model_digest = item.get("modelDigest")
    if not isinstance(model_digest, str) or _DIGEST.fullmatch(model_digest) is None:
        raise _suite_unavailable(name)
    return {"modelId": model_id, "modelVersion": model_version, "modelDigest": model_digest}


def _project_suite_variant(value: object, name: str) -> dict[str, object]:
    item = _suite_closed(value, _SUITE_VARIANT_FIELDS, name)
    components, _component_ids = _project_suite_components(item.get("components"), f"{name}.components")
    variant_id = item.get("variantId")
    if (
        not isinstance(variant_id, str)
        or _EVALUATION_ID.fullmatch(variant_id) is None
        or _PRIVATE_SEGMENT.search(variant_id) is not None
    ):
        raise _suite_unavailable(name)
    model = _project_suite_model(item.get("model"), f"{name}.model")
    revisions: dict[str, str] = {}
    for field_name in (
        "configurationRevision",
        "promptRevision",
        "policyRevision",
        "deploymentRevision",
    ):
        revision = item.get(field_name)
        if (
            not isinstance(revision, str)
            or _REVISION_ID.fullmatch(revision) is None
            or _REVISION_PRIVATE_SEGMENT.search(revision) is not None
        ):
            raise _suite_unavailable(name)
        revisions[field_name] = revision
    return {
        "variantId": variant_id,
        "components": components,
        "model": model,
        **revisions,
    }


def _project_suite_variants(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise _suite_unavailable("variantBindings")
    variants = [_project_suite_variant(item, f"variantBindings[{index}]") for index, item in enumerate(value)]
    variant_ids = [str(item["variantId"]) for item in variants]
    if len(set(variant_ids)) != len(variant_ids):
        raise _suite_unavailable("variantBindings")
    return variants


def _project_suite_limits(value: object) -> dict[str, int]:
    item = _suite_closed(value, _SUITE_LIMIT_FIELDS, "limits")
    result: dict[str, int] = {}
    for field_name, (lower, upper) in _SUITE_LIMIT_BOUNDS.items():
        field_value = item.get(field_name)
        if type(field_value) is not int or not lower <= field_value <= upper:
            raise _suite_unavailable(f"limits.{field_name}")
        result[field_name] = field_value
    return result


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
        dataset_ref = _project_reference(dataset, "dataset")
        evaluators = payload.get("evaluators")
        evaluator_items: list[dict[str, object]] = []
        if isinstance(evaluators, Sequence) and not isinstance(evaluators, (str, bytes)):
            for item in evaluators:
                item = _suite_closed(
                    item,
                    frozenset({"evaluatorId", "evaluatorVersion", "kind"}),
                    "evaluators",
                )
                try:
                    evaluator_items.append(
                        {
                            "evaluatorId": _safe_uuid(item.get("evaluatorId"), "evaluatorId"),
                            "evaluatorVersion": _project_version(
                                item.get("evaluatorVersion"), "evaluatorVersion"
                            ),
                            "kind": _project_enum(
                                item.get("kind"), "kind", {"deterministic", "local_judge"}
                            ),
                        }
                    )
                except EvaluationQueryError as exc:
                    raise _suite_unavailable("evaluators") from exc
        else:
            raise _suite_unavailable("evaluators")
        if not 1 <= len(evaluator_items) <= 32:
            raise _suite_unavailable("evaluators")

        projected_components: list[dict[str, str]] | None = None
        derived_component_ids: list[str] | None = None
        if "components" in payload:
            projected_components, derived_component_ids = _project_suite_components(
                payload.get("components"), "components"
            )
        component_ids_value = payload.get("componentIds")
        if derived_component_ids is not None:
            if component_ids_value is not None:
                try:
                    checked_component_ids = _project_strings(
                        component_ids_value, "componentIds", maximum=32
                    )
                except EvaluationQueryError as exc:
                    raise _suite_unavailable("componentIds") from exc
                if (
                    len(set(checked_component_ids)) != len(checked_component_ids)
                    or checked_component_ids != derived_component_ids
                ):
                    raise _suite_unavailable("componentIds")
            component_ids = derived_component_ids
        else:
            try:
                component_ids = _project_strings(component_ids_value, "componentIds", maximum=32)
            except EvaluationQueryError as exc:
                raise _suite_unavailable("componentIds") from exc
            if (
                not component_ids
                or len(set(component_ids)) != len(component_ids)
                or any(_COMPONENT_ID.fullmatch(item) is None for item in component_ids)
            ):
                raise _suite_unavailable("componentIds")

        projected_limits: dict[str, int] | None = None
        raw_limits = payload.get("limits")
        # Before the enriched suite projection existed, the query read model
        # retained only these two summary values under ``limits``.  Treat that
        # exact shape as the legacy summary encoding; every other present
        # limits object must be the complete authoritative contract shape.
        legacy_summary_limits = (
            "components" not in payload
            and "variantBindings" not in payload
            and isinstance(raw_limits, Mapping)
            and set(raw_limits) == {"maxCases", "timeoutMs"}
        )
        legacy_summary_values = (
            cast(Mapping[str, object], raw_limits) if legacy_summary_limits else None
        )
        if "limits" in payload and not legacy_summary_limits:
            projected_limits = _project_suite_limits(payload.get("limits"))
        case_budget_value = payload.get(
            "caseBudget",
            (
                projected_limits.get("maxCases")
                if projected_limits is not None
                else legacy_summary_values["maxCases"]
                if legacy_summary_values is not None
                else None
            ),
        )
        timeout_value = payload.get(
            "timeoutMs",
            (
                projected_limits.get("timeoutMs")
                if projected_limits is not None
                else legacy_summary_values["timeoutMs"]
                if legacy_summary_values is not None
                else None
            ),
        )
        if type(case_budget_value) is not int or not 1 <= case_budget_value <= 100_000:
            raise _suite_unavailable("caseBudget")
        if type(timeout_value) is not int or not 100 <= timeout_value <= 86_400_000:
            raise _suite_unavailable("timeoutMs")
        if projected_limits is not None and (
            projected_limits["maxCases"] != case_budget_value
            or projected_limits["timeoutMs"] != timeout_value
        ):
            raise _suite_unavailable("limits")

        result: dict[str, object] = {
            "schemaVersion": 1,
            "resourceType": "suite",
            "suiteId": _required_uuid(payload.get("suiteId"), "suiteId"),
            "suiteVersion": _project_version(payload.get("suiteVersion"), "suiteVersion"),
            "dataset": dataset_ref,
            "evaluators": evaluator_items,
            "componentIds": component_ids,
            "caseBudget": case_budget_value,
            "timeoutMs": timeout_value,
            "retentionMode": "manual_only",
            "provenance": prov,
            "state": state,
            "createdAt": created_at,
        }
        if projected_components is not None:
            result["components"] = projected_components
        if "variantBindings" in payload:
            result["variantBindings"] = _project_suite_variants(payload.get("variantBindings"))
        if projected_limits is not None:
            result["limits"] = projected_limits
        return result
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
