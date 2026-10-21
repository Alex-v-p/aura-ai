"""Independent AURA-0096 experiment, baseline, and regression checks.

These tests intentionally exercise the Observatory application boundary with
service-owned fakes.  They do not import Aura Core, submit evaluation content,
or modify protected repository baselines.  The optional migration checks use a
throw-away PostgreSQL schema or ClickHouse database when the corresponding
repository-scoped environment variable is present.
"""

# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false, reportOperatorIssue=false, reportReturnType=false, reportCallIssue=false

from __future__ import annotations

import asyncio
import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from uuid import uuid4

import aura_observatory.adapters.evaluation_experiments as _experiment_adapter
import httpx
import psycopg
import pytest
from aura_observatory.adapters.evaluation_experiments import (
    PostgresExperimentEvidencePort,
    PostgresExperimentReferencePort,
)
from aura_observatory.adapters.evaluation_live import PostgresEvaluationLiveEventStore
from aura_observatory.entrypoints.api.app import create_app
from aura_observatory.evaluation_experiments import (
    EvaluationExperimentService,
    EvidenceAssessment,
    ExperimentConflict,
    ExperimentError,
    MemoryExperimentRepository,
)
from aura_observatory.settings import Settings
from test_aura0095_backend import _client, _client_for

ROOT = Path(__file__).parents[3]
MIGRATION_ROOT = ROOT / "services/aura-observatory/migrations/postgresql/versions"
CLICKHOUSE_MIGRATION_ROOT = ROOT / "services/aura-observatory/migrations/clickhouse/versions"
NOW = "2026-10-21T09:00:00Z"
OWNER_ID = "owner-identity-must-not-appear"


def _id() -> str:
    return str(uuid4())


def _digest(seed: str) -> str:
    return (seed * 64)[:64]


def _component(version: str = "1.0.0") -> dict[str, object]:
    return {
        "componentId": "aura.core.api",
        "componentVersion": version,
        "codeRevision": "code-20261021",
        "imageDigest": _digest("a"),
        "configurationRevision": "config-20261021",
    }


def _variant(
    name: str, *, version: str = "1.0.0", model_version: str = "1.0.0"
) -> dict[str, object]:
    return {
        "variantId": name,
        "components": [_component(version)],
        "model": {
            "modelId": "local-model",
            "modelVersion": model_version,
            "modelDigest": _digest("b"),
        },
        "configurationRevision": f"config-{name}",
        "promptRevision": f"prompt-{name}",
        "policyRevision": f"policy-{name}",
        "deploymentRevision": f"deployment-{name}",
    }


def _experiment(*, first_variant: dict[str, object] | None = None) -> dict[str, object]:
    return {
        "experimentId": _id(),
        "experimentVersion": "1.0.0",
        "suite": {"id": _id(), "version": "1.0.0"},
        "variants": [
            first_variant or _variant("control"),
            _variant("candidate", version="2.0.0", model_version="2.0.0"),
        ],
        "cohorts": [{"cohortId": "smoke", "caseIds": [_id(), _id()]}],
        "captureAuthorization": {
            "captureClassification": "metadata_only",
            "authorizationPurpose": "synthetic_benchmark",
            "authorizationContext": "controlled_evaluation",
            "retentionMode": "manual_only",
        },
        "limits": {
            "timeoutMs": 30_000,
            "maxConcurrency": 2,
            "maxAttempts": 2,
            "maxCases": 10,
            "maxInputBytes": 1024,
            "maxOutputBytes": 1024,
            "maxTokens": 1000,
        },
        "retentionMode": "manual_only",
        "provenance": {
            "createdByComponent": "aura.observatory.tests",
            "createdByVersion": "1.0.0",
            "sourceDigest": _digest("c"),
        },
        "createdAt": NOW,
    }


def _threshold(
    *, mode: str = "absolute", direction: str = "higher_is_better", minimum: int = 2
) -> dict[str, object]:
    return {
        "metric": "quality.score",
        "metricClass": "quality",
        "evaluator": {"evaluatorId": _id(), "evaluatorVersion": "1.0.0", "kind": "deterministic"},
        "unit": "score",
        "aggregation": "mean",
        "direction": direction,
        "comparisonMode": mode,
        "thresholdValue": 0.10,
        "minimumSampleSize": minimum,
        "missingEvidencePolicy": "inconclusive",
    }


