"""Contract-level tests for versioned agent, persona, and prompt behaviour.

These tests deliberately exercise the public interaction boundaries rather than
the SQL mappings.  The production adapter and migration tests cover storage;
this file protects the immutable configuration and conversation semantics that
must remain the same for every entrypoint.
"""

import os
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from typing import Any, cast
from uuid import uuid4

import pytest
import pytest_asyncio
from aura_core.domains.execution.runs.public import RunCoordinator, RunStatus
from aura_core.domains.interaction.agents.adapters.sql_store import SqlAgentStore
from aura_core.domains.interaction.agents.public import (
    AgentCatalog,
    AgentConfigurationService,
    AgentMemoryRepository,
    AgentRevision,
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    InitialMemoryPolicy,
    MemoryPolicy,
    MemoryRecallMode,
    PromptBundleRevision,
    PromptCompiler,
    PromptComponentRevision,
    ToolGrant,
    ToolGrantPolicy,
    ToolPolicySnapshot,
)
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    AgentUnavailable,
    AssignmentReason,
    ConversationNotFound,
    ConversationStore,
    MessageState,
)
from aura_core.domains.interaction.personas.public import (
    PersonaCatalog,
    PersonaConfigurationService,
    PersonaMemoryRepository,
)
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
from aura_core.runtime.streaming.publisher import EventPublisher
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import AsyncSession


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


@pytest_asyncio.fixture
async def sql_agent_store() -> AsyncIterator[tuple[SqlAgentStore, str, str]]:
    """Opt-in production-store fixture; local unit runs remain dependency-free."""

    database_url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not database_url or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip("set isolated AURA_TEST_DATABASE_URL for SQL agent coverage")
    try:
        engine = make_engine(database_url)
    except ModuleNotFoundError as exc:
        pytest.skip(f"PostgreSQL async driver unavailable: {exc.name}")
    sessions = session_factory(engine)
    issuer = f"https://aura-agent-test-{uuid4().hex}.example"
    subject = f"owner-{uuid4().hex}"
    schema_available = False
    try:
        try:
            async with engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM agent_profiles LIMIT 1"))
        except (DBAPIError, OperationalError) as exc:
            pytest.skip(f"PostgreSQL agent schema unavailable: {type(exc).__name__}")
        schema_available = True
        yield SqlAgentStore(sessions, AgentCatalog()), issuer, subject
    finally:
        if schema_available:
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "DELETE FROM tool_policy_grants WHERE policy_id IN "
                        "(SELECT id FROM tool_policy_revisions WHERE principal_issuer LIKE :issuer)"
                    ),
                    {"issuer": issuer + "%"},
                )
                await session.execute(
                    text(
                        "DELETE FROM memory_policy_fallback_grants WHERE policy_id IN "
                        "(SELECT id FROM memory_policy_revisions "
                        "WHERE principal_issuer LIKE :issuer)"
                    ),
                    {"issuer": issuer + "%"},
                )
                await session.execute(
                    text(
                        "DELETE FROM agent_revisions WHERE agent_profile_id IN "
                        "(SELECT id FROM agent_profiles WHERE slug LIKE :slug)"
                    ),
                    {"slug": "aura-agent-test-%"},
                )
                await session.execute(
                    text("DELETE FROM tool_policy_revisions WHERE principal_issuer LIKE :issuer"),
                    {"issuer": issuer + "%"},
                )
                await session.execute(
                    text("DELETE FROM memory_policy_revisions WHERE principal_issuer LIKE :issuer"),
                    {"issuer": issuer + "%"},
                )
                await session.execute(
                    text("DELETE FROM agent_profiles WHERE slug LIKE :slug"),
                    {"slug": "aura-agent-test-%"},
                )
                await session.execute(
                    text("DELETE FROM command_idempotency WHERE principal_issuer LIKE :issuer"),
                    {"issuer": issuer + "%"},
                )
                await session.execute(
                    text(
                        "DELETE FROM audit_log WHERE principal_id IN "
                        "(SELECT id FROM principals WHERE issuer LIKE :issuer)"
                    ),
                    {"issuer": issuer + "%"},
                )
                await session.execute(
                    text("DELETE FROM principals WHERE issuer LIKE :issuer"),
                    {"issuer": issuer + "%"},
                )
        await engine.dispose()


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
    assert first_agent.current_revision.revision == second_agent.current_revision.revision == 2
    assert first_agent.revisions[0].revision == second_agent.revisions[0].revision == 1
    assert first_agent.current_revision_id == second_agent.current_revision_id
    assert first_persona.display_name == second_persona.display_name == "Neutral"
    assert first_persona.current_revision.revision == second_persona.current_revision.revision == 1
    assert first_persona.current_revision_id == second_persona.current_revision_id


