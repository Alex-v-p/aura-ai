"""AURA-0112 authoritative suite projection and replay coverage."""
# pyright: reportPrivateUsage=false, reportUnknownVariableType=false

from __future__ import annotations

import json
import re
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from aura_observatory.adapters.evaluation_query import PostgresEvaluationQueryBackend
from aura_observatory.evaluation_api import EvaluationApplicationService, _project_definition_replay
from aura_observatory.evaluation_query import EvaluationUnavailable, project_definition

DATASET = "00000000-0000-4000-8000-000000011201"
SUITE = "00000000-0000-4000-8000-000000011202"
EVALUATOR = "00000000-0000-4000-8000-000000011203"
OPENAPI = Path(__file__).parents[3] / "contracts/openapi/observatory-v1.yaml"


def _component() -> dict[str, str]:
    return {
        "componentId": "aura.evaluation.synthetic",
        "componentVersion": "1.0.0",
        "codeRevision": "aura0112",
        "imageDigest": "a" * 64,
        "configurationRevision": "aura0112",
    }


def _limits() -> dict[str, int]:
    return {
        "timeoutMs": 30_000,
        "maxConcurrency": 1,
        "maxAttempts": 2,
        "maxCases": 3,
        "maxInputBytes": 1000,
        "maxOutputBytes": 2000,
        "maxTokens": 3000,
    }


def _suite(*, include_variant: bool = True) -> dict[str, object]:
    value: dict[str, object] = {
        "schemaVersion": 1,
        "resourceType": "suite",
        "suiteId": SUITE,
        "suiteVersion": "1.0.0",
        "dataset": {"id": DATASET, "version": "1.0.0"},
        "evaluators": [{
            "evaluatorId": EVALUATOR,
            "evaluatorVersion": "1.0.0",
            "kind": "deterministic",
        }],
        "components": [_component()],
        "limits": _limits(),
        "manualThresholds": [],
        "retentionMode": "manual_only",
        "provenance": {
            "createdByComponent": "aura.evaluation.synthetic",
            "createdByVersion": "1.0.0",
            "sourceDigest": "b" * 64,
        },
        "createdAt": "2026-10-24T00:00:00Z",
    }
    if include_variant:
        value["variantBindings"] = [{
            "variantId": "control",
            "components": [_component()],
            "model": {
                "modelId": "fixture",
                "modelVersion": "1.0.0",
                "modelDigest": "c" * 64,
            },
            "configurationRevision": "config-control",
            "promptRevision": "prompt-control",
            "policyRevision": "policy-control",
            "deploymentRevision": "deployment-control",
        }]
    return value


def _legacy_summary() -> dict[str, object]:
    return {
        "resourceType": "suite",
        "suiteId": SUITE,
        "suiteVersion": "1.0.0",
        "dataset": {"id": DATASET, "version": "1.0.0"},
        "evaluators": [{
            "evaluatorId": EVALUATOR,
            "evaluatorVersion": "1.0.0",
            "kind": "deterministic",
        }],
        "componentIds": ["aura.evaluation.synthetic"],
        "caseBudget": 3,
        "timeoutMs": 30_000,
        "retentionMode": "manual_only",
        "provenance": {
            "createdByComponent": "aura.evaluation.synthetic",
            "createdByVersion": "1.0.0",
            "sourceDigest": "b" * 64,
        },
        "createdAt": "2026-10-24T00:00:00Z",
    }


def _expected_enriched() -> dict[str, object]:
    projected = project_definition(_suite())
    assert isinstance(projected, dict)
    return projected


def test_full_provider_definition_projects_authoritative_fields_and_summary() -> None:
    projected = _expected_enriched()
    assert set(projected) == {
        "schemaVersion", "resourceType", "suiteId", "suiteVersion", "dataset", "evaluators",
        "componentIds", "components", "variantBindings", "caseBudget", "timeoutMs", "limits",
        "retentionMode", "provenance", "state", "createdAt",
    }
    assert projected["componentIds"] == ["aura.evaluation.synthetic"]
    assert projected["components"] == [_component()]
    assert projected["limits"] == _limits()
    assert projected["caseBudget"] == _limits()["maxCases"]
    assert projected["timeoutMs"] == _limits()["timeoutMs"]
    assert projected["variantBindings"] == _suite()["variantBindings"]