def _baseline(experiment: dict[str, object]) -> dict[str, object]:
    suite = experiment["suite"]
    assert isinstance(suite, dict)
    return {
        "baselineId": _id(),
        "baselineVersion": "1.0.0",
        "experiment": {
            "id": experiment["experimentId"],
            "version": experiment["experimentVersion"],
        },
        "suite": {
            "id": suite["resourceId"],
            "version": suite["resourceVersion"],
        },
        "variantId": "control",
        "cohortIds": ["smoke"],
        "sourceResultIds": [_id(), _id()],
        "sourceResultFrom": NOW,
        "sourceResultTo": "2026-10-21T09:30:00Z",
        "thresholds": [_threshold()],
        "captureClassification": "metadata_only",
        "reproducibility": {
            "seed": 7,
            "clockMode": "fixed",
            "timeZone": "UTC",
            "locale": "en-US",
            "fixtureRevision": "fixture-1",
            "promptRevision": "prompt-control",
            "policyRevision": "policy-control",
        },
        "retentionMode": "manual_only",
        "provenance": dict(experiment["provenance"]),
        "createdAt": NOW,
    }


async def _promote(
    service: EvaluationExperimentService, baseline: dict[str, object]
) -> dict[str, object]:
    """Promote a candidate before using it as regression evidence."""
    return await service.promote_baseline(
        str(baseline["baselineId"]),
        {
            "expectedCandidateVersion": str(baseline["baselineVersion"]),
            "authorizationRecordId": _id(),
            "promotedAt": NOW,
        },
    )


class ResultEvidence:
    """Content-free evidence keyed by persisted result identity."""

    def __init__(self, values: dict[str, tuple[float, ...]]) -> None:
        self.values = values

    async def metric_values(
        self,
        result_ids: list[str] | tuple[str, ...],
        metric: str,
        cohort_ids: list[str] | tuple[str, ...],
        *,
        cohort_case_ids: dict[str, list[str] | tuple[str, ...]] | None = None,
    ) -> tuple[float, ...] | None:
        del metric, cohort_ids, cohort_case_ids
        values: list[float] = []
        for result_id in result_ids:
            values.extend(self.values.get(result_id, ()))
        return tuple(values) if values else None

    async def evidence(
        self, result_ids: list[str] | tuple[str, ...], *, version: str
    ) -> list[dict[str, str]]:
        return [
            {"kind": "result", "id": result_id, "digest": _digest(result_id), "version": version}
            for result_id in result_ids[:512]
        ]

    async def assess(
        self,
        baseline_result_ids: list[str] | tuple[str, ...],
        candidate_result_ids: list[str] | tuple[str, ...],
        metric: str,
        cohort_ids: list[str] | tuple[str, ...],
        *,
        exact_version: str,
        expected_evaluator: dict[str, object] | None = None,
        baseline_variant: dict[str, object] | None = None,
        candidate_variant: dict[str, object] | None = None,
        cohort_case_ids: dict[str, list[str] | tuple[str, ...]] | None = None,
    ) -> EvidenceAssessment:
        del exact_version, expected_evaluator, baseline_variant, candidate_variant
        baseline = await self.metric_values(
            baseline_result_ids, metric, cohort_ids, cohort_case_ids=cohort_case_ids
        )
        candidate = await self.metric_values(
            candidate_result_ids, metric, cohort_ids, cohort_case_ids=cohort_case_ids
        )
        return EvidenceAssessment(
            "complete" if baseline is not None and candidate is not None else "missing",
            baseline or (),
            candidate or (),
            tuple(
                await self.evidence(
                    tuple(baseline_result_ids) + tuple(candidate_result_ids),
                    version="1.0.0",
                )
            ),
        )

    async def persist_facts(
        self,
        regression_id: str,
        regression_version: str,
        metric: str,
        metric_class: str,
        cohort_ids: list[str] | tuple[str, ...],
        baseline_values: tuple[float, ...],
        candidate_values: tuple[float, ...],
        baseline_result_ids: list[str] | tuple[str, ...],
        candidate_result_ids: list[str] | tuple[str, ...],
        evidence_state: str,
        *,
        threshold_index: int = 0,
    ) -> None:
        del regression_id, regression_version, metric, metric_class, cohort_ids
        del baseline_values, candidate_values, baseline_result_ids, candidate_result_ids
        del evidence_state, threshold_index


