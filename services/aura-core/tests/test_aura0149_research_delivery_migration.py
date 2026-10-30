"""Regression coverage for the collision-safe 0033 built-in seed."""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from alembic import command
from alembic.migration import MigrationContext
from alembic.operations import Operations
from aura_core.entrypoints.cli import alembic_config
from aura_core.platform.auth import Settings
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import DBAPIError, OperationalError

MIGRATION = (
    Path(__file__).parents[1]
    / "migrations"
    / "versions"
    / "0033_research_completion_delivery.py"
)


def _migration() -> Any:
    spec = importlib.util.spec_from_file_location("aura_migration_0033_aura0149", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _run_upgrade(module: Any, connection: Connection) -> None:
    with Operations.context(MigrationContext.configure(connection)):
        module.upgrade()


def _database_unavailable(exc: BaseException) -> bool:
    message = str(getattr(exc, "orig", exc)).lower()
    return any(
        marker in message
        for marker in (
            "connection refused",
            "could not connect",
            "connection timed out",
            "timeout expired",
            "could not translate host name",
            "database system is starting",
            "database system is not yet accepting",
        )
    )


def _fresh_database() -> tuple[Any, Engine]:
    url = os.environ.get("AURA_0149_MIGRATION_DATABASE_URL")
    if not url:
        pytest.skip("AURA_0149_MIGRATION_DATABASE_URL is required for PostgreSQL coverage")
    module = _migration()
    try:
        command.upgrade(alembic_config(Settings(database_url=url)), "0032_governed_research")
    except (DBAPIError, OperationalError, ImportError) as exc:
        if _database_unavailable(exc):
            pytest.skip(f"PostgreSQL migration dependency unavailable: {type(exc).__name__}")
        raise
    return module, create_engine(url, pool_size=5, max_overflow=0)


def _document_source(connection: Connection, module: Any) -> dict[str, Any]:
    return dict(
        connection.execute(
            text(
                "SELECT display_name, system_prompt, purpose, instructions, "
                "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
                "memory_policy_revision_id, tool_policy_revision_id "
                "FROM agent_revisions WHERE id = :id"
            ),
            {"id": module._GENERAL_DOCUMENT_REVISION},
        ).mappings().one()
    )


def _insert_research_seed(
    connection: Connection,
    module: Any,
    *,
    grants: tuple[tuple[str, int], ...] = (),
    scopes: str = (
        '["memory:read","documents:read","research:run","reminders:read",'
        '"reminders:write"]'
    ),
    max_calls: int = 1,
    timeout_ms: int = 30_000,
    retry_limit: int = 0,
    revision_number: int = 99,
    include_revision: bool = True,
    owner_issuer: str = "",
    owner_subject: str = "",
) -> None:
    connection.execute(
        text(
            "INSERT INTO tool_policy_revisions "
            "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
            "max_calls_per_run, timeout_ms, retry_limit, permission_scopes) "
            "VALUES (:id, :issuer, :subject, :agent, :revision, :max_calls, :timeout_ms, "
            ":retry_limit, CAST(:scopes AS JSONB))"
        ),
        {
            "id": module._GENERAL_POLICY,
            "issuer": owner_issuer,
            "subject": owner_subject,
            "agent": module._GENERAL_AGENT,
            "revision": revision_number,
            "max_calls": max_calls,
            "timeout_ms": timeout_ms,
            "retry_limit": retry_limit,
            "scopes": scopes,
        },
    )
    for tool_id, tool_version in grants:
        connection.execute(
            text(
                "INSERT INTO tool_policy_grants (policy_id, tool_id, tool_version) "
                "VALUES (:policy, :tool, :version)"
            ),
            {"policy": module._GENERAL_POLICY, "tool": tool_id, "version": tool_version},
        )
    if include_revision:
        source = _document_source(connection, module)
        connection.execute(
            text(
                "INSERT INTO agent_revisions "
                "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                "instructions, persona_revision_id, prompt_bundle_revision_id, "
                "model_policy_revision_id, memory_policy_revision_id, tool_policy_revision_id) "
                "VALUES (:id, :agent, :revision, :display_name, :system_prompt, :purpose, "
                ":instructions, :persona_revision_id, :prompt_bundle_revision_id, "
                ":model_policy_revision_id, :memory_policy_revision_id, :policy)"
            ),
            {
                "id": module._GENERAL_REVISION,
                "agent": module._GENERAL_AGENT,
                "revision": revision_number,
                "policy": module._GENERAL_POLICY,
                **source,
            },
        )


def _authority_counts(connection: Connection, module: Any) -> tuple[int, int]:
    return (
        int(
            connection.execute(
                text("SELECT COUNT(*) FROM tool_policy_revisions WHERE id = :id"),
                {"id": module._GENERAL_POLICY},
            ).scalar_one()
        ),
        int(
            connection.execute(
                text("SELECT COUNT(*) FROM agent_revisions WHERE id = :id"),
                {"id": module._GENERAL_REVISION},
            ).scalar_one()
        ),
    )


def _execute(connection: Connection, statement: str, parameters: dict[str, Any]) -> None:
    connection.execute(text(statement), parameters)


def _expect_failure_without_authority(
    engine: Engine, module: Any, mutate: Callable[[Connection], None]
) -> None:
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            mutate(connection)
            authority_before = _authority_counts(connection, module)
            with pytest.raises(RuntimeError, match="0033 research seed refused unsafe state"):
                _run_upgrade(module, connection)
            assert _authority_counts(connection, module) == authority_before
        finally:
            transaction.rollback()


def _remove_general_profile(connection: Connection, module: Any) -> None:
    connection.execute(
        text("DELETE FROM agent_revisions WHERE agent_profile_id = :id"),
        {"id": module._GENERAL_AGENT},
    )
    connection.execute(
        text("DELETE FROM agent_profiles WHERE id = :id"),
        {"id": module._GENERAL_AGENT},
    )


def _clear_research_seed(
    connection: Connection,
    module: Any,
    *,
    other_owner_policy: Any | None = None,
    custom_profile: Any | None = None,
    custom_policy: Any | None = None,
    custom_revision: Any | None = None,
) -> None:
    connection.execute(
        text(
            "UPDATE agent_profiles SET current_revision_id = :document, version = version - 1 "
            "WHERE id = :agent AND current_revision_id = :research"
        ),
        {
            "document": module._GENERAL_DOCUMENT_REVISION,
            "agent": module._GENERAL_AGENT,
            "research": module._GENERAL_REVISION,
        },
    )
    connection.execute(
        text("DELETE FROM tool_policy_grants WHERE policy_id = :id"),
        {"id": module._GENERAL_POLICY},
    )
    connection.execute(
        text("DELETE FROM agent_revisions WHERE id = :id"),
        {"id": module._GENERAL_REVISION},
    )
    connection.execute(
        text("DELETE FROM tool_policy_revisions WHERE id = :id"),
        {"id": module._GENERAL_POLICY},
    )
    if custom_revision is not None:
        connection.execute(
            text("DELETE FROM agent_revisions WHERE id = :id"),
            {"id": custom_revision},
        )
    if custom_profile is not None:
        connection.execute(
            text("DELETE FROM agent_profiles WHERE id = :id"),
            {"id": custom_profile},
        )
    for policy_id in (other_owner_policy, custom_policy):
        if policy_id is None:
            continue
        connection.execute(
            text("DELETE FROM tool_policy_grants WHERE policy_id = :id"),
            {"id": policy_id},
        )
        connection.execute(
            text("DELETE FROM tool_policy_revisions WHERE id = :id"),
            {"id": policy_id},
        )


def test_migration_0033_fails_closed_for_missing_null_and_inconsistent_state() -> None:
    module, engine = _fresh_database()
    try:
        _expect_failure_without_authority(
            engine,
            module,
            lambda connection: _remove_general_profile(connection, module),
        )
        _expect_failure_without_authority(
            engine,
            module,
            lambda connection: _execute(
                connection,
                "UPDATE agent_profiles SET current_revision_id = NULL WHERE id = :id",
                {"id": module._GENERAL_AGENT},
            ),
        )
        _expect_failure_without_authority(
            engine,
            module,
            lambda connection: _insert_research_seed(connection, module, include_revision=False),
        )
        seed_cases: tuple[dict[str, Any], ...] = (
            {"grants": module._RESEARCH_GRANTS[:-1]},
            {
                "grants": module._RESEARCH_GRANTS,
                "scopes": '["memory:read","documents:read"]',
            },
            {"grants": module._RESEARCH_GRANTS, "timeout_ms": 1},
        )
        for seed_options in seed_cases:
            _expect_failure_without_authority(
                engine,
                module,
                lambda connection, options=seed_options: _insert_research_seed(
                    connection, module, **options
                ),
            )
        _expect_failure_without_authority(
            engine,
            module,
            lambda connection: _execute(
                connection,
                "UPDATE tool_policy_revisions SET principal_issuer = 'wrong-owner' WHERE id = :id",
                {"id": module._GENERAL_DOCUMENT_POLICY},
            ),
        )
    finally:
        with engine.begin() as connection:
            _clear_research_seed(connection, module)
        engine.dispose()


def test_migration_0033_preserves_history_allocates_by_owner_and_reenters() -> None:
    module, engine = _fresh_database()
    other_owner_policy = uuid4()
    custom_profile = uuid4()
    custom_policy = uuid4()
    custom_revision = uuid4()
    try:
        with engine.begin() as connection:
            profile = connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": module._GENERAL_AGENT},
            ).one()
            source = _document_source(connection, module)
            policy_occupied = connection.execute(
                text(
                    "SELECT id, agent_profile_id, revision, max_calls_per_run, timeout_ms, "
                    "retry_limit, permission_scopes FROM tool_policy_revisions "
                    "WHERE principal_issuer = '' AND principal_subject = '' "
                    "AND agent_profile_id = :agent AND revision = 5"
                ),
                {"agent": module._GENERAL_AGENT},
            ).one()
            agent_occupied = connection.execute(
                text(
                    "SELECT id, agent_profile_id, revision, tool_policy_revision_id "
                    "FROM agent_revisions WHERE agent_profile_id = :agent AND revision = 5"
                ),
                {"agent": module._GENERAL_AGENT},
            ).one()
            agent_next = connection.execute(
                text(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM agent_revisions "
                    "WHERE agent_profile_id = :agent"
                ),
                {"agent": module._GENERAL_AGENT},
            ).scalar_one()
            connection.execute(
                text(
                    "INSERT INTO tool_policy_revisions "
                    "(id, principal_issuer, principal_subject, agent_profile_id, revision, "
                    "max_calls_per_run, timeout_ms, retry_limit, permission_scopes) "
                    "VALUES (:other, 'other', 'owner', :agent, 900, 0, 30000, 0, '[]'::jsonb), "
                    "(:custom_policy, '', '', :custom_profile, 1, 0, 30000, 0, '[]'::jsonb)"
                ),
                {
                    "other": other_owner_policy,
                    "agent": module._GENERAL_AGENT,
                    "custom_policy": custom_policy,
                    "custom_profile": custom_profile,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO agent_profiles "
                    "(id, slug, display_name, status, version, current_revision_id) "
                    "VALUES (:id, :slug, 'Custom', 'active', 1, :revision)"
                ),
                {
                    "id": custom_profile,
                    "slug": f"aura0149-custom-{custom_profile}",
                    "revision": custom_revision,
                },
            )
            connection.execute(
                text(
                    "INSERT INTO agent_revisions "
                    "(id, agent_profile_id, revision, display_name, system_prompt, purpose, "
                    "instructions, "
                    "persona_revision_id, prompt_bundle_revision_id, model_policy_revision_id, "
                    "memory_policy_revision_id, tool_policy_revision_id) "
                    "VALUES (:id, :profile, 1, :display_name, :system_prompt, :purpose, "
                    ":instructions, "
                    ":persona_revision_id, :prompt_bundle_revision_id, :model_policy_revision_id, "
                    ":memory_policy_revision_id, :policy)"
                ),
                {
                    "id": custom_revision,
                    "profile": custom_profile,
                    "policy": custom_policy,
                    **source,
                },
            )
            custom_before = connection.execute(
                text(
                    "SELECT agent_profile_id, revision, tool_policy_revision_id "
                    "FROM agent_revisions WHERE id = :id"
                ),
                {"id": custom_revision},
            ).one()

        with engine.begin() as connection:
            _run_upgrade(module, connection)
            assert connection.execute(
                text("SELECT revision FROM tool_policy_revisions WHERE id = :id"),
                {"id": module._GENERAL_POLICY},
            ).scalar_one() == int(policy_occupied.revision) + 1
            assert connection.execute(
                text("SELECT revision FROM agent_revisions WHERE id = :id"),
                {"id": module._GENERAL_REVISION},
            ).scalar_one() == agent_next
            assert connection.execute(
                text(
                    "SELECT tool_id, tool_version FROM tool_policy_grants "
                    "WHERE policy_id = :id ORDER BY tool_id"
                ),
                {"id": module._GENERAL_POLICY},
            ).all() == sorted((*module._DOCUMENT_GRANTS, ("web.research", 1)))
            assert connection.execute(
                text(
                    "SELECT id, agent_profile_id, revision, max_calls_per_run, timeout_ms, "
                    "retry_limit, permission_scopes FROM tool_policy_revisions "
                    "WHERE principal_issuer = '' AND principal_subject = '' "
                    "AND agent_profile_id = :agent AND revision = 5"
                ),
                {"agent": module._GENERAL_AGENT},
            ).one() == policy_occupied
            assert connection.execute(
                text(
                    "SELECT id, agent_profile_id, revision, tool_policy_revision_id "
                    "FROM agent_revisions WHERE agent_profile_id = :agent AND revision = 5"
                ),
                {"agent": module._GENERAL_AGENT},
            ).one() == agent_occupied
            assert connection.execute(
                text(
                    "SELECT agent_profile_id, revision, tool_policy_revision_id "
                    "FROM agent_revisions WHERE id = :id"
                ),
                {"id": custom_revision},
            ).one() == custom_before
            assert connection.execute(
                text(
                    "SELECT COUNT(*) FROM tool_policy_grants WHERE policy_id = :id "
                    "AND tool_id = 'web.research'"
                ),
                {"id": custom_policy},
            ).scalar_one() == 0
            assert connection.execute(
                text("SELECT current_revision_id, version FROM agent_profiles WHERE id = :id"),
                {"id": module._GENERAL_AGENT},
            ).one() == (module._GENERAL_REVISION, profile.version + 1)
            counts = {
                table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("tool_policy_revisions", "tool_policy_grants", "agent_revisions")
            }
            _run_upgrade(module, connection)
            assert {
                table: connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
                for table in ("tool_policy_revisions", "tool_policy_grants", "agent_revisions")
            } == counts
    finally:
        with engine.begin() as connection:
            _clear_research_seed(
                connection,
                module,
                other_owner_policy=other_owner_policy,
                custom_profile=custom_profile,
                custom_policy=custom_policy,
                custom_revision=custom_revision,
            )
        engine.dispose()


