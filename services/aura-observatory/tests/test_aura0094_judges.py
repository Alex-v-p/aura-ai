"""AURA-0094 local judge, artifact boundary, and deterministic execution tests."""
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import fields, replace
from typing import Any

import httpx
import pytest
from aura_observatory.adapters.local_ollama import LocalOllamaAdapter, LocalOllamaError
from aura_observatory.adapters.object_storage import (
    EvaluationObjectStoreError,
    GarageEvaluationObjectStore,
)
from aura_observatory.evaluation_judges import (
    AuthorizedArtifactReader,
    InMemoryJudgeDefinitionRepository,
    InMemoryJudgeExecutionRepository,
    InMemoryJudgeScoreStore,
    JudgeAdvisoryState,
    JudgeArtifactReference,
    JudgeError,
    JudgeOutcome,
    JudgeRequest,
    JudgeResponse,
    LocalJudgeDefinition,
    LocalJudgeService,
    PostgresJudgeDefinitionRepository,
)
from aura_observatory.metrics import ObservatoryMetrics
from aura_observatory.ports import EvaluationArtifactMetadata
from test_aura0094_calibration import NOW

IDS = {
    "judge": "00000000-0000-0000-0000-000000000001",
    "prompt": "00000000-0000-0000-0000-000000000002",
    "rubric": "00000000-0000-0000-0000-000000000003",
    "evidence": "00000000-0000-0000-0000-000000000004",
    "dataset": "00000000-0000-0000-0000-000000000005",
    "auth": "00000000-0000-0000-0000-000000000006",
    "request": "00000000-0000-0000-0000-000000000007",
    "job": "00000000-0000-0000-0000-000000000008",
    "suite": "00000000-0000-0000-0000-000000000009",
    "evaluator": "00000000-0000-0000-0000-000000000010",
    "result": "00000000-0000-0000-0000-000000000011",
    "calibration": "00000000-0000-0000-0000-000000000012",
}
DIGEST = "a" * 64
BYTES = {"prompt": b"evaluate the quoted evidence", "rubric": b"score correctness"}


def _reference(
    key: str,
    value: bytes,
    *,
    revoked: bool = False,
    intended_use: str = "input",
) -> JudgeArtifactReference:
    return JudgeArtifactReference(
        IDS[key],
        IDS["dataset"],
        IDS["auth"],
        hashlib.sha256(value).hexdigest(),
        len(value),
        intended_use=intended_use,
        revoked=revoked,
    )


def _definition() -> LocalJudgeDefinition:
    return LocalJudgeDefinition(
        IDS["judge"],
        "1.0.0",
        "llama3.2:3b",
        "3.2.0",
        DIGEST,
        "prompt-1",
        "rubric-1",
        "calibration-1",
        _reference("prompt", BYTES["prompt"], intended_use="prompt"),
        _reference("rubric", BYTES["rubric"], intended_use="rubric"),
        calibration_id=IDS["calibration"],
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
    )


def _request() -> JudgeRequest:
    definition = _definition()
    evidence = b"quoted evidence"
    return JudgeRequest(
        IDS["request"],
        IDS["request"],
        IDS["evidence"],
        IDS["result"],
        IDS["job"],
        IDS["suite"],
        IDS["evaluator"],
        "1.0.0",
        "aura.core.runner",
        "1.0.0",
        (_reference("evidence", evidence),),
        definition,
    )


class _MetadataRepo:
    def __init__(self, metadata: EvaluationArtifactMetadata | None) -> None:
        self.metadata = metadata

    async def lookup_artifact_metadata(self, _reference: str) -> EvaluationArtifactMetadata | None:
        return self.metadata


class _ObjectStore:
    def __init__(self, data: bytes, *, failure: Exception | None = None) -> None:
        self.data = data
        self.failure = failure
        self.calls: list[dict[str, Any]] = []

    async def read(self, reference: str, **kwargs: Any) -> bytes:
        self.calls.append({"reference": reference, **kwargs})
        if self.failure:
            raise self.failure
        return self.data


def _metadata(
    reference: JudgeArtifactReference, *, revoked: bool = False
) -> EvaluationArtifactMetadata:
    return EvaluationArtifactMetadata(
        artifact_reference_id=reference.artifact_reference_id,
        dataset_id=reference.dataset_id,
        capture_classification=reference.capture_classification,
        authorization_record_id=reference.authorization_record_id,
        authorization_purpose=reference.authorization_purpose,
        authorization_context=reference.authorization_context,
        retention_mode=reference.retention_mode,
        sha256=reference.sha256,
        status="revoked" if revoked else "available",
        revoked=revoked,
        artifact_version=reference.artifact_version,
        media_type=reference.media_type,
        byte_count=reference.byte_count,
        media_classification=reference.media_classification,
        intended_use=reference.intended_use,
    )


@pytest.mark.asyncio
async def test_judge_definition_is_immutable_and_idempotent() -> None:
    repository = InMemoryJudgeDefinitionRepository()
    definition = _definition()
    assert await repository.create(definition) is definition
    assert await repository.create(definition) is definition
    assert [audit["outcome"] for audit in repository.audits] == ["accepted", "duplicate"]

    conflicting = LocalJudgeDefinition(
        definition.judge_id,
        definition.judge_version,
        definition.model_name,
        definition.model_version,
        "b" * 64,
        definition.prompt_version,
        definition.rubric_version,
        definition.calibration_version,
        definition.prompt_artifact,
        definition.rubric_artifact,
    )
    with pytest.raises(JudgeError):
        await repository.create(conflicting)


@pytest.mark.parametrize(
    "field,value",
    [("temperature", 0.1), ("max_output_bytes", 8193), ("max_concurrency", 3)],
)
def test_definition_rejects_nondeterministic_or_unbounded_options(field: str, value: Any) -> None:
    definition = _definition()
    object.__setattr__(definition, field, value)
    with pytest.raises(JudgeError, match="validation"):
        definition.validate()


