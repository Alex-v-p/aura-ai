"""Opt-in PostgreSQL migration and durable-state coverage for AURA-0094."""
# pyright: reportPrivateUsage=false, reportArgumentType=false, reportCallIssue=false

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager, contextmanager
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import psycopg
import pytest
from aura_observatory.adapters.evaluation_orchestration import (
    PostgresEvaluationOrchestrationRepository,
)
from aura_observatory.evaluation_calibration import (
    CalibrationError,
    CalibrationExecutionClaim,
    DisagreementClass,
    HumanReview,
    PostgresCalibrationRepository,
    _qualification_fingerprint,
)
from aura_observatory.evaluation_judges import (
    AuthorizedArtifactReader,
    InMemoryJudgeScoreStore,
    JudgeAdvisoryState,
    JudgeError,
    JudgeOutcome,
    JudgeResponse,
    JudgeResult,
    LocalJudgeService,
    PostgresJudgeExecutionRepository,
)
from aura_observatory.evaluation_orchestration import (
    LocalWorkAdmission,
    canonical_digest,
)
from aura_observatory.ports import EvaluationArtifactMetadata
from test_aura0094_calibration import (
    IDS as CAL_IDS,
)
from test_aura0094_calibration import (
    NOW,
    _complete_qualification,
    _complete_report,
    _label,
)
from test_aura0094_judges import _request

MIGRATION_ROOT = Path(__file__).parents[1] / "migrations/postgresql/versions"


def _database_url() -> str:
    value = os.environ.get("AURA_AURA0094_POSTGRES_URL", "")
    if not value:
        pytest.skip("set AURA_AURA0094_POSTGRES_URL for live AURA-0094 coverage")
    return value


