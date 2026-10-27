"""Acceptance checks for the calendar contract retained in aura-v1.21.0."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from pathlib import Path
from typing import Any, cast

import pytest


ROOT = Path(__file__).parents[2]
CONTRACT_PATH = ROOT / "contracts/openapi/aura-v1.yaml"
GENERATED_PATH = ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"

CALENDAR_PATHS = {
    "/api/v1/integrations/calendar/connection",
    "/api/v1/integrations/calendar/connection/start",
    "/api/v1/integrations/calendar/connection/complete",
    "/api/v1/integrations/calendar/connection/reauthorize",
    "/api/v1/integrations/calendar/connection/disconnect",
    "/api/v1/integrations/calendar/calendars",
    "/api/v1/integrations/calendar/calendars/selection",
    "/api/v1/integrations/calendar/events/query",
    "/api/v1/integrations/calendar/conflicts/query",
}
CALENDAR_OPERATIONS = {
    "getCalendarConnection",
    "startCalendarConnection",
    "completeCalendarConnectionCallback",
    "reauthorizeCalendarConnection",
    "disconnectCalendarConnection",
    "listSelectableCalendars",
    "replaceCalendarSelection",
    "queryCalendarEvents",
    "queryCalendarConflicts",
}
MUTATION_OPERATIONS = {
    "startCalendarConnection",
    "completeCalendarConnectionCallback",
    "reauthorizeCalendarConnection",
    "disconnectCalendarConnection",
    "replaceCalendarSelection",
}


def _contract() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(CONTRACT_PATH.read_text()))


def _schemas(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return cast(dict[str, dict[str, Any]], contract["components"]["schemas"])


def _operation(contract: dict[str, Any], path: str, method: str) -> dict[str, Any]:
    return cast(dict[str, Any], contract["paths"][path][method])


def _schema(contract: dict[str, Any], name: str) -> dict[str, Any]:
    return _schemas(contract)[name]


def _ref(name: str) -> str:
    return f"#/components/schemas/{name}"


def _response_schema(operation: dict[str, Any], status: str) -> str:
    response = cast(dict[str, Any], operation["responses"][status])
    return cast(
        str,
        response["content"]["application/json"]["schema"]["$ref"],
    ).rsplit("/", maxsplit=1)[-1]


def _parameter_refs(operation: dict[str, Any]) -> set[str]:
    return {
        cast(str, parameter["$ref"])
        for parameter in cast(list[dict[str, Any]], operation.get("parameters", []))
        if "$ref" in parameter
    }


def _git_baseline() -> dict[str, Any] | None:
    result = subprocess.run(
        ["git", "show", "origin/dev:contracts/openapi/aura-v1.yaml"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    return cast(dict[str, Any], json.loads(result.stdout))


def test_calendar_contract_is_v119_additive_and_operations_are_unique() -> None:
    contract = _contract()
    assert contract["info"]["version"] == "1.21.0"
    assert set(contract["paths"]) >= CALENDAR_PATHS
    assert {"name": "Calendar"} in contract["tags"]

    operation_ids: list[str] = []
    for path, item in cast(dict[str, dict[str, Any]], contract["paths"]).items():
        for method, operation in item.items():
            if method in {"parameters", "summary", "description"}:
                continue
            operation_ids.append(cast(str, operation["operationId"]))
    assert len(operation_ids) == len(set(operation_ids))
    assert CALENDAR_OPERATIONS <= set(operation_ids)

    baseline = _git_baseline()
    if baseline is not None:
        baseline_paths = cast(dict[str, dict[str, Any]], baseline["paths"])
        paths = cast(dict[str, dict[str, Any]], contract["paths"])
        assert set(paths) >= set(baseline_paths)
        for path, item in baseline_paths.items():
            # Every pre-existing path item and operation is byte-for-byte
            # stable. Calendar additions may add new paths only.
            assert paths[path] == item, path
        assert contract["servers"] == baseline["servers"]
        assert contract["security"] == baseline["security"]
        assert {tag["name"] for tag in contract["tags"]} >= {
            *{tag["name"] for tag in baseline["tags"]},
            "Calendar",
        }
        assert contract["info"]["title"] == baseline["info"]["title"]
        assert contract["openapi"] == baseline["openapi"]
        for component_group, baseline_components in cast(dict[str, dict[str, Any]], baseline["components"]).items():
            current_components = cast(dict[str, Any], contract["components"])[component_group]
            for name, component in cast(dict[str, Any], baseline_components).items():
                if component_group == "schemas" and name in {
                    "Notification",
                    "NotificationStreamEvent",
                    "NotificationLifecycleEvent",
                }:
                    continue
                assert current_components[name] == component, f"{component_group}/{name}"
    else:
        # Keep the compatibility assertion useful in source archives without git refs.
        assert {
            "/api/v1/auth/session",
            "/api/v1/conversations",
            "/api/v1/runs/{run_id}/events",
            "/api/v1/memories",
            "/api/v1/reminders",
            "/api/v1/notifications",
            "/api/v1/documents",
            "/health/live",
        } <= set(contract["paths"])


def test_lifecycle_is_closed_owner_safe_and_uses_mutation_protection() -> None:
    contract = _contract()
    schemas = _schemas(contract)
    assert schemas["CalendarConnectionStatus"]["enum"] == [
        "disconnected",
        "connecting",
        "connected",
        "reauthorization_required",
        "revocation_uncertain",
        "disabled",
        "unavailable",
    ]
    assert schemas["CalendarFailureCategory"]["enum"] == [
        "authorization",
        "validation",
        "callback_rejected",
        "provider_auth",
        "provider_rate_limit",
        "provider_unavailable",
        "calendar_unavailable",
        "dependency_unavailable",
        "deadline_exceeded",
        "revocation_uncertain",
    ]
    assert schemas["CalendarRecoveryAction"]["enum"] == [
        "connect",
        "complete_connection",
        "retry",
        "reauthorize",
        "disconnect",
        "wait",
        "contact_administrator",
    ]
    assert schemas["CalendarConnectionAction"]["enum"] == [
        "connect",
        "complete_connection",
        "reauthorize",
        "disconnect",
    ]

    connection = schemas["CalendarConnection"]
    assert connection["additionalProperties"] is False
    assert set(connection["required"]) == {
        "status",
        "readsEnabled",
        "selectedCalendarCount",
        "connectedAt",
        "updatedAt",
        "lastError",
        "availableActions",
    }
    assert connection["properties"]["selectedCalendarCount"]["maximum"] == 50
    assert connection["properties"]["availableActions"]["maxItems"] == 4
    assert connection["properties"]["availableActions"]["uniqueItems"] is True
    assert set(schemas["CalendarConnectionError"]["required"]) == {
        "category",
        "message",
        "retryable",
        "recoveryAction",
        "occurredAt",
    }
    assert schemas["CalendarConnectionError"]["properties"]["message"]["maxLength"] == 500

    lifecycle = {
        "/api/v1/integrations/calendar/connection/start": "post",
        "/api/v1/integrations/calendar/connection/complete": "post",
        "/api/v1/integrations/calendar/connection/reauthorize": "post",
        "/api/v1/integrations/calendar/connection/disconnect": "post",
        "/api/v1/integrations/calendar/calendars/selection": "put",
    }
    for path, method in lifecycle.items():
        operation = _operation(contract, path, method)
        assert {"#/components/parameters/CsrfToken", "#/components/parameters/IdempotencyKey"} <= (
            _parameter_refs(operation)
        )
        assert "requestBody" not in operation or "authorization" not in json.dumps(
            operation["requestBody"],
            separators=(",", ":"),
        ).lower()

    callback = _operation(contract, "/api/v1/integrations/calendar/connection/complete", "post")
    assert "requestBody" not in callback
    assert _response_schema(callback, "200") == "CalendarConnection"
    start = _schema(contract, "CalendarAuthorizationStart")
    assert start["additionalProperties"] is False
    assert set(start["required"]) == {"connection", "authorizationPath", "expiresAt"}
    assert start["properties"]["authorizationPath"]["maxLength"] == 192
    assert re.search(
        start["properties"]["authorizationPath"]["pattern"],
        "/api/v1/integrations/calendar/authorize/" + "a" * 43,
    )


def test_oauth_navigation_handle_and_completion_are_bound_and_fail_closed() -> None:
    contract = _contract()
    paths = cast(dict[str, dict[str, Any]], contract["paths"])
    start = _operation(contract, "/api/v1/integrations/calendar/connection/start", "post")
    reauthorize = _operation(contract, "/api/v1/integrations/calendar/connection/reauthorize", "post")
    complete = _operation(contract, "/api/v1/integrations/calendar/connection/complete", "post")

    start_text = json.dumps(start, separators=(",", ":")).lower()
    reauthorize_text = json.dumps(reauthorize, separators=(",", ":")).lower()
    start_normalized = start_text.replace("-", " ")
    for phrase in (
        "authenticated same-origin",
        "initiating session",
        "exact attempt",
        "high-entropy",
        "single-use",
        "expiresat",
        "fails closed",
        "replay",
        "prefetch",
        "newer attempt",
        "invalidat",
    ):
        assert phrase in start_text or phrase.replace("-", " ") in start_normalized, phrase
    assert "provider endpoint" not in start_text
    reauthorize_normalized = reauthorize_text.replace("-", " ")
    for phrase in ("authenticated same-origin", "initiating session", "exact attempt", "high-entropy", "single-use", "expiry", "replay", "prefetch", "newer attempt", "invalidat"):
        assert phrase in reauthorize_text or phrase.replace("-", " ") in reauthorize_normalized, phrase

    authorization = _schema(contract, "CalendarAuthorizationStart")
    authorization_text = json.dumps(authorization, separators=(",", ":")).lower()
    for phrase in (
        "authenticated same-origin",
        "owner",
        "initiating session",
        "connection attempt",
        "high-entropy",
        "single-use",
        "expires exactly",
        "fail closed",
        "superseded",
        "replayed",
        "prefetched",
        "owner-mismatched",
        "session-mismatched",
        "attempt-mismatched",
    ):
        assert phrase in authorization_text, phrase
    authorization_path = authorization["properties"]["authorizationPath"]
    assert authorization_path["maxLength"] == 192
    assert re.search(authorization_path["pattern"], "/api/v1/integrations/calendar/authorize/" + "a" * 43)
    assert not re.search(authorization_path["pattern"], "/api/v1/integrations/calendar/authorize/" + "a" * 42)

    completion_text = json.dumps(complete, separators=(",", ":")).lower()
    for phrase in (
        "consumes exactly once",
        "server-held result",
        "authenticated owner",
        "initiating session",
        "exact non-expired connection attempt",
        "exact state",
        "nonce",
        "callback origin",
        "missing",
        "expired",
        "superseded",
        "prefetched",
        "replayed",
        "owner-mismatched",
        "session-mismatched",
        "attempt-mismatched",
        "no provider authorization code",
        "no provider authorization code, state, token",
    ):
        assert phrase in completion_text, phrase
    assert "requestBody" not in complete
    assert set(complete["responses"]) >= {"200", "400", "401", "403", "409", "422", "503"}
    assert paths["/api/v1/integrations/calendar/connection/complete"]["post"]["responses"]["200"]["headers"]["Cache-Control"]["schema"]["const"] == "no-store"


def test_selection_discovery_and_event_query_bounds_are_explicit() -> None:
    contract = _contract()
    schemas = _schemas(contract)
    calendar = schemas["SelectableCalendar"]
    assert calendar["additionalProperties"] is False
    assert set(calendar["required"]) == {"id", "displayName", "timeZone", "selected", "discoveredAt"}
    assert "false on first discovery" in calendar["properties"]["selected"]["description"].lower()

    page = schemas["SelectableCalendarPage"]
    assert page["properties"]["items"]["maxItems"] == 100
    assert page["properties"]["nextCursor"]["maxLength"] == 1024
    assert page["properties"]["selectionVersion"]["minimum"] == 1

    selection_request = schemas["ReplaceCalendarSelectionRequest"]
    assert selection_request["additionalProperties"] is False
    assert set(selection_request["required"]) == {"calendarIds", "expectedVersion"}
    assert selection_request["properties"]["calendarIds"]["maxItems"] == 50
    assert selection_request["properties"]["calendarIds"]["uniqueItems"] is True
    assert "empty list" in selection_request["description"]
    assert "never appended" in selection_request["description"]

    query = schemas["CalendarEventQuery"]
    assert query["additionalProperties"] is False
    assert set(query["required"]) == {"rangeStart", "rangeEnd", "displayTimeZone", "calendarIds"}
    assert query["properties"]["calendarIds"]["minItems"] == 1
    assert query["properties"]["calendarIds"]["maxItems"] == 50
    assert query["properties"]["calendarIds"]["uniqueItems"] is True
    assert query["properties"]["limit"]["maximum"] == 100
    assert query["properties"]["cursor"]["maxLength"] == 1024
    assert (query["properties"]["timeoutMs"]["minimum"], query["properties"]["timeoutMs"]["maximum"]) == (500, 10000)
    assert "no more than 31 days" in query["description"]
    assert "selected set" in query["description"]
    assert "cancel" in query["description"]
    assert query["properties"]["displayTimeZone"]["$ref"] == _ref("ClientTimeZone")

    events = _operation(contract, "/api/v1/integrations/calendar/events/query", "post")
    conflicts = _operation(contract, "/api/v1/integrations/calendar/conflicts/query", "post")
    for operation in (events, conflicts):
        assert "parameters" not in operation
        assert operation["requestBody"]["required"] is True
        assert "application/json" in operation["requestBody"]["content"]


def test_event_shapes_preserve_timing_kind_and_timezone_provenance() -> None:
    contract = _contract()
    schemas = _schemas(contract)
    assert schemas["CalendarEventAvailability"]["enum"] == ["busy", "free"]
    assert schemas["CalendarEvent"]["oneOf"] == [
        {"$ref": _ref("TimedCalendarEvent")},
        {"$ref": _ref("AllDayCalendarEvent")},
    ]
    assert schemas["CalendarEvent"]["discriminator"] == {"propertyName": "kind"}

    timed = schemas["TimedCalendarEvent"]
    assert timed["properties"]["kind"]["const"] == "timed"
    assert {
        "startInstant",
        "endInstant",
        "eventTimeZone",
        "displayTimeZone",
        "spansMultipleDays",
    } <= set(timed["required"])
    assert timed["properties"]["eventTimeZone"]["$ref"] == _ref("ClientTimeZone")
    assert timed["properties"]["displayTimeZone"]["$ref"] == _ref("ClientTimeZone")
    assert "Server-computed" in timed["properties"]["spansMultipleDays"]["description"]

    all_day = schemas["AllDayCalendarEvent"]
    assert all_day["properties"]["kind"]["const"] == "all_day"
    assert {"startDate", "endDateExclusive", "eventTimeZone", "displayTimeZone"} <= set(
        all_day["required"]
    )
    assert "half-open" in all_day["description"]
    assert all_day["properties"]["endDateExclusive"]["format"] == "date"

    page = schemas["CalendarEventPage"]
    assert page["properties"]["items"]["maxItems"] == 100
    assert page["properties"]["warnings"]["maxItems"] == 50
    assert "never a durable event cache" in page["description"]


def test_conflicts_are_server_authoritative_canonical_and_bounded() -> None:
    contract = _contract()
    schemas = _schemas(contract)
    assert schemas["CalendarConflictRelationship"]["enum"] == [
        "overlaps",
        "contains",
        "contained_by",
        "same_interval",
    ]
    reference = schemas["CalendarConflictEventReference"]
    assert reference["additionalProperties"] is False
    assert set(reference["required"]) == {"eventId", "calendarId"}
    conflict = schemas["CalendarConflict"]
    assert conflict["additionalProperties"] is False
    assert set(conflict["required"]) == {
        "left",
        "right",
        "relationship",
        "overlapStart",
        "overlapEnd",
    }
    assert "once" in conflict["description"]
    assert "left precedes right" in conflict["description"]

    query = schemas["CalendarConflictQuery"]
    assert query["properties"]["conflictLimit"]["maximum"] == 200
    assert query["properties"]["cursor"]["maxLength"] == 1024
    page = schemas["CalendarConflictPage"]
    assert page["properties"]["items"]["maxItems"] == 200
    assert page["properties"]["warnings"]["maxItems"] == 50
    assert "server-authoritative" in page["description"].lower()
    assert "recalculate" in page["description"].lower()


def test_calendar_privacy_exclusions_and_no_store_responses_are_contractual() -> None:
    contract = _contract()
    paths = cast(dict[str, dict[str, Any]], contract["paths"])
    schemas = _schemas(contract)
    calendar_schema_names = {
        name
        for name in schemas
        if "Calendar" in name or name.startswith("SelectableCalendar")
    }
    calendar_text = json.dumps(
        {
            "paths": {path: paths[path] for path in CALENDAR_PATHS},
            "schemas": {name: schemas[name] for name in calendar_schema_names},
        },
        separators=(",", ":"),
    ).lower()
    for phrase in (
        "durable event-content cache",
        "ordinary logs",
        "telemetry",
        "sse",
        "browser persistence",
        "service-worker",
        "model-facing tools",
        "memory",
    ):
        assert phrase in calendar_text

    forbidden_public_fields = {
        "accessToken",
        "refreshToken",
        "clientSecret",
        "authorizationCode",
        "credentialReference",
        "providerAccountId",
        "providerError",
        "rawProviderError",
    }
    for name in calendar_schema_names:
        properties = set(cast(dict[str, Any], schemas[name].get("properties", {})))
        assert properties.isdisjoint(forbidden_public_fields), name

    for path in CALENDAR_PATHS:
        assert not any(private in path.lower() for private in ("title", "description", "attendee", "location"))
        for method, operation in cast(dict[str, Any], paths[path]).items():
            if method in {"parameters", "summary", "description"}:
                continue
            for status, response in cast(dict[str, Any], operation["responses"]).items():
                if status.startswith("2"):
                    assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store"

    assert not {
        operation
        for operation in CALENDAR_OPERATIONS
        if any(token in operation.lower() for token in ("createevent", "updateevent", "deleteevent", "rsvp", "attendee"))
    }
    assert all(paths[path].get("get", {}).get("operationId") in {"getCalendarConnection", "listSelectableCalendars"} for path in CALENDAR_PATHS if "get" in paths[path])


def test_calendar_errors_are_closed_sanitized_and_no_store_on_every_status() -> None:
    contract = _contract()
    paths = cast(dict[str, dict[str, Any]], contract["paths"])
    responses = cast(dict[str, dict[str, Any]], contract["components"]["responses"])
    calendar_error = _schema(contract, "CalendarErrorEnvelope")
    assert calendar_error["additionalProperties"] is False
    assert set(calendar_error["required"]) == {
        "type",
        "title",
        "status",
        "category",
        "detail",
        "retryable",
        "recoveryAction",
        "traceId",
    }
    assert set(calendar_error["properties"]) == set(calendar_error["required"])
    assert calendar_error["properties"]["category"]["$ref"] == _ref("CalendarFailureCategory")
    assert calendar_error["properties"]["recoveryAction"]["$ref"] == _ref("CalendarRecoveryAction")
    assert calendar_error["properties"]["detail"]["maxLength"] == 500
    assert calendar_error["properties"]["title"]["maxLength"] == 120
    assert calendar_error["properties"]["traceId"]["pattern"] == "^[0-9a-f]{32}$"
    error_text = json.dumps(calendar_error, separators=(",", ":")).lower()
    for phrase in (
        "fixed templates",
        "owner-safe",
        "calendar names",
        "request fields",
        "provider identifiers",
        "diagnostics",
        "credentials",
        "resource identifiers",
        "arbitrary field-error",
    ):
        assert phrase in error_text, phrase
    assert not {"resource", "fieldErrors", "providerError", "ownerId"} & set(calendar_error["properties"])

    for path in CALENDAR_PATHS:
        for method, operation in cast(dict[str, Any], paths[path]).items():
            if method in {"parameters", "summary", "description"}:
                continue
            for status, response in cast(dict[str, Any], operation["responses"]).items():
                if "$ref" in response:
                    response = responses[cast(str, response["$ref"]).rsplit("/", maxsplit=1)[-1]]
                assert response["headers"]["Cache-Control"]["schema"]["const"] == "no-store", (path, method, status)
                if status.startswith("2"):
                    continue
                assert response["content"]["application/problem+json"]["schema"]["$ref"] == _ref("CalendarErrorEnvelope"), (path, method, status)


def test_generated_client_exposes_every_calendar_operation_and_schema() -> None:
    contract = _contract()
    generated = GENERATED_PATH.read_text()
    digest = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()
    assert "Generated from contracts/openapi/aura-v1.yaml. Do not edit by hand." in generated
    assert f"Contract SHA-256: {digest}" in generated

    expected_types = {
        "CalendarConnectionStatus",
        "CalendarRecoveryAction",
        "CalendarFailureCategory",
        "CalendarConnectionError",
        "CalendarConnectionAction",
        "CalendarConnection",
        "CalendarAuthorizationStart",
        "SelectableCalendar",
        "SelectableCalendarPage",
        "ReplaceCalendarSelectionRequest",
        "CalendarSelection",
        "CalendarEventQuery",
        "CalendarEventAvailability",
        "TimedCalendarEvent",
        "AllDayCalendarEvent",
        "CalendarEvent",
        "CalendarReadWarning",
        "CalendarEventPage",
        "CalendarConflictQuery",
        "CalendarConflictEventReference",
        "CalendarConflictRelationship",
        "CalendarConflict",
        "CalendarConflictPage",
    }
    for name in expected_types:
        assert re.search(rf"(?:export type|export interface) {re.escape(name)}\b", generated), name

    for path, method, operation_id, request_mode in (
        ("/api/v1/integrations/calendar/connection", "GET", "getCalendarConnection", "none"),
        ("/api/v1/integrations/calendar/connection/start", "POST", "startCalendarConnection", "none"),
        ("/api/v1/integrations/calendar/connection/complete", "POST", "completeCalendarConnectionCallback", "none"),
        ("/api/v1/integrations/calendar/connection/reauthorize", "POST", "reauthorizeCalendarConnection", "none"),
        ("/api/v1/integrations/calendar/connection/disconnect", "POST", "disconnectCalendarConnection", "none"),
        ("/api/v1/integrations/calendar/calendars", "GET", "listSelectableCalendars", "none"),
        ("/api/v1/integrations/calendar/calendars/selection", "PUT", "replaceCalendarSelection", "json"),
        ("/api/v1/integrations/calendar/events/query", "POST", "queryCalendarEvents", "json"),
        ("/api/v1/integrations/calendar/conflicts/query", "POST", "queryCalendarConflicts", "json"),
    ):
        descriptor = f"{operation_id}: {{ method: '{method}', pathTemplate: '{path}', requestMode: '{request_mode}', responseMode: 'json' }}"
        assert descriptor in generated, operation_id

    operations_text = generated[generated.index("export interface AuraApiOperations"): generated.index("export type AuraOperationId")]
    for operation_id in CALENDAR_OPERATIONS:
        assert f"readonly {operation_id}:" in operations_text
