"""HTTP contract and privacy coverage for AURA-0032 metadata lifecycle."""

import asyncio
import json
import os
from collections.abc import AsyncIterator, Callable, Sequence
from datetime import UTC, datetime, timedelta
from time import perf_counter
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.bootstrap.database import metadata
from aura_core.domains.execution.runs.dto import Run, RunStatus
from aura_core.domains.execution.runs.persistence import RunRow
from aura_core.domains.interaction.agents.adapters.postgres import SqlAgentSeeder
from aura_core.domains.interaction.agents.persistence import (
    PromptBundleRevisionRow,
    PromptComponentRevisionRow,
)
from aura_core.domains.interaction.agents.public import (
    GOVERNANCE_COMPONENT_ID,
    PLATFORM_COMPONENT_ID,
    PROMPT_BUNDLE_ID,
)
from aura_core.domains.interaction.conversations.dto import (
    Conversation,
    ConversationListFilters,
    Message,
)
from aura_core.domains.interaction.conversations.persistence import MessageRow
from aura_core.domains.interaction.conversations.public import ActiveRunConflict
from aura_core.entrypoints.api.routes import conversations as conversations_route
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Session
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.telemetry import new_span_id
from aura_core.runtime.models.ports import ModelDescriptor
from fastapi import FastAPI
from sqlalchemy import event, update
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession
from starlette.requests import Request

from conftest import owner_client

JsonObject = dict[str, Any]
ISSUER = "https://authentik.dev.example"
SUBJECT = "owner-subject"
POSTGRES_MODELS = (ModelDescriptor("chat", "Chat", "ollama", ("chat", "completion")),)


def _database_url() -> str | None:
    value = os.environ.get("AURA_TEST_DATABASE_URL")
    if not value or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        return None
    return value


@pytest_asyncio.fixture
async def sql_metadata_fixture() -> AsyncIterator[tuple[SqlConversationStore, AsyncEngine]]:
    """Use the same explicitly disposable PostgreSQL convention as adapter tests."""

    database_url = _database_url()
    if database_url is None:
        pytest.skip(
            "set AURA_TEST_DATABASE_URL and AURA_TEST_DEPENDENCIES_ISOLATED=1 "
            "for disposable PostgreSQL concurrency and pagination coverage"
        )
    engine = make_engine(database_url)
    sessions = session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata().drop_all)
            await connection.run_sync(metadata().create_all)
    except OperationalError as exc:
        await engine.dispose()
        pytest.skip(f"PostgreSQL integration dependency unavailable: {type(exc).__name__}")
    # Seed the prompt graph in dependency order.  SqlAgentSeeder intentionally
    # batches these rows for the normal application path, but a bare metadata
    # create_all database can otherwise let PostgreSQL flush the bundle before
    # its component revisions.  This fixture owns the disposable schema, so
    # make those prerequisites explicit before invoking the production seeder.
    async with sessions() as session, session.begin():
        session.add_all(
            [
                PromptComponentRevisionRow(
                    id=PLATFORM_COMPONENT_ID, component="platform", revision=1, content=""
                ),
                PromptComponentRevisionRow(
                    id=GOVERNANCE_COMPONENT_ID, component="governance", revision=1, content=""
                ),
            ]
        )
        await session.flush()
        session.add(
            PromptBundleRevisionRow(
                id=PROMPT_BUNDLE_ID,
                revision=1,
                platform_component_revision_id=PLATFORM_COMPONENT_ID,
                governance_component_revision_id=GOVERNANCE_COMPONENT_ID,
            )
        )
        await session.flush()
    await SqlAgentSeeder(sessions).seed()
    try:
        yield SqlConversationStore(sessions), engine
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata().drop_all)
        await engine.dispose()


def _aura(api_app: FastAPI) -> AppState:
    return cast(AppState, api_app.state.aura)


def _headers(
    session: Session, *, key: str | None = None, csrf: str | None = None
) -> dict[str, str]:
    result = {"X-CSRF-Token": csrf or session.csrf_token}
    if key is not None:
        result["Idempotency-Key"] = key
    return result