def _regression(
    experiment: dict[str, object],
    baseline: dict[str, object],
    *,
    baseline_ids: list[str],
    candidate_ids: list[str],
) -> dict[str, object]:
    return {
        "regressionId": _id(),
        "regressionVersion": "1.0.0",
        "experiment": {
            "id": experiment["experimentId"],
            "version": experiment["experimentVersion"],
        },
        "baseline": {"id": baseline["baselineId"], "version": baseline["baselineVersion"]},
        "variantId": "candidate",
        "cohortIds": ["smoke"],
        "baselineResultIds": baseline_ids,
        "candidateResultIds": candidate_ids,
        "thresholds": [_threshold()],
        "createdAt": NOW,
    }


def _assert_no_forbidden_keys(value: object) -> None:
    forbidden = {"prompt", "response", "content", "credential", "owner", "endpoint"}
    if isinstance(value, dict):
        assert forbidden.isdisjoint(value)
        for child in value.values():
            _assert_no_forbidden_keys(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_forbidden_keys(child)


@pytest.mark.asyncio
async def test_experiment_is_closed_bounded_and_idempotent() -> None:
    repository = MemoryExperimentRepository()
    service = EvaluationExperimentService(repository=repository)
    body = _experiment()
    command_key = "00000000-0000-4000-8000-000000000096"
    created = await service.create_experiment(body, idempotency_key=command_key)
    repeated = await service.create_experiment(body, idempotency_key=command_key)
    assert repeated == created
    assert created["retentionMode"] == "manual_only"
    assert len(created["variants"]) == 2
    assert set(created) == {
        "schemaVersion",
        "resourceType",
        "experimentId",
        "experimentVersion",
        "state",
        "suite",
        "variants",
            "cohorts",
            "captureAuthorization",
            "limits",
            "captureClassification",
        "retentionMode",
        "provenance",
        "createdAt",
    }
    with pytest.raises(ExperimentConflict):
        changed = dict(body)
        changed["suite"] = {"id": _id(), "version": "1.0.0"}
        await service.create_experiment(changed, idempotency_key=command_key)
    with pytest.raises(ExperimentError):
        unsafe = dict(body)
        unsafe["ownerIdentity"] = OWNER_ID
        await service.create_experiment(unsafe)


@pytest.mark.asyncio
async def test_experiment_api_is_owner_only_csrf_protected_and_no_store() -> None:
    client, session = await _client()
    async with client:
        listed = await client.get("/api/v1/evaluation/experiments")
        assert listed.status_code == 200
        assert listed.headers["cache-control"] == "no-store"
        missing_csrf = await client.post(
            "/api/v1/evaluation/experiments",
            json={},
            headers={"Idempotency-Key": "experiment-api-csrf-1"},
        )
        assert missing_csrf.status_code == 403
        cross_origin = await client.post(
            "/api/v1/evaluation/experiments",
            json={},
            headers={
                "Idempotency-Key": "experiment-api-csrf-2",
                "X-CSRF-Token": session.csrf_token,
                "Origin": "https://not-aura.example.invalid",
            },
        )
        assert cross_origin.status_code == 403

    other_client, _ = await _client_for("different-owner")
    async with other_client:
        assert (await other_client.get("/api/v1/evaluation/experiments")).status_code == 403

    unauthenticated = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=create_app(Settings(owner_subject="owner-subject"), testing=True)
        ),
        base_url=Settings(owner_subject="owner-subject").public_origin,
    )
    async with unauthenticated:
        assert (await unauthenticated.get("/api/v1/evaluation/experiments")).status_code == 401


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("variants", [_variant("only")]),
        ("cohorts", [{"cohortId": "bad", "caseIds": []}]),
        ("retentionMode", "scheduled"),
        ("createdAt", "2026-10-21T09:00:00+02:00"),
    ],
)
async def test_experiment_rejects_invalid_or_unbounded_definition(
    field: str, value: object
) -> None:
    service = EvaluationExperimentService()
    body = _experiment()
    body[field] = value
    with pytest.raises(ExperimentError):
        await service.create_experiment(body)