def test_built_in_aura_promotes_personal_memory_but_custom_agents_default_off() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    assert catalog.memory_policies[
        aura.current_revision.memory_policy_revision_id
    ].allow_shared_user_promotion

    custom = catalog.create_agent(
        "custom-agent",
        "Custom",
        "A custom assistant.",
        "Be helpful.",
    )
    assert not catalog.memory_policies[
        custom.current_revision.memory_policy_revision_id
    ].allow_shared_user_promotion


def test_built_in_aura_has_exact_memory_and_reminder_grants_and_custom_agents_deny() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    aura_policy = catalog.get_tool_policy(aura.current_revision.tool_policy_revision_id)
    assert set(aura_policy.grants) == {
        ToolGrant("memory.search", 1),
        ToolGrant("reminder.create", 1),
    }
    assert aura_policy.max_calls_per_run == 1
    assert aura_policy.timeout_ms == 10_000
    assert aura_policy.retry_limit == 0

    custom = catalog.create_agent(
        "custom-agent",
        "Custom",
        "A custom assistant.",
        "Be helpful.",
    )
    custom_policy = catalog.get_tool_policy(custom.current_revision.tool_policy_revision_id)
    assert custom_policy.grants == ()
    assert custom_policy.max_calls_per_run == 0


def test_initial_agent_policies_are_published_together_and_server_derive_limits() -> None:
    catalog = AgentCatalog()
    persona = catalog.list_agents()[0].current_revision.persona_revision_id
    initial_grants = (ToolGrant("reminder.create", 1),)
    initial_tool_policy = ToolPolicySnapshot(
        grants=(
            ToolGrantPolicy(
                grant=initial_grants[0],
                permission_scope="reminders:write",
                confirmation="always",
                timeout_ms=10_000,
                retry_limit=0,
            ),
        ),
        permission_scopes=frozenset({"reminders:write"}),
        max_calls_per_run=1,
        timeout_ms=10_000,
        retry_limit=0,
    )
    created = catalog.create_agent(
        "configured-agent",
        "Configured",
        "A configured assistant.",
        "Be useful.",
        persona_revision_id=persona,
        idempotency_key="initial-policies",
        initial_tool_grants=initial_grants,
        initial_tool_policy=initial_tool_policy,
        initial_memory_policy=InitialMemoryPolicy(
            shared_user_read=True,
            current_agent_read=True,
            allow_shared_user_promotion=False,
            fallback_relevance_threshold=0.6,
            max_memories=4,
            context_budget_fraction=0.1,
        ),
    )
    revision = created.current_revision
    memory = catalog.memory_policies[revision.memory_policy_revision_id]
    tools = catalog.tool_policies[revision.tool_policy_revision_id]
    assert memory.max_memories == 4
    assert tools.permission_scopes == frozenset({"reminders:write"})
    assert tools.timeout_ms == 10_000
    assert tools.max_calls_per_run == 1
    assert tools.retry_limit == 0
    assert catalog.create_agent(
        "configured-agent",
        "Configured",
        "A configured assistant.",
        "Be useful.",
        persona_revision_id=persona,
        idempotency_key="initial-policies",
        initial_tool_grants=(ToolGrant("reminder.create", 1),),
        initial_memory_policy=InitialMemoryPolicy(
            shared_user_read=True,
            current_agent_read=True,
            allow_shared_user_promotion=False,
            fallback_relevance_threshold=0.6,
            max_memories=4,
            context_budget_fraction=0.1,
        ),
    ).id == created.id

    with pytest.raises(ConfigurationIdempotencyConflict):
        catalog.create_agent(
            "configured-agent",
            "Configured",
            "A configured assistant.",
            "Changed.",
            persona_revision_id=persona,
            idempotency_key="initial-policies",
            initial_tool_grants=(ToolGrant("reminder.create", 1),),
        )


