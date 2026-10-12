"""HTTP contract and lifecycle coverage for versioned agent configuration."""

from collections.abc import AsyncIterator, Sequence
from typing import cast
from uuid import UUID, uuid4

import pytest
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
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
async def test_persona_preview_auth_failures_are_generic_and_not_cached(api_app: FastAPI) -> None:
    client, session = await owner_client(api_app)
    payload = {
        "modelId": "chat",
        "prompt": "hello",
        "draft": {
            "displayName": "Guide",
            "description": "A guide.",
            "instructions": "Be clear.",
        },
    }
    try:
        missing_csrf = await client.post("/api/v1/personas/preview", json=payload)
        assert missing_csrf.status_code == 403
        assert missing_csrf.headers["cache-control"] == "no-store"
        assert "csrf" not in missing_csrf.text.lower()

        invalid_csrf = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": "invalid"},
            json=payload,
        )
        assert invalid_csrf.status_code == 403
        assert invalid_csrf.headers["cache-control"] == "no-store"
        assert "csrf" not in invalid_csrf.text.lower()

        invalid_request = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": "invalid"},
            json={"modelId": "chat", "prompt": "hello"},
        )
        assert invalid_request.status_code == 403
        assert invalid_request.headers["cache-control"] == "no-store"

        # The dependency must succeed before transport validation can be
        # observed, so use the real session token for this malformed body.
        invalid_request = await client.post(
            "/api/v1/personas/preview",
            headers={"X-CSRF-Token": session.csrf_token},
            json={"modelId": "chat", "prompt": "hello"},
        )
        assert invalid_request.status_code == 422
        assert invalid_request.headers["cache-control"] == "no-store"

        preview_spans = [
            item
            for item in api_app.state.aura.metrics.snapshot()
            if item.component_id == "aura.interaction.persona_preview"
            and item.kind == "span"
            and dict(item.trace_attributes).get("operation") == "persona.preview"
        ]
        auth_span = next(
            item
            for item in preview_spans
            if dict(item.dimensions).get("error_class") == "authorization"
        )
        validation_span = next(
            item
            for item in preview_spans
            if dict(item.dimensions).get("error_class") == "validation"
        )
        assert dict(auth_span.dimensions)["dependency"] == "identity_authorization"
        assert dict(validation_span.dimensions)["dependency"] == "transport_validation"
        assert auth_span.value > 0
        assert validation_span.value > 0

        client.cookies.clear()
        unauthenticated = await client.post("/api/v1/personas/preview", json=payload)
        assert unauthenticated.status_code == 401
        assert unauthenticated.headers["cache-control"] == "no-store"
        assert "authentication required" in unauthenticated.text.lower()
    finally:
        await client.aclose()


