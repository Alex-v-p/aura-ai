"""Focused contract tests for AURA-0028 title generation.

These tests deliberately exercise the conversation-owned title seams rather than
provider adapters.  They protect the privacy and retry rules while allowing the
worker to choose the concrete provider orchestration.
"""

import importlib.util
import os
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
import yaml
from alembic import command
from aura_core.domains.execution.runs.dto import RunStatus
from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.execution.runs.public import RunCoordinator
from aura_core.domains.execution.runs.service import TitleSettlementRetry
from aura_core.domains.interaction.conversations.dto import (
    MessageState,
    TitleState,
)
from aura_core.domains.interaction.conversations.public import (
    ConversationStore,
    normalize_generated_title,
    pending_title_for,
    title_request_messages,
)
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.telemetry import COMPONENT_VERSIONS, MetadataMetrics
from aura_core.providers.models.ollama.fake import FakeChatModel
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
from aura_core.runtime.streaming.publisher import EventPublisher
from sqlalchemy import create_engine, text

MIGRATION_DIR = Path(__file__).parents[1] / "migrations" / "versions"


def _database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL migration coverage")
    return value


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


class _TitleProvider:
    def __init__(self, title: str = "A useful generated topic", *, fail: bool = False) -> None:
        self.title = title
        self.fail = fail
        self.calls: list[tuple[str, tuple[ChatMessage, ...]]] = []
        self.title_calls: list[tuple[str, tuple[ChatMessage, ...]]] = []

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("pinned-model", "Pinned model", "fake", ("chat",)),)

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "pinned-model")

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        self.calls.append((model_id, tuple(messages)))
        if self.fail and len(self.calls) > 1:
            raise RuntimeError("provider unavailable")
        if len(self.calls) == 1:
            yield "The assistant response"
        else:
            yield self.title

    async def infer_title(self, model_id: str, messages: Sequence[ChatMessage]) -> str:
        self.title_calls.append((model_id, tuple(messages)))
        if self.fail:
            raise RuntimeError("provider unavailable")
        return self.title


class _SlowTitleProvider(_TitleProvider):
    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        self.calls.append((model_id, tuple(messages)))
        if len(self.calls) == 1:
            yield "The assistant response"
            return
        yield "unexpected second stream"

    async def infer_title(self, model_id: str, messages: Sequence[ChatMessage]) -> str:
        self.title_calls.append((model_id, tuple(messages)))
        import asyncio

        await asyncio.sleep(0.1)
        return "A late title result"


class _FailingRunProvider(_TitleProvider):
    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        self.calls.append((model_id, tuple(messages)))
        raise RuntimeError("provider unavailable")
        yield "unreachable"


class _ChatOnlyProvider:
    """A chat provider without the optional title-inference capability."""

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return (ModelDescriptor("pinned-model", "Pinned model", "fake", ("chat",)),)

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "pinned-model")

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        del messages
        assert model_id == "pinned-model"
        yield "The assistant response"


class _RecordingPublisher(EventPublisher):
    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self.order = order

    async def publish(self, event: RunEvent) -> None:
        label = event.event_type
        if event.event_type == "run.status":
            label = f"run.status:{event.data['status']}"
        self.order.append(label)
        await super().publish(event)


def test_title_normalization_is_plain_text_bounded_and_word_safe() -> None:
    assert normalize_generated_title('Title: **"Exploring local astronomy"**') == (
        "Exploring local astronomy"
    )
    assert normalize_generated_title("One two three four five six seven eight nine ten") == (
        "One two three four five six seven eight"
    )
    long_title = "A very long conversation title that should stop at a word boundary "
    normalized = normalize_generated_title(long_title)
    assert normalized is not None
    assert len(normalized) <= 72
    assert not normalized.endswith(" ")
    assert normalize_generated_title("\x00\x01\x1f") is None
    assert normalize_generated_title("Title: `\"\"`") is None
    assert normalize_generated_title("only two") is None


