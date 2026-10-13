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
    assert spec["info"]["version"] == "1.13.0"
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


def test_recurring_reminder_contract_is_additive_and_keeps_one_shot_wire_shape() -> None:
    spec = _openapi()
    paths = cast(dict[str, Any], spec["paths"])
    expected_paths = {
        "/api/v1/reminder-series",
        "/api/v1/reminder-series/{series_id}",
        "/api/v1/reminder-series/{series_id}/occurrences",
        "/api/v1/reminder-series/{series_id}/update",
        "/api/v1/reminder-series/{series_id}/pause",
        "/api/v1/reminder-series/{series_id}/resume",
        "/api/v1/reminder-series/{series_id}/skip-next",
        "/api/v1/reminder-series/{series_id}/cancel",
    }
    assert expected_paths <= set(paths)
    assert {"get", "post"} <= set(paths["/api/v1/reminder-series"])
    for path in expected_paths - {"/api/v1/reminder-series"}:
        path_item = cast(dict[str, Any], paths[path])
        operation = next(
            (
                cast(dict[str, Any], value)
                for value in path_item.values()
                if isinstance(value, dict)
            ),
            cast(dict[str, Any], {}),
        )
        operation_dict = operation
        assert operation_dict.get("security") != []
        if path.endswith(("/update", "/pause", "/resume", "/skip-next", "/cancel")):
            refs = {
                item["$ref"].rsplit("/", 1)[-1]
                for item in operation_dict.get("parameters", [])
                if "$ref" in item
            }
            assert {"CsrfToken", "IdempotencyKey"} <= refs

    schemas = cast(dict[str, Any], spec["components"]["schemas"])
    reminder = cast(dict[str, Any], schemas["Reminder"])
    assert set(reminder["required"]) == {
        "id",
        "content",
        "dueAt",
        "status",
        "version",
        "createdAt",
        "updatedAt",
        "firedAt",
        "completedAt",
        "canceledAt",
    }
    assert "seriesId" not in reminder["properties"]
    assert "nextReminderId" not in reminder["properties"]
    recurrence = cast(dict[str, Any], schemas["RecurrenceRule"])
    frequency = cast(dict[str, Any], recurrence["properties"]["frequency"])
    if "$ref" in frequency:
        frequency = cast(dict[str, Any], schemas[frequency["$ref"].rsplit("/", 1)[-1]])
    assert set(frequency["enum"]) == {
        "daily",
        "selected_weekday",
        "weekly",
        "monthly",
    }
    assert recurrence["properties"]["interval"]["minimum"] == 1
    assert recurrence["additionalProperties"] is False
    series_recurrence = cast(dict[str, Any], schemas["ReminderSeriesRecurrence"])
    assert {
        "frequency",
        "interval",
        "weekdays",
        "monthDay",
        "localStart",
        "timeZone",
        "count",
        "endDate",
    } <= set(series_recurrence["properties"])
    series_ref = schemas["ReminderSeries"]["properties"]["recurrence"]["$ref"]
    assert series_ref.endswith("ReminderSeriesRecurrence")
    assert set(schemas["ReminderOccurrence"]["properties"]["outcome"]["enum"]) == {
        "scheduled",
        "fired",
        "completed",
        "skipped",
        "replaced",
        "paused-window",
        "series-canceled",
    }


def _series_input(*, count: int | None = 3, timezone: str = "Europe/Brussels") -> dict[str, Any]:
    local_start = datetime.now(UTC).astimezone().replace(tzinfo=None) + timedelta(days=2)
    return {
        "content": "  water the plants  ",
        "recurrence": {
            "frequency": "daily",
            "interval": 1,
            "weekdays": [],
            "monthDay": None,
            "localStart": local_start.isoformat(timespec="minutes"),
            "timeZone": timezone,
            "count": count,
            "endDate": None,
        },
    }