def test_legacy_summary_remains_readable_without_authoritative_optional_fields() -> None:
    projected = project_definition(_legacy_summary())
    assert set(projected) == {
        "schemaVersion", "resourceType", "suiteId", "suiteVersion", "dataset", "evaluators",
        "componentIds", "caseBudget", "timeoutMs", "retentionMode", "provenance", "state",
        "createdAt",
    }


def _openapi_schema(document: dict[str, Any], name: str) -> dict[str, Any]:
    components = cast(dict[str, Any], document["components"])
    schemas = cast(dict[str, Any], components["schemas"])
    return cast(dict[str, Any], schemas[name])


def _validate_openapi(
    value: Any, schema: dict[str, Any], document: dict[str, Any], path: str
) -> None:
    reference = schema.get("$ref")
    if reference is not None:
        prefix = "#/components/schemas/"
        assert isinstance(reference, str) and reference.startswith(prefix), f"{path}: bad ref"
        _validate_openapi(
            value,
            _openapi_schema(document, reference.removeprefix(prefix)),
            document,
            path,
        )
        return
    for keyword, expected in (("oneOf", 1), ("anyOf", 1)):
        alternatives = schema.get(keyword)
        if alternatives is None:
            continue
        matches = 0
        for candidate in cast(list[dict[str, Any]], alternatives):
            try:
                _validate_openapi(value, candidate, document, path)
            except AssertionError:
                continue
            matches += 1
        assert matches == expected, f"{path}: {keyword} matched {matches} branches"
        if keyword == "oneOf":
            return

    expected_type = schema.get("type")
    if expected_type is not None:
        types = (
            [expected_type]
            if isinstance(expected_type, str)
            else cast(list[str], expected_type)
        )
        matches_type = any(
            (item == "object" and isinstance(value, dict))
            or (item == "array" and isinstance(value, list))
            or (item == "string" and isinstance(value, str))
            or (item == "integer" and isinstance(value, int) and not isinstance(value, bool))
            or (
                item == "number"
                and isinstance(value, (int, float))
                and not isinstance(value, bool)
            )
            or (item == "boolean" and isinstance(value, bool))
            for item in types
        )
        assert matches_type, f"{path}: type mismatch"
    if "const" in schema:
        assert value == schema["const"], f"{path}: const mismatch"
    if "enum" in schema:
        assert value in schema["enum"], f"{path}: enum mismatch"
    if isinstance(value, str):
        assert len(value) >= schema.get("minLength", 0), f"{path}: too short"
        assert len(value) <= schema.get("maxLength", 2**31), f"{path}: too long"
        pattern = schema.get("pattern")
        if pattern is not None:
            assert re.fullmatch(pattern, value) is not None, f"{path}: pattern mismatch"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        assert value >= schema.get("minimum", float("-inf")), f"{path}: below minimum"
        assert value <= schema.get("maximum", float("inf")), f"{path}: above maximum"
    if isinstance(value, list):
        items = cast(list[Any], value)
        assert len(items) >= schema.get("minItems", 0), f"{path}: too few items"
        assert len(items) <= schema.get("maxItems", 2**31), f"{path}: too many items"
        if schema.get("uniqueItems"):
            encoded = {json.dumps(item, sort_keys=True) for item in items}
            assert len(encoded) == len(items), f"{path}: duplicate items"
        item_schema = schema.get("items")
        if item_schema is not None:
            for index, item in enumerate(items):
                _validate_openapi(
                    item,
                    cast(dict[str, Any], item_schema),
                    document,
                    f"{path}[{index}]",
                )
    if isinstance(value, dict):
        object_value = cast(dict[str, Any], value)
        required = cast(list[str], schema.get("required", []))
        assert all(name in object_value for name in required), f"{path}: missing required field"
        properties = cast(dict[str, Any], schema.get("properties", {}))
        if schema.get("additionalProperties") is False:
            assert set(object_value) <= set(properties), f"{path}: unknown field"
        for name, child in properties.items():
            if name in object_value:
                _validate_openapi(object_value[name], child, document, f"{path}.{name}")


