"""Static migration and persistence-privacy checks for AURA-0090."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).parents[3]
PG = ROOT / (
    "services/aura-observatory/migrations/postgresql/versions/0007_evaluation_artifact_storage.sql"
)
CH = ROOT / (
    "services/aura-observatory/migrations/clickhouse/versions/0005_evaluation_scores.sql"
)
ADAPTER = ROOT / (
    "services/aura-observatory/src/aura_observatory/adapters/evaluation_storage.py"
)


def test_postgresql_migration_is_forward_only_idempotent_and_observatory_owned() -> None:
    sql = PG.read_text(encoding="utf-8")
    sql_statements = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    expected_tables = {
        "observatory_evaluation_authorizations",
        "observatory_evaluation_artifacts",
        "observatory_evaluation_deletion_commands",
        "observatory_evaluation_deletion_targets",
        "observatory_evaluation_deletion_score_targets",
        "observatory_evaluation_deletion_audits",
        "observatory_evaluation_metric_series",
    }
    assert all(f"CREATE TABLE IF NOT EXISTS {table}" in sql for table in expected_tables)
    assert sql.count("CREATE TABLE IF NOT EXISTS") == len(expected_tables)
    index_statements = re.findall(r"CREATE (?:UNIQUE )?INDEX[^;]+", sql)
    assert index_statements
    assert all("IF NOT EXISTS" in statement for statement in index_statements)
    assert "DROP TABLE" not in sql.upper()
    assert "ALTER TABLE" not in sql.upper()
    assert "declared_max_bytes BIGINT NOT NULL DEFAULT 0" in sql
    assert "lease_token UUID" in sql
    assert "lease_expires_at TIMESTAMPTZ" in sql
    assert "attempt_count INTEGER NOT NULL DEFAULT 0" in sql
    assert "deletion_phase VARCHAR(16) NOT NULL DEFAULT 'queued'" in sql
    assert "ON DELETE CASCADE" in sql
    assert "manual_only" in sql
    assert "DELETE_EVALUATION_ARTIFACTS" in sql
    assert "label_operation VARCHAR(16) NOT NULL DEFAULT ''" in sql
    assert "label_outcome VARCHAR(16) NOT NULL DEFAULT ''" in sql
    assert "label_selector VARCHAR(16) NOT NULL DEFAULT ''" in sql
    assert "label_phase VARCHAR(16) NOT NULL DEFAULT ''" in sql
    assert "label_failure_code VARCHAR(32) NOT NULL DEFAULT ''" in sql
    assert (
        "PRIMARY KEY (\n        metric_name, label_operation, label_outcome, label_selector,\n"
        "        label_phase, label_failure_code\n    )"
    ) in sql
    assert "metric_name IN (" in sql
    assert "observatory_evaluation_deletion_duration_ms" in sql
    assert "observatory_evaluation_metric_histogram_ck" in sql
    assert "JSONB" in sql
    assert "labels jsonb" not in sql.casefold()
    forbidden_columns = (
        "content",
        "filename",
        "endpoint",
        "object_key",
        "credential",
        "owner_identity",
    )
    for forbidden in forbidden_columns:
        assert re.search(
            rf"\b{re.escape(forbidden)}\b", sql_statements, flags=re.IGNORECASE
        ) is None


def test_postgresql_migration_has_bounded_checks_and_immutable_target_shape() -> None:
    sql = PG.read_text(encoding="utf-8")
    assert "byte_count >= 0 AND byte_count <= 1073741824" in sql
    assert "sha256 ~ '^[0-9a-f]{64}$'" in sql
    assert "selector_kind IN ('job', 'dataset', 'suite', 'cutoff', 'purge_all')" in sql
    assert "PRIMARY KEY (command_id, artifact_reference_id)" in sql
    assert "PRIMARY KEY (command_id, score_id)" in sql
    assert "observatory_evaluation_deletion_score_targets_pending_idx" in sql
    assert "observatory_evaluation_deletion_targets_live_ref_idx" in sql
    assert "ON observatory_evaluation_deletion_targets(artifact_reference_id)" in sql
    assert "WHERE completed_at IS NULL" in sql
    assert "command_id UUID PRIMARY KEY" in sql
    assert "outcome IN ('completed', 'failed')" in sql


def test_clickhouse_migration_is_idempotent_content_free_and_has_no_ttl() -> None:
    sql = CH.read_text(encoding="utf-8")
    sql_statements = "\n".join(line.split("--", 1)[0] for line in sql.splitlines())
    assert "CREATE TABLE IF NOT EXISTS evaluation_scores" in sql
    assert "ENGINE = ReplacingMergeTree" in sql
    assert "ORDER BY (job_id, result_id, score_id)" in sql
    assert "Nullable(Float64)" in sql
    assert "outcome LowCardinality(Nullable(String))" in sql
    assert "MODIFY COLUMN IF EXISTS outcome LowCardinality(Nullable(String))" in sql
    assert "Nullable(LowCardinality(String))" not in sql
    assert "TTL" not in sql.upper()
    assert "maps" not in sql_statements.casefold()
    assert "INSERT INTO observatory_migration_ledger" in sql
    assert "WHERE NOT EXISTS" in sql
    forbidden_columns = (
        "content",
        "prompt",
        "response",
        "rubric",
        "comment",
        "credential",
        "endpoint",
        "object_key",
    )
    for forbidden in forbidden_columns:
        assert re.search(
            rf"\b{re.escape(forbidden)}\b", sql_statements, flags=re.IGNORECASE
        ) is None


def test_evaluation_adapter_names_only_observatory_tables_and_never_logs_payloads() -> None:
    source = ADAPTER.read_text(encoding="utf-8")
    assert "observatory_evaluation_artifacts" in source
    assert "observatory_evaluation_deletion_commands" in source
    assert "observatory_evaluation_deletion_targets" in source
    assert "evaluation_scores" in source
    for forbidden in ("filename", "object_key", "owner_identity", "prompt", "rubric", "raw_bytes"):
        assert forbidden not in source.casefold()
    assert "insert_deduplication_token" in source
    assert "param_reference" in source