class _FailingPreviewProvider:
    async def list_models(self) -> Sequence[ModelDescriptor]:
        return (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in {None, "chat"}

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del model_id, messages
        raise RuntimeError("provider failure")
        yield ""


@pytest.mark.asyncio
async def test_model_gateway_provider_errors_include_bounded_provider() -> None:
    from aura_core.platform.telemetry import MetadataMetrics
    from aura_core.runtime.models.gateway import ModelGateway

    metrics = MetadataMetrics()
    gateway = ModelGateway(_FailingPreviewProvider(), metrics)
    with pytest.raises(RuntimeError, match="preview provider unavailable"):
        await gateway.preview("chat", "system", "prompt")
    provider_error = next(
        item
        for item in metrics.snapshot()
        if item.component_id == "aura.runtime.model_inference"
        and item.metric == "provider_errors"
    )
    assert dict(provider_error.dimensions)["provider"] == "ollama"


@pytest.mark.asyncio
async def test_ollama_persona_preview_trace_parenting_and_inventory_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from aura_core.domains.interaction.personas.public import (
        PersonaCatalog,
        PersonaConfigurationService,
        PersonaMemoryRepository,
        PersonaPreviewResult,
        PersonaPreviewTrace,
        PersonaRevision,
    )
    from aura_core.platform.telemetry import MetadataMetrics
    from aura_core.providers.models.ollama.adapter import OllamaAdapter
    from aura_core.runtime.models.gateway import ModelGateway
    from aura_core.runtime.models.ports import ProviderTraceContext

    metrics = MetadataMetrics()
    adapter = OllamaAdapter(
        "http://ollama.test",
        telemetry=metrics,
        inventory_cache_ttl_seconds=60.0,
    )

    async def inventory() -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)

    async def stream_chat(model_id: str, messages: Sequence[ChatMessage]) -> AsyncIterator[str]:
        del model_id, messages
        yield "preview"

    monkeypatch.setattr(adapter, "_read_inventory", inventory)
    monkeypatch.setattr(adapter, "stream_chat", stream_chat)
    gateway = ModelGateway(adapter, metrics)
    catalog = PersonaCatalog()

    class PreviewPort:
        async def preview(
            self,
            model_id: str,
            system_prompt: str,
            prompt: str,
            *,
            trace: PersonaPreviewTrace,
        ) -> PersonaPreviewResult:
            result = await gateway.preview_with_metadata(
                model_id,
                system_prompt,
                prompt,
                context=ProviderTraceContext(trace_id=trace.trace_id, span_id=trace.span_id),
            )
            return PersonaPreviewResult(result.content, result.provider)

    service = PersonaConfigurationService(
        PersonaMemoryRepository(catalog),
        preview_port=PreviewPort(),
        metrics=metrics,
    )
    draft = PersonaRevision(uuid4(), uuid4(), 1, "Description", "Instructions", "Guide")
    await service.preview("issuer", "subject", "chat", "hello", draft=draft)
    await service.preview("issuer", "subject", "chat", "hello", draft=draft)

    roots = [
        item
        for item in metrics.snapshot()
        if item.component_id == "aura.interaction.persona_preview"
        and item.kind == "span"
        and dict(item.trace_attributes).get("operation") == "persona.preview"
    ]
    assert len(roots) == 2
    for root in roots:
        inventory_spans = [
            item
            for item in metrics.snapshot()
            if item.component_id == "aura.runtime.structured_inference"
            and dict(item.trace_attributes).get("operation") == "model.inventory"
            and item.trace_id == root.trace_id
        ]
        inference_spans = [
            item
            for item in metrics.snapshot()
            if item.component_id == "aura.runtime.model_inference"
            and dict(item.trace_attributes).get("operation") == "model.infer"
            and item.trace_id == root.trace_id
        ]
        assert len(inventory_spans) == 1
        assert len(inference_spans) == 1
        assert inventory_spans[0].parent_span_id == root.span_id
        assert inference_spans[0].parent_span_id == root.span_id
        assert dict(root.dimensions)["provider"] == "ollama"

    cache_outcomes = [
        dict(item.dimensions)["outcome"]
        for item in metrics.snapshot()
        if item.component_id == "aura.runtime.structured_inference"
        and item.metric == "model_inventory_cache_outcome"
    ]
    assert cache_outcomes == ["miss", "hit"]


