# pyright: reportPrivateUsage=false

"""Independent observability and evaluation checks for generated artifacts.

These tests deliberately observe the public generated-artifact service through a
small recording sink.  They do not depend on an exporter implementation: the
component boundary, metric vocabulary, lifecycle outcome, and privacy rules are
the contract under test.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
from collections.abc import AsyncGenerator, AsyncIterable, AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from aura_core.domains.artifacts.generated.evaluation import (
    EVALUATOR_COMPONENTS,
    EVALUATORS,
    FIXTURE_REVISION,
    GeneratedArtifactEvaluationResult,
    evaluate,
    evaluate_generated_artifact_authorization,
    evaluate_generated_artifact_deletion_truth,
    evaluate_generated_artifact_integrity,
    evaluate_generated_artifact_privacy,
)
from aura_core.domains.artifacts.generated.persistence import (
    GeneratedArtifactAppendReservationRow,
    GeneratedArtifactCleanupWorkRow,
    GeneratedArtifactCreateReservationRow,
    GeneratedArtifactIdempotencyRow,
    GeneratedArtifactLifecycleEventRow,
    GeneratedArtifactRevisionRow,
    GeneratedArtifactRow,
    GeneratedArtifactTombstoneRow,
)
from aura_core.domains.artifacts.generated.ports import GeneratedArtifactObjectStoreError
from aura_core.domains.artifacts.generated.public import (
    MAX_PREVIEW_CHARS,
    GeneratedArtifactCleanupUnknown,
    GeneratedArtifactConflict,
    GeneratedArtifactLifecycle,
    GeneratedArtifactNotFound,
    GeneratedArtifactService,
    GeneratedArtifactStorageError,
    GeneratedArtifactValidationError,
    sanitize_preview,
)
from aura_core.domains.artifacts.generated.repository import MemoryGeneratedArtifactRepository
from aura_core.entrypoints.api.routes.artifacts import _detail
from aura_core.platform.outbox.persistence import OutboxRow
from aura_core.platform.telemetry import _METRICS, COMPONENT_VERSIONS, MetadataMetrics
from aura_core.providers.object_storage.generated_artifacts import (
    GeneratedArtifactGarageObjectStore,
)
from sqlalchemy import create_engine, select, text, update
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

ROOT = Path(__file__).parents[1]
OWNER = ("https://authentik.example", "artifact-owner")
CREATOR = "aura.research.synthesis@1"
EXPECTED = {
    "artifact-persistence.yaml": {
        "component": "aura.artifacts.persistence@1",
        "metrics": {"artifact_write_outcome_count", "artifact_write_latency_ms"},
        "evaluators": {
            "generated_artifact_integrity@1",
            "generated_artifact_authorization@1",
            "generated_artifact_deletion_truth@1",
            "generated_artifact_privacy@1",
        },
        "operation": "artifact.revision_write",
    },
    "artifact-deletion.yaml": {
        "component": "aura.artifacts.deletion@1",
        "metrics": {"artifact_deletion_outcome_count", "artifact_deletion_latency_ms"},
        "evaluators": {"generated_artifact_deletion_truth@1", "generated_artifact_privacy@1"},
        "operation": "artifact.delete",
    },
}

_METRIC_DIMENSIONS = {"operation", "outcome", "error_class", "dependency"}
_FORBIDDEN_VALUES = (
    "owner-private title",
    "owner-private artifact bytes",
    "owner-private revised bytes",
    "private digest",
    "artifact/private-object-key",
    "conflicting private bytes",
    "tampered private bytes",
    "secret exception text",
    "secret timeout text",
)


@dataclass(frozen=True)
class _Observation:
    kind: str
    component: str
    name: str
    value: float | None
    attributes: Mapping[str, object]


class _RecordingMetrics:
    """Test sink matching the Core metrics port without exporting anything."""

    def __init__(self) -> None:
        self.observations: list[_Observation] = []

    def increment(self, component: str, name: str, **attributes: str) -> None:
        self.observations.append(_Observation("metric", component, name, 1.0, attributes))

    def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
        self.observations.append(_Observation("metric", component, name, value, attributes))

    def record_span(
        self,
        component: str,
        operation: str,
        duration_ms: float,
        **attributes: object,
    ) -> None:
        self.observations.append(
            _Observation("span", component, operation, duration_ms, attributes)
        )


class _ObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.put_failure: Exception | None = None
        self.delete_failure: Exception | None = None

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        if self.put_failure is not None:
            raise self.put_failure
        value = b"".join([piece async for piece in stream])
        self.objects[key] = value
        return len(value)

    async def put_if_absent(
        self,
        key: str,
        stream: AsyncIterable[bytes],
        *,
        content_type: str,
        sha256: str,
        byte_count: int,
    ) -> int:
        del content_type
        if self.put_failure is not None:
            raise self.put_failure
        value = b"".join([piece async for piece in stream])
        if key in self.objects:
            if len(self.objects[key]) == byte_count and self.objects[key] == value:
                return len(self.objects[key])
            raise GeneratedArtifactObjectStoreError("immutable artifact key collision")
        self.objects[key] = value
        assert len(value) == byte_count
        assert hashlib.sha256(value).hexdigest() == sha256
        return len(value)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            if key not in self.objects:
                raise FileNotFoundError(key)
            yield self.objects[key]

        return stream()

    async def delete(self, key: str) -> None:
        if self.delete_failure is not None:
            raise self.delete_failure
        if key not in self.objects:
            raise FileNotFoundError(key)
        del self.objects[key]


class _GarageBackend:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.delete_failure: Exception | None = None

    async def put_stream(
        self, key: str, stream: AsyncIterable[bytes], *, content_type: str
    ) -> int:
        del content_type
        value = b"".join([piece async for piece in stream])
        self.objects[key] = value
        return len(value)

    def get(self, key: str) -> AsyncIterator[bytes]:
        async def stream() -> AsyncIterator[bytes]:
            if key not in self.objects:
                raise FileNotFoundError(key)
            yield self.objects[key]

        return stream()

    async def delete(self, key: str) -> None:
        if self.delete_failure is not None:
            raise self.delete_failure
        if key not in self.objects:
            raise FileNotFoundError(key)
        del self.objects[key]


async def _bytes_stream(value: bytes) -> AsyncIterator[bytes]:
    yield value


@asynccontextmanager
async def _sql_repository() -> AsyncGenerator[tuple[Any, Any]]:
    # SQL evidence is opt-in: a normal full-Core run must not accidentally
    # connect to a developer database.  Once the isolated gate is enabled,
    # however, a missing URL is a configuration error rather than a skip so
    # the explicit live-PostgreSQL work-item check cannot silently omit either
    # SQL test.
    if os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip(
            "AURA_TEST_DEPENDENCIES_ISOLATED=1 is required for live SQL evidence"
        )
    database_url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not database_url:
        pytest.fail(
            "AURA_TEST_DATABASE_URL is required when the isolated live SQL gate is enabled"
        )
    schema = f"aura0142_artifacts_{uuid4().hex[:12]}"
    admin_engine = create_engine(database_url)
    with admin_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    engine = create_async_engine(
        database_url,
        connect_args={"options": f"-c search_path={schema}"},
    )
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    tables = (
        GeneratedArtifactRow.__table__,
        GeneratedArtifactRevisionRow.__table__,
        GeneratedArtifactIdempotencyRow.__table__,
        GeneratedArtifactLifecycleEventRow.__table__,
        GeneratedArtifactTombstoneRow.__table__,
        GeneratedArtifactCleanupWorkRow.__table__,
        GeneratedArtifactAppendReservationRow.__table__,
        GeneratedArtifactCreateReservationRow.__table__,
        # The generated-artifact repository stages lifecycle events in the
        # existing transactional outbox as part of each SQL transaction.
        OutboxRow.__table__,
    )
    try:
        async with engine.begin() as connection:
            for table in tables:
                await connection.run_sync(cast(Any, table).create, checkfirst=True)
        from aura_core.domains.artifacts.generated.repository import SqlGeneratedArtifactRepository

        yield SqlGeneratedArtifactRepository(sessions), sessions
    finally:
        await engine.dispose()
        with admin_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin_engine.dispose()


def _service(
    metrics: _RecordingMetrics, store: _ObjectStore | None = None
) -> GeneratedArtifactService:
    return GeneratedArtifactService(
        MemoryGeneratedArtifactRepository(), store or _ObjectStore(), metrics=metrics
    )


def _manifest(filename: str) -> dict[str, Any]:
    return yaml.safe_load(
        (ROOT / "resources" / "component-manifests" / filename).read_text(encoding="utf-8")
    )


def _assert_content_free(value: object, *, extra_forbidden: tuple[str, ...] = ()) -> None:
    rendered = repr(value)
    for forbidden in (*_FORBIDDEN_VALUES, *extra_forbidden):
        assert forbidden not in rendered


def _assert_observation_shape(
    metrics: _RecordingMetrics, *, extra_forbidden: tuple[str, ...] = ()
) -> None:
    allowed_components = {str(item["component"]) for item in EXPECTED.values()}
    allowed_outcomes = {
        "success",
        "rejected",
        "partial",
        "canceled",
        "timeout",
        "failed",
        "unknown",
    }
    for observation in metrics.observations:
        assert observation.component in allowed_components
        assert observation.value is not None
        assert observation.value >= 0
        assert observation.value == observation.value  # reject NaN
        dimensions = {
            key: value
            for key, value in observation.attributes.items()
            if key in _METRIC_DIMENSIONS
        }
        assert set(dimensions) <= _METRIC_DIMENSIONS
        if "outcome" in dimensions:
            assert dimensions["outcome"] in allowed_outcomes
        if "dependency" in dimensions:
            assert dimensions["dependency"] in {"core_postgresql", "garage_port"}
        assert not any(key.endswith("_id") for key in dimensions)
        _assert_content_free(observation, extra_forbidden=extra_forbidden)


def _assert_observation_delta(
    metrics: _RecordingMetrics,
    start: int,
    *,
    component: str,
    count: int,
    span_name: str,
) -> None:
    delta = metrics.observations[start:]
    assert len(delta) == count
    assert sum(item.kind == "span" for item in delta) == count // 3
    assert sum(item.kind == "metric" for item in delta) == (count * 2) // 3
    assert sum(item.name == span_name for item in delta) == count // 3
    assert sum(
        item.name
        in {
            "artifact_write_outcome_count",
            "artifact_write_latency_ms",
            "artifact_deletion_outcome_count",
            "artifact_deletion_latency_ms",
        }
        for item in delta
    ) == (count * 2) // 3
    assert all(item.component == component for item in delta)


def test_generated_artifact_components_are_closed_metadata_only_contracts() -> None:
    for filename, expected in EXPECTED.items():
        manifest = _manifest(filename)
        assert manifest["component"]["id"] == expected["component"]
        assert manifest["component"]["version"] == "1.0.0"
        assert manifest["input"]["replayable"] is True
        assert manifest["capture_policy"] == "metadata_only"
        assert set(manifest["metrics"]) == expected["metrics"]
        assert set(manifest["evaluators"]) == expected["evaluators"]
        assert set(manifest["cardinality"]["metric_attributes"]) <= _METRIC_DIMENSIONS
        prohibited = {str(value).lower() for value in manifest["cardinality"]["prohibited"]}
        assert {
            "owner",
            "title",
            "content",
            "text",
            "digest",
            "filename",
            "object_key",
            "exception_text",
            "prompt",
            "response",
            "credential",
        } <= prohibited
        assert manifest["cardinality"]["trace_only"]
        assert all(
            "content" not in str(value).lower()
            for value in manifest["cardinality"]["trace_only"]
        )


def test_manifest_metric_catalogue_uses_exact_typed_boundaries() -> None:
    expected_schemas = {
        "artifact-persistence.yaml": (
            "ArtifactRevisionWriteInputV1",
            "ArtifactRevisionWriteResultV1",
        ),
        "artifact-deletion.yaml": (
            "ArtifactDeleteInputV1",
            "ArtifactDeleteResultV1",
        ),
    }
    for filename, _expected in EXPECTED.items():
        manifest = _manifest(filename)
        assert manifest["metric_semantics"]
        input_schema, output_schema = expected_schemas[filename]
        assert manifest["input"]["schema"] == input_schema
        assert manifest["output"]["schema"] == output_schema
        assert manifest["dependencies"] == [
            "core_postgresql",
            "garage_port",
            "transactional_outbox",
        ]


def _assert_evaluator_result(
    result: GeneratedArtifactEvaluationResult,
    *,
    evaluator_id: str,
    passed: bool,
) -> None:
    assert result.evaluator_id == evaluator_id
    assert result.evaluator_version == "1.0.0"
    assert result.component_id == EVALUATOR_COMPONENTS[evaluator_id]
    assert result.component_version == "1.0.0"
    assert result.fixture_revision == FIXTURE_REVISION
    assert len(result.replay_key) == 64
    assert all(character in "0123456789abcdef" for character in result.replay_key)
    assert result.score == (1.0 if passed else 0.0)
    assert result.outcome == ("pass" if passed else "fail")
    assert len(result.metadata) <= 8
    assert tuple(sorted(result.metadata)) == result.metadata
    assert all(len(key) <= 64 and len(value) <= 64 for key, value in result.metadata)
    payload = result.payload()
    assert set(payload) == {
        "evaluatorId",
        "evaluatorVersion",
        "componentId",
        "componentVersion",
        "fixtureRevision",
        "replayKey",
        "score",
        "outcome",
        "metadata",
    }
    metadata = cast(dict[str, str], payload["metadata"])
    assert set(metadata) == {key for key, _ in result.metadata}
    rendered = repr(payload)
    assert not any(
        field in rendered
        for field in (
            "artifactId",
            "revisionId",
            "eventId",
            "correlationId",
            "causationId",
            "owner",
            "title",
            "content",
            "objectKey",
            "exception",
        )
    )
    _assert_content_free(
        payload,
        extra_forbidden=("owner-private", "private-object-key", "private digest"),
    )


def test_all_generated_artifact_evaluators_have_typed_pass_fail_and_replay_contracts() -> None:
    cases = (
        (
            "generated_artifact_integrity@1",
            evaluate_generated_artifact_integrity(
                digest_matches=True, byte_count_matches=True, immutable=True
            ),
            evaluate_generated_artifact_integrity(
                digest_matches=False, byte_count_matches=True, immutable=True
            ),
        ),
        (
            "generated_artifact_authorization@1",
            evaluate_generated_artifact_authorization(
                owner_scope_valid=True, foreign_scope_reads=0, unauthorized_mutations=0
            ),
            evaluate_generated_artifact_authorization(
                owner_scope_valid=True, foreign_scope_reads=1, unauthorized_mutations=0
            ),
        ),
        (
            "generated_artifact_deletion_truth@1",
            evaluate_generated_artifact_deletion_truth(
                lifecycle="deleted", object_absent=True, preview_blocked=False
            ),
            evaluate_generated_artifact_deletion_truth(
                lifecycle="deleted", object_absent=False, preview_blocked=False
            ),
        ),
        (
            "generated_artifact_privacy@1",
            evaluate_generated_artifact_privacy(
                prohibited_fields_seen=0, content_captures=0, identifier_labels=0
            ),
            evaluate_generated_artifact_privacy(
                prohibited_fields_seen=1, content_captures=0, identifier_labels=0
            ),
        ),
    )
    assert tuple(item[0] for item in cases) == EVALUATORS
    for evaluator_id, passed, failed in cases:
        _assert_evaluator_result(passed, evaluator_id=evaluator_id, passed=True)
        _assert_evaluator_result(failed, evaluator_id=evaluator_id, passed=False)
        assert passed.replay_key == failed.replay_key
    assert evaluate(
        "generated_artifact_integrity@1",
        digest_matches=True,
        byte_count_matches=True,
        immutable=True,
    ) == cases[0][1]
    assert evaluate(
        "generated_artifact_authorization@1",
        owner_scope_valid=True,
        foreign_scope_reads=0,
        unauthorized_mutations=0,
    ) == cases[1][1]
    assert evaluate(
        "generated_artifact_deletion_truth@1",
        lifecycle="deleted",
        object_absent=True,
        preview_blocked=False,
    ) == cases[2][1]
    assert evaluate(
        "generated_artifact_privacy@1",
        prohibited_fields_seen=0,
        content_captures=0,
        identifier_labels=0,
    ) == cases[3][1]
    assert len({evaluate_id for evaluate_id in (item[1].replay_key for item in cases)}) == 4


def test_generated_artifact_evaluators_fail_closed_on_invalid_inputs() -> None:
    invalid_results = (
        evaluate_generated_artifact_integrity(
            digest_matches=True, byte_count_matches=True, immutable="yes"  # type: ignore[arg-type]
        ),
        evaluate_generated_artifact_authorization(
            owner_scope_valid=True, foreign_scope_reads=True, unauthorized_mutations=0  # type: ignore[arg-type]
        ),
        evaluate_generated_artifact_deletion_truth(
            lifecycle="unknown_lifecycle", object_absent=True, preview_blocked=False
        ),
        evaluate_generated_artifact_privacy(
            prohibited_fields_seen=-1, content_captures=0, identifier_labels=0
        ),
    )
    for result in invalid_results:
        assert result.outcome == "fail"
        assert result.score == 0.0
        _assert_evaluator_result(result, evaluator_id=result.evaluator_id, passed=False)
    with pytest.raises(ValueError, match="not registered"):
        evaluate("not-a-generated-artifact-evaluator")


@pytest.mark.asyncio
async def test_failure_phases_emit_exact_metadata_observation_deltas() -> None:
    metrics = _RecordingMetrics()
    service = _service(metrics)
    with pytest.raises(GeneratedArtifactValidationError):
        await service.create_generated_artifact(
            *OWNER,
            kind="not-a-generated-kind",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
        )
    _assert_observation_delta(
        metrics,
        0,
        component="aura.artifacts.persistence@1",
        count=3,
        span_name="artifact.revision_write",
    )

    metrics = _RecordingMetrics()
    store = _ObjectStore()
    store.put_failure = GeneratedArtifactObjectStoreError("secret exception text")
    service = _service(metrics, store)
    with pytest.raises(GeneratedArtifactStorageError):
        await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
        )
    _assert_observation_delta(
        metrics,
        0,
        component="aura.artifacts.persistence@1",
        count=3,
        span_name="artifact.revision_write",
    )

    metrics = _RecordingMetrics()
    service = _service(metrics)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    baseline = len(metrics.observations)
    with pytest.raises(GeneratedArtifactValidationError):
        await service.append_revision(
            *OWNER,
            created.id,
            expected_version=0,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
        )
    _assert_observation_delta(
        metrics,
        baseline,
        component="aura.artifacts.persistence@1",
        count=3,
        span_name="artifact.revision_write",
    )
    baseline = len(metrics.observations)
    with pytest.raises(GeneratedArtifactNotFound):
        await service.append_revision(
            *OWNER,
            uuid4(),
            expected_version=1,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
        )
    _assert_observation_delta(
        metrics,
        baseline,
        component="aura.artifacts.persistence@1",
        count=3,
        span_name="artifact.revision_write",
    )

    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    baseline = len(metrics.observations)
    store.put_failure = GeneratedArtifactObjectStoreError("secret exception text")
    with pytest.raises(GeneratedArtifactStorageError):
        await service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
        )
    _assert_observation_delta(
        metrics,
        baseline,
        component="aura.artifacts.persistence@1",
        count=3,
        span_name="artifact.revision_write",
    )

    metrics = _RecordingMetrics()
    service = _service(metrics)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    baseline = len(metrics.observations)
    with pytest.raises(GeneratedArtifactConflict):
        await service.admit_delete(
            *OWNER,
            created.id,
            expected_version=created.version + 1,
            idempotency_key="failure-phase-delete",
        )
    _assert_observation_delta(
        metrics,
        baseline,
        component="aura.artifacts.deletion@1",
        count=3,
        span_name="artifact.delete",
    )

    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    await service.admit_delete(
        *OWNER,
        created.id,
        expected_version=created.version,
        idempotency_key="failure-phase-cleanup",
    )
    baseline = len(metrics.observations)
    store.delete_failure = TimeoutError("secret timeout text")
    settled = await service.resume_pending_cleanup(*OWNER)
    assert [item.lifecycle for item in settled] == [GeneratedArtifactLifecycle.DELETION_FAILED]
    _assert_observation_delta(
        metrics,
        baseline,
        component="aura.artifacts.deletion@1",
        count=6,
        span_name="artifact.delete",
    )
    _assert_observation_shape(metrics)


def test_production_metadata_metrics_register_exact_artifact_components() -> None:
    expected_metrics = {
        "aura.artifacts.persistence@1": {
            "artifact_write_outcome_count",
            "artifact_write_latency_ms",
        },
        "aura.artifacts.deletion@1": {
            "artifact_deletion_outcome_count",
            "artifact_deletion_latency_ms",
        },
    }
    for component, names in expected_metrics.items():
        assert COMPONENT_VERSIONS[component] == "1.0.0"
        assert set(_METRICS[component]) == names

    metrics = MetadataMetrics(capacity=64)
    metrics.increment(
        "aura.artifacts.persistence@1",
        "artifact_write_outcome_count",
        outcome="success",
        dependency="garage_port",
    )
    metrics.observe(
        "aura.artifacts.persistence@1",
        "artifact_write_latency_ms",
        2.0,
        dependency="garage_port",
    )
    metrics.record_span(
        "aura.artifacts.persistence@1",
        "artifact.revision_write",
        2.0,
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
        dependency="garage_port",
        outcome="success",
    )
    snapshot = metrics.snapshot()
    assert metrics.stats().rejected == 0
    assert {item.metric for item in snapshot} == {
        "artifact_write_outcome_count",
        "artifact_write_latency_ms",
    }
    for item in snapshot:
        assert item.component_id == "aura.artifacts.persistence@1"
        assert set(dict(item.dimensions)) <= _METRIC_DIMENSIONS
        assert not any(key.endswith("_id") for key, _ in item.dimensions)
        _assert_content_free(item)
        if item.kind == "metric":
            assert item.trace_id is None
            assert item.span_id is None
            assert not item.trace_attributes
        else:
            assert item.trace_id == "a" * 32
            assert item.span_id == "b" * 16
            assert dict(item.trace_attributes)["operation"] == "artifact.revision_write"

    before = len(metrics.snapshot())
    metrics.increment(
        "aura.artifacts.persistence@1",
        "artifact_write_outcome_count",
        outcome="success",
        dependency="garage_port",
        content="owner-private artifact bytes",
    )
    assert metrics.stats().rejected == 1
    assert len(metrics.snapshot()) == before


@pytest.mark.asyncio
async def test_public_artifact_detail_and_revision_metadata_never_expose_object_keys() -> None:
    # The service's stored revision contains the provider key internally, but
    # the owner-facing DTO is intentionally a metadata-only projection.
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)

    item = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    detail = _detail(item)
    assert "object_key" not in repr(detail)
    assert "objectKey" not in repr(detail)
    assert item.revision is not None
    assert all(key not in repr(detail) for key in store.objects)
    revision_detail = detail["revision"]
    assert isinstance(revision_detail, dict)
    assert set(cast(dict[str, object], revision_detail)) == {
        "revision",
        "sha256",
        "byteCount",
        "mediaType",
        "creatorComponent",
        "previousRevision",
        "createdAt",
    }
    revisions = await service.revisions(*OWNER, item.id)
    assert all(not hasattr(revision, "object_key") for revision in revisions)


@pytest.mark.asyncio
async def test_garage_adapter_preserves_integrity_and_unknown_delete_truth() -> None:
    adapter = GeneratedArtifactGarageObjectStore(
        "http://127.0.0.1:3900", access_key="key", secret_key="secret"
    )
    backend = _GarageBackend()
    cast(Any, adapter)._store = backend
    value = b"owner-private artifact bytes"
    digest = hashlib.sha256(value).hexdigest()
    key = "artifact/private-object-key"

    assert (
        await adapter.put_if_absent(
            key,
            _bytes_stream(value),
            content_type="text/plain; charset=utf-8",
            sha256=digest,
            byte_count=len(value),
        )
        == len(value)
    )
    # A replay of the exact immutable revision is safe, while a digest or
    # length mismatch cannot overwrite an existing Garage object.
    assert (
        await adapter.put_if_absent(
            key,
            _bytes_stream(value),
            content_type="text/plain; charset=utf-8",
            sha256=digest,
            byte_count=len(value),
        )
        == len(value)
    )
    with pytest.raises(GeneratedArtifactObjectStoreError):
        await adapter.put_if_absent(
            key,
            _bytes_stream(b"tampered private bytes"),
            content_type="text/plain; charset=utf-8",
            sha256=hashlib.sha256(b"tampered private bytes").hexdigest(),
            byte_count=len(b"tampered private bytes"),
        )
    assert backend.objects[key] == value

    backend.delete_failure = TimeoutError("secret timeout text")
    with pytest.raises(GeneratedArtifactCleanupUnknown) as unknown:
        await adapter.delete(key)
    assert "secret timeout text" not in str(unknown.value)
    backend.delete_failure = OSError("secret exception text")
    with pytest.raises(GeneratedArtifactObjectStoreError) as failed:
        await adapter.delete(key)
    assert "secret exception text" not in str(failed.value)
    backend.delete_failure = None
    await adapter.delete(key)
    await adapter.delete(key)  # provider-not-found is an idempotent delete


def test_generated_artifact_preview_sanitizer_is_inert_and_bounded() -> None:
    markdown = (
        b"<script>alert('x')</script>\n"
        b"[run](javascript:alert('x')) ![image](https://private.example/x.png)\n"
        b"<iframe src='https://private.example'></iframe>"
    )
    safe, truncated = sanitize_preview(markdown, "text/markdown; charset=utf-8")
    assert not truncated
    assert "<script" not in safe.lower()
    assert "javascript:" not in safe.lower()
    assert "https://private.example" not in safe.lower()
    assert "<iframe" not in safe.lower()

    plain, plain_truncated = sanitize_preview(b"safe\x00text", "text/plain; charset=utf-8")
    assert plain == "safetext"
    assert not plain_truncated

    long_preview, long_truncated = sanitize_preview(
        ("x" * (MAX_PREVIEW_CHARS + 10)).encode(), "text/plain; charset=utf-8"
    )
    assert len(long_preview) == MAX_PREVIEW_CHARS
    assert long_truncated


@pytest.mark.asyncio
async def test_persistence_instrumentation_covers_success_append_and_storage_failure() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
        idempotency_key="replay-safe-create",
    )
    revised = await service.append_revision(
        *OWNER,
        created.id,
        expected_version=created.version,
        media_type="text/plain; charset=utf-8",
        content="owner-private revised bytes",
        creator_component=CREATOR,
        idempotency_key="replay-safe-append",
    )
    assert revised.current_revision == 2
    store.put_failure = GeneratedArtifactObjectStoreError("secret exception text")
    with pytest.raises(GeneratedArtifactStorageError):
        await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
        )
    # A failed provider publication cannot stage or emit a lifecycle event.
    assert len(service.lifecycle_events) == 2

    names = {item.name for item in metrics.observations if item.kind == "metric"}
    assert {"artifact_write_outcome_count", "artifact_write_latency_ms"} <= names
    spans = [item for item in metrics.observations if item.kind == "span"]
    assert spans
    assert {item.name for item in spans} == {"artifact.revision_write"}
    assert all(item.component == "aura.artifacts.persistence@1" for item in spans)
    forbidden = tuple(
        value
        for revision in (created.revision, revised.revision)
        if revision is not None
        for value in (revision.sha256, *store.objects)
    )
    _assert_observation_shape(metrics, extra_forbidden=forbidden)


@pytest.mark.asyncio
async def test_conflicting_write_does_not_overwrite_revision_or_claim_success() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    artifact_id = uuid4()
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
        artifact_id=artifact_id,
    )
    assert created.revision is not None
    original_key = next(iter(store.objects))
    with pytest.raises((GeneratedArtifactConflict, GeneratedArtifactStorageError)):
        await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="conflicting private bytes",
            creator_component=CREATOR,
            artifact_id=artifact_id,
        )
    assert store.objects[original_key] == b"owner-private artifact bytes"
    assert len(service.lifecycle_events) == 1
    failed = [
        item
        for item in metrics.observations
        if item.component == "aura.artifacts.persistence@1"
        and item.name == "artifact_write_outcome_count"
    ]
    assert failed[-1].attributes["outcome"] == "failed"
    _assert_observation_shape(metrics, extra_forbidden=(created.revision.sha256, original_key))


@pytest.mark.asyncio
async def test_append_reservation_survives_publish_loss_and_replays_once() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    store.put_failure = GeneratedArtifactObjectStoreError("secret exception text")
    with pytest.raises(GeneratedArtifactStorageError):
        await service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
            idempotency_key="append-loss",
        )

    store.put_failure = None
    replayed = await service.append_revision(
        *OWNER,
        created.id,
        expected_version=created.version,
        media_type="text/plain; charset=utf-8",
        content="owner-private revised bytes",
        creator_component=CREATOR,
        idempotency_key="append-loss",
    )
    assert replayed.current_revision == 2
    revisions = await service.revisions(*OWNER, created.id)
    assert [revision.revision for revision in revisions] == [1, 2]
    assert (
        len(
            [
                event
                for event in service.lifecycle_events
                if event.event_type == "artifact.revision_published"
            ]
        )
        == 1
    )
    write_metrics = [
        item
        for item in metrics.observations
        if item.component == "aura.artifacts.persistence@1"
        and item.name == "artifact_write_outcome_count"
    ]
    assert [item.attributes.get("outcome") for item in write_metrics[-2:]] == [
        "failed",
        "success",
    ]
    assert replayed.revision is not None
    _assert_observation_shape(
        metrics,
        extra_forbidden=(replayed.revision.sha256, *store.objects),
    )


@pytest.mark.asyncio
async def test_full_idempotency_fingerprints_reject_mutated_replays() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
        idempotency_key="fingerprint-create",
    )
    with pytest.raises(GeneratedArtifactConflict, match="idempotency key"):
        await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="conflicting private bytes",
            creator_component=CREATOR,
            idempotency_key="fingerprint-create",
        )

    revised = await service.append_revision(
        *OWNER,
        created.id,
        expected_version=created.version,
        media_type="text/plain; charset=utf-8",
        content="owner-private revised bytes",
        creator_component=CREATOR,
        idempotency_key="fingerprint-append",
    )
    with pytest.raises(GeneratedArtifactConflict, match="idempotency key"):
        await service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="conflicting private bytes",
            creator_component=CREATOR,
            idempotency_key="fingerprint-append",
        )
    assert revised.current_revision == 2
    assert len(await service.revisions(*OWNER, created.id)) == 2

    admitted_delete = await service.admit_delete(
        *OWNER,
        created.id,
        expected_version=revised.version,
        idempotency_key="fingerprint-delete",
    )
    assert admitted_delete.lifecycle is GeneratedArtifactLifecycle.DELETION_PENDING
    deleted = (await service.resume_pending_cleanup(*OWNER))[0]
    assert deleted.lifecycle is GeneratedArtifactLifecycle.DELETED
    with pytest.raises(GeneratedArtifactConflict, match="idempotency key"):
        await service.admit_delete(
            *OWNER,
            created.id,
            expected_version=created.version,
            idempotency_key="fingerprint-delete",
        )
    _assert_observation_shape(metrics)


@pytest.mark.asyncio
async def test_concurrent_append_admits_one_command_and_publishes_one_revision() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    results = await asyncio.gather(
        service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
            idempotency_key="race-a",
        ),
        service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="conflicting private bytes",
            creator_component=CREATOR,
            idempotency_key="race-b",
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(result, GeneratedArtifactConflict) for result in results) == 1
    assert sum(not isinstance(result, BaseException) for result in results) == 1
    current = await service.get(*OWNER, created.id)
    assert current.current_revision == 2
    assert len(await service.revisions(*OWNER, created.id)) == 2
    assert (
        len(
            [
                event
                for event in service.lifecycle_events
                if event.event_type == "artifact.revision_published"
            ]
        )
        == 1
    )
    assert len(store.objects) == 2
    _assert_observation_shape(metrics)


@pytest.mark.asyncio
async def test_digest_mismatch_is_rejected_without_private_error_or_telemetry() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    assert created.revision is not None
    store.objects[next(iter(store.objects))] = b"tampered private bytes"
    with pytest.raises(GeneratedArtifactStorageError) as raised:
        await service.preview(*OWNER, created.id)
    assert "tampered private bytes" not in str(raised.value)
    _assert_observation_shape(
        metrics,
        extra_forbidden=(created.revision.sha256, *store.objects),
    )


@pytest.mark.asyncio
async def test_deletion_instrumentation_preserves_failed_and_unknown_truth() -> None:
    for failure, expected_lifecycle, expected_outcome in (
        (
            GeneratedArtifactObjectStoreError("secret exception text"),
            GeneratedArtifactLifecycle.DELETION_FAILED,
            ("failed", "storage_unavailable"),
        ),
        (
            GeneratedArtifactCleanupUnknown("secret exception text"),
            GeneratedArtifactLifecycle.CLEANUP_UNKNOWN,
            ("unknown", "cleanup_unknown"),
        ),
    ):
        metrics = _RecordingMetrics()
        store = _ObjectStore()
        service = _service(metrics, store)
        created = await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
        )
        store.delete_failure = failure
        await service.admit_delete(
            *OWNER,
            created.id,
            expected_version=created.version,
            idempotency_key="delete-replay",
        )
        result = (await service.resume_pending_cleanup(*OWNER))[0]
        assert result.lifecycle is expected_lifecycle
        deletion = [
            item
            for item in metrics.observations
            if item.component == "aura.artifacts.deletion@1"
        ]
        assert deletion
        assert any(
            item.name == "artifact_deletion_outcome_count"
            and item.attributes.get("outcome") == expected_outcome[0]
        and item.attributes.get("error_class") == expected_outcome[1]
            for item in deletion
        )
        assert {item.name for item in deletion} >= {
            "artifact_deletion_outcome_count",
            "artifact_deletion_latency_ms",
        }
        assert any(item.kind == "span" and item.name == "artifact.delete" for item in deletion)
        forbidden = tuple(
            value
            for revision in (created.revision,)
            if revision is not None
            for value in (revision.sha256, *store.objects)
        )
        _assert_observation_shape(metrics, extra_forbidden=forbidden)


@pytest.mark.asyncio
async def test_cleanup_timeout_retry_unknown_reconcile_and_delete_replay_are_truthful() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    store.delete_failure = TimeoutError("secret timeout text")
    await service.admit_delete(
        *OWNER,
        created.id,
        expected_version=created.version,
        idempotency_key="delete-replay",
    )
    failed = (await service.resume_pending_cleanup(*OWNER))[0]
    assert failed.lifecycle is GeneratedArtifactLifecycle.DELETION_FAILED
    store.delete_failure = None
    await service.admit_retry_cleanup(
        *OWNER,
        created.id,
        expected_version=failed.version,
        idempotency_key="retry-cleanup",
    )
    retried = (await service.resume_pending_cleanup(*OWNER))[0]
    assert retried.lifecycle is GeneratedArtifactLifecycle.DELETED
    replayed = await service.admit_delete(
        *OWNER,
        created.id,
        expected_version=created.version,
        idempotency_key="delete-replay",
    )
    assert replayed.lifecycle is GeneratedArtifactLifecycle.DELETED
    replay_metrics = [
        item
        for item in metrics.observations
        if item.component == "aura.artifacts.deletion@1"
        and item.name == "artifact_deletion_outcome_count"
    ]
    assert replay_metrics[-1].attributes.get("outcome") == "success"

    metrics_unknown = _RecordingMetrics()
    unknown_store = _ObjectStore()
    unknown_service = _service(metrics_unknown, unknown_store)
    unknown_created = await unknown_service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    unknown_store.delete_failure = GeneratedArtifactCleanupUnknown("secret exception text")
    await unknown_service.admit_delete(
        *OWNER,
        unknown_created.id,
        expected_version=unknown_created.version,
        idempotency_key="unknown-delete",
    )
    unknown = (await unknown_service.resume_pending_cleanup(*OWNER))[0]
    assert unknown.lifecycle is GeneratedArtifactLifecycle.CLEANUP_UNKNOWN
    assert unknown_created.revision is not None
    object_key = next(iter(unknown_store.objects))
    assert unknown_store.objects[object_key] == b"owner-private artifact bytes"
    unknown_store.delete_failure = None
    await unknown_service.admit_reconcile_cleanup(
        *OWNER,
        unknown_created.id,
        expected_version=unknown.version,
        idempotency_key="reconcile-cleanup",
    )
    reconciled = (await unknown_service.resume_pending_cleanup(*OWNER))[0]
    # Reconciliation is read-only: an existing object is evidence that the
    # provider state is not settled.  Only an explicit retry may delete it.
    assert reconciled.lifecycle is GeneratedArtifactLifecycle.DELETION_FAILED
    assert unknown_store.objects[object_key] == b"owner-private artifact bytes"
    await unknown_service.admit_retry_cleanup(
        *OWNER,
        unknown_created.id,
        expected_version=reconciled.version,
        idempotency_key="retry-after-reconcile",
    )
    retried_unknown = (await unknown_service.resume_pending_cleanup(*OWNER))[0]
    assert retried_unknown.lifecycle is GeneratedArtifactLifecycle.DELETED
    assert unknown_store.objects == {}

    deletion = [
        item
        for item in metrics.observations
        if item.component == "aura.artifacts.deletion@1"
    ]
    assert any(
        item.name == "artifact_deletion_outcome_count"
        and item.attributes.get("outcome") == "timeout"
        and item.attributes.get("error_class") in {"timeout", "storage_unavailable"}
        for item in deletion
    )
    assert any(
        item.name == "artifact_deletion_outcome_count"
        and item.attributes.get("outcome") == "success"
        for item in deletion
    )
    _assert_observation_shape(
        metrics,
        extra_forbidden=(created.revision.sha256, *store.objects)
        if created.revision is not None
        else (),
    )
    _assert_observation_shape(metrics_unknown)


@pytest.mark.asyncio
async def test_pending_cleanup_is_claimed_resumed_once_after_restart() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    repository = MemoryGeneratedArtifactRepository()
    service = GeneratedArtifactService(repository, store, metrics=metrics)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )
    admitted = await repository.begin_delete(
        OWNER,
        created.id,
        expected_version=created.version,
        idempotency_key="restart-delete",
        idempotency_fingerprint=f"delete:{created.id}:{created.version}",
    )
    assert admitted.lifecycle is GeneratedArtifactLifecycle.DELETION_PENDING
    resumed = await service.resume_pending_cleanup(*OWNER)
    assert [item.lifecycle for item in resumed] == [GeneratedArtifactLifecycle.DELETED]
    assert store.objects == {}
    assert await service.resume_pending_cleanup(*OWNER) == []
    deletion = [
        item
        for item in metrics.observations
        if item.component == "aura.artifacts.deletion@1"
        and item.name == "artifact_deletion_outcome_count"
    ]
    assert deletion[-1].attributes.get("outcome") == "success"
    _assert_observation_shape(metrics)


@pytest.mark.asyncio
async def test_admission_claim_execute_settle_honors_leases_and_stale_claims() -> None:
    now = datetime(2026, 10, 28, tzinfo=UTC)

    def clock() -> datetime:
        return now

    metrics = _RecordingMetrics()
    store = _ObjectStore()
    repository = MemoryGeneratedArtifactRepository(clock=clock)
    service = GeneratedArtifactService(repository, store, metrics=metrics, clock=clock)
    created = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="text/plain; charset=utf-8",
        content="owner-private artifact bytes",
        creator_component=CREATOR,
    )

    admitted = await service.admit_delete(
        *OWNER,
        created.id,
        expected_version=created.version,
        idempotency_key="admission-delete",
    )
    assert admitted.lifecycle is GeneratedArtifactLifecycle.DELETION_PENDING
    assert store.objects
    first_claim = (await repository.claim_pending_cleanup(OWNER))[0]
    assert first_claim.status == "claimed"
    assert first_claim.claim_token
    assert first_claim.lease_expires_at == now + timedelta(seconds=180)
    assert await repository.claim_pending_cleanup(OWNER) == []

    with pytest.raises(GeneratedArtifactConflict, match="stale cleanup claim"):
        await repository.set_cleanup_state(
            OWNER,
            created.id,
            expected_version=admitted.version,
            lifecycle=GeneratedArtifactLifecycle.DELETED.value,
            idempotency_key="admission-delete",
            operation="delete",
            idempotency_fingerprint=first_claim.fingerprint,
            claim_token="stale-claim-token",
        )

    # A crashed worker's claim is reclaimable only after its lease expires.
    now += timedelta(seconds=181)
    second_claim = (await repository.claim_pending_cleanup(OWNER))[0]
    assert second_claim.claim_token != first_claim.claim_token
    now += timedelta(seconds=181)
    settled = await service.resume_pending_cleanup(*OWNER)
    assert [item.lifecycle for item in settled] == [GeneratedArtifactLifecycle.DELETED]
    assert store.objects == {}
    assert await service.resume_pending_cleanup(*OWNER) == []
    _assert_observation_shape(metrics)


@pytest.mark.asyncio
async def test_replayed_idempotent_lifecycle_observations_stay_bounded_and_content_free() -> None:
    metrics = _RecordingMetrics()
    store = _ObjectStore()
    service = _service(metrics, store)
    first = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="application/json",
        content={"secret": "owner-private artifact bytes"},
        creator_component=CREATOR,
        idempotency_key="same-request",
    )
    replay = await service.create_generated_artifact(
        *OWNER,
        kind="generated_text",
        title="owner-private title",
        media_type="application/json",
        content={"secret": "owner-private artifact bytes"},
        creator_component=CREATOR,
        idempotency_key="same-request",
    )
    assert replay.id == first.id
    assert replay.current_revision == first.current_revision
    assert service.lifecycle_events
    assert {event.event_id for event in service.lifecycle_events} == {
        service.lifecycle_events[0].event_id
    }
    for event in service.lifecycle_events:
        payload = event.payload()
        assert set(payload) == {
            "schemaVersion",
            "eventId",
            "eventType",
            "artifactId",
            "artifactVersion",
            "lifecycle",
            "currentRevision",
            "occurredAt",
            "correlationId",
            "causationId",
        }
        _assert_content_free(payload)
    forbidden = tuple(
        value
        for revision in (first.revision, replay.revision)
        if revision is not None
            for value in (revision.sha256, *store.objects)
    )
    _assert_observation_shape(metrics, extra_forbidden=forbidden)


@pytest.mark.asyncio
async def test_sql_repository_create_append_replay_conflict_and_outbox_atomicity() -> None:
    async with _sql_repository() as (repository, sessions):
        metrics = _RecordingMetrics()
        store = _ObjectStore()
        service = GeneratedArtifactService(repository, store, metrics=metrics)

        # A provider failure leaves only the durable admission reservation; no
        # artifact row, lifecycle event, or outbox envelope may be committed.
        store.put_failure = GeneratedArtifactObjectStoreError("secret exception text")
        with pytest.raises(GeneratedArtifactStorageError):
            await service.create_generated_artifact(
                *OWNER,
                kind="generated_text",
                title="owner-private title",
                media_type="text/plain; charset=utf-8",
                content="owner-private artifact bytes",
                creator_component=CREATOR,
                idempotency_key="sql-crash-restart",
            )
        async with sessions() as session:
            assert (await session.scalars(select(GeneratedArtifactRow))).all() == []
            assert (await session.scalars(select(OutboxRow))).all() == []

        # Re-admission after the simulated crash publishes exactly once.
        store.put_failure = None
        created = await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
            idempotency_key="sql-crash-restart",
        )
        replay = await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
            idempotency_key="sql-crash-restart",
        )
        assert replay.id == created.id
        with pytest.raises(GeneratedArtifactConflict, match="idempotency key"):
            await service.create_generated_artifact(
                *OWNER,
                kind="generated_text",
                title="owner-private title",
                media_type="text/plain; charset=utf-8",
                content="conflicting private bytes",
                creator_component=CREATOR,
                idempotency_key="sql-crash-restart",
            )

        revised = await service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
            idempotency_key="sql-append-replay",
        )
        replayed_append = await service.append_revision(
            *OWNER,
            created.id,
            expected_version=created.version,
            media_type="text/plain; charset=utf-8",
            content="owner-private revised bytes",
            creator_component=CREATOR,
            idempotency_key="sql-append-replay",
        )
        assert replayed_append.version == revised.version
        with pytest.raises(GeneratedArtifactConflict, match="idempotency key"):
            await service.append_revision(
                *OWNER,
                created.id,
                expected_version=created.version,
                media_type="text/plain; charset=utf-8",
                content="conflicting private bytes",
                creator_component=CREATOR,
                idempotency_key="sql-append-replay",
            )

        async with sessions() as session:
            artifacts = (await session.scalars(select(GeneratedArtifactRow))).all()
            revisions = (await session.scalars(select(GeneratedArtifactRevisionRow))).all()
            events = (await session.scalars(select(GeneratedArtifactLifecycleEventRow))).all()
            outbox = (await session.scalars(select(OutboxRow))).all()
            assert len(artifacts) == 1
            assert [row.revision for row in revisions] == [1, 2]
            assert [row.event_type for row in events] == [
                "artifact.created",
                "artifact.revision_published",
            ]
            assert len(outbox) == 2
            assert {row.topic for row in outbox} == {"aura.artifacts.generated.events.v1"}
            assert all("owner-private" not in repr(row.payload) for row in outbox)
            assert all("conflicting" not in repr(row.payload) for row in outbox)


@pytest.mark.asyncio
async def test_sql_cleanup_duplicate_claim_lease_stale_token_settlement_and_outbox() -> None:
    async with _sql_repository() as (repository, sessions):
        metrics = _RecordingMetrics()
        store = _ObjectStore()
        service = GeneratedArtifactService(repository, store, metrics=metrics)
        created = await service.create_generated_artifact(
            *OWNER,
            kind="generated_text",
            title="owner-private title",
            media_type="text/plain; charset=utf-8",
            content="owner-private artifact bytes",
            creator_component=CREATOR,
            idempotency_key="sql-cleanup-create",
        )
        admitted = await service.admit_delete(
            *OWNER,
            created.id,
            expected_version=created.version,
            idempotency_key="sql-cleanup-delete",
        )
        duplicate = await service.admit_delete(
            *OWNER,
            created.id,
            expected_version=created.version,
            idempotency_key="sql-cleanup-delete",
        )
        assert admitted.lifecycle is GeneratedArtifactLifecycle.DELETION_PENDING
        assert duplicate.lifecycle is GeneratedArtifactLifecycle.DELETION_PENDING
        with pytest.raises(GeneratedArtifactConflict, match="idempotency key"):
            await service.admit_delete(
                *OWNER,
                created.id,
                expected_version=created.version + 1,
                idempotency_key="sql-cleanup-delete",
            )

        first_claim = (await repository.claim_pending_cleanup(OWNER))[0]
        assert first_claim.claim_token is not None
        with pytest.raises(GeneratedArtifactConflict, match="stale cleanup claim"):
            await repository.set_cleanup_state(
                OWNER,
                created.id,
                expected_version=admitted.version,
                lifecycle=GeneratedArtifactLifecycle.DELETED.value,
                idempotency_key="sql-cleanup-delete",
                operation="delete",
                idempotency_fingerprint=first_claim.fingerprint,
                claim_token="stale-sql-claim",
            )

        async with sessions.begin() as session:
            await session.execute(
                update(GeneratedArtifactCleanupWorkRow)
                .where(
                    GeneratedArtifactCleanupWorkRow.artifact_id == created.id,
                    GeneratedArtifactCleanupWorkRow.operation == "delete",
                )
                .values(lease_expires_at=text("CURRENT_TIMESTAMP - interval '1 second'"))
            )
        second_claim = (await repository.claim_pending_cleanup(OWNER))[0]
        assert second_claim.claim_token != first_claim.claim_token

        # Expire the replacement claim and settle from a fresh service object,
        # modelling a worker crash followed by process restart.
        async with sessions.begin() as session:
            await session.execute(
                update(GeneratedArtifactCleanupWorkRow)
                .where(
                    GeneratedArtifactCleanupWorkRow.artifact_id == created.id,
                    GeneratedArtifactCleanupWorkRow.operation == "delete",
                )
                .values(lease_expires_at=text("CURRENT_TIMESTAMP - interval '1 second'"))
            )
        restarted = GeneratedArtifactService(repository, store, metrics=metrics)
        settled = await restarted.resume_pending_cleanup(*OWNER)
        assert [item.lifecycle for item in settled] == [GeneratedArtifactLifecycle.DELETED]
        assert store.objects == {}
        assert await restarted.resume_pending_cleanup(*OWNER) == []

        async with sessions() as session:
            events = (await session.scalars(select(GeneratedArtifactLifecycleEventRow))).all()
            outbox = (await session.scalars(select(OutboxRow))).all()
            assert [row.event_type for row in events] == [
                "artifact.created",
                "artifact.deletion_changed",
                "artifact.deletion_changed",
            ]
            assert len(outbox) == 3
            assert all("owner-private" not in repr(row.payload) for row in outbox)
