"""AURA-0093 metric, span, and typed-manifest observability coverage."""
# pyright: reportPrivateUsage=false, reportArgumentType=false

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest
import yaml
from aura_observatory.adapters.evaluation_definitions import InMemoryEvaluationDefinitionRepository
from aura_observatory.adapters.evaluation_orchestration import OtlpComponentSpanExporter
from aura_observatory.adapters.evaluation_storage import ClickHouseEvaluationScoreStore
from aura_observatory.evaluation_definitions import (
    CaseExpectations,
    DefinitionValidationError,
    EvaluationDefinitionService,
)
from aura_observatory.evaluation_evaluators import (
    EvaluationApplicationService,
    RetrievedEvidence,
    ScoreContext,
    TypedReplayEvidence,
)
from aura_observatory.evaluation_storage import ArtifactStorageError, EvaluationScore
from aura_observatory.metrics import ObservatoryMetrics
from test_aura0093_contract import IDS
from test_aura0093_definitions import _case, _dataset, _evaluator, _suite

ROOT = Path(__file__).parents[3]
MANIFEST_ROOT = ROOT / "services/aura-observatory/resources/component-manifests"


class _SpanTransport:
    def __init__(self) -> None:
        self.payloads: list[bytes] = []

    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        del endpoint, credential, timeout
        self.payloads.append(payload)


class _ScoreStore:
    def __init__(self) -> None:
        self.scores: list[EvaluationScore] = []

    async def insert_score(self, score: EvaluationScore) -> None:
        score.validate()
        self.scores.append(score)


class _StorageFailureResponse:
    status_code = 503

    def raise_for_status(self) -> None:
        import httpx

        request = httpx.Request("POST", "http://clickhouse")
        raise httpx.HTTPStatusError(
            "provider unavailable; password=do-not-leak",
            request=request,
            response=httpx.Response(503, request=request),
        )


class _StorageFailureClient:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _StorageFailureClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: Any, **_kwargs: Any) -> _StorageFailureResponse:
        return _StorageFailureResponse()


def _metric_total(
    metrics: ObservatoryMetrics, name: str, labels: dict[str, str] | None = None
) -> float:
    expected = labels or {}
    return sum(
        value
        for (metric_name, metric_labels), value in metrics.values.items()
        if metric_name == name and dict(metric_labels) == expected
    )


def _score_context(**overrides: Any) -> ScoreContext:
    values: dict[str, Any] = {
        "score_id": IDS["suite"],
        "job_id": IDS["dataset"],
        "result_id": IDS["case"],
        "dataset_id": IDS["dataset"],
        "suite_id": IDS["suite"],
        "evaluator_id": IDS["evaluator"],
        "evaluator_version": "1.0.0",
        "component_id": "aura.core.runner",
        "component_version": "1.0.0",
        "observed_at": datetime(2026, 10, 19, tzinfo=UTC),
        "case_id": IDS["case"],
    }
    values.update(overrides)
    return ScoreContext(**values)


async def _application_fixture(
    *,
    metrics: ObservatoryMetrics | None = None,
    case_payload: dict[str, Any] | None = None,
    evaluator_payload: dict[str, Any] | None = None,
    suite_payload: dict[str, Any] | None = None,
    score_store: Any | None = None,
) -> tuple[EvaluationApplicationService, _ScoreStore, ObservatoryMetrics]:
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    metric_sink = metrics or ObservatoryMetrics()
    admission = EvaluationDefinitionService(repository)
    await admission.admit_dataset(_dataset())
    await admission.admit_case(case_payload or _case())
    await admission.admit_evaluator(evaluator_payload or _evaluator())
    await admission.admit_suite(suite_payload or _suite())
    scores = score_store or _ScoreStore()
    return (
        EvaluationApplicationService(repository, scores, metrics=metric_sink),
        scores,
        metric_sink,
    )


