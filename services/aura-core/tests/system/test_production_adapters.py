"""Integration coverage for Core's production persistence and transport adapters.

These tests never fall back to the in-memory composition. They run against
operator-provided disposable PostgreSQL, Valkey, and NATS endpoints only when
the corresponding ``AURA_TEST_*`` URL and isolation acknowledgement are set.
"""

import asyncio
import hashlib
import json
import os
import sys
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
import pytest_asyncio
from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.bootstrap.database import metadata
from aura_core.domains.execution.runs.events import new_event
from aura_core.domains.execution.runs.public import RunCoordinator, RunStatus
from aura_core.domains.interaction.agents.adapters import SqlAgentSeeder
from aura_core.domains.interaction.conversations.persistence import MessageRow
from aura_core.domains.interaction.conversations.public import TitleState
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import (
    MemorySessionBackend,
    Principal,
    RedisSessionBackend,
    Session,
    SessionService,
    Settings,
)
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.oidc import OIDCValidationError, exchange_code
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.platform.outbox.service import OutboxCommand
from aura_core.runtime.models.capacity import estimate_tokens
from aura_core.runtime.models.ports import ChatMessage, ModelDescriptor
from aura_core.runtime.streaming.publisher import EventPublisher
from authlib.jose import JsonWebKey, JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import update

from conftest import owner_client

pytestmark = pytest.mark.integration
MODELS = (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)


class PromptCaptureProvider:
    def __init__(self) -> None:
        self.messages: tuple[ChatMessage, ...] = ()

    async def list_models(self) -> tuple[ModelDescriptor, ...]:
        return MODELS

    async def is_ready(self, model_id: str | None = None) -> bool:
        return model_id in (None, "chat")

    async def stream_chat(
        self, model_id: str, messages: Sequence[ChatMessage]
    ) -> AsyncIterator[str]:
        assert model_id == "chat"
        self.messages = tuple(messages)
        yield "captured"


def _url(name: str) -> str | None:
    value = os.environ.get(name)
    if not value or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        return None
    return value


@pytest_asyncio.fixture
async def sql_store() -> AsyncIterator[SqlConversationStore]:
    database_url = _url("AURA_TEST_DATABASE_URL")
    if database_url is None:
        pytest.skip(
            "set AURA_TEST_DATABASE_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 "
            "for a disposable PostgreSQL database"
        )
    engine = make_engine(database_url)
    sessions = session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata().drop_all)
            await connection.run_sync(metadata().create_all)
    except Exception as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}")
    store = SqlConversationStore(sessions)
    await SqlAgentSeeder(sessions).seed()
    try:
        yield store
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata().drop_all)
        await engine.dispose()


