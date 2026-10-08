"""Static contract and deployment boundary checks without network dependencies."""

import json
import re
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
from aura_core.platform.auth import Settings
from aura_core.platform.outbox.service import make_run_command

ROOT = Path(__file__).parents[4]


def test_ollama_discovery_settings_have_independent_safe_bounds() -> None:
    settings = Settings()

    assert settings.ollama_inventory_timeout_seconds == 5.0
    assert settings.ollama_verification_timeout_seconds == 60.0
    assert settings.ollama_inventory_cache_ttl_seconds == 15.0

    for field, value in (
        ("ollama_inventory_timeout_seconds", 0),
        ("ollama_inventory_timeout_seconds", 61),
        ("ollama_verification_timeout_seconds", 0),
        ("ollama_verification_timeout_seconds", 301),
        ("ollama_inventory_cache_ttl_seconds", -1),
        ("ollama_inventory_cache_ttl_seconds", 301),
    ):
        with pytest.raises(ValueError):
            Settings(**cast(Any, {field: value}))


def test_openapi_exposes_only_same_origin_contract_and_required_commands() -> None:
    contract = cast(
        dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text())
    )
    paths = contract["paths"]
    expected = {
        "/api/v1/auth/session",
        "/api/v1/auth/login",
        "/api/v1/auth/callback",
        "/api/v1/auth/logout",
        "/api/v1/models",
        "/api/v1/conversations",
        "/api/v1/conversations/{conversation_id}",
        "/api/v1/conversations/{conversation_id}/runs",
        "/api/v1/runs/{run_id}/cancel",
        "/api/v1/runs/{run_id}/retry",
        "/api/v1/runs/{run_id}/events",
        "/api/v1/runs/{run_id}/memory-activity",
        "/health/live",
        "/health/ready",
    }
    assert expected <= paths.keys()
    assert all("ollama" not in path.lower() for path in paths)
    assert contract["servers"] == [{"url": "/", "description": "Same-origin Aura BFF"}]
    assert (
        contract["components"]["securitySchemes"]["sessionCookie"]["name"] == "__Host-aura_session"
    )
    for path, operation in (
        ("/api/v1/conversations", "post"),
        ("/api/v1/conversations/{conversation_id}/runs", "post"),
        ("/api/v1/runs/{run_id}/cancel", "post"),
        ("/api/v1/runs/{run_id}/retry", "post"),
    ):
        params = cast(list[dict[str, Any]], paths[path][operation]["parameters"])
        refs = {cast(str, parameter["$ref"]) for parameter in params}
        assert "#/components/parameters/CsrfToken" in refs
        assert "#/components/parameters/IdempotencyKey" in refs