@pytest.mark.asyncio
async def test_authorized_reader_verifies_metadata_digest_and_bounded_stream() -> None:
    reference = _reference("evidence", b"evidence")
    store = _ObjectStore(b"evidence")
    reader = AuthorizedArtifactReader(_MetadataRepo(_metadata(reference)), store)
    assert await reader.read(reference, max_bytes=32) == b"evidence"
    assert store.calls == [
        {
            "reference": reference.artifact_reference_id,
            "expected_sha256": reference.sha256,
            "expected_bytes": reference.byte_count,
            "max_bytes": 32,
        }
    ]

    with pytest.raises(JudgeError, match="artifact_limit"):
        await reader.read(reference, max_bytes=2)


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [True])
async def test_authorized_reader_rejects_revoked_or_unknown_artifacts(revoked: bool) -> None:
    reference = _reference("evidence", b"evidence")
    reader = AuthorizedArtifactReader(
        _MetadataRepo(_metadata(reference, revoked=revoked)), _ObjectStore(b"evidence")
    )
    with pytest.raises(JudgeError, match="artifact_revoked"):
        await reader.read(reference, max_bytes=32)

    unknown = AuthorizedArtifactReader(_MetadataRepo(None), _ObjectStore(b"evidence"))
    with pytest.raises(JudgeError, match="artifact_unauthorized"):
        await unknown.read(reference, max_bytes=32)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field",
    [
        "artifact_reference_id",
        "dataset_id",
        "authorization_record_id",
        "artifact_version",
        "sha256",
        "byte_count",
        "media_type",
        "media_classification",
        "intended_use",
        "authorization_purpose",
        "authorization_context",
        "retention_mode",
        "capture_classification",
    ],
)
async def test_authorized_reader_rejects_every_metadata_mismatch(field: str) -> None:
    reference = _reference("evidence", b"evidence")
    metadata = _metadata(reference)
    replacement: Any = "mismatch"
    if field == "byte_count":
        replacement = metadata.byte_count + 1
    elif field == "sha256":
        replacement = "c" * 64
    elif field == "artifact_reference_id":
        replacement = IDS["request"]
    elif field == "dataset_id":
        replacement = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    elif field == "authorization_record_id":
        replacement = "ffffffff-ffff-ffff-ffff-ffffffffffff"
    elif field == "artifact_version":
        replacement = "2.0.0"
    broken = replace(metadata, **{field: replacement})
    reader = AuthorizedArtifactReader(_MetadataRepo(broken), _ObjectStore(b"evidence"))
    with pytest.raises(JudgeError, match="artifact_integrity"):
        await reader.read(reference, max_bytes=32)


@pytest.mark.parametrize(
    ("intended_use", "media_classification", "allowed"),
    [
        ("input", "evaluation_input", True),
        ("prompt", "evaluation_input", False),
        ("rubric", "evaluation_input", False),
        ("comment", "evaluation_evidence", False),
        ("input", "evaluation_evidence", False),
        ("input", "evaluation_output", False),
    ],
)
def test_controlled_replay_is_limited_to_authorized_input_context(
    intended_use: str, media_classification: str, allowed: bool
) -> None:
    reference = replace(
        _reference("evidence", b"evidence"),
        authorization_purpose="controlled_replay",
        intended_use=intended_use,
        media_classification=media_classification,
    )
    if allowed:
        reference.validate()
    else:
        with pytest.raises(JudgeError, match="artifact_unauthorized"):
            reference.validate()


@pytest.mark.asyncio
async def test_authorized_reader_maps_object_store_failure_without_locator_or_secret() -> None:
    reference = _reference("evidence", b"evidence")
    reader = AuthorizedArtifactReader(
        _MetadataRepo(_metadata(reference)),
        _ObjectStore(b"", failure=RuntimeError("garage secret endpoint leaked")),
    )
    with pytest.raises(JudgeError, match="artifact_storage_failure") as error:
        await reader.read(reference, max_bytes=32)
    assert "garage" not in str(error.value).casefold()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_code", ["artifact_limit", "artifact_integrity"])
async def test_authorized_reader_preserves_bounded_garage_failure_classification(
    failure_code: str,
) -> None:
    class _Failure(Exception):
        def __init__(self, code: str) -> None:
            self.failure_code = code

    reference = _reference("evidence", b"evidence")
    reader = AuthorizedArtifactReader(
        _MetadataRepo(_metadata(reference)),
        _ObjectStore(b"", failure=_Failure(failure_code)),
    )
    with pytest.raises(JudgeError, match=failure_code):
        await reader.read(reference, max_bytes=32)


@pytest.mark.asyncio
async def test_authorized_reader_enforces_returned_object_size_even_if_store_is_misbehaving() -> (
    None
):
    reference = _reference("evidence", b"evidence")
    reader = AuthorizedArtifactReader(_MetadataRepo(_metadata(reference)), _ObjectStore(b"x" * 33))
    with pytest.raises(JudgeError, match="artifact_limit"):
        await reader.read(reference, max_bytes=32)


class _GarageReadClient:
    status_code = 200
    body = b"authorized bytes"
    error: Exception | None = None
    requests: list[httpx.Request] = []

    def __init__(self, **_: Any) -> None:
        pass

    async def __aenter__(self) -> _GarageReadClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    async def send(self, request: httpx.Request, **_: Any) -> httpx.Response:
        self.requests.append(request)
        if self.error is not None:
            raise self.error
        return httpx.Response(
            self.status_code,
            content=self.body,
            request=request,
            headers={"content-length": str(len(self.body))},
        )