def test_legacy_and_enriched_shapes_match_unchanged_openapi_suite_contract() -> None:
    document = cast(dict[str, object], yaml.safe_load(OPENAPI.read_text(encoding="utf-8")))
    components = cast(dict[str, object], document["components"])
    schemas = cast(dict[str, object], components["schemas"])
    suite_schema = cast(dict[str, object], schemas["EvaluationSuite"])
    properties = cast(dict[str, object], suite_schema["properties"])
    required = set(cast(list[str], suite_schema["required"]))

    legacy = project_definition(_legacy_summary())
    enriched = _expected_enriched()
    assert suite_schema["additionalProperties"] is False
    assert required <= set(legacy) <= set(properties)
    assert required <= set(enriched) <= set(properties)
    for schema_name, value in (
        ("EvaluationComponentBinding", _component()),
        ("EvaluationExecutionLimits", _limits()),
    ):
        nested = cast(dict[str, object], schemas[schema_name])
        assert nested["additionalProperties"] is False
        assert set(cast(list[str], nested["required"])) == set(cast(dict[str, object], value))
    variants = cast(list[dict[str, object]], _expected_enriched()["variantBindings"])
    variant = variants[0]
    variant_schema = cast(dict[str, object], schemas["EvaluationExperimentVariant"])
    assert variant_schema["additionalProperties"] is False
    assert set(cast(list[str], variant_schema["required"])) == set(variant)
    document_for_validation = cast(dict[str, Any], document)
    suite_schema_for_validation = _openapi_schema(document_for_validation, "EvaluationSuite")
    _validate_openapi(legacy, suite_schema_for_validation, document_for_validation, "$legacy")
    _validate_openapi(enriched, suite_schema_for_validation, document_for_validation, "$enriched")


def _remove_component_digest(value: dict[str, object]) -> None:
    components = cast(list[dict[str, object]], value["components"])
    components[0].pop("imageDigest")


def _mismatch_summary_case_budget(value: dict[str, object]) -> None:
    value["caseBudget"] = 4


def _duplicate_variant(value: dict[str, object]) -> None:
    variants = cast(list[dict[str, object]], value["variantBindings"])
    variants.append(deepcopy(variants[0]))


def _mismatch_component_ids(value: dict[str, object]) -> None:
    value["componentIds"] = ["aura.other.component"]


@pytest.mark.parametrize(
    "mutate",
    [
        _remove_component_digest,
        _mismatch_summary_case_budget,
        _duplicate_variant,
        _mismatch_component_ids,
    ],
)
def test_malformed_or_inconsistent_optional_fields_fail_closed(
    mutate: Callable[[dict[str, object]], None],
) -> None:
    value = _suite()
    mutate(value)
    with pytest.raises(EvaluationUnavailable):
        project_definition(value)


@pytest.mark.parametrize(
    "field",
    ["codeRevision", "configurationRevision"],
)
@pytest.mark.parametrize("forbidden", ["release.hash", "release.digest", "release.endpoint"])
def test_component_revision_identifiers_apply_contract_exclusions(
    field: str, forbidden: str,
) -> None:
    value = _suite(include_variant=False)
    components = cast(list[dict[str, object]], value["components"])
    components[0][field] = forbidden
    with pytest.raises(EvaluationUnavailable):
        project_definition(value)

    benign = _suite(include_variant=False)
    benign_components = cast(list[dict[str, object]], benign["components"])
    benign_components[0][field] = "release.hashless"
    assert project_definition(benign)["components"] == benign_components


