"""Independent AURA-0095 backend contract and live-stream regression tests."""

# ASGI and DTO fixtures intentionally cross dynamic transport boundaries.
# pyright: reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
import pytest
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.evaluation_api import (
    EvaluationApplicationService,
    _project_definition_replay,  # pyright: ignore[reportPrivateUsage] - test-only projector regression
)
from aura_observatory.evaluation_live import EvaluationLiveEventStore
from aura_observatory.evaluation_query import (
    EvaluationQueryError,
    EvaluationQueryService,
    EvaluationUnavailable,
    project_definition,
)
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.settings import Settings

OWNER = "owner-subject"
ISSUER = "https://identity.example.invalid/application/o/aura-observatory/"
DATASET = "00000000-0000-4000-8000-000000000101"
CASE = "00000000-0000-4000-8000-000000000102"
SUITE = "00000000-0000-4000-8000-000000000103"
EVALUATOR = "00000000-0000-4000-8000-000000000104"
JOB = "00000000-0000-4000-8000-000000000105"


async def _owner_client(*, testing: bool = True) -> tuple[httpx.AsyncClient, Any, Any, str, Any]:
    settings = Settings(owner_subject=OWNER)
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(Principal(ISSUER, OWNER, "Owner"))
    app = create_app(settings, auth=auth, testing=testing)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    )
    return client, app, auth, session_id, session


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


def _suite() -> dict[str, object]:
    return {
        "suiteId": SUITE,
        "suiteVersion": "1.0.0",
        "dataset": {"id": DATASET, "version": "1.0.0"},
        "evaluators": [
            {"evaluatorId": EVALUATOR, "evaluatorVersion": "1.0.0", "kind": "deterministic"}
        ],
        "components": [{"componentId": "aura.core", "componentVersion": "1.0.0"}],
        "limits": {"maxCases": 1, "timeoutMs": 1000},
    }


async def _read_sse(client: httpx.AsyncClient, **kwargs: object) -> str:
    response = await client.get("/api/v1/evaluation/events", **kwargs)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    return response.text


@pytest.mark.asyncio
async def test_dedicated_evaluation_sse_pages_more_than_one_thousand_events() -> None:
    client, app, _auth, _session_id, _session = await _owner_client()
    class PagingLive(EvaluationLiveEventStore):
        async def after(self, last_event_id: str | None, *, limit: int = 1000):
            bounded = max(1, min(1000, limit))
            if last_event_id is None:
                return list(self.events[:bounded])
            return await super().after(last_event_id, limit=bounded)

    live = PagingLive()
    app.state.evaluation.live = live
    for index in range(1001):
        await live.publish("evaluation_job", f"job-{index:04d}", "updated", "running")
    async with client:
        body = await _read_sse(client)
    ids = [line[4:] for line in body.splitlines() if line.startswith("id: ")]
    assert len(ids) == 1001
    assert len(set(ids)) == 1001
    assert "job-0000" in body
    assert "job-1000" in body
    assert "event: evaluation.changed" in body
    assert '"capturePolicy":"metadata_only"' in body


@pytest.mark.asyncio
async def test_evaluation_sse_emits_post_connect_event_and_bounded_heartbeat() -> None:
    client, app, auth, session_id, _session = await _owner_client(testing=False)
    metrics = ObservatoryMetrics()
    app.state.evaluation.metrics = metrics

    class ControlledLive(EvaluationLiveEventStore):
        waits = 0

        async def wait(self, timeout: float = 15.0) -> None:
            del timeout
            self.waits += 1
            if self.waits == 1:
                await self.publish("evaluation_job", JOB, "completed", "succeeded")
            else:
                # End the finite test stream after the event has been observed.
                await auth.revoke(session_id)

    live = ControlledLive()
    app.state.evaluation.live = live
    await live.publish("evaluation_job", "replayed-job", "created", "queued")
    async with client:
        body = await _read_sse(client)
    assert body.count(": heartbeat") == 1
    assert JOB in body
    assert '"capturePolicy":"metadata_only"' in body
    assert live.waits == 2
    spans = metrics.component_spans
    assert any(span.operation == "live_replay" and span.duration_ms >= 0 for span in spans)
    assert any(span.operation == "live" and span.duration_ms >= 0 for span in spans)


@pytest.mark.asyncio
async def test_evaluation_sse_reconnect_cursor_is_exclusive() -> None:
    client, app, _auth, _session_id, _session = await _owner_client()
    first = await app.state.evaluation.live.publish(
        "evaluation_job", "first", "created", "queued"
    )
    second = await app.state.evaluation.live.publish(
        "evaluation_job", "second", "created", "queued"
    )
    async with client:
        body = await _read_sse(client, headers={"Last-Event-ID": first.event_id})
    ids = [line[4:] for line in body.splitlines() if line.startswith("id: ")]
    assert ids == [second.event_id]
    assert "first" not in body