def test_existing_agent_revisions_keep_their_pinned_policy_when_aura_successor_is_seeded() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    original_revision = aura.current_revision
    original_policy = catalog.get_tool_policy(original_revision.tool_policy_revision_id)
    successor = catalog.create_tool_policy(
        aura.id,
        (
            ToolGrant("memory.search", 1),
            ToolGrant("reminder.create", 1),
        ),
        expected_revision=original_policy.revision,
    )
    revised = catalog.attach_tool_policy(aura.id, successor.id, aura.version)
    assert revised.current_revision_id != original_revision.id
    assert any(
        item.id == original_revision.id
        and item.tool_policy_revision_id == original_revision.tool_policy_revision_id
        for item in revised.revisions
    )
    assert revised.current_revision.tool_policy_revision_id == successor.id


def test_tool_policy_is_immutable_versioned_and_agent_owned() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    original = aura.current_revision

    policy = catalog.create_tool_policy(
        aura.id,
        (ToolGrant("memory.search", 1),),
        expected_revision=2,
        idempotency_key="tool-policy-r2",
    )
    assert policy.revision == 3
    assert catalog.get_tool_policy(original.tool_policy_revision_id).revision == 2

    updated = catalog.attach_tool_policy(
        aura.id, policy.id, aura.version, key="tool-policy-attach-r2"
    )
    assert updated.version == aura.version + 1
    assert updated.current_revision.tool_policy_revision_id == policy.id
    assert any(
        item.id == original.id
        and item.tool_policy_revision_id == original.tool_policy_revision_id
        for item in updated.revisions
    )


def test_tool_policy_rejects_unknown_grants_and_stale_revision() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    with pytest.raises(ValueError, match="unknown tool grant"):
        catalog.create_tool_policy(aura.id, (ToolGrant("shell.exec", 1),))
    with pytest.raises(ConfigurationVersionConflict):
        catalog.create_tool_policy(
            aura.id,
            (ToolGrant("memory.search", 1),),
            expected_revision=0,
        )


def test_tool_policy_cannot_be_attached_across_agent_ownership() -> None:
    catalog = AgentCatalog()
    aura = catalog.list_agents()[0]
    custom = catalog.create_agent("custom-agent", "Custom", "purpose", "instructions")
    policy = catalog.create_tool_policy(custom.id, (ToolGrant("memory.search", 1),))

    with pytest.raises(ConfigurationNotFound):
        catalog.attach_tool_policy(aura.id, policy.id, aura.version)


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

    assert original.revision == 2
    assert created.revision == 3
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


