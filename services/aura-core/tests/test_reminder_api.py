"""Public API and contract acceptance tests for reminder management."""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from aura_core.domains.automation.reminders import repository as reminder_repository
from aura_core.entrypoints.api.app import create_app
from aura_core.platform.auth import Principal, Settings
from fastapi import FastAPI

REPOSITORY = Path(__file__).parents[3]
OPENAPI_PATH = REPOSITORY / "contracts/openapi/aura-v1.yaml"
ISSUER = "https://authentik.dev.example"
OWNER = "owner-subject"
ORIGIN = "https://aura.dev.example"


@pytest.fixture
def reminder_app() -> FastAPI:
    return create_app(
        Settings(
            public_origin=ORIGIN,
            oidc_issuer=ISSUER,
            oidc_client_id="aura-web",
            oidc_audience="aura-web",
            oidc_redirect_uri=f"{ORIGIN}/api/v1/auth/callback",
            owner_subject=OWNER,
            default_model="chat",
            secure_cookies=False,
        ),
        testing=True,
    )


async def _client(app: FastAPI, *, subject: str = OWNER) -> tuple[httpx.AsyncClient, str]:
    session_id, session = await app.state.aura.sessions.create(
        Principal(ISSUER, subject, "Test owner")
    )
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=ORIGIN,
        headers={"Origin": ORIGIN},
    )
    client.cookies.set(app.state.aura.settings.session_cookie.name, session_id)
    return client, session.csrf_token


def _openapi() -> dict[str, Any]:
    return json.loads(OPENAPI_PATH.read_text(encoding="utf-8"))


def test_sql_advisory_lock_key_is_deterministic_unambiguous_and_nul_free() -> None:
    make_lock_key = cast(
        Callable[[str, str, str], str],
        reminder_repository._idempotency_lock_key,  # pyright: ignore[reportPrivateUsage]
    )
    commands = (
        ("issuer\x00one", "subject", "request"),
        ("issuer", "subject", "request"),
        ("issuer", "subject", "request\x00suffix"),
        ("issuer", "subject\x00suffix", "request"),
        ("issuer:subject", "request", "id"),
        ("issuer", "subject:request", "id"),
    )
    keys = [make_lock_key(*command) for command in commands]

    # The same owner command must serialize on the same advisory lock, while
    # owner and idempotency components remain independently partitioned even
    # when values contain delimiter-like or PostgreSQL-incompatible bytes.
    assert keys[0] == make_lock_key(*commands[0])
    assert len(set(keys)) == len(commands)
    assert all("\x00" not in key for key in keys)


def test_reminder_contract_is_versioned_owner_authenticated_and_mutations_are_protected() -> None:
    spec = _openapi()
    assert spec["info"]["version"] == "1.9.0"
    expected = {
        "/api/v1/reminders",
        "/api/v1/reminders/{reminder_id}",
        "/api/v1/reminders/{reminder_id}/complete",
        "/api/v1/reminders/{reminder_id}/cancel",
    }
    assert expected <= set(spec["paths"])
    for path in expected:
        for operation in spec["paths"][path].values():
            if isinstance(operation, dict):
                assert cast(dict[str, Any], operation).get("security") != []
    for suffix in ("complete", "cancel"):
        operation = cast(
            dict[str, Any], spec["paths"][f"/api/v1/reminders/{{reminder_id}}/{suffix}"]["post"]
        )
        refs = {
            item["$ref"].rsplit("/", 1)[-1] for item in operation["parameters"] if "$ref" in item
        }
        assert {"CsrfToken", "IdempotencyKey"} <= refs
        body = operation["requestBody"]["content"]["application/json"]["schema"]
        assert body["$ref"].endswith("ReminderTransitionRequest")
    create = spec["paths"]["/api/v1/reminders"]["post"]
    refs = {item["$ref"].rsplit("/", 1)[-1] for item in create["parameters"] if "$ref" in item}
    assert {"CsrfToken", "IdempotencyKey"} <= refs


@pytest.mark.asyncio
async def test_create_list_get_and_lifecycle_api_require_session_csrf_origin_and_uuid_idempotency(
    reminder_app: FastAPI,
) -> None:
    anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=reminder_app), base_url=ORIGIN)
    response = await anonymous.get("/api/v1/reminders")
    assert response.status_code == 401
    await anonymous.aclose()

    client, csrf = await _client(reminder_app)
    due = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    payload = {"content": "  take medication  ", "dueAt": due}
    headers = {"Idempotency-Key": str(uuid4()), "X-CSRF-Token": csrf}
    missing_csrf = await client.post(
        "/api/v1/reminders",
        json=payload,
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert missing_csrf.status_code == 403
    wrong_origin = await client.post(
        "/api/v1/reminders",
        json=payload,
        headers={**headers, "Origin": "https://evil.example"},
    )
    assert wrong_origin.status_code == 403
    created = await client.post("/api/v1/reminders", json=payload, headers=headers)
    assert created.status_code == 201, created.text
    item = created.json()
    assert item["content"] == "take medication"
    assert item["status"] == "scheduled"
    assert item["dueAt"].endswith("+00:00")
    assert "issuer" not in item and "subject" not in item

    replay = await client.post("/api/v1/reminders", json=payload, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == item
    conflicting_reuse = await client.post(
        "/api/v1/reminders",
        json={**payload, "content": "different command"},
        headers=headers,
    )
    assert conflicting_reuse.status_code == 409
    listed = await client.get("/api/v1/reminders?view=upcoming")
    assert listed.status_code == 200
    assert listed.json()["items"] == [item]
    inspected = await client.get(f"/api/v1/reminders/{item['id']}")
    assert inspected.status_code == 200 and inspected.json() == item

    completed = await client.post(
        f"/api/v1/reminders/{item['id']}/complete",
        json={"expectedVersion": 1},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert completed.status_code == 200
    assert completed.json()["status"] == "completed"
    history = await client.get("/api/v1/reminders?view=history")
    assert history.status_code == 200 and history.json()["items"][0]["status"] == "completed"
    metrics = reminder_app.state.aura.metrics.snapshot()
    assert {item.metric for item in metrics} >= {
        "reminder_command_duration_ms",
        "reminder_command_outcome",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_api_rejects_naive_or_past_time_content_and_transition_conflicts(
    reminder_app: FastAPI,
) -> None:
    client, csrf = await _client(reminder_app)
    base_headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}
    for content, due_at in (
        ("ok", datetime.now(UTC).isoformat().replace("+00:00", "")),
        ("ok", (datetime.now(UTC) - timedelta(minutes=1)).isoformat()),
        ("   ", (datetime.now(UTC) + timedelta(days=1)).isoformat()),
    ):
        response = await client.post(
            "/api/v1/reminders", json={"content": content, "dueAt": due_at}, headers=base_headers
        )
        assert response.status_code == 422

    created = await client.post(
        "/api/v1/reminders",
        json={"content": "versioned", "dueAt": (datetime.now(UTC) + timedelta(days=1)).isoformat()},
        headers={**base_headers, "Idempotency-Key": str(uuid4())},
    )
    item = created.json()
    conflict = await client.post(
        f"/api/v1/reminders/{item['id']}/complete",
        json={"expectedVersion": 99},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert conflict.status_code == 409
    await client.aclose()


@pytest.mark.asyncio
async def test_api_rejects_authenticated_session_for_a_different_owner(
    reminder_app: FastAPI,
) -> None:
    client, _ = await _client(reminder_app, subject="different-owner")
    response = await client.get("/api/v1/reminders")
    assert response.status_code == 403
    await client.aclose()
