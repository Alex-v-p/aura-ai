"""End-to-end API checks using only in-process deterministic dependencies."""

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Sequence
from typing import TypedDict, cast
from uuid import UUID, uuid4

import httpx
import pytest
from aura_core.domains.execution.runs.events import RunEvent, new_event
from aura_core.domains.interaction.conversations.public import InvalidConversationCursor
from aura_core.domains.knowledge.memory.public import (
    CandidateState,
    MemoryModelConfiguration,
    MemoryProcessingCommand,
    MemoryProcessingJob,
    MemoryProcessingService,
    MemoryScopeType,
    MemoryTurnEvidence,
)
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import Settings
from aura_core.platform.telemetry import new_span_id
from aura_core.runtime.models.gateway import ModelGateway
from aura_core.runtime.models.ports import ChatMessage, EmbeddingResult, StructuredInferenceRequest
from fastapi import FastAPI

from conftest import ScriptedModel, owner_client


class _ConversationSummary(TypedDict):
    id: str
    version: int


class _RunSummary(TypedDict):
    id: str


class _CreatedConversation(TypedDict):
    conversation: _ConversationSummary
    run: _RunSummary


class _ConversationPageItem(TypedDict):
    id: str


class _ConversationPage(TypedDict):
    items: list[_ConversationPageItem]
    nextCursor: str | None


class _PreferenceInference:
    """Deterministic structured extractor for the assembled memory path."""

    async def infer(self, request: StructuredInferenceRequest) -> dict[str, object]:
        raw_segments = request.input.get("evidence_segments")
        assert isinstance(raw_segments, list) and raw_segments
        segments = cast(list[object], raw_segments)
        first = segments[0]
        assert isinstance(first, dict)
        first_payload = cast(dict[str, object], first)
        handle = first_payload.get("handle")
        assert isinstance(handle, str)
        return {
            "action": "create",
            "content": "I prefer concise answers.",
            "kind": "preference",
            "scope_type": "user",
            "confidence": 0.99,
            "importance": 0.8,
            "half_life_days": 365,
            "grounded_evidence_handles": [handle],
        }


class _PreferenceEmbedding:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def embed(self, model_id: str, text: str) -> EmbeddingResult:
        self.calls.append(text)
        return EmbeddingResult(
            vector=(0.1, 0.2, 0.3),
            model_id=model_id,
            model_revision="embedder-v1",
            dimension=3,
            model_digest="e" * 64,
            digest=hashlib.sha256(text.encode()).hexdigest(),
        )


