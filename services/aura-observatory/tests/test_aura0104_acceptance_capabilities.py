"""Focused AURA-0104 tests for governed Observatory admission boundaries."""
# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportGeneralTypeIssues=false, reportArgumentType=false

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from aura_observatory.adapters.evaluation_definitions import InMemoryEvaluationDefinitionRepository
from aura_observatory.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    ObservatoryAuth,
    Principal,
)
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.evaluation_api import (
    EvaluationApplicationService,
    _validate_artifact_upload_metadata,
    observatory_public_evaluation_admission,
)
from aura_observatory.evaluation_definitions import DefinitionCommandPendingError
from aura_observatory.evaluation_judges import LocalJudgeDefinition
from aura_observatory.evaluation_query import EvaluationQueryError
from aura_observatory.evaluation_storage import ArtifactRecord, ArtifactStorageError
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.query import analytics_metric_catalogue
from aura_observatory.settings import Settings

ID = "00000000-0000-4000-8000-000000000101"
ID2 = "00000000-0000-4000-8000-000000000102"
DIGEST = "a" * 64
AT = "2026-10-23T00:00:00Z"


def _provenance() -> dict[str, object]:
    return {
        "dataset": {"resourceId": ID, "resourceVersion": "1.0.0"},
        "suite": {"resourceId": ID2, "resourceVersion": "1.0.0"},
        "evaluators": [{"evaluatorId": ID, "evaluatorVersion": "1.0.0", "kind": "local_judge"}],
        "components": [
            {
                "componentId": "aura.observatory.acceptance",
                "componentVersion": "1.0.0",
                "codeRevision": "fixture",
                "imageDigest": DIGEST,
                "configurationRevision": "fixture",
            }
        ],
        "model": {"modelId": "fixture", "modelVersion": "1.0.0", "modelDigest": DIGEST},
        "deploymentRevision": "fixture",
        "reproducibility": {
            "seed": 1,
            "clockMode": "fixed",
            "timeZone": "UTC",
            "locale": "en-US",
            "fixtureRevision": "1.0.0",
            "promptRevision": "1.0.0",
            "policyRevision": "1.0.0",
        },
    }


def _metadata() -> dict[str, object]:
    return {
        "artifactId": ID,
        "artifactVersion": "1.0.0",
        "purpose": "input",
        "mediaClassification": "evaluation_input",
        "mediaType": "text/plain",
        "byteCount": 5,
        "integrity": {"algorithm": "sha256", "digest": DIGEST},
        "captureAuthorization": {
            "captureClassification": "full_content",
            "authorizationPurpose": "synthetic_benchmark",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
            "authorizationRecordId": ID2,
            "authorizedAt": AT,
        },
        "retentionMode": "manual_only",
        "provenance": _provenance(),
        "createdAt": AT,
    }


class _ArtifactRepository:
    def __init__(self) -> None:
        self._lookups = 0

    async def lookup_authorization_metadata(self, _authorization: str, _dataset: str) -> object:
        return SimpleNamespace(
            dataset_version="1.0.0",
            capture_classification="full_content",
            authorization_purpose="synthetic_benchmark",
            authorization_context="controlled_evaluation",
            retention_mode="manual_only",
            revoked=False,
            status="authorized",
            request_id=ID,
            authorized_at=datetime.fromisoformat(AT.replace("Z", "+00:00")),
        )

    async def lookup_artifact_metadata(self, _reference: str) -> object:
        self._lookups += 1
        return SimpleNamespace(
            artifact_reference_id=ID,
            dataset_id=ID,
            authorization_record_id=ID2,
            sha256=DIGEST,
            byte_count=5,
            media_type="text/plain",
            capture_classification="full_content",
            authorization_purpose="synthetic_benchmark",
            revoked=False,
            artifact_version="1.0.0",
            authorization_context="controlled_evaluation",
            retention_mode="manual_only",
            media_classification="evaluation_input",
            intended_use="prompt" if self._lookups % 2 else "rubric",
            status="available",
        )


class _ArtifactStorage:
    repository = _ArtifactRepository()

    def __init__(self) -> None:
        self.received: bytearray = bytearray()

    async def store(self, request: Any, _stream: object) -> ArtifactRecord:
        async for chunk in _stream:  # type: ignore[union-attr]
            self.received.extend(chunk)
        value = request
        return ArtifactRecord(
            artifact_id=value.artifact_id,
            artifact_reference_id=value.artifact_reference_id,
            request_id=value.request_id,
            result_id=value.result_id,
            idempotency_key=value.idempotency_key,
            authorization_record_id=value.authorization_record_id,
            capture_classification=value.capture_classification,
            media_classification=value.media_classification,
            media_type=value.media_type,
            byte_count=value.max_bytes,
            sha256=value.expected_sha256,
            job_id=value.job_id,
            dataset_id=value.dataset_id,
            suite_id=value.suite_id,
            created_at=value.created_at,
            artifact_version=value.artifact_version,
            authorization_purpose=value.authorization_purpose,
            authorization_context=value.authorization_context,
            intended_use=value.intended_use or "input",
        )