def test_title_request_is_neutral_and_does_not_include_agent_prompt() -> None:
    candidate = type(
        "Candidate",
        (),
        {
            "user_content": "User asks about a private topic.",
            "assistant_content": "The answer discusses that private topic.",
        },
    )()
    system, user = title_request_messages(candidate)  # type: ignore[arg-type]
    assert system[0] == "system"
    assert "Return only the title" in system[1]
    assert "agent" not in system[1].lower()
    assert "persona" not in system[1].lower()
    assert "User asks about a private topic." in user[1]
    assert "The answer discusses that private topic." in user[1]


@pytest.mark.asyncio
async def test_pending_title_uses_first_completed_exchange_and_caps_each_message() -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    user_text = "u" * 2500
    conversation, _, run = await store.create(
        "https://issuer",
        "owner",
        user_text,
        "fake",
        models,
        str(uuid4()),
    )
    await store.start_run(run.id)
    await store.append_assistant(run.id, "a" * 2500)
    await store.finish_run(run.id, RunStatus.COMPLETED)

    candidate = pending_title_for(conversation, run)
    assert candidate is not None
    assert candidate.run_id == run.id
    assert candidate.model_id == "fake"
    assert len(candidate.user_content) == 2000
    assert len(candidate.assistant_content) == 2000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "message_state"),
    [
        (RunStatus.FAILED, MessageState.FAILED),
        (RunStatus.INTERRUPTED, MessageState.INTERRUPTED),
        (RunStatus.CANCELED, MessageState.INTERRUPTED),
    ],
)
async def test_failed_interrupted_and_canceled_runs_do_not_trigger_title(
    status: RunStatus, message_state: MessageState
) -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer", "owner", "A failed first request", "fake", models, str(uuid4())
    )
    await store.start_run(run.id)
    await store.append_assistant(run.id, "partial answer")
    await store.finish_run(run.id, status)
    assistant = next(
        message
        for message in conversation.messages
        if message.run_id == run.id and message.role.value == "assistant"
    )
    assert assistant.state is message_state
    assert pending_title_for(conversation, run) is None
    assert conversation.title_state is TitleState.PENDING


@pytest.mark.asyncio
async def test_later_completed_response_is_the_only_eligible_settlement_after_failure() -> None:
    provider = FakeChatModel()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, first = await store.create(
        "https://issuer", "owner", "First attempt fails", "fake", models, str(uuid4())
    )
    await store.start_run(first.id)
    await store.append_assistant(first.id, "partial")
    await store.finish_run(first.id, RunStatus.INTERRUPTED)
    _, _, second = await store.add_run(
        conversation.id,
        "owner",
        "Second attempt completes",
        conversation.version,
        str(uuid4()),
        "https://issuer",
    )
    await store.start_run(second.id)
    await store.append_assistant(second.id, "A complete response")
    await store.finish_run(second.id, RunStatus.COMPLETED)
    assert pending_title_for(conversation, first) is None
    assert pending_title_for(conversation, second) is not None