@pytest.mark.asyncio
async def test_baseline_requires_exact_experiment_bindings_and_is_manual_only() -> None:
    service = EvaluationExperimentService()
    experiment = await service.create_experiment(_experiment())
    body = _baseline(experiment)
    candidate = await service.create_baseline(
        body, idempotency_key="00000000-0000-4000-8000-000000000097"
    )
    assert candidate["state"] == "candidate"
    assert candidate["immutable"] is True
    assert candidate["promotionMode"] == "manual_owner_action"

    invalid_suite = dict(body)
    invalid_suite["suite"] = {"id": _id(), "version": "1.0.0"}
    with pytest.raises(ExperimentError):
        await service.create_baseline(invalid_suite)
    invalid_variant = dict(body)
    invalid_variant["variantId"] = "not-declared"
    with pytest.raises(ExperimentError):
        await service.create_baseline(invalid_variant)
    invalid_cohort = dict(body)
    invalid_cohort["cohortIds"] = ["not-declared"]
    with pytest.raises(ExperimentError):
        await service.create_baseline(invalid_cohort)
    reversed_range = dict(body)
    reversed_range["sourceResultFrom"] = "2026-10-21T10:00:00Z"
    with pytest.raises(ExperimentError):
        await service.create_baseline(reversed_range)


def test_result_scope_keeps_variant_prompt_policy_separate_from_stable_replay() -> None:
    experiment = _experiment()
    normalized_experiment = {
        "suite": {"resourceId": experiment["suite"]["id"], "resourceVersion": "1.0.0"},
        "cohorts": experiment["cohorts"],
    }
    candidate = _variant("candidate", version="2.0.0", model_version="2.0.0")
    candidate["components"][0]["configurationRevision"] = candidate["configurationRevision"]
    stable_replay = {
        "seed": 7,
        "clockMode": "fixed",
        "timeZone": "UTC",
        "locale": "en-US",
        "fixtureRevision": "fixture-1",
        "promptRevision": "prompt-control",
        "policyRevision": "policy-control",
    }
    provenance = {
        "dataset": {"id": _id(), "version": "1.0.0"},
        "suite": normalized_experiment["suite"],
        "evaluators": [],
        "components": candidate["components"],
        "model": candidate["model"],
        "deploymentRevision": candidate["deploymentRevision"],
        "reproducibility": {
            **stable_replay,
            "promptRevision": "prompt-candidate",
            "policyRevision": "policy-candidate",
        },
    }
    results: list[dict[str, object]] = []
    for case_id, received_at in zip(
        experiment["cohorts"][0]["caseIds"],
        (NOW, "2026-10-21T09:30:00Z"),
        strict=True,
    ):
        results.append({
            "__resultData": {"caseIds": [case_id], "provenance": provenance},
            "__receiptMetadata": {"receivedAt": received_at},
        })
    PostgresExperimentReferencePort._validate_result_scope(
        results,
        normalized_experiment,
        ["smoke"],
        expected_variant=candidate,
        expected_reproducibility=stable_replay,
        expected_time_range=(NOW, "2026-10-21T09:30:00Z"),
    )
    candidate_provenance = dict(provenance)
    candidate_provenance["reproducibility"] = {
        **stable_replay,
        "promptRevision": "prompt-wrong",
        "policyRevision": "policy-candidate",
    }
    results[0]["__resultData"] = {
        "caseIds": [experiment["cohorts"][0]["caseIds"][0]],
        "provenance": candidate_provenance,
    }
    with pytest.raises(ExperimentError, match="prompt provenance"):
        PostgresExperimentReferencePort._validate_result_scope(
            results,
            normalized_experiment,
            ["smoke"],
            expected_variant=candidate,
            expected_reproducibility=stable_replay,
            expected_time_range=(NOW, "2026-10-21T09:30:00Z"),
        )


