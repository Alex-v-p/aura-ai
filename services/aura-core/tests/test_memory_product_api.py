"""Contract and privacy guards for the AURA-0040 memory product surface.

These tests intentionally exercise the versioned transport contracts rather than
reimplementing memory policy in a test double.  Core owns the authorization,
idempotency, and lifecycle decisions; this suite makes the public boundary
regressions visible while the assembled API tests exercise the implementation.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from aura_core.domains.interaction.agents.public import GENERAL_PROFILE_ID
from aura_core.domains.knowledge.memory.public import (
    MemoryAction,
    MemoryCandidate,
    MemoryKind,
    MemoryModelApplicationService,
    MemoryProcessingJob,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
    MemoryValidationError,
)
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import Principal, Session, Settings
from aura_core.runtime.models.gateway import ModelGateway
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor, ProviderTraceContext
from fastapi import FastAPI

REPOSITORY = Path(__file__).parents[3]
OPENAPI_PATH = REPOSITORY / "contracts/openapi/aura-v1.yaml"
EVENTS_PATH = REPOSITORY / "contracts/events/runs/v1/run-events.schema.json"
GENERATED_CLIENT_PATH = (
    REPOSITORY / "frontend/libs/platform/aura-api-client/src/lib/generated/aura-api.ts"
)
ISSUER = "https://authentik.dev.example"
OWNER = "owner-subject"


class _ProductModel:
    async def list_models(
        self, *, context: ProviderTraceContext | None = None
    ) -> Sequence[ModelDescriptor]:
        del context
        return (
            ModelDescriptor(
                "chat", "Chat model", "ollama", ("structured_output",),
                model_revision="chat-rev", model_digest="a" * 64,
            ),
            ModelDescriptor(
                "embed", "Embedding model", "ollama", ("embedding",),
                model_revision="embed-rev", model_digest="b" * 64, dimension=3,
            ),
        )

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del messages
        if model_id == "chat":
            yield "ok"

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "chat", "embed")

    async def refresh_models(
        self, *, context: ProviderTraceContext | None = None
    ) -> Sequence[ModelDescriptor]:
        return await self.list_models(context=context)

    async def verify_model(
        self,
        model_id: str,
        capability: str,
        *,
        context: ProviderTraceContext | None = None,
    ) -> ModelDescriptor | None:
        del context
        model = next((item for item in await self.list_models() if item.id == model_id), None)
        if model is None:
            return None
        if capability == "structured_output" and "structured_output" in model.capabilities:
            return model
        if capability == "embedding" and "embedding" in model.capabilities:
            return model
        return None


@pytest.fixture
def product_app() -> FastAPI:
    settings = Settings(
        public_origin="https://aura.dev.example",
        oidc_issuer=ISSUER,
        oidc_client_id="aura-web",
        oidc_audience="aura-web",
        oidc_redirect_uri="https://aura.dev.example/api/v1/auth/callback",
        owner_subject=OWNER,
        default_model="chat",
        secure_cookies=False,
    )
    app = create_app(settings, testing=True)
    provider = _ProductModel()
    app.state.aura.provider = provider
    app.state.aura.gateway = ModelGateway(provider)
    return app


async def _owner_client(
    app: FastAPI, *, subject: str = OWNER
) -> tuple[httpx.AsyncClient, Session]:
    session_id, session = await app.state.aura.sessions.create(
        Principal(ISSUER, subject, "Test user")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://aura.dev.example",
        headers={"Origin": "https://aura.dev.example"},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, session_id)
    return client, session


async def _seed_candidate(
    app: FastAPI,
    *,
    subject: str = OWNER,
    content: str = "The owner prefers tea.",
    action: MemoryAction = MemoryAction.CREATE,
    importance: float | None = 0.8,
    half_life_days: float | None = 30.0,
) -> UUID:
    job = MemoryProcessingJob(uuid4(), ISSUER, subject, uuid4(), uuid4())
    repository = app.state.aura.memory_repository
    await repository.enqueue_processing_job(job)
    candidate = MemoryCandidate(
        uuid4(),
        job.id,
        ISSUER,
        subject,
        action,
        content,
        MemoryKind.PREFERENCE,
        MemoryScope(MemoryScopeType.USER),
        0.9,
        importance,
        half_life_days,
        created_at=datetime.now(UTC),
    )
    await repository.persist_candidate(candidate)
    return candidate.id


def _openapi() -> dict[str, Any]:
    # aura-v1.yaml is deliberately JSON-compatible so contract tooling has one
    # canonical representation without introducing a parser dependency here.
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def _events() -> dict[str, Any]:
    return json.loads(EVENTS_PATH.read_text(encoding="utf-8"))


def _schema(spec: dict[str, Any], name: str) -> dict[str, Any]:
    return spec["components"]["schemas"][name]


def _refs(operation: dict[str, Any], name: str) -> set[str]:
    return {
        item["$ref"].rsplit("/", 1)[-1]
        for item in operation.get("parameters", [])
        if "$ref" in item and item["$ref"].startswith(f"#/components/{name}/")
    }


def _parameter(spec: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
    reference = item.get("$ref")
    if reference:
        return spec["components"]["parameters"][reference.rsplit("/", 1)[-1]]
    return item


def _property_names(schema: Any) -> set[str]:
    """Collect names from the bounded public activity schema recursively."""

    if not isinstance(schema, dict):
        return set()
    object_schema = cast(dict[str, Any], schema)
    names = set(cast(dict[str, Any], object_schema.get("properties", {})))
    for key in ("allOf", "oneOf", "anyOf"):
        for child in cast(list[Any], object_schema.get(key, [])):
            names |= _property_names(child)
    return names


def test_memory_product_resources_are_versioned_and_owner_authenticated() -> None:
    spec = _openapi()
    assert spec["info"]["version"] == "1.5.0"
    assert spec["security"] == [{"sessionCookie": []}]

    expected = {
        "/api/v1/memories",
        "/api/v1/memories/{memory_id}",
        "/api/v1/memory-candidates",
        "/api/v1/memory-candidates/{candidate_id}",
        "/api/v1/memory-candidates/{candidate_id}/approve",
        "/api/v1/memory-candidates/{candidate_id}/reject",
        "/api/v1/memory-model-inventory",
        "/api/v1/memory-model-configuration",
        "/api/v1/memory-reindex",
        "/api/v1/memory-reindex/resume",
        "/api/v1/runs/{run_id}/memory-activity",
        "/api/v1/agents/{agent_profile_id}/memory-policies",
        "/api/v1/agents/{agent_profile_id}/memory-policies/{policy_revision_id}/attach",
    }
    assert expected <= set(spec["paths"])

    # No memory endpoint opts out of the global owner session requirement.
    for path in expected:
        for operation in spec["paths"][path].values():
            if isinstance(operation, dict):
                assert cast(dict[str, Any], operation).get("security") != []


def test_memory_browse_filters_are_bounded_and_history_is_opt_in() -> None:
    operation = _openapi()["paths"]["/api/v1/memories"]["get"]
    params = {
        _parameter(_openapi(), item)["name"]: _parameter(_openapi(), item)
        for item in operation["parameters"]
    }
    assert {
        "cursor",
        "limit",
        "kind",
        "scopeType",
        "agentProfileId",
        "status",
        "provenanceType",
        "confidenceMin",
        "confidenceMax",
        "createdFrom",
        "createdTo",
        "includeHistorical",
    } <= set(params)
    assert "q" not in params
    assert params["cursor"]["schema"]["maxLength"] == 1024
    assert params["limit"]["schema"]["maximum"] == 100
    assert params["includeHistorical"]["schema"]["default"] is False
    search = _openapi()["paths"]["/api/v1/memories/search"]["post"]
    search_schema = search["requestBody"]["content"]["application/json"]["schema"]
    assert search_schema["$ref"].endswith("SearchMemoriesRequest")
    assert _schema(_openapi(), "SearchMemoriesRequest")["properties"]["query"]["maxLength"] == 500


def test_candidate_decisions_require_idempotency_and_optimistic_versioning() -> None:
    spec = _openapi()
    for suffix, body_name in (
        ("approve", "ApproveMemoryCandidateRequest"),
        ("reject", "RejectMemoryCandidateRequest"),
    ):
        operation = spec["paths"][f"/api/v1/memory-candidates/{{candidate_id}}/{suffix}"]["post"]
        assert {"CsrfToken", "IdempotencyKey"} <= _refs(operation, "parameters")
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith(body_name)
        assert "expectedVersion" in _schema(spec, body_name)["required"]

    edit = _schema(spec, "MemoryCandidateEdit")
    assert {
        "content",
        "action",
        "kind",
        "scope",
        "confidence",
        "importance",
        "halfLifeDays",
        "validTo",
        "relatedMemoryId",
    } <= set(edit["required"])
    assert _schema(spec, "MemoryCandidateDecisionReceipt")["required"] == [
        "candidate",
        "activityId",
    ]


def test_model_inventory_configuration_and_reindex_never_accept_or_return_provider_secrets(
) -> None:
    spec = _openapi()
    inventory = _schema(spec, "MemoryCompatibleModel")
    assert set(inventory["properties"]) == {
        "id",
        "displayName",
        "provider",
        "modelRevision",
        "modelDigest",
        "capabilities",
        "dimension",
        "available",
        "disabledReason",
    }
    selection = _schema(spec, "UpdateMemoryModelConfigurationRequest")
    assert set(selection["properties"]) == {
        "extractionModelId",
        "embeddingModelId",
        "expectedVersion",
    }
    forbidden = {"endpoint", "url", "token", "credential", "password", "vector"}
    for name in ("MemoryModelInventory", "MemoryModelConfiguration", "MemoryReindexStatus"):
        assert not forbidden & {key.lower() for key in _schema(spec, name)["properties"]}
    reindex = _schema(spec, "MemoryReindexStatus")
    assert {
        "activeGeneration",
        "replacementGeneration",
        "processedRevisionCount",
        "totalRevisionCount",
        "retryable",
    } <= set(reindex["required"])


def test_agent_memory_policy_contract_enforces_retrieval_only_ceilings_and_immutable_attach(
) -> None:
    spec = _openapi()
    policy = _schema(spec, "AgentMemoryPolicy")
    assert {
        "sharedUserRead",
        "currentAgentRead",
        "sharedUserPromotion",
        "fallbackRelevanceThreshold",
        "maxMemories",
        "contextBudgetFraction",
        "fallbackAgentProfileIds",
    } <= set(policy["required"])
    assert policy["properties"]["maxMemories"]["maximum"] == 8
    assert policy["properties"]["contextBudgetFraction"]["maximum"] == 0.2
    description = policy["properties"]["fallbackAgentProfileIds"]["description"].lower()
    assert "retrieval" in description and "delegation" in description

    attach = spec["paths"][
        "/api/v1/agents/{agent_profile_id}/memory-policies/{policy_revision_id}/attach"
    ]["post"]
    assert {"CsrfToken", "IdempotencyKey"} <= _refs(attach, "parameters")
    attach_schema = _schema(spec, "AttachAgentMemoryPolicyRequest")
    assert attach_schema["required"] == ["expectedAgentVersion"]


def test_run_memory_activity_snapshot_and_sse_event_are_identifier_only() -> None:
    spec = _openapi()
    snapshot = _schema(spec, "RunMemoryActivitySnapshot")
    assert snapshot["properties"]["items"]["maxItems"] == 100
    assert {
        "runId",
        "processingStatus",
        "items",
        "lastEventId",
        "reconciledAt",
    } <= set(snapshot["required"])
    activity = _schema(spec, "MemoryActivity")
    assert activity["additionalProperties"] is False
    assert {
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
    } == set(activity["properties"])
    forbidden = {
        "content",
        "evidence",
        "prompt",
        "response",
        "vector",
        "credential",
        "ownerSubject",
    }
    assert not forbidden & _property_names(activity)

    events = _events()
    memory_event = events["$defs"]["MemoryActivityEvent"]
    assert memory_event["allOf"][1]["properties"]["eventType"]["const"] == "memory.activity"
    assert {"MemoryActivityEvent", "HeartbeatEvent"} <= {
        ref["$ref"].rsplit("/", 1)[-1] for ref in events["oneOf"]
    }
    event_activity = events["$defs"]["MemoryActivity"]
    assert event_activity["additionalProperties"] is False
    assert not forbidden & _property_names(event_activity)


def test_memory_activity_reconnect_contract_has_cursor_replay_and_content_safe_expiry() -> None:
    spec = _openapi()
    stream = spec["paths"]["/api/v1/runs/{run_id}/events"]["get"]
    last_event_id = next(
        _parameter(spec, item)
        for item in stream["parameters"]
        if _parameter(spec, item)["name"] == "Last-Event-ID"
    )
    assert last_event_id["in"] == "header"
    assert any(str(code) == "410" for code in stream["responses"])
    snapshot = spec["paths"]["/api/v1/runs/{run_id}/memory-activity"]["get"]
    assert snapshot["operationId"] == "getRunMemoryActivity"
    assert any(str(code) == "404" for code in snapshot["responses"])


def test_generated_client_contains_transport_only_memory_surface() -> None:
    generated = GENERATED_CLIENT_PATH.read_text(encoding="utf-8")
    expected_operations = {
        "getRunMemoryActivity",
        "listMemoryCandidates",
        "getMemoryCandidate",
        "approveMemoryCandidate",
        "rejectMemoryCandidate",
        "getMemoryModelInventory",
        "getMemoryModelConfiguration",
        "updateMemoryModelConfiguration",
        "getMemoryReindexStatus",
        "resumeMemoryReindex",
        "listAgentMemoryPolicies",
        "createAgentMemoryPolicy",
        "attachAgentMemoryPolicy",
    }
    for operation in expected_operations:
        assert f"{operation}:" in generated
    assert "MemoryActivity" in generated
    assert "export type" in generated


@pytest.mark.asyncio
async def test_candidate_api_is_owner_scoped_and_reject_replay_is_content_free(
    product_app: FastAPI,
) -> None:
    owner, owner_session = await _owner_client(product_app)
    foreign, _ = await _owner_client(product_app, subject="other-owner")
    try:
        candidate_id = await _seed_candidate(product_app)
        foreign_candidate_id = await _seed_candidate(product_app, subject="other-owner")

        assert (
            await owner.get(f"/api/v1/memory-candidates/{foreign_candidate_id}")
        ).status_code == 404
        listed = await owner.get("/api/v1/memory-candidates")
        assert listed.status_code == 200
        assert [item["id"] for item in listed.json()["items"]] == [str(candidate_id)]

        headers = {
            "X-CSRF-Token": owner_session.csrf_token,
            "Idempotency-Key": str(uuid4()),
        }
        first = await owner.post(
            f"/api/v1/memory-candidates/{candidate_id}/reject",
            headers=headers,
            json={"expectedVersion": 1, "reason": "not durable"},
        )
        replay = await owner.post(
            f"/api/v1/memory-candidates/{candidate_id}/reject",
            headers=headers,
            json={"expectedVersion": 1, "reason": "not durable"},
        )
        assert first.status_code == replay.status_code == 200, first.text
        assert replay.json() == first.json()
        rejected = first.json()["candidate"]
        assert rejected["state"] == "rejected"
        assert rejected["content"] is None
        assert rejected["groundedMessageIds"] == []
        assert "not durable" in rejected["decisionReason"]
        assert "The owner prefers tea." not in first.text
    finally:
        await owner.aclose()
        await foreign.aclose()


@pytest.mark.asyncio
async def test_legacy_provider_review_candidate_can_be_approved_unchanged(
    product_app: FastAPI,
) -> None:
    client, session = await _owner_client(product_app)
    try:
        candidate_id = await _seed_candidate(
            product_app,
            action=MemoryAction.REVIEW,
            importance=None,
            half_life_days=None,
        )
        response = await client.post(
            f"/api/v1/memory-candidates/{candidate_id}/approve",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={"expectedVersion": 1},
        )

        assert response.status_code == 200, response.text
        approved = response.json()["candidate"]
        assert approved["action"] == "create"
        assert approved["state"] == "accepted"
        assert approved["memoryId"]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_api_boundaries_emit_registered_metadata_without_rejection(
    product_app: FastAPI,
) -> None:
    client, session = await _owner_client(product_app)
    try:
        assert (await client.get("/api/v1/memories")).status_code == 200
        candidate_id = await _seed_candidate(product_app)
        rejected = await client.post(
            f"/api/v1/memory-candidates/{candidate_id}/reject",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={"expectedVersion": 1, "reason": "not durable"},
        )
        assert rejected.status_code == 200
        assert (await client.get("/api/v1/memory-model-configuration")).status_code == 404
        assert (
            await client.get(
                f"/api/v1/agents/{str(GENERAL_PROFILE_ID)}/memory-policies"
            )
        ).status_code == 200
        metrics = product_app.state.aura.metrics
        names = {item.metric for item in metrics.snapshot()}
        assert {
            "memory_product_operation_duration_ms",
            "memory_product_operation_outcome",
            "memory_candidate_review_duration_ms",
            "memory_candidate_review_outcome",
            "memory_model_configuration_duration_ms",
            "memory_model_configuration_outcome",
            "memory_policy_configuration_duration_ms",
            "memory_policy_configuration_outcome",
        } <= names
        assert metrics.stats().rejected == 0
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_candidate_edit_approve_rechecks_version_and_sensitive_content(
    product_app: FastAPI,
) -> None:
    client, session = await _owner_client(product_app)
    try:
        candidate_id = await _seed_candidate(product_app)
        stale = await client.post(
            f"/api/v1/memory-candidates/{candidate_id}/approve",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={"expectedVersion": 2},
        )
        assert stale.status_code == 409

        secret = "api_key: do-not-persist"
        rejected = await client.post(
            f"/api/v1/memory-candidates/{candidate_id}/approve",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "expectedVersion": 1,
                "edit": {
                    "content": secret,
                    "action": "create",
                    "kind": "preference",
                    "scope": {"type": "user"},
                    "confidence": 0.9,
                    "importance": 0.8,
                    "halfLifeDays": 30,
                    "validTo": None,
                    "relatedMemoryId": None,
                },
            },
        )
        assert rejected.status_code == 422
        assert secret not in rejected.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_model_inventory_configuration_and_reindex_are_owner_scoped(
    product_app: FastAPI,
) -> None:
    client, session = await _owner_client(product_app)
    foreign, _ = await _owner_client(product_app, subject="other-owner")
    try:
        inventory = await client.get("/api/v1/memory-model-inventory")
        assert inventory.status_code == 200
        assert {item["id"] for item in inventory.json()["models"]} == {"chat", "embed"}
        assert all(
            not {"endpoint", "url", "token", "credential", "vector"}
            & {key.lower() for key in item}
            for item in inventory.json()["models"]
        )

        headers = {
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": str(uuid4()),
        }
        configured = await client.put(
            "/api/v1/memory-model-configuration",
            headers=headers,
            json={
                "extractionModelId": "chat",
                "embeddingModelId": "embed",
                "expectedVersion": 1,
            },
        )
        assert configured.status_code == 200, configured.text
        payload = configured.json()
        assert payload["extraction"]["modelId"] == "chat"
        assert payload["embedding"]["modelId"] == "embed"
        assert "vector" not in configured.text.lower()

        reindex = await client.get("/api/v1/memory-reindex")
        assert reindex.status_code == 200
        active_generation = reindex.json()["activeGeneration"]
        assert active_generation is None or active_generation["dimension"] >= 1
        assert (await foreign.get("/api/v1/memory-model-configuration")).status_code == 404
    finally:
        await client.aclose()
        await foreign.aclose()


@pytest.mark.asyncio
async def test_memory_configuration_verifies_selected_models_and_preserves_on_failure(
) -> None:
    class LazyProvider(_ProductModel):
        def __init__(self) -> None:
            self.verifications: list[tuple[str, str]] = []
            self.fail = False
            self.tamper = False

        async def list_models(
            self, *, context: ProviderTraceContext | None = None
        ) -> Sequence[ModelDescriptor]:
            del context
            return (
                ModelDescriptor(
                    "chat", "Chat model", "ollama", ("chat", "structured_output"),
                    model_revision="chat-rev", model_digest="a" * 64,
                ),
                ModelDescriptor(
                    "embed", "Embedding model", "ollama", ("embedding",),
                    model_revision="embed-rev", model_digest="b" * 64,
                ),
                ModelDescriptor(
                    "unused", "Unused model", "ollama", ("chat",),
                    model_revision="unused-rev", model_digest="c" * 64,
                ),
            )

        async def verify_model(
            self,
            model_id: str,
            capability: str,
            *,
            context: ProviderTraceContext | None = None,
        ) -> ModelDescriptor | None:
            del context
            self.verifications.append((model_id, capability))
            if self.fail:
                return None
            if capability == "structured_output":
                return ModelDescriptor(
                    model_id, model_id, "ollama", ("chat", "structured_output"),
                    model_revision="chat-rev", model_digest="c" * 64 if self.tamper else "a" * 64,
                )
            return ModelDescriptor(
                model_id, model_id, "ollama", ("embedding",),
                model_revision="embed-rev", model_digest="b" * 64, dimension=3,
            )

    provider = LazyProvider()
    repository = MemoryStore()
    service = MemoryModelApplicationService(repository, provider, None)
    await service.save_configuration(
        ISSUER,
        OWNER,
        extraction_model_id="chat",
        embedding_model_id="embed",
        expected_version=1,
        idempotency_key="first-selection",
    )
    assert provider.verifications == [("chat", "structured_output"), ("embed", "embedding")]

    provider.fail = True
    with pytest.raises(MemoryValidationError):
        await service.save_configuration(
            ISSUER,
            OWNER,
            extraction_model_id="chat",
            embedding_model_id="embed",
            expected_version=2,
            idempotency_key="failed-selection",
        )
    current = await repository.get_model_configuration(ISSUER, OWNER)
    assert current.version == 1

    provider.fail = False
    provider.tamper = True
    with pytest.raises(MemoryValidationError):
        await service.save_configuration(
            ISSUER,
            OWNER,
            extraction_model_id="chat",
            embedding_model_id="embed",
            expected_version=2,
            idempotency_key="mismatched-selection",
        )
    current = await repository.get_model_configuration(ISSUER, OWNER)
    assert current.version == 1


@pytest.mark.asyncio
async def test_memory_inventory_excludes_unverified_provider_models(
    product_app: FastAPI,
) -> None:
    class InventoryProvider(_ProductModel):
        async def list_models(
            self, *, context: ProviderTraceContext | None = None
        ) -> Sequence[ModelDescriptor]:
            del context
            return (
                ModelDescriptor(
                    "good-chat",
                    "Good chat",
                    "ollama",
                    ("structured_output",),
                    model_revision="chat-rev",
                    model_digest="a" * 64,
                ),
                ModelDescriptor(
                    "missing-chat-digest",
                    "Missing digest",
                    "ollama",
                    ("structured_output",),
                    model_revision="chat-rev",
                ),
                ModelDescriptor(
                    "good-embed",
                    "Good embed",
                    "ollama",
                    ("embedding",),
                    model_revision="embed-rev",
                    model_digest="b" * 64,
                    dimension=3,
                ),
                ModelDescriptor(
                    "missing-embed-dimension",
                    "Missing dimension",
                    "ollama",
                    ("embedding",),
                    model_revision="embed-rev",
                    model_digest="c" * 64,
                ),
            )

    product_app.state.aura.provider = InventoryProvider()
    client, _ = await _owner_client(product_app)
    try:
        response = await client.get("/api/v1/memory-model-inventory")
        assert response.status_code == 200
        assert {item["id"] for item in response.json()["models"]} == {
            "good-chat",
            "good-embed",
            "missing-embed-dimension",
        }
        assert all(item["available"] for item in response.json()["models"])
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_reindex_resume_keeps_pending_receipt_until_dispatch_settles() -> None:
    store = MemoryStore()
    generation = await store.register_embedding_generation(
        ISSUER,
        OWNER,
        generation=1,
        model_id="embed",
        model_revision="embed-rev",
        model_digest="b" * 64,
        dimension=3,
        status="building",
    )

    class Worker:
        calls = 0

        async def resume_reindex(self, issuer: str, subject: str, generation_id: UUID) -> None:
            del issuer, subject, generation_id
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("injected dispatch failure")

    worker = Worker()
    service = MemoryModelApplicationService(store, _ProductModel(), worker)
    with pytest.raises(RuntimeError, match="injected dispatch failure"):
        await service.resume(ISSUER, OWNER, generation.id, "resume-key")
    await service.resume(ISSUER, OWNER, generation.id, "resume-key")
    await service.resume(ISSUER, OWNER, generation.id, "resume-key")
    assert worker.calls == 2


@pytest.mark.asyncio
async def test_memory_policy_api_is_versioned_owner_scoped_and_retrieval_only(
    product_app: FastAPI,
) -> None:
    client, session = await _owner_client(product_app)
    foreign, _ = await _owner_client(product_app, subject="other-owner")
    agent_id = str(GENERAL_PROFILE_ID)
    try:
        policies = await client.get(f"/api/v1/agents/{agent_id}/memory-policies")
        assert policies.status_code == 200, policies.text
        assert policies.json()["items"]
        expected_revision = policies.json()["items"][-1]["revision"]
        headers = {
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": str(uuid4()),
        }
        body: dict[str, object] = {
            "sharedUserRead": True,
            "currentAgentRead": True,
            "sharedUserPromotion": False,
            "fallbackRelevanceThreshold": 0.6,
            "maxMemories": 8,
            "contextBudgetFraction": 0.2,
            "fallbackAgentProfileIds": [],
            "expectedRevision": expected_revision,
        }
        created = await client.post(
            f"/api/v1/agents/{agent_id}/memory-policies",
            headers=headers,
            json=body,
        )
        assert created.status_code == 201, created.text
        assert created.json()["revision"] == expected_revision + 1
        replay = await client.post(
            f"/api/v1/agents/{agent_id}/memory-policies", headers=headers, json=body
        )
        assert replay.status_code == 201, replay.text
        assert replay.json() == created.json()
        assert (await foreign.get(f"/api/v1/agents/{agent_id}/memory-policies")).status_code in {
            403,
            404,
        }

        self_grant = await client.post(
            f"/api/v1/agents/{agent_id}/memory-policies",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={
                "sharedUserRead": True,
                "currentAgentRead": True,
                "sharedUserPromotion": False,
                "fallbackRelevanceThreshold": 0.6,
                "maxMemories": 8,
                "contextBudgetFraction": 0.2,
                "fallbackAgentProfileIds": [agent_id],
                "expectedRevision": expected_revision + 1,
            },
        )
        assert self_grant.status_code in {400, 409, 422}
    finally:
        await client.aclose()
        await foreign.aclose()