@pytest.mark.asyncio
async def test_completed_run_uses_its_pinned_model_and_settles_without_version_bump() -> None:
    provider = _TitleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer",
        "owner",
        "Discuss nearby observatories",
        "pinned-model",
        models,
        str(uuid4()),
    )
    version_before_execution = conversation.version
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics=metrics)

    await coordinator.execute(run.id, provider)

    assert run.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.GENERATED
    assert conversation.title == "A useful generated topic"
    assert conversation.version == version_before_execution
    assert [model_id for model_id, _ in provider.calls] == ["pinned-model"]
    assert [model_id for model_id, _ in provider.title_calls] == ["pinned-model"]
    title_messages = provider.title_calls[0][1]
    assert title_messages[0].role == "system"
    assert "Return only the title" in title_messages[0].content
    assert all("agent" not in message.content.lower() for message in title_messages[:1])


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_type", ["failure", "malformed", "timeout"])
async def test_title_errors_fall_back_without_failing_run_or_retrying(
    provider_type: str,
) -> None:
    provider: _TitleProvider
    if provider_type == "failure":
        provider = _TitleProvider(fail=True)
    elif provider_type == "malformed":
        provider = _TitleProvider("two words")
    else:
        provider = _SlowTitleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer",
        "owner",
        "Fallback should remain stable",
        "pinned-model",
        models,
        str(uuid4()),
    )
    version_before_execution = conversation.version
    coordinator = RunCoordinator(
        store,
        EventPublisher(),
        InMemoryOutbox(),
        metrics=MetadataMetrics(),
        title_timeout_seconds=0.01 if provider_type == "timeout" else 10.0,
    )

    await coordinator.execute(run.id, provider)
    first_call_count = len(provider.title_calls)

    assert run.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.FALLBACK
    assert conversation.title == "Fallback should remain stable"
    assert conversation.version == version_before_execution

    # A redelivery after fallback is settled must not invoke title inference
    # again or overwrite the chosen fallback.
    await coordinator.execute(run.id, provider)
    assert len(provider.title_calls) == first_call_count
    assert conversation.title_state is TitleState.FALLBACK
    assert conversation.title == "Fallback should remain stable"


@pytest.mark.asyncio
async def test_provider_without_title_capability_settles_fallback_once() -> None:
    provider = _ChatOnlyProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer", "owner", "No title capability", "pinned-model", models, str(uuid4())
    )
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())

    await coordinator.execute(run.id, provider)

    assert run.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.FALLBACK
    assert conversation.title == "No title capability"
    assert await store.pending_title(run.id) is None

    await coordinator.execute(run.id, provider)
    assert conversation.title_state is TitleState.FALLBACK
    assert conversation.title == "No title capability"


@pytest.mark.asyncio
async def test_title_persistence_failure_keeps_completed_run_and_redelivery_retries_settlement(
) -> None:
    provider = _TitleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer", "owner", "Retry the title write", "pinned-model", models, str(uuid4())
    )
    original_settle = store.settle_title
    calls = 0

    async def fail_once(run_id: Any, title: str, state: TitleState) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("database unavailable")
        return await original_settle(run_id, title, state)

    store.settle_title = fail_once  # type: ignore[method-assign]
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())

    with pytest.raises(TitleSettlementRetry):
        await coordinator.execute(run.id, provider)

    assistant = next(
        message for message in conversation.messages if message.role.value == "assistant"
    )
    assert run.status is RunStatus.COMPLETED
    assert assistant.state is MessageState.COMPLETE
    assert conversation.title_state is TitleState.PENDING
    assert calls == 1

    await coordinator.execute(run.id, provider)
    assert run.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.GENERATED
    assert conversation.title == "A useful generated topic"
    assert calls == 2
    assert len(provider.title_calls) == 2


@pytest.mark.asyncio
async def test_missing_assistant_lookup_failure_retries_without_fallback() -> None:
    """A persistence lookup failure must not relax the assistant eligibility guard."""

    provider = _TitleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, first = await store.create(
        "https://issuer",
        "owner",
        "First response is unavailable",
        "pinned-model",
        models,
        str(uuid4()),
    )
    await store.start_run(first.id)
    # A completed run without a completed assistant message is not an eligible
    # title exchange.  This models a redelivery after persistence has committed
    # the run but before an assistant snapshot became available.
    await store.finish_run(first.id, RunStatus.COMPLETED)
    original_pending = store.pending_title
    lookup_calls = 0

    async def fail_pending_lookup(run_id: Any) -> Any:
        nonlocal lookup_calls
        lookup_calls += 1
        raise RuntimeError("database unavailable")

    store.pending_title = fail_pending_lookup  # type: ignore[method-assign]
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())

    with pytest.raises(TitleSettlementRetry):
        await coordinator.execute(first.id, provider)

    assert lookup_calls == 1
    assert first.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.PENDING
    assert len(provider.title_calls) == 0

    # A later run with a real completed assistant response remains eligible;
    # it settles normally without needing to retry the empty exchange.
    store.pending_title = original_pending  # type: ignore[method-assign]
    _, _, second = await store.add_run(
        conversation.id,
        "owner",
        "Second attempt completes",
        conversation.version,
        str(uuid4()),
        "https://issuer",
    )
    await store.start_run(second.id)
    await store.append_assistant(second.id, "A complete response")
    await store.finish_run(second.id, RunStatus.COMPLETED)

    await coordinator.execute(second.id, provider)

    assert second.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.GENERATED
    assert conversation.title == "A useful generated topic"
    assert len(provider.title_calls) == 1


