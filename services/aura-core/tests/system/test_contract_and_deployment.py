"""Static contract and deployment boundary checks without network dependencies."""

import json
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from aura_core.platform.outbox.service import make_run_command

ROOT = Path(__file__).parents[4]


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
    assert "networks: [aura-identity]" in identity_override
    assert "aura-valkey-password" in compose
    assert "authentik-valkey-password" in identity_override
