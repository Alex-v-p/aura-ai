from pathlib import Path


def test_research_completion_delivery_migration_is_additive_and_single_head() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0033_research_completion_delivery.py"
    )
    source = path.read_text(encoding="utf-8")
    assert 'revision = "0033_research_delivery"' in source
    assert 'down_revision = "0032_governed_research"' in source
    assert "research_completion_deliveries" in source
    assert "append_key" in source and "notification_key" in source
    assert "ADD COLUMN IF NOT EXISTS research_activity" in source
    assert "DROP CONSTRAINT IF EXISTS ck_notifications_source" in source


def test_research_migration_preserves_workflow_ordinal_contract_for_both_sources() -> None:
    path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "0033_research_completion_delivery.py"
    )
    source = path.read_text(encoding="utf-8")
    assert source.count("workflow_step_ordinal BETWEEN 1 AND 8") >= 2
    assert "workflow_step_ordinal IS NOT NULL" not in source