def _quote(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def test_postgres_migrations_are_fresh_apply_ordered_and_contiguous() -> None:
    versions = sorted(
        int(path.name[:4])
        for path in MIGRATION_ROOT.glob("*.sql")
    )
    assert versions == list(range(1, len(versions) + 1))
    assert versions[-1] == 16
    assert (MIGRATION_ROOT / "0010_local_judges_calibration.sql").exists()
    assert [path.name[:4] for path in sorted(MIGRATION_ROOT.glob("*.sql"))][-2] == "0015"


@contextmanager
def _isolated_schema(database_url: str):
    connection = psycopg.connect(database_url)
    schema = f"aura0094_{uuid4().hex[:12]}"
    quoted = _quote(schema)
    primary_error: Exception | None = None
    try:
        connection.execute(f"CREATE SCHEMA {quoted}")
        connection.execute(f"SET search_path TO {quoted}")
        for migration in sorted(MIGRATION_ROOT.glob("*.sql")):
            connection.execute(migration.read_text(encoding="utf-8"))
        connection.commit()
        # Both fresh and upgraded deployments must tolerate an exact rerun.
        migration = (MIGRATION_ROOT / "0010_local_judges_calibration.sql").read_text(
            encoding="utf-8"
        )
        connection.execute(migration)
        connection.commit()
        yield connection
    except Exception as error:
        # Keep the migration/assertion exception as the primary failure.  The
        # finally block below must never replace it with cleanup diagnostics.
        primary_error = error
        raise
    finally:
        cleanup_error: Exception | None = None
        cleanup_stage = "rollback"
        try:
            # A failed migration leaves PostgreSQL's transaction aborted.  A
            # rollback is therefore required before restoring the search path
            # or dropping the project-scoped schema.
            connection.rollback()
            cleanup_stage = "restore search path"
            connection.execute("SET search_path TO public")
            cleanup_stage = "drop isolated schema"
            connection.execute(f"DROP SCHEMA {quoted} CASCADE")
            cleanup_stage = "commit schema cleanup"
            connection.commit()
        except Exception as error:
            cleanup_error = RuntimeError(
                f"AURA-0094 isolated PostgreSQL cleanup failed at {cleanup_stage}"
            )
            cleanup_error.__cause__ = error
        finally:
            try:
                connection.close()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = RuntimeError(
                        "AURA-0094 isolated PostgreSQL cleanup failed at close"
                    )
                    cleanup_error.__cause__ = error
        if cleanup_error is not None and primary_error is None:
            raise cleanup_error


def test_postgres_0010_fresh_upgrade_rerun_constraints_and_round_trips() -> None:
    with _isolated_schema(_database_url()) as connection:
        required_tables = {
            "observatory_local_judge_definitions",
            "observatory_local_judge_labels",
            "observatory_local_judge_reviews",
            "observatory_local_judge_qualifications",
            "observatory_local_judge_calibration_definitions",
            "observatory_local_judge_calibration_results",
            "observatory_local_judge_calibration_claims",
            "observatory_local_judge_execution_claims",
            "observatory_local_judge_audits",
        }
        table_rows = connection.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        ).fetchall()
        assert required_tables <= {row[0] for row in table_rows}

        forbidden_columns = {
            "prompt",
            "response",
            "content",
            "filename",
            "credential",
            "owner_identity",
        }
        columns = connection.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema()"
        ).fetchall()
        assert forbidden_columns.isdisjoint({row[0] for row in columns})

        judge = "10000000-0000-0000-0000-000000000001"
        version = "1.0.0"
        artifact = "10000000-0000-0000-0000-000000000002"
        connection.execute(
            "INSERT INTO observatory_local_judge_definitions "
                "(judge_id, judge_version, evaluator_id, evaluator_version, model_name, "
                "model_version, model_digest, instruction_version, rubric_version, "
                "calibration_version, calibration_id, "
                "instruction_artifact_id, "
            "rubric_artifact_id, label_ids, seed, temperature, timeout_seconds, max_output_bytes, "
            "max_output_tokens, max_input_bytes, context_tokens, max_concurrency, "
            "metric, fingerprint, definition_json) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                judge,
                version,
                "10000000-0000-0000-0000-000000000004",
                version,
                "llama3.2:3b",
                "3.2.0",
                "a" * 64,
                "prompt-1",
                "rubric-1",
                "calibration-1",
                "10000000-0000-0000-0000-000000000005",
                artifact,
                artifact,
                ["pass", "fail", "inconclusive"],
                0,
                0,
                120,
                8192,
                2048,
                1048576,
                4096,
                1,
                "evaluation.local_judge.score",
                "b" * 64,
                '{"modelDigest":"' + "a" * 64 + '"}',
            ),
        )
        row = connection.execute(
            "SELECT evaluator_id, evaluator_version, calibration_id, model_name, model_digest, "
            "instruction_version, rubric_version, label_ids "
            "FROM observatory_local_judge_definitions WHERE judge_id=%s AND judge_version=%s",
            (judge, version),
        ).fetchone()
        normalized_row = tuple(
            str(value) if index in {0, 2} else value for index, value in enumerate(row)
        )
        assert normalized_row == (
            "10000000-0000-0000-0000-000000000004",
            version,
            "10000000-0000-0000-0000-000000000005",
            "llama3.2:3b",
            "a" * 64,
            "prompt-1",
            "rubric-1",
            ["pass", "fail", "inconclusive"],
        )

        with pytest.raises(psycopg.errors.CheckViolation):
            connection.execute(
                "INSERT INTO observatory_local_judge_definitions "
                "(judge_id, judge_version, evaluator_id, evaluator_version, model_name, "
                "model_version, model_digest, "
                "instruction_version, rubric_version, calibration_version, calibration_id, "
                "instruction_artifact_id, rubric_artifact_id, label_ids, seed, temperature, "
                "timeout_seconds, max_output_bytes, max_output_tokens, max_input_bytes, "
                "context_tokens, max_concurrency, metric, fingerprint, definition_json) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                "%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    "10000000-0000-0000-0000-000000000003",
                    version,
                    "10000000-0000-0000-0000-000000000006",
                    version,
                    "bad",
                    version,
                    "not-a-digest",
                    "prompt-1",
                    "rubric-1",
                    "calibration-1",
                    "10000000-0000-0000-0000-000000000007",
                    artifact,
                    artifact,
                    ["pass"],
                    0,
                    0,
                    120,
                    8192,
                    2048,
                    1048576,
                    4096,
                    1,
                    "evaluation.local_judge.score",
                    "c" * 64,
                    "{}",
                ),
            )


