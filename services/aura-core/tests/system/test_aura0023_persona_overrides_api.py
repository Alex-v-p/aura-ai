"""HTTP regressions for AURA-0023 conversation persona overrides.

These tests intentionally use the authenticated API boundary.  They protect
the contract and lifecycle semantics without coupling the suite to the
ConversationStore implementation chosen by the Core owner.
"""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI

from conftest import owner_client


def command_headers(csrf: str, key: str | None = None) -> dict[str, str]:
    return {
        "X-CSRF-Token": csrf,
        "Idempotency-Key": key or str(uuid4()),
    }


def _spans(metrics: Any, component: str, operation: str) -> list[Any]:
    return [
        record
        for record in metrics.snapshot()
        if record.component_id == component
        and dict(record.trace_attributes).get("operation") == operation
    ]


def _persona_audits(api_app: FastAPI) -> list[dict[str, Any]]:
    return [
        cast(dict[str, Any], entry)
        for entry in api_app.state.aura.store.auth_audit
        if str(entry.get("action", "")).startswith("conversation.persona.")
    ]


def _assert_configuration_span(
    record: Any,
    *,
    conversation_id: str,
    outcome: str,
    source: str,
    reason: str,
    error_class: str | None = None,
) -> None:
    dimensions = dict(record.dimensions)
    attributes = dict(record.trace_attributes)
    assert record.metric == "operation_duration_ms"
    assert record.value >= 0
    assert dimensions["outcome"] == outcome
    assert attributes["configuration_source"] == source
    assert attributes["configuration_reason"] == reason
    assert attributes["conversation_id"] == conversation_id
    if error_class is None:
        assert "error_class" not in dimensions
    else:
        assert dimensions["error_class"] == error_class


def _assert_configuration_outcome_metric(
    metrics: Any, *, conversation_id: str, outcome: str
) -> None:
    records = [
        record
        for record in metrics.snapshot()
        if record.component_id == "aura.interaction.agent_configuration"
        and record.metric == "configuration_outcome"
        and dict(record.trace_attributes).get("conversation_id") == conversation_id
    ]
    assert records
    assert dict(records[-1].dimensions)["outcome"] == outcome


async def seeded_configuration(client: Any) -> tuple[dict[str, Any], dict[str, Any]]:
    agents = (await client.get("/api/v1/agents")).json()["items"]
    personas = (await client.get("/api/v1/personas")).json()["items"]
    assert agents and personas
    return cast(dict[str, Any], agents[0]), cast(dict[str, Any], personas[0])


