"""AURA-0093 forward-only PostgreSQL definition migration checks."""

from __future__ import annotations

import inspect
from pathlib import Path

from aura_observatory.adapters.evaluation_definitions import (
    PostgresEvaluationDefinitionRepository,
)

ROOT = Path(__file__).parents[3]
MIGRATION = (
    ROOT
    / "services/aura-observatory/migrations/postgresql/versions/0009_evaluation_definitions.sql"
)
CLICKHOUSE_MIGRATION = (
    ROOT
    / "services/aura-observatory/migrations/clickhouse/versions/0006_evaluation_score_metadata.sql"
)


def test_definition_migration_is_forward_only_idempotent_and_normalized() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    tables = (
        "observatory_evaluation_datasets",
        "observatory_evaluation_cases",
        "observatory_evaluation_evaluators",
        "observatory_evaluation_suites",
        "observatory_evaluation_suite_evaluators",
        "observatory_evaluation_suite_components",
        "observatory_evaluation_suite_thresholds",
        "observatory_evaluation_definition_audits",
    )
    for table in tables:
        assert f"CREATE TABLE IF NOT EXISTS {table}" in sql
    assert sql.count("CREATE TABLE IF NOT EXISTS") == len(tables) + 1
    assert sql.count("CREATE INDEX IF NOT EXISTS") >= 2
    assert "DROP TABLE" not in sql.upper()
    assert "TRUNCATE" not in sql.upper()
    assert "observatory_core" not in sql
    for forbidden in (
        "prompt",
        "response",
        "filename",
        "endpoint",
        "credential",
        "owner_identity",
        "object_key",
    ):
        assert forbidden not in sql.casefold()


def test_migration_preserves_immutable_keys_and_restrictive_relationships() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "PRIMARY KEY (dataset_id, dataset_version)" in sql
    assert "PRIMARY KEY (case_id, case_version)" in sql
    assert "PRIMARY KEY (evaluator_id, evaluator_version)" in sql
    assert "PRIMARY KEY (suite_id, suite_version)" in sql
    assert sql.count("ON DELETE RESTRICT") >= 4
    assert "retention_mode = 'manual_only'" in sql
    assert "threshold_value = threshold_value" in sql
    assert "observatory_eval_suite_dataset_fk" in sql
    assert "FOREIGN KEY (dataset_id, dataset_version)" in sql
    assert "REFERENCES observatory_evaluation_datasets(dataset_id, dataset_version)" in sql


def test_dataset_case_membership_is_versioned_and_restrictive() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "observatory_evaluation_dataset_cases" in sql
    assert "PRIMARY KEY (dataset_id, dataset_version, case_id)" in sql
    assert "case_version VARCHAR(64) NOT NULL" in sql
    assert "FOREIGN KEY (case_id, case_version)" in sql
    assert "REFERENCES observatory_evaluation_cases(case_id, case_version)" in sql
    assert "ON DELETE RESTRICT" in sql


def test_clickhouse_score_metadata_migration_is_forward_only_idempotent_and_content_free() -> None:
    sql = CLICKHOUSE_MIGRATION.read_text(encoding="utf-8")
    for column in (
        "case_id Nullable(UUID)",
        "metric LowCardinality(String)",
        "failure_code LowCardinality(Nullable(String))",
        "evidence_reference_id Nullable(String)",
        "reproducibility LowCardinality(String)",
        "threshold_applicable Nullable(UInt8)",
        "threshold_reference Nullable(String)",
        "threshold_value Nullable(Float64)",
        "threshold_operator LowCardinality(Nullable(String))",
        "threshold_status LowCardinality(Nullable(String))",
    ):
        assert column in sql
    assert sql.count("ADD COLUMN IF NOT EXISTS") == 10
    assert "DROP" not in sql.upper()
    assert "TTL" not in sql.upper()
    sql_without_comments = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    for forbidden in ("prompt", "response", "content", "filename", "endpoint", "credential"):
        assert forbidden not in sql_without_comments.casefold()


def test_postgres_adapter_is_service_owned_and_does_not_read_core_state() -> None:
    source = inspect.getsource(PostgresEvaluationDefinitionRepository)
    assert "aura_core" not in source
    assert "observatory_" in source
    assert "observatory_component_definitions" in source
    assert "observatory_evaluation_" in source