@pytest.mark.asyncio
async def test_postgres_qualification_revoke_is_durable_concurrent_and_restart_safe() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as connection:
        schema_row = connection.execute("SELECT current_schema()").fetchone()
        assert schema_row is not None
        schema = str(schema_row[0])
        quoted_schema = _quote(schema)

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async_connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await async_connection.execute(f"SET search_path TO {quoted_schema}")
                yield async_connection
                await async_connection.commit()
            except BaseException:
                await async_connection.rollback()
                raise
            finally:
                await async_connection.close()

        qualification = _complete_qualification()
        connection.execute(
            """INSERT INTO observatory_local_judge_definitions(
                judge_id, judge_version, evaluator_id, evaluator_version,
                model_name, model_version, model_digest, instruction_version,
                rubric_version, calibration_version, calibration_id,
                instruction_artifact_id, rubric_artifact_id, label_ids, seed,
                temperature, timeout_seconds, max_output_bytes, max_output_tokens,
                max_input_bytes, context_tokens, max_concurrency, metric,
                fingerprint, definition_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s)""",
            (
                qualification.judge_id,
                qualification.judge_version,
                qualification.evaluator_id,
                qualification.evaluator_version,
                qualification.model_name,
                qualification.model_version,
                qualification.model_digest,
                qualification.prompt_version,
                qualification.rubric_version,
                qualification.calibration_version,
                qualification.calibration_id,
                qualification.prompt_artifact_reference_id,
                qualification.rubric_artifact_reference_id,
                ["pass", "fail", "inconclusive"],
                qualification.seed,
                0,
                120,
                8192,
                2048,
                1_048_576,
                4_096,
                1,
                "evaluation.local_judge.score",
                "e" * 64,
                "{}",
            ),
        )
        connection.commit()

        repository = PostgresCalibrationRepository(connection_factory)
        qualification_scope = _qualification_fingerprint(qualification)
        connection.execute(
            """INSERT INTO observatory_evaluation_owner_actions(
                authorization_record_id, action, resource_id, scope_fingerprint,
                idempotency_key)
            VALUES (%s,'qualification',%s,%s,%s)""",
            (
                qualification.authorization_record_id,
                qualification.qualification_id,
                qualification_scope,
                qualification.idempotency_key,
            ),
        )
        connection.commit()
        assert await repository.is_authorized(
            qualification.authorization_record_id or "",
            "qualification",
            resource_id=qualification.qualification_id,
            scope_fingerprint=qualification_scope,
        )
        stored = await repository.put_qualification(qualification)
        assert stored == qualification
        scope = {
            "suite_id": qualification.suite_id,
            "suite_version": qualification.suite_version,
            "dataset_id": qualification.dataset_id,
            "dataset_version": qualification.dataset_version,
            "evaluator_id": qualification.evaluator_id,
            "evaluator_version": qualification.evaluator_version,
            "judge_id": qualification.judge_id,
            "judge_version": qualification.judge_version,
            "instruction_version": qualification.prompt_version,
            "rubric_version": qualification.rubric_version,
            "calibration_version": qualification.calibration_version,
            "model_name": qualification.model_name or "",
            "model_version": qualification.model_version or "",
            "model_digest": qualification.model_digest or "",
            "prompt_artifact_id": qualification.prompt_artifact_reference_id or "",
            "prompt_artifact_version": qualification.prompt_artifact_version or "",
            "prompt_artifact_digest": qualification.prompt_artifact_digest or "",
            "rubric_artifact_id": qualification.rubric_artifact_reference_id or "",
            "rubric_artifact_version": qualification.rubric_artifact_version or "",
            "rubric_artifact_digest": qualification.rubric_artifact_digest or "",
            "calibration_result_id": qualification.calibration_result_id or "",
            "seed": qualification.seed,
        }
        assert await repository.active_qualification(**scope) == qualification
        for field, value in scope.items():
            mismatch = dict(scope)
            if field.endswith("_id"):
                assert isinstance(value, str)
                mismatch[field] = value[:-1] + ("1" if value[-1] != "1" else "2")
            elif field.endswith("_digest"):
                mismatch[field] = "0" * 64
            else:
                mismatch[field] = (value + "-mismatch") if isinstance(value, str) else value + 1
            assert await repository.active_qualification(**mismatch) is None
        budget_row = connection.execute(
            "SELECT budget_json FROM observatory_local_judge_qualifications "
            "WHERE qualification_id=%s",
            (qualification.qualification_id,),
        ).fetchone()
        assert budget_row is not None
        stored_budget = budget_row[0]
        if isinstance(stored_budget, str):
            stored_budget = json.loads(stored_budget)
        assert stored_budget == dict(qualification.budget)
        for budget_field, budget_value in qualification.budget.items():
            mismatch_budget = dict(qualification.budget)
            mismatch_budget[budget_field] = budget_value + 1
            mismatch_row = connection.execute(
                "SELECT qualification_id FROM observatory_local_judge_qualifications "
                "WHERE qualification_id=%s AND budget_json=%s::jsonb",
                (qualification.qualification_id, json.dumps(mismatch_budget, sort_keys=True)),
            ).fetchone()
            assert mismatch_row is None
        assert not await repository.is_authorized(
            qualification.authorization_record_id or "",
            "revocation",
            resource_id=qualification.qualification_id,
            scope_fingerprint="a" * 64,
        )
        duplicate_results = await asyncio.gather(
            repository.put_qualification(qualification),
            repository.put_qualification(qualification),
        )
        assert duplicate_results == [qualification, qualification]
        with pytest.raises(CalibrationError, match="idempotency_conflict"):
            await repository.put_qualification(replace(qualification, model_version="3.2.1"))
        with pytest.raises(CalibrationError, match="not_found"):
            await repository.revoke_qualification(
                "10000000-0000-0000-0000-000000000099",
                revoked_at=datetime.now(UTC),
                authorization_record_id=qualification.authorization_record_id or "",
            )

        revoked_at = datetime.now(UTC)
        outcomes = await asyncio.gather(
            repository.revoke_qualification(
                qualification.qualification_id,
                revoked_at=revoked_at,
                authorization_record_id=qualification.authorization_record_id or "",
            ),
            repository.revoke_qualification(
                qualification.qualification_id,
                revoked_at=revoked_at,
                authorization_record_id=qualification.authorization_record_id or "",
            ),
        )
        assert len(outcomes) == 2
        assert all(item is not None and item.state == "revoked" for item in outcomes)
        assert all(item is not None and item.active is False for item in outcomes)

        restarted = PostgresCalibrationRepository(connection_factory)
        restored = await restarted.get_qualification(qualification.qualification_id)
        assert restored is not None
        assert restored.state == "revoked"
        assert restored.revoked_at is not None

        replay = await restarted.put_qualification(qualification)
        assert replay.state == "revoked"
        assert replay.active is False
        audits = connection.execute(
            "SELECT operation, outcome FROM observatory_local_judge_audits "
            "WHERE resource_id=%s ORDER BY occurred_at",
            (qualification.qualification_id,),
        ).fetchall()
        assert audits and all(row[0] == "revoke" and row[1] == "accepted" for row in audits)