@pytest.mark.asyncio
async def test_owner_authentication_csrf_and_lazy_idempotent_creation(api_app: FastAPI) -> None:
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="https://aura.dev.example"
    )
    try:
        assert (await anonymous.get("/api/v1/models")).status_code == 401
    finally:
        await anonymous.aclose()

    client, session = await owner_client(api_app)
    try:
        # A new draft is browser-local; no database conversation exists before POST.
        assert (await client.get("/api/v1/conversations")).json()["items"] == []
        missing_csrf = await client.post(
            "/api/v1/conversations",
            headers={"Idempotency-Key": str(uuid4())},
            json={"message": "Hello", "modelId": "chat"},
        )
        assert missing_csrf.status_code == 403

        headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())}
        first = await client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"message": "Hello", "modelId": "chat"},
        )
        assert first.status_code == 202
        accepted = first.json()
        assert {"conversation", "userMessage", "run"} <= accepted.keys()
        assert accepted["conversation"]["currentRun"]["id"] == accepted["run"]["id"]

        replay = await client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"message": "Hello", "modelId": "chat"},
        )
        assert replay.status_code == 202
        assert replay.json()["conversation"]["id"] == accepted["conversation"]["id"]
        listing = await client.get("/api/v1/conversations")
        assert len(listing.json()["items"]) == 1
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_recent_conversation_pages_follow_accepted_user_activity(api_app: FastAPI) -> None:
    """Metadata updates and pagination must not make a conversation look active."""

    client, session = await owner_client(api_app)
    try:
        def headers() -> dict[str, str]:
            return {
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            }

        async def create(message: str) -> _CreatedConversation:
            response = await client.post(
                "/api/v1/conversations",
                headers=headers(),
                json={"message": message, "modelId": "chat"},
            )
            assert response.status_code == 202
            return cast(_CreatedConversation, response.json())

        first = await create("first accepted message")
        second = await create("second accepted message")
        third = await create("third accepted message")
        for created in (first, second, third):
            await api_app.state.aura.coordinator.execute(
                UUID(created["run"]["id"]), api_app.state.aura.provider
            )
        first_conversation = first["conversation"]
        assert isinstance(first_conversation, dict)
        first_id = str(first_conversation["id"])

        initial_page = await client.get("/api/v1/conversations", params={"limit": 2})
        assert initial_page.status_code == 200
        initial_payload = cast(_ConversationPage, initial_page.json())
        assert [item["id"] for item in initial_payload["items"]] == [
            third["conversation"]["id"],
            second["conversation"]["id"],
        ]
        cursor = initial_payload["nextCursor"]
        assert isinstance(cursor, str) and cursor

        remainder = await client.get(
            "/api/v1/conversations", params={"limit": 2, "cursor": cursor}
        )
        remainder_payload = cast(_ConversationPage, remainder.json())
        assert [item["id"] for item in remainder_payload["items"]] == [first_id]

        # Configuration is a metadata-only mutation and must retain the same
        # activity order and cursor boundary.
        configured = await client.patch(
            f"/api/v1/conversations/{first_id}",
            headers=headers(),
            json={
                "modelId": "chat-plus",
                "version": first_conversation["version"],
            },
        )
        assert configured.status_code == 200
        after_configuration = await client.get(
            "/api/v1/conversations", params={"limit": 2}
        )
        after_configuration_payload = cast(_ConversationPage, after_configuration.json())
        assert [item["id"] for item in after_configuration_payload["items"]] == [
            third["conversation"]["id"],
            second["conversation"]["id"],
        ]
        assert after_configuration_payload["nextCursor"] == cursor

        # A newly accepted user message is the only operation in this flow
        # that promotes an existing conversation.
        accepted = await client.post(
            f"/api/v1/conversations/{first_id}/runs",
            headers=headers(),
            json={
                "message": "new accepted activity",
                "conversationVersion": cast(_ConversationSummary, configured.json())["version"],
            },
        )
        assert accepted.status_code == 202
        promoted = await client.get("/api/v1/conversations", params={"limit": 3})
        promoted_payload = cast(_ConversationPage, promoted.json())
        assert [item["id"] for item in promoted_payload["items"]] == [
            first_id,
            third["conversation"]["id"],
            second["conversation"]["id"],
        ]
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_conversation_list_failure_emits_parented_bounded_error_spans(
    api_app: FastAPI,
) -> None:
    client, _ = await owner_client(api_app)
    cookies = client.cookies
    await client.aclose()
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app, raise_app_exceptions=False),
        base_url="https://aura.dev.example",
        headers={"Origin": "https://aura.dev.example"},
        cookies=cookies,
    )
    try:
        metrics = api_app.state.aura.metrics
        store = api_app.state.aura.store

        async def failing_list(
            subject: str,
            limit: int = 30,
            cursor: str | None = None,
            issuer: str | None = None,
            *,
            trace_id: str | None = None,
            parent_span_id: str | None = None,
        ) -> tuple[list[object], str | None]:
            del subject, limit, cursor, issuer
            assert trace_id is not None
            assert parent_span_id is not None
            metrics.record_span(
                "aura.interaction.conversation_persistence",
                "conversation.list",
                1.0,
                trace_id=trace_id,
                span_id=new_span_id(),
                parent_span_id=parent_span_id,
                dependency="postgresql",
                outcome="error",
                error_class="persistence",
            )
            raise ValueError("PRIVATE_DATABASE_DETAIL")

        store.list = failing_list  # type: ignore[method-assign]
        response = await client.get("/api/v1/conversations")
        assert response.status_code == 500
        assert "PRIVATE_DATABASE_DETAIL" not in response.text

        spans = [item for item in metrics.snapshot() if item.kind == "span"]
        root = next(
            item
            for item in spans
            if item.component_id == "aura.interaction.conversation_persistence"
            and dict(item.trace_attributes).get("operation") == "conversation.list.request"
            and dict(item.dimensions).get("outcome") == "error"
        )
        child = next(
            item
            for item in spans
            if item.component_id == "aura.interaction.conversation_persistence"
            and dict(item.trace_attributes).get("operation") == "conversation.list"
        )
        assert dict(root.dimensions)["error_class"] == "persistence"
        assert dict(child.dimensions)["error_class"] == "persistence"
        assert child.trace_id == root.trace_id
        assert child.parent_span_id == root.span_id
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_invalid_conversation_cursor_emits_linked_validation_spans(
    api_app: FastAPI,
) -> None:
    client, _ = await owner_client(api_app)
    try:
        metrics = api_app.state.aura.metrics
        store = api_app.state.aura.store

        async def invalid_cursor(
            subject: str,
            limit: int = 30,
            cursor: str | None = None,
            issuer: str | None = None,
            *,
            trace_id: str | None = None,
            parent_span_id: str | None = None,
        ) -> tuple[list[object], str | None]:
            del subject, limit, cursor, issuer
            assert trace_id is not None and parent_span_id is not None
            metrics.record_span(
                "aura.interaction.conversation_persistence",
                "conversation.list",
                1.0,
                trace_id=trace_id,
                span_id=new_span_id(),
                parent_span_id=parent_span_id,
                dependency="postgresql",
                outcome="error",
                error_class="validation",
            )
            raise InvalidConversationCursor("invalid cursor")

        store.list = invalid_cursor  # type: ignore[method-assign]
        response = await client.get("/api/v1/conversations", params={"cursor": "bad"})
        assert response.status_code == 400
        assert response.json()["detail"] == "invalid cursor"

        spans = [item for item in metrics.snapshot() if item.kind == "span"]
        root = next(
            item
            for item in spans
            if item.component_id == "aura.interaction.conversation_persistence"
            and dict(item.trace_attributes).get("operation") == "conversation.list.request"
            and dict(item.dimensions).get("outcome") == "error"
        )
        child = next(
            item
            for item in spans
            if item.component_id == "aura.interaction.conversation_persistence"
            and dict(item.trace_attributes).get("operation") == "conversation.list"
        )
        assert dict(root.dimensions)["error_class"] == "validation"
        assert dict(child.dimensions)["error_class"] == "validation"
        assert child.trace_id == root.trace_id
        assert child.parent_span_id == root.span_id
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_liveness_is_separate_from_dependency_readiness(api_app: FastAPI) -> None:
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app), base_url="https://aura.dev.example"
    )
    try:
        live = await client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"status": "alive"}
        ready = await client.get("/health/ready")
        assert ready.status_code == 503
        assert ready.json()["status"] == "degraded"
        assert {"postgres", "nats", "oidc", "defaultModel", "ollama"} <= ready.json()[
            "checks"
        ].keys()
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_non_owner_is_rejected_and_csrf_is_session_bound(api_app: FastAPI) -> None:
    client, session = await owner_client(api_app, subject="different-subject")
    try:
        assert (await client.get("/api/v1/auth/session")).status_code == 403
        assert (await client.get("/api/v1/conversations")).status_code == 403
        response = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token + "x", "Idempotency-Key": str(uuid4())},
            json={"message": "blocked", "modelId": "chat"},
        )
        assert response.status_code == 403
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_model_catalog_selection_and_incompatible_model_rejection(api_app: FastAPI) -> None:
    client, session = await owner_client(api_app)
    try:
        catalog = (await client.get("/api/v1/models")).json()
        models = {model["id"]: model for model in catalog["models"]}
        assert models["chat"]["selectable"] is True
        assert models["embed"]["selectable"] is False
        assert models["embed"]["disabledReason"]

        headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())}
        rejected = await client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"message": "bad model", "modelId": "embed"},
        )
        assert rejected.status_code == 422

        created = await client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"message": "select chat", "modelId": "chat"},
        )
        conversation = created.json()["conversation"]
        updated = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"modelId": "chat-plus", "version": conversation["version"]},
        )
        assert updated.status_code == 200
        assert updated.json()["modelId"] == "chat-plus"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_streaming_contract_replays_snapshot_deltas_and_terminal_status(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())}
        created = await client.post(
            "/api/v1/conversations", headers=headers, json={"message": "stream", "modelId": "chat"}
        )
        run_id = created.json()["run"]["id"]
        await api_app.state.aura.publisher.publish(
            new_event(
                "run.snapshot",
                UUID(run_id),
                UUID(created.json()["conversation"]["id"]),
                0,
                {"run": created.json()["run"], "assistantMessage": None},
            )
        )
        await api_app.state.aura.coordinator.execute(UUID(run_id), api_app.state.aura.provider)

        async with client.stream("GET", f"/api/v1/runs/{run_id}/events") as response:
            assert response.status_code == 200
            assert response.headers["cache-control"] == "no-store"
            body = (await response.aread()).decode()
        blocks = [block for block in body.strip().split("\n\n") if block]
        event_names = [
            next(
                line.removeprefix("event: ")
                for line in block.splitlines()
                if line.startswith("event:")
            )
            for block in blocks
        ]
        payloads = [
            json.loads(
                next(
                    line.removeprefix("data: ")
                    for line in block.splitlines()
                    if line.startswith("data:")
                )
            )
            for block in blocks
        ]
        assert event_names[0] == "run.snapshot"
        assert "assistant.delta" in event_names
        assert "assistant.snapshot" in event_names
        assert event_names[-1] == "run.status"
        assert payloads[0]["schemaVersion"] == 1
        assert payloads[0]["data"]["run"]["status"] == "completed"
        assert payloads[-1]["data"]["status"] == "completed"
        assert all(payload["runId"] == run_id for payload in payloads)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_completed_question_delivers_and_processes_durable_memory_end_to_end(
    api_app: FastAPI,
) -> None:
    """A completed answer must enqueue, settle, and embed one memory job."""

    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "message": "I prefer concise answers.",
                "modelId": "chat",
            },
        )
        assert created.status_code == 202
        run_id = UUID(created.json()["run"]["id"])

        # This is the same coordinator/provider boundary used by the local
        # worker path; a delivery failure must not be converted into a failed
        # run after the assistant has been persisted.
        await api_app.state.aura.coordinator.execute(run_id, api_app.state.aura.provider)
        conversation, run = await api_app.state.aura.store.find_run_any(run_id)
        assert run.status.value == "completed"
        assistant = next(
            message for message in conversation.messages if message.id == run.assistant_message_id
        )
        assert assistant.state.value == "complete"
        assert assistant.content == "Hello from Aura."

        history = await api_app.state.aura.publisher.history(run_id)
        assert [event.sequence for event in history] == list(range(len(history)))
        assert {event.event_type for event in history} >= {
            "run.snapshot",
            "assistant.snapshot",
            "run.status",
        }
        assert all(
            cast(dict[str, object], event.data).get("code") != "DELIVERY_ERROR"
            for event in history
        )

        execution_command = await api_app.state.aura.outbox.receive()
        assert execution_command.topic == "aura.runs.execute.v1"
        command = await api_app.state.aura.outbox.receive()
        assert command.topic == "aura.memory.process.v1"
        assert command.run_id == run_id

        repository = api_app.state.aura.memory_repository
        generation = await repository.register_embedding_generation(
            "https://authentik.dev.example",
            "owner-subject",
            generation=1,
            model_id="memory-embedder",
            model_revision="embedder-v1",
            dimension=3,
            model_digest="e" * 64,
        )
        generation = await repository.activate_embedding_generation(
            "https://authentik.dev.example", "owner-subject", generation.id
        )
        configuration = MemoryModelConfiguration(
            "https://authentik.dev.example",
            "owner-subject",
            "memory-extractor",
            "memory-embedder",
            extraction_model_revision="extractor-v1",
            embedding_model_revision="embedder-v1",
            embedding_generation=generation.id,
        )
        await repository.save_model_configuration(
            "https://authentik.dev.example",
            "owner-subject",
            configuration,
            expected_version=1,
            idempotency_key="system-memory-configuration",
            dimension=3,
            model_digest="e" * 64,
        )

        async def evidence_loader(job: MemoryProcessingJob) -> MemoryTurnEvidence:
            owner_conversation, owner_run = await api_app.state.aura.store.find_run_any(
                run_id
            )
            user = next(
                item
                for item in owner_conversation.messages
                if item.id == owner_run.user_message_id
            )
            completed_assistant = next(
                item
                for item in owner_conversation.messages
                if item.id == owner_run.assistant_message_id
            )
            user_ids = (user.id,)
            assistant_ids = (completed_assistant.id,)
            digest = hashlib.sha256(user.content.encode()).hexdigest()
            return MemoryTurnEvidence(
                job.issuer,
                job.subject,
                job.run_id,
                job.conversation_id,
                job.agent_revision_id,
                user_ids,
                assistant_ids,
                user.content,
                completed_assistant.content,
                digest,
            )

        processor = MemoryProcessingService(
            repository,
            _PreferenceInference(),
            _PreferenceEmbedding(),
            evidence_loader=evidence_loader,
        )
        job = MemoryProcessingJob(
            run_id,
            "https://authentik.dev.example",
            "owner-subject",
            run_id,
            conversation.id,
            correlation_id=run.attempt_id,
            causation_id=run.id,
            agent_revision_id=run.agent_revision_id,
            user_message_ids=(run.user_message_id,),
            assistant_message_ids=(run.assistant_message_id,),
            agent_profile_id=conversation.agent_profile_id,
            allow_shared_user_promotion=True,
            memory_policy_revision_id=run.memory_policy_revision_id,
            evidence_digest=hashlib.sha256(
                b"I prefer concise answers."
            ).hexdigest(),
        )
        await repository.enqueue_processing_job(job)
        processor.jobs[(job.issuer, job.subject, job.run_id)] = job
        assert job.id == run_id

        # Exercise the same generic identifier-envelope parser used by the
        # assembled worker transport, rather than bypassing it with the
        # domain-specific payload parser.
        parsed_command = MemoryProcessingCommand.from_outbox(command)
        settlement = await processor.process_command(parsed_command)
        assert settlement.terminal is True
        assert settlement.status.value == "completed"

        candidates = await repository.list_candidates(
            "https://authentik.dev.example", "owner-subject", run_id=run_id
        )
        assert len(candidates) == 1
        assert candidates[0].state is CandidateState.ACCEPTED
        assert candidates[0].memory_id is not None
        assert candidates[0].action.value == "create"

        memory = await repository.get_memory(
            "https://authentik.dev.example",
            "owner-subject",
            candidates[0].memory_id,
            scope_type=MemoryScopeType.USER,
        )
        assert memory.content == "I prefer concise answers."
        assert len(memory.revisions) == 1
        assert len(memory.embeddings) == 1

        replay = await processor.process_command(parsed_command)
        assert replay.terminal is True
        assert len(
            await repository.list_candidates(
                "https://authentik.dev.example", "owner-subject", run_id=run_id
            )
        ) == 1
        replayed_memory = await repository.get_memory(
            "https://authentik.dev.example",
            "owner-subject",
            candidates[0].memory_id,
            scope_type=MemoryScopeType.USER,
        )
        assert len(replayed_memory.revisions) == 1
        assert len(replayed_memory.embeddings) == 1

        activity = await client.get(f"/api/v1/runs/{run_id}/memory-activity")
        assert activity.status_code == 200
        payload = activity.json()
        assert payload["processingStatus"] == "settled"
        assert payload["items"][0]["action"] == "created"
        assert payload["items"][0]["status"] == "completed"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_off_agent_recall_is_skipped_while_memory_processing_is_queued(
    api_app: FastAPI,
) -> None:
    """Task-focused agents omit ambient recall without disabling extraction."""

    client, session = await owner_client(api_app)
    try:
        persona = (await client.get("/api/v1/personas")).json()["items"][0]
        created_agent = await client.post(
            "/api/v1/agents",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "displayName": "Task-focused test agent",
                "purpose": "Keep task context narrow.",
                "instructions": "Answer only the current task.",
                "personaRevisionId": persona["currentRevision"]["id"],
            },
        )
        assert created_agent.status_code == 201, created_agent.text
        agent_revision_id = created_agent.json()["currentRevision"]["id"]

        created = await client.post(
            "/api/v1/conversations",
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": str(uuid4()),
            },
            json={
                "message": "Remember that this turn must stay task-focused.",
                "modelId": "chat",
                "agentRevisionId": agent_revision_id,
            },
        )
        assert created.status_code == 202, created.text
        run_id = UUID(created.json()["run"]["id"])
        await api_app.state.aura.coordinator.execute(run_id, api_app.state.aura.provider)

        conversation, run = await api_app.state.aura.store.find_run_any(run_id)
        assert str(conversation.agent_revision_id) == agent_revision_id
        assert run.status.value == "completed"
        assert run.memory_recall_metadata is not None
        assert run.memory_recall_metadata["recallMode"] == "off"
        assert run.memory_recall_metadata["gateOutcome"] == "skipped/policy_off"
        assert run.memory_recall_metadata["selections"] == []
        assert run.memory_recall_metadata["embeddingGenerationId"] is None

        execution_command = await api_app.state.aura.outbox.receive()
        assert execution_command.topic == "aura.runs.execute.v1"
        memory_command = await api_app.state.aura.outbox.receive()
        assert memory_command.topic == "aura.memory.process.v1"
        assert memory_command.run_id == run_id
    finally:
        await client.aclose()


