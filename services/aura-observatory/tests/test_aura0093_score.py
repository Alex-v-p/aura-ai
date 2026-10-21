"""AURA-0093 typed score application and ClickHouse metadata round trips."""
# pyright: reportPrivateUsage=false, reportArgumentType=false

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from aura_observatory.adapters.evaluation_definitions import (
    InMemoryEvaluationDefinitionRepository,
)
from aura_observatory.adapters.evaluation_storage import ClickHouseEvaluationScoreStore
from aura_observatory.evaluation_definitions import (
    DefinitionValidationError,
    EvaluationDefinitionService,
)
from aura_observatory.evaluation_evaluators import (
    EvaluationApplicationService,
    ScoreContext,
    TypedReplayEvidence,
)
from aura_observatory.evaluation_storage import ArtifactStorageError, EvaluationScore
from aura_observatory.metrics import ObservatoryMetrics
from test_aura0093_contract import IDS
from test_aura0093_definitions import _case, _dataset, _evaluator, _suite


class _ScoreStore:
    def __init__(self) -> None:
        self.scores: list[EvaluationScore] = []

    async def insert_score(self, score: EvaluationScore) -> None:
        score.validate()
        self.scores.append(score)


@pytest.mark.asyncio
async def test_application_service_persists_typed_scores_through_score_port() -> None:
    definitions = InMemoryEvaluationDefinitionRepository()
    definitions.add_component("aura.core.runner", "1.0.0")
    admission = EvaluationDefinitionService(definitions)
    await admission.admit_dataset(_dataset())
    await admission.admit_case(_case())
    await admission.admit_evaluator(_evaluator())
    await admission.admit_suite(_suite())
    scores = _ScoreStore()
    application = EvaluationApplicationService(definitions, scores)

    context = ScoreContext(
        score_id=IDS["suite"],
        job_id=IDS["dataset"],
        result_id=IDS["case"],
        dataset_id=IDS["dataset"],
        suite_id=IDS["suite"],
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        component_id="aura.core.runner",
        component_version="1.0.0",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        case_id=IDS["case"],
    )
    persisted = await application.evaluate_case(
        case_id=IDS["case"],
        case_version="1.0.0",
        suite_id=IDS["suite"],
        suite_version="1.0.0",
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        context=context,
        evidence=TypedReplayEvidence(),
    )
    assert persisted == tuple(scores.scores)
    assert len(persisted) == 1
    score = persisted[0]
    assert score.case_id == IDS["case"]
    assert score.evaluator_id == IDS["evaluator"]
    assert score.metric == "evaluation.exact.ratio"
    assert score.evidence_status == "not_observed"
    assert score.value is None and score.outcome is None
    assert score.threshold_applicable is True
    assert score.threshold_reference == f'{IDS["evaluator"]}:1.0.0:evaluation.exact.ratio'
    assert score.threshold_value == 0.95
    assert score.threshold_operator == "greater_than_or_equal"
    assert score.threshold_status == "inconclusive"


@pytest.mark.asyncio
async def test_application_service_rejects_unbound_score_component_provenance() -> None:
    definitions = InMemoryEvaluationDefinitionRepository()
    definitions.add_component("aura.core.runner", "1.0.0")
    admission = EvaluationDefinitionService(definitions)
    await admission.admit_dataset(_dataset())
    await admission.admit_case(_case())
    await admission.admit_evaluator(_evaluator())
    await admission.admit_suite(_suite())
    application = EvaluationApplicationService(definitions, _ScoreStore())
    context = ScoreContext(
        score_id=IDS["suite"],
        job_id=IDS["dataset"],
        result_id=IDS["case"],
        dataset_id=IDS["dataset"],
        suite_id=IDS["suite"],
        evaluator_id=IDS["evaluator"],
        evaluator_version="1.0.0",
        component_id="aura.unbound.component",
        component_version="9.0.0",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        case_id=IDS["case"],
    )
    with pytest.raises(DefinitionValidationError, match="provenance"):
        await application.evaluate_case(
            case_id=IDS["case"],
            case_version="1.0.0",
            suite_id=IDS["suite"],
            suite_version="1.0.0",
            evaluator_id=IDS["evaluator"],
            evaluator_version="1.0.0",
            context=context,
            evidence=TypedReplayEvidence(),
        )


class _Response:
    status_code = 200

    def raise_for_status(self) -> None:
        return None


