"""AURA-0089 content-free evaluation lifecycle-event contract tests."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, cast

import pytest
from test_aura0089_contract import FAILURE_CODES_PATH, InvalidSchema, validate_schema

ROOT = Path(__file__).parents[3]
EVENT_SCHEMA_PATH = ROOT / "contracts/events/evaluation/v1/evaluation-events.schema.json"


def _event_schema() -> dict[str, Any]:
    return json.loads(EVENT_SCHEMA_PATH.read_text(encoding="utf-8"))


EVENT_IDS = {
    "event": "10000000-0000-0000-0000-000000000001",
    "correlation": "10000000-0000-0000-0000-000000000002",
    "causation": "10000000-0000-0000-0000-000000000003",
    "request": "10000000-0000-0000-0000-000000000004",
    "job": "10000000-0000-0000-0000-000000000005",
    "attempt": "10000000-0000-0000-0000-000000000006",
    "dataset": "10000000-0000-0000-0000-000000000007",
    "suite": "10000000-0000-0000-0000-000000000008",
    "result": "10000000-0000-0000-0000-000000000009",
}
DIGEST = "b" * 64
AT = "2026-10-18T12:00:00Z"


def _event(event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    return {
        "schemaVersion": 1,
        "eventId": EVENT_IDS["event"],
        "eventType": event_type,
        "occurredAt": AT,
        "correlationId": EVENT_IDS["correlation"],
        "causationId": EVENT_IDS["causation"],
        "requestId": EVENT_IDS["request"],
        "jobId": EVENT_IDS["job"],
        "attemptId": EVENT_IDS["attempt"],
        "idempotencyKey": "10000000-0000-0000-0000-000000000010",
        "provenance": {
            "requestVersion": "1.0.0",
            "requestDigest": DIGEST,
            "datasetId": EVENT_IDS["dataset"],
            "datasetVersion": "1.0.0",
            "suiteId": EVENT_IDS["suite"],
            "suiteVersion": "1.0.0",
        },
        "data": data,
    }


@pytest.fixture
def events() -> dict[str, dict[str, Any]]:
    return {
        "evaluation.requested": _event(
            "evaluation.requested",
            {
                "captureClassification": "metadata_only",
                "deadlineAt": "2026-10-18T12:05:00Z",
                "maxAttempts": 2,
            },
        ),
        "evaluation.completed": _event(
            "evaluation.completed",
            {"resultId": EVENT_IDS["result"], "resultDigest": DIGEST, "artifactReferenceIds": []},
        ),
        "evaluation.cancelled": _event("evaluation.cancelled", {"reasonCode": "OWNER_REQUESTED"}),
        "evaluation.failed": _event(
            "evaluation.failed", {"failureCode": "DEPENDENCY_UNAVAILABLE", "retryable": True}
        ),
    }


def test_event_schema_meta_contract_is_versioned_closed_and_exactly_four_event_kinds() -> None:
    schema = _event_schema()
    assert schema["$schema"].endswith("/draft/2020-12/schema")
    assert schema["$id"].endswith("/contracts/events/evaluation/v1/evaluation-events.schema.json")
    refs = [entry["$ref"] for entry in schema["oneOf"]]
    assert refs == [
        "#/$defs/RequestedEvent",
        "#/$defs/CompletedEvent",
        "#/$defs/CancelledEvent",
        "#/$defs/FailedEvent",
    ]
    assert len(set(refs)) == 4
    for ref in refs:
        definition = schema["$defs"][ref.removeprefix("#/$defs/")]
        assert definition["additionalProperties"] is False
        assert set(definition["required"]).issubset(definition["properties"])
    assert set(schema["$defs"]["CaptureClassification"]["enum"]) == {
        "metadata_only",
        "hashed",
        "redacted",
        "sampled_content",
        "full_content",
    }


def test_valid_representative_for_every_lifecycle_event(events: dict[str, dict[str, Any]]) -> None:
    event_schema = _event_schema()
    for event_type, value in events.items():
        # The event validator is intentionally imported from the contract test
        # module, so use its schema root for this independent event contract.
        validate_schema(value, event_schema, event_schema, f"$.{event_type}")


@pytest.mark.parametrize(
    "field",
    [
        "input",
        "output",
        "prompt",
        "rubric",
        "comment",
        "credential",
        "endpoint",
        "objectKey",
        "ownerIdentity",
        "exceptionText",
        "hiddenReasoning",
        "arbitraryAttributes",
    ],
)
def test_event_payload_rejects_content_credentials_endpoints_and_owner_identity(
    events: dict[str, dict[str, Any]], field: str
) -> None:
    candidate = deepcopy(events["evaluation.completed"])
    candidate["data"][field] = "must remain outside durable events"
    with pytest.raises(InvalidSchema):
        validate_schema(candidate, _event_schema(), _event_schema(), "$.event")


def test_event_required_identifiers_and_idempotency_are_not_optional(
    events: dict[str, dict[str, Any]],
) -> None:
    event_schema = _event_schema()
    for field in (
        "eventId",
        "correlationId",
        "causationId",
        "requestId",
        "jobId",
        "attemptId",
        "idempotencyKey",
        "provenance",
    ):
        candidate = deepcopy(events["evaluation.requested"])
        del candidate[field]
        with pytest.raises(InvalidSchema):
            validate_schema(candidate, event_schema, event_schema, "$.event")


def test_event_conflicting_data_and_unbounded_failure_details_are_rejected(
    events: dict[str, dict[str, Any]],
) -> None:
    event_schema = _event_schema()
    metadata = deepcopy(events["evaluation.requested"])
    metadata["data"]["captureClassification"] = "full_content"
    # Lifecycle events carry only classification; explicit authorization and
    # content belong to the resource contract, never the durable event.
    metadata["data"]["input"] = "raw evaluation content"
    with pytest.raises(InvalidSchema):
        validate_schema(metadata, event_schema, event_schema, "$.event")

    failure = deepcopy(events["evaluation.failed"])
    failure["data"]["failureCode"] = "provider returned secret=token"
    with pytest.raises(InvalidSchema):
        validate_schema(failure, event_schema, event_schema, "$.event")


def test_event_idempotency_and_artifact_references_are_bounded_and_opaque(
    events: dict[str, dict[str, Any]],
) -> None:
    event_schema = _event_schema()
    completed = deepcopy(events["evaluation.completed"])
    completed["data"]["artifactReferenceIds"] = [EVENT_IDS["result"], EVENT_IDS["result"]]
    with pytest.raises(InvalidSchema):
        validate_schema(completed, event_schema, event_schema, "$.event")

    completed = deepcopy(events["evaluation.completed"])
    completed["data"]["artifactReferenceIds"] = ["https://storage.invalid/private/object"]
    with pytest.raises(InvalidSchema):
        validate_schema(completed, event_schema, event_schema, "$.event")

    failed = deepcopy(events["evaluation.failed"])
    failed["data"]["retryable"] = "yes"
    with pytest.raises(InvalidSchema):
        validate_schema(failed, event_schema, event_schema, "$.event")


def test_event_array_and_number_bounds_accept_exact_maximum_and_reject_one_past(
    events: dict[str, dict[str, Any]],
) -> None:
    event_schema = _event_schema()
    requested = deepcopy(events["evaluation.requested"])
    requested["data"]["maxAttempts"] = 10
    validate_schema(requested, event_schema, event_schema, "$.requested")
    requested["data"]["maxAttempts"] = 11
    with pytest.raises(InvalidSchema):
        validate_schema(requested, event_schema, event_schema, "$.requested")

    completed = deepcopy(events["evaluation.completed"])
    completed["data"]["artifactReferenceIds"] = [
        f"10000000-0000-0000-0000-{index:012d}" for index in range(1, 10001)
    ]
    validate_schema(completed, event_schema, event_schema, "$.completed")
    completed["data"]["artifactReferenceIds"].append("10000000-0000-0000-0000-000000010001")
    with pytest.raises(InvalidSchema):
        validate_schema(completed, event_schema, event_schema, "$.completed")


def test_each_registered_failure_category_is_valid_and_unregistered_details_fail(
    events: dict[str, dict[str, Any]],
) -> None:
    event_schema = _event_schema()
    categories = json.loads(FAILURE_CODES_PATH.read_text(encoding="utf-8"))["enum"]
    for category in categories:
        failed = deepcopy(events["evaluation.failed"])
        failed["data"]["failureCode"] = category
        validate_schema(failed, event_schema, event_schema, "$.event")
    for invalid in (
        "PROVIDER_TIMEOUT",
        "credential=secret",
        "exception ValueError",
        "Bearer token",
    ):
        failed = deepcopy(events["evaluation.failed"])
        failed["data"]["failureCode"] = invalid
        with pytest.raises(InvalidSchema):
            validate_schema(failed, event_schema, event_schema, "$.event")


def test_production_telemetry_schema_is_metadata_only_and_does_not_reference_evaluation() -> None:
    telemetry_paths = sorted(
        (ROOT / "contracts/telemetry").glob("*/metadata-observation.schema.json")
    )
    assert telemetry_paths
    for path in telemetry_paths:
        telemetry = json.loads(path.read_text(encoding="utf-8"))
        assert telemetry["$id"].split("/contracts/")[-1].startswith("telemetry/")
        assert telemetry["properties"]["capturePolicy"] == {"const": "metadata_only"}

        def keys(value: Any) -> set[str]:
            if isinstance(value, dict):
                value_dict = cast(dict[str, Any], value)
                result: set[str] = set(value_dict)
                for child in value_dict.values():
                    result.update(keys(child))
                return result
            if isinstance(value, list):
                value_list = cast(list[Any], value)
                result: set[str] = set()
                for child in value_list:
                    result.update(keys(child))
                return result
            return set()

        assert "evaluation.schema.json" not in json.dumps(telemetry, sort_keys=True)
        assert "evaluation-events.schema.json" not in json.dumps(telemetry, sort_keys=True)
        contract_keys = {key.casefold() for key in keys(telemetry)}
        for forbidden in (
            "prompt",
            "response",
            "memory",
            "document",
            "credential",
            "endpoint",
            "owneridentity",
            "objectkey",
        ):
            assert forbidden not in contract_keys