@pytest.mark.asyncio
async def test_sql_agent_configuration_survives_fresh_app_and_idempotent_replays(
    sql_store: SqlConversationStore,
) -> None:
    """Configuration commands must use SQL persistence, not process-local state."""

    del sql_store
    database_url = _url("AURA_TEST_DATABASE_URL")
    assert database_url is not None
    settings = Settings(
        database_url=database_url,
        public_origin="https://aura.dev.example",
        oidc_issuer="https://authentik.dev.example",
        owner_subject="owner-subject",
        default_model="chat",
        secure_cookies=False,
    )
    applications: list[Any] = []

    async def fresh_app() -> Any:
        application = create_app(settings, testing=False)
        application.state.aura.sessions = SessionService(MemorySessionBackend(), settings)
        applications.append(application)
        return application

    try:
        first_app = await fresh_app()
        first_client, first_session = await owner_client(first_app)
        persona_payload = {
            "displayName": "Research style",
            "description": "Research-oriented answers.",
            "instructions": "Cite sources and separate facts from hypotheses.",
        }
        persona_key = str(uuid4())
        created_persona = await first_client.post(
            "/api/v1/personas",
            headers={"X-CSRF-Token": first_session.csrf_token, "Idempotency-Key": persona_key},
            json=persona_payload,
        )
        assert created_persona.status_code == 201
        persona = created_persona.json()
        persona_id = persona["id"]
        await first_client.aclose()

        second_app = await fresh_app()
        second_client, second_session = await owner_client(second_app)
        persisted_persona = await second_client.get(f"/api/v1/personas/{persona_id}")
        assert persisted_persona.status_code == 200
        assert persisted_persona.json() == persona
        replay_persona = await second_client.post(
            "/api/v1/personas",
            headers={"X-CSRF-Token": second_session.csrf_token, "Idempotency-Key": persona_key},
            json=persona_payload,
        )
        assert replay_persona.status_code == 201
        assert replay_persona.json() == persona
        conflict_persona = await second_client.post(
            "/api/v1/personas",
            headers={"X-CSRF-Token": second_session.csrf_token, "Idempotency-Key": persona_key},
            json={**persona_payload, "instructions": "conflicting replay"},
        )
        assert conflict_persona.status_code == 409

        persona_revision_payload = {
            "displayName": "Research style",
            "description": "Research-oriented answers with evidence.",
            "instructions": persona_payload["instructions"],
            "expectedVersion": persona["version"],
        }
        persona_revision_key = str(uuid4())
        revised_persona = await second_client.post(
            f"/api/v1/personas/{persona_id}/revisions",
            headers={
                "X-CSRF-Token": second_session.csrf_token,
                "Idempotency-Key": persona_revision_key,
            },
            json=persona_revision_payload,
        )
        assert revised_persona.status_code == 201
        persona = revised_persona.json()
        await second_client.aclose()

        third_app = await fresh_app()
        third_client, third_session = await owner_client(third_app)
        replay_revision = await third_client.post(
            f"/api/v1/personas/{persona_id}/revisions",
            headers={
                "X-CSRF-Token": third_session.csrf_token,
                "Idempotency-Key": persona_revision_key,
            },
            json=persona_revision_payload,
        )
        assert replay_revision.status_code == 201
        assert replay_revision.json() == persona
        conflict_revision = await third_client.post(
            f"/api/v1/personas/{persona_id}/revisions",
            headers={
                "X-CSRF-Token": third_session.csrf_token,
                "Idempotency-Key": persona_revision_key,
            },
            json={**persona_revision_payload, "description": "conflicting replay"},
        )
        assert conflict_revision.status_code == 409

        agent_payload = {
            "displayName": "Researcher",
            "purpose": "Research carefully.",
            "instructions": "Cite sources.",
            "personaRevisionId": persona["currentRevision"]["id"],
        }
        agent_key = str(uuid4())
        created_agent = await third_client.post(
            "/api/v1/agents",
            headers={"X-CSRF-Token": third_session.csrf_token, "Idempotency-Key": agent_key},
            json=agent_payload,
        )
        assert created_agent.status_code == 201
        agent = created_agent.json()
        agent_id = agent["id"]
        await third_client.aclose()

        fourth_app = await fresh_app()
        fourth_client, fourth_session = await owner_client(fourth_app)
        persisted_agent = await fourth_client.get(f"/api/v1/agents/{agent_id}")
        assert persisted_agent.status_code == 200
        assert persisted_agent.json() == agent
        replay_agent = await fourth_client.post(
            "/api/v1/agents",
            headers={"X-CSRF-Token": fourth_session.csrf_token, "Idempotency-Key": agent_key},
            json=agent_payload,
        )
        assert replay_agent.status_code == 201
        assert replay_agent.json() == agent
        conflict_agent = await fourth_client.post(
            "/api/v1/agents",
            headers={"X-CSRF-Token": fourth_session.csrf_token, "Idempotency-Key": agent_key},
            json={**agent_payload, "purpose": "conflicting replay"},
        )
        assert conflict_agent.status_code == 409

        agent_revision_payload = {
            "displayName": "Researcher",
            "purpose": "Research carefully and transparently.",
            "instructions": agent_payload["instructions"],
            "personaRevisionId": agent["currentRevision"]["personaRevisionId"],
            "expectedVersion": agent["version"],
        }
        agent_revision_key = str(uuid4())
        revised_agent = await fourth_client.post(
            f"/api/v1/agents/{agent_id}/revisions",
            headers={
                "X-CSRF-Token": fourth_session.csrf_token,
                "Idempotency-Key": agent_revision_key,
            },
            json=agent_revision_payload,
        )
        assert revised_agent.status_code == 201
        agent = revised_agent.json()
        await fourth_client.aclose()

        fifth_app = await fresh_app()
        fifth_client, fifth_session = await owner_client(fifth_app)
        replay_agent_revision = await fifth_client.post(
            f"/api/v1/agents/{agent_id}/revisions",
            headers={
                "X-CSRF-Token": fifth_session.csrf_token,
                "Idempotency-Key": agent_revision_key,
            },
            json=agent_revision_payload,
        )
        assert replay_agent_revision.status_code == 201
        assert replay_agent_revision.json() == agent
        conflict_agent_revision = await fifth_client.post(
            f"/api/v1/agents/{agent_id}/revisions",
            headers={
                "X-CSRF-Token": fifth_session.csrf_token,
                "Idempotency-Key": agent_revision_key,
            },
            json={**agent_revision_payload, "purpose": "conflicting replay"},
        )
        assert conflict_agent_revision.status_code == 409

        status_payload = {"status": "disabled", "expectedVersion": agent["version"]}
        status_key = str(uuid4())
        disabled_agent = await fifth_client.patch(
            f"/api/v1/agents/{agent_id}",
            headers={"X-CSRF-Token": fifth_session.csrf_token, "Idempotency-Key": status_key},
            json=status_payload,
        )
        assert disabled_agent.status_code == 200
        await fifth_client.aclose()

        sixth_app = await fresh_app()
        sixth_client, sixth_session = await owner_client(sixth_app)
        replay_status = await sixth_client.patch(
            f"/api/v1/agents/{agent_id}",
            headers={"X-CSRF-Token": sixth_session.csrf_token, "Idempotency-Key": status_key},
            json=status_payload,
        )
        assert replay_status.status_code == 200
        assert replay_status.json() == disabled_agent.json()
        conflict_status = await sixth_client.patch(
            f"/api/v1/agents/{agent_id}",
            headers={"X-CSRF-Token": sixth_session.csrf_token, "Idempotency-Key": status_key},
            json={**status_payload, "status": "active"},
        )
        assert conflict_status.status_code == 409
        await sixth_client.aclose()
    finally:
        for application in applications:
            engine = application.state.aura.engine
            if engine is not None:
                await engine.dispose()


