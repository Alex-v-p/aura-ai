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
from collections.abc import AsyncIterator
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
from aura_core.domains.execution.runs.public import RunStatus
from aura_core.domains.interaction.agents.adapters import SqlAgentSeeder
from aura_core.domains.interaction.conversations.public import GENERAL_AGENT
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import (
    Principal,
    RedisSessionBackend,
    Session,
    SessionService,
    Settings,
)
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.oidc import OIDCValidationError, exchange_code
from aura_core.platform.outbox.nats import NatsOutbox, NatsRunConsumer
from aura_core.platform.outbox.service import OutboxCommand
from aura_core.runtime.models.capacity import estimate_tokens
from aura_core.runtime.models.ports import ModelDescriptor
from authlib.jose import JsonWebKey, JsonWebToken
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

pytestmark = pytest.mark.integration
MODELS = (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)


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
    await sql_store.add_run(
        conversation.id,
        "owner",
        current_prompt,
        conversation.version,
        str(uuid4()),
        issuer,
    )
    budget = (
        estimate_tokens(GENERAL_AGENT.system_prompt)
        + estimate_tokens(current_prompt)
        + estimate_tokens(prior_assistant)
    )

    context = await sql_store.context(conversation.id, "owner", issuer, budget)

    assert context == [
        ("system", GENERAL_AGENT.system_prompt),
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