class _HTTPClient:
    calls: list[dict[str, Any]] = []

    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _HTTPClient:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def post(self, *_args: Any, **kwargs: Any) -> _Response:
        self.calls.append(kwargs)
        return _Response()


class _FailureHTTPClient(_HTTPClient):
    async def post(self, *_args: Any, **kwargs: Any) -> _Response:
        del kwargs
        response = _Response()
        response.status_code = 503
        request = httpx.Request("POST", "http://clickhouse")
        provider_response = httpx.Response(503, request=request)
        response.raise_for_status = lambda: (_ for _ in ()).throw(
            httpx.HTTPStatusError(
                "clickhouse unavailable", request=request, response=provider_response
            )
        )
        return response


def _score(*, status: str = "observed") -> EvaluationScore:
    measurement: dict[str, Any] = {
        "score_id": IDS["suite"],
        "job_id": IDS["dataset"],
        "result_id": IDS["case"],
        "dataset_id": IDS["dataset"],
        "suite_id": IDS["suite"],
        "evaluator_id": IDS["evaluator"],
        "evaluator_version": "1.0.0",
        "evaluator_kind": "deterministic",
        "component_id": "aura.core.runner",
        "component_version": "1.0.0",
        "capture_classification": "metadata_only",
        "evidence_status": status,
        "value": 1.0 if status == "observed" else None,
        "outcome": "pass" if status == "observed" else None,
        "observed_at": datetime(2026, 10, 19, 1, 2, 3, 123456, tzinfo=UTC),
        "case_id": IDS["case"],
        "metric": "evaluation.exact.ratio",
        "failure_code": None if status == "observed" else "missing_evidence",
        "evidence_reference": "evidence-1",
        "reproducibility": "deterministic-v1",
        "threshold_applicable": True,
        "threshold_reference": "threshold-1",
    }
    return EvaluationScore(**measurement)


@pytest.mark.asyncio
async def test_clickhouse_score_insert_preserves_complete_content_free_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _HTTPClient.calls = []
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient", _HTTPClient
    )
    store = ClickHouseEvaluationScoreStore("http://clickhouse", database="aura_observatory")
    await store.insert_score(_score())
    row = json.loads(_HTTPClient.calls[0]["content"].split("\n", 1)[1])
    assert row["case_id"] == IDS["case"]
    assert row["metric"] == "evaluation.exact.ratio"
    assert row["evidence_reference_id"] == "evidence-1"
    assert row["reproducibility"] == "deterministic-v1"
    assert row["threshold_applicable"] == 1
    assert row["threshold_reference"] == "threshold-1"
    serialized = json.dumps(row, sort_keys=True)
    # Prompt/rubric version fields are bounded provenance metadata. Only
    # content-bearing values and private transport fields are prohibited.
    assert all(
        marker not in serialized.casefold()
        for marker in ("response", "content", "filename", "endpoint", "credential")
    )


@pytest.mark.asyncio
async def test_clickhouse_write_failure_records_one_dependency_span_without_rejected_export(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    metrics = ObservatoryMetrics()
    monkeypatch.setattr(
        "aura_observatory.adapters.evaluation_storage.httpx.AsyncClient", _FailureHTTPClient
    )
    store = ClickHouseEvaluationScoreStore(
        "http://clickhouse", database="aura_observatory", metrics=metrics
    )
    with pytest.raises(ArtifactStorageError, match="database_failure"):
        await store.insert_score(_score())
    assert metrics.values[
        (
            "observatory_evaluation_dependency_outcome_count",
            (("operation", "clickhouse"), ("outcome", "failure")),
        )
    ] == 1
    assert len(metrics.component_spans) == 1
    span = metrics.component_spans[0]
    assert (
        span.component_id,
        span.component_version,
        span.operation,
        span.dependency,
        span.outcome,
        span.error_code,
    ) == (
        "aura.observatory.deterministic_evaluators",
        "1.0.0",
        "write",
        "clickhouse",
        "failure",
        "database_failure",
    )


def test_score_round_trip_preserves_observed_incomplete_and_not_applicable_metadata() -> None:
    for status in ("observed", "not_observed", "not_applicable"):
        score = _score(status=status)
        score.validate()
        assert score.case_id == IDS["case"]
        assert score.metric == "evaluation.exact.ratio"
        assert score.reproducibility == "deterministic-v1"
        if status != "observed":
            assert score.value is None and score.outcome is None