@pytest.mark.asyncio
async def test_projection_compensates_when_retention_fences_after_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A late CH insert is removed when the PG lease was tombstoned mid-flight."""

    regression_id = _id()
    projection_id = _id()
    lease_token = _id()
    sql_calls: list[str] = []

    class _Result:
        def __init__(self, row: object | None) -> None:
            self.row = row

        async def fetchone(self) -> object | None:
            return self.row

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: str, _params: object = ()) -> _Result:
            sql_calls.append(statement)
            if "WITH claimable" in statement:
                return _Result(
                    (
                        projection_id,
                        lease_token,
                        {"baselineValues": [0.8], "candidateValues": [0.7]},
                    )
                )
            if "SET cleanup_pending = TRUE" in statement:
                return _Result((projection_id,))
            if "SELECT status, lease_token, cleanup_pending" in statement:
                return _Result(("pending", lease_token, True))
            if "SET status = 'applied'" in statement:
                # Simulate retention fencing the row after the lease check.
                return _Result(None)
            return _Result(None)

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class _Response:
        def raise_for_status(self) -> None:
            return None

    posts: list[str] = []

    class _Client:
        def __init__(self, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str | None, **kwargs: object) -> _Response:
            posts.append(str(kwargs["content"]))
            return _Response()

    monkeypatch.setattr(_experiment_adapter.httpx, "AsyncClient", _Client)
    evidence = PostgresExperimentEvidencePort(
        lambda: _Connection(),
        clickhouse_url="http://clickhouse",
        clickhouse_database="observatory",
        clickhouse_user="default",
        clickhouse_password="secret",
    )

    with pytest.raises(Exception, match="ClickHouse fact projection"):
        await evidence.persist_facts(
            regression_id,
            "1.0.0",
            "quality.score",
            "quality",
            ("smoke",),
            (0.8,),
            (0.7,),
            (_id(),),
            (_id(),),
            "complete",
        )

    assert len(posts) == 2
    assert posts[0].startswith("INSERT INTO evaluation_regression_metric_facts")
    assert "ALTER TABLE evaluation_regression_metric_facts DELETE" in posts[1]


@pytest.mark.asyncio
async def test_projection_compensation_failure_leaves_restart_tombstone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed exact CH cleanup remains durable for the worker after restart."""

    projection_id = _id()
    sql_calls: list[str] = []
    posts: list[str] = []

    lease_token = _id()

    class _Result:
        def __init__(self, row: object | None = None) -> None:
            self.row = row

        async def fetchone(self) -> object | None:
            return self.row

    class _Connection:
        async def __aenter__(self) -> _Connection:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def execute(self, statement: str, _params: object = ()) -> _Result:
            sql_calls.append(statement)
            if "SELECT projection_id, lease_token, payload_json" in statement:
                return _Result(
                    (
                        projection_id,
                        lease_token,
                        {"baselineValues": [0.8], "candidateValues": [0.7]},
                    )
                )
            if "SET cleanup_pending = TRUE" in statement:
                return _Result((projection_id,))
            if "SELECT status, lease_token, cleanup_pending" in statement:
                return _Result(("pending", lease_token, True))
            return _Result()

        async def commit(self) -> None:
            return None

        async def rollback(self) -> None:
            return None

    class _Response:
        def raise_for_status(self) -> None:
            return None

    class _Client:
        calls = 0

        def __init__(self, **_kwargs: object) -> None:
            return None

        async def __aenter__(self) -> _Client:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def post(self, _url: str | None, **kwargs: object) -> _Response:
            self.calls += 1
            type(self).calls += 1
            posts.append(str(kwargs.get("content", "")))
            if type(self).calls > 1:
                raise RuntimeError("clickhouse unavailable")
            return _Response()

    monkeypatch.setattr(_experiment_adapter.httpx, "AsyncClient", _Client)
    evidence = PostgresExperimentEvidencePort(
        lambda: _Connection(),
        clickhouse_url="http://clickhouse",
        clickhouse_database="observatory",
        clickhouse_user="default",
        clickhouse_password="secret",
    )

    with pytest.raises(Exception, match="provider unavailable|compensation"):
        await evidence.persist_facts(
            _id(), "1.0.0", "quality.score", "quality", ("smoke",),
            (0.8,), (0.7,), (_id(),), (_id(),), "complete",
            projection_id=projection_id, lease_token=lease_token,
        )
    assert any("cleanup_pending = TRUE" in statement for statement in sql_calls)
    assert f"projection_id = toUUID('{projection_id}')" in posts[1]
    assert any("SET status = 'failed'" in statement for statement in sql_calls)