@pytest.mark.asyncio
async def test_garage_read_adapter_streams_and_classifies_boundary_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reference = IDS["evidence"]
    body = _GarageReadClient.body
    store = GarageEvaluationObjectStore(
        "http://garage.internal:3900",
        "observatory-evaluation",
        access_key="judge-read",
        secret_key="s" * 64,
    )
    monkeypatch.setattr(
        "aura_observatory.adapters.object_storage.httpx.AsyncClient", _GarageReadClient
    )
    _GarageReadClient.status_code = 200
    _GarageReadClient.body = body
    _GarageReadClient.error = None
    _GarageReadClient.requests = []
    assert await store.read(
        reference,
        expected_sha256=hashlib.sha256(body).hexdigest(),
        expected_bytes=len(body),
        max_bytes=64,
    ) == body
    assert _GarageReadClient.requests[0].url.path.endswith(
        f"/artifacts/{reference}"
    )
    assert "secret" not in str(_GarageReadClient.requests[0].url)

    cases: list[tuple[int, str]] = [
        (401, "storage_authentication"),
        (403, "storage_authentication"),
        (404, "storage_failure"),
        (302, "storage_integrity"),
    ]
    for status, failure_code in cases:
        _GarageReadClient.status_code = status
        with pytest.raises(EvaluationObjectStoreError, match=failure_code):
            await store.read(
                reference,
                expected_sha256=hashlib.sha256(body).hexdigest(),
                expected_bytes=len(body),
                max_bytes=64,
            )

    _GarageReadClient.status_code = 200
    _GarageReadClient.body = body + b" overflow"
    with pytest.raises(EvaluationObjectStoreError, match="artifact_limit_exceeded"):
        await store.read(
            reference,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_bytes=len(body),
            max_bytes=len(body),
        )

    _GarageReadClient.body = b"wrong digest"
    with pytest.raises(EvaluationObjectStoreError, match="storage_integrity"):
        await store.read(
            reference,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_bytes=len(body),
            max_bytes=64,
        )

    _GarageReadClient.body = body
    _GarageReadClient.error = httpx.ReadTimeout("provider unavailable")
    with pytest.raises(EvaluationObjectStoreError, match="storage_timeout"):
        await store.read(
            reference,
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_bytes=len(body),
            max_bytes=64,
        )

    with pytest.raises(ValueError, match="UUID"):
        await store.read(
            "../private/secret",
            expected_sha256=hashlib.sha256(body).hexdigest(),
            expected_bytes=len(body),
            max_bytes=64,
        )


class _Provider:
    def __init__(
        self, response: JudgeResponse | None = None, failure: Exception | None = None
    ) -> None:
        self.response = response or JudgeResponse(0.9, JudgeOutcome.PASS, "pass")
        self.failure = failure
        self.verify_calls = 0
        self.generate_calls = 0

    async def verify_model(
        self, model_name: str, model_digest: str, *, model_version: str | None = None
    ) -> None:
        assert model_name == "llama3.2:3b"
        assert model_digest == DIGEST
        assert model_version in {None, "3.2.0"}
        self.verify_calls += 1
        if self.failure:
            raise self.failure

    async def generate(self, **kwargs: Any) -> JudgeResponse:
        assert kwargs["timeout_seconds"] == 120.0
        assert kwargs["max_output_tokens"] == 2048
        assert kwargs["context_tokens"] == 4096
        self.generate_calls += 1
        if self.failure:
            raise self.failure
        return self.response


@pytest.mark.asyncio
async def test_judge_service_duplicate_is_idempotent_and_content_free() -> None:
    request = _request()
    prompt = _reference("prompt", BYTES["prompt"], intended_use="prompt")
    rubric = _reference("rubric", BYTES["rubric"], intended_use="rubric")
    evidence = request.input_artifacts[0]
    data = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    metadata = {
        ref.artifact_reference_id: _metadata(ref, revoked=False)
        for ref in (prompt, rubric, evidence)
    }

    class _AllMetadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return metadata[reference]

    class _AllStore:
        async def read(self, reference: str, **_kwargs: Any) -> bytes:
            return data[reference]

    provider = _Provider()
    service = LocalJudgeService(provider, AuthorizedArtifactReader(_AllMetadata(), _AllStore()))
    first = await service.evaluate(request)
    second = await service.evaluate(request)
    assert first == second
    assert provider.verify_calls == 1
    assert provider.generate_calls == 1
    assert first.advisory_state is JudgeAdvisoryState.ADVISORY
    assert not any(
        token in repr(first).casefold() for token in ("prompt", "rubric", "quoted evidence")
    )


@pytest.mark.asyncio
async def test_definition_repository_round_trips_intended_use_and_artifact_version() -> None:
    class _Result:
        def __init__(self, row: object | None = None) -> None:
            self.row = row

        async def fetchone(self) -> object | None:
            return self.row

    class _Connection:
        def __init__(self) -> None:
            self.definition_json: str | None = None
            self.fingerprint: str | None = None

        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_: object) -> None:
            return None

        async def execute(self, query: str, params: tuple[object, ...]) -> _Result:
            if query.startswith("INSERT"):
                self.definition_json = str(params[-1])
                self.fingerprint = str(params[-2])
                return _Result()
            if "SELECT fingerprint" in query:
                return _Result((self.fingerprint,))
            return _Result((self.definition_json, self.fingerprint))

    connection = _Connection()
    repository = PostgresJudgeDefinitionRepository(lambda: connection)
    definition = _definition()
    await repository.create(definition)
    restored = await repository.get(definition.judge_id, definition.judge_version)
    assert restored is not None
    assert restored.prompt_artifact.intended_use == "prompt"
    assert restored.prompt_artifact.artifact_version == "1.0.0"
    assert restored.rubric_artifact.intended_use == "rubric"
    assert restored.rubric_artifact.artifact_version == "1.0.0"
    assert restored.evaluator_id == definition.evaluator_id
    assert restored.evaluator_version == definition.evaluator_version
    assert restored.evaluator_id != restored.judge_id
    assert restored.calibration_id == definition.calibration_id
    assert (
        restored.prompt_artifact.authorization_purpose
        == definition.prompt_artifact.authorization_purpose
    )


