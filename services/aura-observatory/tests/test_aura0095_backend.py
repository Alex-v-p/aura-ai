"""AURA-0095 owner evaluation API, CLI and metadata-live regressions."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from aura_observatory.adapters.evaluation_live import PostgresEvaluationLiveEventStore
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.evaluation_api import EvaluationApplicationService
from aura_observatory.evaluation_live import (
    EvaluationLiveEventStore,
)
from aura_observatory.evaluation_query import (
    EvaluationConflict,
    EvaluationQueryBackend,
    EvaluationQueryError,
    EvaluationQueryService,
)
from aura_observatory.evaluation_storage import ArtifactStorageError
from aura_observatory.settings import Settings

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"
DATASET = "00000000-0000-4000-8000-000000000101"
CASE = "00000000-0000-4000-8000-000000000102"


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/evaluation/datasets",
        "/api/v1/evaluation/datasets/00000000-0000-4000-8000-000000000101/versions/1.0.0/cases",
        "/api/v1/evaluation/suites",
        "/api/v1/evaluation/evaluators",
        "/api/v1/evaluation/jobs",
        "/api/v1/evaluation/results",
        "/api/v1/evaluation/artifacts",
        "/api/v1/evaluation/reviews",
        "/api/v1/evaluation/calibrations",
        "/api/v1/evaluation/qualifications",
        "/api/v1/evaluation/history",
    ],
)
@pytest.mark.asyncio
async def test_every_evaluation_collection_is_owner_bounded_and_no_store(path: str) -> None:
    client, _session = await _client()
    async with client:
        response = await client.get(path, params={"page_size": "200"})
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    payload = response.json()
    assert set(payload) >= {"items", "partial", "nextCursor"}
    assert len(payload["items"]) <= 200


async def _client_for(subject: str = OWNER) -> tuple[httpx.AsyncClient, Any]:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal(ISSUER, subject, "Owner"))
    app = create_app(settings, auth=auth, testing=True)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    )
    return client, session


async def _client() -> tuple[httpx.AsyncClient, Any]:
    return await _client_for()


def _dataset() -> dict[str, object]:
    return {
        "datasetId": DATASET,
        "datasetVersion": "1.0.0",
        "sourceKind": "synthetic",
        "scopeIds": ["private"],
        "captureClassification": "metadata_only",
        "cases": [{"caseId": CASE, "caseVersion": "1.0.0", "artifactIds": []}],
        "retentionMode": "manual_only",
    }


@pytest.mark.asyncio
async def test_evaluation_commands_are_owner_csrf_idempotent_and_no_store() -> None:
    client, session = await _client()
    async with client:
        response = await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "dataset-command-0001",
            },
        )
        assert response.status_code == 201
        assert response.headers["cache-control"] == "no-store"
        assert "content" not in response.text.casefold()
        repeat = await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "dataset-command-0001",
            },
        )
        assert repeat.status_code == 201
        assert repeat.json()["datasetId"] == DATASET


@pytest.mark.asyncio
async def test_evaluation_reads_require_the_configured_owner() -> None:
    settings = Settings(owner_subject=OWNER)
    unauthenticated = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(settings, testing=True)),
        base_url=settings.public_origin,
    )
    async with unauthenticated:
        assert (await unauthenticated.get("/api/v1/evaluation/datasets")).status_code == 401

    client, _session = await _client_for("different-owner")
    async with client:
        assert (await client.get("/api/v1/evaluation/datasets")).status_code == 403


@pytest.mark.asyncio
async def test_observatory_health_remains_available_without_optional_judge_provider() -> None:
    settings = Settings(owner_subject=OWNER)
    app = create_app(settings, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "aura-observatory"}


@pytest.mark.asyncio
async def test_evaluation_commands_reject_missing_csrf_and_cross_origin_requests() -> None:
    client, session = await _client()
    async with client:
        missing = await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={"Idempotency-Key": "csrf-missing-0001"},
        )
        assert missing.status_code == 403
        cross_origin = await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={
                "Idempotency-Key": "csrf-origin-0001",
                "X-CSRF-Token": session.csrf_token,
                "Origin": "https://not-aura.example.invalid",
            },
        )
        assert cross_origin.status_code == 403


@pytest.mark.asyncio
async def test_evaluation_idempotency_conflict_is_deterministic() -> None:
    client, session = await _client()
    async with client:
        headers = {
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": "dataset-conflict-0001",
        }
        first = await client.post("/api/v1/evaluation/datasets", json=_dataset(), headers=headers)
        assert first.status_code == 201
        changed = _dataset()
        changed["scopeIds"] = ["different-scope"]
        conflict = await client.post(
            "/api/v1/evaluation/datasets", json=changed, headers=headers
        )
        assert conflict.status_code == 409
        assert "credential" not in conflict.text.casefold()


@pytest.mark.asyncio
async def test_evaluation_query_rejects_malformed_cursor_and_clamps_page_size() -> None:
    client, _session = await _client()
    async with client:
        malformed = await client.get(
            "/api/v1/evaluation/datasets?cursor=not-a-cursor&page_size=999999"
        )
        assert malformed.status_code == 400
        bounded = await client.get("/api/v1/evaluation/datasets?page_size=999999")
        assert bounded.status_code == 200
        assert len(bounded.json()["items"]) <= 200


@pytest.mark.asyncio
async def test_deletion_is_idempotent_and_conflicting_reuse_is_rejected() -> None:
    client, session = await _client()
    async with client:
        headers = {
            "X-CSRF-Token": session.csrf_token,
            "Idempotency-Key": "deletion-repeat-0001",
            "X-Observatory-Confirmation": "DELETE_EVALUATION_ARTIFACTS",
        }
        body = {
            "commandId": "00000000-0000-4000-8000-000000000201",
            "requestedAt": "2026-10-21T00:00:00Z",
            "kind": "purge_all",
        }
        first = await client.post("/api/v1/evaluation/deletion-actions", json=body, headers=headers)
        repeat = await client.post(
            "/api/v1/evaluation/deletion-actions", json=body, headers=headers
        )
        assert first.status_code == repeat.status_code == 202
        assert first.json()["actionId"] == repeat.json()["actionId"]
        conflict = await client.post(
            "/api/v1/evaluation/deletion-actions",
            json={
                "commandId": "00000000-0000-4000-8000-000000000202",
                "requestedAt": "2026-10-21T00:00:00Z",
                "kind": "cutoff",
                "cutoff": "2026-01-01T00:00:00Z",
            },
            headers=headers,
        )
        assert conflict.status_code == 409


@pytest.mark.asyncio
async def test_evaluation_sse_last_event_id_catches_up_only_new_metadata() -> None:
    client, session = await _client()
    async with client:
        first = await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "sse-first-000001",
            },
        )
        assert first.status_code == 201
        initial = await client.get(
            "/api/v1/evaluation/events", headers={"Origin": Settings().public_origin}
        )
        ids = re.findall(r"^id: ([0-9a-f-]+)$", initial.text, flags=re.MULTILINE)
        assert ids
        cursor = ids[-1]
        second = _dataset()
        second["datasetId"] = "00000000-0000-4000-8000-000000000103"
        second["cases"] = [
            {
                "caseId": "00000000-0000-4000-8000-000000000104",
                "caseVersion": "1.0.0",
                "artifactIds": [],
            }
        ]
        await client.post(
            "/api/v1/evaluation/datasets",
            json=second,
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "sse-second-000001",
            },
        )
        caught_up = await client.get(
            "/api/v1/evaluation/events",
            headers={"Origin": Settings().public_origin, "Last-Event-ID": cursor},
        )
        assert caught_up.status_code == 200
        assert second["datasetId"] in caught_up.text
        assert DATASET not in caught_up.text
        assert '"capturePolicy":"metadata_only"' in caught_up.text
        for forbidden in ("prompt", "response", "comment", "credential", "endpoint", "owner"):
            assert forbidden not in caught_up.text.casefold()


@pytest.mark.asyncio
async def test_evaluation_dataset_query_is_bounded_and_cursored() -> None:
    client, session = await _client()
    async with client:
        await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": "dataset-query-0001"},
        )
        result = await client.get("/api/v1/evaluation/datasets?page_size=1")
        assert result.status_code == 200
        assert result.json()["partial"] is False
        assert len(result.json()["items"]) == 1
        assert result.json()["nextCursor"] is None


@pytest.mark.asyncio
async def test_evaluation_sse_replays_metadata_only_event() -> None:
    client, session = await _client()
    async with client:
        await client.post(
            "/api/v1/evaluation/datasets",
            json=_dataset(),
            headers={"X-CSRF-Token": session.csrf_token, "Idempotency-Key": "dataset-sse-0001"},
        )
        response = await client.get(
            "/api/v1/evaluation/events", headers={"Origin": Settings().public_origin}
        )
        assert response.status_code == 200
        assert "evaluation.changed" in response.text
        assert DATASET in response.text
        assert "capturePolicy" in response.text
        assert all(
            secret not in response.text.casefold() for secret in ("credential", "endpoint", "owner")
        )


@pytest.mark.asyncio
async def test_destructive_commands_require_closed_confirmation_and_csrf() -> None:
    client, session = await _client()
    async with client:
        missing = await client.post(
            "/api/v1/evaluation/deletion-actions",
            json={
                "commandId": "00000000-0000-4000-8000-000000000203",
                "requestedAt": "2026-10-21T00:00:00Z",
                "kind": "purge_all",
            },
            headers={"Idempotency-Key": "delete-command-0001"},
        )
        assert missing.status_code in {403, 422}
        accepted = await client.post(
            "/api/v1/evaluation/deletion-actions",
            json={
                "commandId": "00000000-0000-4000-8000-000000000203",
                "requestedAt": "2026-10-21T00:00:00Z",
                "kind": "purge_all",
            },
            headers={
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "delete-command-0001",
                "X-Observatory-Confirmation": "DELETE_EVALUATION_ARTIFACTS",
            },
        )
        assert accepted.status_code == 202
        assert accepted.json()["status"] == "queued"


def test_cli_output_is_json_serializable_and_content_free() -> None:
    from aura_observatory.entrypoints.cli import evaluation_list

    del evaluation_list
    # The command uses the same query DTO serializer; ensure an empty result
    # remains a closed metadata object rather than a provider diagnostic.
    from aura_observatory.evaluation_api import EvaluationApplicationService

    value = EvaluationApplicationService().query.collection("datasets", limit=1)
    encoded = json.dumps(value)
    assert "credential" not in encoded.casefold()


def test_durable_query_composition_rejects_in_memory_live_fallback() -> None:
    with pytest.raises(EvaluationQueryError, match="durable evaluation live-event port"):
        EvaluationApplicationService(
            query=EvaluationQueryService(backend=cast(EvaluationQueryBackend, object()))
        )


@pytest.mark.asyncio
async def test_durable_deletion_idempotency_conflict_is_not_validation_error() -> None:
    class ConflictDeletionService:
        async def create_command(self, _command: object) -> None:
            raise ArtifactStorageError("idempotency_conflict")

    service = EvaluationApplicationService(
        query=EvaluationQueryService(backend=cast(EvaluationQueryBackend, object())),
        live=EvaluationLiveEventStore(),
        deletion_service=ConflictDeletionService(),
    )
    with pytest.raises(EvaluationConflict):
        await service.create_deletion(
            {
                "commandId": "00000000-0000-4000-8000-000000000301",
                "requestedAt": "2026-10-21T00:00:00Z",
                "kind": "purge_all",
            },
            "00000000-0000-4000-8000-000000000302",
        )


def test_live_event_migration_is_forward_only_idempotent_and_metadata_only() -> None:
    migration = (
        (
            Path(__file__).parents[1]
            / "migrations/postgresql/versions/0011_evaluation_live_events.sql"
        )
        .read_text(encoding="utf-8")
        .lower()
    )
    assert "create table if not exists observatory_evaluation_live_events" in migration
    assert "create index if not exists" in migration
    assert "metadata_only" in migration
    for forbidden in ("credential", "owner_identity"):
        assert forbidden not in migration
    assert "drop table" not in migration
    assert "drop column" not in migration
    assert "alter table" in migration  # additive projection metadata for upgraded schemas


class _LiveResult:
    def __init__(
        self, row: tuple[object, ...] | None = None, rows: list[tuple[object, ...]] | None = None
    ) -> None:
        self.row = row
        self.rows = rows or []

    async def fetchone(self) -> tuple[object, ...] | None:
        return self.row

    async def fetchall(self) -> list[tuple[object, ...]]:
        return self.rows


class _LiveConnection:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []

    async def execute(self, query: str, params: tuple[object, ...] = ()) -> _LiveResult:
        now = datetime.now(UTC)
        if query.startswith("INSERT"):
            event = (params[0], params[1], params[2], params[3], params[4], now)
            self.events.append(event)
            return _LiveResult(event)
        return _LiveResult(rows=list(self.events))


@pytest.mark.asyncio
async def test_postgres_live_event_store_replays_after_restart_without_payload() -> None:
    connection = _LiveConnection()

    class Factory:
        def __call__(self) -> Any:
            class Context:
                async def __aenter__(self) -> _LiveConnection:
                    return connection

                async def __aexit__(self, *_args: object) -> None:
                    return None

            return Context()

    store = PostgresEvaluationLiveEventStore(Factory())
    first = await store.publish("evaluation_job", DATASET, "created", "queued")
    replay = await PostgresEvaluationLiveEventStore(Factory()).after(None)
    assert len(replay) == 1
    assert replay[0].event_id == first.event_id
    assert replay[0].as_sse()["capturePolicy"] == "metadata_only"
    assert "payload" not in replay[0].as_sse()
