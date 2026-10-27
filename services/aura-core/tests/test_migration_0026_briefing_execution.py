"""Migration-level invariants for the scheduled briefing state model."""

from __future__ import annotations

import importlib.util
from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations/versions/0026_briefing_execution.py"


def _source() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_has_one_forward_revision_and_owns_only_briefing_objects() -> None:
    spec = importlib.util.spec_from_file_location("aura_migration_0026", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.revision == "0026_briefing_execution"
    assert module.down_revision == "0025_notification_brief_source"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_migration_preserves_manual_retention_and_non_cascading_history() -> None:
    source = _source().lower()

    for table in (
        "briefing_schedules",
        "briefing_occurrences",
        "briefing_results",
        "briefing_command_idempotency",
        "briefing_run_lineage",
    ):
        assert f"create table if not exists {table}" in source
    assert "on delete cascade" not in source
    assert "drop table if exists briefing_run_lineage" in source
    assert "drop table if exists briefing_schedules" in source
    assert "deleted_at" in source


def test_migration_enforces_one_occurrence_per_schedule_local_date_and_one_result() -> None:
    source = _source()

    assert "UNIQUE (schedule_id, due_local_date)" in source
    assert "UNIQUE (schedule_id, occurrence_sequence)" in source
    assert "UNIQUE (occurrence_id)" in source
    assert (
        "CHECK (status IN ('scheduled', 'admitted', 'running', 'completed', "
        "'partial', 'failed', 'canceled', 'skipped', 'unknown'))"
    ) in source
    assert (
        "CHECK (source_completeness IN ('complete', 'partial', 'unavailable', "
        "'unknown'))"
    ) in source


def test_migration_keeps_command_receipts_owner_scoped_and_replay_safe() -> None:
    source = _source()

    assert "PRIMARY KEY (principal_issuer, principal_subject, operation, idempotency_key)" in source
    assert "fingerprint VARCHAR(64) NOT NULL" in source
    assert "response JSONB NOT NULL" in source
    assert "briefing_run_lineage" in source
    assert "run_id UUID NOT NULL UNIQUE" in source