@pytest.mark.asyncio
async def test_unsettled_title_records_skipped_outcomes_without_failing_run() -> None:
    provider = _TitleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer",
        "owner",
        "Conditional settlement race",
        "pinned-model",
        models,
        str(uuid4()),
    )
    await store.start_run(run.id)
    await store.append_assistant(run.id, "A complete response")
    await store.finish_run(run.id, RunStatus.COMPLETED)

    async def conditional_settlement_lost(
        run_id: Any, title: str, state: TitleState
    ) -> bool:
        del run_id, title, state
        return False

    store.settle_title = conditional_settlement_lost  # type: ignore[method-assign]
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(
        store, EventPublisher(), InMemoryOutbox(), metrics=metrics
    )

    await coordinator.execute(run.id, provider)

    assert run.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.PENDING
    persistence_spans = [
        record
        for record in metrics.snapshot()
        if record.kind == "span"
        and record.component_id == "aura.interaction.conversation_persistence"
        and dict(record.trace_attributes).get("operation") == "conversation.persist"
    ]
    assert len(persistence_spans) == 1
    assert dict(persistence_spans[0].dimensions)["outcome"] == "skipped"
    outcomes = [
        record
        for record in metrics.snapshot()
        if record.metric == "title_generation_outcome"
    ]
    assert len(outcomes) == 1
    assert dict(outcomes[0].dimensions)["outcome"] == "skipped"


@pytest.mark.asyncio
async def test_assistant_snapshot_precedes_title_settlement_and_terminal_status() -> None:
    provider = _TitleProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer", "owner", "Verify publication order", "pinned-model", models, str(uuid4())
    )
    order: list[str] = []
    publisher = _RecordingPublisher(order)
    original_settle = store.settle_title

    async def record_settlement(run_id: Any, title: str, state: TitleState) -> bool:
        order.append("title.settle")
        return await original_settle(run_id, title, state)

    store.settle_title = record_settlement  # type: ignore[method-assign]
    coordinator = RunCoordinator(store, publisher, InMemoryOutbox())

    await coordinator.execute(run.id, provider)

    assert conversation.title_state is TitleState.GENERATED
    assert order.index("assistant.snapshot") < order.index("title.settle")
    assert order.index("title.settle") < order.index("run.status:completed")


@pytest.mark.asyncio
async def test_oversized_title_output_is_rejected_without_leak_or_redelivery_retry() -> None:
    oversized_title = "oversized-title-content " * 10_000
    provider = _TitleProvider(oversized_title)
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer",
        "owner",
        "Deterministic fallback topic",
        "pinned-model",
        models,
        str(uuid4()),
    )
    metrics = MetadataMetrics()
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox(), metrics=metrics)

    await coordinator.execute(run.id, provider)

    assert run.status is RunStatus.COMPLETED
    assert conversation.title_state is TitleState.FALLBACK
    assert conversation.title == "Deterministic fallback topic"
    assert len(provider.title_calls) == 1
    assert conversation.version == 1

    # A redelivered terminal run must not invoke the oversized title request a
    # second time or replace the deterministic fallback.
    await coordinator.execute(run.id, provider)
    assert len(provider.title_calls) == 1
    assert conversation.title == "Deterministic fallback topic"

    rendered = repr(list(metrics.snapshot()))
    assert oversized_title not in rendered
    assert "oversized-title-content" not in rendered
    assert "title_output_size" in rendered