@pytest.mark.asyncio
async def test_postgres_execution_and_calibration_repositories_are_restart_safe() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as connection:
        schema_row = connection.execute("SELECT current_schema()").fetchone()
        assert schema_row is not None
        quoted_schema = _quote(str(schema_row[0]))

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async_connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await async_connection.execute(f"SET search_path TO {quoted_schema}")
                yield async_connection
                await async_connection.commit()
            except BaseException:
                await async_connection.rollback()
                raise
            finally:
                await async_connection.close()

        execution = PostgresJudgeExecutionRepository(connection_factory)
        request = _request()
        fingerprint = request.idempotency_fingerprint()
        claims = await asyncio.gather(
            execution.claim(request.idempotency_key, fingerprint),
            execution.claim(request.idempotency_key, fingerprint),
        )
        assert sorted(claims) == [False, True]
        with pytest.raises(JudgeError, match="validation"):
            await execution.claim(request.idempotency_key, "b" * 64)

        result = JudgeResult(
            request.result_id,
            request.request_id,
            fingerprint,
            0.9,
            JudgeOutcome.PASS,
            "pass",
            JudgeAdvisoryState.ADVISORY,
        )
        await execution.complete(request.idempotency_key, fingerprint, result)
        restarted_execution = PostgresJudgeExecutionRepository(connection_factory)
        assert await restarted_execution.get(request.idempotency_key, fingerprint) == result

        failed_key = "00000000-0000-0000-0000-000000000013"
        assert await restarted_execution.claim(failed_key, "c" * 64)
        await restarted_execution.fail(failed_key, "c" * 64, "provider_timeout")
        assert await PostgresJudgeExecutionRepository(connection_factory).claim(
            failed_key, "c" * 64
        )
        stale_key = "00000000-0000-0000-0000-000000000014"
        assert await execution.claim(stale_key, "d" * 64)
        async with connection_factory() as async_connection:
            await async_connection.execute(
                "UPDATE observatory_local_judge_execution_claims "
                "SET claimed_at=CURRENT_TIMESTAMP-INTERVAL '6 minutes' "
                "WHERE idempotency_key=%s",
                (stale_key,),
            )
        assert await PostgresJudgeExecutionRepository(connection_factory).claim(stale_key, "d" * 64)

        qualification = _complete_qualification()
        connection.execute(
            """INSERT INTO observatory_local_judge_definitions(
                judge_id, judge_version, evaluator_id, evaluator_version, model_name,
                model_version, model_digest, instruction_version, rubric_version,
                calibration_version, calibration_id, instruction_artifact_id,
                rubric_artifact_id, label_ids, seed, temperature, timeout_seconds,
                max_output_bytes, max_output_tokens, max_input_bytes, context_tokens,
                max_concurrency, metric, fingerprint, definition_json)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s)""",
            (
                qualification.judge_id,
                qualification.judge_version,
                qualification.evaluator_id,
                qualification.evaluator_version,
                qualification.model_name,
                qualification.model_version,
                qualification.model_digest,
                qualification.prompt_version,
                qualification.rubric_version,
                qualification.calibration_version,
                qualification.calibration_id,
                qualification.prompt_artifact_reference_id,
                qualification.rubric_artifact_reference_id,
                ["pass", "fail", "inconclusive"],
                qualification.seed,
                0,
                120,
                8192,
                2048,
                1_048_576,
                4_096,
                1,
                "evaluation.local_judge.score",
                "e" * 64,
                "{}",
            ),
        )
        connection.execute(
            """INSERT INTO observatory_local_judge_calibration_definitions(
                calibration_id, calibration_version, dataset_id, dataset_version,
                suite_id, suite_version, evaluator_id, evaluator_version,
                judge_id, judge_version, definition_json, fingerprint)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s)""",
            (
                qualification.calibration_id,
                qualification.calibration_version,
                qualification.dataset_id,
                qualification.dataset_version,
                qualification.suite_id,
                qualification.suite_version,
                qualification.evaluator_id,
                qualification.evaluator_version,
                qualification.judge_id,
                qualification.judge_version,
                "{}",
                "f" * 64,
            ),
        )
        connection.commit()

        calibration = PostgresCalibrationRepository(connection_factory)
        claim = CalibrationExecutionClaim(
            qualification.calibration_id or "",
            qualification.calibration_version,
            "1" * 64,
        )
        claimed = await asyncio.gather(
            calibration.claim_calibration(claim), calibration.claim_calibration(claim)
        )
        assert sum(item is not None for item in claimed) == 1
        with pytest.raises(CalibrationError, match="idempotency_conflict"):
            await calibration.claim_calibration(replace(claim, fingerprint="2" * 64))
        report = _complete_report()
        await calibration.put_calibration_result(report)
        assert await PostgresCalibrationRepository(connection_factory).get_calibration_result(
            report.calibration_result_id or ""
        ) == report

        label = replace(
            _label(900),
            reference_label="accepted",
            judge_label="accepted",
            calibration_id=qualification.calibration_id,
            result_id=report.calibration_result_id,
            reference_outcome=JudgeOutcome.PASS,
        )
        await calibration.put_label(label)
        review = HumanReview(
            CAL_IDS["review"], CAL_IDS["case"], CAL_IDS["result"], CAL_IDS["judge"],
            "1.0.0", "prompt-1", "rubric-1", "calibration-1", "pass", 0.9,
            DisagreementClass.NONE, created_at=NOW, idempotency_key=CAL_IDS["review"],
            authorization_record_id=CAL_IDS["result"], calibration_id=CAL_IDS["calibration"],
            score_id=CAL_IDS["result"], outcome=JudgeOutcome.PASS, review_label="accepted",
        )
        await calibration.put_review(review)
        persisted = await PostgresCalibrationRepository(connection_factory).list_labels(
            judge_id=label.judge_id, judge_version=label.judge_version,
            prompt_version=label.prompt_version, rubric_version=label.rubric_version,
            calibration_version=label.calibration_version,
        )
        assert persisted and persisted[0].calibration_id == qualification.calibration_id
        forbidden = {"prompt", "response", "credential", "owner_identity", "endpoint"}
        audit_rows = connection.execute(
            "SELECT resource_type, operation, outcome, failure_code "
            "FROM observatory_local_judge_audits"
        ).fetchall()
        assert all(forbidden.isdisjoint(str(row).casefold().split()) for row in audit_rows)


