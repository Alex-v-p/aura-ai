"""AURA-0078 contract and privacy regression tests."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from aura_observatory.contract import MetadataObservation
from pydantic import ValidationError

ROOT = Path(__file__).parents[3]
SCHEMA_PATH = ROOT / "contracts/telemetry/v1/metadata-observation.schema.json"
OPENAPI_PATH = ROOT / "contracts/openapi/observatory-v1.yaml"


def _valid_payload() -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "observationId": "01234567-89ab-4cde-8123-0123456789ab",
        "observedAt": datetime.now(UTC).isoformat(),
        "kind": "metric",
        "component": {"id": "aura.runtime.model_inference", "version": "1.0.0"},
        "operation": "inference",
        "measurement": {"name": "duration_ms", "value": 12.5, "unit": "milliseconds"},
        "dimensions": {"outcome": "success"},
        "trace": {
            "traceId": "abcdefabcdefabcdefabcdefabcdefab",
            "spanId": "abcdefabcdefabcd",
        },
        "correlation": {"runId": "fedcba98-7654-4fed-8123-fedcba987654"},
        "deployment": {
            "environment": "test",
            "deploymentId": "deployment-test",
            "gitCommit": "0123456789abcdef01234567",
            "hostId": "host-test",
        },
        "serviceRole": "worker",
        "capturePolicy": "metadata_only",
    }


def test_schema_is_closed_and_declares_metadata_only_capture() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert schema["$schema"].endswith("/draft/2020-12/schema")
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False
    assert schema["properties"]["capturePolicy"] == {"const": "metadata_only"}
    forbidden = {
        "prompt",
        "response",
        "content",
        "filename",
        "toolArguments",
        "toolResult",
        "credential",
        "endpoint",
        "bearerToken",
        "ownerId",
    }
    assert not forbidden & set(schema["properties"])
    dimension_properties = set(schema["$defs"]["Dimensions"]["properties"])
    assert not {"prompt", "response", "content", "filename", "token", "url"} & dimension_properties
    for definition_name, definition in schema["$defs"].items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, definition_name
    assert set(schema["required"]) >= {
        "schemaVersion",
        "observationId",
        "observedAt",
        "component",
        "operation",
        "measurement",
        "dimensions",
        "serviceRole",
        "capturePolicy",
    }
    measurement = schema["$defs"]["Measurement"]
    assert measurement["properties"]["value"]["type"] == "number"
    assert measurement["properties"]["value"]["maximum"] <= 1.0e15


def test_contract_accepts_a_bounded_metadata_observation() -> None:
    observation = MetadataObservation.from_payload(_valid_payload())

    assert observation.to_payload()["capturePolicy"] == "metadata_only"
    assert observation.component.id.startswith("aura.")
    assert math.isfinite(observation.measurement.value)


def test_contract_accepts_canonical_core_ids_without_uuid_marker_bits() -> None:
    payload = _valid_payload()
    core_identifier = "01234567-89ab-0cde-7123-0123456789ab"
    payload["correlation"] = {
        "runId": core_identifier,
        "conversationId": "fedcba98-7654-f321-0123-fedcba987654",
    }

    observation = MetadataObservation.from_payload(payload)
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))

    assert observation.correlation is not None
    assert observation.correlation.run_id == core_identifier
    assert re.fullmatch(schema["$defs"]["Uuid"]["pattern"], core_identifier)


def test_telemetry_and_query_contracts_share_the_canonical_uuid_bound() -> None:
    telemetry = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    openapi = json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))

    assert (
        telemetry["$defs"]["Uuid"]["pattern"]
        == openapi["components"]["schemas"]["Uuid"]["pattern"]
        == r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )


@pytest.mark.parametrize(
    "identifier",
    [
        "01234567-89AB-0cde-7123-0123456789ab",
        "0123456789ab0cde71230123456789ab",
        "01234567-89ab-0cde-7123-0123456789ag",
    ],
)
def test_contract_rejects_noncanonical_or_unbounded_core_ids(identifier: str) -> None:
    payload = _valid_payload()
    payload["correlation"] = {"conversationId": identifier}

    with pytest.raises(ValidationError):
        MetadataObservation.from_payload(payload)


def test_wire_schema_and_runtime_contract_do_not_drift() -> None:
    schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    payload = MetadataObservation.from_payload(_valid_payload()).to_payload()

    # Every emitted root field must be represented by the versioned JSON
    # Schema.  This catches a producer silently widening or renaming the
    # transport contract.
    assert set(payload) <= set(schema["properties"])
    identifier_pattern = schema["$defs"]["Uuid"]["pattern"]
    assert re.fullmatch(identifier_pattern, payload["observationId"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt", "private prompt"),
        ("response", "private response"),
        ("content", "document contents"),
        ("filename", "secret.txt"),
        ("toolArguments", {"path": "/private"}),
        ("toolResult", {"value": "private"}),
        ("credential", "bearer secret"),
        ("endpoint", "https://private.invalid"),
        ("ownerId", "owner-1"),
        ("arbitraryAttribute", "must not persist"),
    ],
)
def test_contract_rejects_sensitive_or_arbitrary_top_level_fields(
    field: str, value: object
) -> None:
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises(ValidationError):
        MetadataObservation.from_payload(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("capturePolicy", "full_content"),
        ("observationId", "not-an-opaque-id"),
        ("operation", "Not bounded"),
        ("serviceRole", "owner"),
    ],
)
def test_contract_rejects_invalid_identifiers_and_capture_policy(field: str, value: object) -> None:
    payload = _valid_payload()
    payload[field] = value

    with pytest.raises((ValidationError, ValueError)):
        MetadataObservation.from_payload(payload)


def test_contract_rejects_non_finite_measurements_and_oversized_dimensions() -> None:
    payload = _valid_payload()
    payload["measurement"]["value"] = float("nan")
    with pytest.raises(ValidationError):
        MetadataObservation.from_payload(payload)


def _remove_dimensions(payload: dict[str, Any]) -> None:
    payload.pop("dimensions")


def _null_trace(payload: dict[str, Any]) -> None:
    payload["trace"] = None


def _null_parent_span(payload: dict[str, Any]) -> None:
    payload["trace"]["parentSpanId"] = None


def _null_measurement_unit(payload: dict[str, Any]) -> None:
    payload["measurement"]["unit"] = None


def _null_run_id(payload: dict[str, Any]) -> None:
    payload["correlation"]["runId"] = None


@pytest.mark.parametrize(
    "mutate",
    [_remove_dimensions, _null_trace, _null_parent_span, _null_measurement_unit, _null_run_id],
)
def test_wire_contract_rejects_missing_required_or_explicit_null_optional_fields(
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    payload = _valid_payload()
    mutate(payload)

    with pytest.raises(ValueError):
        MetadataObservation.from_payload(payload)

    payload = _valid_payload()
    payload["dimensions"] = {f"dimension_{index}": "value" for index in range(13)}
    with pytest.raises(ValidationError):
        MetadataObservation.from_payload(payload)


@pytest.mark.parametrize(
    "field", ["prompt", "response", "content", "filename", "owner", "token", "url"]
)
def test_sensitive_dimension_names_are_rejected(field: str) -> None:
    payload = _valid_payload()
    payload["dimensions"] = {field: "private"}

    with pytest.raises((ValidationError, ValueError)):
        MetadataObservation.from_payload(payload)