@pytest.mark.asyncio
async def test_evaluation_sse_stops_after_session_revocation() -> None:
    client, app, auth, session_id, _session = await _owner_client(testing=False)

    class RevokingLive(EvaluationLiveEventStore):
        async def wait(self, timeout: float = 15.0) -> None:
            del timeout
            await auth.revoke(session_id)

    app.state.evaluation.live = RevokingLive()
    async with client:
        body = await _read_sse(client)
    assert body == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "payload", "extra"),
    [
        ("/api/v1/evaluation/datasets", _dataset(), "datasetExtra"),
        ("/api/v1/evaluation/suites", _suite(), "suiteExtra"),
        (
            "/api/v1/evaluation/deletion-actions",
            {"commandId": DATASET, "requestedAt": "2026-10-21T00:00:00Z", "kind": "purge_all"},
            "deletionExtra",
        ),
    ],
)
async def test_mutating_contracts_reject_unknown_fields(
    path: str, payload: dict[str, object], extra: str
) -> None:
    client, _app, _auth, _session_id, session = await _owner_client()
    payload[extra] = "must-not-be-accepted"
    headers = {
        "X-CSRF-Token": session.csrf_token,
        "Idempotency-Key": f"unknown-{extra}-0001",
        "X-Observatory-Confirmation": "DELETE_EVALUATION_ARTIFACTS",
    }
    if path != "/api/v1/evaluation/deletion-actions":
        headers.pop("X-Observatory-Confirmation")
    async with client:
        response = await client.post(path, json=payload, headers=headers)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_definition_nested_unknown_fields_are_rejected_before_domain_admission() -> None:
    class SpyDefinitionService:
        dataset_calls = 0

        async def admit_dataset(self, _payload: object) -> object:
            self.dataset_calls += 1
            raise AssertionError("invalid transport reached domain admission")

    spy = SpyDefinitionService()
    service = EvaluationApplicationService(definition_service=spy)
    invalid = _dataset()
    invalid["cases"] = [
        {
            "caseId": CASE,
            "caseVersion": "1.0.0",
            "artifactIds": [],
            "unexpected": "must-not-cross-boundary",
        }
    ]
    with pytest.raises(EvaluationQueryError):
        await service.create_definition("dataset", invalid)
    assert spy.dataset_calls == 0


def test_definition_projection_rejects_nested_scalar_payloads() -> None:
    with pytest.raises(EvaluationQueryError):
        project_definition(
            {
                "resourceType": "evaluator",
                "evaluatorId": EVALUATOR,
                "evaluatorVersion": "1.0.0",
                "metric": {"private": "must-not-cross-boundary"},
            }
        )


def test_definition_projection_rejects_legacy_missing_authoritative_facts() -> None:
    with pytest.raises(EvaluationQueryError):
        project_definition(
            {
                "resourceType": "dataset",
                "datasetId": DATASET,
                "datasetVersion": "1.0.0",
                "sourceKind": "synthetic",
                "scopeIds": ["private"],
                "caseIds": [CASE],
                "captureClassification": "metadata_only",
                "createdAt": "2026-01-01T00:00:00Z",
                "provenance": {
                    "createdByComponent": "aura.observatory.test",
                    "createdByVersion": "1.0.0",
                    "sourceDigest": "a" * 64,
                },
            }
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/evaluation/jobs?status=completed",
        "/api/v1/evaluation/results?status=queued",
        "/api/v1/evaluation/evaluators?kind=cloud_judge",
        "/api/v1/evaluation/artifacts?deletion_status=destroyed",
        "/api/v1/evaluation/jobs?private=secret",
        "/api/v1/evaluation/jobs?status=queued&status=failed",
    ],
)
async def test_collection_filters_are_closed_and_contract_bounded(path: str) -> None:
    client, _app, _auth, _session_id, _session = await _owner_client()
    async with client:
        response = await client.get(path)
    assert response.status_code == 400


