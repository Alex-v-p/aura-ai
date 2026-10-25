"""Opt-in AURA-0095 ClickHouse provenance compatibility checks."""

# pyright: reportPrivateUsage=false, reportUnknownVariableType=false, reportUnknownArgumentType=false, reportUnknownMemberType=false, reportGeneralTypeIssues=false, reportArgumentType=false, reportIndexIssue=false

from __future__ import annotations

import json
from dataclasses import replace
from uuid import uuid4

import pytest
from aura_observatory.adapters.evaluation_query import PostgresEvaluationQueryBackend
from aura_observatory.adapters.evaluation_storage import ClickHouseEvaluationScoreStore
from aura_observatory.evaluation_query import EvaluationUnavailable
from test_aura0094_clickhouse_integration import (
    _apply_all_migrations,
    _endpoint_config,
    _isolated_database,
    _post,
    _score,
)


def _authoritative_score():
    """Build a score whose persisted provenance is complete and self-consistent."""

    score = replace(_score(), case_id=str(uuid4()))
    return replace(
        score,
        execution_provenance={
            "dataset": {
                "resourceId": score.dataset_id,
                "resourceVersion": "1.0.0",
            },
            "suite": {
                "resourceId": score.suite_id,
                "resourceVersion": "1.0.0",
            },
            "evaluators": [
                {
                    "evaluatorId": score.evaluator_id,
                    "evaluatorVersion": score.evaluator_version,
                    "kind": score.evaluator_kind,
                }
            ],
            "components": [
                {
                    "componentId": score.component_id,
                    "componentVersion": score.component_version,
                    "codeRevision": score.code_revision,
                    "imageDigest": score.image_digest,
                    "configurationRevision": score.configuration_revision,
                }
            ],
            "model": {
                "modelId": "llama3.2:3b",
                "modelVersion": "3.2.0",
                "modelDigest": score.model_digest,
            },
            "deploymentRevision": "deploy-20261021",
            "reproducibility": {
                "seed": score.seed,
                "clockMode": "fixed",
                "timeZone": "UTC",
                "locale": "en-US",
                "fixtureRevision": "fixture-20261021",
                "promptRevision": score.prompt_version,
                "policyRevision": "policy-20261021",
            },
        },
    )


@pytest.mark.asyncio
async def test_clickhouse_0008_old_rows_are_unavailable_and_new_provenance_round_trips() -> None:
    """Legacy rows must not be projected as complete score DTOs."""

    endpoint, user, password = _endpoint_config()
    with _isolated_database(endpoint, user, password) as settings:
        _apply_all_migrations(settings)
        old_score_id = str(uuid4())
        old_job_id, old_result_id, old_dataset_id, old_suite_id, old_evaluator_id = [
            str(uuid4()) for _ in range(5)
        ]
        _post(
            endpoint,
            user,
            password,
            settings.clickhouse_database,
            "INSERT INTO evaluation_scores(score_id, job_id, result_id, dataset_id, suite_id, "
            "evaluator_id, evaluator_version, evaluator_kind, component_id, component_version, "
            "capture_classification, evidence_status, score_value, outcome, observed_at) "
            + f"VALUES (toUUID('{old_score_id}'), toUUID('{old_job_id}'), "
            + f"toUUID('{old_result_id}'), toUUID('{old_dataset_id}'), "
            + f"toUUID('{old_suite_id}'), toUUID('{old_evaluator_id}'), "
            + "'1.0.0', 'deterministic', 'aura.core', '1.0.0', "
            + "'metadata_only', 'observed', 0.5, 'pass', '2026-10-20 00:00:00')",
        )
        backend = PostgresEvaluationQueryBackend(
            lambda: None,
            clickhouse_url=settings.clickhouse_url,
            clickhouse_database=settings.clickhouse_database,
            clickhouse_user=settings.clickhouse_user,
            clickhouse_password=settings.clickhouse_password,
        )
        with pytest.raises(EvaluationUnavailable):
            await backend.collection("scores", cursor=None, limit=10, filters=None)

        score = _authoritative_score()
        await ClickHouseEvaluationScoreStore(
            settings.clickhouse_url,
            database=settings.clickhouse_database,
            user=settings.clickhouse_user,
            password=settings.clickhouse_password,
        ).insert_score(score)
        projected = await backend.collection(
            "scores", cursor=None, limit=1, filters={"resultId": score.result_id}
        )
        item = projected["items"][0]
        assert item["scoreId"] == score.score_id
        assert item["component"] == {
            "componentId": score.component_id,
            "componentVersion": score.component_version,
            "codeRevision": score.code_revision,
            "imageDigest": score.image_digest,
            "configurationRevision": score.configuration_revision,
        }
        assert item["provenance"] == {
            **score.execution_provenance,
            "dataset": {"id": score.dataset_id, "version": "1.0.0"},
            "suite": {"id": score.suite_id, "version": "1.0.0"},
        }
        assert all(
            forbidden not in json.dumps(item).casefold()
            for forbidden in ("response", "credential", "endpoint", "owner")
        )