class _AuthorizationMismatchStorage(_ArtifactStorage):
    async def store(self, request: Any, _stream: object) -> ArtifactRecord:
        del request
        raise ArtifactStorageError("authorization_mismatch")


class _JudgeRepository:
    async def create(self, definition: LocalJudgeDefinition) -> LocalJudgeDefinition:
        definition.validate()
        return definition


def test_recovery_metric_is_in_the_closed_catalogue() -> None:
    catalogue = analytics_metric_catalogue()
    names = {item["metric"] for item in catalogue["items"]}  # type: ignore[index]
    assert "evaluation_runner_nats_recovery_count" in names


def test_public_admission_evaluator_and_metric_span_are_executable() -> None:
    assert observatory_public_evaluation_admission(
        {
            "statusCode": 422,
            "authenticated": True,
            "csrfRejected": False,
            "noStore": True,
            "storageBytes": 0,
        }
    )
    metrics = ObservatoryMetrics()
    service = EvaluationApplicationService(metrics=metrics)
    service._record_admission_span(
        operation="artifact_admission",
        started=__import__("time").monotonic(),
        outcome="rejected",
        dependency="garage",
        error_code="authorization_required",
    )
    assert metrics.component_spans[-1].operation == "artifact_admission"
    assert any(
        key[0] == "observatory_evaluation_operation_duration_ms"
        and dict(key[1])["operation"] == "artifact_admission"
        for key in metrics.values
    )
    assert not observatory_public_evaluation_admission(
        {
            "statusCode": 201,
            "authenticated": True,
            "csrfRejected": True,
            "noStore": True,
            "storageBytes": 1,
        }
    )
    assert not observatory_public_evaluation_admission(
        {
            "statusCode": 422,
            "authenticated": False,
            "csrfRejected": False,
            "noStore": True,
            "storageBytes": 0,
        }
    )


def test_artifact_upload_metadata_is_closed_and_bounded() -> None:
    normalized = _validate_artifact_upload_metadata(_metadata())
    assert normalized["artifactId"] == ID
    invalid = _metadata()
    invalid["credential"] = "must not cross the boundary"
    with pytest.raises(EvaluationQueryError):
        _validate_artifact_upload_metadata(invalid)


def test_definition_command_accepts_local_judge_and_replays() -> None:
    repository = InMemoryEvaluationDefinitionRepository()

    async def exercise() -> None:
        claim = await repository.begin_definition_command(
            "create_local_judge_evaluator", ID, DIGEST, ID, "1.0.0"
        )
        assert claim.claimed is True
        assert claim.claim_token
        with pytest.raises(DefinitionCommandPendingError):
            await repository.begin_definition_command(
                "create_local_judge_evaluator", ID, DIGEST, ID, "1.0.0"
            )
        response = {"resourceType": "evaluator", "kind": "local_judge"}
        await repository.complete_definition_command(
            "create_local_judge_evaluator", ID, DIGEST, response, claim.claim_token
        )
        replay = await repository.begin_definition_command(
            "create_local_judge_evaluator", ID, DIGEST, ID, "1.0.0"
        )
        assert replay.replay == response

    import asyncio

    asyncio.run(exercise())


def test_credentials_are_rejected_in_json_shaped_text() -> None:
    from aura_observatory.evaluation_api import _contains_prohibited_content

    assert _contains_prohibited_content(b'{"nested":{"apiKey":"secret"}}', "text/plain")
    assert _contains_prohibited_content(b"AWS_SECRET_ACCESS_KEY=redacted", "text/plain")
    assert _contains_prohibited_content(
        b"-----BEGIN PRIVATE KEY-----\nredacted\n-----END PRIVATE KEY-----", "text/plain"
    )


@pytest.mark.asyncio
async def test_artifact_admission_uses_typed_storage_and_returns_no_bytes() -> None:
    storage = _ArtifactStorage()
    service = EvaluationApplicationService(
        artifact_storage=storage, artifact_metadata=storage.repository
    )

    async def stream():
        yield b"hello"

    result = await service.create_artifact(
        _metadata(), stream(), idempotency_key="artifact-command-0001"
    )
    assert result["artifactId"] == ID
    assert result["deletionStatus"] == "retained"
    assert "content" not in result


