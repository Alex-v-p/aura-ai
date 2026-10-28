"""Contract acceptance for AURA-0144 governed research providers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).parents[2]
SCHEMA_PATH = ROOT / "contracts/providers/research/v1/research-provider-transport.schema.json"
FIXTURES_PATH = ROOT / "contracts/providers/research/v1/conformance-fixtures.json"


def _json(path: Path) -> dict[str, Any]:
    return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))


SCHEMA = _json(SCHEMA_PATH)
DEFS = cast(dict[str, dict[str, Any]], SCHEMA["$defs"])
FIXTURES = _json(FIXTURES_PATH)


def _matches(schema: dict[str, Any], value: Any) -> bool:
    """Validate the closed Draft 2020-12 subset used by this contract."""
    if "$ref" in schema:
        return _matches(DEFS[schema["$ref"].rsplit("/", 1)[1]], value)
    if "oneOf" in schema:
        return sum(_matches(option, value) for option in schema["oneOf"]) == 1
    if "allOf" in schema and not all(_matches(option, value) for option in schema["allOf"]):
        return False
    if "if" in schema:
        branch = schema.get("then") if _matches(schema["if"], value) else schema.get("else")
        if branch is not None and not _matches(branch, value):
            return False
    if "const" in schema and value != schema["const"]:
        return False
    if "enum" in schema and value not in schema["enum"]:
        return False

    expected = schema.get("type")
    if expected == "object" or any(
        keyword in schema for keyword in ("properties", "required", "additionalProperties")
    ):
        if not isinstance(value, dict):
            return False
        properties = schema.get("properties", {})
        if any(name not in value for name in schema.get("required", [])):
            return False
        if schema.get("additionalProperties") is False and set(value) - set(properties):
            return False
        return all(
            name not in value or _matches(properties[name], value[name]) for name in properties
        )
    if expected == "array":
        if not isinstance(value, list):
            return False
        if len(value) < schema.get("minItems", 0):
            return False
        if len(value) > schema.get("maxItems", len(value)):
            return False
        if schema.get("uniqueItems") and len(
            {json.dumps(item, sort_keys=True) for item in value}
        ) != len(value):
            return False
        return all(_matches(schema["items"], item) for item in value)
    if expected == "string":
        if not isinstance(value, str):
            return False
        if len(value) < schema.get("minLength", 0):
            return False
        if len(value) > schema.get("maxLength", len(value)):
            return False
        return "pattern" not in schema or re.match(schema["pattern"], value) is not None
    if expected == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return False
        return value >= schema.get("minimum", value) and value <= schema.get("maximum", value)
    if expected == "boolean":
        return isinstance(value, bool)
    return True


def test_contract_identity_is_versioned_and_transport_is_closed() -> None:
    assert SCHEMA["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert SCHEMA["$id"] == (
        "https://aura.local/contracts/providers/research/v1/research-provider-transport.schema.json"
    )
    assert SCHEMA["oneOf"] == [
        {"$ref": "#/$defs/ProviderRequestEnvelope"},
        {"$ref": "#/$defs/ProviderResultEnvelope"},
        {"$ref": "#/$defs/ProviderErrorEnvelope"},
    ]
    for name, definition in DEFS.items():
        if definition.get("type") == "object":
            assert definition.get("additionalProperties") is False, name


def test_signed_request_envelope_has_expiry_replay_audience_and_method_binding() -> None:
    request = DEFS["ProviderRequestEnvelope"]
    assert set(request["required"]) == {
        "schemaVersion",
        "envelopeType",
        "requestId",
        "replayId",
        "audience",
        "method",
        "issuedAt",
        "expiresAt",
        "ttlMs",
        "payload",
        "signature",
    }
    assert request["properties"]["schemaVersion"]["const"] == "research-provider.v1"
    assert request["properties"]["ttlMs"]["maximum"] == 60000
    signature = DEFS["Signature"]
    assert signature["properties"]["algorithm"]["const"] == "Ed25519"
    assert signature["properties"]["value"]["maxLength"] == 86
    descriptions = f"{SCHEMA['description']} {request['description']}"
    for phrase in ("never authorize a destination", "replay-id uniqueness"):
        assert phrase in descriptions


def test_provider_limits_cannot_exceed_adr_0018_caps() -> None:
    expected = {
        "SearchProviderLimits": {
            "deadlineMs": 15000,
            "maxCandidates": 20,
            "maxConcurrency": 8,
            "cpuMillicores": 2000,
            "memoryBytes": 1073741824,
            "pids": 128,
            "temporaryStorageBytes": 134217728,
        },
        "StaticProviderLimits": {
            "deadlineMs": 20000,
            "maxRedirects": 5,
            "maxTransferredBytes": 8388608,
            "maxDecompressedBytes": 8388608,
            "maxExtractedUtf8Bytes": 262144,
            "maxConcurrency": 4,
            "cpuMillicores": 2000,
            "memoryBytes": 1073741824,
            "pids": 64,
            "temporaryStorageBytes": 67108864,
        },
        "BrowserProviderLimits": {
            "deadlineMs": 45000,
            "maxRedirects": 5,
            "maxResponseBytes": 8388608,
            "maxExtractedUtf8Bytes": 262144,
            "maxConcurrency": 2,
            "cpuMillicores": 4000,
            "memoryBytes": 2147483648,
            "pids": 256,
            "temporaryStorageBytes": 268435456,
        },
    }
    for definition, maximums in expected.items():
        properties = DEFS[definition]["properties"]
        assert {name: properties[name]["maximum"] for name in maximums} == maximums


def test_search_fetch_cancel_limits_health_results_and_errors_are_typed() -> None:
    required = {
        "SearchRequest",
        "StaticFetchRequest",
        "BrowserFetchRequest",
        "CancelRequest",
        "LimitsRequest",
        "HealthRequest",
        "SearchResult",
        "FetchSuccessResult",
        "FetchInadequateResult",
        "CancelResult",
        "LimitsResult",
        "HealthResult",
        "ProviderError",
    }
    assert required <= set(DEFS)
    assert DEFS["SearchResult"]["properties"]["candidates"]["maxItems"] == 20
    evidence = DEFS["FetchedEvidence"]["properties"]
    assert evidence["redirectCount"]["maximum"] == 5
    assert evidence["transferredBytes"]["maximum"] == 8388608
    assert evidence["extractedUtf8Bytes"]["maximum"] == 262144
    assert evidence["extractedText"]["maxLength"] == 262144
    assert _matches(DEFS["PublicHttpsUrl"], "https://example.org:443/research")
    assert not _matches(DEFS["PublicHttpsUrl"], "https://user@example.org/research")
    assert not _matches(DEFS["PublicHttpsUrl"], "https://example.org:8443/research")


def test_errors_health_and_limits_cannot_carry_private_or_arbitrary_diagnostics() -> None:
    prohibited = {
        "message",
        "detail",
        "diagnostic",
        "exception",
        "query",
        "url",
        "domain",
        "content",
        "excerpt",
        "browserState",
        "signature",
    }
    for name in ("ProviderError", "HealthResult", "LimitsResult"):
        definition = DEFS[name]
        assert definition["additionalProperties"] is False
        assert not prohibited & set(definition["properties"])
    codes = DEFS["ProviderError"]["properties"]["code"]["enum"]
    assert "signature_invalid" in codes
    assert "expired" in codes
    assert "replayed" in codes
    assert "destination_blocked" in codes


def test_shared_conformance_fixtures_have_schema_and_semantic_rejections() -> None:
    assert FIXTURES["fixtureVersion"] == "research-provider-conformance.v1"
    assert FIXTURES["schemaId"] == SCHEMA["$id"]
    cases = cast(list[dict[str, Any]], FIXTURES["cases"])
    assert len({case["caseId"] for case in cases}) == len(cases)
    assert {case["expectation"] for case in cases} == {
        "accept",
        "schema_reject",
        "semantic_reject",
    }
    for case in cases:
        schema_valid = _matches(DEFS[case["definition"]], case["document"])
        if case["expectation"] == "schema_reject":
            assert not schema_valid, case["caseId"]
            assert case["rejectionCode"] == "contract_invalid"
        else:
            assert schema_valid, case["caseId"]
        if case["expectation"] == "semantic_reject":
            assert case["precondition"] in {
                "clock_after_expires_at",
                "replay_id_already_seen",
                "signature_verification_fails",
            }
            assert case["rejectionCode"] in {
                "expired",
                "replayed",
                "signature_invalid",
            }


def test_contract_has_no_business_authority_or_generated_client_surface() -> None:
    request_fields = set(DEFS["ProviderRequestEnvelope"]["properties"])
    payload_fields = set().union(
        *(
            set(DEFS[name]["properties"])
            for name in (
                "SearchRequest",
                "StaticFetchRequest",
                "BrowserFetchRequest",
                "CancelRequest",
                "LimitsRequest",
                "HealthRequest",
            )
        )
    )
    prohibited = {
        "ownerId",
        "conversationId",
        "taskId",
        "runId",
        "artifactId",
        "permissions",
        "grants",
        "policy",
        "instructions",
        "credentials",
        "objectKey",
    }
    assert not prohibited & (request_fields | payload_fields)
    generated_client = (
        ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/research-provider.ts"
    )
    assert not generated_client.exists()
