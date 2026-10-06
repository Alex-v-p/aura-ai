"""Contract-level tests for versioned agent, persona, and prompt behaviour.

These tests deliberately exercise the public interaction boundaries rather than
the SQL mappings.  The production adapter and migration tests cover storage;
this file protects the immutable configuration and conversation semantics that
must remain the same for every entrypoint.
"""

from collections.abc import AsyncIterator, Sequence
from uuid import uuid4

import pytest
from aura_core.domains.execution.runs.public import RunCoordinator, RunStatus
from aura_core.domains.interaction.agents.public import (
    GENERAL_REVISION_ID,
    AgentCatalog,
    AgentRevision,
    ConfigurationDisabled,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    PromptBundleRevision,
    PromptCompiler,
    PromptComponentRevision,
)
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    AgentUnavailable,
    AssignmentReason,
    ConversationNotFound,
    ConversationStore,
    MessageState,
)
from aura_core.domains.interaction.personas.public import PersonaCatalog
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
from aura_core.runtime.streaming.publisher import EventPublisher


class PromptCaptureProvider:
    def __init__(self) -> None:
        self.messages: tuple[ChatMessage, ...] = ()

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "chat")

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        assert model_id == "chat"
        self.messages = tuple(messages)
        yield "captured"


def test_seeded_configuration_is_deterministic_and_revisioned() -> None:
    first_personas = PersonaCatalog()
    second_personas = PersonaCatalog()
    first = AgentCatalog(first_personas)
    second = AgentCatalog(second_personas)

    first_agent = first.list_agents()[0]
    second_agent = second.list_agents()[0]
    first_persona = first_personas.list_personas()[0]
    second_persona = second_personas.list_personas()[0]

    assert first_agent.display_name == second_agent.display_name == "Aura"
    assert first_agent.current_revision.revision == second_agent.current_revision.revision == 1
    assert first_agent.current_revision_id == second_agent.current_revision_id
    assert first_persona.display_name == second_persona.display_name == "Neutral"
    assert first_persona.current_revision.revision == second_persona.current_revision.revision == 1
    assert first_persona.current_revision_id == second_persona.current_revision_id


def test_revisions_are_sequenced_immutably_and_require_expected_version() -> None:
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    profile = catalog.list_agents()[0]
    original = profile.current_revision
    persona_revision = personas.list_personas()[0].current_revision

    created = catalog.revise_agent(
        profile.id,
        profile.version,
        "A research assistant.",
        "Cite sources and distinguish facts from hypotheses.",
        persona_revision.id,
    )

    assert original.revision == 1
    assert created.revision == 2
    assert original.instructions != created.instructions
    assert profile.current_revision_id == created.id
    with pytest.raises(ConfigurationVersionConflict):
        catalog.revise_agent(
            profile.id,
            profile.version - 1,
            "stale",
            "stale",
            persona_revision.id,
        )


def test_persona_revision_does_not_rewrite_existing_agent_revision() -> None:
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    persona = personas.list_personas()[0]
    agent = catalog.list_agents()[0]
    original = agent.current_revision

    changed_persona = personas.revise_persona(
        "issuer",
        "owner",
        persona.id,
        persona.version,
        persona.display_name,
        "A terse style.",
        "Use short, direct sentences.",
        str(uuid4()),
    )

    assert original.persona_revision_id != changed_persona.current_revision.id
    assert agent.current_revision_id == original.id
    assert catalog.resolve_revision(original.id).persona_revision_id == persona.revisions[0].id


def test_disabled_profile_rejects_new_revision_resolution_but_keeps_history() -> None:
    catalog = AgentCatalog()
    profile = catalog.list_agents()[0]
    historical = profile.current_revision

    catalog.set_agent_status(profile.id, profile.version, ConfigurationStatus.DISABLED)

    assert catalog.resolve_revision_unchecked(historical.id) is historical
    with pytest.raises(ConfigurationDisabled):
        catalog.resolve_revision(historical.id)