def _multipart(
    metadata: Mapping[str, object], content: bytes, *, filename: str = "fixture.txt"
) -> tuple[bytes, str]:
    boundary = "aura-boundary"
    metadata_bytes = json.dumps(metadata, separators=(",", ":")).encode()
    body = b"".join(
        (
            (
                f"--{boundary}\r\nContent-Disposition: form-data; name=\"metadata\"\r\n"
                "Content-Type: application/json\r\n\r\n"
            ).encode(),
            metadata_bytes,
            (
                f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"content\"; "
                f"filename=\"{filename}\"\r\nContent-Type: text/plain\r\n\r\n"
            ).encode(),
            content,
            f"\r\n--{boundary}--\r\n".encode(),
        )
    )
    return body, f"multipart/form-data; boundary={boundary}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    [
        b'{"password":"synthetic-value"}',
        b'{"token":"synthetic-value"}',
        b'{"pass\\u0077ord":"synthetic-value"}',
    ],
)
async def test_authenticated_multipart_rejects_credential_json_before_storage(
    content: bytes,
) -> None:
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    storage = _ArtifactStorage()
    service = EvaluationApplicationService(
        artifact_storage=storage, artifact_metadata=storage.repository
    )
    app = create_app(
        settings, auth=auth, query_store=None, evaluation_service=service, testing=True
    )
    body, media_type = _multipart(_metadata(), content)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/evaluation/artifacts",
            content=body,
            headers={
                "Content-Type": media_type,
                "Origin": settings.public_origin,
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "00000000-0000-4000-8000-000000000103",
            },
        )
    assert 400 <= response.status_code < 500
    assert storage.received == b""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extra_headers", "expected_status"),
    [
        ({"Idempotency-Key": "00000000-0000-4000-8000-000000000104"}, 403),
        ({"X-CSRF-Token": "placeholder"}, 400),
    ],
)
async def test_authenticated_admission_header_rejection_records_one_span(
    extra_headers: dict[str, str], expected_status: int
) -> None:
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    metrics = ObservatoryMetrics()
    storage = _ArtifactStorage()
    service = EvaluationApplicationService(
        artifact_storage=storage, artifact_metadata=storage.repository, metrics=metrics
    )
    app = create_app(
        settings, auth=auth, query_store=None, evaluation_service=service, testing=True
    )
    headers = {"Origin": settings.public_origin, **extra_headers}
    if "X-CSRF-Token" in extra_headers:
        headers["X-CSRF-Token"] = session.csrf_token
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/evaluation/artifacts",
            content=b"",
            headers=headers,
        )
    assert response.status_code == expected_status
    spans = [
        span
        for span in metrics.component_spans
        if span.operation == "artifact_admission"
    ]
    assert len(spans) == 1
    assert spans[0].outcome == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/api/v1/evaluation/artifacts", "artifact_admission"),
        (
            "/api/v1/evaluation/capture-authorizations",
            "capture_authorization_admission",
        ),
        ("/api/v1/evaluation/evaluators", "local_judge_admission"),
    ],
)
@pytest.mark.parametrize(
    "rejection_mode", ["unauthenticated", "invalid_csrf", "missing_idempotency"]
)
async def test_all_public_admission_routes_emit_one_session_span(
    path: str, operation: str, rejection_mode: str
) -> None:
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, _session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    metrics = ObservatoryMetrics()
    service = EvaluationApplicationService(metrics=metrics)
    app = create_app(
        settings, auth=auth, query_store=None, evaluation_service=service, testing=True
    )
    headers = {
        "Origin": settings.public_origin,
        "Idempotency-Key": "00000000-0000-4000-8000-000000000106",
    }
    cookies = None
    expected_status = 401
    expected_error = "authorization_required"
    if rejection_mode == "invalid_csrf":
        cookies = {settings.effective_session_cookie_name: session_id}
        headers["X-CSRF-Token"] = "invalid-csrf-token"
        expected_status = 403
    elif rejection_mode == "missing_idempotency":
        cookies = {settings.effective_session_cookie_name: session_id}
        headers["X-CSRF-Token"] = _session.csrf_token
        headers.pop("Idempotency-Key")
        expected_status = 400
        expected_error = "validation"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies=cookies,
    ) as client:
        response = await client.post(path, content=b"", headers=headers)
    assert response.status_code == expected_status
    spans = [span for span in metrics.component_spans if span.operation == operation]
    assert len(spans) == 1
    assert spans[0].dependency == "session_store"
    assert spans[0].error_code == expected_error