class PausingModel(ScriptedModel):
    def __init__(self) -> None:
        super().__init__(("partial",))
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        self.started.set()
        async for chunk in super().stream_chat(model_id, messages):
            yield chunk
            await self.release.wait()
        # A provider may deliver one more frame after cancellation is requested;
        # the coordinator must observe the command before appending it.
        if self.release.is_set():
            yield "tail-after-cancel"


@pytest.mark.asyncio
async def test_cancel_preserves_partial_output_and_retry_links_history(api_app: FastAPI) -> None:
    provider = PausingModel()
    api_app.state.aura.provider = provider
    api_app.state.aura.gateway = ModelGateway(provider)
    client, session = await owner_client(api_app)
    try:
        create_headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())}
        created = await client.post(
            "/api/v1/conversations",
            headers=create_headers,
            json={"message": "cancel me", "modelId": "chat"},
        )
        run_id = created.json()["run"]["id"]
        execution = asyncio.create_task(
            api_app.state.aura.coordinator.execute(UUID(run_id), provider)
        )
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        for _ in range(100):
            current, _ = await api_app.state.aura.store.find_run_any(UUID(run_id))
            if any(message.role.value == "assistant" for message in current.messages):
                break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("partial assistant checkpoint was not persisted")

        canceled = await client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        assert canceled.status_code == 202
        provider.release.set()
        await asyncio.wait_for(execution, timeout=1)

        conversation_id = created.json()["conversation"]["id"]
        detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assert detail["recentRuns"][0]["status"] == "canceled"
        assistant = next(
            message for message in detail["messages"] if message["role"] == "assistant"
        )
        assert assistant["content"] == "partial"
        assert assistant["state"] == "interrupted"

        retried = await client.post(
            f"/api/v1/runs/{run_id}/retry",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        assert retried.status_code == 202
        retry = retried.json()
        assert retry["run"]["retryOfRunId"] == run_id
        assert retry["run"]["userMessageId"] == created.json()["userMessage"]["id"]
        final_detail = (await client.get(f"/api/v1/conversations/{conversation_id}")).json()
        assert len(final_detail["messages"]) == 2
    finally:
        if not provider.release.is_set():
            provider.release.set()
        await client.aclose()


@pytest.mark.asyncio
async def test_cancel_closes_provider_that_never_emits(api_app: FastAPI) -> None:
    class StalledModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__(())
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.closed = asyncio.Event()

        async def stream_chat(
            self, model_id: str, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            del model_id, messages
            self.started.set()
            try:
                await self.release.wait()
                yield "unexpected"
            finally:
                self.closed.set()

    provider = StalledModel()
    api_app.state.aura.provider = provider
    client, session = await owner_client(api_app)
    execution: asyncio.Task[None] | None = None
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "cancel stalled provider", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])
        execution = asyncio.create_task(api_app.state.aura.coordinator.execute(run_id, provider))
        await asyncio.wait_for(provider.started.wait(), timeout=1)

        canceled = await client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
        )
        assert canceled.status_code == 202
        await asyncio.wait_for(execution, timeout=1)
        await asyncio.wait_for(provider.closed.wait(), timeout=1)

        _, run = await api_app.state.aura.store.find_run_any(run_id)
        assert run.status.value == "canceled"
        assert run.assistant_message_id is None
    finally:
        provider.release.set()
        if execution is not None and not execution.done():
            execution.cancel()
            await asyncio.gather(execution, return_exceptions=True)
        await client.aclose()