@pytest.mark.asyncio
async def test_production_score_path_persists_qualification_provenance_and_privacy() -> None:
    request = _request()
    prompt = request.definition.prompt_artifact
    rubric = request.definition.rubric_artifact
    evidence = request.input_artifacts[0]
    values = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    refs = (prompt, rubric, evidence)

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return _metadata(next(item for item in refs if item.artifact_reference_id == reference))

    class _Store:
        def __init__(self) -> None:
            self.scores: list[Any] = []

        async def insert_score(self, score: Any) -> None:
            self.scores.append(score)

    class _Objects:
        async def read(self, reference: str, **_: Any) -> bytes:
            return values[reference]

    from test_aura0094_calibration import _qualification

    # A versionless request and the intentionally incomplete fixture must stay
    # advisory.  Qualification is never inferred from a partial transport
    # request, even when a caller returns a record with a matching judge id.
    qualification = _qualification()

    async def resolve(**_: object) -> object:
        return qualification

    store = _Store()
    result = await LocalJudgeService(
        _Provider(),
        AuthorizedArtifactReader(_Metadata(), _Objects()),
        score_store=store,
        qualification_resolver=resolve,
    ).evaluate(request)
    assert result.advisory_state is JudgeAdvisoryState.ADVISORY
    assert len(store.scores) == 1
    score = store.scores[0]
    assert score.score_disposition == "advisory_only"
    assert score.qualification_id == qualification.qualification_id
    assert score.qualification_version == qualification.qualification_version
    assert score.capture_classification == "full_content"
    assert not {
        name for name in ("prompt", "response", "content", "credential", "endpoint")
    }.intersection(field.name for field in fields(score))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mismatch_field",
    [
        None,
        "suite_version",
        "dataset_version",
        "evaluator_version",
        "model_name",
        "model_version",
        "model_digest",
        "prompt_version",
        "prompt_artifact_reference_id",
        "prompt_artifact_version",
        "prompt_artifact_digest",
        "rubric_version",
        "rubric_artifact_reference_id",
        "rubric_artifact_version",
        "rubric_artifact_digest",
        "calibration_version",
        "calibration_result_id",
        "seed",
        "budget",
    ],
)
async def test_production_score_path_maps_exact_qualified_provenance(
    mismatch_field: str | None,
) -> None:
    request = replace(_request(), dataset_version="1.0.0", suite_version="1.0.0")
    definition = request.definition
    refs = (definition.prompt_artifact, definition.rubric_artifact, *request.input_artifacts)
    values = {
        refs[0].artifact_reference_id: BYTES["prompt"],
        refs[1].artifact_reference_id: BYTES["rubric"],
        refs[2].artifact_reference_id: b"quoted evidence",
    }

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return _metadata(next(item for item in refs if item.artifact_reference_id == reference))

    class _Objects:
        async def read(self, reference: str, **_: Any) -> bytes:
            return values[reference]

    class _Store:
        def __init__(self) -> None:
            self.scores: list[Any] = []

        async def insert_score(self, score: Any) -> None:
            self.scores.append(score)

    from test_aura0094_calibration import _qualification

    qualification = replace(
        _qualification(),
        suite_id=request.suite_id,
        suite_version=request.suite_version,
        dataset_id=definition.prompt_artifact.dataset_id,
        dataset_version=request.dataset_version,
        evaluator_id=request.evaluator_id,
        evaluator_version=request.evaluator_version,
        judge_id=definition.judge_id,
        judge_version=definition.judge_version,
        prompt_version=definition.prompt_version,
        rubric_version=definition.rubric_version,
        calibration_version=definition.calibration_version,
        calibration_id=definition.calibration_id,
        qualified_at=NOW,
        model_name=definition.model_name,
        model_version=definition.model_version,
        model_digest=definition.model_digest,
        prompt_artifact_reference_id=definition.prompt_artifact.artifact_reference_id,
        prompt_artifact_version=definition.prompt_artifact.artifact_version,
        prompt_artifact_digest=definition.prompt_artifact.sha256,
        rubric_artifact_reference_id=definition.rubric_artifact.artifact_reference_id,
        rubric_artifact_version=definition.rubric_artifact.artifact_version,
        rubric_artifact_digest=definition.rubric_artifact.sha256,
        budget={
            "timeoutMs": 120_000,
            "maxInputBytes": definition.max_input_bytes,
            "maxOutputBytes": definition.max_output_bytes,
            "maxOutputTokens": definition.max_output_tokens,
            "maxContextTokens": definition.context_tokens,
            "maxConcurrency": definition.max_concurrency,
            "temperature": 0.0,
        },
        provenance_component="aura.observatory.local_judges",
        provenance_version="1.0.0",
        provenance_digest="d" * 64,
    )
    qualified_calibration_result_id = qualification.calibration_result_id
    if mismatch_field is not None:
        replacement: Any = {
            "suite_version": "2.0.0",
            "dataset_version": "2.0.0",
            "evaluator_version": "2.0.0",
            "model_name": "llama3.2:7b",
            "model_version": "7.0.0",
            "model_digest": "e" * 64,
            "prompt_version": "prompt-2",
            "prompt_artifact_reference_id": IDS["rubric"],
            "prompt_artifact_version": "2.0.0",
            "prompt_artifact_digest": "e" * 64,
            "rubric_version": "rubric-2",
            "rubric_artifact_reference_id": IDS["prompt"],
            "rubric_artifact_version": "2.0.0",
            "rubric_artifact_digest": "e" * 64,
            "calibration_version": "calibration-2",
            "calibration_result_id": IDS["request"],
            "seed": 1,
            "budget": {
                "timeoutMs": 60_000,
                "maxInputBytes": definition.max_input_bytes,
                "maxOutputBytes": definition.max_output_bytes,
                "maxOutputTokens": definition.max_output_tokens,
                "maxContextTokens": definition.context_tokens,
                "maxConcurrency": definition.max_concurrency,
                "temperature": 0.0,
            },
        }[mismatch_field]
        qualification = replace(qualification, **{mismatch_field: replacement})

    async def resolve(**_: object) -> object:
        return qualification

    from aura_observatory.evaluation_calibration import CalibrationReport

    calibration_result = CalibrationReport(
        definition.judge_id,
        definition.judge_version,
        definition.prompt_version,
        definition.rubric_version,
        definition.calibration_version,
        20,
        1.0,
        0.0,
        0.0,
        True,
        dataset_id=definition.prompt_artifact.dataset_id,
        dataset_version=request.dataset_version,
        suite_id=request.suite_id,
        suite_version=request.suite_version,
        evaluator_id=request.evaluator_id,
        evaluator_version=request.evaluator_version,
        calibration_result_id=qualified_calibration_result_id,
        calibration_id=definition.calibration_id,
        model_name=definition.model_name,
        model_version=definition.model_version,
        model_digest=definition.model_digest,
        seed=definition.seed,
        budget={
            "timeoutMs": 120_000,
            "maxInputBytes": definition.max_input_bytes,
            "maxOutputBytes": definition.max_output_bytes,
            "maxOutputTokens": definition.max_output_tokens,
            "maxContextTokens": definition.context_tokens,
            "maxConcurrency": definition.max_concurrency,
            "temperature": 0.0,
        },
        prompt_artifact_reference_id=definition.prompt_artifact.artifact_reference_id,
        prompt_artifact_version=definition.prompt_artifact.artifact_version,
        prompt_artifact_digest=definition.prompt_artifact.sha256,
        rubric_artifact_reference_id=definition.rubric_artifact.artifact_reference_id,
        rubric_artifact_version=definition.rubric_artifact.artifact_version,
        rubric_artifact_digest=definition.rubric_artifact.sha256,
    )

    async def resolve_calibration_result(_result_id: str) -> CalibrationReport:
        return calibration_result

    store = _Store()
    result = await LocalJudgeService(
        _Provider(),
        AuthorizedArtifactReader(_Metadata(), _Objects()),
        score_store=store,
        qualification_resolver=resolve,
        calibration_result_resolver=resolve_calibration_result,
    ).evaluate(request)
    assert result.advisory_state is (
        JudgeAdvisoryState.CALIBRATED
        if mismatch_field is None
        else JudgeAdvisoryState.ADVISORY
    )
    assert len(store.scores) == 1
    score = store.scores[0]
    expected = {
        "judge_definition_id": definition.judge_id,
        "judge_definition_version": definition.judge_version,
        "model_name": definition.model_name,
        "model_version": definition.model_version,
        "model_digest": definition.model_digest,
        "prompt_version": definition.prompt_version,
        "rubric_version": definition.rubric_version,
        "calibration_version": definition.calibration_version,
        "seed": definition.seed,
        "budget_timeout_ms": 120_000,
        "budget_max_input_bytes": definition.max_input_bytes,
        "budget_max_output_bytes": definition.max_output_bytes,
        "budget_max_output_tokens": definition.max_output_tokens,
        "budget_max_context_tokens": definition.context_tokens,
        "budget_max_concurrency": definition.max_concurrency,
        "score_disposition": (
            "calibrated_enabled" if mismatch_field is None else "advisory_only"
        ),
        "qualification_id": qualification.qualification_id,
        "qualification_version": qualification.qualification_version,
    }
    for name, value in expected.items():
        assert getattr(score, name) == value
    assert score.uncertainty is None
    assert score.disagreement == "none"


