"""HTTP contract and lifecycle coverage for versioned agent configuration."""

from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from conftest import owner_client


def command_headers(csrf: str, key: str | None = None) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key or str(uuid4()),
    }


def assert_agent_reference(reference: dict[str, object]) -> None:
    assert set(reference) == {
        "profileId",
        "revisionId",
        "displayName",
        "revision",
        "status",
        "newerRevisionAvailable",
    }


def assert_assignment(assignment: dict[str, object], reason: str) -> None:
    assert set(assignment) == {
        "id",
        "agent",
        "reason",
        "effectiveAfterMessageId",
        "createdAt",
    }
    assert assignment["reason"] == reason
    agent = assignment["agent"]
    assert isinstance(agent, dict)
    assert_agent_reference(cast(dict[str, object], agent))


@pytest.mark.asyncio
async def test_owner_only_agent_persona_mutations_require_csrf_and_replay_safely(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        assert (await client.get("/api/v1/agents")).status_code == 200
        assert (await client.get("/api/v1/personas")).status_code == 200

        missing_csrf = await client.post(
            "/api/v1/personas",
            headers={"Idempotency-Key": str(uuid4())},
            json={
                "displayName": "Clear",
                "description": "Clear",
                "instructions": "Be clear.",
            },
        )
        assert missing_csrf.status_code == 403

        key = str(uuid4())
        payload = {
            "displayName": "Researcher",
            "purpose": "Research carefully.",
            "instructions": "Cite sources.",
            "personaRevisionId": (
                await client.get("/api/v1/personas")
            ).json()["items"][0]["currentRevision"]["id"],
        }
        created = await client.post(
            "/api/v1/agents", headers=command_headers(session.csrf_token, key), json=payload
        )
        assert created.status_code == 201
        audit = api_app.state.aura.store.auth_audit[-1]
        assert audit["action"] == "agent.create"
        assert "Research carefully." not in str(audit)
        assert "Cite sources." not in str(audit)
        replay = await client.post(
            "/api/v1/agents", headers=command_headers(session.csrf_token, key), json=payload
        )
        assert replay.status_code == 201
        assert replay.json() == created.json()
        conflicting_replay = await client.post(
            "/api/v1/agents",
            headers=command_headers(session.csrf_token, key),
            json={**payload, "purpose": "different"},
        )
        assert conflicting_replay.status_code == 409

        other, _ = await owner_client(api_app, subject="not-the-owner")
        try:
            assert (await other.get("/api/v1/agents")).status_code == 403
        finally:
            await other.aclose()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_revision_history_uses_optimistic_versions_and_persona_non_cascade(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        persona = (await client.get("/api/v1/personas")).json()["items"][0]
        agent = (await client.get("/api/v1/agents")).json()["items"][0]
        agent_detail = await client.get(f"/api/v1/agents/{agent['id']}")
        original_agent_revision = agent_detail.json()["revisions"][0]["id"]
        revised_persona = await client.post(
            f"/api/v1/personas/{persona['id']}/revisions",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": "Neutral",
                "description": "Terse",
                "instructions": "Use concise sentences.",
                "expectedVersion": persona["version"],
            },
        )
        assert revised_persona.status_code == 201
        revised_persona_id = revised_persona.json()["currentRevision"]["id"]
        refreshed = (await client.get(f"/api/v1/agents/{agent['id']}")).json()
        assert refreshed["revisions"][0]["id"] == original_agent_revision
        assert refreshed["revisions"][0]["personaRevisionId"] != revised_persona_id

        revised = await client.post(
            f"/api/v1/agents/{agent['id']}/revisions",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": "Aura",
                "purpose": "Updated",
                "instructions": "Use the new style.",
                "personaRevisionId": revised_persona_id,
                "expectedVersion": agent_detail.json()["version"],
            },
        )
        assert revised.status_code == 201
        stale = await client.post(
            f"/api/v1/agents/{agent['id']}/revisions",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": "Aura",
                "purpose": "Updated",
                "instructions": "Use the new style.",
                "personaRevisionId": revised_persona_id,
                "expectedVersion": agent_detail.json()["version"],
            },
        )
        assert stale.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_conversation_assignment_requires_confirmation_and_preserves_run_provenance(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agents = (await client.get("/api/v1/agents")).json()["items"]
        agent = agents[0]
        agent_detail = (await client.get(f"/api/v1/agents/{agent['id']}" )).json()
        original_revision = agent_detail["currentRevision"]
        persona_revision_id = (
            await client.get("/api/v1/personas")
        ).json()["items"][0]["currentRevision"]["id"]
        revised_agent = await client.post(
            f"/api/v1/agents/{agent['id']}/revisions",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": "Aura",
                "purpose": "A more focused Aura.",
                "instructions": "Be precise.",
                "personaRevisionId": persona_revision_id,
                "expectedVersion": agent_detail["version"],
            },
        )
        assert revised_agent.status_code == 201
        newer_revision = revised_agent.json()["currentRevision"]
        create_headers = command_headers(session.csrf_token)
        created = await client.post(
            "/api/v1/conversations",
            headers=create_headers,
            json={
                "message": "first",
                "modelId": "chat",
                "agentRevisionId": original_revision["id"],
            },
        )
        assert created.status_code == 202
        body = created.json()
        conversation = body["conversation"]
        run = body["run"]
        assert conversation["agentRevisionId"] == original_revision["id"]
        assert_agent_reference(conversation["agent"])
        assert conversation["agent"]["newerRevisionAvailable"] is True
        assert_assignment(conversation["agentAssignments"][0], "initial")
        assert run["agentRevisionId"] == conversation["agentRevisionId"]

        no_confirmation = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": newer_revision["id"],
                "version": conversation["version"],
            },
        )
        assert no_confirmation.status_code == 409
        active_conflict = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": newer_revision["id"],
                "version": conversation["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert active_conflict.status_code == 409

        await api_app.state.aura.coordinator.execute(UUID(run["id"]), api_app.state.aura.provider)
        detail = (await client.get(f"/api/v1/conversations/{conversation['id']}")).json()
        assert detail["recentRuns"][0]["status"] == "completed"
        assert {"personaRevisionId", "promptBundleRevisionId", "promptHash"} <= detail[
            "recentRuns"
        ][0].keys()

        switched = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": newer_revision["id"],
                "version": detail["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert switched.status_code == 200
        assert_agent_reference(switched.json()["agent"])
        assert_assignment(switched.json()["agentAssignments"][-1], "revision_upgrade")
        assert switched.json()["agentAssignments"][-1]["effectiveAfterMessageId"]

        cross_profile = await client.post(
            "/api/v1/agents",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": "Researcher",
                "purpose": "Research carefully.",
                "instructions": "Cite sources.",
                "personaRevisionId": persona_revision_id,
            },
        )
        assert cross_profile.status_code == 201
        cross_revision = cross_profile.json()["currentRevision"]
        switched_again = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": cross_revision["id"],
                "version": switched.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert switched_again.status_code == 200
        assert_assignment(switched_again.json()["agentAssignments"][-1], "manual_switch")
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_disabled_agent_blocks_new_runs_but_historical_transcript_remains_readable(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent = (await client.get("/api/v1/agents")).json()["items"][0]
        agent_revision_id = agent["currentRevision"]["id"]
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "history",
                "modelId": "chat",
                "agentRevisionId": agent_revision_id,
            },
        )
        conversation = created.json()["conversation"]
        await api_app.state.aura.coordinator.execute(
            UUID(created.json()["run"]["id"]), api_app.state.aura.provider
        )
        disabled = await client.patch(
            f"/api/v1/agents/{agent['id']}",
            headers=command_headers(session.csrf_token),
            json={"status": "disabled", "expectedVersion": agent["version"]},
        )
        assert disabled.status_code == 200
        detail = await client.get(f"/api/v1/conversations/{conversation['id']}")
        assert detail.status_code == 200
        retry = await client.post(
            f"/api/v1/runs/{created.json()['run']['id']}/retry",
            headers=command_headers(session.csrf_token),
        )
        assert retry.status_code == 409
        blocked = await client.post(
            f"/api/v1/conversations/{conversation['id']}/runs",
            headers=command_headers(session.csrf_token),
            json={"message": "should be blocked", "conversationVersion": conversation["version"]},
        )
        assert blocked.status_code == 409
    finally:
        await client.aclose()
