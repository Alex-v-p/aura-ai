"""Static and executable-shape checks for the AURA-0023 migration.

The PostgreSQL fixture is intentionally opt-in in the repository.  These
checks still fail loudly once the migration is present if it does not describe
the required forward-only, deterministic upgrade path.
"""

import importlib.util
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from aura_core.bootstrap.database import metadata
from aura_core.domains.governance.audit.persistence import AuditRow
from aura_core.domains.interaction.agents.public import AgentCatalog
from aura_core.domains.interaction.personas.adapters import SqlPersonaStore
from aura_core.domains.interaction.personas.public import (
    ConfigurationDisabled,
    ConfigurationStatus,
    PersonaConfigurationService,
)
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from sqlalchemy import create_engine, select, text

MIGRATION_DIR = Path(__file__).parents[1] / "migrations" / "versions"


def _database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL migration coverage")
    return value


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(alembic_config(Settings(database_url=url)), revision)


def _isolated_database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip(
            f"{name} and AURA_TEST_DEPENDENCIES_ISOLATED=1 are required for SQL adapter coverage"
        )
    return value


def _migration() -> Any:
    candidates = sorted(MIGRATION_DIR.glob("0005_*.py"))
    if not candidates:
        pytest.skip("AURA-0023 migration has not landed yet")
    assert len(candidates) == 1, "AURA-0023 must have one 0005 migration"
    migration_path = candidates[0]
    spec = importlib.util.spec_from_file_location("aura_migration_0005", migration_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_persona_override_migration_has_one_forward_upgrade_after_0004() -> None:
    migration = _migration()
    assert str(migration.revision).startswith("0005")
    assert migration.down_revision == "0004_agent_persona_revisions"
    source = next(MIGRATION_DIR.glob("0005_*.py")).read_text().lower()
    assert "downgrade is disabled" in source.lower() or "raise RuntimeError" in source


def test_persona_override_migration_is_idempotent_and_backfills_history() -> None:
    _migration()
    source = next(MIGRATION_DIR.glob("0005_*.py")).read_text().lower()
    required_fragments = (
        "persona_override_revision_id",
        "conversation_persona_assignments",
        "effective_after_message_id",
        "agent_default",
        "not exists",
        "create index",
        "conversation_id",
    )
    assert all(fragment in source for fragment in required_fragments)


def test_persona_override_migration_preserves_existing_lineage_identifiers() -> None:
    _migration()
    source = next(MIGRATION_DIR.glob("0005_*.py")).read_text().lower()
    assert "delete from conversations" not in source
    assert "delete from runs" not in source


def test_fresh_postgresql_install_creates_persona_override_history() -> None:
    url = _database_url("AURA_0023_MIGRATION_FRESH_DATABASE_URL")
    _upgrade(url, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_name = 'conversations' "
                    "AND column_name = 'persona_override_revision_id'"
                )
            ).scalar_one_or_none() == 1
            assert connection.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'conversation_persona_assignments'"
                )
            ).scalar_one_or_none() == 1
            indexes = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'conversation_persona_assignments'"
                )
            ).scalars().all()
            assert any("conversation_id" in indexdef for indexdef in indexes)
    finally:
        engine.dispose()