@pytest.mark.asyncio
async def test_score_persistence_precedes_execution_completion_and_failure_releases_claim() -> None:
    request = _request()
    refs = (
        request.definition.prompt_artifact,
        request.definition.rubric_artifact,
        *request.input_artifacts,
    )
    values = {
        refs[0].artifact_reference_id: BYTES["prompt"],
        refs[1].artifact_reference_id: BYTES["rubric"],
        refs[2].artifact_reference_id: b"quoted evidence",
    }

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return _metadata(next(item for item in refs if item.artifact_reference_id == reference))

    class _Objects:
        async def read(self, reference: str, **_: Any) -> bytes:
            return values[reference]

    class _ScoreStore:
        def __init__(self, events: list[str], failure: bool = False) -> None:
            self.events, self.failure = events, failure

        async def insert_score(self, score: Any) -> None:
            del score
            self.events.append("score")
            if self.failure:
                raise RuntimeError("clickhouse unavailable")

    class _Execution(InMemoryJudgeExecutionRepository):
        def __init__(self, events: list[str]) -> None:
            super().__init__()
            self.events = events

        async def claim(self, key: str, fingerprint: str) -> bool:
            self.events.append("claim")
            return await super().claim(key, fingerprint)

        async def complete(self, key: str, fingerprint: str, result: Any) -> None:
            self.events.append("complete")
            await super().complete(key, fingerprint, result)

        async def fail(self, key: str, fingerprint: str, failure_code: str) -> None:
            self.events.append("fail")
            await super().fail(key, fingerprint, failure_code)

    events: list[str] = []
    service = LocalJudgeService(
        _Provider(),
        AuthorizedArtifactReader(_Metadata(), _Objects()),
        execution_repository=_Execution(events),
        score_store=_ScoreStore(events),
    )
    await service.evaluate(request)
    assert events.index("claim") < events.index("score") < events.index("complete")

    failed_events: list[str] = []
    with pytest.raises(JudgeError, match="storage_failure"):
        await LocalJudgeService(
            _Provider(),
            AuthorizedArtifactReader(_Metadata(), _Objects()),
            execution_repository=_Execution(failed_events),
            score_store=_ScoreStore(failed_events, failure=True),
        ).evaluate(request)
    assert failed_events.index("claim") < failed_events.index("score") < failed_events.index("fail")
    assert "complete" not in failed_events


