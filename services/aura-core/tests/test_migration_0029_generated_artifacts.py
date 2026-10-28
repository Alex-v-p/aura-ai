# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path

MIGRATION = Path(__file__).parents[1] / "migrations/versions/0029_generated_artifacts.py"


def test_generated_artifact_migration_identity_and_boundary() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert 'revision = "0029_generated_artifacts"' in source
    assert 'down_revision = "0028_workflow_execution"' in source
    assert "generated_artifacts" in source
    assert "generated_artifact_revisions" in source
    assert "0030_task_projection" not in source