@pytest.mark.asyncio
async def test_persona_preview_domain_classifications_are_bounded_and_content_free() -> None:
    from aura_core.domains.interaction.personas.public import (
        ConfigurationStatus,
        PersonaCatalog,
        PersonaConfigurationService,
        PersonaMemoryRepository,
        PersonaPreviewError,
        PersonaPreviewTrace,
        PersonaRevision,
    )
    from aura_core.platform.telemetry import MetadataMetrics

    metrics = MetadataMetrics()
    catalog = PersonaCatalog()
    draft = PersonaRevision(uuid4(), uuid4(), 1, "Description", "Instructions", "Guide")

    class ErrorPort:
        def __init__(self, error_class: str) -> None:
            self.error_class = error_class

        async def preview(
            self,
            model_id: str,
            system_prompt: str,
            prompt: str,
            *,
            trace: PersonaPreviewTrace,
        ) -> str:
            del model_id, system_prompt, prompt, trace
            if self.error_class == "unknown":
                raise Exception("UNEXPECTED_PREVIEW_SECRET")
            raise PersonaPreviewError(self.error_class, provider="ollama")

    async def invoke(
        service: PersonaConfigurationService,
        *,
        persona_revision_id: UUID | None = None,
    ) -> None:
        with pytest.raises(PersonaPreviewError):
            await service.preview(
                "owner-issuer",
                "owner-subject",
                "chat",
                "PROMPT_SECRET",
                draft=draft if persona_revision_id is None else None,
                persona_revision_id=persona_revision_id,
            )

    await invoke(
        PersonaConfigurationService(PersonaMemoryRepository(catalog), metrics=metrics)
    )
    await invoke(
        PersonaConfigurationService(
            PersonaMemoryRepository(catalog), preview_port=ErrorPort("provider"), metrics=metrics
        )
    )
    await invoke(
        PersonaConfigurationService(
            PersonaMemoryRepository(catalog), preview_port=ErrorPort("timeout"), metrics=metrics
        )
    )
    await invoke(
        PersonaConfigurationService(
            PersonaMemoryRepository(catalog), preview_port=ErrorPort("validation"), metrics=metrics
        )
    )
    await invoke(
        PersonaConfigurationService(
            PersonaMemoryRepository(catalog), preview_port=ErrorPort("unknown"), metrics=metrics
        )
    )
    await invoke(
        PersonaConfigurationService(
            PersonaMemoryRepository(catalog), preview_port=ErrorPort("capability"), metrics=metrics
        ),
        persona_revision_id=uuid4(),
    )
    disabled_catalog = PersonaCatalog()
    disabled_profile = next(iter(disabled_catalog.personas.values()))
    disabled_profile.status = ConfigurationStatus.DISABLED
    await invoke(
        PersonaConfigurationService(
            PersonaMemoryRepository(disabled_catalog),
            preview_port=ErrorPort("provider"),
            metrics=metrics,
        ),
        persona_revision_id=disabled_profile.current_revision_id,
    )

    outcomes = [
        dict(item.dimensions).get("error_class")
        for item in metrics.snapshot()
        if item.metric == "persona_preview_outcome"
    ]
    assert outcomes == [
        "capability",
        "provider",
        "timeout",
        "validation",
        "unknown",
        "not_found",
        "disabled",
    ]
    expected_dependencies = {
        "capability": "model_provider",
        "provider": "model_provider",
        "timeout": "model_provider",
        "validation": "application_validation",
        "unknown": "application_unknown",
        "not_found": "configuration_store",
        "disabled": "configuration_store",
    }
    snapshots = metrics.snapshot()
    outcome_records = [item for item in snapshots if item.metric == "persona_preview_outcome"]
    for outcome_record in outcome_records:
        outcome_dimensions = dict(outcome_record.dimensions)
        error_class = outcome_dimensions["error_class"]
        root_span = next(
            item
            for item in snapshots
            if item.trace_id == outcome_record.trace_id
            and item.kind == "span"
            and dict(item.trace_attributes).get("operation") == "persona.preview"
        )
        error_record = next(
            item
            for item in snapshots
            if item.trace_id == outcome_record.trace_id and item.metric == "errors"
        )
        expected_dependency = expected_dependencies[error_class]
        assert dict(error_record.dimensions)["dependency"] == expected_dependency
        assert dict(root_span.dimensions)["dependency"] == expected_dependency
    provider_errors = [
        item
        for item in metrics.snapshot()
        if item.metric == "provider_errors"
    ]
    assert {dict(item.dimensions)["error_class"] for item in provider_errors} == {
        "provider",
        "timeout",
    }
    assert all(dict(item.dimensions)["provider"] == "ollama" for item in provider_errors)
    assert "owner-subject" not in repr(metrics.snapshot())
    assert "PROMPT_SECRET" not in repr(metrics.snapshot())
    assert "UNEXPECTED_PREVIEW_SECRET" not in repr(metrics.snapshot())