@pytest.mark.asyncio
async def test_local_ollama_adapter_uses_fixed_private_nonstreaming_request() -> None:
    captured: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "llama3.2:3b", "digest": DIGEST}]})
        return httpx.Response(
            200,
            json={
                "message": {
                    "role": "assistant",
                    "content": json.dumps({"score": 0.75, "outcome": "pass", "label": "pass"}),
                }
            },
        )

    transport = httpx.MockTransport(handler)
    adapter = LocalOllamaAdapter("http://ollama.internal", transport=transport)
    await adapter.verify_model("llama3.2:3b", DIGEST)
    response = await adapter.generate(
        model_name="llama3.2:3b",
        prompt=b"quoted prompt",
        rubric=b"quoted rubric",
        evidence=(b"quoted evidence",),
        timeout_seconds=2,
        max_output_bytes=8192,
        max_output_tokens=2048,
        context_tokens=4096,
    )
    assert response == JudgeResponse(0.75, JudgeOutcome.PASS, "pass")
    generated = json.loads(captured[1].content)
    assert generated["stream"] is False
    assert generated["options"] == {
        "temperature": 0,
        "seed": 0,
        "num_predict": 2048,
        "num_ctx": 4096,
    }
    assert "tools" not in generated
    assert captured[0].method == "GET"
    assert captured[0].url == httpx.URL("http://ollama.internal/api/tags")
    assert captured[0].content in (b"", None)
    assert captured[1].method == "POST"
    assert captured[1].url == httpx.URL("http://ollama.internal/api/chat")
    assert [message["role"] for message in generated["messages"]] == ["system", "user"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "response",
    [
        {
            "message": {
                "role": "assistant",
                "content": '{"score": NaN, "outcome": "pass", "label": "pass"}',
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": (
                    '{"score": 0.5, "outcome": "pass", "label": "pass", "rationale": "secret"}'
                ),
            }
        },
        {"message": {"role": "assistant", "content": "not json"}},
    ],
)
async def test_local_ollama_adapter_rejects_nonclosed_or_nonfinite_response(
    response: dict[str, str],
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response)

    adapter = LocalOllamaAdapter("http://ollama.internal", transport=httpx.MockTransport(handler))
    with pytest.raises(LocalOllamaError, match="provider_malformed"):
        await adapter.generate(
            model_name="llama3.2:3b",
            prompt=b"p",
            rubric=b"r",
            evidence=(),
            timeout_seconds=1,
            max_output_bytes=8192,
            max_output_tokens=2048,
            context_tokens=64,
        )


@pytest.mark.asyncio
async def test_local_ollama_adapter_maps_digest_redirect_timeout_and_oversize() -> None:
    async def redirect(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(307, headers={"location": "https://cloud.invalid"})

    with pytest.raises(LocalOllamaError, match="provider_redirect"):
        await LocalOllamaAdapter(
            "http://ollama.internal", transport=httpx.MockTransport(redirect)
        ).generate(
            model_name="m",
            prompt=b"p",
            rubric=b"r",
            evidence=(),
            timeout_seconds=1,
            max_output_bytes=8192,
            max_output_tokens=2048,
            context_tokens=64,
        )

    async def timeout(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out")

    with pytest.raises(LocalOllamaError, match="provider_timeout"):
        await LocalOllamaAdapter(
            "http://ollama.internal", transport=httpx.MockTransport(timeout)
        ).generate(
            model_name="m",
            prompt=b"p",
            rubric=b"r",
            evidence=(),
            timeout_seconds=1,
            max_output_bytes=8192,
            max_output_tokens=2048,
            context_tokens=64,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("inventory", "expected"),
    [
        ({"models": [{"name": "llama3.2:3b", "digest": "c" * 64}]}, "model_digest_drift"),
        ({"models": [{"digest": DIGEST}]}, "model_not_found"),
    ],
)
async def test_local_ollama_model_inventory_is_exact_and_unambiguous(
    inventory: dict[str, str], expected: str
) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=inventory)

    adapter = LocalOllamaAdapter("http://ollama.internal", transport=httpx.MockTransport(handler))
    with pytest.raises(LocalOllamaError, match=expected):
        await adapter.verify_model("llama3.2:3b", DIGEST)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_code",
    ["provider_timeout", "provider_redirect", "model_not_found", "provider_resource_limit"],
)
async def test_judge_service_maps_each_provider_failure_to_bounded_metrics(
    failure_code: str,
) -> None:
    request = _request()
    metrics = ObservatoryMetrics()
    with pytest.raises(JudgeError, match=failure_code):
        await LocalJudgeService(
            _Provider(failure=JudgeError(failure_code)),
            _NeverReader(),
            metrics=metrics,
        ).evaluate(request)
    assert metrics.component_spans[-1].dependency == "ollama"
    assert metrics.component_spans[-1].error_code == failure_code
    assert any(
        name == "observatory_local_judge_provider_failure_count"
        and dict(labels).get("failure_code") == failure_code
        for (name, labels), _value in metrics.values.items()
    )
    assert failure_code in metrics.prometheus()