def test_prompt_compilation_order_and_hash_provenance_are_stable() -> None:
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    agent = catalog.list_agents()[0].current_revision
    persona = personas.list_personas()[0].current_revision
    bundle = PromptBundleRevision(
        uuid4(),
        7,
        PromptComponentRevision(uuid4(), "platform", 4, "PLATFORM"),
        PromptComponentRevision(uuid4(), "governance", 9, "GOVERNANCE"),
    )
    compiler = PromptCompiler(bundle)
    compiled = compiler.compile(
        AgentRevision(
            agent.id,
            agent.profile_id,
            agent.revision,
            agent.display_name,
            "AGENT PURPOSE",
            "AGENT INSTRUCTIONS",
            persona.id,
            bundle.id,
            agent.model_policy_revision_id,
            agent.system_prompt,
        ),
        persona,
    )
    again = compiler.compile(
        AgentRevision(
            agent.id,
            agent.profile_id,
            agent.revision,
            agent.display_name,
            "AGENT PURPOSE",
            "AGENT INSTRUCTIONS",
            persona.id,
            bundle.id,
            agent.model_policy_revision_id,
            agent.system_prompt,
        ),
        persona,
    )

    assert compiled.text.split("\n\n")[:4] == [
        "PLATFORM",
        "GOVERNANCE",
        "AGENT PURPOSE\nAGENT INSTRUCTIONS",
        persona.instructions,
    ]
    assert compiled.component_count == 4
    assert compiled.component_revision_ids == (
        bundle.platform.id,
        bundle.governance.id,
        agent.id,
        persona.id,
    )
    assert compiled.prompt_hash == again.prompt_hash
    changed = compiler.compile(
        AgentRevision(
            agent.id,
            agent.profile_id,
            agent.revision,
            agent.display_name,
            "CHANGED PURPOSE",
            "AGENT INSTRUCTIONS",
            persona.id,
            bundle.id,
            agent.model_policy_revision_id,
            agent.system_prompt,
        ),
        persona,
    )
    assert changed.prompt_hash != compiled.prompt_hash


@pytest.mark.asyncio
async def test_run_provider_receives_compiled_prompt_matching_persisted_hash() -> None:
    provider = PromptCaptureProvider()
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    conversation, _, first_run = await store.create(
        "https://issuer", "owner", "first question", "chat", models, str(uuid4())
    )
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())

    await coordinator.execute(first_run.id, provider)
    first_revision = catalog.resolve_revision(first_run.agent_revision_id)
    first_compilation = catalog.compilation(first_revision.id)
    first_completed = (await store.get(conversation.id, "owner", "https://issuer")).runs[0]
    assert provider.messages[0].role == "system"
    assert provider.messages[0].content == first_compilation.text
    assert first_completed.prompt_hash == first_compilation.prompt_hash

    custom = catalog.create_agent(
        "researcher", "Researcher", "Research carefully.", "Cite sources."
    ).current_revision
    switched = await store.switch_agent(
        conversation.id,
        "owner",
        custom.id,
        conversation.version,
        str(uuid4()),
        "https://issuer",
        confirmation=True,
    )
    _, _, second_run = await store.add_run(
        conversation.id,
        "owner",
        "second question",
        switched.version,
        str(uuid4()),
        "https://issuer",
    )
    await coordinator.execute(second_run.id, provider)
    second_compilation = catalog.compilation(custom.id)
    second_completed = (await store.get(conversation.id, "owner", "https://issuer")).runs[-1]
    assert provider.messages[0].role == "system"
    assert provider.messages[0].content == second_compilation.text
    assert second_completed.prompt_hash == second_compilation.prompt_hash
    assert second_completed.prompt_hash != first_completed.prompt_hash


