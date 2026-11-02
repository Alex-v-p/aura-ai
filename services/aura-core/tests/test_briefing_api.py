"""Capability discovery tests for the owner-authenticated briefing API."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import Principal, Settings
from fastapi import FastAPI

REPOSITORY = Path(__file__).parents[3]
OPENAPI_PATH = REPOSITORY / "contracts/openapi/aura-v1.yaml"
ISSUER = "https://authentik.dev.example"
OWNER = "briefing-owner"
ORIGIN = "https://aura.dev.example"


@pytest.fixture
def briefing_app() -> FastAPI:
    return create_app(
        Settings(
            public_origin=ORIGIN,
            oidc_issuer=ISSUER,
            oidc_client_id="aura-web",
            oidc_audience="aura-web",
            oidc_redirect_uri=f"{ORIGIN}/api/v1/auth/callback",
            owner_subject=OWNER,
            default_model="chat",
            secure_cookies=False,
        ),
        testing=True,
    )


async def _client(app: FastAPI) -> tuple[httpx.AsyncClient, str]:
    session_id, session = await app.state.aura.sessions.create(
        Principal(ISSUER, OWNER, "Test owner")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, session_id)
    return client, session.csrf_token


def _openapi() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_capability_contract_is_closed_and_has_no_private_runtime_state() -> None:
    spec = _openapi()
    operation = spec["paths"]["/api/v1/briefing-capabilities"]["get"]
    assert operation["operationId"] == "getBriefingCapabilities"
    cache_control = operation["responses"]["200"]["headers"]["Cache-Control"]
    assert cache_control["schema"]["const"] == "no-store"
    assert {"401", "403"} <= set(operation["responses"])

    schema = spec["components"]["schemas"]["BriefingCapabilities"]
    assert schema["additionalProperties"] is False
    assert schema["required"] == [
        "executionPolicyRevisionId",
        "executionPolicyRevision",
        "deadlineSeconds",
        "maxOutputTokens",
        "maxOutputChars",
        "cadenceKind",
        "memoryEnabled",
        "toolsEnabled",
    ]
    assert schema["properties"]["executionPolicyRevisionId"]["const"] == (
        "e80e8c61-575a-5a8c-862e-0c7cc91baa61"
    )
    assert schema["properties"]["executionPolicyRevision"]["const"] == "briefing-execution-v1"
    assert schema["properties"]["deadlineSeconds"]["const"] == 300
    assert schema["properties"]["maxOutputTokens"]["const"] == 16_384
    assert schema["properties"]["maxOutputChars"]["const"] == 32_768
    assert schema["properties"]["cadenceKind"]["const"] == "daily"
    assert schema["properties"]["memoryEnabled"]["const"] is False
    assert schema["properties"]["toolsEnabled"]["const"] is False
    forbidden = {
        "provider",
        "model",
        "modelId",
        "prompt",
        "tool",
        "grant",
        "owner",
        "source",
        "runtime",
    }
    assert not forbidden & set(schema["properties"])


@pytest.mark.anyio
async def test_capability_service_is_derived_from_the_canonical_fixed_policy(
    briefing_app: FastAPI,
) -> None:
    capabilities = await briefing_app.state.aura.briefing_service.get_capabilities()
    assert capabilities.as_payload() == {
        "executionPolicyRevisionId": "e80e8c61-575a-5a8c-862e-0c7cc91baa61",
        "executionPolicyRevision": "briefing-execution-v1",
        "deadlineSeconds": 300,
        "maxOutputTokens": 16_384,
        "maxOutputChars": 32_768,
        "cadenceKind": "daily",
        "memoryEnabled": False,
        "toolsEnabled": False,
    }


@pytest.mark.anyio
async def test_capability_endpoint_requires_owner_session_and_is_no_store(
    briefing_app: FastAPI,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=briefing_app),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    ) as anonymous:
        response = await anonymous.get("/api/v1/briefing-capabilities")
    assert response.status_code == 401

    client, _csrf = await _client(briefing_app)
    async with client:
        response = await client.get("/api/v1/briefing-capabilities")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.json() == {
        "executionPolicyRevisionId": "e80e8c61-575a-5a8c-862e-0c7cc91baa61",
        "executionPolicyRevision": "briefing-execution-v1",
        "deadlineSeconds": 300,
        "maxOutputTokens": 16_384,
        "maxOutputChars": 32_768,
        "cadenceKind": "daily",
        "memoryEnabled": False,
        "toolsEnabled": False,
    }
    assert not {
        "provider",
        "model",
        "modelId",
        "prompt",
        "toolGrants",
        "owner",
        "sourceGrants",
        "runtime",
    } & set(response.json())
