"""PostgreSQL and ClickHouse adapters for AURA-0090 storage state.

Every statement names Observatory-owned tables explicitly.  No adapter accepts
provider keys, paths, raw bytes, arbitrary attributes, or owner identity.
"""

# Provider SQL is kept visually aligned with the service-owned schema.
# ruff: noqa: E501

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import TYPE_CHECKING, Any, cast
from uuid import UUID, uuid4

import httpx

from aura_observatory.evaluation_storage import (
    ArtifactRecord,
    ArtifactStorageError,
    ArtifactUpload,
    DeletionCommand,
    DeletionKind,
    DeletionSelector,
    EvaluationScore,
)
from aura_observatory.ports import EvaluationArtifactMetadata

_LOGGER = logging.getLogger(__name__)

if TYPE_CHECKING:
    from aura_observatory.metrics import ObservatoryMetrics


class PostgresEvaluationArtifactRepository:
    """Transactional metadata repository for artifact lifecycle and deletion."""

    def __init__(self, connection_factory: Callable[[], Any]) -> None:
        self._connection_factory = connection_factory

    async def lookup_artifact_metadata(
        self, artifact_reference_id: str
    ) -> EvaluationArtifactMetadata | None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT artifact.artifact_reference_id, artifact.dataset_id,
                artifact.capture_classification, artifact.authorization_record_id,
                authz.authorization_purpose, authz.authorization_context,
                authz.retention_mode, artifact.sha256, artifact.status,
                artifact.media_type, artifact.media_classification,
                artifact.byte_count,
                artifact.intended_use,
                artifact.artifact_version,
                authz.authorized_at, (authz.revoked_at IS NOT NULL) AS revoked
                FROM observatory_evaluation_artifacts AS artifact
                JOIN observatory_evaluation_authorizations AS authz
                  ON authz.authorization_record_id = artifact.authorization_record_id
                WHERE artifact.artifact_reference_id = %s LIMIT 1""",
                (artifact_reference_id,),
            )
            row = await result.fetchone()
        if row is None:
            return None
        return EvaluationArtifactMetadata(
            artifact_reference_id=str(row[0]),
            dataset_id=str(row[1]),
            capture_classification=str(row[2]),
            authorization_record_id=str(row[3]),
            authorization_purpose=str(row[4]),
            authorization_context=str(row[5]),
            retention_mode=str(row[6]),
            sha256=str(row[7]),
            status=str(row[8]),
            media_type=str(row[9]),
            media_classification=str(row[10]),
            byte_count=int(row[11]),
            intended_use=str(row[12]),
            artifact_version=str(row[13]),
            authorized_at=cast(datetime, row[14]).astimezone(UTC),
            revoked=bool(row[15]),
        )

    async def lookup_authorization_metadata(
        self, authorization_record_id: str, dataset_id: str
    ) -> EvaluationArtifactMetadata | None:
        """Resolve authorization state without exposing any artifact bytes."""

        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT authorization_record_id, dataset_id,
                capture_classification, authorization_purpose,
                authorization_context, retention_mode, authorized_at,
                (revoked_at IS NOT NULL) AS revoked
                FROM observatory_evaluation_authorizations
                WHERE authorization_record_id = %s AND dataset_id = %s
                LIMIT 1""",
                (authorization_record_id, dataset_id),
            )
            row = await result.fetchone()
        if row is None:
            return None
        return EvaluationArtifactMetadata(
            artifact_reference_id="",
            dataset_id=str(row[1]),
            capture_classification=str(row[2]),
            authorization_record_id=str(row[0]),
            authorization_purpose=str(row[3]),
            authorization_context=str(row[4]),
            retention_mode=str(row[5]),
            sha256="",
            status="authorized",
            authorized_at=cast(datetime, row[6]).astimezone(UTC),
            revoked=bool(row[7]),
        )

    async def reserve_artifact(self, request: ArtifactUpload) -> ArtifactRecord | None | bool:
        intended_use = request.intended_use or "input"
        async with self._connection_factory() as connection:
            existing_result = await connection.execute(
                """SELECT artifact.artifact_id, artifact.artifact_reference_id,
                artifact.request_id, artifact.result_id, artifact.idempotency_key,
                artifact.authorization_record_id, artifact.capture_classification,
                artifact.media_classification, artifact.media_type, artifact.byte_count,
                artifact.sha256, artifact.job_id, artifact.dataset_id, artifact.suite_id,
                artifact.created_at, artifact.status, artifact.declared_max_bytes,
                artifact.intended_use, artifact.artifact_version
                FROM observatory_evaluation_artifacts AS artifact
                JOIN observatory_evaluation_authorizations AS authz
                  ON authz.authorization_record_id = artifact.authorization_record_id
                 AND authz.revoked_at IS NULL
                 AND authz.request_id = artifact.request_id
                 AND authz.dataset_id = artifact.dataset_id
                 AND authz.authorization_purpose = %s
                 AND authz.authorization_context = %s
                 AND authz.retention_mode = 'manual_only'
                 AND authz.capture_classification = %s
                WHERE artifact.idempotency_key = %s
                  AND artifact.authorization_record_id = %s
                  AND artifact.request_id = %s
                  AND artifact.dataset_id = %s
                LIMIT 1""",
                (
                    request.authorization_purpose,
                    request.authorization_context,
                    request.capture_classification,
                    request.idempotency_key,
                    request.authorization_record_id,
                    request.request_id,
                    request.dataset_id,
                ),
            )
            existing = await existing_result.fetchone()
            if existing is not None:
                columns = (
                    "artifact_id",
                    "artifact_reference_id",
                    "request_id",
                    "result_id",
                    "idempotency_key",
                    "authorization_record_id",
                    "capture_classification",
                    "media_classification",
                    "media_type",
                    "byte_count",
                    "sha256",
                    "job_id",
                    "dataset_id",
                    "suite_id",
                    "created_at",
                    "status",
                    "declared_max_bytes",
                    "intended_use",
                    "artifact_version",
                )
                if any(
                    str(
                        _row_value_or(
                            existing,
                            index,
                            columns[index],
                            "input" if index == 17 else "1.0.0" if index == 18 else None,
                        )
                    )
                    != str(value)
                    for index, value in (
                        (0, request.artifact_id),
                        (1, request.artifact_reference_id),
                        (2, request.request_id),
                        (3, request.result_id),
                        (5, request.authorization_record_id),
                        (6, request.capture_classification),
                        (7, request.media_classification),
                        (8, request.media_type),
                        (10, request.expected_sha256),
                        (11, request.job_id),
                        (12, request.dataset_id),
                        (13, request.suite_id),
                        (16, request.max_bytes),
                        (
                            17,
                            intended_use,
                        ),
                        (
                            18,
                            request.artifact_version,
                        ),
                    )
                ):
                    raise ArtifactStorageError("idempotency_conflict")
                status = str(_row_value(existing, 15, "status"))
                if status == "available":
                    return _artifact_from_row(existing)
                if status == "deleting":
                    raise ArtifactStorageError("artifact_deleting")
                await connection.execute(
                    "UPDATE observatory_evaluation_artifacts SET status = 'pending', "
                    "failure_code = NULL, updated_at = CURRENT_TIMESTAMP "
                    "WHERE artifact_reference_id = %s",
                    (request.artifact_reference_id,),
                )
                return None
            authorized = await connection.execute(
                """SELECT capture_classification FROM observatory_evaluation_authorizations
                WHERE authorization_record_id = %s
                  AND request_id = %s AND dataset_id = %s
                  AND authorization_purpose = %s
                  AND authorization_context = %s
                  AND retention_mode = 'manual_only'
                  AND revoked_at IS NULL LIMIT 1""",
                (
                    request.authorization_record_id,
                    request.request_id,
                    request.dataset_id,
                    request.authorization_purpose,
                    request.authorization_context,
                ),
            )
            authorization = await authorized.fetchone()
            if authorization is None or str(authorization[0]) not in {
                "redacted",
                "sampled_content",
                "full_content",
            }:
                return False
            if str(authorization[0]) != request.capture_classification:
                raise ArtifactStorageError("authorization_mismatch")
            await connection.execute(
                """INSERT INTO observatory_evaluation_artifacts(
                artifact_id, artifact_reference_id, request_id, result_id,
                idempotency_key, authorization_record_id, capture_classification,
                media_classification, media_type, byte_count, sha256, job_id,
                dataset_id, suite_id, status, declared_max_bytes, created_at, updated_at,
                intended_use, artifact_version)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, 0, %s, %s, %s, %s,
                'pending', %s, %s, CURRENT_TIMESTAMP, %s, %s)""",
                (
                    request.artifact_id,
                    request.artifact_reference_id,
                    request.request_id,
                    request.result_id,
                    request.idempotency_key,
                    request.authorization_record_id,
                    request.capture_classification,
                    request.media_classification,
                    request.media_type,
                    request.expected_sha256,
                    request.job_id,
                    request.dataset_id,
                    request.suite_id,
                    request.max_bytes,
                    request.created_at.astimezone(UTC),
                    request.intended_use,
                    request.artifact_version,
                ),
            )
            return None

    async def publish_artifact(self, artifact: ArtifactRecord) -> None:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_artifacts AS artifact
                SET byte_count = %s, sha256 = %s, status = 'available',
                    updated_at = CURRENT_TIMESTAMP, failure_code = NULL
                FROM observatory_evaluation_authorizations AS authz
                WHERE artifact.artifact_reference_id = %s
                  AND artifact.status = 'pending'
                  AND authz.authorization_record_id = artifact.authorization_record_id
                  AND authz.revoked_at IS NULL
                  AND authz.request_id = artifact.request_id
                  AND authz.dataset_id = artifact.dataset_id
                  AND authz.authorization_purpose = %s
                  AND authz.authorization_context = %s
                  AND authz.retention_mode = 'manual_only'
                  AND authz.capture_classification = artifact.capture_classification
                  AND artifact.intended_use = %s
                  AND artifact.artifact_version = %s
                  AND artifact.sha256 = %s""",
                (
                    artifact.byte_count,
                    artifact.sha256,
                    artifact.artifact_reference_id,
                    artifact.authorization_purpose,
                    artifact.authorization_context,
                    artifact.intended_use,
                    artifact.artifact_version,
                    artifact.sha256,
                ),
            )
            if getattr(result, "rowcount", 1) == 0:
                raise ArtifactStorageError("artifact_state_conflict")

    async def mark_artifact_failed(self, artifact_reference_id: str, failure_code: str) -> None:
        if failure_code not in {
            "artifact_limit_exceeded",
            "integrity_mismatch",
            "integrity_invalid",
            "malformed_stream",
            "storage_failure",
            "storage_authentication",
            "storage_integrity",
            "storage_timeout",
            "database_failure",
        }:
            failure_code = "storage_failure"
        async with self._connection_factory() as connection:
            await connection.execute(
                """UPDATE observatory_evaluation_artifacts SET status = 'failed',
                failure_code = %s, updated_at = CURRENT_TIMESTAMP
                WHERE artifact_reference_id = %s AND status <> 'available'""",
                (failure_code, artifact_reference_id),
            )

    async def create_deletion_command(
        self, command: DeletionCommand, *, score_ids: Sequence[str] = ()
    ) -> DeletionCommand:
        command.validate()
        normalized_score_ids = tuple(
            dict.fromkeys(_canonical_score_id(value) for value in score_ids)
        )
        if len(normalized_score_ids) > 10_000:
            raise ArtifactStorageError("target_limit_exceeded")
        selector = command.selector
        value = (
            selector.value.astimezone(UTC)
            if isinstance(selector.value, datetime)
            else selector.value
        )
        async with self._connection_factory() as connection:
            existing_result = await connection.execute(
                "SELECT command_id, selector_kind, selector_value, confirmation, requested_at "
                "FROM observatory_evaluation_deletion_commands "
                "WHERE idempotency_key = %s LIMIT 1",
                (command.idempotency_key,),
            )
            existing = await existing_result.fetchone()
            if existing is not None:
                if str(_row_value(existing, 1, "selector_kind")) != selector.kind.value or str(
                    _row_value(existing, 2, "selector_value")
                ) != str(value):
                    raise ArtifactStorageError("idempotency_conflict")
                persisted_requested_at = _row_value(existing, 4, "requested_at")
                if isinstance(persisted_requested_at, str):
                    persisted_requested_at = datetime.fromisoformat(
                        persisted_requested_at.replace("Z", "+00:00")
                    )
                if not isinstance(persisted_requested_at, datetime) or (
                    persisted_requested_at.utcoffset() != timedelta(0)
                ):
                    raise ArtifactStorageError("database_failure")
                return DeletionCommand(
                    command_id=str(_row_value(existing, 0, "command_id")),
                    idempotency_key=command.idempotency_key,
                    selector=DeletionSelector(selector.kind, value),
                    confirmation=str(_row_value(existing, 3, "confirmation")),
                    requested_at=persisted_requested_at.astimezone(UTC),
                )
            await connection.execute(
                """INSERT INTO observatory_evaluation_deletion_commands(
                command_id, idempotency_key, selector_kind, selector_value,
                confirmation, status, requested_at)
                VALUES (%s, %s, %s, %s, %s, 'queued', %s)""",
                (
                    command.command_id,
                    command.idempotency_key,
                    selector.kind.value,
                    value,
                    command.confirmation,
                    command.requested_at.astimezone(UTC),
                ),
            )
            where, params = _selector_sql(selector)
            await connection.execute(
                f"""INSERT INTO observatory_evaluation_deletion_targets(
                command_id, artifact_reference_id, artifact_id, job_id, dataset_id, suite_id)
                SELECT %s, artifact_reference_id, artifact_id, job_id, dataset_id, suite_id
                FROM observatory_evaluation_artifacts
                WHERE status = 'available' AND {where}
                ON CONFLICT DO NOTHING""",
                (command.command_id, *params),
            )
            for score_id in normalized_score_ids:
                await connection.execute(
                    """INSERT INTO observatory_evaluation_deletion_score_targets(
                    command_id, score_id) VALUES (%s, %s) ON CONFLICT DO NOTHING""",
                    (command.command_id, score_id),
                )
        return command

    async def claim_deletion_command(self, command_id: str) -> str | None:
        lease_token = str(uuid4())
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_deletion_commands
                SET status = 'running', started_at = COALESCE(started_at, CURRENT_TIMESTAMP),
                updated_at = CURRENT_TIMESTAMP, lease_token = %s,
                lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '60 seconds',
                attempt_count = attempt_count + 1
                WHERE command_id = %s AND (status = 'queued' OR
                (status = 'running' AND lease_expires_at < CURRENT_TIMESTAMP))
                RETURNING command_id""",
                (lease_token, command_id),
            )
            row = await result.fetchone()
        return lease_token if row else None

    async def renew_deletion_lease(self, command_id: str, lease_token: str) -> bool:
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """UPDATE observatory_evaluation_deletion_commands
                SET lease_expires_at = CURRENT_TIMESTAMP + INTERVAL '60 seconds',
                    updated_at = CURRENT_TIMESTAMP
                WHERE command_id = %s AND status = 'running'
                  AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                RETURNING command_id""",
                (command_id, lease_token),
            )
            return bool(await result.fetchone())

    async def mark_deletion_phase(
        self, command_id: str, phase: str, *, lease_token: str | None = None
    ) -> bool:
        if phase not in {"scores", "objects"}:
            raise ValueError("invalid deletion phase")
        async with self._connection_factory() as connection:
            if lease_token is None:
                result = await connection.execute(
                    "UPDATE observatory_evaluation_deletion_commands "
                    "SET deletion_phase = %s, updated_at = CURRENT_TIMESTAMP "
                    "WHERE command_id = %s AND status = 'running'",
                    (phase, command_id),
                )
            else:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_commands
                    SET deletion_phase = %s, updated_at = CURRENT_TIMESTAMP
                    WHERE command_id = %s AND status = 'running'
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP""",
                    (phase, command_id, lease_token),
                )
            return _updated(result)

    async def pending_deletion_commands(self, *, limit: int = 16) -> list[DeletionCommand]:
        from aura_observatory.evaluation_storage import DeletionKind, DeletionSelector

        bounded = max(1, min(16, int(limit)))
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT command_id, idempotency_key, selector_kind, selector_value,
                confirmation, requested_at FROM observatory_evaluation_deletion_commands
                WHERE status = 'queued' OR (status = 'running' AND
                lease_expires_at < CURRENT_TIMESTAMP)
                ORDER BY requested_at LIMIT %s""",
                (bounded,),
            )
            rows = await result.fetchall()
        commands: list[DeletionCommand] = []
        for row in rows:
            kind = DeletionKind(str(row[2]))
            value: str | datetime | None = row[3]
            if kind is DeletionKind.CUTOFF and isinstance(value, str):
                value = datetime.fromisoformat(value.replace("Z", "+00:00"))
            commands.append(
                DeletionCommand(
                    command_id=str(row[0]),
                    idempotency_key=str(row[1]),
                    selector=DeletionSelector(kind, value),
                    confirmation=str(row[4]),
                    requested_at=row[5],
                )
            )
        return commands

    async def deletion_targets(self, command_id: str, *, limit: int) -> list[Mapping[str, str]]:
        bounded = max(1, min(1000, int(limit)))
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT artifact_reference_id
                FROM observatory_evaluation_deletion_targets
                WHERE command_id = %s AND completed_at IS NULL
                ORDER BY artifact_reference_id LIMIT %s""",
                (command_id, bounded),
            )
            rows = await result.fetchall()
        return [{"artifact_reference_id": str(row[0])} for row in rows]

    async def deletion_score_targets(
        self, command_id: str, *, limit: int
    ) -> list[Mapping[str, str]]:
        bounded = max(1, min(1000, int(limit)))
        async with self._connection_factory() as connection:
            result = await connection.execute(
                """SELECT score_id
                FROM observatory_evaluation_deletion_score_targets
                WHERE command_id = %s AND completed_at IS NULL
                ORDER BY score_id LIMIT %s""",
                (command_id, bounded),
            )
            rows = await result.fetchall()
        return [{"score_id": str(row[0])} for row in rows]

    async def mark_deletion_target(
        self, command_id: str, artifact_reference_id: str, *, lease_token: str | None = None
    ) -> bool:
        async with self._connection_factory() as connection:
            if lease_token is None:
                result = await connection.execute(
                    "UPDATE observatory_evaluation_deletion_targets "
                    "SET completed_at = CURRENT_TIMESTAMP "
                    "WHERE command_id = %s AND artifact_reference_id = %s "
                    "AND completed_at IS NULL",
                    (command_id, artifact_reference_id),
                )
            else:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_targets
                    SET completed_at = CURRENT_TIMESTAMP
                    WHERE command_id = %s AND artifact_reference_id = %s
                      AND completed_at IS NULL
                      AND EXISTS (
                        SELECT 1 FROM observatory_evaluation_deletion_commands
                        WHERE command_id = %s AND status = 'running'
                          AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                      )""",
                    (command_id, artifact_reference_id, command_id, lease_token),
                )
            return _updated(result)

    async def mark_deletion_score_target(
        self, command_id: str, score_id: str, *, lease_token: str | None = None
    ) -> bool:
        normalized = _canonical_score_id(score_id)
        async with self._connection_factory() as connection:
            if lease_token is None:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_score_targets
                    SET completed_at = CURRENT_TIMESTAMP
                    WHERE command_id = %s AND score_id = %s AND completed_at IS NULL""",
                    (command_id, normalized),
                )
            else:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_score_targets
                    SET completed_at = CURRENT_TIMESTAMP
                    WHERE command_id = %s AND score_id = %s
                      AND completed_at IS NULL
                      AND EXISTS (
                        SELECT 1 FROM observatory_evaluation_deletion_commands
                        WHERE command_id = %s AND status = 'running'
                          AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                      )""",
                    (command_id, normalized, command_id, lease_token),
                )
            return _updated(result)

    async def delete_artifact_metadata(
        self,
        artifact_reference_id: str,
        *,
        command_id: str | None = None,
        lease_token: str | None = None,
    ) -> bool:
        async with self._connection_factory() as connection:
            if command_id is None or lease_token is None:
                result = await connection.execute(
                    "DELETE FROM observatory_evaluation_artifacts "
                    "WHERE artifact_reference_id = %s "
                    "AND status IN ('available', 'failed', 'deleting')",
                    (artifact_reference_id,),
                )
            else:
                result = await connection.execute(
                    """DELETE FROM observatory_evaluation_artifacts
                    WHERE artifact_reference_id = %s
                      AND status IN ('available', 'failed', 'deleting')
                      AND EXISTS (
                        SELECT 1 FROM observatory_evaluation_deletion_commands
                        WHERE command_id = %s AND status = 'running'
                          AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP
                      )""",
                    (artifact_reference_id, command_id, lease_token),
                )
            return _updated(result)

    async def complete_deletion_command(
        self, command_id: str, deleted_count: int = 0, *, lease_token: str | None = None
    ) -> int | None:
        async with self._connection_factory() as connection:
            if lease_token is None:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_commands
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                    deleted_count = %s, updated_at = CURRENT_TIMESTAMP,
                    lease_token = NULL, lease_expires_at = NULL
                    WHERE command_id = %s AND status <> 'completed'
                    RETURNING deleted_count""",
                    (max(0, deleted_count), command_id),
                )
            else:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_commands AS command
                    SET status = 'completed', completed_at = CURRENT_TIMESTAMP,
                    deleted_count = (
                        SELECT COUNT(*)::BIGINT
                        FROM observatory_evaluation_deletion_targets
                        WHERE command_id = %s AND completed_at IS NOT NULL
                    ),
                    updated_at = CURRENT_TIMESTAMP,
                    lease_token = NULL, lease_expires_at = NULL
                    WHERE command.command_id = %s AND command.status = 'running'
                      AND command.lease_token = %s
                      AND command.lease_expires_at > CURRENT_TIMESTAMP
                    RETURNING command.deleted_count""",
                    (command_id, command_id, lease_token),
                )
            row = await result.fetchone()
            if not _updated(result) and row is None:
                terminal = await connection.execute(
                    """SELECT status, deleted_count
                    FROM observatory_evaluation_deletion_commands
                    WHERE command_id = %s""",
                    (command_id,),
                )
                terminal_row = await terminal.fetchone()
                if terminal_row is None or str(_row_value(terminal_row, 0, "status")) != "completed":
                    return None
                # A completed command is immutable. Recovery/retry callers
                # receive its authoritative count without appending another
                # audit or repeating notification deletion.
                return int(_row_value(terminal_row, 1, "deleted_count"))
            persisted_count = int(row[0]) if row is not None else max(0, deleted_count)
            await connection.execute(
                """INSERT INTO observatory_evaluation_deletion_audits(
                audit_id, command_id, outcome, failure_code, deleted_count)
                VALUES (%s, %s, 'completed', NULL, %s)
                ON CONFLICT (command_id) WHERE outcome = 'completed' DO NOTHING""",
                (str(uuid4()), command_id, persisted_count),
            )
            # Ordinary notification rows are projections and may be removed
            # with the same authorized selector. Content-free deletion audit
            # evidence is preserved in its dedicated table and live rows.
            await connection.execute(
                """DELETE FROM observatory_evaluation_live_events AS live
                WHERE resource_type <> 'evaluation_deletion'
                  AND EXISTS (
                    SELECT 1 FROM observatory_evaluation_deletion_commands AS command
                    WHERE command.command_id = %s
                      AND (
                        command.selector_kind = 'purge_all'
                        OR (command.selector_kind = 'cutoff'
                            AND occurred_at < command.selector_value::timestamptz)
                        OR (command.selector_kind = 'job'
                            AND (
                                (resource_type = 'evaluation_job'
                                 AND resource_id = command.selector_value)
                                OR (resource_type = 'evaluation_attempt'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_attempts AS attempt
                                        WHERE attempt.attempt_id::text = live.resource_id
                                          AND attempt.job_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_result'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_result_receipts AS result
                                        WHERE result.result_id::text = live.resource_id
                                          AND result.job_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_artifact'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_artifacts AS artifact
                                        WHERE artifact.artifact_id::text = live.resource_id
                                          AND artifact.job_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_artifact'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_deletion_targets AS target
                                        WHERE target.command_id = command.command_id
                                          AND target.artifact_id::text = live.resource_id
                                          AND target.job_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_review'
                                    AND EXISTS (
                                        SELECT 1
                                        FROM observatory_local_judge_reviews AS review
                                        JOIN observatory_evaluation_result_receipts AS result
                                          ON result.result_id = review.result_id
                                        WHERE review.review_id::text = live.resource_id
                                          AND result.job_id::text = command.selector_value
                                    ))
                            ))
                        OR (command.selector_kind = 'dataset'
                            AND (
                                (resource_type = 'evaluation_dataset'
                                 AND resource_id = command.selector_value)
                                OR (resource_type = 'evaluation_case'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_cases AS case_row
                                        WHERE case_row.case_id::text = live.resource_id
                                          AND case_row.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_suite'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_suites AS suite
                                        WHERE suite.suite_id::text = live.resource_id
                                          AND suite.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_job'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_jobs AS job
                                        WHERE job.job_id::text = live.resource_id
                                          AND job.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_attempt'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_attempts AS attempt
                                        JOIN observatory_evaluation_jobs AS job
                                          ON job.job_id = attempt.job_id
                                        WHERE attempt.attempt_id::text = live.resource_id
                                          AND job.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_result'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_result_receipts AS result
                                        JOIN observatory_evaluation_jobs AS job
                                          ON job.job_id = result.job_id
                                        WHERE result.result_id::text = live.resource_id
                                          AND job.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_artifact'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_artifacts AS artifact
                                        WHERE artifact.artifact_id::text = live.resource_id
                                          AND artifact.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_artifact'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_deletion_targets AS target
                                        WHERE target.command_id = command.command_id
                                          AND target.artifact_id::text = live.resource_id
                                          AND target.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_calibration'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_local_judge_calibration_definitions AS calibration
                                        WHERE calibration.calibration_id::text = live.resource_id
                                          AND calibration.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_qualification'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_local_judge_qualifications AS qualification
                                        WHERE qualification.qualification_id::text = live.resource_id
                                          AND qualification.dataset_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_review'
                                    AND EXISTS (
                                        SELECT 1
                                        FROM observatory_local_judge_reviews AS review
                                        LEFT JOIN observatory_evaluation_result_receipts AS result
                                          ON result.result_id = review.result_id
                                        LEFT JOIN observatory_evaluation_jobs AS job
                                          ON job.job_id = result.job_id
                                        LEFT JOIN observatory_local_judge_calibration_definitions AS calibration
                                          ON calibration.calibration_id = review.calibration_id
                                        WHERE review.review_id::text = live.resource_id
                                          AND (job.dataset_id::text = command.selector_value
                                               OR calibration.dataset_id::text = command.selector_value)
                                    ))
                            ))
                        OR (command.selector_kind = 'suite'
                            AND (
                                (resource_type = 'evaluation_suite'
                                 AND resource_id = command.selector_value)
                                OR (resource_type = 'evaluation_job'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_jobs AS job
                                        WHERE job.job_id::text = live.resource_id
                                          AND job.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_attempt'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_attempts AS attempt
                                        JOIN observatory_evaluation_jobs AS job
                                          ON job.job_id = attempt.job_id
                                        WHERE attempt.attempt_id::text = live.resource_id
                                          AND job.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_result'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_result_receipts AS result
                                        JOIN observatory_evaluation_jobs AS job
                                          ON job.job_id = result.job_id
                                        WHERE result.result_id::text = live.resource_id
                                          AND job.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_artifact'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_artifacts AS artifact
                                        WHERE artifact.artifact_id::text = live.resource_id
                                          AND artifact.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_artifact'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_evaluation_deletion_targets AS target
                                        WHERE target.command_id = command.command_id
                                          AND target.artifact_id::text = live.resource_id
                                          AND target.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_calibration'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_local_judge_calibration_definitions AS calibration
                                        WHERE calibration.calibration_id::text = live.resource_id
                                          AND calibration.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_qualification'
                                    AND EXISTS (
                                        SELECT 1 FROM observatory_local_judge_qualifications AS qualification
                                        WHERE qualification.qualification_id::text = live.resource_id
                                          AND qualification.suite_id::text = command.selector_value
                                    ))
                                OR (resource_type = 'evaluation_review'
                                    AND EXISTS (
                                        SELECT 1
                                        FROM observatory_local_judge_reviews AS review
                                        LEFT JOIN observatory_evaluation_result_receipts AS result
                                          ON result.result_id = review.result_id
                                        LEFT JOIN observatory_evaluation_jobs AS job
                                          ON job.job_id = result.job_id
                                        LEFT JOIN observatory_local_judge_calibration_definitions AS calibration
                                          ON calibration.calibration_id = review.calibration_id
                                        WHERE review.review_id::text = live.resource_id
                                          AND (job.suite_id::text = command.selector_value
                                               OR calibration.suite_id::text = command.selector_value)
                                    ))
                            ))
                      )
                  )""",
                (command_id,),
            )
            return persisted_count

    async def fail_deletion_command(
        self, command_id: str, failure_code: str, *, lease_token: str | None = None
    ) -> bool:
        if failure_code not in {
            "storage_failure",
            "storage_authentication",
            "storage_integrity",
            "storage_timeout",
            "database_failure",
            "target_invalid",
            "lease_lost",
        }:
            failure_code = "storage_failure"
        async with self._connection_factory() as connection:
            if lease_token is None:
                result = await connection.execute(
                    "UPDATE observatory_evaluation_deletion_commands "
                    "SET status = 'queued', failure_code = %s, updated_at = CURRENT_TIMESTAMP, "
                    "lease_token = NULL, lease_expires_at = NULL "
                    "WHERE command_id = %s",
                    (failure_code, command_id),
                )
            else:
                result = await connection.execute(
                    """UPDATE observatory_evaluation_deletion_commands
                    SET status = 'queued', failure_code = %s, updated_at = CURRENT_TIMESTAMP,
                        lease_token = NULL, lease_expires_at = NULL
                    WHERE command_id = %s AND status = 'running'
                      AND lease_token = %s AND lease_expires_at > CURRENT_TIMESTAMP""",
                    (failure_code, command_id, lease_token),
                )
            if not _updated(result):
                return False
            await connection.execute(
                """INSERT INTO observatory_evaluation_deletion_audits(
                audit_id, command_id, outcome, failure_code, deleted_count)
                VALUES (%s, %s, 'failed', %s, 0)""",
                (str(uuid4()), command_id, failure_code),
            )
            return True