def test_evaluation_api_metrics_are_closed_and_content_free() -> None:
    metrics = ObservatoryMetrics()
    service = EvaluationApplicationService(metrics=metrics)
    with metrics.trace_context("trace-evaluation-definition"):
        service.record_api_metric("success", operation="read")
        service.record_live_metric("replay", "success")
        service.record_live_replay_metric(datetime.now(UTC).isoformat().replace("+00:00", "Z"))
        for phase, outcome, error_code in (
            ("definition_claim", "success", None),
            ("definition_takeover", "success", None),
            ("definition_renewal", "success", None),
            ("definition_replay", "duplicate", None),
            ("definition_completion", "unavailable", "lease_lost"),
        ):
            service.record_definition_command_ledger(
                monotonic() - 0.001,
                outcome,
                phase=phase,
                error_code=error_code,
            )
        service.record_binding_validation("accepted", started=monotonic() - 0.001)
        service.record_binding_validation(
            "invalid", started=monotonic() - 0.001, error_code="validation"
        )
        service.record_binding_validation(
            "not_found", started=monotonic() - 0.001, error_code="not_found"
        )
        service.record_binding_validation(
            "unavailable", started=monotonic() - 0.001, error_code="database_failure"
        )
    names = {entry["name"] for entry in metrics.snapshot().values()}
    assert names >= {
        "observatory_evaluation_api_operation_count",
        "observatory_evaluation_live_event_count",
        "observatory_evaluation_live_replay_lag_seconds",
    }
    snapshot = metrics.snapshot()
    assert any(
        entry["name"] == "observatory_evaluation_dependency_outcome_count"
        and entry["labels"].get("operation") == "evaluation_definition_state"
        and entry["labels"].get("outcome") == "duplicate"
        and entry["labels"].get("phase") == "definition_replay"
        for entry in snapshot.values()
    )
    assert any(
        entry["name"] == "observatory_evaluation_definition_validation_count"
        and entry["labels"].get("outcome") == "not_found"
        for entry in snapshot.values()
    )
    definition_spans = [
        span
        for span in metrics.component_spans
        if span.component_id == "aura.observatory.evaluation_definitions"
    ]
    assert {span.operation for span in definition_spans} >= {
        "definition_claim",
        "definition_takeover",
        "definition_renewal",
        "definition_replay",
        "definition_completion",
        "definition_binding_validation",
    }
    assert all(span.trace_id == "trace-evaluation-definition" for span in definition_spans)
    assert all(span.duration_ms >= 0 for span in definition_spans)
    assert any(
        span.operation == "definition_completion"
        and span.outcome == "unavailable"
        and span.error_code == "lease_lost"
        for span in definition_spans
    )
    assert {
        span.outcome
        for span in definition_spans
        if span.operation == "definition_binding_validation"
    } == {"success", "failure", "unavailable"}
    assert len(
        [
            span
            for span in definition_spans
            if span.operation == "definition_binding_validation"
        ]
    ) == 4
    with pytest.raises(ValueError):
        metrics.record(
            "observatory_evaluation_api_operation_count",
            labels={"operation": "private", "outcome": "success"},
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/evaluation/datasets",
        "/api/v1/evaluation/suites",
        "/api/v1/evaluation/jobs",
        "/api/v1/evaluation/reviews",
        "/api/v1/evaluation/qualifications",
        "/api/v1/evaluation/deletion-actions",
        f"/api/v1/evaluation/jobs/{JOB}/cancel",
        f"/api/v1/evaluation/qualifications/{EVALUATOR}/revoke",
    ],
)
async def test_every_evaluation_command_requires_owner_session(path: str) -> None:
    settings = Settings(owner_subject=OWNER)
    app = create_app(settings, testing=True)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=settings.public_origin
    ) as client:
        response = await client.post(path, json={})
    assert response.status_code == 401


def test_known_projection_is_closed_and_content_free() -> None:
    projection = project_definition(
        {
            "resourceType": "dataset",
            "datasetId": DATASET,
            "datasetVersion": "1.0.0",
            "sourceKind": "synthetic",
                "scopeIds": ["private"],
                "caseIds": [CASE],
                "caseCount": 1,
                "captureClassification": "metadata_only",
                "createdAt": "2026-01-01T00:00:00Z",
                "provenance": {
                "createdByComponent": "aura.observatory.test",
                "createdByVersion": "1.0.0",
                "sourceDigest": "a" * 64,
                "arbitraryPrivateField": "must-not-cross-boundary",
            },
        }
    )
    assert set(projection) == {
        "schemaVersion",
        "resourceType",
        "datasetId",
        "datasetVersion",
        "sourceKind",
        "scopeIds",
        "captureClassification",
        "caseCount",
        "retentionMode",
        "provenance",
        "state",
        "createdAt",
    }
    assert set(projection["provenance"]) == {
        "createdByComponent",
        "createdByVersion",
        "sourceDigest",
    }
    encoded = json.dumps(projection).casefold()
    assert all(
        value not in encoded
        for value in ("prompt", "response", "credential", "endpoint", "owner")
    )


