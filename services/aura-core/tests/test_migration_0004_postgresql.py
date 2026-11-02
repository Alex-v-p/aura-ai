"""Executable PostgreSQL coverage for the 0004 migration paths.

The operator supplies disposable database URLs when running integration tests.
Keeping these tests opt-in avoids silently replacing real local data while still
making the migration path executable in CI and release validation.
"""

import os
from uuid import uuid4

import pytest
from alembic import command
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from sqlalchemy import create_engine, text


def _database_url(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"{name} is required for PostgreSQL migration coverage")
    return value


def _upgrade(url: str, revision: str) -> None:
    command.upgrade(
        alembic_config(Settings(database_url=url)),
        revision,
    )


def test_fresh_postgresql_install_reaches_head_and_seeds_deterministically() -> None:
    url = _database_url("AURA_MIGRATION_FRESH_DATABASE_URL")
    _upgrade(url, "head")
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            first = connection.execute(
                text(
                    "SELECT id, current_revision_id FROM agent_profiles "
                    "WHERE slug = 'general-assistant'"
                )
            ).one()
            persona = connection.execute(
                text(
                    "SELECT id, current_revision_id FROM persona_profiles "
                    "WHERE slug = 'neutral'"
                )
            ).one()
            second = connection.execute(
                text(
                    "SELECT id, current_revision_id FROM agent_profiles "
                    "WHERE slug = 'general-assistant'"
                )
            ).one()
            assert first == second
            assert first.id
            assert first.current_revision_id
            assert persona.id
            assert persona.current_revision_id
            assert connection.execute(
                text("SELECT COUNT(*) FROM prompt_bundle_revisions")
            ).scalar_one() == 1
    finally:
        engine.dispose()


def test_existing_0003_rows_are_backfilled_without_changing_historical_ids() -> None:
    url = _database_url("AURA_MIGRATION_UPGRADE_DATABASE_URL")
    _upgrade(url, "0003_idempotency_issuer")
    principal_id, profile_id, revision_id = uuid4(), uuid4(), uuid4()
    conversation_id, message_id, run_id = uuid4(), uuid4(), uuid4()
    policy_id = uuid4()
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO principals (id, issuer, subject, display_name) "
                    "VALUES (:id, 'https://issuer', 'owner', 'Owner')"
                ),
                {"id": principal_id},
            )
            connection.execute(
                text(
                    "INSERT INTO model_policy_revisions (id, revision, provider, policy) "
                    "VALUES (:id, 1, 'ollama', CAST(:policy AS JSONB))"
                ),
                {"id": policy_id, "policy": '{"fallback": false}'},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_profiles (id, slug, display_name) "
                    "VALUES (:id, 'legacy', 'Legacy')"
                ),
                {"id": profile_id},
            )
            connection.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, "
                    "purpose, instructions, model_policy_revision_id) "
                    "VALUES (:id, :profile, 1, 'Legacy', 'Legacy prompt', "
                    "'Legacy purpose', 'Legacy instructions', :policy)"
                ),
                {"id": revision_id, "profile": profile_id, "policy": policy_id},
            )
            connection.execute(
                text(
                    "INSERT INTO conversations "
                    "(id, principal_id, title, agent_profile_id, agent_revision_id, "
                    "model_id, version) "
                    "VALUES (:id, :principal, 'Legacy', :profile, :revision, 'chat', 1)"
                ),
                {
                    "id": conversation_id,
                    "principal": principal_id,
                    "profile": profile_id,
                    "revision": revision_id,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO messages "
                    "(id, conversation_id, role, content, state) "
                    "VALUES (:id, :conversation, 'user', 'hello', 'complete')"
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
                    "revision": revision_id,
                    "policy": policy_id,
                },
            )
        _upgrade(url, "head")
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT id FROM conversations WHERE id = :id"), {"id": conversation_id}
            ).scalar_one() == conversation_id
            assert connection.execute(
                text("SELECT id FROM runs WHERE id = :id"), {"id": run_id}
            ).scalar_one() == run_id
            assignment = connection.execute(
                text(
                    "SELECT conversation_id, agent_revision_id, reason, created_at "
                    "FROM conversation_agent_assignments WHERE conversation_id = :id"
                ),
                {"id": conversation_id},
            ).one()
            assert assignment.conversation_id == conversation_id
            assert assignment.agent_revision_id == revision_id
            assert assignment.reason == "initial"
            assert assignment.created_at is not None
            provenance = connection.execute(
                text(
                    "SELECT persona_revision_id, prompt_bundle_revision_id, prompt_hash "
                    "FROM runs WHERE id = :id"
                ),
                {"id": run_id},
            ).one()
            assert provenance == (None, None, None)
    finally:
        engine.dispose()
