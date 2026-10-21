"""Opt-in live ClickHouse coverage for AURA-0094 migration 0007.

The test uses a generated ClickHouse database and the repository's migration
splitter.  It is skipped unless a project-scoped ClickHouse URL is supplied;
the URL and credentials are never printed or persisted.
"""
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportCallIssue=false, reportIndexIssue=false

from __future__ import annotations

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest
from aura_observatory.adapters.evaluation_storage import ClickHouseEvaluationScoreStore
from aura_observatory.entrypoints import cli
from aura_observatory.evaluation_storage import ArtifactStorageError, EvaluationScore
from aura_observatory.settings import Settings
from test_aura0094_contract import _schema, validate_schema

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/clickhouse/versions"
FORBIDDEN_SCORE_COLUMNS = {
    "response",
    "comment",
    "content",
    "payload",
    "endpoint",
    "provider_endpoint",
}


def _endpoint_config() -> tuple[str, str, str]:
    endpoint = os.environ.get("AURA_AURA0094_CLICKHOUSE_URL", "")
    if not endpoint:
        pytest.skip("set AURA_AURA0094_CLICKHOUSE_URL for live AURA-0094 coverage")
    return (
        endpoint,
        os.environ.get("AURA_AURA0094_CLICKHOUSE_USER", "default"),
        os.environ.get("AURA_AURA0094_CLICKHOUSE_PASSWORD", ""),
    )


def _post(
    endpoint: str,
    user: str,
    password: str,
    database: str,
    statement: str,
) -> httpx.Response:
    response = httpx.post(
        endpoint,
        params={"database": database},
        content=statement,
        timeout=20.0,
        auth=(user, password),
    )
    response.raise_for_status()
    return response


def _query(settings: Settings, statement: str, *, output_format: str = "TabSeparatedRaw") -> str:
    response = _post(
        settings.clickhouse_url,
        settings.clickhouse_user,
        settings.clickhouse_password,
        settings.clickhouse_database,
        statement.rstrip() + " FORMAT " + output_format,
    )
    return response.text


def _apply_all_migrations(settings: Settings) -> None:
    # Match the production runner's ledger protocol: each version is visible
    # only after all of its statements have succeeded.
    cli._clickhouse_post(  # pyright: ignore[reportPrivateUsage]
        settings,
        "CREATE TABLE IF NOT EXISTS observatory_migration_ledger ("
        "version String, applied_at DateTime64(3, 'UTC')) "
        "ENGINE = ReplacingMergeTree(applied_at) ORDER BY version",
    )
    for path in sorted(MIGRATION_ROOT.glob("*.sql")):
        version = path.stem
        if cli._clickhouse_applied(  # pyright: ignore[reportPrivateUsage]
            settings, version
        ):
            continue
        content = path.read_text(encoding="utf-8")
        content += (
            "\nINSERT INTO observatory_migration_ledger(version, applied_at) "
            f"SELECT '{version}', now64(3) WHERE NOT EXISTS (SELECT 1 FROM "
            "observatory_migration_ledger WHERE version = '" + version + "');\n"
        )
        cli._clickhouse_post(  # pyright: ignore[reportPrivateUsage]
            settings, content
        )


@contextmanager
def _isolated_database(endpoint: str, user: str, password: str) -> Generator[Settings]:
    database = f"aura0094_{uuid4().hex[:12]}"
    _post(endpoint, user, password, "default", f"CREATE DATABASE {database}")
    settings = Settings(
        clickhouse_url=endpoint,
        clickhouse_database=database,
        clickhouse_user=user,
        clickhouse_password=password,
    )
    primary_error: Exception | None = None
    try:
        yield settings
    except Exception as error:
        primary_error = error
        raise
    finally:
        cleanup_error: Exception | None = None
        try:
            _post(endpoint, user, password, "default", f"DROP DATABASE IF EXISTS {database}")
        except Exception as error:
            cleanup_error = RuntimeError("AURA-0094 ClickHouse cleanup failed")
            cleanup_error.__cause__ = error
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


def _score() -> EvaluationScore:
    return EvaluationScore(
        score_id=str(uuid4()),
        job_id=str(uuid4()),
        result_id=str(uuid4()),
        dataset_id=str(uuid4()),
        suite_id=str(uuid4()),
        evaluator_id=str(uuid4()),
        evaluator_version="1.0.0",
        evaluator_kind="local_judge",
        component_id="aura.core.evaluation",
        component_version="1.0.0",
        capture_classification="metadata_only",
        evidence_status="observed",
        value=0.75,
        outcome="pass",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        judge_definition_id=str(uuid4()),
        judge_definition_version="1.0.0",
        model_name="llama3.2:3b",
        model_version="3.2.0",
        model_digest="a" * 64,
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
        seed=7,
        budget_timeout_ms=120_000,
        budget_max_input_bytes=1_048_576,
        budget_max_output_bytes=8_192,
        budget_max_output_tokens=2_048,
        budget_max_context_tokens=4_096,
        budget_max_concurrency=1,
        score_disposition="calibrated_enabled",
        qualification_id=str(uuid4()),
        qualification_version="1.0.0",
        uncertainty=0.25,
        disagreement="within_tolerance",
        prompt_artifact_id=str(uuid4()),
        prompt_artifact_version="1.0.0",
        prompt_artifact_digest="b" * 64,
        rubric_artifact_id=str(uuid4()),
        rubric_artifact_version="1.0.0",
        rubric_artifact_digest="c" * 64,
        calibration_id=str(uuid4()),
    )