@pytest.mark.asyncio
async def test_active_event_subscription_receives_lease_expiry_without_reconnect(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    pending: asyncio.Task[RunEvent] | None = None
    stream: AsyncIterator[RunEvent] | None = None
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "expire lease", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])
        acquired = await api_app.state.aura.store.start_run(
            run_id, worker_id=uuid4(), lease_seconds=0.01
        )
        assert acquired.acquired is True

        current_stream = api_app.state.aura.publisher.stream(run_id)
        stream = current_stream
        pending = asyncio.ensure_future(anext(current_stream))
        await asyncio.sleep(0.03)
        expired = await api_app.state.aura.store.start_run(
            run_id, worker_id=uuid4(), lease_seconds=300
        )
        assert expired.run.status.value == "interrupted"
        await api_app.state.aura.coordinator.publish_reconciled_status(expired.run)

        event = await asyncio.wait_for(pending, timeout=1)
        assert event.event_type == "run.status"
        assert event.data["status"] == "interrupted"
    finally:
        if pending is not None and not pending.done():
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if stream is not None:
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()
        await client.aclose()


@pytest.mark.asyncio
async def test_independent_conversations_can_execute_concurrently(api_app: FastAPI) -> None:
    class BarrierModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__(("done",))
            self.entered = 0
            self.both_entered = asyncio.Event()
            self.release = asyncio.Event()

        async def stream_chat(
            self, model_id: str, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            del messages
            self.entered += 1
            if self.entered == 2:
                self.both_entered.set()
            await self.release.wait()
            yield "done"

    provider = BarrierModel()
    api_app.state.aura.provider = provider
    api_app.state.aura.gateway = ModelGateway(provider)
    client, session = await owner_client(api_app)
    try:
        headers = {"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())}
        first = await client.post(
            "/api/v1/conversations",
            headers=headers,
            json={"message": "one", "modelId": "chat"},
        )
        second = await client.post(
            "/api/v1/conversations",
            headers={**headers, "Idempotency-Key": str(uuid4())},
            json={"message": "two", "modelId": "chat"},
        )
        tasks = [
            asyncio.create_task(
                api_app.state.aura.coordinator.execute(UUID(item.json()["run"]["id"]), provider)
            )
            for item in (first, second)
        ]
        await asyncio.wait_for(provider.both_entered.wait(), timeout=1)
        provider.release.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        assert provider.entered == 2
    finally:
        provider.release.set()
        await client.aclose()


@pytest.mark.asyncio
async def test_redelivered_run_never_enters_provider_twice(api_app: FastAPI) -> None:
    class ClaimedModel(ScriptedModel):
        def __init__(self) -> None:
            super().__init__(("done",))
            self.entered = 0
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def stream_chat(
            self, model_id: str, messages: Sequence[ChatMessage]
        ) -> AsyncIterator[str]:
            del model_id, messages
            self.entered += 1
            self.started.set()
            await self.release.wait()
            yield "done"

    provider = ClaimedModel()
    api_app.state.aura.provider = provider
    client, session = await owner_client(api_app)
    try:
        created = await client.post(
            "/api/v1/conversations",
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": str(uuid4())},
            json={"message": "claim once", "modelId": "chat"},
        )
        run_id = UUID(created.json()["run"]["id"])
        first = asyncio.create_task(api_app.state.aura.coordinator.execute(run_id, provider))
        await asyncio.wait_for(provider.started.wait(), timeout=1)
        await asyncio.wait_for(api_app.state.aura.coordinator.execute(run_id, provider), timeout=1)
        assert provider.entered == 1
        provider.release.set()
        await asyncio.wait_for(first, timeout=1)
        await api_app.state.aura.coordinator.execute(run_id, provider)
        assert provider.entered == 1
    finally:
        provider.release.set()
        await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "issuer", ["https://authentik.dev.example", "https://identity.external.example"]
)
async def test_login_uses_configured_oidc_issuer_in_local_or_external_mode(issuer: str) -> None:
    app = create_app(Settings(oidc_issuer=issuer, oidc_client_id="aura-web"), testing=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://aura.dev.example"
    )
    try:
        async def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/.well-known/openid-configuration"
            return httpx.Response(
                200, json={"authorization_endpoint": f"{issuer}/oauth2/authorize"}
            )

        original = httpx.AsyncClient
        transport = httpx.MockTransport(handler)
        httpx.AsyncClient = lambda *args, **kwargs: original(*args, transport=transport, **kwargs)  # type: ignore[method-assign]
        try:
            response = await client.get(
                "/api/v1/auth/login", params={"return_to": "/chat"}
            )
        finally:
            httpx.AsyncClient = original  # type: ignore[method-assign]
        assert response.status_code == 302
        location = response.headers["location"]
        assert location.startswith(f"{issuer}/oauth2/authorize?")
        assert "aura-core-api" not in location
        assert "authentik-server" not in location
    finally:
        await client.aclose()
