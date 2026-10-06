"""Regression tests for the AURA-0020 review findings.

The tests in this module intentionally exercise the public application seams.
They are kept separate from the feature smoke tests so a change cannot pass by
only checking the happy-path fixture data.
"""

from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from uuid import uuid4

import pytest
from aura_core.domains.execution.runs.dto import RunStatus
from aura_core.domains.execution.runs.public import RunCoordinator, run_payload
from aura_core.domains.interaction.agents.public import (
    GENERAL_POLICY_ID,
    AgentCatalog,
    ConfigurationDisabled,
    ConfigurationStatus,
    PromptBundleRevision,
    PromptComponentRevision,
)
from aura_core.domains.interaction.conversations.public import (
    AgentUnavailable,
    ConversationStore,
    IdempotencyConflict,
)
from aura_core.domains.interaction.personas.public import PersonaCatalog
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.telemetry import MetadataMetrics, new_span_id
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
from aura_core.runtime.streaming.publisher import EventPublisher


class CaptureProvider:
    def __init__(self, *chunks: str) -> None:
        self.chunks = chunks or ("ok",)
        self.calls = 0
        self.messages: list[tuple[ChatMessage, ...]] = []

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "chat")

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        assert model_id == "chat"
        self.calls += 1
        self.messages.append(tuple(messages))
        for chunk in self.chunks:
            yield chunk


@pytest.mark.asyncio
async def test_disabled_assigned_agent_blocks_new_run_and_retry_but_history_remains_readable(
) -> None:
    provider = CaptureProvider()
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    conversation, _, original = await store.create(
        "issuer", "owner", "first", "chat", models, str(uuid4())
    )
    await store.start_run(original.id)
    await store.append_assistant(original.id, "answer")
    await store.finish_run(original.id, RunStatus.COMPLETED)
    profile = catalog.list_agents()[0]
    catalog.set_agent_status(profile.id, profile.version, ConfigurationStatus.DISABLED)

    with pytest.raises(AgentUnavailable):
        await store.add_run(
            conversation.id,
            "owner",
            "blocked",
            conversation.version,
            str(uuid4()),
            "issuer",
        )
    with pytest.raises(AgentUnavailable):
        await store.retry(original.id, "owner", str(uuid4()), "issuer")
    readable = await store.get(conversation.id, "owner", "issuer")
    assert readable.messages[0].content == "first"
    assert readable.runs[0].status is RunStatus.COMPLETED


@pytest.mark.asyncio
async def test_non_default_model_policy_is_pinned_to_new_runs_and_retries() -> None:
    models = await CaptureProvider().list_models()
    catalog = AgentCatalog()
    custom_policy = uuid4()
    profile = catalog.create_agent(
        "researcher", "Researcher", "Research", "Cite sources.",
        model_policy_revision_id=custom_policy,
    )
    revision = profile.current_revision
    assert revision.model_policy_revision_id == custom_policy
    store = ConversationStore(agents=catalog)
    conversation, _, original = await store.create(
        "issuer", "owner", "question", "chat", models, str(uuid4()), revision.id
    )
    assert original.model_policy_revision_id == custom_policy
    await store.start_run(original.id)
    await store.append_assistant(original.id, "answer")
    await store.finish_run(original.id, RunStatus.COMPLETED)
    _, _, retry = await store.retry(original.id, "owner", str(uuid4()), "issuer")
    assert retry.model_policy_revision_id == custom_policy
    await store.start_run(retry.id)
    await store.finish_run(retry.id, RunStatus.COMPLETED)
    _, _, added = await store.add_run(
        conversation.id, "owner", "next", conversation.version, str(uuid4()), "issuer"
    )
    assert added.model_policy_revision_id == custom_policy


@pytest.mark.asyncio
async def test_disabled_persona_rejects_revision_without_rewriting_old_agent_revision() -> None:
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    persona = personas.list_personas()[0]
    agent = catalog.list_agents()[0]
    original = agent.current_revision
    personas.set_status(
        "issuer",
        "owner",
        persona.id,
        persona.version,
        ConfigurationStatus.DISABLED,
        str(uuid4()),
    )
    persona_revision_id = persona.current_revision_id
    assert persona_revision_id is not None

    with pytest.raises(ConfigurationDisabled):
        catalog.revise_agent(
            agent.id,
            agent.version,
            "new purpose",
            "new instructions",
            persona_revision_id,
        )
    assert agent.current_revision_id == original.id
    assert (
        catalog.resolve_revision_unchecked(original.id).persona_revision_id
        == persona_revision_id
    )