def test_local_judge_score_metadata_round_trips_through_closed_contract() -> None:
    score = _score()
    contract = score.as_local_judge_contract()
    schema = _schema()
    validate_schema(contract, schema["$defs"]["LocalJudgeScoreMetadata"], schema, "$instance")
    assert contract["calibration"] == {
        "calibrationId": score.calibration_id,
        "calibrationVersion": score.calibration_version,
    }
    assert contract["prompt"]["digest"] == score.prompt_artifact_digest
    assert contract["rubric"]["digest"] == score.rubric_artifact_digest


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("evaluator_kind", "cloud_judge"),
        ("seed", -1),
        ("budget_timeout_ms", 99),
        ("score_disposition", "automatic_gate"),
        ("uncertainty", float("nan")),
    ],
)
def test_local_judge_score_validation_rejects_invalid_enum_null_and_bounds(
    field_name: str, value: object
) -> None:
    with pytest.raises(ArtifactStorageError, match="score_invalid"):
        replace(_score(), **{field_name: value}).validate()


@pytest.mark.asyncio
async def test_clickhouse_0007_fresh_rerun_score_round_trip_and_privacy() -> None:
    endpoint, user, password = _endpoint_config()
    with _isolated_database(endpoint, user, password) as settings:
        _apply_all_migrations(settings)
        applied = _query(
            settings,
            "SELECT version FROM observatory_migration_ledger ORDER BY version",
        ).splitlines()
        assert {item.split("_")[0] for item in applied} >= {f"000{i}" for i in range(1, 8)}

        # A second runner pass must consult the ledger and leave the schema
        # usable.  This catches partial migration markers and 0007 drift.
        _apply_all_migrations(settings)
        columns = set(
            _query(
                settings,
                "SELECT name FROM system.columns "
                f"WHERE database = '{settings.clickhouse_database}' "
                "AND table = 'evaluation_scores' ORDER BY name",
            ).splitlines()
        )
        expected = {
            "judge_definition_id",
            "judge_definition_version",
            "model_name",
            "model_version",
            "model_digest",
            "prompt_version",
            "rubric_version",
            "calibration_version",
            "judge_seed",
            "judge_budget_timeout_ms",
            "score_disposition",
            "qualification_id",
            "uncertainty",
            "disagreement",
        }
        assert expected <= columns
        assert FORBIDDEN_SCORE_COLUMNS.isdisjoint(columns)
        assert "prompt" not in columns
        assert "rubric" not in columns

        score = _score()
        store = ClickHouseEvaluationScoreStore(
            settings.clickhouse_url,
            database=settings.clickhouse_database,
            user=settings.clickhouse_user,
            password=settings.clickhouse_password,
        )
        await store.insert_score(score)
        rows = _query(
            settings,
            "SELECT toString(score_id), toString(judge_definition_id), "
            "judge_definition_version, model_name, model_digest, prompt_version, "
            "rubric_version, calibration_version, judge_seed, "
            "judge_budget_timeout_ms, score_disposition, toString(qualification_id), "
            "qualification_version, uncertainty, disagreement "
            "FROM evaluation_scores FINAL "
            f"WHERE score_id = '{score.score_id}'",
            output_format="JSONEachRow",
        )
        row: dict[str, Any] = json.loads(rows.splitlines()[0])
        assert row == {
            "toString(score_id)": score.score_id,
            "toString(judge_definition_id)": score.judge_definition_id,
            "judge_definition_version": score.judge_definition_version,
            "model_name": score.model_name,
            "model_digest": score.model_digest,
            "prompt_version": score.prompt_version,
            "rubric_version": score.rubric_version,
            "calibration_version": score.calibration_version,
            "judge_seed": score.seed,
            "judge_budget_timeout_ms": score.budget_timeout_ms,
            "score_disposition": score.score_disposition,
            "toString(qualification_id)": score.qualification_id,
            "qualification_version": score.qualification_version,
            "uncertainty": score.uncertainty,
            "disagreement": score.disagreement,
        }
        assert FORBIDDEN_SCORE_COLUMNS.isdisjoint(row)