@pytest.mark.asyncio
async def test_series_api_is_owner_scoped_paginated_and_preserves_one_shot_payload(
    reminder_app: FastAPI,
) -> None:
    client, csrf = await _client(reminder_app)
    anonymous = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reminder_app), base_url=ORIGIN
    )
    assert (await anonymous.get("/api/v1/reminder-series")).status_code == 401
    await anonymous.aclose()
    series_headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}
    first = await client.post(
        "/api/v1/reminder-series", json=_series_input(), headers=series_headers
    )
    assert first.status_code == 201, first.text
    first_item = first.json()
    assert {
        "id",
        "content",
        "recurrence",
        "readableRule",
        "timeZone",
        "localStart",
        "nextOccurrence",
        "nextReminderId",
        "status",
        "version",
        "missedCount",
        "count",
        "endDate",
        "createdAt",
        "updatedAt",
    } <= set(first_item)
    assert first_item["status"] == "active"
    assert first_item["nextReminderId"]
    second = await client.post(
        "/api/v1/reminder-series",
        json={**_series_input(count=None), "content": "read the meter"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert second.status_code == 201

    page = await client.get("/api/v1/reminder-series?limit=1")
    assert page.status_code == 200
    payload = page.json()
    assert len(payload["items"]) == 1
    assert payload["nextCursor"]
    assert not payload["nextCursor"].startswith("{")
    next_page = await client.get(
        "/api/v1/reminder-series", params={"limit": 1, "cursor": payload["nextCursor"]}
    )
    assert next_page.status_code == 200
    assert len(next_page.json()["items"]) == 1
    assert next_page.json()["items"][0]["id"] != payload["items"][0]["id"]

    detail = await client.get(f"/api/v1/reminder-series/{first_item['id']}")
    assert detail.status_code == 200
    occurrences = await client.get(f"/api/v1/reminder-series/{first_item['id']}/occurrences")
    assert occurrences.status_code == 200
    assert occurrences.json()["items"][0]["outcome"] == "scheduled"

    foreign = uuid4()
    assert (await client.get(f"/api/v1/reminder-series/{foreign}")).status_code == 404
    assert (
        await client.get(f"/api/v1/reminder-series/{foreign}/occurrences")
    ).status_code == 404

    one_shot = await client.post(
        "/api/v1/reminders",
        json={
            "content": "one-shot compatibility",
            "dueAt": (datetime.now(UTC) + timedelta(days=3)).isoformat(),
        },
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert one_shot.status_code == 201
    assert set(one_shot.json()) == {
        "id",
        "content",
        "dueAt",
        "status",
        "version",
        "createdAt",
        "updatedAt",
        "firedAt",
        "completedAt",
        "canceledAt",
    }
    await client.aclose()


@pytest.mark.asyncio
async def test_series_and_occurrence_pagination_remain_opaque_above_the_page_limit(
    reminder_app: FastAPI,
) -> None:
    """The public collection contracts must not silently cap at one page."""

    client, csrf = await _client(reminder_app)
    for index in range(101):
        response = await client.post(
            "/api/v1/reminder-series",
            json={**_series_input(count=None), "content": f"series pagination {index}"},
            headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 201, response.text

    first_page = await client.get("/api/v1/reminder-series?limit=100")
    assert first_page.status_code == 200
    first_payload = first_page.json()
    assert len(first_payload["items"]) == 100
    assert first_payload["nextCursor"]
    assert not first_payload["nextCursor"].startswith("{")
    second_page = await client.get(
        "/api/v1/reminder-series",
        params={"limit": 100, "cursor": first_payload["nextCursor"]},
    )
    assert second_page.status_code == 200
    second_payload = second_page.json()
    assert len(second_payload["items"]) == 1
    assert second_payload["nextCursor"] is None
    assert {
        item["id"] for item in first_payload["items"]
    }.isdisjoint(item["id"] for item in second_payload["items"])
    tampered = await client.get(
        "/api/v1/reminder-series",
        params={"limit": 100, "cursor": first_payload["nextCursor"] + "x"},
    )
    assert tampered.status_code == 422

    await client.aclose()


@pytest.mark.asyncio
async def test_occurrence_pagination_remains_opaque_above_the_page_limit(
    reminder_app: FastAPI,
) -> None:
    """Occurrence history uses the same bounded, opaque pagination contract."""

    client, csrf = await _client(reminder_app)
    occurrence_create = await client.post(
        "/api/v1/reminder-series",
        json={**_series_input(count=None), "content": "occurrence pagination"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert occurrence_create.status_code == 201, occurrence_create.text
    occurrence_series = occurrence_create.json()
    next_due = datetime.fromisoformat(occurrence_series["nextOccurrence"])
    repository = reminder_app.state.aura.reminder_repository
    for day in range(105):
        await repository.fire_due(now=next_due + timedelta(days=day), limit=100)

    occurrence_page = await client.get(
        f"/api/v1/reminder-series/{occurrence_series['id']}/occurrences?limit=100"
    )
    assert occurrence_page.status_code == 200
    occurrence_payload = occurrence_page.json()
    assert len(occurrence_payload["items"]) == 100
    assert occurrence_payload["nextCursor"]
    tampered = await client.get(
        f"/api/v1/reminder-series/{occurrence_series['id']}/occurrences",
        params={"limit": 100, "cursor": occurrence_payload["nextCursor"] + "x"},
    )
    assert tampered.status_code == 422
    occurrence_tail = await client.get(
        f"/api/v1/reminder-series/{occurrence_series['id']}/occurrences",
        params={"limit": 100, "cursor": occurrence_payload["nextCursor"]},
    )
    assert occurrence_tail.status_code == 200
    assert len(occurrence_tail.json()["items"]) >= 1
    assert occurrence_tail.json()["nextCursor"] is None
    await client.aclose()


@pytest.mark.asyncio
async def test_series_mutations_enforce_csrf_origin_uuid_replay_and_expected_version(
    reminder_app: FastAPI,
) -> None:
    client, csrf = await _client(reminder_app)
    payload = _series_input()
    key = str(uuid4())
    headers = {"X-CSRF-Token": csrf, "Idempotency-Key": key}
    created = await client.post("/api/v1/reminder-series", json=payload, headers=headers)
    assert created.status_code == 201
    series = created.json()

    missing_csrf = await client.post(
        f"/api/v1/reminder-series/{series['id']}/pause",
        json={"expectedVersion": series["version"]},
        headers={"Idempotency-Key": str(uuid4())},
    )
    assert missing_csrf.status_code == 403
    wrong_origin = await client.post(
        f"/api/v1/reminder-series/{series['id']}/pause",
        json={"expectedVersion": series["version"]},
        headers={**headers, "Origin": "https://evil.example", "Idempotency-Key": str(uuid4())},
    )
    assert wrong_origin.status_code == 403
    invalid_uuid = await client.post(
        f"/api/v1/reminder-series/{series['id']}/pause",
        json={"expectedVersion": series["version"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": "not-a-uuid"},
    )
    assert invalid_uuid.status_code == 422

    pause_key = str(uuid4())
    pause = await client.post(
        f"/api/v1/reminder-series/{series['id']}/pause",
        json={"expectedVersion": series["version"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": pause_key},
    )
    assert pause.status_code == 200
    replay = await client.post(
        f"/api/v1/reminder-series/{series['id']}/pause",
        json={"expectedVersion": series["version"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": pause_key},
    )
    assert replay.status_code == 200
    assert replay.json() == pause.json()
    conflicting_reuse = await client.post(
        f"/api/v1/reminder-series/{series['id']}/resume",
        json={"expectedVersion": pause.json()["version"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": pause_key},
    )
    assert conflicting_reuse.status_code == 409
    stale = await client.post(
        f"/api/v1/reminder-series/{series['id']}/resume",
        json={"expectedVersion": series["version"]},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert stale.status_code == 409
    await client.aclose()


@pytest.mark.asyncio
async def test_series_api_rejects_invalid_recurrence_timezone_and_termination(
    reminder_app: FastAPI,
) -> None:
    client, csrf = await _client(reminder_app)
    base_headers = {"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())}
    for payload in (
        {
            **_series_input(),
            "recurrence": {**_series_input()["recurrence"], "timeZone": "Mars/Olympus"},
        },
        {
            **_series_input(),
            "recurrence": {**_series_input()["recurrence"], "frequency": "daily", "weekdays": [1]},
        },
        {
            **_series_input(),
            "recurrence": {
                **_series_input()["recurrence"],
                "frequency": "monthly",
                "monthDay": None,
            },
        },
        {
            **_series_input(),
            "recurrence": {**_series_input()["recurrence"], "count": 2, "endDate": "2099-01-01"},
        },
    ):
        response = await client.post(
            "/api/v1/reminder-series",
            json={**payload, "content": "invalid series"},
            headers={**base_headers, "Idempotency-Key": str(uuid4())},
        )
        assert response.status_code == 422, response.text

    bounded_count = await client.post(
        "/api/v1/reminder-series",
        json={**_series_input(count=3), "content": "bounded by count"},
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert bounded_count.status_code == 201, bounded_count.text
    assert bounded_count.json()["count"] == 3
    assert bounded_count.json()["endDate"] is None
    bounded_date = await client.post(
        "/api/v1/reminder-series",
        json={
            **_series_input(count=None),
            "content": "bounded by end date",
            "recurrence": {
                **_series_input(count=None)["recurrence"],
                "endDate": "2099-01-01",
            },
        },
        headers={"X-CSRF-Token": csrf, "Idempotency-Key": str(uuid4())},
    )
    assert bounded_date.status_code == 201, bounded_date.text
    assert bounded_date.json()["count"] is None
    assert bounded_date.json()["endDate"] == "2099-01-01"
    await client.aclose()


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