@pytest.mark.asyncio
async def test_failed_assistant_run_never_invokes_title_inference() -> None:
    provider = _FailingRunProvider()
    models = await provider.list_models()
    store = ConversationStore()
    conversation, _, run = await store.create(
        "https://issuer", "owner", "Provider failure", "pinned-model", models, str(uuid4())
    )
    coordinator = RunCoordinator(store, EventPublisher(), InMemoryOutbox())

    await coordinator.execute(run.id, provider)

    assert run.status is RunStatus.FAILED
    assert conversation.title_state is TitleState.PENDING
    assert len(provider.calls) == 1


@pytest.mark.asyncio
async def test_title_telemetry_contains_sizes_and_identifiers_but_no_private_text() -> None:
    private_user = "PRIVATE_USER_TRANSCRIPT"
    private_assistant = "PRIVATE_ASSISTANT_RESPONSE"
    private_title = "PRIVATE GENERATED TITLE"
    provider = _TitleProvider(private_title)
    models = await provider.list_models()
    store = ConversationStore()
    metrics = MetadataMetrics()
    _, _, run = await store.create(
        "https://issuer", "owner", private_user, "pinned-model", models, str(uuid4())
    )
    coordinator = RunCoordinator(
        store, EventPublisher(), InMemoryOutbox(), metrics=metrics
    )

    # The provider's first response is the assistant transcript; the second is
    # title output.  The assertion below inspects only the metadata sink.
    await coordinator.execute(run.id, provider)
    records: list[object] = list(metrics.snapshot())
    rendered = repr(records)
    assert private_user not in rendered
    assert private_assistant not in rendered
    assert private_title not in rendered
    assert "title_inference_duration_ms" in rendered
    assert "title_persistence_duration_ms" in rendered
    assert "title_input_size" in rendered
    assert "title_output_size" in rendered


@pytest.mark.asyncio
async def test_sql_repository_title_lookup_and_settlement_are_conditional_without_version_bump(
) -> None:
    from aura_core.domains.interaction.conversations.persistence import ConversationRow
    from aura_core.domains.interaction.conversations.repository import SqlConversationRepository

    conversation_id = uuid4()
    row = ConversationRow(
        id=conversation_id,
        principal_id=uuid4(),
        title="Pending topic",
        title_state=TitleState.PENDING.value,
        agent_profile_id=uuid4(),
        agent_revision_id=uuid4(),
        model_id="pinned-model",
        version=4,
    )

    class Session:
        async def get(self, model: object, identifier: object, **kwargs: object) -> object:
            del model, kwargs
            return row if identifier == conversation_id else None

    repository = SqlConversationRepository()
    session: Any = Session()
    pending = await repository.pending_title(session, conversation_id)  # type: ignore[arg-type]
    assert pending is not None
    assert pending.title_state is TitleState.PENDING
    assert pending.version == 4

    assert await repository.settle_title(  # type: ignore[arg-type]
        session, conversation_id, "Generated topic", TitleState.GENERATED
    )
    assert row.title == "Generated topic"
    assert row.title_state == TitleState.GENERATED.value
    assert row.version == 4
    assert not await repository.settle_title(  # type: ignore[arg-type]
        session, conversation_id, "Replacement topic", TitleState.FALLBACK
    )
    assert row.title == "Generated topic"


