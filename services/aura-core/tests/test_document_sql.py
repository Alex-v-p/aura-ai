"""SQL repository, transactional-outbox, and production composition checks."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from aura_core.bootstrap.document_uow import document_service
from aura_core.domains.knowledge.documents.persistence import (
    DocumentChunkRow,
    DocumentRow,
)
from aura_core.domains.knowledge.documents.public import (
    Document,
    DocumentChunk,
    DocumentService,
    DocumentStatus,
    DocumentUploadSession,
    DocumentVersionConflict,
    MemoryDocumentObjectStore,
    MemoryDocumentRepository,
)
from aura_core.domains.knowledge.documents.repository import SqlDocumentRepository
from aura_core.platform.auth import Settings
from aura_core.providers.document_parser import HttpDocumentParser

OWNER = ("https://issuer.example", "sql-document-owner")


def _document() -> Document:
    now = datetime.now(UTC)
    return Document(
        uuid4(),
        OWNER[0],
        OWNER[1],
        "transaction.txt",
        "text/plain",
        11,
        "a" * 64,
        "documents/sql/transaction",
        DocumentStatus.QUEUED,
        1,
        now,
        now,
    )


class _Session:
    def __init__(self) -> None:
        self.pending: list[object] = []
        self.persisted: list[object] = []
        self.commit_count = 0
        self.rollback_count = 0
        self.rows: dict[tuple[object, object], object] = {}
        self.locked_gets: list[tuple[object, object, bool]] = []

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.commit_count == 0:
            self.pending.clear()

    async def get(
        self,
        model: object,
        identity: object,
        *,
        with_for_update: bool = False,
    ) -> object | None:
        """Model AsyncSession.get(), including its row-lock contract."""

        self.locked_gets.append((model, identity, with_for_update))
        return self.rows.get((model, identity))

    def add(self, value: object) -> None:
        self.pending.append(value)

    async def commit(self) -> None:
        self.commit_count += 1
        self.persisted.extend(self.pending)
        self.pending.clear()

    async def rollback(self) -> None:
        self.rollback_count += 1
        self.pending.clear()


class _Sessions:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


class _FailingEventOutbox:
    def __init__(self) -> None:
        self.staged: list[str] = []

    async def stage(self, _session: object, *, document_id: object, operation: str) -> None:
        del document_id
        self.staged.append(operation)

    async def stage_event(self, *_: object, **__: object) -> None:
        raise RuntimeError("event staging failed")


@pytest.mark.asyncio
async def test_state_outbox_and_event_share_one_transaction_on_failure() -> None:
    session = _Session()
    outbox = _FailingEventOutbox()
    repository = SqlDocumentRepository(_Sessions(session), outbox=outbox)  # type: ignore[arg-type]
    document = _document()

    with pytest.raises(RuntimeError, match="event staging failed"):
        await repository.create_upload(document, idempotency_key="transaction-command")

    assert outbox.staged == ["process"]
    assert session.commit_count == 0
    assert session.persisted == []
    assert session.locked_gets == [(DocumentRow, document.id, True)]


@pytest.mark.asyncio
async def test_sql_upload_cas_rejects_placeholder_after_deletion_wins() -> None:
    session = _Session()
    document = _document()
    session.rows[(DocumentRow, document.id)] = SimpleNamespace(
        status=DocumentStatus.DELETING.value,
        version=document.version + 1,
    )
    repository = SqlDocumentRepository(_Sessions(session))  # type: ignore[arg-type]

    with pytest.raises(DocumentVersionConflict, match="upload placeholder changed"):
        await repository.create_upload(document)

    assert session.locked_gets == [(DocumentRow, document.id, True)]
    assert session.commit_count == 0
    assert session.rollback_count == 1
    assert session.pending == []


class _CommandSink:
    def __init__(self) -> None:
        self.published: list[tuple[object, str]] = []

    async def publish(self, document_id: object, *, operation: str = "process") -> None:
        self.published.append((document_id, operation))

    async def stage(self, *_: object, **__: object) -> None:
        return None

    async def stage_event(self, *_: object, **__: object) -> None:
        return None


@pytest.mark.asyncio
async def test_stale_selection_rechecks_consumed_session_before_cleanup() -> None:
    """A stale scan must not delete an upload completed after it selected it."""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    session = DocumentUploadSession(
        uuid4(),
        uuid4(),
        OWNER[0],
        OWNER[1],
        "stale.txt",
        "text/plain",
        4,
        "stale-race",
        now - timedelta(hours=1),
        now - timedelta(minutes=1),
    )

    class SelectThenConsume(MemoryDocumentRepository):
        async def stale_uploads(self, before: object, *, limit: int = 100) -> list[Document]:
            selected = await super().stale_uploads(before, limit=limit)
            claimed = await self.consume_upload_session(
                session.owner, session.id, expected_version=session.version
            )
            assert claimed is not None
            return selected

    repository = SelectThenConsume()
    await repository.create_upload_session(session)
    commands = _CommandSink()
    service = DocumentService(
        repository,
        MemoryDocumentObjectStore(),
        commands=commands,
        clock=lambda: now,
    )

    assert await service.recover_stale_uploads() == 0
    current = await repository.get_any(session.document_id)
    assert current is not None
    assert current.status is DocumentStatus.UPLOADING
    assert commands.published == []


@pytest.mark.asyncio
async def test_completion_cas_cannot_restore_document_after_delete_wins() -> None:
    """A stale processing completion loses the version race with deletion."""

    repository = MemoryDocumentRepository()
    document = _document()
    await repository.create_upload(document)
    commands = _CommandSink()

    processing = await repository.claim_processing(document.id)
    assert processing is not None
    deleting = await repository.transition_deleting(
        processing,
        expected_version=processing.version,
        command_sink=commands,
    )
    stale_chunk = DocumentChunk(
        uuid4(),
        document.id,
        0,
        "must never become searchable",
        "section:0",
        "b" * 64,
    )

    assert (
        await repository.settle_ready(
            processing,
            [stale_chunk],
            expected_version=processing.version,
        )
        is None
    )
    current = await repository.get_any(document.id)
    assert current is not None
    assert current.status is DocumentStatus.DELETING
    assert current.version == deleting.version
    assert await repository.chunks(document.id) == []


def test_sql_search_uses_expanded_lexical_and_vector_candidate_windows() -> None:
    statements: list[Any] = []

    class Session:
        async def __aenter__(self) -> Session:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, statement: Any) -> SimpleNamespace:
            statements.append(statement)
            return SimpleNamespace(all=lambda: [])  # pyright: ignore[reportUnknownLambdaType]

    class Sessions:
        def __call__(self) -> Session:
            return Session()

    async def exercise() -> None:
        repository = SqlDocumentRepository(Sessions())  # type: ignore[arg-type]
        await repository.search(
            OWNER,
            "rare evidence",
            limit=2,
            query_embedding=(1.0, 0.0),
        )

    asyncio.run(exercise())
    assert len(statements) == 2
    assert all(statement._limit_clause.value == 16 for statement in statements)
    assert DocumentChunkRow.__table__.c.document_id.index is True
    migration = (
        __import__("pathlib").Path(__file__).parents[1]
        / "migrations/versions/0019_document_library.py"
    ).read_text()
    assert "ix_document_chunks_lexical" in migration
    assert "ix_document_chunks_embedding" in migration
    assert "vector_cosine_ops" in migration
    assert DocumentRow.__table__.c.created_at is not None


def test_production_composition_uses_http_parser_provider() -> None:
    settings = Settings(
        database_url="postgresql+psycopg://aura:test@db/aura",
        object_storage_access_key="access",
        object_storage_secret_key="secret",
        document_parser_url="http://document-parser:8080",
    )
    service = document_service(object(), settings=settings, testing=False)
    assert isinstance(service.parser, HttpDocumentParser)
    assert service.parser.endpoint == "http://document-parser:8080"


@pytest.mark.asyncio
async def test_concurrent_sql_session_idempotency_is_optional_integration_coverage() -> None:
    database_url = os.environ.get("AURA_0068_DATABASE_URL")
    if not database_url:
        pytest.skip("AURA_0068_DATABASE_URL is required for SQL concurrency coverage")
    from alembic import command
    from aura_core.entrypoints.cli import alembic_config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    try:
        command.upgrade(alembic_config(Settings(database_url=database_url)), "head")
    except Exception as exc:
        message = str(getattr(exc, "orig", exc)).lower()
        if any(
            marker in message
            for marker in ("connection refused", "could not connect", "timed out")
        ):
            pytest.skip(f"PostgreSQL unavailable: {type(exc).__name__}")
        raise
    engine = create_async_engine(database_url, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    subject = f"sql-session-{uuid4().hex}"
    try:
        first = DocumentService(
            SqlDocumentRepository(sessions), MemoryDocumentObjectStore()
        )
        second = DocumentService(
            SqlDocumentRepository(sessions), MemoryDocumentObjectStore()
        )

        async def create(service: DocumentService):
            return await service.create_upload_session(
                OWNER[0],
                subject,
                filename="concurrent.txt",
                media_type="text/plain",
                byte_count=4,
                idempotency_key="same-session",
            )

        results = await asyncio.gather(
            create(first),
            create(second),
            return_exceptions=True,
        )
        assert all(not isinstance(result, BaseException) for result in results)
        assert results[0].id == results[1].id  # type: ignore[union-attr]
        completed = results[0]
        uploaded = await first.complete_upload(
            completed.id,  # type: ignore[union-attr]
            b"test",
            issuer=OWNER[0],
            subject=subject,
            content_type="text/plain",
            expected_version=completed.version,  # type: ignore[union-attr]
            idempotency_key="completed-content",
        )
        replay = await second.create_upload_session(
            OWNER[0],
            subject,
            filename="concurrent.txt",
            media_type="text/plain",
            byte_count=4,
            idempotency_key="same-session",
        )
        assert replay.id == completed.id  # type: ignore[union-attr]
        assert replay.document_id == uploaded.id  # type: ignore[union-attr]
    finally:
        async with sessions() as session:
            await session.execute(
                text("DELETE FROM document_upload_sessions WHERE principal_subject = :subject"),
                {"subject": subject},
            )
            await session.execute(
                text("DELETE FROM documents WHERE principal_subject = :subject"),
                {"subject": subject},
            )
            await session.commit()
        await engine.dispose()