@pytest.mark.asyncio
async def test_judge_phase_duration_metrics_cover_each_dependency_boundary() -> None:
    request = replace(_request(), dataset_version="1.0.0", suite_version="1.0.0")
    definitions = InMemoryJudgeDefinitionRepository()
    await definitions.create(request.definition)
    metrics = ObservatoryMetrics()

    refs = (
        request.definition.prompt_artifact,
        request.definition.rubric_artifact,
        *request.input_artifacts,
    )

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return _metadata(next(item for item in refs if item.artifact_reference_id == reference))

    class _Objects:
        async def read(self, reference: str, **_: Any) -> bytes:
            return {
                refs[0].artifact_reference_id: BYTES["prompt"],
                refs[1].artifact_reference_id: BYTES["rubric"],
                refs[2].artifact_reference_id: b"quoted evidence",
            }[reference]

    await LocalJudgeService(
        _Provider(),
        AuthorizedArtifactReader(_Metadata(), _Objects()),
        definition_repository=definitions,
        execution_repository=InMemoryJudgeExecutionRepository(),
        score_store=InMemoryJudgeScoreStore(),
        metrics=metrics,
    ).evaluate(request)
    expected = {
        "definition_get",
        "execution_get",
        "execution_claim",
        "verify_model",
        "read_artifacts",
        "generate",
        "write_score",
        "execution_complete",
    }
    observed = {
        dict(labels)["operation"]
        for (name, labels), _value in metrics.values.items()
        if name == "observatory_evaluation_operation_duration_ms"
    }
    assert expected <= observed
    dependencies = {span.operation: span.dependency for span in metrics.component_spans}
    assert dependencies["read_artifacts"] == "garage"