@pytest.mark.asyncio
async def test_tool_catalog_and_agent_policies_are_owner_scoped_and_immutable(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        catalog = await client.get("/api/v1/tools")
        assert catalog.status_code == 200
        tools = catalog.json()["items"]
        assert [item["id"] for item in tools] == [
            "memory.search",
            "reminder.create",
            "reminder.list",
            "reminder.inspect",
            "reminder.series.create",
            "reminder.reschedule",
            "reminder.series.update",
            "reminder.series.pause",
            "reminder.series.resume",
            "reminder.cancel",
        ]
        assert tools[0]["version"] == 1
        assert "handler" not in tools[0]
        assert "schema" not in tools[0]

        agents = (await client.get("/api/v1/agents")).json()["items"]
        aura = agents[0]
        aura_policies = await client.get(f"/api/v1/agents/{aura['id']}/tool-policies")
        assert aura_policies.status_code == 200
        aura_policy = aura_policies.json()
        assert aura_policy["items"][-1]["grants"] == [
            {"toolId": "memory.search", "toolVersion": 1},
            {"toolId": "reminder.create", "toolVersion": 1},
            {"toolId": "reminder.list", "toolVersion": 1},
            {"toolId": "reminder.inspect", "toolVersion": 1},
            {"toolId": "reminder.series.create", "toolVersion": 1},
            {"toolId": "reminder.reschedule", "toolVersion": 1},
            {"toolId": "reminder.series.update", "toolVersion": 1},
            {"toolId": "reminder.series.pause", "toolVersion": 1},
            {"toolId": "reminder.series.resume", "toolVersion": 1},
            {"toolId": "reminder.cancel", "toolVersion": 1},
        ]
        assert aura_policy["items"][0]["maxCallsPerRun"] == 1

        persona = (await client.get("/api/v1/personas")).json()["items"][0]
        created = await client.post(
            "/api/v1/agents",
            headers=command_headers(session.csrf_token),
            json={
                "displayName": "Custom",
                "purpose": "Test",
                "instructions": "Be concise.",
                "personaRevisionId": persona["currentRevision"]["id"],
            },
        )
        assert created.status_code == 201
        custom = created.json()
        policies = await client.get(f"/api/v1/agents/{custom['id']}/tool-policies")
        assert policies.status_code == 200
        assert policies.json()["items"][0]["grants"] == []

        key = str(uuid4())
        policy = await client.post(
            f"/api/v1/agents/{custom['id']}/tool-policies",
            headers=command_headers(session.csrf_token, key),
            json={"grants": [{"toolId": "memory.search", "toolVersion": 1}], "expectedRevision": 1},
        )
        assert policy.status_code == 201
        assert policy.json()["maxCallsPerRun"] == 1
        replay = await client.post(
            f"/api/v1/agents/{custom['id']}/tool-policies",
            headers=command_headers(session.csrf_token, key),
            json={"grants": [{"toolId": "memory.search", "toolVersion": 1}], "expectedRevision": 1},
        )
        assert replay.status_code == 201
        assert replay.json() == policy.json()

        detail = (await client.get(f"/api/v1/agents/{custom['id']}")).json()
        attached = await client.post(
            f"/api/v1/agents/{custom['id']}/tool-policies/{policy.json()['id']}/attach",
            headers=command_headers(session.csrf_token),
            json={"expectedAgentVersion": detail["version"]},
        )
        assert attached.status_code == 201
        assert attached.json()["currentRevision"]["toolPolicyRevisionId"] == policy.json()["id"]
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