@pytest.mark.asyncio
async def test_promotion_is_explicit_immutable_and_idempotent() -> None:
    repository = MemoryExperimentRepository()
    service = EvaluationExperimentService(repository=repository)
    experiment = await service.create_experiment(_experiment())
    candidate = await service.create_baseline(_baseline(experiment))
    baseline_id = str(candidate["baselineId"])
    promotion = {
        "expectedCandidateVersion": "1.0.0",
        "authorizationRecordId": _id(),
        "promotedAt": "2026-10-21T10:00:00Z",
    }
    promoted = await service.promote_baseline(
        baseline_id,
        promotion,
        idempotency_key="00000000-0000-4000-8000-000000000098",
    )
    assert promoted["state"] == "promoted"
    assert promoted["manualPromotionRecordId"] == promotion["authorizationRecordId"]
    assert await service.promote_baseline(baseline_id, promotion) == promoted
    with pytest.raises(ExperimentConflict):
        conflicting = dict(promotion)
        conflicting["authorizationRecordId"] = _id()
        await service.promote_baseline(baseline_id, conflicting)
    with pytest.raises(ExperimentConflict):
        altered = dict(promoted)
        altered["variantId"] = "tampered"
        await repository.put("baselines", altered)


@pytest.mark.asyncio
async def test_concurrent_promotion_allows_one_immutable_winner() -> None:
    service = EvaluationExperimentService()
    experiment = await service.create_experiment(_experiment())
    candidate = await service.create_baseline(_baseline(experiment))
    baseline_id = str(candidate["baselineId"])

    async def promote() -> object:
        try:
            return await service.promote_baseline(
                baseline_id,
                {
                    "expectedCandidateVersion": "1.0.0",
                    "authorizationRecordId": _id(),
                    "promotedAt": NOW,
                },
            )
        except ExperimentConflict:
            return "conflict"

    results = await asyncio.gather(promote(), promote())
    assert sum(result != "conflict" for result in results) == 1
    assert sum(result == "conflict" for result in results) == 1


@pytest.mark.asyncio
async def test_regression_is_deterministic_and_preserves_metric_class_and_attribution() -> None:
    experiment_body = _experiment()
    experiment_body["variants"][1]["policyRevision"] = "policy-candidate-2"
    evidence = ResultEvidence(
        {"base-1": (0.90,), "base-2": (0.90,), "cand-1": (0.60,), "cand-2": (0.60,)}
    )
    service = EvaluationExperimentService(evidence=evidence)
    experiment = await service.create_experiment(experiment_body)
    baseline_body = _baseline(experiment)
    baseline_ids = list(baseline_body["sourceResultIds"])
    baseline = await service.create_baseline(baseline_body)
    baseline = await _promote(service, baseline)
    body = _regression(
        experiment,
        baseline,
        baseline_ids=baseline_ids,
        candidate_ids=[
            "00000000-0000-4000-8000-000000000003",
            "00000000-0000-4000-8000-000000000004",
        ],
    )
    evidence.values = {
        body["baselineResultIds"][0]: (0.90,),
        body["baselineResultIds"][1]: (0.90,),
        body["candidateResultIds"][0]: (0.60,),
        body["candidateResultIds"][1]: (0.60,),
    }
    regression_key = "00000000-0000-4000-8000-000000000099"
    first = await service.create_regression(body, idempotency_key=regression_key)
    repeated = await service.create_regression(body, idempotency_key=regression_key)
    assert repeated == first
    assert first["decision"] == "regressed"
    assert first["evidenceState"] == "complete"
    comparison = first["comparisons"][0]
    assert comparison["metricClass"] == "quality"
    assert comparison["baselineValue"] == 0.9
    assert comparison["candidateValue"] == 0.6
    assert comparison["outcome"] == "regressed"
    attribution = first["attribution"]
    assert attribution["availability"] == "available"
    assert attribution["candidates"]
    assert all(
        candidate["affectedCohortIds"] == ["smoke"] for candidate in attribution["candidates"]
    )
    _assert_no_forbidden_keys(first)
    assert OWNER_ID not in json.dumps(first)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("values", "policy", "expected_state", "expected_decision"),
    [
        ({}, "inconclusive", "missing", "inconclusive"),
        ({"base": (0.9,), "candidate": (0.8,)}, "inconclusive", "insufficient", "inconclusive"),
    ],
)
async def test_regression_missing_and_insufficient_evidence_are_explicit(
    values: dict[str, tuple[float, ...]], policy: str, expected_state: str, expected_decision: str
) -> None:
    evidence = ResultEvidence(values)
    service = EvaluationExperimentService(evidence=evidence)
    experiment = await service.create_experiment(_experiment())
    baseline_body = _baseline(experiment)
    baseline_body["thresholds"] = [_threshold(minimum=2)]
    baseline_ids = list(baseline_body["sourceResultIds"])
    baseline = await service.create_baseline(baseline_body)
    baseline = await _promote(service, baseline)
    body = _regression(
        experiment, baseline, baseline_ids=baseline_ids, candidate_ids=[_id(), _id()]
    )
    if expected_state == "insufficient":
        evidence.values = {
            body["baselineResultIds"][0]: (0.9,),
            body["candidateResultIds"][0]: (0.8,),
        }
    body["thresholds"][0]["missingEvidencePolicy"] = policy
    result = await service.create_regression(body)
    assert result["evidenceState"] == expected_state
    assert result["decision"] == expected_decision
    assert result["attribution"]["availability"] == "unavailable"