@pytest.mark.asyncio
async def test_artifact_authorization_mismatch_is_bounded_and_attributed_to_postgresql() -> None:
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    storage = _AuthorizationMismatchStorage()
    metrics = ObservatoryMetrics()
    service = EvaluationApplicationService(
        artifact_storage=storage,
        artifact_metadata=storage.repository,
        metrics=metrics,
    )
    app = create_app(
        settings, auth=auth, query_store=None, evaluation_service=service, testing=True
    )
    payload, content_type = _multipart(_metadata(), b"hello")
    headers = {
        "Origin": settings.public_origin,
        "Idempotency-Key": "00000000-0000-4000-8000-000000000107",
        "X-CSRF-Token": session.csrf_token,
        "Content-Type": content_type,
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/evaluation/artifacts", content=payload, headers=headers
        )
    assert response.status_code == 422
    spans = [span for span in metrics.component_spans if span.operation == "artifact_admission"]
    assert len(spans) == 1
    assert spans[0].dependency == "postgresql"
    assert spans[0].error_code == "authorization_mismatch"


@pytest.mark.asyncio
async def test_authenticated_benign_large_upload_replays_full_spool() -> None:
    payload = b"synthetic-observatory-fixture\n" * 2414
    assert len(payload) > 65_536
    metadata = _metadata()
    metadata["byteCount"] = len(payload)
    metadata["integrity"] = {
        "algorithm": "sha256",
        "digest": hashlib.sha256(payload).hexdigest(),
    }
    settings = Settings(owner_subject="owner-subject")
    auth = ObservatoryAuth(settings, MemorySessionBackend(), MemoryLoginStateBackend())
    session_id, session = await auth.create_session(
        Principal(
            "https://identity.example.invalid/application/o/aura-observatory/",
            "owner-subject",
            "Owner",
        )
    )
    storage = _ArtifactStorage()
    service = EvaluationApplicationService(
        artifact_storage=storage, artifact_metadata=storage.repository
    )
    app = create_app(
        settings, auth=auth, query_store=None, evaluation_service=service, testing=True
    )
    body, media_type = _multipart(metadata, payload)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url=settings.public_origin,
        cookies={settings.effective_session_cookie_name: session_id},
    ) as client:
        response = await client.post(
            "/api/v1/evaluation/artifacts",
            content=body,
            headers={
                "Content-Type": media_type,
                "Origin": settings.public_origin,
                "X-CSRF-Token": session.csrf_token,
                "Idempotency-Key": "00000000-0000-4000-8000-000000000105",
            },
        )
    assert response.status_code == 201
    assert bytes(storage.received) == payload
    assert response.json()["byteCount"] == len(payload)
    assert response.json()["digest"] == hashlib.sha256(payload).hexdigest()


@pytest.mark.asyncio
async def test_local_judge_admission_is_idempotent_and_closed() -> None:
    body = {
        "evaluatorId": ID,
        "evaluatorVersion": "1.0.0",
        "metric": "evaluation.local_judge.score",
        "componentIds": ["aura.observatory.acceptance"],
        "judgeDefinitionId": ID2,
        "judgeDefinitionVersion": "1.0.0",
        "model": {
            "provider": "local_ollama",
            "modelName": "llama3",
            "modelVersion": "1.0.0",
            "modelDigest": DIGEST,
        },
        "prompt": {
            "artifactId": ID,
            "artifactVersion": "1.0.0",
            "purpose": "judge_prompt",
            "digest": DIGEST,
        },
        "rubric": {
            "artifactId": ID,
            "artifactVersion": "1.0.0",
            "purpose": "judge_rubric",
            "digest": DIGEST,
        },
        "calibration": {"calibrationId": ID, "calibrationVersion": "1.0.0"},
        "seed": 1,
        "budget": {
            "timeoutMs": 1000,
            "maxInputBytes": 5,
            "maxOutputBytes": 64,
            "maxOutputTokens": 8,
            "maxContextTokens": 64,
            "maxConcurrency": 1,
            "temperature": 0,
        },
        "outputContract": "local_judge_score_v1",
        "defaultDisposition": "advisory_only",
        "retentionMode": "manual_only",
        "provenance": {
            "createdByComponent": "aura.observatory.acceptance",
            "createdByVersion": "1.0.0",
            "sourceDigest": DIGEST,
        },
        "createdAt": AT,
    }
    storage = _ArtifactStorage()
    service = EvaluationApplicationService(
        artifact_storage=storage,
        artifact_metadata=storage.repository,
        judge_definitions=_JudgeRepository(),
    )
    first = await service.create_local_judge_evaluator(body, idempotency_key="judge-command-0001")
    second = await service.create_local_judge_evaluator(body, idempotency_key="judge-command-0001")
    assert first == second
    assert first["kind"] == "local_judge"