@pytest.mark.asyncio
async def test_sql_run_sends_compiled_prompt_and_persists_matching_provenance(
    sql_store: SqlConversationStore,
) -> None:
    provider = PromptCaptureProvider()
    _, _, run = await sql_store.create(
        "https://identity.integration.example",
        "owner",
        "verify compiled prompt",
        "chat",
        MODELS,
        str(uuid4()),
    )
    coordinator = RunCoordinator(sql_store, EventPublisher(), InMemoryOutbox())
    await coordinator.execute(run.id, provider)
    _, completed = await sql_store.find_run_any(run.id)
    expected = sql_store.agents.compilation(run.agent_revision_id)

    assert provider.messages
    assert provider.messages[0].role == "system"
    assert provider.messages[0].content == expected.text
    assert completed.prompt_hash == expected.prompt_hash


@pytest_asyncio.fixture
async def valkey_client() -> AsyncIterator[Any]:
    valkey_url = _url("AURA_TEST_VALKEY_URL")
    if valkey_url is None:
        pytest.skip(
            "set AURA_TEST_VALKEY_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 "
            "for a disposable Valkey database"
        )
    import redis.asyncio

    client = redis.asyncio.from_url(  # pyright: ignore[reportUnknownMemberType]
        valkey_url, decode_responses=True
    )
    try:
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
    except Exception as exc:
        await client.aclose()
        pytest.skip(f"Valkey integration dependency unavailable: {type(exc).__name__}")
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
def nats_url() -> str:
    value = _url("AURA_TEST_NATS_URL")
    if value is None:
        pytest.skip(
            "set AURA_TEST_NATS_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 "
            "for a disposable NATS JetStream server"
        )
    return value