@pytest.mark.asyncio
async def test_relative_zero_baseline_is_inconclusive_and_unsupported_query_is_bounded() -> None:
    result_ids = [_id(), _id(), _id(), _id()]
    values = {
        result_ids[0]: (0.0,),
        result_ids[1]: (0.0,),
        result_ids[2]: (0.5,),
        result_ids[3]: (0.5,),
    }
    service = EvaluationExperimentService(evidence=ResultEvidence(values))
    experiment = await service.create_experiment(_experiment())
    baseline_body = _baseline(experiment)
    baseline_body["sourceResultIds"] = result_ids[:2]
    baseline = await service.create_baseline(baseline_body)
    baseline = await _promote(service, baseline)
    body = _regression(
        experiment, baseline, baseline_ids=result_ids[:2], candidate_ids=result_ids[2:]
    )
    body["thresholds"][0]["comparisonMode"] = "relative"
    result = await service.create_regression(body)
    assert result["decision"] == "inconclusive"
    assert result["comparisons"][0]["outcome"] == "inconclusive"
    with pytest.raises(ExperimentError):
        await service.list_resources("regressions", limit=201)
    with pytest.raises(ExperimentError):
        await service.list_resources("baselines", cursor="not-a-cursor")


@pytest.mark.asyncio
async def test_pagination_filters_and_retention_are_bounded() -> None:
    repository = MemoryExperimentRepository()
    service = EvaluationExperimentService(repository=repository)
    for _ in range(3):
        await service.create_experiment(_experiment())
    page = await service.list_resources("experiments", limit=2)
    assert len(page["items"]) == 2
    assert page["nextCursor"] is not None
    second = await service.list_resources("experiments", cursor=page["nextCursor"], limit=2)
    assert len(second["items"]) == 1
    assert await service.delete_for_retention(cutoff="2026-10-22T00:00:00Z") == 3
    assert service.history


@pytest.mark.asyncio
async def test_live_ledger_preserves_same_id_versions_for_exact_retention_cleanup() -> None:
    class Result:
        def __init__(self, row: tuple[object, ...] | None = None) -> None:
            self.row = row

        async def fetchone(self) -> tuple[object, ...] | None:
            return self.row

        async def fetchall(self) -> list[tuple[object, ...]]:
            return []

    class Connection:
        def __init__(self) -> None:
            self.events: list[tuple[object, ...]] = []

        async def execute(self, query: str, params: tuple[object, ...] = ()) -> Result:
            if query.startswith("INSERT"):
                event = (params[0], params[1], params[2], params[3], params[4], params[5], "now")
                self.events.append(event)
                return Result(event)
            return Result()

    connection = Connection()

    class Factory:
        def __call__(self) -> Any:
            class Context:
                async def __aenter__(self) -> Connection:
                    return connection

                async def __aexit__(self, *_args: object) -> None:
                    return None

            return Context()

    store = PostgresEvaluationLiveEventStore(Factory())
    await store.publish(
        "evaluation_regression", "same-id", "comparison_completed", "regressed", "1.0.0"
    )
    await store.publish(
        "evaluation_regression", "same-id", "comparison_completed", "unchanged", "2.0.0"
    )
    assert [(row[2], row[3]) for row in connection.events] == [
        ("same-id", "1.0.0"),
        ("same-id", "2.0.0"),
    ]