def test_memory_contract_is_owner_scoped_versioned_and_content_safe() -> None:
    contract = cast(
        dict[str, Any], json.loads((ROOT / "contracts/openapi/aura-v1.yaml").read_text())
    )
    assert contract["info"]["version"] == "1.5.0"
    paths = cast(dict[str, dict[str, Any]], contract["paths"])
    mutation_operations = {
        ("/api/v1/memories", "post"),
        ("/api/v1/memories/{memory_id}/revisions", "post"),
        ("/api/v1/memories/{memory_id}/status", "patch"),
        ("/api/v1/memories/{memory_id}/pin", "patch"),
        ("/api/v1/memories/{memory_id}/purge", "post"),
    }
    assert {
        "/api/v1/memories",
        "/api/v1/memories/{memory_id}",
        *(path for path, _ in mutation_operations),
    } <= paths.keys()
    for path, method in mutation_operations:
        parameters = cast(list[dict[str, Any]], paths[path][method]["parameters"])
        refs = {cast(str, parameter["$ref"]) for parameter in parameters}
        assert "#/components/parameters/CsrfToken" in refs
        assert "#/components/parameters/IdempotencyKey" in refs
        if path != "/api/v1/memories":
            assert "#/components/parameters/MemoryReadScopeType" in refs
            assert "#/components/parameters/MemoryReadAgentProfileId" in refs

    list_parameters = cast(list[dict[str, Any]], paths["/api/v1/memories"]["get"]["parameters"])
    list_refs = {
        cast(str, parameter["$ref"]) for parameter in list_parameters if "$ref" in parameter
    }
    detail_parameters = cast(
        list[dict[str, Any]], paths["/api/v1/memories/{memory_id}"]["get"]["parameters"]
    )
    detail_refs = {cast(str, parameter["$ref"]) for parameter in detail_parameters}
    assert "#/components/parameters/MemoryCollectionScopeType" in list_refs
    assert "#/components/parameters/MemoryCollectionAgentProfileId" in list_refs
    required_read_scope_refs = {
        "#/components/parameters/MemoryReadScopeType",
        "#/components/parameters/MemoryReadAgentProfileId",
    }
    assert required_read_scope_refs <= detail_refs
    assert all(parameter.get("name") != "q" for parameter in list_parameters)
    schemas = cast(dict[str, dict[str, Any]], contract["components"]["schemas"])
    parameters = cast(dict[str, dict[str, Any]], contract["components"]["parameters"])
    collection_scope = parameters["MemoryCollectionScopeType"]
    assert collection_scope["schema"]["default"] == "user"
    assert "all returns user and" in collection_scope["description"]
    assert "every owner-authorized agent scope" in collection_scope["description"]
    assert "rejects agentProfileId" in collection_scope["description"]
    read_scope = parameters["MemoryReadScopeType"]
    assert read_scope["schema"]["allOf"][0]["$ref"].endswith("/MemoryScopeType")
    assert "not permitted for detail or mutations" in read_scope["description"]
    assert "Rejected for user scope" in parameters["MemoryReadAgentProfileId"]["description"]
    detail_description = paths["/api/v1/memories/{memory_id}"]["get"]["responses"]["200"][
        "description"
    ]
    assert "scopeType=agent" in detail_description
    assert "agentProfileId" in detail_description
    list_description = paths["/api/v1/memories"]["get"]["responses"]["200"]["description"]
    assert "explicit all" in list_description
    for path, method in mutation_operations:
        if path == "/api/v1/memories":
            continue
        mutation_refs = {
            cast(str, parameter["$ref"])
            for parameter in paths[path][method]["parameters"]
            if "$ref" in parameter
        }
        assert "#/components/parameters/MemoryReadScopeType" in mutation_refs
    search = paths["/api/v1/memories/search"]["post"]
    search_body = search["requestBody"]["content"]["application/json"]["schema"]
    assert search_body["$ref"].endswith("SearchMemoriesRequest")
    search_parameters = search.get("parameters", [])
    assert any(parameter.get("$ref", "").endswith("/CsrfToken") for parameter in search_parameters)
    assert schemas["SearchMemoriesRequest"]["properties"]["query"]["maxLength"] == 500
    search_scope = schemas["SearchMemoriesRequest"]["properties"]["scopeType"]
    assert search_scope["allOf"][0]["$ref"].endswith("/MemoryCollectionScopeType")
    assert search_scope["default"] == "user"
    assert "all combines user and every owner-authorized agent scope" in search_scope[
        "description"
    ]
    generated_client = (
        ROOT / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"
    ).read_text()
    assert "searchMemories:" in generated_client
    assert "SearchMemoriesRequest" in generated_client

    assert schemas["MemoryScopeType"]["enum"] == ["user", "agent"]
    assert schemas["MemoryCollectionScopeType"]["enum"] == ["user", "agent", "all"]
    assert schemas["MemoryKind"]["enum"] == [
        "episodic",
        "semantic",
        "procedural",
        "preference",
        "system",
    ]
    assert schemas["MemoryLifecycleStatus"]["enum"] == [
        "active",
        "dormant",
        "archived",
        "disabled",
        "disputed",
        "superseded",
    ]
    half_life = schemas["MemoryRevision"]["properties"]["halfLifeDays"]
    assert (half_life["minimum"], half_life["maximum"]) == (0.25, 3650)
    assert "correctionReason" in schemas["MemoryRevision"]["required"]
    assert "evidence" in schemas["MemoryProvenance"]["required"]
    assert "embeddingGenerations" in schemas["MemoryDetail"]["required"]
    assert schemas["MemoryEmbeddingGeneration"]["properties"]["status"]["$ref"].endswith(
        "/MemoryEmbeddingGenerationStatus"
    )
    assert schemas["PurgeMemoryRequest"]["properties"]["confirmation"]["const"] == (
        "PURGE MEMORY"
    )
    assert "vector" not in schemas["MemoryEmbedding"]["properties"]
    assert {"generationId", "generation", "modelRevision", "digest"} <= set(
        schemas["MemoryEmbedding"]["required"]
    )


