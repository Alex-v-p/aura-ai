"""HTTP regressions for the AURA-0020 review findings."""

from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from conftest import owner_client


def headers(session: object, *, key: str | None = None) -> dict[str, str]:
    return {
        "X-CSRF-Token": session.csrf_token,  # type: ignore[attr-defined]
        "Idempotency-Key": key or str(uuid4()),
    }


@pytest.mark.asyncio
async def test_create_conversation_with_unavailable_agent_fails_closed_with_409(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent = (await client.get("/api/v1/agents")).json()["items"][0]
        disabled = await client.patch(
            f"/api/v1/agents/{agent['id']}",
            headers=headers(session),
            json={"status": "disabled", "expectedVersion": agent["version"]},
        )
        assert disabled.status_code == 200
        response = await client.post(
            "/api/v1/conversations",
            headers=headers(session),
            json={
                "message": "must not start",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert response.status_code == 409
        assert "must not start" not in response.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_mutation_rejects_non_uuid_idempotency_header(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        persona = (await client.get("/api/v1/personas")).json()["items"][0]
        response = await client.patch(
            f"/api/v1/personas/{persona['id']}",
            headers=headers(session, key="not-a-uuid"),
            json={"status": "active", "expectedVersion": persona["version"]},
        )
        assert response.status_code == 422
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_configuration_failure_does_not_write_a_success_audit(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        before = len(api_app.state.aura.store.auth_audit)
        response = await client.post(
            "/api/v1/agents",
            headers=headers(session),
            json={
                "displayName": "Invalid",
                "purpose": "Should fail",
                "instructions": "Should not persist",
                "personaRevisionId": str(uuid4()),
            },
        )
        assert response.status_code in {404, 422}
        assert len(api_app.state.aura.store.auth_audit) == before
        assert all(
            item["action"] != "agent.create"
            for item in api_app.state.aura.store.auth_audit[before:]
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_configuration_telemetry_accepts_success_and_failure_without_content(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        persona = (await client.get("/api/v1/personas")).json()["items"][0]
        before = api_app.state.aura.metrics.stats()
        success = await client.post(
            "/api/v1/agents",
            headers=headers(session),
            json={
                "displayName": "Telemetry Agent",
                "purpose": "Keep metadata only.",
                "instructions": "Do not export this instruction text.",
                "personaRevisionId": persona["currentRevision"]["id"],
            },
        )
        assert success.status_code == 201
        failure = await client.post(
            "/api/v1/agents",
            headers=headers(session),
            json={
                "displayName": "Rejected Agent",
                "purpose": "Private failure purpose.",
                "instructions": "Private failure instructions.",
                "personaRevisionId": str(uuid4()),
            },
        )
        assert failure.status_code in {404, 422}
        records = [
            item
            for item in api_app.state.aura.metrics.snapshot()
            if item.component_id == "aura.interaction.agent_configuration"
        ]
        assert {item.metric for item in records} >= {
            "operation_duration_ms",
            "configuration_outcome",
        }
        assert all(item.value >= 0 for item in records)
        assert any(dict(item.dimensions).get("outcome") == "ok" for item in records)
        assert any(dict(item.dimensions).get("outcome") == "error" for item in records)
        assert api_app.state.aura.metrics.stats().rejected == before.rejected
        rendered = repr(records)
        assert "Private failure instructions." not in rendered
        assert "Do not export this instruction text." not in rendered
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_prompt_telemetry_records_numeric_provenance_without_prompt_content(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers=headers(session),
            json={"message": "telemetry", "modelId": "chat"},
        )
        assert created.status_code == 202
        run_id = created.json()["run"]["id"]
        await api_app.state.aura.coordinator.execute(
            UUID(run_id), api_app.state.aura.provider
        )
        records = [
            item
            for item in api_app.state.aura.metrics.snapshot()
            if item.component_id == "aura.runtime.prompt_compilation"
        ]
        prompt_span = next(item for item in records if item.metric == "prompt_compile_duration_ms")
        attrs = dict(prompt_span.trace_attributes)
        assert prompt_span.value >= 0
        assert int(attrs["prompt_component_count"]) >= 1
        assert int(attrs["compiled_prompt_size"]) >= 0
        assert len(attrs["prompt_hash"]) == 64
        assert all(
            forbidden not in repr(records)
            for forbidden in ("instructions", "rendered prompt", "telemetry")
        )
    finally:
        await client.aclose()