@pytest.mark.asyncio
async def test_sql_repository_hydrates_principal_and_paginates(
    sql_store: SqlConversationStore,
) -> None:
    issuer = "https://identity.integration.example"
    first, _, _ = await sql_store.create(issuer, "owner", "first", "chat", MODELS, str(uuid4()))
    second, _, _ = await sql_store.create(issuer, "owner", "second", "chat", MODELS, str(uuid4()))

    hydrated = await sql_store.get(first.id, "owner", issuer)
    assert hydrated.principal_issuer == issuer
    assert hydrated.principal_subject == "owner"
    # A fresh application composition must be able to hydrate the same
    # durable record; this guards restart persistence rather than only the
    # current repository instance's identity map.
    restarted_store = SqlConversationStore(sql_store.sessions)
    reloaded = await restarted_store.get(first.id, "owner", issuer)
    assert reloaded.id == first.id
    assert reloaded.messages[0].content == "first"
    page, cursor = await sql_store.list("owner", limit=1, issuer=issuer)
    assert len(page) == 1
    assert cursor is not None
    next_page, next_cursor = await sql_store.list(
        "owner", limit=1, cursor=cursor, issuer=issuer
    )
    assert {item.id for item in page + next_page} == {first.id, second.id}
    assert next_cursor is None


@pytest.mark.asyncio
async def test_sql_recent_activity_order_ignores_metadata_and_non_user_events(
    sql_store: SqlConversationStore,
) -> None:
    """SQL ordering is activity-based, owner-scoped, and cursor-stable."""

    issuer = "https://identity.integration.example"
    owner = "activity-owner"
    other_owner = "other-activity-owner"
    first, _, first_run = await sql_store.create(
        issuer, owner, "first accepted message", "chat", MODELS, str(uuid4())
    )
    second, _, _ = await sql_store.create(
        issuer, owner, "second accepted message", "chat", MODELS, str(uuid4())
    )
    other, _, _ = await sql_store.create(
        issuer, other_owner, "other owner's message", "chat", MODELS, str(uuid4())
    )

    # Force equal activity timestamps so the repository's UUID tie-break and
    # the matching opaque cursor boundary are exercised deterministically.
    equal_activity = datetime(2026, 1, 1, tzinfo=UTC)
    async with sql_store.sessions() as session, session.begin():
        await session.execute(
            update(MessageRow)
            .where(MessageRow.id.in_([first_run.user_message_id, second.messages[0].id]))
            .values(created_at=equal_activity)
        )

    expected_tie_winner = max(first.id, second.id)
    expected_tie_loser = min(first.id, second.id)
    page, cursor = await sql_store.list(owner, limit=1, issuer=issuer)
    assert [item.id for item in page] == [expected_tie_winner]
    assert cursor is not None
    owner_remainder, remainder_cursor = await sql_store.list(
        owner, limit=1, cursor=cursor, issuer=issuer
    )
    assert [item.id for item in owner_remainder] == [expected_tie_loser]
    assert remainder_cursor is None
    other_page, other_cursor = await sql_store.list(other_owner, issuer=issuer)
    assert [item.id for item in other_page] == [other.id]
    assert other_cursor is None

    async def assert_cursor_is_stable() -> None:
        current_page, current_cursor = await sql_store.list(owner, limit=1, issuer=issuer)
        assert [item.id for item in current_page] == [expected_tie_winner]
        assert current_cursor == cursor

    # Ordinary model metadata changes do not promote the tie loser.
    loaded_first = await sql_store.get(first.id, owner, issuer)
    await sql_store.update_model(
        first.id,
        owner,
        "chat",
        loaded_first.version,
        MODELS,
        str(uuid4()),
        issuer,
    )
    await assert_cursor_is_stable()

    # Completing the assistant side of the first exchange and settling its
    # generated title also leave accepted-user activity unchanged.
    await sql_store.append_assistant(first_run.id, "assistant completion")
    await sql_store.finish_run(first_run.id, RunStatus.COMPLETED)
    assert await sql_store.settle_title(first_run.id, "Generated topic", TitleState.GENERATED)
    settled = await sql_store.get(first.id, owner, issuer)
    assert settled.title == "Generated topic"
    await assert_cursor_is_stable()

    # A retry reuses the accepted user message and therefore does not promote.
    retried = await sql_store.retry(first_run.id, owner, str(uuid4()), issuer)
    await sql_store.finish_run(retried[2].id, RunStatus.FAILED)
    await assert_cursor_is_stable()

    # Only a newly accepted user message moves its target to the front.
    ready = await sql_store.get(first.id, owner, issuer)
    accepted = await sql_store.add_run(
        first.id,
        owner,
        "new accepted activity",
        ready.version,
        str(uuid4()),
        issuer,
    )
    assert accepted[1].role.value == "user"
    promoted, promoted_cursor = await sql_store.list(owner, limit=2, issuer=issuer)
    assert [item.id for item in promoted] == [first.id, second.id]
    assert promoted_cursor is None