def test_title_observability_manifests_keep_versions_and_content_prohibitions() -> None:
    manifest_dir = Path(__file__).parents[1] / "resources" / "component-manifests"
    required = {
        "model-inference.yaml": (
            "aura.runtime.model_inference",
            {
                "title_inference_duration_ms",
                "title_generation_outcome",
                "title_input_size",
                "title_output_size",
            },
        ),
        "conversation-persistence.yaml": (
            "aura.interaction.conversation_persistence",
            {"title_persistence_duration_ms"},
        ),
    }
    for filename, (component_id, metrics) in required.items():
        manifest = yaml.safe_load((manifest_dir / filename).read_text())
        component = manifest["component"]
        assert component["id"] == component_id
        assert component["version"] == COMPONENT_VERSIONS[component_id]
        assert metrics <= set(manifest["metrics"])
        assert manifest["capture_policy"] == "metadata_only"
        assert {"prompt", "response", "payload"} <= set(manifest["cardinality"]["prohibited"])
        assert "title_input_size" in manifest["cardinality"]["trace_only"]
        assert "title_output_size" in manifest["cardinality"]["trace_only"]


def test_title_migration_is_forward_only_and_preserves_legacy_lineage() -> None:
    paths = sorted(MIGRATION_DIR.glob("0006_*.py"))
    assert len(paths) == 1, "AURA-0028 must provide one 0006 title migration"
    path = paths[0]
    spec = importlib.util.spec_from_file_location("aura_migration_0006", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.down_revision == "0005_persona_overrides"
    assert module.revision == "0006_title_generation"
    source = path.read_text().lower()
    assert "title_state" in source
    assert "legacy" in source
    assert "pending" in source
    assert "delete from conversations" not in source
    assert "delete from messages" not in source
    assert "delete from runs" not in source
    assert "downgrade is disabled" in source or "raise runtimeerror" in source


def test_fresh_postgresql_install_has_legacy_default_for_migration_rows() -> None:
    url = _database_url("AURA_0028_MIGRATION_FRESH_DATABASE_URL")
    _upgrade(url, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            column = connection.execute(
                text(
                    "SELECT column_default, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'conversations' AND column_name = 'title_state'"
                )
            ).one()
            assert "legacy" in str(column.column_default).lower()
            assert column.is_nullable == "NO"
    finally:
        engine.dispose()


def test_upgrade_from_0005_marks_existing_conversation_legacy_without_changing_ids() -> None:
    url = _database_url("AURA_0028_MIGRATION_UPGRADE_DATABASE_URL")
    _upgrade(url, "0005_persona_overrides")
    principal_id, conversation_id = uuid4(), uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            seeded = connection.execute(
                text(
                    "SELECT p.id AS profile_id, r.id AS revision_id "
                    "FROM agent_profiles p JOIN agent_revisions r "
                    "ON r.id = p.current_revision_id "
                    "WHERE p.slug = 'general-assistant'"
                )
            ).one()
            connection.execute(
                text(
                    "INSERT INTO principals (id, issuer, subject, display_name) "
                    "VALUES (:id, 'https://issuer', :subject, 'Owner')"
                ),
                {"id": principal_id, "subject": f"title-owner-{principal_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_id, title, agent_profile_id, agent_revision_id, "
                    "model_id, version) VALUES (:id, :principal, 'Legacy topic', :profile, "
                    ":revision, 'pinned-model', 7)"
                ),
                {
                    "id": conversation_id,
                    "principal": principal_id,
                    "profile": seeded.profile_id,
                    "revision": seeded.revision_id,
                },
            )
        _upgrade(url, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, title, title_state, version FROM conversations WHERE id = :id"
                ),
                {"id": conversation_id},
            ).one()
            assert row.id == conversation_id
            assert row.title == "Legacy topic"
            assert row.title_state == "legacy"
            assert row.version == 7
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_title_state_defaults_legacy_in_persistence_and_pending_in_memory() -> None:
    from aura_core.domains.interaction.conversations.persistence import ConversationRow

    default = ConversationRow.__table__.c.title_state.server_default
    assert default is not None
    assert "legacy" in str(default.arg).lower()
    provider = FakeChatModel()
    models = await provider.list_models()
    conversation, _, _ = await ConversationStore().create(
        "https://issuer", "owner", "A new pending conversation", "fake", models, str(uuid4())
    )
    assert conversation.title_state is TitleState.PENDING
