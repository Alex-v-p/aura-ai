"""Owner-scope, mutation-safety, and content-privacy API coverage for AURA-0037."""

from __future__ import annotations

from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_core.domains.knowledge.memory.public import MemoryKind, MemoryScope, MemoryScopeType
from aura_core.platform.auth import Session

from conftest import owner_client

JsonObject = dict[str, Any]


def _headers(
    session: Session, *, key: str | None = None, csrf: str | None = None
) -> dict[str, str]:
    headers = {"X-CSRF-Token": csrf or session.csrf_token}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _body(
    *, content: str = "The owner prefers concise answers.", scope: dict[str, Any] | None = None
) -> JsonObject:
    return {
        "content": content,
        "kind": "preference",
        "scope": scope or {"type": "user"},
        "confidence": 0.9,
        "importance": 0.8,
        "halfLifeDays": 30,
    }


async def _create(
    client: httpx.AsyncClient,
    session: Session,
    *,
    key: str = "00000000-0000-4000-8000-000000000001",
    **body: Any,
) -> JsonObject:
    response = await client.post(
        "/api/v1/memories",
        headers=_headers(session, key=key),
        json=_body(**body),
    )
    assert response.status_code == 201, response.text
    return cast(JsonObject, response.json())


@pytest.mark.asyncio
async def test_memory_create_list_detail_are_owner_scoped_and_metadata_safe(api_app: Any) -> None:
    client, session = await owner_client(api_app)
    secret = "The owner prefers concise answers."
    try:
        created = await _create(client, session, content=secret)
        memory_id = created["id"]
        assert created["currentRevision"]["content"] == secret
        assert "vector" not in str(created).lower()
        assert "embeddings" in created

        listed = await client.get("/api/v1/memories")
        assert listed.status_code == 200, listed.text
        assert memory_id in {item["id"] for item in listed.json()["items"]}
        detail = await client.get(f"/api/v1/memories/{memory_id}")
        assert detail.status_code == 200, detail.text
        assert detail.json()["id"] == memory_id
        assert "vector" not in detail.text.lower()

        metrics = api_app.state.aura.metrics.snapshot()
        assert all(secret not in repr(measurement) for measurement in metrics)
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_mutations_require_csrf_and_idempotency_and_replay_exactly(
    api_app: Any,
) -> None:
    client, session = await owner_client(api_app)
    try:
        missing_csrf = await client.post(
            "/api/v1/memories", headers={"Idempotency-Key": str(uuid4())}, json=_body()
        )
        assert missing_csrf.status_code == 403
        missing_key = await client.post(
            "/api/v1/memories", headers={"X-CSRF-Token": session.csrf_token}, json=_body()
        )
        assert missing_key.status_code == 422

        headers = _headers(session, key=str(uuid4()))
        first = await client.post("/api/v1/memories", headers=headers, json=_body())
        replay = await client.post("/api/v1/memories", headers=headers, json=_body())
        assert first.status_code == replay.status_code == 201
        assert replay.json() == first.json()

        conflict = await client.post(
            "/api/v1/memories", headers=headers, json=_body(content="changed payload")
        )
        assert conflict.status_code == 409
        assert "changed payload" not in conflict.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_credential_rejection_and_half_life_bounds_are_content_safe(
    api_app: Any,
) -> None:
    client, session = await owner_client(api_app)
    try:
        secret = "api_key: never persist this"
        rejected = await client.post(
            "/api/v1/memories",
            headers=_headers(session, key=str(uuid4())),
            json=_body(content=secret),
        )
        assert rejected.status_code == 422
        assert secret not in rejected.text
        for half_life in (0.249, 3650.1):
            body = _body()
            body["halfLifeDays"] = half_life
            response = await client.post(
                "/api/v1/memories", headers=_headers(session, key=str(uuid4())), json=body
            )
            assert response.status_code == 422
            assert secret not in response.text
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_correction_status_pin_conflicts_and_confirmed_purge(api_app: Any) -> None:
    client, session = await owner_client(api_app)
    try:
        memory = await _create(client, session)
        memory_id = memory["id"]
        corrected = await client.post(
            f"/api/v1/memories/{memory_id}/revisions",
            headers=_headers(session, key=str(uuid4())),
            json={
                "content": "The owner prefers detailed answers.",
                "reason": "owner correction",
                "expectedVersion": 1,
            },
        )
        assert corrected.status_code == 201, corrected.text
        payload = corrected.json()
        assert payload["version"] == 2
        assert [revision["revision"] for revision in payload["revisions"]] == [1, 2]
        secret_reason = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.signature"
        rejected_reason = await client.post(
            f"/api/v1/memories/{memory_id}/revisions",
            headers=_headers(session, key=str(uuid4())),
            json={
                "content": "The owner prefers balanced answers.",
                "reason": secret_reason,
                "expectedVersion": 2,
            },
        )
        assert rejected_reason.status_code == 422
        assert secret_reason not in rejected_reason.text

        stale = await client.patch(
            f"/api/v1/memories/{memory_id}/pin",
            headers=_headers(session, key=str(uuid4())),
            json={"pinned": True, "expectedVersion": 1},
        )
        assert stale.status_code == 409
        pinned = await client.patch(
            f"/api/v1/memories/{memory_id}/pin",
            headers=_headers(session, key=str(uuid4())),
            json={"pinned": True, "expectedVersion": 2},
        )
        assert pinned.status_code == 200
        assert pinned.json()["pinned"] is True

        incorrect = await client.post(
            f"/api/v1/memories/{memory_id}/purge",
            headers=_headers(session, key=str(uuid4())),
            json={"confirmation": "PURGE", "expectedVersion": 3},
        )
        assert incorrect.status_code == 400, incorrect.text
        purged = await client.post(
            f"/api/v1/memories/{memory_id}/purge",
            headers=_headers(session, key=str(uuid4())),
            json={"confirmation": "PURGE MEMORY", "expectedVersion": 3},
        )
        assert purged.status_code == 200, purged.text
        assert (await client.get(f"/api/v1/memories/{memory_id}")).status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_owner_isolation_returns_not_found_for_other_session(api_app: Any) -> None:
    owner, owner_session = await owner_client(api_app)
    try:
        foreign = await api_app.state.aura.memory_repository.create_memory(
            "https://authentik.dev.example",
            "owner-b",
            content="foreign owner fact",
            kind=MemoryKind.PREFERENCE,
            scope=MemoryScope(MemoryScopeType.USER),
            confidence=0.9,
            importance=0.8,
            half_life_days=30,
        )
        memory_id = foreign.id
        assert (await owner.get(f"/api/v1/memories/{memory_id}")).status_code == 404
        assert (await owner.get("/api/v1/memories")).json()["items"] == []
        assert (
            await owner.patch(
                f"/api/v1/memories/{memory_id}/status",
                headers=_headers(owner_session, key=str(uuid4())),
                json={"status": "disabled", "relatedMemoryId": None, "expectedVersion": 1},
            )
        ).status_code == 404
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_agent_scope_requires_explicit_agent_filter_for_two_agent_records(
    api_app: Any,
) -> None:
    client, session = await owner_client(api_app)
    agent_a, agent_b = str(uuid4()), str(uuid4())
    try:
        first = await _create(
            client,
            session,
            key=str(uuid4()),
            scope={"type": "agent", "agentProfileId": agent_a},
            content="agent A private fact",
        )
        second = await _create(
            client,
            session,
            key=str(uuid4()),
            scope={"type": "agent", "agentProfileId": agent_b},
            content="agent B private fact",
        )
        unscoped = await client.get("/api/v1/memories")
        assert unscoped.status_code == 200
        assert {first["id"], second["id"]}.isdisjoint(
            {item["id"] for item in unscoped.json()["items"]}
        )
        agent_a_items = await client.get(
            "/api/v1/memories",
            params={"scopeType": "agent", "agentProfileId": agent_a},
        )
        assert [item["id"] for item in agent_a_items.json()["items"]] == [first["id"]]
        agent_b_items = await client.get(
            "/api/v1/memories",
            params={"scopeType": "agent", "agentProfileId": agent_b},
        )
        assert [item["id"] for item in agent_b_items.json()["items"]] == [second["id"]]
        wrong_agent = await client.get(
            "/api/v1/memories",
            params={"scopeType": "agent", "agentProfileId": agent_b, "q": "agent A"},
        )
        assert wrong_agent.json()["items"] == []
        assert (await client.get(f"/api/v1/memories/{first['id']}")).status_code == 404
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_correction_preserves_manual_provenance_and_reason(api_app: Any) -> None:
    client, session = await owner_client(api_app)
    try:
        memory = await _create(client, session, key=str(uuid4()))
        corrected = await client.post(
            f"/api/v1/memories/{memory['id']}/revisions",
            headers=_headers(session, key=str(uuid4())),
            json={
                "content": "The owner prefers detailed answers.",
                "reason": "owner correction reason",
                "expectedVersion": 1,
            },
        )
        assert corrected.status_code == 201, corrected.text
        payload = corrected.json()
        provenance = payload["provenance"]
        assert any(item["type"] == "manual" for item in provenance)
        assert payload["revisions"][-1]["correctionReason"] == "owner correction reason"
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_memory_q_rejects_overlong_terms_and_emits_all_declared_metrics(api_app: Any) -> None:
    client, session = await owner_client(api_app)
    try:
        too_long = await client.get("/api/v1/memories", params={"q": "x" * 501})
        assert too_long.status_code == 422
        accepted = await client.get("/api/v1/memories", params={"q": "x" * 500})
        assert accepted.status_code == 200
        await _create(client, session, key=str(uuid4()))
        measurements = [
            item
            for item in api_app.state.aura.metrics.snapshot()
            if item.component_id == "aura.knowledge.memory_persistence"
        ]
        metrics = {item.metric for item in measurements}
        assert {
            "memory_operation_duration_ms",
            "memory_operation_outcome",
            "memory_lifecycle_status",
            "memory_scope_type",
        } <= metrics
        for measurement in measurements:
            dimensions = dict(measurement.dimensions)
            if measurement.metric == "memory_scope_type":
                assert dimensions.get("scope_type") == "user"
            elif measurement.metric == "memory_lifecycle_status":
                assert dimensions.get("status") in {
                    "active",
                    "dormant",
                    "archived",
                    "disabled",
                    "disputed",
                    "superseded",
                    "unknown",
                }
            else:
                assert dimensions.get("dependency") in {"memory_store", "postgresql"}
        for trace_id in {item.trace_id for item in measurements if item.trace_id is not None}:
            correlated = {item.metric for item in measurements if item.trace_id == trace_id}
            assert {
                "memory_operation_duration_ms",
                "memory_operation_outcome",
                "memory_lifecycle_status",
                "memory_scope_type",
            } <= correlated
    finally:
        await client.aclose()