@pytest.mark.asyncio
async def test_fresh_production_uow_atomically_creates_cross_domain_rows(
    sql_store: SqlConversationStore,
) -> None:
    del sql_store
    database_url = _url("AURA_TEST_DATABASE_URL")
    assert database_url is not None
    script = '''
import asyncio
import json
import os

from sqlalchemy import text

from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.runtime.models.ports import ModelDescriptor


async def main() -> None:
    engine = make_engine(os.environ["AURA_TEST_DATABASE_URL"])
    store = SqlConversationStore(session_factory(engine))
    models = (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)
    try:
        conversation, message, run = await store.create(
            "https://fresh-process.integration.example",
            "owner",
            "atomic cross-domain create",
            "chat",
            models,
            "fresh-production-uow",
        )
        async with engine.connect() as connection:
            counts = (
                await connection.execute(
                    text(
                        """
                        SELECT
                          (SELECT count(*) FROM conversations WHERE id = :conversation_id)
                            AS conversations,
                          (SELECT count(*) FROM messages WHERE id = :message_id) AS messages,
                          (SELECT count(*) FROM runs WHERE id = :run_id) AS runs,
                          (SELECT count(*) FROM outbox WHERE id = :run_id) AS outbox
                        """
                    ),
                    {
                        "conversation_id": conversation.id,
                        "message_id": message.id,
                        "run_id": run.id,
                    },
                )
            ).mappings().one()
        print(json.dumps(dict(counts)))
    finally:
        await engine.dispose()


asyncio.run(main())
'''
    environment = os.environ.copy()
    source_root = Path(__file__).parents[2] / "src"
    environment["PYTHONPATH"] = os.pathsep.join(
        part for part in (str(source_root), environment.get("PYTHONPATH")) if part
    )
    environment["AURA_TEST_DATABASE_URL"] = database_url
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env=environment,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()

    assert process.returncode == 0, stderr.decode()
    assert json.loads(stdout.decode().splitlines()[-1]) == {
        "conversations": 1,
        "messages": 1,
        "runs": 1,
        "outbox": 1,
    }


@pytest.mark.asyncio
async def test_sql_repository_event_round_trip_and_outbox_publish_mark(
    sql_store: SqlConversationStore,
) -> None:
    conversation, _, run = await sql_store.create(
        "https://identity.integration.example", "owner", "event", "chat", MODELS, str(uuid4())
    )
    event = new_event("run.status", run.id, conversation.id, 0, {"status": "queued"})
    await sql_store.persist_event(event)
    await sql_store.persist_event(event)
    history = await sql_store.event_history(run.id)
    assert len(history) == 1
    assert history[0].payload() == event.payload()
    commands = await sql_store.pending_commands()
    assert len(commands) == 1
    assert commands[0].run_id == run.id
    await sql_store.mark_published(commands[0].id)
    assert await sql_store.pending_commands() == []


@pytest.mark.asyncio
async def test_sql_repository_idempotency_replay_and_mismatch_are_atomic(
    sql_store: SqlConversationStore,
) -> None:
    from aura_core.domains.interaction.conversations.public import IdempotencyConflict

    key = str(uuid4())
    first = await sql_store.create("https://id", "owner", "same", "chat", MODELS, key)
    replay = await sql_store.create("https://id", "owner", "same", "chat", MODELS, key)
    assert replay[0].id == first[0].id
    with pytest.raises(IdempotencyConflict):
        await sql_store.create("https://id", "owner", "changed", "chat", MODELS, key)
    conversations, _ = await sql_store.list("owner", issuer="https://id")
    assert len(conversations) == 1