@pytest.mark.asyncio
async def test_conversation_assignments_switch_only_when_idle_and_preserve_context() -> None:
    provider = FakeChatModel(("completed",))
    models = await provider.list_models()
    personas = PersonaCatalog()
    catalog = AgentCatalog(personas)
    store = ConversationStore(agents=catalog)
    original = catalog.list_agents()[0].current_revision
    profile = catalog.list_agents()[0]
    replacement = catalog.revise_agent(
        profile.id,
        profile.version,
        "Replacement purpose",
        "Replacement instructions",
        personas.list_personas()[0].current_revision.id,
    )

    conversation, _, first_run = await store.create(
        "https://issuer",
        "owner",
        "first question",
        "fake",
        models,
        str(uuid4()),
    )
    await store.start_run(first_run.id)
    first_assistant = await store.append_assistant(first_run.id, "first answer")
    await store.finish_run(first_run.id, RunStatus.COMPLETED)

    switched = await store.switch_agent(
        conversation.id,
        "owner",
        replacement.id,
        conversation.version,
        str(uuid4()),
        "https://issuer",
        confirmation=True,
    )
    assert switched.agent_revision_id == replacement.id
    assert [item.reason for item in switched.assignments] == [
        AssignmentReason.INITIAL,
        AssignmentReason.REVISION_UPGRADE,
    ]
    assert switched.assignments[-1].effective_after_message_id == first_assistant.id
    context = await store.context(conversation.id, "owner", "https://issuer")
    assert ("user", "first question") in context
    assert ("assistant", "first answer") in context

    second = await store.add_run(
        conversation.id,
        "owner",
        "second question",
        switched.version,
        str(uuid4()),
        "https://issuer",
    )
    with pytest.raises(ActiveRunConflict):
        await store.switch_agent(
            conversation.id,
            "owner",
            original.id,
            second[0].version,
            str(uuid4()),
            "https://issuer",
            confirmation=True,
        )


@pytest.mark.asyncio
async def test_retry_is_pinned_to_original_revision_and_owner_isolation_holds() -> None:
    provider = FakeChatModel(("completed",))
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    conversation, _, original_run = await store.create(
        "https://issuer", "owner", "question", "fake", models, str(uuid4())
    )
    await store.start_run(original_run.id)
    await store.append_assistant(original_run.id, "answer")
    await store.finish_run(original_run.id, RunStatus.COMPLETED)

    with pytest.raises(ConversationNotFound):
        await store.get(conversation.id, "other-owner", "https://issuer")
    retry_conversation, _, retry = await store.retry(
        original_run.id, "owner", str(uuid4()), "https://issuer"
    )
    assert retry_conversation.id == conversation.id
    assert retry.agent_revision_id == original_run.agent_revision_id == GENERAL_REVISION_ID

    partial = await store.append_assistant(retry.id, "partial", MessageState.PARTIAL)
    assert partial.state is MessageState.PARTIAL


@pytest.mark.asyncio
async def test_historical_retry_is_rejected_when_its_original_agent_is_disabled() -> None:
    provider = FakeChatModel(("completed",))
    models = await provider.list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    conversation, _, original_run = await store.create(
        "https://issuer", "owner", "question", "fake", models, str(uuid4())
    )
    await store.start_run(original_run.id)
    await store.append_assistant(original_run.id, "answer")
    await store.finish_run(original_run.id, RunStatus.COMPLETED)
    replacement = catalog.create_agent(
        "researcher", "Researcher", "Research", "Cite sources."
    ).current_revision
    switched = await store.switch_agent(
        conversation.id,
        "owner",
        replacement.id,
        conversation.version,
        str(uuid4()),
        "https://issuer",
        confirmation=True,
    )
    assert switched.agent_revision_id == replacement.id
    original_profile = catalog.list_agents()[0]
    catalog.set_agent_status(
        original_profile.id, original_profile.version, ConfigurationStatus.DISABLED
    )
    with pytest.raises(AgentUnavailable):
        await store.retry(original_run.id, "owner", str(uuid4()), "https://issuer")


@pytest.mark.asyncio
async def test_queued_work_keeps_its_pinned_revision_after_agent_is_disabled() -> None:
    models = await FakeChatModel(("completed",)).list_models()
    catalog = AgentCatalog()
    store = ConversationStore(agents=catalog)
    _, _, queued = await store.create(
        "https://issuer", "owner", "queued", "fake", models, str(uuid4())
    )
    original_profile = catalog.list_agents()[0]
    catalog.set_agent_status(
        original_profile.id, original_profile.version, ConfigurationStatus.DISABLED
    )

    claim = await store.start_run(queued.id)
    assert claim.acquired is True
    await store.append_assistant(queued.id, "completed")
    _, finished, _ = await store.finish_run(queued.id, RunStatus.COMPLETED)
    assert finished.status is RunStatus.COMPLETED
    assert finished.agent_revision_id == GENERAL_REVISION_ID
