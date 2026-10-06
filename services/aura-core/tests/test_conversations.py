from uuid import uuid4

import pytest
from aura_core.domains.execution.runs.public import RunStatus
from aura_core.domains.interaction.conversations.public import (
    ActiveRunConflict,
    ConversationStore,
    MessageState,
    ModelUnavailable,
    VersionConflict,
)
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.capacity import estimate_tokens


@pytest.mark.asyncio
async def test_create_is_atomic_and_idempotent() -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    first = await store.create(
        "https://issuer", "owner", "Hello Aura", "fake", models, str(uuid4())
    )
    replay = await store.create("https://issuer", "owner", "Hello Aura", "fake", models, "same")
    replay_again = await store.create(
        "https://issuer", "owner", "Hello Aura", "fake", models, "same"
    )
    assert replay[0].id == replay_again[0].id
    assert first[0].title == "Hello Aura"
    assert len(first[0].messages) == 1
    assert first[2].status == RunStatus.QUEUED


@pytest.mark.asyncio
async def test_version_and_active_run_guards() -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, _ = await store.create(
        "https://issuer", "owner", "Hello", "fake", models, str(uuid4())
    )
    with pytest.raises(VersionConflict):
        await store.update_model(
            conversation.id,
            "owner",
            "fake",
            999,
            models,
            str(uuid4()),
            "https://issuer",
        )
    with pytest.raises(ActiveRunConflict):
        await store.add_run(
            conversation.id,
            "owner",
            "Again",
            conversation.version,
            str(uuid4()),
            "https://issuer",
        )
    with pytest.raises(ModelUnavailable):
        await store.update_model(
            conversation.id,
            "owner",
            "missing",
            conversation.version,
            models,
            str(uuid4()),
            "https://issuer",
        )


@pytest.mark.asyncio
async def test_context_excludes_partial_and_failed_assistant_output() -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer", "owner", "Question", "fake", models, str(uuid4())
    )
    await store.start_run(run.id)
    await store.append_assistant(run.id, "partial secret", MessageState.PARTIAL)
    await store.finish_run(run.id, RunStatus.INTERRUPTED)
    context = await store.context(conversation.id, "owner", "https://issuer")
    assert all("partial secret" not in content for _, content in context)
    assert any(content == "Question" for _, content in context)


@pytest.mark.asyncio
async def test_context_budgets_completed_turns_atomically_and_keeps_current_prompt() -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    prior_user = "paired user content that is deliberately too large"
    prior_assistant = "ok"
    current_prompt = "current question"
    conversation, _, prior_run = await store.create(
        "https://issuer", "owner", prior_user, "fake", models, str(uuid4())
    )
    await store.start_run(prior_run.id)
    await store.append_assistant(prior_run.id, prior_assistant)
    await store.finish_run(prior_run.id, RunStatus.COMPLETED)
    compiled_prompt = store.agents.compilation(prior_run.agent_revision_id)
    assert prior_run.prompt_hash == compiled_prompt.prompt_hash
    await store.add_run(
        conversation.id,
        "owner",
        current_prompt,
        conversation.version,
        str(uuid4()),
        "https://issuer",
    )
    budget = (
        estimate_tokens(compiled_prompt.text)
        + estimate_tokens(current_prompt)
        + estimate_tokens(prior_assistant)
    )

    context = await store.context(
        conversation.id, "owner", "https://issuer", budget
    )

    assert context == [
        ("system", compiled_prompt.text),
        ("user", current_prompt),
    ]
    assert sum(content == current_prompt for _, content in context) == 1