@pytest.mark.asyncio
async def test_sql_create_add_and_retry_stage_foreign_keys_in_order(
    sql_store: SqlConversationStore,
) -> None:
    issuer = "https://identity.integration.example"
    conversation, _, first_run = await sql_store.create(
        issuer, "owner", "first", "chat", MODELS, str(uuid4())
    )
    await sql_store.finish_run(first_run.id, RunStatus.COMPLETED)

    add_key = str(uuid4())
    added = await sql_store.add_run(
        conversation.id,
        "owner",
        "second",
        conversation.version,
        add_key,
        issuer,
    )
    add_replay = await sql_store.add_run(
        conversation.id,
        "owner",
        "second",
        conversation.version,
        add_key,
        issuer,
    )
    assert add_replay[2].id == added[2].id
    await sql_store.finish_run(added[2].id, RunStatus.FAILED)

    retry_key = str(uuid4())
    retried = await sql_store.retry(added[2].id, "owner", retry_key, issuer)
    retry_replay = await sql_store.retry(added[2].id, "owner", retry_key, issuer)
    assert retried[2].retry_of_run_id == added[2].id
    assert retry_replay[2].id == retried[2].id
    assert retried[1].id == added[1].id


@pytest.mark.asyncio
async def test_sql_context_never_includes_orphan_assistant_when_pair_exceeds_budget(
    sql_store: SqlConversationStore,
) -> None:
    issuer = "https://identity.integration.example"
    prior_user = "paired user content that is deliberately too large"
    prior_assistant = "ok"
    current_prompt = "current question"
    conversation, _, prior_run = await sql_store.create(
        issuer, "owner", prior_user, "chat", MODELS, str(uuid4())
    )
    await sql_store.start_run(prior_run.id)
    await sql_store.append_assistant(prior_run.id, prior_assistant)
    await sql_store.finish_run(prior_run.id, RunStatus.COMPLETED)
    compiled_prompt = sql_store.agents.compilation(prior_run.agent_revision_id)
    assert prior_run.prompt_hash == compiled_prompt.prompt_hash
    await sql_store.add_run(
        conversation.id,
        "owner",
        current_prompt,
        conversation.version,
        str(uuid4()),
        issuer,
    )
    budget = (
        estimate_tokens(compiled_prompt.text)
        + estimate_tokens(current_prompt)
        + estimate_tokens(prior_assistant)
    )

    context = await sql_store.context(conversation.id, "owner", issuer, budget)

    assert context == [
        ("system", compiled_prompt.text),
        ("user", current_prompt),
    ]


@pytest.mark.asyncio
async def test_sql_repository_concurrent_same_key_has_one_committed_conversation(
    sql_store: SqlConversationStore,
) -> None:
    key = str(uuid4())

    async def create() -> tuple[Any, ...]:
        return await sql_store.create("https://id", "owner", "concurrent", "chat", MODELS, key)

    results = await asyncio.gather(create(), create(), return_exceptions=True)
    errors = [result for result in results if isinstance(result, BaseException)]
    assert errors == []
    conversations, _ = await sql_store.list("owner", issuer="https://id")
    assert len(conversations) == 1
    assert results[0][0].id == results[1][0].id  # type: ignore[index]


@pytest.mark.asyncio
async def test_sql_run_claim_is_stable_across_redelivery(sql_store: SqlConversationStore) -> None:
    """Claims carry an attempt and expire without restarting inference."""
    _, _, run = await sql_store.create("https://id", "owner", "claim", "chat", MODELS, str(uuid4()))
    worker_id = uuid4()
    first = await sql_store.start_run(run.id, worker_id=worker_id, lease_seconds=0.01)
    assert first[1].status.value == "running"
    assert first[1].attempt_id == worker_id
    assert first[1].attempt_count == 1
    await asyncio.sleep(0.03)
    expired = await sql_store.expire_leases()
    assert [item.id for item in expired] == [run.id]
    _, interrupted = await sql_store.find_run_any(run.id)
    assert interrupted.status.value == "interrupted"
    assert interrupted.error is not None
    assert interrupted.error.code == "WORKER_LEASE_EXPIRED"
    assert interrupted.error.trace_id == run.id.hex