def test_aura0096_migrations_are_forward_only_content_free_and_bounded() -> None:
    postgres = (MIGRATION_ROOT / "0012_evaluation_experiments.sql").read_text(encoding="utf-8")
    clickhouse = (CLICKHOUSE_MIGRATION_ROOT / "0009_evaluation_regressions.sql").read_text(
        encoding="utf-8"
    )
    for migration in (postgres, clickhouse):
        upper = migration.upper()
        lower = migration.casefold()
        assert "DROP TABLE" not in upper
        assert "DROP COLUMN" not in upper
        assert all(
            forbidden not in lower
            for forbidden in ("owner_identity", "credential", "bearer", "password")
        )
    assert "CREATE TABLE IF NOT EXISTS observatory_evaluation_experiments" in postgres
    assert "CREATE TABLE IF NOT EXISTS observatory_evaluation_baselines" in postgres
    assert "CREATE TABLE IF NOT EXISTS observatory_evaluation_regressions" in postgres
    assert (
        "CONSTRAINT observatory_regression_capture_ck CHECK (capture_policy = 'metadata_only')"
        in postgres
    )
    assert "CREATE TABLE IF NOT EXISTS evaluation_regression_metric_facts" in clickhouse
    assert "TTL" not in clickhouse.upper()


def _postgres_url() -> str:
    value = os.environ.get("AURA_AURA0096_POSTGRES_URL", "")
    if not value:
        pytest.skip("set AURA_AURA0096_POSTGRES_URL for live PostgreSQL 17 coverage")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


@contextmanager
def _isolated_postgres(database_url: str):
    connection = psycopg.connect(database_url)
    schema = f"aura0096_{uuid4().hex[:12]}"
    quoted = _quote(schema)
    try:
        connection.execute(f"CREATE SCHEMA {quoted}")
        connection.execute(f"SET search_path TO {quoted}")
        for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()
        yield connection
    finally:
        connection.rollback()
        connection.execute("SET search_path TO public")
        connection.execute(f"DROP SCHEMA {quoted} CASCADE")
        connection.commit()
        connection.close()


def test_postgres_0012_fresh_upgrade_rerun_and_privacy() -> None:
    with _isolated_postgres(_postgres_url()) as connection:
        migration = (MIGRATION_ROOT / "0012_evaluation_experiments.sql").read_text(encoding="utf-8")
        connection.execute(migration)
        connection.commit()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
        assert {
            "observatory_evaluation_experiments",
            "observatory_evaluation_baselines",
            "observatory_evaluation_baseline_promotions",
            "observatory_evaluation_regressions",
            "observatory_evaluation_experiment_commands",
        } <= tables
        columns = {
            row[0]
            for row in connection.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = current_schema()"
            ).fetchall()
        }
        assert not columns.intersection(
            {"prompt", "response", "content", "credential", "owner_identity"}
        )


def test_postgres_migration_rejects_non_metadata_regression_capture() -> None:
    with _isolated_postgres(_postgres_url()) as connection:
        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO observatory_evaluation_regressions "
                "(regression_id, regression_version, experiment_id, "
                "experiment_version, baseline_id, baseline_version, variant_id, "
                "cohort_ids_json, baseline_result_ids_json, candidate_result_ids_json, "
                "comparisons_json, decision, evidence_state, evidence_json, "
                "attribution_json, capture_policy, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,'[]','[]','[]','[]',"
                "'unchanged','complete','{}','{}','full_content',CURRENT_TIMESTAMP)",
                (
                    uuid4(),
                    "1.0.0",
                    uuid4(),
                    "1.0.0",
                    uuid4(),
                    "1.0.0",
                    "candidate",
                ),
            )