def test_run_event_contract_keeps_typed_replay_and_delta_shapes() -> None:
    schema = cast(
        dict[str, Any],
        json.loads((ROOT / "contracts/events/runs/v1/run-events.schema.json").read_text()),
    )
    assert schema["$schema"].endswith("draft/2020-12/schema")
    definitions = cast(dict[str, dict[str, Any]], schema["$defs"])
    event_types: set[str] = {
        cast(str, definition["allOf"][1]["properties"]["eventType"]["const"])
        for definition in definitions.values()
        if "allOf" in definition
    }
    assert event_types == {
        "run.snapshot",
        "run.status",
        "assistant.delta",
        "assistant.snapshot",
        "run.error",
        "memory.activity",
        "heartbeat",
    }
    assert schema["$defs"]["AssistantDeltaEvent"]["allOf"][1]["properties"]["data"]["required"] == [
        "messageId",
        "offset",
        "text",
    ]
    assert schema["$defs"]["RunSnapshotEvent"]["allOf"][1]["properties"]["data"]["required"] == [
        "run",
        "assistantMessage",
    ]
    memory_activity = definitions["MemoryActivity"]
    assert memory_activity["additionalProperties"] is False
    assert set(memory_activity["properties"]) == {
        "id",
        "action",
        "status",
        "scope",
        "candidateId",
        "memoryId",
        "memoryRevisionId",
        "policyRevisionId",
        "embeddingGenerationId",
        "reconciliationStatus",
        "occurredAt",
    }
    assert not {
        "content",
        "evidence",
        "prompt",
        "response",
        "vector",
        "credential",
        "ownerSubject",
    } & set(memory_activity["properties"])
    assert (
        definitions["MemoryActivityEvent"]["allOf"][1]["properties"]["eventType"]["const"]
        == "memory.activity"
    )


def test_outbox_wire_command_contains_identifiers_but_no_conversation_content() -> None:
    payload = json.loads(make_run_command(uuid4(), uuid4()).wire_payload())
    assert set(payload) == {
        "schemaVersion",
        "commandId",
        "runId",
        "conversationId",
        "createdAt",
        "correlationId",
        "causationId",
    }
    assert "content" not in payload and "prompt" not in payload and "message" not in payload


def test_local_identity_is_optional_and_core_has_no_container_dependency() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    identity_override = (ROOT / "deploy/compose/local-identity.yaml").read_text()
    assert "authentik" not in compose.lower()
    assert "AUTHENTIK" not in compose
    assert "profiles: [local-identity]" in identity_override
    core_block = compose.split("  aura-core-api:", 1)[1].split("  aura-core-worker:", 1)[0]
    assert "authentik-server" not in core_block.lower()
    assert "authentik-postgres" not in core_block.lower()
    assert "authentik-worker" not in core_block.lower()
    assert "AURA_OWNER_SUBJECT: ${AURA_OWNER_SUBJECT:?" in core_block
    blueprint = (ROOT / "deploy/authentik/blueprints/aura-oidc.yaml").read_text()
    assert "temporary-local-identity" in blueprint
    assert "AURA_AUTHENTIK_REDIRECT_URI" in blueprint


def test_platform_requires_encrypted_state_and_isolates_identity_cache() -> None:
    compose = (ROOT / "compose.yaml").read_text()
    identity_override = (ROOT / "deploy/compose/local-identity.yaml").read_text()
    assert "${AURA_STATE_ROOT:?encrypted state root is required}" in compose
    assert (
        "${AUTHENTIK_IMAGE:-ghcr.io/goauthentik/server@sha256:"
        "76bf433fd434c067cb25dc3e197cee793998441cda912441ea275293e76cc32c}"
    ) in identity_override
    assert "AUTHENTIK_REDIS__HOST: authentik-valkey" in identity_override
    assert "AUTHENTIK_HOST_BROWSER: ${AUTHENTIK_HOST_BROWSER:?" in identity_override
    assert "AURA_OIDC_ISSUER: ${AURA_OIDC_ISSUER:?" in identity_override
    assert "networks: [aura-identity, aura-identity-db, aura-identity-cache]" in identity_override
    assert "networks: [aura-core-db, aura-core-bus, aura-core-cache, aura-core-egress]" in compose
    assert "host.docker.internal:host-gateway" in compose
    identity_core_block = identity_override.split("  aura-core-api:", 1)[1].split(
        "  authentik-postgres:", 1
    )[0]
    identity_core_networks = next(
        line.strip()
        for line in identity_core_block.splitlines()
        if line.strip().startswith("networks:")
    )
    network_match = re.fullmatch(r"networks:\s*\[([^\]]*)\]", identity_core_networks)
    assert network_match is not None
    assert {network.strip() for network in network_match.group(1).split(",")} == {
        "aura-frontend",
        "aura-core-db",
        "aura-core-bus",
        "aura-core-cache",
        "aura-core-egress",
        "aura-identity",
    }
    assert "aura-valkey-password" in compose
    assert "authentik-valkey-password" in identity_override