async def _create_conversation(
    client: httpx.AsyncClient,
    session: Session,
    message: str = "metadata test",
) -> JsonObject:
    response = await client.post(
        "/api/v1/conversations",
        headers=_headers(session, key=str(uuid4())),
        json={"message": message, "modelId": "chat"},
    )
    assert response.status_code == 202, response.text
    return cast(JsonObject, response.json()["conversation"])


@pytest.mark.asyncio
async def test_metadata_patch_renames_archives_restores_and_returns_archived_at(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session)
        identifier = conversation["id"]
        renamed = await client.patch(
            f"/api/v1/conversations/{identifier}/metadata",
            headers=_headers(session, key="metadata-rename"),
            json={"title": "  Manual title  ", "version": conversation["version"]},
        )
        assert renamed.status_code == 200, renamed.text
        renamed_payload = renamed.json()
        assert renamed_payload["title"] == "Manual title"
        assert renamed_payload["archivedAt"] is None
        assert renamed_payload["version"] == conversation["version"] + 1

        # The initial run is still queued in the deterministic API fixture;
        # make it terminal before exercising archive.
        run_id = conversation["currentRun"]["id"]
        await _aura(api_app).store.finish_run(UUID(run_id), RunStatus.COMPLETED)
        archived = await client.patch(
            f"/api/v1/conversations/{identifier}/metadata",
            headers=_headers(session, key="metadata-archive"),
            json={"archived": True, "version": renamed_payload["version"]},
        )
        assert archived.status_code == 200, archived.text
        archived_payload = archived.json()
        assert archived_payload["archivedAt"] is not None
        assert (await client.get(f"/api/v1/conversations/{identifier}")).json()["archivedAt"]

        default_items = (await client.get("/api/v1/conversations")).json()["items"]
        assert identifier not in {item["id"] for item in default_items}
        archived_items = (
            await client.get("/api/v1/conversations", params={"archiveState": "archived"})
        ).json()["items"]
        assert identifier in {item["id"] for item in archived_items}

        restored = await client.patch(
            f"/api/v1/conversations/{identifier}/metadata",
            headers=_headers(session, key="metadata-restore"),
            json={"archived": False, "version": archived_payload["version"]},
        )
        assert restored.status_code == 200, restored.text
        assert restored.json()["archivedAt"] is None
        archive_measurements = [
            item
            for item in _aura(api_app).metrics.snapshot()
            if item.metric == "conversation_archive_outcome"
        ]
        archive_states = {
            (
                dict(item.dimensions).get("archive_state"),
                dict(item.dimensions).get("outcome"),
                dict(item.trace_attributes).get("conversation_id"),
            )
            for item in archive_measurements
        }
        assert ("archived", "ok", identifier) in archive_states
        assert ("active", "ok", identifier) in archive_states
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_metadata_endpoint_requires_csrf_idempotency_and_valid_mutation(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session)
        path = f"/api/v1/conversations/{conversation['id']}/metadata"
        no_csrf = await client.patch(
            path,
            headers={"Idempotency-Key": "missing-csrf"},
            json={"title": "blocked", "version": conversation["version"]},
        )
        assert no_csrf.status_code == 403
        no_key = await client.patch(
            path,
            headers={"X-CSRF-Token": session.csrf_token},
            json={"title": "blocked", "version": conversation["version"]},
        )
        assert no_key.status_code == 422
        no_mutation = await client.patch(
            path,
            headers=_headers(session, key="no-mutation"),
            json={"version": conversation["version"]},
        )
        assert no_mutation.status_code == 422
        invalid_title = await client.patch(
            path,
            headers=_headers(session, key="bad-title"),
            json={"title": "bad\nvalue", "version": conversation["version"]},
        )
        assert invalid_title.status_code == 422
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_metadata_endpoint_enforces_idempotency_and_optimistic_versions(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session)
        path = f"/api/v1/conversations/{conversation['id']}/metadata"
        body = {"title": "Stable", "version": conversation["version"]}
        first = await client.patch(path, headers=_headers(session, key="same-key"), json=body)
        replay = await client.patch(path, headers=_headers(session, key="same-key"), json=body)
        assert first.status_code == replay.status_code == 200
        assert replay.json()["version"] == first.json()["version"]
        conflict = await client.patch(
            path,
            headers=_headers(session, key="same-key"),
            json={"title": "Different", "version": conversation["version"]},
        )
        assert conflict.status_code == 409
        stale = await client.patch(
            path,
            headers=_headers(session, key="stale-version"),
            json={"title": "Fresh key", "version": conversation["version"]},
        )
        assert stale.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_archive_conflicts_with_queued_run_and_blocks_new_run(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session, "active run")
        path = f"/api/v1/conversations/{conversation['id']}/metadata"
        blocked = await client.patch(
            path,
            headers=_headers(session, key="archive-active"),
            json={"archived": True, "version": conversation["version"]},
        )
        assert blocked.status_code == 409
        await _aura(api_app).store.finish_run(
            UUID(conversation["currentRun"]["id"]), RunStatus.COMPLETED
        )
        archived = await client.patch(
            path,
            headers=_headers(session, key="archive-after-run"),
            json={"archived": True, "version": conversation["version"]},
        )
        assert archived.status_code == 200
        new_run = await client.post(
            f"/api/v1/conversations/{conversation['id']}/runs",
            headers=_headers(session, key="new-run-archived"),
            json={"message": "blocked", "conversationVersion": archived.json()["version"]},
        )
        assert new_run.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_archived_configuration_retry_and_cancel_are_rejected_with_conflict(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session, "archive controls")
        run_id = str(conversation["currentRun"]["id"])
        await _aura(api_app).store.finish_run(UUID(run_id), RunStatus.COMPLETED)
        archived = await client.patch(
            f"/api/v1/conversations/{conversation['id']}/metadata",
            headers=_headers(session, key="archive-controls"),
            json={"archived": True, "version": conversation["version"]},
        )
        assert archived.status_code == 200, archived.text
        version = archived.json()["version"]

        configuration = await client.patch(
            f"/api/v1/conversations/{conversation['id']}",
            headers=_headers(session, key="config-archived"),
            json={"modelId": "chat-plus", "version": version},
        )
        assert configuration.status_code == 409
        retry = await client.post(
            f"/api/v1/runs/{run_id}/retry",
            headers=_headers(session, key="retry-archived"),
        )
        assert retry.status_code == 409
        cancel = await client.post(
            f"/api/v1/runs/{run_id}/cancel",
            headers=_headers(session, key="cancel-archived"),
        )
        assert cancel.status_code == 409
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_list_filters_are_metadata_only_and_cursor_mismatch_is_bad_request(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        first = await _create_conversation(client, session, "private transcript secret")
        second = await _create_conversation(client, session, "Second visible title")
        renamed = await client.patch(
            f"/api/v1/conversations/{first['id']}/metadata",
            headers=_headers(session, key="filter-fixture-rename"),
            json={"title": "Private conversation", "version": first["version"]},
        )
        assert renamed.status_code == 200
        first = renamed.json()
        await _aura(api_app).store.finish_run(
            UUID(first["currentRun"]["id"]), RunStatus.COMPLETED
        )
        await _aura(api_app).store.finish_run(
            UUID(second["currentRun"]["id"]), RunStatus.FAILED
        )

        title_results = (
            await client.get("/api/v1/conversations", params={"q": "visible"})
        ).json()["items"]
        assert [item["id"] for item in title_results] == [second["id"]]
        content_results = (
            await client.get("/api/v1/conversations", params={"q": "transcript secret"})
        ).json()["items"]
        assert content_results == []
        status_results = (
            await client.get("/api/v1/conversations", params={"runStatus": "failed"})
        ).json()["items"]
        assert [item["id"] for item in status_results] == [second["id"]]
        bounds = (
            await client.get(
                "/api/v1/conversations",
                params={
                    "activityFrom": "2026-01-01T00:00:00Z",
                    "activityTo": "2027-01-01T00:00:00Z",
                },
            )
        )
        assert bounds.status_code == 200

        page = await client.get("/api/v1/conversations", params={"limit": 1})
        assert page.status_code == 200
        cursor = page.json()["nextCursor"]
        assert cursor
        mismatch = await client.get(
            "/api/v1/conversations", params={"limit": 1, "cursor": cursor, "q": "visible"}
        )
        assert mismatch.status_code == 400
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_archive_state_results_and_list_telemetry_include_result_count(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        active = await _create_conversation(client, session, "active conversation")
        archived = await _create_conversation(client, session, "archived conversation")
        await _aura(api_app).store.finish_run(
            UUID(active["currentRun"]["id"]), RunStatus.COMPLETED
        )
        await _aura(api_app).store.finish_run(
            UUID(archived["currentRun"]["id"]), RunStatus.COMPLETED
        )
        archive_response = await client.patch(
            f"/api/v1/conversations/{archived['id']}/metadata",
            headers=_headers(session, key="archive-state-fixture"),
            json={"archived": True, "version": archived["version"]},
        )
        assert archive_response.status_code == 200

        active_items = (
            await client.get("/api/v1/conversations", params={"archiveState": "active"})
        ).json()["items"]
        archived_items = (
            await client.get("/api/v1/conversations", params={"archiveState": "archived"})
        ).json()["items"]
        all_items = (
            await client.get("/api/v1/conversations", params={"archiveState": "all"})
        ).json()["items"]
        assert {item["id"] for item in active_items} == {active["id"]}
        assert {item["id"] for item in archived_items} == {archived["id"]}
        assert {item["id"] for item in all_items} == {active["id"], archived["id"]}

        filter_measurements = [
            item
            for item in _aura(api_app).metrics.snapshot()
            if item.metric == "conversation_filter_outcome"
        ]
        result_counts = {
            (
                dict(item.dimensions).get("archive_state"),
                dict(item.trace_attributes).get("result_count"),
            )
            for item in filter_measurements
        }
        assert {("active", "1"), ("archived", "1"), ("all", "2")} <= result_counts
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_metadata_mutation_telemetry_contains_identifiers_not_private_title_or_search_terms(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session, "private message text")
        private_title = "private-title-text"
        response = await client.patch(
            f"/api/v1/conversations/{conversation['id']}/metadata",
            headers=_headers(session, key="telemetry-metadata"),
            json={"title": private_title, "version": conversation["version"]},
        )
        assert response.status_code == 200
        telemetry = json.dumps(
            [
                {
                    "metric": item.metric,
                    "dimensions": dict(item.dimensions),
                    "trace": dict(item.trace_attributes),
                }
                for item in _aura(api_app).metrics.snapshot()
            ]
        )
        assert "conversation_metadata_mutation_duration_ms" in telemetry
        assert str(conversation["id"]) in telemetry
        assert private_title not in telemetry
        assert "private message text" not in telemetry
        current = cast(JsonObject, response.json())

        failed = await client.patch(
            f"/api/v1/conversations/{conversation['id']}/metadata",
            headers=_headers(session, key="telemetry-failure"),
            json={"title": "new title", "version": current["version"] - 1},
        )
        assert failed.status_code == 409
        mutation_measurements = [
            item
            for item in _aura(api_app).metrics.snapshot()
            if item.metric == "conversation_metadata_mutation_duration_ms"
        ]
        outcomes = {
            (
                dict(item.dimensions).get("outcome"),
                dict(item.trace_attributes).get("conversation_id"),
            )
            for item in mutation_measurements
        }
        assert ("ok", str(conversation["id"])) in outcomes
        assert ("error", str(conversation["id"])) in outcomes
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_metadata_request_and_sql_persistence_spans_share_trace_parent_and_local_timing(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        payload = await _create_conversation(client, session)
        conversation = await _aura(api_app).store.get(
            UUID(payload["id"]), SUBJECT, ISSUER
        )
        # The SQL store seam is isolated from the in-memory API fixture here;
        # its persistence method is replaced so this test exercises telemetry
        # linkage without requiring a second application composition.
        sql_store = SqlConversationStore(lambda: None)  # type: ignore[arg-type]
        sql_store._persist_metadata = AsyncMock(  # type: ignore[method-assign]
            return_value=conversation
        )
        metrics = _aura(api_app).metrics
        trace_id = uuid4().hex
        request_span_id = new_span_id()
        await sql_store.update_metadata(
            conversation.id,
            SUBJECT,
            title="Private title that must not enter telemetry",
            archived=None,
            version=conversation.version,
            idempotency_key="telemetry-child",
            issuer=ISSUER,
            trace_id=trace_id,
            parent_span_id=request_span_id,
            metrics=metrics,
        )
        request = Request({"type": "http", "app": api_app})
        record_metadata_mutation = cast(
            Callable[..., None], conversations_route._record_metadata_mutation  # pyright: ignore[reportPrivateUsage]
        )
        record_metadata_mutation(
            request,
            trace_id,
            request_span_id,
            conversation.id,
            perf_counter(),
            "active",
            "ok",
            None,
            False,
            result_count=1,
        )
        spans = {
            dict(item.trace_attributes)["operation"]: item
            for item in metrics.snapshot()
            if item.kind == "span"
        }
        request_span = spans["conversation.metadata.request"]
        persistence_span = spans["conversation.persist"]
        assert request_span.trace_id == persistence_span.trace_id == trace_id
        assert request_span.parent_span_id is None
        assert persistence_span.parent_span_id == request_span.span_id == request_span_id
        assert dict(request_span.dimensions) == {
            "dependency": "conversation_store",
            "outcome": "ok",
        }
        assert dict(persistence_span.dimensions) == {
            "dependency": "postgresql",
            "outcome": "ok",
        }
        assert request_span.value >= 0
        assert persistence_span.value >= 0
        rendered = json.dumps(
            [dict(item.trace_attributes) for item in (request_span, persistence_span)]
        )
        assert "Private title" not in rendered
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_validation_and_in_memory_metadata_mutations_have_no_postgresql_child(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session, "validation private message")
        baseline = len(_aura(api_app).metrics.snapshot())
        invalid = await client.patch(
            f"/api/v1/conversations/{conversation['id']}/metadata",
            headers=_headers(session, key="telemetry-validation"),
            json={"title": "invalid\nprivate title", "version": conversation["version"]},
        )
        assert invalid.status_code == 422
        renamed = await client.patch(
            f"/api/v1/conversations/{conversation['id']}/metadata",
            headers=_headers(session, key="telemetry-memory-success"),
            json={"title": "memory-only title", "version": conversation["version"]},
        )
        assert renamed.status_code == 200
        spans = [
            item
            for item in _aura(api_app).metrics.snapshot()[baseline:]
            if item.kind == "span"
        ]
        operations = [dict(item.trace_attributes)["operation"] for item in spans]
        assert operations.count("conversation.metadata.request") == 2
        assert "conversation.persist" not in operations
        assert all(dict(item.dimensions)["dependency"] == "conversation_store" for item in spans)
        assert all(
            "validation private title" not in json.dumps(dict(item.trace_attributes))
            for item in spans
        )
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_unexpected_metadata_failure_is_unknown_and_bounded_without_private_data(
    api_app: FastAPI,
) -> None:
    client, session = await owner_client(api_app)
    try:
        conversation = await _create_conversation(client, session, "failure private message")

        async def fail_update(*args: object, **kwargs: object) -> object:
            del args, kwargs
            raise RuntimeError("provider stack trace must not be telemetry")

        _aura(api_app).store.update_metadata = fail_update  # type: ignore[method-assign]
        with pytest.raises(RuntimeError):
            await client.patch(
                f"/api/v1/conversations/{conversation['id']}/metadata",
                headers=_headers(session, key="telemetry-unexpected-failure"),
                json={"title": "failure private title", "version": conversation["version"]},
            )
        spans = [item for item in _aura(api_app).metrics.snapshot() if item.kind == "span"]
        failure = next(
            item
            for item in spans
            if dict(item.trace_attributes).get("operation") == "conversation.metadata.request"
        )
        assert dict(failure.dimensions) == {
            "dependency": "conversation_store",
            "error_class": "persistence",
            "outcome": "error",
        }
        measurements = [
            item
            for item in _aura(api_app).metrics.snapshot()
            if item.metric == "conversation_metadata_mutation_duration_ms"
        ]
        assert dict(measurements[-1].dimensions)["archive_state"] == "unknown"
        rendered = json.dumps(dict(failure.trace_attributes))
        assert "failure private title" not in rendered
        assert "provider stack trace" not in rendered
    finally:
        await client.aclose()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_archive_and_cancel_overlap_has_valid_lock_order(
    sql_metadata_fixture: tuple[SqlConversationStore, AsyncEngine],
) -> None:
    """Concurrent admission paths must finish without a PostgreSQL deadlock."""

    sql_metadata_store, _ = sql_metadata_fixture
    conversation, _, run = await sql_metadata_store.create(
        ISSUER,
        SUBJECT,
        "concurrent lifecycle fixture",
        "chat",
        POSTGRES_MODELS,
        "postgres-concurrency-create",
    )

    async def archive() -> str:
        try:
            await sql_metadata_store.update_metadata(
                conversation.id,
                SUBJECT,
                title=None,
                archived=True,
                version=conversation.version,
                idempotency_key="postgres-concurrency-archive",
                issuer=ISSUER,
            )
        except ActiveRunConflict:
            return "archive-conflict"
        return "archive-ok"

    async def cancel() -> str:
        cancelled = await sql_metadata_store.request_cancel(
            run.id,
            SUBJECT,
            "postgres-concurrency-cancel",
            issuer=ISSUER,
        )
        return cancelled.status.value

    outcomes = await asyncio.wait_for(asyncio.gather(archive(), cancel()), timeout=10)
    assert outcomes == ["archive-conflict", RunStatus.CANCEL_REQUESTED.value]

    final = await sql_metadata_store.get(conversation.id, SUBJECT, ISSUER)
    assert final.archived_at is None
    assert len(final.runs) == 1
    assert final.runs[0].status is RunStatus.CANCEL_REQUESTED


@pytest.mark.integration
@pytest.mark.asyncio
async def test_postgres_run_status_filter_batches_latest_lookup_and_paginates_sparse_matches(
    sql_metadata_fixture: tuple[SqlConversationStore, AsyncEngine],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sparse status matches preserve order and use one lookup per batch."""

    sql_metadata_store, engine = sql_metadata_fixture
    created: list[tuple[Conversation, Message, Run]] = []
    for index in range(65):
        created.append(
            await sql_metadata_store.create(
                ISSUER,
                SUBJECT,
                f"batch fixture {index}",
                "chat",
                POSTGRES_MODELS,
                f"postgres-batch-create-{index}",
            )
        )

    base = datetime(2026, 1, 1, tzinfo=UTC)
    failed_indices = {0, 30}
    async with sql_metadata_store.sessions() as session, session.begin():
        for index, (_, message, run) in enumerate(created):
            await session.execute(
                update(MessageRow)
                .where(MessageRow.id == message.id)
                .values(created_at=base + timedelta(seconds=index))
            )
            await session.execute(
                update(RunRow)
                .where(RunRow.id == run.id)
                .values(
                    status=RunStatus.FAILED.value
                    if index in failed_indices
                    else RunStatus.COMPLETED.value
                )
            )

    original = sql_metadata_store.runs.latest_statuses
    batch_sizes: list[int] = []
    latest_status_query_count = 0

    def count_latest_status_query(
        _connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal latest_status_query_count
        normalized = statement.casefold()
        if "row_number" in normalized and "from runs" in normalized:
            latest_status_query_count += 1

    async def counted_latest_statuses(
        session: AsyncSession, conversation_ids: Sequence[UUID]
    ) -> dict[UUID, RunStatus]:
        batch_sizes.append(len(conversation_ids))
        return await original(session, conversation_ids)

    monkeypatch.setattr(sql_metadata_store.runs, "latest_statuses", counted_latest_statuses)
    event.listen(engine.sync_engine, "before_cursor_execute", count_latest_status_query)
    try:
        filters = ConversationListFilters(run_status=RunStatus.FAILED)
        first_page, cursor = await sql_metadata_store.list(
            SUBJECT,
            limit=1,
            issuer=ISSUER,
            filters=filters,
        )
        assert cursor is not None
        assert [item.id for item in first_page] == [created[30][0].id]
        assert batch_sizes == [30, 30, 5]
        assert latest_status_query_count == len(batch_sizes)

        second_page, final_cursor = await sql_metadata_store.list(
            SUBJECT,
            limit=1,
            cursor=cursor,
            issuer=ISSUER,
            filters=filters,
        )
        assert [item.id for item in second_page] == [created[0][0].id]
        assert final_cursor is None
        assert batch_sizes == [30, 30, 5, 30]
        assert latest_status_query_count == len(batch_sizes)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", count_latest_status_query)