async def create_persona_revision(
    client: Any, csrf: str, persona: dict[str, Any], *, instructions: str
) -> dict[str, Any]:
    response = await client.post(
        f"/api/v1/personas/{persona['id']}/revisions",
        headers=command_headers(csrf),
        json={
            "displayName": persona["currentRevision"]["displayName"],
            "description": "A test-only independent conversation style.",
            "instructions": instructions,
            "expectedVersion": persona["version"],
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json()["currentRevision"])


async def create_persona(
    client: Any,
    csrf: str,
    *,
    display_name: str,
    instructions: str,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/personas",
        headers=command_headers(csrf),
        json={
            "displayName": display_name,
            "description": "A test-only independent persona profile.",
            "instructions": instructions,
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


async def create_agent(
    client: Any,
    csrf: str,
    *,
    display_name: str,
    persona_revision_id: str,
) -> dict[str, Any]:
    response = await client.post(
        "/api/v1/agents",
        headers=command_headers(csrf),
        json={
            "displayName": display_name,
            "purpose": "A test-only alternate agent.",
            "instructions": "Use the selected default persona.",
            "personaRevisionId": persona_revision_id,
        },
    )
    assert response.status_code == 201, response.text
    return cast(dict[str, Any], response.json())


@pytest.mark.asyncio
async def test_conversation_can_pin_override_and_reset_to_agent_default(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate_persona = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="Use concise independent override instructions.",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Use an independent persona.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
                "personaRevisionId": alternate_persona["id"],
            },
        )
        assert created.status_code == 202, created.text
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        assert conversation["persona"]["revisionId"] == alternate_persona["id"]
        assert conversation["personaOverride"] is True
        assert conversation["personaAssignments"][0]["source"] == "conversation_override"
        assert conversation["personaAssignments"][0]["reason"] == "initial"
        assert body["run"]["personaRevisionId"] == alternate_persona["id"]

        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )
        current = await client.get(f"/api/v1/conversations/{conversation['id']}")
        assert current.status_code == 200
        reset = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "useAgentDefaultPersona": True,
                "version": current.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert reset.status_code == 200, reset.text
        reset_body = cast(dict[str, Any], reset.json())
        assert reset_body["persona"]["revisionId"] == agent["currentRevision"]["personaRevisionId"]
        assert reset_body["personaOverride"] is False
        assert reset_body["personaAssignments"][-1]["source"] == "agent_default"
        assert reset_body["personaAssignments"][-1]["reason"] == "reset_to_agent_default"
        assert reset_body["personaAssignments"][-1]["effectiveAfterMessageId"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_override_persists_across_agent_switch_and_default_follows_agent_without_override(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, _ = await seeded_configuration(client)
        alternate_profile = await create_persona(
            client,
            session.csrf_token,
            display_name="Focused test persona",
            instructions="Use the focused test persona.",
        )
        alternate_persona = alternate_profile["currentRevision"]
        alternate_agent = await create_agent(
            client,
            session.csrf_token,
            display_name="Independent test agent",
            persona_revision_id=alternate_persona["id"],
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Start with a manual persona.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
                "personaRevisionId": alternate_persona["id"],
            },
        )
        assert created.status_code == 202, created.text
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )

        current = await client.get(f"/api/v1/conversations/{conversation['id']}")
        assert current.status_code == 200
        switched = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": alternate_agent["currentRevision"]["id"],
                "version": current.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert switched.status_code == 200, switched.text
        switched_body = cast(dict[str, Any], switched.json())
        assert switched_body["persona"]["revisionId"] == alternate_persona["id"]
        assert switched_body["personaOverride"] is True
        assert switched_body["agentAssignments"][-1]["reason"] in {
            "manual_switch",
            "agent_revision_upgrade",
        }

        reset = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "useAgentDefaultPersona": True,
                "version": switched_body["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert reset.status_code == 200, reset.text
        reset_body = cast(dict[str, Any], reset.json())
        assert reset_body["persona"]["revisionId"] == alternate_persona["id"]
        assert reset_body["personaOverride"] is False
        assert reset_body["personaAssignments"][-1]["source"] == "agent_default"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persona_switch_requires_confirmation_and_rejects_active_run(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="An explicitly selected persona.",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Keep this run active.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert created.status_code == 202, created.text
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        patch = {
            "personaRevisionId": alternate["id"],
            "version": conversation["version"],
        }
        no_confirmation = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json=patch,
        )
        assert no_confirmation.status_code == 409
        active = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={**patch, "transcriptSharingConfirmed": True},
        )
        assert active.status_code == 409

        stale = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "version": conversation["version"] + 100,
                "transcriptSharingConfirmed": True,
            },
        )
        assert stale.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persona_override_audit_is_private_and_replay_or_rejection_is_not_duplicated(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="PRIVATE AUDIT PERSONA INSTRUCTIONS",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Private transcript text.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert created.status_code == 202, created.text
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )
        detail = await client.get(f"/api/v1/conversations/{conversation['id']}")
        key = str(uuid4())
        payload = {
            "personaRevisionId": alternate["id"],
            "version": detail.json()["version"],
            "transcriptSharingConfirmed": True,
        }
        before = len(_persona_audits(api_app))
        first = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token, key),
            json=payload,
        )
        assert first.status_code == 200, first.text
        audits_after_first = _persona_audits(api_app)
        assert len(audits_after_first) == before + 1
        audit = audits_after_first[-1]
        assert audit["action"] == "conversation.persona.override"
        metadata = cast(dict[str, Any], audit["metadata"])
        assert set(metadata) == {
            "conversationId",
            "personaProfileId",
            "personaRevisionId",
            "revision",
            "source",
            "reason",
        }
        assert metadata["personaRevisionId"] == alternate["id"]
        assert metadata["source"] == "conversation_override"
        assert metadata["reason"] == "manual_override"
        for secret in (
            "PRIVATE AUDIT PERSONA INSTRUCTIONS",
            "Private transcript text.",
            "description",
            "instructions",
            "prompt",
        ):
            assert secret not in str(audit).lower()
        replay = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token, key),
            json=payload,
        )
        assert replay.status_code == 200
        assert len(_persona_audits(api_app)) == len(audits_after_first)

        current = await client.get(f"/api/v1/conversations/{conversation['id']}")
        reset = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "useAgentDefaultPersona": True,
                "version": current.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert reset.status_code == 200, reset.text
        reset_audit = _persona_audits(api_app)[-1]
        assert reset_audit["action"] == "conversation.persona.reset"
        reset_metadata = cast(dict[str, Any], reset_audit["metadata"])
        assert set(reset_metadata) == {
            "conversationId",
            "personaProfileId",
            "personaRevisionId",
            "revision",
            "source",
            "reason",
        }
        assert reset_metadata["source"] == "agent_default"
        assert reset_metadata["reason"] == "reset_to_agent_default"
        assert all(
            secret not in str(reset_audit).lower()
            for secret in (
                "PRIVATE AUDIT PERSONA INSTRUCTIONS",
                "Private transcript text.",
                "description",
                "instructions",
                "prompt",
            )
        )

        rejected_conversation = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Rejected change.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert rejected_conversation.status_code == 202
        rejected_body = cast(dict[str, Any], rejected_conversation.json())
        rejected = await client.patch(
            f"/api/v1/conversations/{rejected_body['conversation']['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "version": rejected_body["conversation"]["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert rejected.status_code == 409
        assert len(_persona_audits(api_app)) == len(audits_after_first) + 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_combined_agent_and_persona_change_is_atomic_and_shares_one_boundary(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, _ = await seeded_configuration(client)
        alternate_profile = await create_persona(
            client,
            session.csrf_token,
            display_name="Combined change persona",
            instructions="Combined change persona instructions.",
        )
        explicit_profile = await create_persona(
            client,
            session.csrf_token,
            display_name="Explicit override persona",
            instructions="Explicit override instructions.",
        )
        alternate_agent = await create_agent(
            client,
            session.csrf_token,
            display_name="Combined change agent",
            persona_revision_id=alternate_profile["currentRevision"]["id"],
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Prepare an atomic configuration change.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert created.status_code == 202
        body = cast(dict[str, Any], created.json())
        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )
        current = await client.get(f"/api/v1/conversations/{body['conversation']['id']}")
        switched = await client.patch(
            f"/api/v1/conversations/{body['conversation']['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": alternate_agent["currentRevision"]["id"],
                "personaRevisionId": explicit_profile["currentRevision"]["id"],
                "version": current.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert switched.status_code == 200, switched.text
        result = cast(dict[str, Any], switched.json())
        assert result["agentRevisionId"] == alternate_agent["currentRevision"]["id"]
        assert result["persona"]["revisionId"] == explicit_profile["currentRevision"]["id"]
        assert result["personaOverride"] is True
        assert result["agentAssignments"][-1]["effectiveAfterMessageId"] == result[
            "personaAssignments"
        ][-1]["effectiveAfterMessageId"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persona_update_rejects_false_reset_and_mutually_exclusive_fields(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="Validation persona instructions.",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Validation",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert created.status_code == 202
        conversation_id = created.json()["conversation"]["id"]
        version = created.json()["conversation"]["version"]
        false_only = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={"useAgentDefaultPersona": False, "version": version},
        )
        assert false_only.status_code == 422
        both = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "useAgentDefaultPersona": True,
                "version": version,
            },
        )
        assert both.status_code == 422
        false_with_persona = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "useAgentDefaultPersona": False,
                "version": version,
            },
        )
        assert false_with_persona.status_code == 422
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_configuration_telemetry_records_success_failure_and_no_false_model_route(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        _, persona = await seeded_configuration(client)
        metrics = api_app.state.aura.metrics
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="Telemetry persona instructions.",
        )
        configuration_spans = _spans(
            metrics, "aura.interaction.agent_configuration", "agent.configure"
        )
        success = [
            record
            for record in configuration_spans
            if dict(record.dimensions).get("outcome") == "ok"
        ]
        assert success
        assert all(record.value >= 0 for record in success)
        stale = await client.post(
            f"/api/v1/personas/{persona['id']}/revisions",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": persona["currentRevision"]["displayName"],
                "description": "stale retry",
                "instructions": "stale retry",
                "expectedVersion": persona["version"],
            },
        )
        assert stale.status_code == 409
        failures = [
            record
            for record in _spans(metrics, "aura.interaction.agent_configuration", "agent.configure")
            if dict(record.dimensions).get("outcome") == "error"
        ]
        assert failures
        assert any(dict(record.dimensions).get("error_class") == "conflict" for record in failures)
        assert all(record.value >= 0 for record in failures)
        assert all(
            "Telemetry persona instructions." not in str(record)
            for record in failures + success
        )

        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={"message": "Model route check", "modelId": "chat"},
        )
        assert created.status_code == 202
        body = cast(dict[str, Any], created.json())
        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )
        before_routes = len(_spans(metrics, "aura.runtime.model_routing", "model.route"))
        detail = await client.get(f"/api/v1/conversations/{body['conversation']['id']}")
        switched = await client.patch(
            f"/api/v1/conversations/{body['conversation']['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert switched.status_code == 200
        assert len(_spans(metrics, "aura.runtime.model_routing", "model.route")) == before_routes
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persona_configuration_telemetry_covers_lifecycle_and_agent_only_changes(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="Telemetry lifecycle persona instructions.",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Configuration telemetry lifecycle.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert created.status_code == 202
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        conversation_id = conversation["id"]
        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )

        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        override = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert override.status_code == 200, override.text
        override_span = _spans(
            api_app.state.aura.metrics, "aura.interaction.agent_configuration", "agent.configure"
        )[-1]
        _assert_configuration_span(
            override_span,
            conversation_id=conversation_id,
            outcome="ok",
            source="conversation_override",
            reason="manual_override",
        )
        _assert_configuration_outcome_metric(
            api_app.state.aura.metrics, conversation_id=conversation_id, outcome="ok"
        )

        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        reset = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "useAgentDefaultPersona": True,
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert reset.status_code == 200, reset.text
        reset_span = _spans(
            api_app.state.aura.metrics, "aura.interaction.agent_configuration", "agent.configure"
        )[-1]
        _assert_configuration_span(
            reset_span,
            conversation_id=conversation_id,
            outcome="ok",
            source="agent_default",
            reason="reset_to_agent_default",
        )
        _assert_configuration_outcome_metric(
            api_app.state.aura.metrics, conversation_id=conversation_id, outcome="ok"
        )

        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        invalid = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": str(uuid4()),
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert invalid.status_code == 409
        invalid_span = _spans(
            api_app.state.aura.metrics, "aura.interaction.agent_configuration", "agent.configure"
        )[-1]
        _assert_configuration_span(
            invalid_span,
            conversation_id=conversation_id,
            outcome="error",
            source="conversation_override",
            reason="manual_override",
            error_class="not_found",
        )
        _assert_configuration_outcome_metric(
            api_app.state.aura.metrics, conversation_id=conversation_id, outcome="error"
        )

        disabled = await client.patch(
            f"/api/v1/personas/{persona['id']}",
            headers=command_headers(session.csrf_token),
            json={"status": "disabled", "expectedVersion": persona["version"] + 1},
        )
        assert disabled.status_code == 200, disabled.text
        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        disabled_selection = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert disabled_selection.status_code == 409
        disabled_span = _spans(
            api_app.state.aura.metrics, "aura.interaction.agent_configuration", "agent.configure"
        )[-1]
        _assert_configuration_span(
            disabled_span,
            conversation_id=conversation_id,
            outcome="error",
            source="conversation_override",
            reason="manual_override",
            error_class="disabled",
        )
        _assert_configuration_outcome_metric(
            api_app.state.aura.metrics, conversation_id=conversation_id, outcome="error"
        )

        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        conflict = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": agent["currentRevision"]["personaRevisionId"],
                "version": detail.json()["version"] + 10,
                "transcriptSharingConfirmed": True,
            },
        )
        assert conflict.status_code == 409
        conflict_span = _spans(
            api_app.state.aura.metrics, "aura.interaction.agent_configuration", "agent.configure"
        )[-1]
        _assert_configuration_span(
            conflict_span,
            conversation_id=conversation_id,
            outcome="error",
            source="conversation_override",
            reason="manual_override",
            error_class="conflict",
        )
        _assert_configuration_outcome_metric(
            api_app.state.aura.metrics, conversation_id=conversation_id, outcome="error"
        )

        replacement_profile = await create_persona(
            client,
            session.csrf_token,
            display_name="Replay persona",
            instructions="Replay persona instructions.",
        )
        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        key = str(uuid4())
        first = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token, key),
            json={
                "personaRevisionId": replacement_profile["currentRevision"]["id"],
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert first.status_code == 200
        replay_conflict = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token, key),
            json={
                "personaRevisionId": agent["currentRevision"]["personaRevisionId"],
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert replay_conflict.status_code == 409
        replay_span = _spans(
            api_app.state.aura.metrics, "aura.interaction.agent_configuration", "agent.configure"
        )[-1]
        _assert_configuration_span(
            replay_span,
            conversation_id=conversation_id,
            outcome="error",
            source="conversation_override",
            reason="manual_override",
            error_class="idempotency",
        )
        _assert_configuration_outcome_metric(
            api_app.state.aura.metrics, conversation_id=conversation_id, outcome="error"
        )

        alternate_agent = await create_agent(
            client,
            session.csrf_token,
            display_name="Agent-only telemetry change",
            persona_revision_id=replacement_profile["currentRevision"]["id"],
        )
        agent_only_spans = len(
            _spans(
                api_app.state.aura.metrics,
                "aura.interaction.agent_configuration",
                "agent.configure",
            )
        )
        detail = await client.get(f"/api/v1/conversations/{conversation_id}")
        agent_change = await client.patch(
            f"/api/v1/conversations/{conversation_id}",
            headers=command_headers(session.csrf_token),
            json={
                "agentRevisionId": alternate_agent["currentRevision"]["id"],
                "version": detail.json()["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert agent_change.status_code == 200, agent_change.text
        assert len(
            _spans(
                api_app.state.aura.metrics,
                "aura.interaction.agent_configuration",
                "agent.configure",
            )
        ) == agent_only_spans
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persona_override_replay_is_idempotent_and_conflicting_replay_is_rejected(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="Idempotent persona selection.",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Start",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
            },
        )
        assert created.status_code == 202
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        await api_app.state.aura.coordinator.execute(
            UUID(body["run"]["id"]), api_app.state.aura.provider
        )
        key = str(uuid4())
        payload = {
            "personaRevisionId": alternate["id"],
            "version": conversation["version"] + 1,
            "transcriptSharingConfirmed": True,
        }
        # The first user message creates version 2; the exact version is
        # returned by the API and is used to avoid relying on implementation
        # details about version increments.
        detail = await client.get(f"/api/v1/conversations/{conversation['id']}")
        payload["version"] = detail.json()["version"]
        first = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token, key),
            json=payload,
        )
        assert first.status_code == 200, first.text
        replay = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token, key),
            json=payload,
        )
        assert replay.status_code == 200
        assert replay.json() == first.json()
        conflict = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token, key),
            json={**payload, "personaRevisionId": agent["currentRevision"]["personaRevisionId"]},
        )
        assert conflict.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_disabled_persona_cannot_be_selected_but_pinned_run_and_retry_keep_provenance(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        agent, persona = await seeded_configuration(client)
        alternate = await create_persona_revision(
            client,
            session.csrf_token,
            persona,
            instructions="A persona that will be disabled after pinning.",
        )
        created = await client.post(
            "/api/v1/conversations",
            headers=command_headers(session.csrf_token),
            json={
                "message": "Pin before disabling.",
                "modelId": "chat",
                "agentRevisionId": agent["currentRevision"]["id"],
                "personaRevisionId": alternate["id"],
            },
        )
        assert created.status_code == 202, created.text
        body = cast(dict[str, Any], created.json())
        conversation = cast(dict[str, Any], body["conversation"])
        run_id = UUID(body["run"]["id"])
        disabled = await client.patch(
            f"/api/v1/personas/{persona['id']}",
            headers=command_headers(session.csrf_token),
            json={"status": "disabled", "expectedVersion": persona["version"] + 1},
        )
        assert disabled.status_code == 200, disabled.text
        rejected = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=command_headers(session.csrf_token),
            json={
                "personaRevisionId": alternate["id"],
                "version": conversation["version"],
                "transcriptSharingConfirmed": True,
            },
        )
        assert rejected.status_code == 409
        await api_app.state.aura.coordinator.execute(run_id, api_app.state.aura.provider)
        detail = await client.get(f"/api/v1/conversations/{conversation['id']}")
        assert detail.status_code == 200
        assert detail.json()["recentRuns"][0]["personaRevisionId"] == alternate["id"]
        retry = await client.post(
            f"/api/v1/runs/{run_id}/retry",
            headers=command_headers(session.csrf_token),
        )
        assert retry.status_code == 202, retry.text
        assert retry.json()["run"]["personaRevisionId"] == alternate["id"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_persona_routes_remain_owner_scoped_and_require_csrf_for_mutations(
    api_app: FastAPI,
) -> None:
    owner, _ = await owner_client(api_app)
    other, _ = await owner_client(api_app, subject="another-owner")
    try:
        assert (await other.get("/api/v1/personas")).status_code == 403
        persona = (await owner.get("/api/v1/personas")).json()["items"][0]
        missing_csrf = await owner.patch(
            f"/api/v1/personas/{persona['id']}",
            headers={"Idempotency-Key": str(uuid4())},
            json={"status": "disabled", "expectedVersion": persona["version"]},
        )
        assert missing_csrf.status_code == 403
        assert (await other.get("/api/v1/agents")).status_code == 403
    finally:
        await owner.aclose()
        await other.aclose()