@pytest.mark.asyncio
async def test_all_six_aura0093_metrics_and_spans_export_cleanly() -> None:
    transport = _SpanTransport()
    exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/traces",
        transport=transport,
        max_attempts=1,
    )
    metrics = ObservatoryMetrics(span_exporter=exporter)
    metrics.record(
        "observatory_evaluation_definition_operation_count",
        labels={"resource_type": "case", "outcome": "accepted"},
    )
    metrics.record(
        "observatory_evaluation_definition_validation_count",
        labels={"resource_type": "case", "outcome": "rejected"},
    )
    metrics.record(
        "observatory_evaluator_execution_count",
        labels={"evaluator_kind": "deterministic", "outcome": "success"},
    )
    metrics.record(
        "observatory_evaluator_duration_ms",
        4.0,
        labels={"evaluator_kind": "deterministic"},
    )
    metrics.record(
        "observatory_evaluator_outcome_count",
        labels={"evaluator_kind": "deterministic", "outcome": "fail"},
    )
    metrics.record(
        "observatory_evaluator_incomplete_evidence_count",
        labels={"evaluator_kind": "deterministic", "evidence_status": "not_observed"},
    )
    for outcome, error in (("success", None), ("failure", "validation"), ("failure", "validation")):
        metrics.record_component_span(
            component_id="aura.observatory.deterministic_evaluators",
            component_version="1.0.0",
            deployment_id="observatory",
            operation="evaluate",
            duration_ms=4.0,
            outcome=outcome,
            dependency="postgresql",
            error_code=error,
        )
    await metrics.flush()
    rendered = metrics.prometheus()
    for name in (
        "observatory_evaluation_definition_operation_count",
        "observatory_evaluation_definition_validation_count",
        "observatory_evaluator_execution_count",
        "observatory_evaluator_duration_ms",
        "observatory_evaluator_outcome_count",
        "observatory_evaluator_incomplete_evidence_count",
    ):
        assert name in rendered
    assert len(transport.payloads) == 3
    assert exporter.stats["dropped"] == 0
    assert exporter.stats["failures"] == 0
    assert all(
        marker not in b"".join(transport.payloads).decode("utf-8").casefold()
        for marker in ("prompt", "response", "content", "credential", "endpoint")
    )


def test_aura0093_typed_manifests_mutation_and_runtime_surface_are_valid() -> None:
    import importlib.util
    import sys

    validator_path = ROOT / "tooling/architecture/validate-observability.py"
    sys.path.insert(0, str(ROOT / "tooling/architecture"))
    spec = importlib.util.spec_from_file_location("observability_validator", validator_path)
    assert spec and spec.loader
    validator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(validator)
    manifests = [
        (path, yaml.safe_load(path.read_text(encoding="utf-8")))
        for path in MANIFEST_ROOT.glob("*.yaml")
        if yaml.safe_load(path.read_text(encoding="utf-8"))["component"]["id"]
        in {
            "aura.observatory.evaluation_definitions",
            "aura.observatory.deterministic_evaluators",
        }
    ]
    assert len(manifests) == 2
    assert validator.catalogue_errors(manifests) == []
    for path, manifest in manifests:
        mutated = deepcopy(manifest)
        mutated["metric_definitions"][0]["labels"] = ["raw_content"]
        errors = validator.catalogue_errors([(path, mutated)])
        assert any("labels must be declared" in error for error in errors)
        mutated = deepcopy(manifest)
        mutated["capture_policy"] = "full_content"
        assert any(
            "capture_policy must be metadata_only" in error
            for error in validator.catalogue_errors([(path, mutated)])
        )

@pytest.mark.asyncio
async def test_definition_rejection_records_bounded_validation_metrics_and_span() -> None:
    metrics = ObservatoryMetrics()
    service = EvaluationDefinitionService(
        InMemoryEvaluationDefinitionRepository(), metrics=metrics
    )
    malformed = _dataset()
    malformed["sourceKind"] = "unsupported"
    with pytest.raises(ValueError):
        await service.admit_dataset(malformed)
    for metric_name in (
        "observatory_evaluation_definition_operation_count",
        "observatory_evaluation_definition_validation_count",
    ):
        assert any(
            name == metric_name
            and dict(labels).get("resource_type") == "dataset"
            and dict(labels).get("outcome") == "rejected"
            for (name, labels), _value in metrics.values.items()
        )


