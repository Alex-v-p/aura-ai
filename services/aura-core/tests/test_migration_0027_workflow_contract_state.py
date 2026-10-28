import importlib.util
from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations/versions/0027_workflow_contract_state.py"


def test_workflow_migration_is_single_forward_head() -> None:
    spec = importlib.util.spec_from_file_location("aura_migration_0027", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.revision == "0027_workflow_contract_state"
    assert module.down_revision == "0026_briefing_execution"
    assert module.branch_labels is None
    assert module.depends_on is None


def test_workflow_tables_are_bounded_and_non_cascading() -> None:
    source = MIGRATION.read_text(encoding="utf-8").lower()
    for table in (
        "workflow_definitions", "workflow_drafts", "workflow_revisions",
        "workflow_instances", "workflow_instance_steps", "workflow_checkpoints",
        "workflow_results", "workflow_instance_history", "workflow_tombstones",
        "workflow_command_idempotency", "workflow_lifecycle_events",
        "workflow_definition_tombstones", "workflow_revision_tombstones",
    ):
        assert f"create table if not exists {table}" in source
    assert "on delete cascade" not in source
    assert "replay_count between 0 and 1" in source
    assert "deadline > admitted_at and deadline <= admitted_at + interval '30 days'" in source
    assert "terminal_outcome" in source
    assert "octet_length(configuration::text) <= 65536" in source
    assert "references workflow_" not in source
    assert "workflow_definition_tombstone_owner" in source
    assert "workflow_revision_tombstone_owner" in source
    assert "workflow_tombstones" in source and "version integer not null" in source
    assert "workflow_revisions" in source and "check (version > 0)" in source
    assert "workflow_revision_tombstones" in source and "version integer not null" in source


def test_migration_evolves_approval_notification_and_run_lineage_additively() -> None:
    source = MIGRATION.read_text(encoding="utf-8").lower()
    compact = " ".join(source.split())
    for table in (
        "approvals",
        "approval_events",
        "notifications",
        "notification_events",
        "runs",
        "run_events",
    ):
        assert f"alter table {table}" in source
    for column in (
        "origin", "workflow_id", "workflow_revision_id", "workflow_instance_id",
        "workflow_step_ordinal", "workflow_step_execution_id", "workflow_result_id",
    ):
        assert f"add column if not exists {column}" in source
    assert "alter column run_id drop not null" in source
    assert "alter column conversation_id drop not null" in source
    assert "alter column user_message_id drop not null" in source
    assert "ck_approvals_workflow_lineage" in source
    assert "ck_approval_events_workflow_lineage" in source
    assert "ck_notifications_source" in source
    assert "ck_notification_events_source" in source
    assert "ck_runs_workflow_lineage" in source
    assert "ck_run_events_workflow_lineage" in source
    assert "ck_runs_closed_lineage" in source
    assert "ck_run_events_closed_lineage" in source
    assert "conversation_id is not null and user_message_id is not null" in compact
    assert "conversation_id is null and user_message_id is null" in compact
    assert "workflow_step_execution_id is not null" in source
    assert "ck_approvals_status" in source
    assert "ck_approval_events_status" in source


def test_migration_rejects_mixed_or_incomplete_lineage_origins() -> None:
    source = MIGRATION.read_text(encoding="utf-8").lower()
    # Every ordinary branch closes all workflow fields, and every workflow
    # branch closes both conversation lineage fields and requires all origin
    # fields. These structural assertions guard against nullable-union drift.
    assert "workflow_id is null" in source
    assert "workflow_revision_id is null" in source
    assert "workflow_instance_id is null" in source
    assert "workflow_step_ordinal is null" in source
    assert "workflow_step_execution_id is null" in source
    assert source.count("workflow_id is not null") >= 6
    assert source.count("workflow_revision_id is not null") >= 6
    assert source.count("workflow_instance_id is not null") >= 6


def test_migration_adds_owner_scoped_workflow_indexes_and_safe_rollback() -> None:
    source = MIGRATION.read_text(encoding="utf-8").lower()
    for index in (
        "ix_approvals_workflow_instance",
        "ix_approval_events_workflow_instance",
        "uq_notification_workflow_result",
        "ix_notification_workflow_instance",
        "ix_notification_event_workflow_instance",
        "ix_runs_workflow_instance",
        "ix_run_events_workflow_instance",
    ):
        prefix = "unique " if index.startswith("uq_") else ""
        assert f"create {prefix}index if not exists {index}" in source
    assert "drop index if exists uq_notification_workflow_result" in source
    assert "drop column if exists workflow_step_execution_id" in source
    assert "on delete cascade" not in source