@pytest.mark.asyncio
async def test_postgres_local_work_claim_provider_score_and_terminal_retry() -> None:
    database_url = _database_url()
    with _isolated_schema(database_url) as connection:
        schema_row = connection.execute("SELECT current_schema()").fetchone()
        assert schema_row is not None
        quoted_schema = _quote(str(schema_row[0]))

        @asynccontextmanager
        async def connection_factory() -> AsyncGenerator[Any]:
            async_connection = await psycopg.AsyncConnection.connect(database_url)
            try:
                await async_connection.execute(f"SET search_path TO {quoted_schema}")
                yield async_connection
                await async_connection.commit()
            except BaseException:
                await async_connection.rollback()
                raise
            finally:
                await async_connection.close()

        request = _request()
        payload = {"request_id": request.request_id, "job_id": request.job_id, "kind": "judge"}
        admission = LocalWorkAdmission(
            request.job_id,
            request.job_id,
            request.result_id,
            "judge",
            request.idempotency_key,
            canonical_digest(payload),
            payload,
            NOW,
        )
        repository = PostgresEvaluationOrchestrationRepository(connection_factory)
        assert await repository.enqueue_local_work(admission)
        claimed = await repository.claim_local_work(
            now=NOW, lease_seconds=120, worker_id="aura0094-worker-one"
        )
        assert claimed is not None

        refs = (
            request.definition.prompt_artifact,
            request.definition.rubric_artifact,
            request.input_artifacts[0],
        )
        bytes_by_id = {
            refs[0].artifact_reference_id: b"evaluate the quoted evidence",
            refs[1].artifact_reference_id: b"score correctness",
            refs[2].artifact_reference_id: b"quoted evidence",
        }

        class _Metadata:
            async def lookup_artifact_metadata(
                self, reference: str
            ) -> EvaluationArtifactMetadata:
                item = next(item for item in refs if item.artifact_reference_id == reference)
                return EvaluationArtifactMetadata(
                    artifact_reference_id=item.artifact_reference_id,
                    dataset_id=item.dataset_id,
                    capture_classification=item.capture_classification,
                    authorization_record_id=item.authorization_record_id,
                    authorization_purpose=item.authorization_purpose,
                    authorization_context=item.authorization_context,
                    retention_mode=item.retention_mode,
                    sha256=item.sha256,
                    status="available",
                    revoked=False,
                    artifact_version=item.artifact_version,
                    media_type=item.media_type,
                    byte_count=item.byte_count,
                    media_classification=item.media_classification,
                    intended_use=item.intended_use,
                )

        class _Objects:
            async def read(self, reference: str, **_: Any) -> bytes:
                return bytes_by_id[reference]

        class _Provider:
            async def verify_model(
                self, _model_name: str, _model_digest: str, *, model_version: str
            ) -> None:
                assert model_version == request.definition.model_version

            async def generate(self, **_: Any) -> JudgeResponse:
                return JudgeResponse(0.9, JudgeOutcome.PASS, "pass")

        execution = PostgresJudgeExecutionRepository(connection_factory)
        judge = LocalJudgeService(
            _Provider(),
            AuthorizedArtifactReader(_Metadata(), _Objects()),
            execution_repository=execution,
            score_store=InMemoryJudgeScoreStore(),
        )
        result = await judge.evaluate(request)
        assert await repository.complete_local_work(claimed, result=result, at=NOW)
        fresh = PostgresEvaluationOrchestrationRepository(connection_factory)
        async with connection_factory() as async_connection:
            row = await async_connection.execute(
                "SELECT status, result_json::text FROM observatory_evaluation_local_work "
                "WHERE work_id=%s",
                (request.job_id,),
            )
            stored = await row.fetchone()
        assert stored is not None and stored[0] == "completed"
        assert "prompt" not in str(stored[1]).casefold()
        assert await fresh.claim_local_work(now=NOW, lease_seconds=120, worker_id="fresh") is None

        retry_work_id = "00000000-0000-0000-0000-000000000015"
        retry_key = "00000000-0000-0000-0000-000000000016"
        retry_payload = {
            "request_id": request.request_id,
            "job_id": request.job_id,
            "kind": "judge",
        }
        retry_admission = LocalWorkAdmission(
            retry_work_id,
            request.job_id,
            request.result_id,
            "judge",
            retry_key,
            canonical_digest(retry_payload),
            retry_payload,
            NOW,
        )
        assert await fresh.enqueue_local_work(retry_admission)
        failed_claim = await fresh.claim_local_work(
            now=NOW, lease_seconds=120, worker_id="worker-before-restart"
        )
        assert failed_claim is not None
        assert await fresh.fail_local_work(
            failed_claim, failure_code="provider_timeout", at=NOW
        )
        async with connection_factory() as async_connection:
            await async_connection.execute(
                "UPDATE observatory_evaluation_local_work "
                "SET status='queued', available_at=%s WHERE work_id=%s",
                (NOW, retry_work_id),
            )
        recovered_claim = await PostgresEvaluationOrchestrationRepository(
            connection_factory
        ).claim_local_work(now=NOW, lease_seconds=120, worker_id="worker-after-restart")
        assert recovered_claim is not None
        duplicate_result = await judge.evaluate(request)
        assert await repository.complete_local_work(
            recovered_claim, result=duplicate_result, at=NOW
        )