class _FailingDefinitionRepository(InMemoryEvaluationDefinitionRepository):
    async def create_dataset(self, definition: Any) -> Any:
        del definition
        raise RuntimeError("simulated definition-state outage")


@pytest.mark.asyncio
async def test_definition_service_records_success_validation_and_state_failure_observability(
) -> None:
    transport = _SpanTransport()
    exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/traces", transport=transport, max_attempts=1
    )
    metrics = ObservatoryMetrics(span_exporter=exporter)
    repository = InMemoryEvaluationDefinitionRepository()
    service = EvaluationDefinitionService(repository, metrics=metrics)
    await service.admit_dataset(_dataset())
    malformed = _dataset()
    malformed["sourceKind"] = "unsupported"
    with pytest.raises(DefinitionValidationError):
        await service.admit_dataset(malformed)

    failing = EvaluationDefinitionService(_FailingDefinitionRepository(), metrics=metrics)
    with pytest.raises(DefinitionValidationError, match="storage failure"):
        await failing.admit_dataset(_dataset())
    await metrics.flush()

    assert _metric_total(
        metrics,
        "observatory_evaluation_definition_operation_count",
        {"resource_type": "dataset", "outcome": "accepted"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluation_definition_operation_count",
        {"resource_type": "dataset", "outcome": "rejected"},
    ) == 2
    spans = metrics.component_spans
    assert len(spans) == 3
    assert all(
        (span.component_id, span.component_version, span.operation)
        == ("aura.observatory.evaluation_definitions", "1.0.0", "admit")
        for span in spans
    )
    assert [span.outcome for span in spans] == ["success", "failure", "failure"]
    assert [span.dependency for span in spans] == [
        "evaluation_definition_state",
        "evaluation_definition_state",
        "evaluation_definition_state",
    ]
    assert [span.error_code for span in spans] == [None, "validation", "database_failure"]
    assert len(transport.payloads) == 3
    assert exporter.stats["dropped"] == 0
    assert exporter.stats["failures"] == 0
    assert all(
        marker not in b"".join(transport.payloads).decode().casefold()
        for marker in ("prompt", "response", "content", "credential", "endpoint")
    )


@pytest.mark.asyncio
async def test_application_service_records_success_incomplete_not_applicable_and_failure_metrics(
) -> None:
    metrics = ObservatoryMetrics()
    application, scores, metrics = await _application_fixture(metrics=metrics)
    complete = TypedReplayEvidence(fields={"answer": "a" * 64})
    persisted = await application.evaluate_case(
        case_id=IDS["case"],
        case_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        context=_score_context(),
        evidence=complete,
    )
    assert len(persisted) == 1 and persisted[0].outcome == "pass"

    with pytest.raises(DefinitionValidationError, match="provenance"):
        await application.evaluate_case(
            case_id=IDS["case"],
            case_version="1.0.0",
            suite_id=IDS["suite"],
            suite_version="1.0.0",
            evaluator_id=IDS["evaluator"],
            evaluator_version="1.0.0",
            context=_score_context(suite_id="wrong-suite"),
            evidence=complete,
        )

    incomplete = await application.evaluate_case(
        case_id=IDS["case"],
        case_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        context=_score_context(score_id=IDS["case"]),
        evidence=TypedReplayEvidence(),
    )
    assert incomplete[0].evidence_status == "not_observed"

    failed = await application.evaluate_case(
        case_id=IDS["case"],
        case_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        context=_score_context(score_id=IDS["dataset"]),
        evidence=TypedReplayEvidence(fields={"answer": "b" * 64}),
    )
    assert failed[0].outcome == "fail"
    assert len(scores.scores) == 3
    assert _metric_total(
        metrics,
        "observatory_evaluator_execution_count",
        {"evaluator_kind": "deterministic", "outcome": "success"},
    ) == 3
    assert _metric_total(
        metrics,
        "observatory_evaluator_outcome_count",
        {"evaluator_kind": "deterministic", "outcome": "pass"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_outcome_count",
        {"evaluator_kind": "deterministic", "outcome": "fail"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_outcome_count",
        {"evaluator_kind": "deterministic", "outcome": "unknown"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_incomplete_evidence_count",
        {"evaluator_kind": "deterministic", "evidence_status": "not_observed"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_duration_ms",
        {"evaluator_kind": "deterministic"},
    ) > 0
    duration_key = (
        "observatory_evaluator_duration_ms",
        (("evaluator_kind", "deterministic"),),
    )
    assert metrics._histogram_counts[duration_key] == 3
    assert len(metrics.component_spans) == 4
    assert [span.outcome for span in metrics.component_spans] == [
        "success",
        "failure",
        "success",
        "success",
    ]
    assert all(
        (span.component_id, span.component_version, span.operation, span.dependency)
        == (
            "aura.observatory.deterministic_evaluators",
            "1.0.0",
            "evaluate",
            "evaluation_definition_state",
        )
        for span in metrics.component_spans
    )
    assert metrics.component_spans[1].error_code == "validation"


@pytest.mark.asyncio
async def test_application_service_persists_not_applicable_without_compute_duration_gap() -> None:
    from aura_observatory.evaluation_evaluators import BUILTIN_EVALUATOR_IDS

    retrieval_id = next(
        identifier
        for identifier, family in BUILTIN_EVALUATOR_IDS.items()
        if family == "retrieval"
    )
    evaluator_payload = _evaluator()
    evaluator_payload["evaluatorId"] = retrieval_id
    evaluator_payload["metric"] = "evaluation.retrieval.ratio"
    suite_payload = _suite()
    suite_payload["evaluators"][0]["evaluatorId"] = retrieval_id
    suite_payload["manualThresholds"][0]["evaluator"]["evaluatorId"] = retrieval_id
    suite_payload["manualThresholds"][0]["metric"] = "evaluation.retrieval.ratio"
    repository = InMemoryEvaluationDefinitionRepository()
    repository.add_component("aura.core.runner", "1.0.0")
    admission = EvaluationDefinitionService(repository)
    await admission.admit_dataset(_dataset())
    admitted_case = await admission.admit_case(_case())
    await admission.admit_evaluator(evaluator_payload)
    await admission.admit_suite(suite_payload)
    repository.cases[(IDS["case"], "1.0.0")] = replace(
        admitted_case,
        expectations=CaseExpectations(
            MappingProxyType({"retrieval": {"rankedEvidence": [], "cutoffs": [1]}})
        ),
    )
    scores = _ScoreStore()
    metrics = ObservatoryMetrics()
    application = EvaluationApplicationService(repository, scores, metrics=metrics)
    result = await application.evaluate_case(
        case_id=IDS["case"],
        case_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=retrieval_id,
        evaluator_version="1.0.0",
        context=_score_context(evaluator_id=retrieval_id),
        evidence=TypedReplayEvidence(),
    )
    assert len(result) == len(scores.scores) == 7
    assert all(score.evidence_status == "not_applicable" for score in result)
    assert all(score.outcome is None for score in result)
    assert _metric_total(
        metrics,
        "observatory_evaluator_execution_count",
        {"evaluator_kind": "deterministic", "outcome": "success"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_outcome_count",
        {"evaluator_kind": "deterministic", "outcome": "unknown"},
    ) == 7
    assert _metric_total(
        metrics,
        "observatory_evaluator_duration_ms",
        {"evaluator_kind": "deterministic"},
    ) > 0
    assert metrics._histogram_counts[
        ("observatory_evaluator_duration_ms", (("evaluator_kind", "deterministic"),))
    ] == 1
    assert metrics.component_spans[0].outcome == "success"


@pytest.mark.asyncio
async def test_application_service_records_one_invocation_for_multiple_retrieval_scores() -> None:
    from aura_observatory.evaluation_evaluators import BUILTIN_EVALUATOR_IDS

    retrieval_id = next(
        identifier
        for identifier, family in BUILTIN_EVALUATOR_IDS.items()
        if family == "retrieval"
    )
    evaluator_payload = _evaluator()
    evaluator_payload["evaluatorId"] = retrieval_id
    evaluator_payload["metric"] = "evaluation.retrieval.ratio"
    suite_payload = _suite()
    suite_payload["evaluators"][0]["evaluatorId"] = retrieval_id
    suite_payload["manualThresholds"][0]["evaluator"]["evaluatorId"] = retrieval_id
    suite_payload["manualThresholds"][0]["metric"] = "evaluation.retrieval.ratio"
    metrics = ObservatoryMetrics()
    application, scores, metrics = await _application_fixture(
        metrics=metrics,
        evaluator_payload=evaluator_payload,
        suite_payload=suite_payload,
    )
    result = await application.evaluate_case(
        case_id=IDS["case"],
        case_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=retrieval_id,
        evaluator_version="1.0.0",
        context=_score_context(
            evaluator_id=retrieval_id,
            score_id=IDS["case"],
        ),
        evidence=TypedReplayEvidence(
            retrieved=(
                RetrievedEvidence("fixture-1", 1, 3, True, True),
            )
        ),
    )
    assert len(result) == len(scores.scores) > 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_execution_count",
        {"evaluator_kind": "deterministic", "outcome": "success"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_duration_ms",
        {"evaluator_kind": "deterministic"},
    ) > 0
    assert metrics._histogram_counts[
        ("observatory_evaluator_duration_ms", (("evaluator_kind", "deterministic"),))
    ] == 1
    assert len(metrics.component_spans) == 1


@pytest.mark.asyncio
async def test_score_store_failure_keeps_compute_telemetry_and_traces_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient",
        _StorageFailureClient,
    )
    transport = _SpanTransport()
    exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/traces", transport=transport, max_attempts=1
    )
    metrics = ObservatoryMetrics(span_exporter=exporter)
    store = ClickHouseEvaluationScoreStore(
        "http://clickhouse", database="aura_observatory", metrics=metrics
    )
    application, _scores, metrics = await _application_fixture(
        metrics=metrics, score_store=store
    )
    with pytest.raises(ArtifactStorageError, match="database_failure"):
        await application.evaluate_case(
            case_id=IDS["case"],
            case_version="1.0.0",
            suite_id=IDS["suite"],
            suite_version="1.0.0",
            evaluator_id=IDS["evaluator"],
            evaluator_version="1.0.0",
            context=_score_context(),
            evidence=TypedReplayEvidence(fields={"answer": "a" * 64}),
        )
    assert _metric_total(
        metrics,
        "observatory_evaluator_execution_count",
        {"evaluator_kind": "deterministic", "outcome": "success"},
    ) == 1
    assert _metric_total(
        metrics,
        "observatory_evaluator_duration_ms",
        {"evaluator_kind": "deterministic"},
    ) > 0
    assert metrics._histogram_counts[
        ("observatory_evaluator_duration_ms", (("evaluator_kind", "deterministic"),))
    ] == 1
    assert _metric_total(
        metrics,
        "observatory_evaluation_dependency_outcome_count",
        {"operation": "clickhouse", "outcome": "failure"},
    ) == 2
    spans = metrics.component_spans
    clickhouse_spans = [span for span in spans if span.operation == "write"]
    application_spans = [span for span in spans if span.operation == "evaluate"]
    assert len(clickhouse_spans) == 2
    assert len(application_spans) == 1
    assert {span.error_code for span in clickhouse_spans} == {
        "database_failure",
        "storage_failure",
    }
    assert all(
        (span.dependency, span.outcome) == ("clickhouse", "failure")
        for span in clickhouse_spans
    )
    assert (
        application_spans[0].dependency,
        application_spans[0].outcome,
        application_spans[0].error_code,
    ) == ("evaluation_definition_state", "success", None)
    await metrics.flush()
    assert exporter.stats["dropped"] == 0
    assert exporter.stats["failures"] == 0
    assert "password=do-not-leak" not in b"".join(transport.payloads).decode()