@pytest.mark.asyncio
async def test_redis_session_backend_round_trip_and_idle_expiry(valkey_client: Any) -> None:
    backend = RedisSessionBackend(valkey_client)
    service = SessionService(backend, Settings(oidc_issuer="https://identity.integration.example"))
    session_id, session = await service.create(
        Principal("https://identity.integration.example", "owner", "Owner")
    )
    try:
        loaded = await service.get(session_id)
        assert loaded is not None
        assert loaded.principal == session.principal
        assert loaded.csrf_token == session.csrf_token
        expired = Session(
            session.principal,
            "expired-csrf",
            datetime.now(UTC) - timedelta(seconds=1),
            datetime.now(UTC) + timedelta(hours=1),
        )
        expired_id = "expired-" + str(uuid4())
        await backend.put(expired_id, expired)
        assert await service.get(expired_id) is None
        assert await backend.get(expired_id) is None
    finally:
        await service.revoke(session_id)


def _signed_token(
    *, issuer: str, subject: str, audience: str, nonce: str
) -> tuple[bytes, dict[str, object]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = private_key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_pem = private_key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    public_jwk = cast(
        dict[str, object],
        JsonWebKey.import_key(  # pyright: ignore[reportUnknownMemberType]
            public_pem, {"kty": "RSA"}
        ).as_dict(),  # pyright: ignore[reportUnknownMemberType]
    )
    public_jwk["kid"] = "integration-key"
    token = JsonWebToken(["RS256"]).encode(  # pyright: ignore[reportUnknownMemberType]
        {"alg": "RS256", "kid": "integration-key"},
        {
            "iss": issuer,
            "sub": subject,
            "aud": audience,
            "nonce": nonce,
            "name": "Owner",
            "iat": int(datetime.now(UTC).timestamp()),
            "exp": int((datetime.now(UTC) + timedelta(minutes=5)).timestamp()),
        },
        private_pem,
    )
    return token, public_jwk


def _mock_oidc_client(
    monkeypatch: pytest.MonkeyPatch,
    token: bytes | None,
    public_jwk: dict[str, object] | None,
) -> dict[str, object]:
    issuer = "https://identity.integration.example"
    responses: dict[str, object] = {"token": token, "public_jwk": public_jwk}

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/.well-known/openid-configuration":
            return httpx.Response(
                200,
                json={
                    "authorization_endpoint": f"{issuer}/oauth/authorize",
                    "token_endpoint": f"{issuer}/oauth/token",
                    "jwks_uri": f"{issuer}/oauth/jwks",
                },
            )
        if request.url.path == "/oauth/token":
            current_token = responses["token"]
            assert isinstance(current_token, bytes)
            return httpx.Response(200, json={"id_token": current_token.decode()})
        if request.url.path == "/oauth/jwks":
            current_jwk = responses["public_jwk"]
            assert isinstance(current_jwk, dict)
            return httpx.Response(200, json={"keys": [current_jwk]})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    original = httpx.AsyncClient

    def client_factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        kwargs["transport"] = transport
        return original(*args, **kwargs)

    monkeypatch.setattr("aura_core.platform.oidc.httpx.AsyncClient", client_factory)
    return responses


@pytest.mark.asyncio
async def test_signed_oidc_exchange_accepts_provider_jwks(monkeypatch: pytest.MonkeyPatch) -> None:
    issuer = "https://identity.integration.example"
    token, public_jwk = _signed_token(
        issuer=issuer, subject="owner", audience="aura", nonce="nonce"
    )
    _mock_oidc_client(monkeypatch, token, public_jwk)
    identity = await exchange_code(
        issuer=issuer,
        client_id="aura-web",
        client_secret="secret",
        code="authorization-code",
        redirect_uri="https://aura.example/api/v1/auth/callback",
        expected_nonce="nonce",
        audience="aura",
        owner_subject="owner",
    )
    assert identity.subject == "owner"
    assert identity.issuer == issuer


@pytest.mark.asyncio
async def test_signed_oidc_exchange_rejects_bad_claim(monkeypatch: pytest.MonkeyPatch) -> None:
    issuer = "https://identity.integration.example"
    token, public_jwk = _signed_token(
        issuer=issuer, subject="owner", audience="aura", nonce="wrong"
    )
    _mock_oidc_client(monkeypatch, token, public_jwk)
    with pytest.raises(OIDCValidationError):
        await exchange_code(
            issuer=issuer,
            client_id="aura-web",
            client_secret="secret",
            code="authorization-code",
            redirect_uri="https://aura.example/api/v1/auth/callback",
            expected_nonce="nonce",
            audience="aura",
            owner_subject="owner",
        )


@pytest.mark.asyncio
async def test_production_oidc_callback_uses_valkey_state_and_sets_session_cookie(
    monkeypatch: pytest.MonkeyPatch,
    valkey_client: Any,
    sql_store: SqlConversationStore,
) -> None:
    del sql_store
    issuer = "https://identity.integration.example"
    settings = Settings(
        database_url=os.environ["AURA_TEST_DATABASE_URL"],
        valkey_url=os.environ["AURA_TEST_VALKEY_URL"],
        oidc_issuer=issuer,
        oidc_client_id="aura-web",
        oidc_client_secret="secret",
        oidc_audience="aura",
        oidc_redirect_uri="https://aura.example/api/v1/auth/callback",
        owner_subject="owner",
        secure_cookies=False,
    )
    app = create_app(settings)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://aura.example"
    )
    try:
        responses = _mock_oidc_client(monkeypatch, None, None)
        login = await client.get("/api/v1/auth/login", params={"return_to": "/chat"})
        assert login.status_code == 302
        state = httpx.URL(login.headers["location"]).params["state"]
        state_key = "aura:oidc-state:" + hashlib.sha256(state.encode()).hexdigest()
        stored = await valkey_client.get(state_key)
        assert stored is not None
        nonce = json.loads(stored)["nonce"]
        token, public_jwk = _signed_token(
            issuer=issuer, subject="owner", audience="aura", nonce=nonce
        )
        responses["token"] = token
        responses["public_jwk"] = public_jwk
        callback = await client.get(
            "/api/v1/auth/callback",
            params={"code": "authorization-code", "state": state},
        )
        assert callback.status_code == 302
        assert callback.headers["location"] == "/chat"
        cookie = callback.headers["set-cookie"]
        assert "aura_session=" in cookie
        assert "__Host-aura_session=" not in cookie
        assert "Secure" not in cookie
        assert "HttpOnly" in cookie
        assert "SameSite=lax" in cookie
    finally:
        await client.aclose()
        await app.state.aura.shutdown()


@pytest.mark.asyncio
async def test_nats_duplicate_command_and_producer_restart_are_deduplicated(nats_url: str) -> None:
    producer = NatsOutbox(nats_url)
    consumer = NatsRunConsumer(nats_url)
    try:
        try:
            await producer.connect()
            await consumer.connect()
        except Exception as exc:
            await producer.close()
            await consumer.close()
            pytest.skip(f"NATS JetStream integration dependency unavailable: {type(exc).__name__}")
        command = OutboxCommand(
            uuid4(), "aura.runs.execute.v1", uuid4(), uuid4(), datetime.now(UTC)
        )
        await producer.publish(command)
        await producer.close()
        restarted = NatsOutbox(nats_url)
        await restarted.connect()
        try:
            await restarted.publish(command)
        finally:
            await restarted.close()
        received = await asyncio.wait_for(consumer.receive(), timeout=3)
        assert received is not None
        assert received.id == command.id
        assert received.run_id == command.run_id
        await consumer.ack()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(consumer.receive(), timeout=1)
    finally:
        await producer.close()
        await consumer.close()