@pytest.mark.parametrize(
    "field",
    ["configurationRevision", "promptRevision", "policyRevision", "deploymentRevision"],
)
@pytest.mark.parametrize("forbidden", ["release.hash", "release.digest", "release.endpoint"])
def test_variant_revision_identifiers_apply_contract_exclusions(
    field: str, forbidden: str,
) -> None:
    value = _suite()
    variants = cast(list[dict[str, object]], value["variantBindings"])
    variants[0][field] = forbidden
    with pytest.raises(EvaluationUnavailable):
        project_definition(value)

    benign = _suite()
    benign_variants = cast(list[dict[str, object]], benign["variantBindings"])
    benign_variants[0][field] = "release.hashless"
    assert project_definition(benign)["variantBindings"] == benign_variants


def test_variant_identifier_rejects_private_segments_but_accepts_substrings() -> None:
    value = _suite()
    variants = cast(list[dict[str, object]], value["variantBindings"])
    variants[0]["variantId"] = "owner.identity"
    with pytest.raises(EvaluationUnavailable):
        project_definition(value)

    benign = _suite()
    benign_variants = cast(list[dict[str, object]], benign["variantBindings"])
    benign_variants[0]["variantId"] = "owneridentity"
    assert project_definition(benign)["variantBindings"] == benign_variants


@pytest.mark.parametrize("field", ["variantId", "modelId"])
@pytest.mark.parametrize(
    "forbidden", ["owner.identity", "url", "uri", "filename", "path"]
)
def test_variant_and_model_identifiers_reject_private_segments(
    field: str, forbidden: str,
) -> None:
    value = _suite()
    variant = cast(list[dict[str, object]], value["variantBindings"])[0]
    if field == "modelId":
        model = cast(dict[str, object], variant["model"])
        model[field] = forbidden
    else:
        variant[field] = forbidden
    with pytest.raises(EvaluationUnavailable, match="^stored suite"):
        project_definition(value)

    benign = _suite()
    benign_variant = cast(list[dict[str, object]], benign["variantBindings"])[0]
    benign_value = "owneridentity" if forbidden == "owner.identity" else f"{forbidden}ish"
    if field == "modelId":
        benign_model = cast(dict[str, object], benign_variant["model"])
        benign_model[field] = benign_value
    else:
        benign_variant[field] = benign_value
    assert project_definition(benign)["variantBindings"] == benign["variantBindings"]


class _Result:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.rows[0] if self.rows else None


class _Connection:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    async def execute(self, _sql: str, _params: tuple[object, ...]) -> _Result:
        return _Result([(json.dumps(self.payload),)])


@pytest.mark.asyncio
async def test_postgres_list_and_detail_use_the_same_enriched_projector() -> None:
    payload = _suite()
    connection = _Connection(payload)

    @asynccontextmanager
    async def connection_factory() -> AsyncGenerator[_Connection]:
        yield connection

    backend = PostgresEvaluationQueryBackend(connection_factory)
    listed = await backend.collection("suites", cursor=None, limit=1, filters=None)
    detailed = await backend.get("suites", SUITE, "1.0.0")
    assert listed["items"] == [deepcopy(_expected_enriched())]
    assert detailed == _expected_enriched()


@pytest.mark.asyncio
async def test_create_and_idempotent_replay_preserve_enriched_projection() -> None:
    service = EvaluationApplicationService()
    request = _suite()
    request.pop("schemaVersion")
    request.pop("resourceType")
    first = await service.create_definition("suite", request, idempotency_key="suite-replay-0112")
    replay = await service.create_definition("suite", request, idempotency_key="suite-replay-0112")
    assert first == replay == _expected_enriched()
    assert _project_definition_replay("suite", first, SUITE, "1.0.0") == first


def test_frontend_job_admission_payload_has_authoritative_component_and_limit_bindings() -> None:
    projected = project_definition(_suite(include_variant=False))
    assert projected["components"] == [_component()]
    assert projected["limits"] == _limits()
    components = cast(list[dict[str, object]], projected["components"])
    assert projected["componentIds"] == [
        component["componentId"] for component in components
    ]