def test_definition_replay_is_closed_and_bound_to_durable_ledger() -> None:
    replay = project_definition(
        {
            "resourceType": "dataset",
            "datasetId": DATASET,
            "datasetVersion": "1.0.0",
            "sourceKind": "synthetic",
            "scopeIds": ["private"],
            "caseIds": [CASE],
            "caseCount": 1,
            "captureClassification": "metadata_only",
            "createdAt": "2026-01-01T00:00:00Z",
            "provenance": {
                "createdByComponent": "aura.observatory.test",
                "createdByVersion": "1.0.0",
                "sourceDigest": "a" * 64,
            },
        }
    )
    assert _project_definition_replay("dataset", replay, DATASET, "1.0.0") == replay
    tampered = dict(replay)
    tampered["private"] = "must-not-cross-boundary"
    with pytest.raises(EvaluationUnavailable):
        _project_definition_replay("dataset", tampered, DATASET, "1.0.0")
    with pytest.raises(EvaluationUnavailable):
        _project_definition_replay(
            "dataset", replay, "00000000-0000-4000-8000-000000000999", "1.0.0"
        )


@pytest.mark.asyncio
async def test_unavailable_query_provider_is_reported_as_retryable_503() -> None:
    class UnavailableBackend:
        async def collection(self, *args: object, **kwargs: object) -> dict[str, object]:
            del args, kwargs
            raise EvaluationUnavailable("provider down")

        async def get(self, *args: object, **kwargs: object) -> None:
            del args, kwargs
            raise EvaluationUnavailable("provider down")

        async def history_page(self, **kwargs: object) -> dict[str, object]:
            del kwargs
            raise EvaluationUnavailable("provider down")

    client, app, _auth, _session_id, _session = await _owner_client()
    app.state.evaluation.query = EvaluationQueryService(backend=UnavailableBackend())  # type: ignore[arg-type]
    async with client:
        response = await client.get("/api/v1/evaluation/datasets")
    assert response.status_code == 503
    envelope = response.json()
    assert set(envelope) == {"code", "message", "retryable", "traceId"}
    assert envelope["code"] == "service_unavailable"
    assert envelope["message"] == "service unavailable"
    assert envelope["retryable"] is True
    assert envelope["traceId"]
    assert "provider down" not in response.text


def test_cli_exposes_no_provider_or_owner_credentials() -> None:
    from aura_observatory.entrypoints.cli import app
    from typer.testing import CliRunner

    result = CliRunner().invoke(app, ["evaluation", "list", "--help"])
    assert result.exit_code == 0
    help_text = result.stdout.casefold()
    assert all(
        value not in help_text for value in ("--token", "--password", "--endpoint", "--owner")
    )


def test_observatory_provider_boundary_does_not_import_core() -> None:
    root = Path(__file__).parents[1] / "src" / "aura_observatory"
    forbidden = []
    for path in root.rglob("*.py"):
        if "aura_core" in path.read_text(encoding="utf-8"):
            forbidden.append(str(path.relative_to(root)))
    assert forbidden == []


@pytest.mark.asyncio
async def test_cursor_history_and_filters_are_bounded_and_ordered() -> None:
    service = EvaluationApplicationService()
    first = service.query.store.put(
        "datasets",
        {
            "datasetId": DATASET,
            "datasetVersion": "1.0.0",
            "resourceType": "dataset",
            "createdAt": "2026-10-20T00:00:00Z",
        },
    )
    second = service.query.store.put(
        "datasets",
        {
            "datasetId": "00000000-0000-4000-8000-000000000106",
            "datasetVersion": "2.0.0",
            "resourceType": "dataset",
            "createdAt": "2026-10-21T00:00:00Z",
        },
    )
    del first, second
    page = await service.query.collection_async("datasets", cursor=None, limit=1)
    assert len(page["items"]) == 1
    assert page["nextCursor"] is not None
    following = await service.query.collection_async(
        "datasets", cursor=str(page["nextCursor"]), limit=1
    )
    assert len(following["items"]) == 1
    assert following["items"][0]["datasetVersion"] == "1.0.0"
    assert following["nextCursor"] is None


@pytest.mark.asyncio
async def test_live_event_deletion_keeps_content_free_ledger_notifications() -> None:
    live = EvaluationLiveEventStore()
    requested = await live.publish("evaluation_deletion", DATASET, "requested", "queued")
    completed = await live.publish("evaluation_deletion", DATASET, "completed", "succeeded")
    events = await live.after(requested.event_id)
    assert [event.event_id for event in events] == [completed.event_id]
    assert events[0].as_sse()["capturePolicy"] == "metadata_only"
    assert all(key not in events[0].as_sse() for key in ("content", "artifact", "credential"))


@pytest.mark.asyncio
async def test_live_event_wait_is_bounded() -> None:
    live = EvaluationLiveEventStore()
    started = asyncio.get_running_loop().time()
    await live.wait(timeout=0.01)
    assert asyncio.get_running_loop().time() - started < 1