def test_migration_0033_concurrent_reentry_keeps_one_successor() -> None:
    module, engine = _fresh_database()
    with engine.connect() as connection:
        version_before = connection.execute(
            text("SELECT version FROM agent_profiles WHERE id = :id"),
            {"id": module._GENERAL_AGENT},
        ).scalar_one()

    def run_once() -> None:
        with engine.begin() as connection:
            module._seed_research_delivery(connection)

    def run_index(_: int) -> None:
        run_once()

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            list(executor.map(run_index, (0, 1)))
        with engine.connect() as connection:
            assert connection.execute(
                text("SELECT COUNT(*) FROM agent_revisions WHERE id = :id"),
                {"id": module._GENERAL_REVISION},
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT COUNT(*) FROM tool_policy_revisions WHERE id = :id"),
                {"id": module._GENERAL_POLICY},
            ).scalar_one() == 1
            assert connection.execute(
                text("SELECT version FROM agent_profiles WHERE id = :id"),
                {"id": module._GENERAL_AGENT},
            ).scalar_one() == version_before + 1
    finally:
        with engine.begin() as connection:
            _clear_research_seed(connection, module)
        engine.dispose()


def test_migration_0033_declares_collision_safe_owner_scoped_seed() -> None:
    source = MIGRATION.read_text(encoding="utf-8")
    assert "pg_advisory_xact_lock" in source
    assert "FOR UPDATE" in source
    assert "COALESCE(MAX(revision), 0) + 1" in source
    assert "principal_issuer = '' AND principal_subject = ''" in source
    assert "general-assistant-revision-4" in source
    assert "general-assistant-tool-policy-4" in source
    assert "current_revision_id = :source" in source
    assert "ON CONFLICT (id) DO UPDATE" not in source
