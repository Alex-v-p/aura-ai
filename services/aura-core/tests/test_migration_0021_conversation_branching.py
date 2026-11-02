"""Static safeguards for the immutable conversation-branching migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "migrations/versions/0021_conversation_branching.py"


def _module():
    spec = importlib.util.spec_from_file_location("aura_migration_0021", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_conversation_branching_migration_is_the_linear_history_successor() -> None:
    migration = _module()

    assert migration.revision == "0021_conversation_branching"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0020_document_search_citations"


def test_conversation_branching_migration_preserves_existing_rows_and_backfills_root() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    normalized = " ".join(source.split())

    # Branch identity is additive: old messages/runs stay addressable and are
    # assigned to one root branch without copying or rewriting their content.
    assert "CREATE TABLE IF NOT EXISTS conversation_branches" in normalized
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS branch_id" in normalized
    assert "ALTER TABLE messages ADD COLUMN IF NOT EXISTS parent_message_id" in normalized
    assert "ALTER TABLE runs ADD COLUMN IF NOT EXISTS branch_id" in normalized
    assert "ALTER TABLE runs ADD COLUMN IF NOT EXISTS regenerated_from_run_id" in normalized
    assert "INSERT INTO conversation_branches" in normalized
    assert "UPDATE messages m SET branch_id" in normalized
    assert "UPDATE runs r SET branch_id" in normalized
    assert "UPDATE messages" in source
    assert "UPDATE runs" in source


def test_conversation_branching_migration_does_not_enable_destructive_downgrade() -> None:
    source = MIGRATION.read_text(encoding="utf-8")

    assert "downgrade" in source.lower() and "disabled" in source.lower()
    assert "DROP TABLE" not in source
    assert "DROP COLUMN" not in source


def test_conversation_branching_backfill_has_no_root_bind_parameter() -> None:
    migration = _module()
    statements: list[object] = []
    migration.op.execute = statements.append

    migration.upgrade()

    backfill = next(
        statement
        for statement in statements
        if "INSERT INTO conversation_branches" in str(statement)
    )
    bindparams = getattr(backfill, "_bindparams", {})
    assert "root" not in bindparams
    assert "chr(58) || 'root'" in str(backfill)