@pytest.mark.asyncio
async def test_concurrent_redelivery_claims_once_and_conflicting_reuse_is_rejected() -> None:
    class _BarrierProvider(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()

        async def generate(self, **kwargs: Any) -> JudgeResponse:
            self.started.set()
            await asyncio.sleep(0)
            return await super().generate(**kwargs)

    request = _request()
    prompt = request.definition.prompt_artifact
    rubric = request.definition.rubric_artifact
    evidence = request.input_artifacts[0]
    data = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    metadata = {item.artifact_reference_id: _metadata(item) for item in (prompt, rubric, evidence)}

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return metadata[reference]

    class _Store:
        async def read(self, reference: str, **_: Any) -> bytes:
            return data[reference]

    provider = _BarrierProvider()
    execution = InMemoryJudgeExecutionRepository()
    service = LocalJudgeService(
        provider,
        AuthorizedArtifactReader(_Metadata(), _Store()),
        execution_repository=execution,
        score_store=InMemoryJudgeScoreStore(),
    )
    results = await asyncio.gather(service.evaluate(request), service.evaluate(request))
    assert results[0] == results[1]
    assert provider.verify_calls == 1
    assert provider.generate_calls == 1

    conflicting = JudgeRequest(
        request.request_id,
        request.idempotency_key,
        request.case_id,
        IDS["job"],
        request.job_id,
        request.suite_id,
        request.evaluator_id,
        request.evaluator_version,
        request.component_id,
        request.component_version,
        request.input_artifacts,
        request.definition,
        request.observed_at,
    )
    with pytest.raises(JudgeError, match="validation"):
        await service.evaluate(conflicting)


@pytest.mark.asyncio
async def test_first_claim_blocks_second_worker_during_provider_execution() -> None:
    class _BlockingProvider(_Provider):
        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def generate(self, **kwargs: Any) -> JudgeResponse:
            self.entered.set()
            await self.release.wait()
            return await super().generate(**kwargs)

    request = _request()
    prompt, rubric, evidence = (
        request.definition.prompt_artifact,
        request.definition.rubric_artifact,
        request.input_artifacts[0],
    )
    data = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    metadata = {item.artifact_reference_id: _metadata(item) for item in (prompt, rubric, evidence)}

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return metadata[reference]

    class _Store:
        async def read(self, reference: str, **_: Any) -> bytes:
            return data[reference]

    provider = _BlockingProvider()
    execution = InMemoryJudgeExecutionRepository()
    score_store = InMemoryJudgeScoreStore()

    def build() -> LocalJudgeService:
        return LocalJudgeService(
            provider,
            AuthorizedArtifactReader(_Metadata(), _Store()),
            execution_repository=execution,
            score_store=score_store,
        )

    first = asyncio.create_task(build().evaluate(request))
    await asyncio.wait_for(provider.entered.wait(), timeout=1)
    second = asyncio.create_task(build().evaluate(request))
    with pytest.raises(JudgeError, match="duplicate"):
        await asyncio.wait_for(second, timeout=1)
    provider.release.set()
    result = await first
    assert result.outcome is JudgeOutcome.PASS
    assert provider.verify_calls == 1
    assert provider.generate_calls == 1


@pytest.mark.asyncio
async def test_completed_claim_is_replayed_after_worker_restart_without_provider_call() -> None:
    request = _request()
    prompt, rubric, evidence = (
        request.definition.prompt_artifact,
        request.definition.rubric_artifact,
        request.input_artifacts[0],
    )
    data = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    metadata = {item.artifact_reference_id: _metadata(item) for item in (prompt, rubric, evidence)}

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return metadata[reference]

    class _Store:
        async def read(self, reference: str, **_: Any) -> bytes:
            return data[reference]

    execution = InMemoryJudgeExecutionRepository()
    first_provider = _Provider()
    first = LocalJudgeService(
        first_provider,
        AuthorizedArtifactReader(_Metadata(), _Store()),
        execution_repository=execution,
        score_store=InMemoryJudgeScoreStore(),
    )
    expected = await first.evaluate(request)
    restarted_provider = _Provider(failure=JudgeError("provider_unavailable"))
    restarted = LocalJudgeService(
        restarted_provider,
        AuthorizedArtifactReader(_Metadata(), _Store()),
        execution_repository=execution,
        score_store=InMemoryJudgeScoreStore(),
    )
    assert await restarted.evaluate(request) == expected
    assert restarted_provider.verify_calls == 0
    assert restarted_provider.generate_calls == 0


@pytest.mark.asyncio
async def test_failed_claim_is_recoverable_after_redelivery_without_content_in_error() -> None:
    request = _request()
    execution = InMemoryJudgeExecutionRepository()
    provider = _Provider(failure=JudgeError("provider_timeout"))
    service = LocalJudgeService(provider, _NeverReader(), execution_repository=execution)
    with pytest.raises(JudgeError, match="provider_timeout"):
        await service.evaluate(request)
    assert execution.items[request.idempotency_key][1] == "failed"
    provider.failure = None
    # A fresh worker instance sees the durable failed claim and may safely retry.
    prompt = request.definition.prompt_artifact
    rubric = request.definition.rubric_artifact
    evidence = request.input_artifacts[0]
    data = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    metadata = {item.artifact_reference_id: _metadata(item) for item in (prompt, rubric, evidence)}

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return metadata[reference]

    class _Store:
        async def read(self, reference: str, **_: Any) -> bytes:
            return data[reference]

    recovered = LocalJudgeService(
        provider,
        AuthorizedArtifactReader(_Metadata(), _Store()),
        execution_repository=execution,
        score_store=InMemoryJudgeScoreStore(),
    )
    result = await recovered.evaluate(request)
    assert result.outcome is JudgeOutcome.PASS
    assert provider.generate_calls == 1


@pytest.mark.asyncio
async def test_score_storage_failure_marks_claim_failed_and_retries_idempotently() -> None:
    request = _request()
    prompt = request.definition.prompt_artifact
    rubric = request.definition.rubric_artifact
    evidence = request.input_artifacts[0]
    data = {
        prompt.artifact_reference_id: BYTES["prompt"],
        rubric.artifact_reference_id: BYTES["rubric"],
        evidence.artifact_reference_id: b"quoted evidence",
    }
    metadata = {item.artifact_reference_id: _metadata(item) for item in (prompt, rubric, evidence)}

    class _Metadata:
        async def lookup_artifact_metadata(self, reference: str) -> EvaluationArtifactMetadata:
            return metadata[reference]

    class _Store:
        async def read(self, reference: str, **_: Any) -> bytes:
            return data[reference]

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def insert_score(self, score: Any) -> None:
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("clickhouse unavailable")

    provider = _Provider()
    execution = InMemoryJudgeExecutionRepository()
    scores = _Flaky()
    service = LocalJudgeService(
        provider,
        AuthorizedArtifactReader(_Metadata(), _Store()),
        execution_repository=execution,
        score_store=scores,
    )
    with pytest.raises(JudgeError, match="storage_failure"):
        await service.evaluate(request)
    assert execution.items[request.idempotency_key][1] == "failed"
    result = await service.evaluate(request)
    assert result.outcome is JudgeOutcome.PASS
    assert scores.calls == 2
    assert provider.generate_calls == 2


@pytest.mark.parametrize(
    "origin",
    [
        "https://ollama.internal",
        "http://ollama.internal/path",
        "http://user:pass@ollama.internal",
        "http://ollama.internal?token=secret",
        "http://ollama.internal#fragment",
        "http://public.invalid",
        "http://127.0.0.1:9000",
    ],
)
def test_local_ollama_origin_is_private_and_non_delegable(origin: str) -> None:
    with pytest.raises(ValueError):
        LocalOllamaAdapter(origin)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"message": {"role": "user", "content": "{}"}},
        {
            "message": {
                "role": "assistant",
                "content": '{"score": 0.5, "outcome": "pass", "label": "pass", "echo": "private"}',
            }
        },
        {
            "message": {
                "role": "assistant",
                "content": '{"score": 0.5, "outcome": "pass", "label": "pass"}',
            },
            "unexpected": True,
        },
    ],
)
async def test_local_ollama_chat_response_is_closed_and_role_bound(payload: dict[str, Any]) -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    adapter = LocalOllamaAdapter("http://ollama.internal", transport=httpx.MockTransport(handler))
    with pytest.raises(LocalOllamaError, match="provider_malformed"):
        await adapter.generate(
            model_name="m",
            prompt=b"p",
            rubric=b"r",
            evidence=(),
            timeout_seconds=1,
            max_output_bytes=8192,
            max_output_tokens=2048,
            context_tokens=64,
        )


@pytest.mark.asyncio
async def test_local_ollama_stream_and_utf8_overflow_are_bounded() -> None:
    async def overflow(_request: httpx.Request) -> httpx.Response:
        content = b"{" + b"x" * (LocalOllamaAdapter.MAX_RESPONSE_BYTES + 1)
        return httpx.Response(200, content=content)

    adapter = LocalOllamaAdapter("http://ollama.internal", transport=httpx.MockTransport(overflow))
    with pytest.raises(LocalOllamaError, match="provider_oversized"):
        await adapter.generate(
            model_name="m",
            prompt=b"p",
            rubric=b"r",
            evidence=(),
            timeout_seconds=1,
            max_output_bytes=8192,
            max_output_tokens=2048,
            context_tokens=64,
        )

    async def invalid_utf8(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"\xff")

    adapter = LocalOllamaAdapter(
        "http://ollama.internal", transport=httpx.MockTransport(invalid_utf8)
    )
    with pytest.raises(LocalOllamaError, match="provider_malformed"):
        await adapter.generate(
            model_name="m",
            prompt=b"p",
            rubric=b"r",
            evidence=(),
            timeout_seconds=1,
            max_output_bytes=8192,
            max_output_tokens=2048,
            context_tokens=64,
        )


class _NeverReader:
    async def read(self, *_args: Any, **_kwargs: Any) -> bytes:
        raise AssertionError("artifact reader must not run after model verification failure")