def test_upgrade_from_0004_backfills_default_persona_without_changing_conversation_id() -> None:
    url = _database_url("AURA_0023_MIGRATION_UPGRADE_DATABASE_URL")
    _upgrade(url, "0004_agent_persona_revisions")
    principal_id, conversation_id, message_id, run_id, assignment_id = (
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
        uuid4(),
    )
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            seeded = connection.execute(
                text(
                    "SELECT p.id AS profile_id, r.id AS revision_id, "
                    "r.persona_revision_id, r.model_policy_revision_id AS policy_id "
                    "FROM agent_profiles p JOIN agent_revisions r "
                    "ON r.id = p.current_revision_id "
                    "WHERE p.slug = 'general-assistant'"
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
                    "(id, principal_id, title, agent_profile_id, agent_revision_id, "
                    "model_id, version) VALUES (:id, :principal, 'Legacy', :profile, "
                    ":revision, 'chat', 1)"
                ),
                {
                    "id": conversation_id,
                    "principal": principal_id,
                    "profile": seeded.profile_id,
                    "revision": seeded.revision_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, conversation_id, role, content, state) "
                    "VALUES (:id, :conversation, 'user', 'legacy', 'complete')"
                ),
                {"id": message_id, "conversation": conversation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO runs "
                    "(id, conversation_id, user_message_id, agent_revision_id, "
                    "model_policy_revision_id, provider, model_id, status, attempt_count) "
                    "VALUES (:id, :conversation, :message, :revision, :policy, "
                    "'ollama', 'chat', 'completed', 0)"
                ),
                {
                    "id": run_id,
                    "conversation": conversation_id,
                    "message": message_id,
                    "revision": seeded.revision_id,
                    "policy": seeded.policy_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO conversation_agent_assignments "
                    "(id, conversation_id, agent_profile_id, agent_revision_id, "
                    "reason) VALUES (:id, :conversation, :profile, :revision, 'initial')"
                ),
                {
                    "id": assignment_id,
                    "conversation": conversation_id,
                    "profile": seeded.profile_id,
                    "revision": seeded.revision_id,
                },
            )
        _upgrade(url, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT id FROM conversations WHERE id = :id"),
                {"id": conversation_id},
            ).scalar_one() == conversation_id
            assert connection.execute(
                text("SELECT id FROM messages WHERE id = :id"),
                {"id": message_id},
            ).scalar_one() == message_id
            assert connection.execute(
                text("SELECT id FROM runs WHERE id = :id"),
                {"id": run_id},
            ).scalar_one() == run_id
            row = connection.execute(
                text(
                    "SELECT persona_revision_id, source, reason "
                    "FROM conversation_persona_assignments "
                    "WHERE conversation_id = :conversation"
                ),
                {"conversation": conversation_id},
            ).one()
            assert row.persona_revision_id == seeded.persona_revision_id
            assert (row.source, row.reason) == ("agent_default", "initial")
            indexes = connection.execute(
                text(
                    "SELECT indexdef FROM pg_indexes "
                    "WHERE tablename = 'conversation_persona_assignments'"
                )
            ).scalars().all()
            assert any("conversation_id" in indexdef for indexdef in indexes)
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_sql_persona_adapter_keeps_public_query_synchronized_after_disable() -> None:
    url = _isolated_database_url("AURA_0023_PERSONA_DATABASE_URL")
    engine = make_engine(url)
    sessions = session_factory(engine)
    try:
        async with engine.begin() as connection:
            await connection.run_sync(metadata().drop_all)
            await connection.run_sync(metadata().create_all)

        store = SqlPersonaStore(sessions)
        service = PersonaConfigurationService(store)
        created = await service.create_persona(
            "https://issuer",
            "owner",
            f"sql-independent-{uuid4().hex}",
            "SQL independent style",
            "A persisted independent style.",
            "Use the persisted independent style.",
            str(uuid4()),
        )
        revised = await service.revise_persona(
            "https://issuer",
            "owner",
            created.id,
            created.version,
            "SQL independent style",
            "A revised persisted independent style.",
            "Use the revised persisted independent style.",
            str(uuid4()),
        )
        selected = await service.require_active_revision(revised.current_revision.id)
        profiles = await store.list_personas()
        profile, query_revision = store.catalog.find_revision(selected.id)
        assert any(item.id == profile.id for item in profiles)
        assert query_revision.id == selected.id

        agents = AgentCatalog(store.catalog)
        compilation = agents.compilation(
            agents.list_agents()[0].current_revision.id,
            query_revision.id,
        )
        assert compilation.component_revision_ids[-1] == selected.id

        disabled = await service.set_status(
            "https://issuer",
            "owner",
            revised.id,
            revised.version,
            ConfigurationStatus.DISABLED,
            str(uuid4()),
        )
        assert disabled.status is ConfigurationStatus.DISABLED
        refreshed = await store.list_personas()
        refreshed_profile, refreshed_revision = store.catalog.find_revision(selected.id)
        assert any(item.id == refreshed_profile.id for item in refreshed)
        assert refreshed_profile.status is ConfigurationStatus.DISABLED
        assert refreshed_revision.id == selected.id
        with pytest.raises(ConfigurationDisabled):
            await service.require_active_revision(selected.id)
        historical = agents.compilation(
            agents.list_agents()[0].current_revision.id,
            selected.id,
        )
        assert historical.prompt_hash == compilation.prompt_hash

        async with sessions() as session:
            audit_rows = (
                await session.execute(select(AuditRow).order_by(AuditRow.occurred_at))
            ).scalars().all()
        actions = [row.action for row in audit_rows]
        assert actions[-3:] == ["persona.create", "persona.revise", "persona.status"]
        assert all(
            "instructions" not in str(row.metadata_).lower()
            and "description" not in str(row.metadata_).lower()
            for row in audit_rows[-3:]
        )
    finally:
        async with engine.begin() as connection:
            await connection.run_sync(metadata().drop_all)
        await engine.dispose()
