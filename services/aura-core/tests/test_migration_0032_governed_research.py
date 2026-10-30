from __future__ import annotations

from pathlib import Path


def test_research_migration_is_additive_and_descends_from_memory_reindex() -> None:
    path = Path(__file__).parents[1] / "migrations" / "versions" / "0032_governed_research.py"
    text = path.read_text(encoding="utf-8")
    assert 'revision = "0032_governed_research"' in text
    assert 'down_revision = "0031_memory_reindex_task_version"' in text
    assert "CREATE TABLE IF NOT EXISTS research_tasks" in text
    assert "research_checkpoints" in text
    assert "research_evidence" in text
    assert "research_citations" in text