@pytest.mark.asyncio
async def test_memory_policy_idempotency_fingerprint_includes_recall_controls() -> None:
    catalog = AgentCatalog()
    repository = AgentMemoryRepository(catalog)
    profile = catalog.list_agents()[0]
    policy = MemoryPolicy(
        uuid4(),
        profile.id,
        2,
        max_memories=2,
        context_budget_fraction=0.05,
        recall_mode=MemoryRecallMode.AUTOMATIC,
        automatic_recall_threshold=0.70,
    )

    created = await repository.create_memory_policy("issuer", "owner", policy, "same-key")
    assert created is policy

    with pytest.raises(ConfigurationIdempotencyConflict):
        await repository.create_memory_policy(
            "issuer",
            "owner",
            replace(
                policy,
                id=uuid4(),
                recall_mode=MemoryRecallMode.OFF,
                automatic_recall_threshold=0.85,
            ),
            "same-key",
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
    assert retry.agent_revision_id == original_run.agent_revision_id
    assert retry.memory_policy_revision_id == original_run.memory_policy_revision_id

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
    assert finished.agent_revision_id == queued.agent_revision_id


class _SqlToolRegistry:
    """Minimal server-side registry seam used by the production service test."""

    def resolve(self, grants: Sequence[ToolGrant]) -> ToolPolicySnapshot:
        if any(grant != ToolGrant("memory.search", 1) for grant in grants):
            raise ValueError("unknown tool grant")
        return ToolPolicySnapshot(
            grants=tuple(
                ToolGrantPolicy(grant, "memory:read", "never", 30_000, 0)
                for grant in grants
            ),
            permission_scopes=frozenset({"memory:read"}),
            max_calls_per_run=1 if grants else 0,
            timeout_ms=30_000,
            retry_limit=0,
        )


def _sql_agent_service(store: SqlAgentStore) -> AgentConfigurationService:
    catalog = store.catalog
    personas = PersonaConfigurationService(
        PersonaMemoryRepository(cast(PersonaCatalog, catalog.persona_query))
    )
    return AgentConfigurationService(
        store,
        personas,
        tool_grant_resolver=_SqlToolRegistry(),
    )


async def _sql_counts(
    store: SqlAgentStore, issuer: str, subject: str, slug: str
) -> tuple[int, ...]:
    async with store.sessions() as session:
        values: list[int] = []
        for query, params in (
            (
                "SELECT count(*) FROM agent_profiles WHERE slug=:slug",
                {"slug": slug},
            ),
            (
                "SELECT count(*) FROM agent_revisions WHERE agent_profile_id IN "
                "(SELECT id FROM agent_profiles WHERE slug=:slug)",
                {"slug": slug},
            ),
            (
                "SELECT count(*) FROM memory_policy_revisions "
                "WHERE principal_issuer=:issuer AND principal_subject=:subject",
                {"issuer": issuer, "subject": subject},
            ),
            (
                "SELECT count(*) FROM memory_policy_fallback_grants WHERE policy_id IN "
                "(SELECT id FROM memory_policy_revisions WHERE principal_issuer=:issuer "
                "AND principal_subject=:subject)",
                {"issuer": issuer, "subject": subject},
            ),
            (
                "SELECT count(*) FROM tool_policy_revisions "
                "WHERE principal_issuer=:issuer AND principal_subject=:subject",
                {"issuer": issuer, "subject": subject},
            ),
            (
                "SELECT count(*) FROM tool_policy_grants WHERE policy_id IN "
                "(SELECT id FROM tool_policy_revisions WHERE principal_issuer=:issuer "
                "AND principal_subject=:subject)",
                {"issuer": issuer, "subject": subject},
            ),
            (
                "SELECT count(*) FROM command_idempotency "
                "WHERE principal_issuer=:issuer AND principal_subject=:subject",
                {"issuer": issuer, "subject": subject},
            ),
            (
                "SELECT count(*) FROM audit_log WHERE principal_id IN "
                "(SELECT id FROM principals WHERE issuer=:issuer AND subject=:subject)",
                {"issuer": issuer, "subject": subject},
            ),
        ):
            values.append(int((await session.execute(text(query), params)).scalar_one()))
        return tuple(values)


@pytest.mark.asyncio
async def test_sql_agent_create_persists_initial_policies_and_replays_atomically(
    sql_agent_store: tuple[SqlAgentStore, str, str],
) -> None:
    store, issuer, subject = sql_agent_store
    service = _sql_agent_service(store)
    default_slug = f"aura-agent-test-default-{uuid4().hex}"
    custom_slug = f"aura-agent-test-custom-{uuid4().hex}"
    default = await store.create_agent(
        issuer, subject, default_slug, "Default", "Purpose", "Instructions",
        store.catalog.persona_query.list_personas()[0].current_revision.id, str(uuid4())
    )
    default_memory = await store.get_memory_policy(
        issuer, subject, default.current_revision.memory_policy_revision_id
    )
    default_tools = await store.get_tool_policy(
        issuer, subject, default.current_revision.tool_policy_revision_id
    )
    assert (default_memory.max_memories, default_memory.context_budget_fraction) == (2, 0.05)
    assert default_memory.recall_mode is MemoryRecallMode.OFF
    assert (default_tools.grants, default_tools.max_calls_per_run) == ((), 0)

    memory = InitialMemoryPolicy(
        shared_user_read=True,
        current_agent_read=False,
        allow_shared_user_promotion=True,
        fallback_relevance_threshold=0.8,
        max_memories=4,
        context_budget_fraction=0.1,
        recall_mode=MemoryRecallMode.AUTOMATIC,
        automatic_recall_threshold=0.85,
    )
    grant = ToolGrant("memory.search", 1)
    key = str(uuid4())
    created = await service.create_agent(
        issuer,
        subject,
        custom_slug,
        "Custom",
        "Purpose",
        "Instructions",
        store.catalog.persona_query.list_personas()[0].current_revision.id,
        key,
        initial_memory_policy=memory,
        initial_tool_grants=(grant,),
    )
    replay = await service.create_agent(
        issuer,
        subject,
        custom_slug,
        "Custom",
        "Purpose",
        "Instructions",
        store.catalog.persona_query.list_personas()[0].current_revision.id,
        key,
        initial_memory_policy=memory,
        initial_tool_grants=(grant,),
    )
    assert replay.id == created.id
    with pytest.raises(ConfigurationIdempotencyConflict):
        await service.create_agent(
            issuer,
            subject,
            custom_slug,
            "Custom",
            "Changed purpose",
            "Instructions",
            store.catalog.persona_query.list_personas()[0].current_revision.id,
            key,
            initial_memory_policy=memory,
            initial_tool_grants=(grant,),
        )
    persisted_memory = await store.get_memory_policy(
        issuer, subject, created.current_revision.memory_policy_revision_id
    )
    persisted_tools = await store.get_tool_policy(
        issuer, subject, created.current_revision.tool_policy_revision_id
    )
    assert persisted_memory.max_memories == 4
    assert persisted_memory.current_agent_read is False
    assert persisted_memory.automatic_recall_threshold == 0.85
    assert persisted_tools.grants == (grant,)
    assert persisted_tools.permission_scopes == frozenset({"memory:read"})
    assert (persisted_tools.max_calls_per_run, persisted_tools.timeout_ms) == (1, 30_000)

    with pytest.raises(ValueError, match="unknown tool grant"):
        await service.create_agent(
            issuer,
            subject,
            f"aura-agent-test-unknown-{uuid4().hex}",
            "Unknown",
            "Purpose",
            "Instructions",
            store.catalog.persona_query.list_personas()[0].current_revision.id,
            str(uuid4()),
            initial_tool_grants=(ToolGrant("unknown.tool", 1),),
        )

    foreign_issuer = issuer + "/foreign"
    foreign = await store.create_agent(
        foreign_issuer,
        "foreign-owner",
        f"aura-agent-test-foreign-{uuid4().hex}",
        "Foreign",
        "Purpose",
        "Instructions",
        store.catalog.persona_query.list_personas()[0].current_revision.id,
        str(uuid4()),
    )
    with pytest.raises(ConfigurationNotFound):
        await service.create_agent(
            issuer,
            subject,
            f"aura-agent-test-fallback-{uuid4().hex}",
            "Fallback",
            "Purpose",
            "Instructions",
            store.catalog.persona_query.list_personas()[0].current_revision.id,
            str(uuid4()),
            initial_memory_policy=replace(
                memory, fallback_agent_profile_ids=(foreign.id,)
            ),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_stage",
    [
        "MemoryPolicyRevisionRow",
        "MemoryPolicyFallbackGrantRow",
        "ToolPolicyRevisionRow",
        "ToolPolicyGrantRow",
        "AgentProfileRow",
        "AgentRevisionRow",
        "idempotency",
        "audit",
    ],
)
async def test_sql_agent_create_rolls_back_after_every_staged_write(
    sql_agent_store: tuple[SqlAgentStore, str, str],
    failure_stage: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, issuer, subject = sql_agent_store
    service = _sql_agent_service(store)
    persona_revision_id = store.catalog.persona_query.list_personas()[0].current_revision.id
    baseline = await store.create_agent(
        issuer,
        subject,
        f"aura-agent-test-baseline-{uuid4().hex}",
        "Baseline",
        "Purpose",
        "Instructions",
        persona_revision_id,
        str(uuid4()),
    )
    slug = f"aura-agent-test-failure-{failure_stage}-{uuid4().hex}"
    before = await _sql_counts(store, issuer, subject, slug)
    memory = InitialMemoryPolicy(
        True,
        False,
        False,
        0.8,
        4,
        0.1,
        fallback_agent_profile_ids=(baseline.id,),
    )
    grant = ToolGrant("memory.search", 1)
    original_add = AsyncSession.add
    if failure_stage != "idempotency":
        def fail_add(self: AsyncSession, instance: object, _warn: bool = True) -> None:
            original_add(self, instance, _warn=_warn)
            if instance.__class__.__name__ == failure_stage:
                raise RuntimeError(f"injected failure at {failure_stage}")

        monkeypatch.setattr(AsyncSession, "add", fail_add)
    else:
        original_record = cast(Any, store)._record

        async def fail_record(*args: Any, **kwargs: Any) -> None:
            await original_record(*args, **kwargs)
            raise RuntimeError("injected failure at idempotency")

        monkeypatch.setattr(store, "_record", fail_record)
    if failure_stage == "audit":
        original_stage_audit = cast(Any, store)._stage_audit

        async def fail_audit(*args: Any, **kwargs: Any) -> None:
            await original_stage_audit(*args, **kwargs)
            raise RuntimeError("injected failure at audit")

        monkeypatch.setattr(store, "_stage_audit", fail_audit)
    with pytest.raises(RuntimeError, match="injected failure"):
        await service.create_agent(
            issuer,
            subject,
            slug,
            "Failure",
            "Purpose",
            "Instructions",
            persona_revision_id,
            str(uuid4()),
            initial_memory_policy=memory,
            initial_tool_grants=(grant,),
        )
    assert await _sql_counts(store, issuer, subject, slug) == before
