"""Static checks for the forward-only orchestration migration."""

import inspect
from pathlib import Path

from aura_observatory.adapters.evaluation_orchestration import (
    PostgresEvaluationOrchestrationRepository,
)

ROOT = Path(__file__).parents[3]
MIGRATION = (
    ROOT
    / "services/aura-observatory/migrations/postgresql/versions/0008_evaluation_orchestration.sql"
)


def test_migration_is_idempotent_and_has_durable_state() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    for table in (
        "observatory_evaluation_jobs",
        "observatory_evaluation_attempts",
        "observatory_evaluation_schedules",
        "observatory_evaluation_outbox",
        "observatory_evaluation_result_receipts",
        "observatory_evaluation_lifecycle_audits",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert "FOR UPDATE" not in sql
    assert "DROP TABLE" not in sql.upper()
    assert "ALTER ' || 'TABLE observatory_evaluation_metric_series" in sql
    assert "observatory_evaluation_dependency_outcome_count" in sql
    assert "ALTER COLUMN label_operation TYPE VARCHAR(64)" in sql
    assert "ALTER COLUMN label_failure_code TYPE VARCHAR(64)" in sql
    assert "published_at" in sql
    assert "lease_expires_at" in sql
    assert "UNIQUE (job_id, attempt_number)" in sql
    for forbidden in ("filename", "object_key", "credential", "owner_identity"):
        assert forbidden not in sql.casefold()


def test_claim_outbox_locks_only_outbox_rows() -> None:
    """PostgreSQL must not lock the nullable side of an outer join."""

    source = inspect.getsource(PostgresEvaluationOrchestrationRepository.claim_outbox)
    assert "LEFT JOIN observatory_evaluation_attempts" not in source
    assert "EXISTS (" in source
    assert "FOR UPDATE SKIP LOCKED" in source


def test_outbox_producers_set_due_time_from_their_transaction_clock() -> None:
    """A clock-skewed database default must not hide newly admitted work."""

    for producer in ("admit", "request_cancellation", "materialize_due_schedules"):
        source = inspect.getsource(getattr(PostgresEvaluationOrchestrationRepository, producer))
        assert "next_publish_at" in source
        assert "VALUES" in source
