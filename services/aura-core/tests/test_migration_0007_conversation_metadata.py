"""Migration-shape and opt-in PostgreSQL coverage for AURA-0032."""

import importlib.util
import os
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest
import yaml
from alembic import command
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from aura_core.platform.telemetry import COMPONENT_VERSIONS
from sqlalchemy import create_engine, text

MIGRATION_DIR = Path(__file__).parents[1] / "migrations" / "versions"
MANIFEST_PATH = (
    Path(__file__).parents[1]
    / "resources"
    / "component-manifests"
    / "conversation-persistence.yaml"
)


def _migration() -> tuple[Path, Any]:
    paths = sorted(MIGRATION_DIR.glob("0007_*.py"))
    assert len(paths) == 1, "AURA-0032 must have one 0007 migration"
    path = paths[0]
    spec = importlib.util.spec_from_file_location("aura_migration_0007", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return path, module


def _database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL migration coverage")
    return value


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


def test_migration_0007_has_one_forward_upgrade_after_title_generation() -> None:
    path, migration = _migration()
    assert path.name == "0007_conversation_metadata.py"
    assert migration.revision == "0007_conversation_metadata"
    assert migration.down_revision == "0006_title_generation"
    source = path.read_text().lower()
    assert "archived_at" in source
    assert "create index" in source
    assert "principal_id" in source
    assert "downgrade is disabled" in source or "raise runtimeerror" in source
    assert "delete from conversations" not in source
    assert "drop table conversations" not in source


def test_conversation_component_manifest_matches_versioned_metadata_contract() -> None:
    manifest = cast(dict[str, Any], yaml.safe_load(MANIFEST_PATH.read_text()))
    component = cast(dict[str, Any], manifest["component"])
    output = cast(dict[str, Any], manifest["output"])
    capture = cast(dict[str, Any], output["capture"])
    assert component["id"] == "aura.interaction.conversation_persistence"
    assert component["version"] == COMPONENT_VERSIONS[component["id"]]
    assert component["version"] == "1.6.0"
    assert capture["content"] == "none"
    assert output["schema"] == "core-conversation-record-v2"
    assert {
        "conversation_list_duration_ms",
        "conversation_metadata_mutation_duration_ms",
        "conversation_filter_outcome",
        "conversation_archive_outcome",
    } <= set(manifest["metrics"])
    assert manifest["capture_policy"] == "metadata_only"
    assert {
        "prompt",
        "response",
        "payload",
        "endpoint",
        "token",
        "credential",
    } <= set(manifest["cardinality"]["prohibited"])


def test_fresh_postgresql_install_adds_nullable_archive_column_and_owner_index() -> None:
    url = _database_url("AURA_0032_MIGRATION_FRESH_DATABASE_URL")
    _upgrade(url, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            column = connection.execute(
                text(
                    "SELECT is_nullable, data_type FROM information_schema.columns "
                    "WHERE table_name = 'conversations' AND column_name = 'archived_at'"
                )
            ).one()
            assert column == ("YES", "timestamp with time zone")
            indexes = connection.execute(
                text("SELECT indexdef FROM pg_indexes WHERE tablename = 'conversations'")
            ).scalars().all()
            assert any(
                "principal_id" in definition and "archived_at" in definition
                for definition in indexes
            )
    finally:
        engine.dispose()


def test_upgrade_from_0006_preserves_existing_conversation_identity_and_metadata() -> None:
    url = _database_url("AURA_0032_MIGRATION_UPGRADE_DATABASE_URL")
    _upgrade(url, "0006_title_generation")
    principal_id, conversation_id, message_id = uuid4(), uuid4(), uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            profile = connection.execute(
                text(
                    "SELECT id, current_revision_id FROM agent_profiles "
                    "WHERE slug = 'general-assistant'"
                )
            ).one()
            connection.execute(
                text(
                    "INSERT INTO principals (id, issuer, subject, display_name) "
                    "VALUES (:id, 'https://issuer', :subject, 'Owner')"
                ),
                {"id": principal_id, "subject": f"owner-{principal_id}"},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_id, title, title_state, agent_profile_id, "
                    "agent_revision_id, model_id, version) "
                    "VALUES (:id, :principal, 'Legacy title', 'legacy', :profile, "
                    ":revision, 'chat', 7)"
                ),
                {
                    "id": conversation_id,
                    "principal": principal_id,
                    "profile": profile.id,
                    "revision": profile.current_revision_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, conversation_id, role, content, state) "
                    "VALUES (:id, :conversation, 'user', 'legacy body', 'complete')"
                ),
                {"id": message_id, "conversation": conversation_id},
            )
        _upgrade(url, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text(
                    "SELECT id, title, title_state, version, archived_at "
                    "FROM conversations WHERE id = :id"
                ),
                {"id": conversation_id},
            ).one()
            assert row == (conversation_id, "Legacy title", "legacy", 7, None)
            assert connection.execute(
                text("SELECT id FROM messages WHERE id = :id"), {"id": message_id}
            ).scalar_one() == message_id
    finally:
        engine.dispose()