class ClickHouseEvaluationScoreStore:
    """ClickHouse score storage with bounded immutable deletion snapshots."""

    # A deletion command snapshots every matching score ID before it is
    # queued.  This is intentionally a hard ceiling: silently truncating a
    # selector would leave scores behind while reporting a successful purge.
    MAX_SCORE_TARGETS = 10_000

    """Finite, metadata-only evaluation score persistence."""

    def __init__(
        self,
        endpoint: str,
        *,
        database: str,
        user: str = "default",
        password: str = "",
        metrics: ObservatoryMetrics | None = None,
        component_id: str = "aura.observatory.deterministic_evaluators",
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.database = database
        self.user = user
        self.password = password
        self._metrics = metrics
        self._component_id = component_id

    async def insert_score(self, score: EvaluationScore) -> None:
        score.validate()
        row = {
            "score_id": score.score_id,
            "job_id": score.job_id,
            "result_id": score.result_id,
            "dataset_id": score.dataset_id,
            "suite_id": score.suite_id,
            "evaluator_id": score.evaluator_id,
            "evaluator_version": score.evaluator_version,
            "evaluator_kind": score.evaluator_kind,
            "component_id": score.component_id,
            "component_version": score.component_version,
            "code_revision": score.code_revision,
            "image_digest": score.image_digest,
            "configuration_revision": score.configuration_revision,
            "provenance_component": score.provenance_component,
            "provenance_version": score.provenance_version,
            "provenance_digest": score.provenance_digest,
            "execution_provenance_json": (
                json.dumps(score.execution_provenance, sort_keys=True, separators=(",", ":"))
                if score.execution_provenance is not None
                else None
            ),
            "capture_classification": score.capture_classification,
            "evidence_status": score.evidence_status,
            "score_value": score.value,
            "outcome": score.outcome,
            # ClickHouse 25.8's default JSONEachRow DateTime64 parser uses
            # its basic UTC form unless best-effort parsing is enabled. Keep
            # the wire value provider-native instead of relying on a mutable
            # server setting or ISO-8601 ``T``/``Z`` extensions.
            "observed_at": _clickhouse_timestamp(score.observed_at),
            "artifact_reference_id": score.artifact_reference_id,
            "case_id": score.case_id,
            "metric": score.metric,
            "failure_code": score.failure_code,
            "evidence_reference_id": score.evidence_reference,
            "reproducibility": score.reproducibility,
            "threshold_applicable": score.threshold_applicable,
            "threshold_reference": score.threshold_reference,
            "threshold_value": score.threshold_value,
            "threshold_operator": score.threshold_operator,
            "threshold_status": score.threshold_status,
            "judge_definition_id": score.judge_definition_id,
            "judge_definition_version": score.judge_definition_version,
            "model_name": score.model_name,
            "model_version": score.model_version,
            "model_digest": score.model_digest,
            "prompt_version": score.prompt_version,
            "rubric_version": score.rubric_version,
            "calibration_version": score.calibration_version,
            "judge_seed": score.seed,
            "judge_budget_timeout_ms": score.budget_timeout_ms,
            "judge_budget_max_input_bytes": score.budget_max_input_bytes,
            "judge_budget_max_output_bytes": score.budget_max_output_bytes,
            "judge_budget_max_output_tokens": score.budget_max_output_tokens,
            "judge_budget_max_context_tokens": score.budget_max_context_tokens,
            "judge_budget_max_concurrency": score.budget_max_concurrency,
            "score_disposition": score.score_disposition,
            "qualification_id": score.qualification_id,
            "qualification_version": score.qualification_version,
            "prompt_artifact_id": score.prompt_artifact_id,
            "prompt_artifact_version": score.prompt_artifact_version,
            "prompt_artifact_digest": score.prompt_artifact_digest,
            "rubric_artifact_id": score.rubric_artifact_id,
            "rubric_artifact_version": score.rubric_artifact_version,
            "rubric_artifact_digest": score.rubric_artifact_digest,
            "calibration_id": score.calibration_id,
            "uncertainty": score.uncertainty,
            "disagreement": score.disagreement,
        }
        query = "INSERT INTO evaluation_scores FORMAT JSONEachRow\n" + json.dumps(
            row, separators=(",", ":")
        )
        started = monotonic()
        response: httpx.Response | None = None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
                response = await client.post(
                    self.endpoint,
                    params={
                        "database": self.database,
                        "insert_deduplicate": "1",
                        "insert_deduplication_token": score.score_id,
                    },
                    auth=(self.user, self.password),
                    content=query,
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            _LOGGER.error(
                "ClickHouse evaluation score insert failed status=%s",
                response.status_code if response is not None else 0,
            )
            self._record_write(started, "database_failure")
            raise ArtifactStorageError("database_failure") from exc
        except Exception:
            self._record_write(started, "storage_failure")
            raise
        self._record_write(started, None)

    def _record_write(self, started: float, error_code: str | None) -> None:
        if self._metrics is None:
            return
        outcome = "failure" if error_code else "success"
        self._metrics.record(
            "observatory_evaluation_dependency_outcome_count",
            labels={"operation": "clickhouse", "outcome": outcome},
        )
        self._metrics.record_component_span(
            component_id=self._component_id,
            component_version="1.0.0",
            deployment_id=self._metrics.deployment_id,
            operation="write",
            duration_ms=max(0.0, (monotonic() - started) * 1000.0),
            outcome=outcome,
            dependency="clickhouse",
            error_code=error_code,
        )

    async def delete_scores_for_artifact(self, artifact_reference_id: str) -> None:
        query = (
            "ALTER TABLE evaluation_scores DELETE WHERE artifact_reference_id = {reference:String}"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            response = await client.post(
                self.endpoint,
                params={
                    "database": self.database,
                    "param_reference": artifact_reference_id,
                    "mutations_sync": "2",
                },
                auth=(self.user, self.password),
                content=query,
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ArtifactStorageError("database_failure") from exc

    async def delete_scores_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime | None = None
    ) -> None:
        """Synchronously remove scores using the persisted selector snapshot.

        The predicate intentionally does not depend on artifact references, so
        metadata-only scores and nullable artifact references are removed too.
        ``mutations_sync=2`` makes ClickHouse wait for the mutation before the
        caller can publish a terminal deletion state.
        """

        selector.validate()
        predicate = _score_selector_predicate(selector, snapshot_at=snapshot_at)
        query = f"ALTER TABLE evaluation_scores DELETE WHERE {predicate}"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            response = await client.post(
                self.endpoint,
                params={
                    "database": self.database,
                    "mutations_sync": "2",
                },
                auth=(self.user, self.password),
                content=query,
            )
            try:
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                raise ArtifactStorageError("storage_timeout") from exc
            except httpx.HTTPStatusError as exc:
                raise ArtifactStorageError("database_failure") from exc

    async def list_score_ids_for_selector(
        self, selector: DeletionSelector, *, snapshot_at: datetime, limit: int
    ) -> list[str]:
        selector.validate()
        if snapshot_at.tzinfo is None or snapshot_at.utcoffset() != UTC.utcoffset(snapshot_at):
            raise ArtifactStorageError("deletion_requested_at_invalid")
        bounded = max(1, min(self.MAX_SCORE_TARGETS, int(limit)))
        predicate = _score_selector_predicate(selector, snapshot_at=snapshot_at)
        query = (
            "SELECT DISTINCT score_id FROM evaluation_scores "
            f"WHERE {predicate} ORDER BY score_id LIMIT {bounded + 1} FORMAT JSON"
        )
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                response = await client.post(
                    self.endpoint,
                    params={"database": self.database},
                    auth=(self.user, self.password),
                    content=query,
                )
                response.raise_for_status()
                payload: Any = _decode_clickhouse_score_rows(response)
            except httpx.TimeoutException as exc:
                raise ArtifactStorageError("storage_timeout") from exc
            except (httpx.HTTPStatusError, ValueError) as exc:
                raise ArtifactStorageError("database_failure") from exc
        if not isinstance(payload, list):
            raise ArtifactStorageError("database_failure")
        score_rows = cast(list[Any], payload)
        if len(score_rows) > bounded:
            raise ArtifactStorageError("target_limit_exceeded")
        try:
            return [
                _canonical_score_id(row[0] if isinstance(row, list) else row["score_id"])
                for row in score_rows
            ]
        except (KeyError, TypeError, ValueError) as exc:
            raise ArtifactStorageError("database_failure") from exc

    async def delete_scores_for_ids(self, score_ids: Sequence[str]) -> None:
        normalized = tuple(dict.fromkeys(_canonical_score_id(value) for value in score_ids))
        if not normalized:
            return
        if len(normalized) > 1000:
            raise ArtifactStorageError("target_limit_exceeded")
        literals = ",".join(f"toUUID('{value}')" for value in normalized)
        query = f"ALTER TABLE evaluation_scores DELETE WHERE score_id IN ({literals})"
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            try:
                response = await client.post(
                    self.endpoint,
                    params={"database": self.database, "mutations_sync": "2"},
                    auth=(self.user, self.password),
                    content=query,
                )
                response.raise_for_status()
            except httpx.TimeoutException as exc:
                raise ArtifactStorageError("storage_timeout") from exc
            except httpx.HTTPStatusError as exc:
                raise ArtifactStorageError("database_failure") from exc


def _decode_clickhouse_score_rows(response: httpx.Response) -> list[Any]:
    """Decode ClickHouse ``FORMAT JSON`` or JSONEachRow score output.

    The query requests ``FORMAT JSON`` explicitly, but accepting JSONEachRow
    keeps this boundary compatible with deployments that apply a default
    format at the HTTP proxy.  Both forms remain content-free and bounded by
    the caller's target limit check.
    """

    try:
        payload: Any = response.json()
    except ValueError:
        try:
            lines = [line for line in response.text.splitlines() if line.strip()]
            payload = [json.loads(line) for line in lines]
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ArtifactStorageError("database_failure") from exc
    if isinstance(payload, Mapping):
        payload_map = cast(Mapping[str, Any], payload)
        if "score_id" in payload_map:
            return [payload_map]
        rows: Any = payload_map.get("data")
        return cast(list[Any], rows) if isinstance(rows, list) else []
    if isinstance(payload, list):
        return cast(list[Any], payload)
    return []


def _clickhouse_timestamp(value: datetime) -> str:
    """Encode a UTC millisecond timestamp for ClickHouse DateTime64(3)."""

    utc = value.astimezone(UTC)
    milliseconds = utc.microsecond // 1_000
    return utc.strftime("%Y-%m-%d %H:%M:%S") + f".{milliseconds:03d}"


def _artifact_from_row(row: Any) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=str(_row_value(row, 0, "artifact_id")),
        artifact_reference_id=str(_row_value(row, 1, "artifact_reference_id")),
        request_id=str(_row_value(row, 2, "request_id")),
        result_id=str(_row_value(row, 3, "result_id")),
        idempotency_key=str(_row_value(row, 4, "idempotency_key")),
        authorization_record_id=str(_row_value(row, 5, "authorization_record_id")),
        capture_classification=str(_row_value(row, 6, "capture_classification")),
        media_classification=str(_row_value(row, 7, "media_classification")),
        media_type=str(_row_value(row, 8, "media_type")),
        byte_count=int(_row_value(row, 9, "byte_count")),
        sha256=str(_row_value(row, 10, "sha256")),
        job_id=str(_row_value(row, 11, "job_id")),
        dataset_id=str(_row_value(row, 12, "dataset_id")),
        suite_id=str(_row_value(row, 13, "suite_id")),
        created_at=_row_value(row, 14, "created_at"),
        intended_use=(
            str(_row_value(row, 17, "intended_use"))
            if _has_row(row, 17, "intended_use")
            else "input"
        ),
        artifact_version=(
            str(_row_value(row, 18, "artifact_version"))
            if _has_row(row, 18, "artifact_version")
            else "1.0.0"
        ),
    )


def _row_value(row: Any, index: int, name: str) -> Any:
    if isinstance(row, Mapping):
        return cast(Mapping[str, Any], row)[name]
    return row[index]


def _row_value_or(row: Any, index: int, name: str, default: Any) -> Any:
    if isinstance(row, Mapping):
        return cast(Mapping[str, Any], row).get(name, default)
    return row[index] if len(row) > index else default


def _has_row(row: Any, index: int, name: str | None = None) -> bool:
    if isinstance(row, Mapping):
        return name is None or name in row
    if isinstance(row, tuple):
        return len(cast(tuple[object, ...], row)) > index
    if isinstance(row, list):
        return len(cast(list[object], row)) > index
    return False


def _updated(result: Any) -> bool:
    rowcount = getattr(result, "rowcount", 1)
    return isinstance(rowcount, int) and rowcount > 0


def _canonical_score_id(value: Any) -> str:
    if not isinstance(value, str):
        raise ArtifactStorageError("target_invalid")
    try:
        parsed = UUID(value)
    except (AttributeError, ValueError) as exc:
        raise ArtifactStorageError("target_invalid") from exc
    if str(parsed) != value.lower() or len(value) != 36:
        raise ArtifactStorageError("target_invalid")
    return value.lower()


def _selector_sql(selector: Any) -> tuple[str, tuple[Any, ...]]:
    if selector.kind.value == "purge_all":
        return "TRUE", ()
    if selector.kind.value in {"job", "dataset", "suite"}:
        return f"{selector.kind.value}_id = %s", (str(selector.value),)
    return "created_at < %s", (selector.value.astimezone(UTC),)


def _score_selector_predicate(
    selector: DeletionSelector, *, snapshot_at: datetime | None = None
) -> str:
    snapshot_predicate = ""
    if snapshot_at is not None:
        timestamp = snapshot_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        snapshot_predicate = f" AND observed_at <= toDateTime64('{timestamp}', 3, 'UTC')"
    if selector.kind is DeletionKind.PURGE_ALL:
        return "1 = 1" + snapshot_predicate
    if selector.kind is DeletionKind.JOB:
        return f"job_id = toUUID('{selector.value}')" + snapshot_predicate
    if selector.kind is DeletionKind.DATASET:
        return f"dataset_id = toUUID('{selector.value}')" + snapshot_predicate
    if selector.kind is DeletionKind.SUITE:
        return f"suite_id = toUUID('{selector.value}')" + snapshot_predicate
    assert isinstance(selector.value, datetime)
    timestamp = selector.value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
    return f"observed_at < toDateTime64('{timestamp}', 3, 'UTC')" + snapshot_predicate


__all__ = ["ClickHouseEvaluationScoreStore", "PostgresEvaluationArtifactRepository"]