@pytest.mark.asyncio
async def test_queued_run_completes_with_pinned_revision_after_agent_is_disabled() -> None:
    provider = CaptureProvider("done")
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    _, _, queued = await store.create(
        "issuer", "owner", "queued", "chat", models, str(uuid4())
    )
    revision_id = queued.agent_revision_id
    profile = catalog.list_agents()[0]
    catalog.set_agent_status(profile.id, profile.version, ConfigurationStatus.DISABLED)

    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())
    await coordinator.execute(queued.id, provider)
    _, completed = await store.find_run_any(queued.id)
    assert completed.status is RunStatus.COMPLETED
    assert completed.agent_revision_id == revision_id
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_historical_retry_after_switch_uses_original_prompt_revision() -> None:
    provider = CaptureProvider("answer")
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    conversation, _, original = await store.create(
        "issuer", "owner", "question", "chat", models, str(uuid4())
    )
    original_prompt = catalog.compilation(original.agent_revision_id).text
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())
    await coordinator.execute(original.id, provider)
    replacement = catalog.create_agent(
        "researcher", "Researcher", "Research", "Cite sources."
    ).current_revision
    conversation = await store.switch_agent(
        conversation.id,
        "owner",
        replacement.id,
        conversation.version,
        str(uuid4()),
        "issuer",
        confirmation=True,
    )
    _, _, retry = await store.retry(original.id, "owner", str(uuid4()), "issuer")
    await coordinator.execute(retry.id, provider)
    assert retry.agent_revision_id == original.agent_revision_id
    assert provider.messages[-1][0].content == original_prompt


@pytest.mark.asyncio
async def test_worker_rejects_stored_prompt_provenance_before_provider_call() -> None:
    provider = CaptureProvider("must not run")
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    _, _, run = await store.create(
        "issuer", "owner", "question", "chat", models, str(uuid4())
    )
    run.prompt_hash = "0" * 64
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())

    await coordinator.execute(run.id, provider)
    _, persisted = await store.find_run_any(run.id)
    assert provider.calls == 0
    assert persisted.status is RunStatus.FAILED
    assert persisted.error is not None
    assert persisted.error.code in {"PERSISTENCE_ERROR", "PROMPT_PROVENANCE_MISMATCH"}


def test_compilation_resolves_the_revision_bundle_by_id() -> None:
    catalog = AgentCatalog()
    original = catalog.list_agents()[0].current_revision
    alternate_bundle_id = uuid4()
    alternate = PromptBundleRevision(
        alternate_bundle_id,
        2,
        PromptComponentRevision(uuid4(), "platform", 2, "alternate platform"),
        PromptComponentRevision(uuid4(), "governance", 2, "alternate governance"),
    )
    catalog.replace_bundles({catalog.bundle.id: catalog.bundle, alternate.id: alternate})
    custom = replace(original, id=uuid4(), prompt_bundle_revision_id=alternate_bundle_id)
    profile = catalog.list_agents()[0]
    profile.revisions.append(custom)
    profile.current_revision_id = custom.id
    compiled = catalog.compilation(custom.id)
    assert compiled.text.startswith("alternate platform\n\nalternate governance")
    assert compiled.component_revision_ids[:2] == (
        alternate.platform.id,
        alternate.governance.id,
    )


def test_legacy_run_provenance_is_omitted_from_backward_compatible_response() -> None:
    from aura_core.domains.execution.runs.dto import Run

    legacy = Run(
        conversation_id=uuid4(),
        user_message_id=uuid4(),
        agent_revision_id=uuid4(),
        model_policy_revision_id=GENERAL_POLICY_ID,
        provider="ollama",
        model_id="chat",
    )
    payload = run_payload(legacy)
    assert "personaRevisionId" not in payload
    assert "promptBundleRevisionId" not in payload
    assert "promptHash" not in payload


@pytest.mark.asyncio
async def test_create_idempotency_fingerprint_has_no_delimiter_collision() -> None:
    models = (
        ModelDescriptor("c", "C", "ollama", ("chat",)),
        ModelDescriptor("b:c", "B:C", "ollama", ("chat",)),
    )
    store = ConversationStore()
    key = str(uuid4())
    await store.create("issuer", "owner", "a:b", "c", models, key)
    with pytest.raises(IdempotencyConflict):
        await store.create("issuer", "owner", "a", "b:c", models, key)


def test_configuration_telemetry_accepts_success_and_failure_metadata_only() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    for outcome in ("ok", "error"):
        metrics.record_span(
            "aura.interaction.agent_configuration",
            "agent.configure",
            1.25,
            trace_id=trace_id,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency="postgresql",
            outcome=outcome,
            error_class="persistence" if outcome == "error" else None,
            agent_revision_id=str(uuid4()),
            agent_revision_number="2",
        )
    records = metrics.snapshot()
    assert len(records) == 2
    assert all(record.value >= 0 for record in records)
    assert metrics.stats().rejected == 0
    assert all("instructions" not in str(record) for record in records)
