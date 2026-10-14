"""Static safeguards for the immutable document-search policy migration."""

from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).parents[1]
MIGRATION = ROOT / "migrations/versions/0020_document_search_tool_citations.py"


def _module():
    spec = importlib.util.spec_from_file_location("aura_migration_0020", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_document_search_migration_is_forward_only_and_has_stable_successor_ids() -> None:
    migration = _module()

    assert migration.revision == "0020_document_search_citations"
    assert len(migration.revision) <= 32
    assert migration.down_revision == "0019_document_library"
    assert migration._GENERAL_DOCUMENT_POLICY == migration.uuid.uuid5(
        migration._NAMESPACE, "general-assistant-tool-policy-4"
    )
    assert migration._GENERAL_DOCUMENT_REVISION == migration.uuid.uuid5(
        migration._NAMESPACE, "general-assistant-revision-4"
    )
    assert '"document.search"' in MIGRATION.read_text()


def test_document_search_migration_persists_only_citation_metadata_columns() -> None:
    source = MIGRATION.read_text()

    assert "messages ADD COLUMN IF NOT EXISTS citations JSONB" in source
    assert "runs ADD COLUMN IF NOT EXISTS citations JSONB" in source
    assert "document_search_used BOOLEAN" in source
    assert "ALTER TABLE messages" in source
    assert "ALTER TABLE runs" in source
    assert "DROP COLUMN" not in source
    assert "downgrade is disabled" in source
