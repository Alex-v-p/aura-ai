"""Focused SQL policy authorization and revision-allocation tests."""

from __future__ import annotations

import os
from uuid import uuid4, uuid5

import pytest
from aura_core.domains.interaction.agents.adapters.sql_store import (
    SqlAgentStore,
    _is_deterministic_platform_policy,
    _policy_owner_matches,
)
from aura_core.domains.interaction.agents.persistence import MemoryPolicyRevisionRow
from aura_core.domains.interaction.agents.public import (
    GENERAL_MEMORY_POLICY_ID,
    GENERAL_PROFILE_ID,
    NAMESPACE,
    AgentCatalog,
    MemoryPolicy,
    platform_memory_policy_id,
)
from aura_core.platform.database.engine import make_engine, session_factory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError


def _blank_policy(policy_id: object, agent_profile_id: object) -> MemoryPolicyRevisionRow:
    return MemoryPolicyRevisionRow(
        id=policy_id,
        principal_issuer="",
        principal_subject="",
        agent_profile_id=agent_profile_id,
        revision=1,
        shared_user_read=True,
        current_agent_read=True,
        fallback_relevance_threshold=0.5,
        max_memories=8,
        context_budget_fraction=0.2,
        allow_shared_user_promotion=False,
    )


def test_seeded_general_platform_policy_is_available_to_authenticated_owner() -> None:
    row = _blank_policy(GENERAL_MEMORY_POLICY_ID, GENERAL_PROFILE_ID)

    assert _policy_owner_matches(row, "issuer", "owner")


def test_deterministic_per_agent_migrated_policy_is_available() -> None:
    agent_profile_id = uuid4()
    row = _blank_policy(platform_memory_policy_id(agent_profile_id), agent_profile_id)

    assert _policy_owner_matches(row, "issuer", "owner")


def test_deterministic_recall_mode_successor_is_available() -> None:
    agent_profile_id = uuid4()
    row = _blank_policy(
        uuid5(NAMESPACE, f"agent-memory-recall-policy:{agent_profile_id}:2"),
        agent_profile_id,
    )

    assert _policy_owner_matches(row, "issuer", "owner")


def test_arbitrary_blank_principal_policy_remains_unauthorized() -> None:
    row = _blank_policy(uuid4(), uuid4())

    assert not _policy_owner_matches(row, "issuer", "owner")


def test_revision_allocation_whitelist_includes_only_deterministic_blank_policies() -> None:
    agent_profile_id = uuid4()

    assert _is_deterministic_platform_policy(GENERAL_MEMORY_POLICY_ID, GENERAL_PROFILE_ID)
    assert not _is_deterministic_platform_policy(GENERAL_MEMORY_POLICY_ID, agent_profile_id)
    assert _is_deterministic_platform_policy(
        platform_memory_policy_id(agent_profile_id), agent_profile_id
    )
    assert _is_deterministic_platform_policy(
        uuid5(NAMESPACE, f"agent-memory-recall-policy:{agent_profile_id}:2"),
        agent_profile_id,
    )
    assert not _is_deterministic_platform_policy(uuid4(), agent_profile_id)


def test_recall_mode_columns_have_conservative_sql_defaults() -> None:
    table = MemoryPolicyRevisionRow.__table__
    mode = table.c.recall_mode
    threshold = table.c.automatic_recall_threshold

    assert mode.nullable is False
    assert threshold.nullable is False
    assert str(mode.server_default.arg) == "automatic"
    assert str(threshold.server_default.arg) == "0.7"


def test_new_custom_policy_fixture_can_be_scoped_to_off_mode() -> None:
    row = MemoryPolicyRevisionRow(
        id=uuid4(),
        principal_issuer="issuer",
        principal_subject="owner",
        agent_profile_id=uuid4(),
        revision=1,
        recall_mode="off",
        automatic_recall_threshold=0.7,
        max_memories=2,
        context_budget_fraction=0.05,
    )

    assert row.recall_mode == "off"
    assert row.automatic_recall_threshold == 0.7
    assert row.max_memories == 2
    assert row.context_budget_fraction == 0.05


@pytest.mark.asyncio
async def test_sql_policy_revision_allocation_counts_deterministic_blank_seed() -> None:
    """A seeded blank policy must make the owner's first save revision two."""

    database_url = os.environ.get("AURA_TEST_DATABASE_URL")
    if not database_url or os.environ.get("AURA_TEST_DEPENDENCIES_ISOLATED") != "1":
        pytest.skip(
            "set isolated AURA_TEST_DATABASE_URL for SQL policy allocation coverage"
        )
    profile_id = uuid4()
    legacy_policy_id = platform_memory_policy_id(profile_id)
    created_policy_id = uuid4()
    try:
        engine = make_engine(database_url)
    except ModuleNotFoundError as exc:
        pytest.skip(f"PostgreSQL async driver unavailable: {exc.name}")
    sessions = session_factory(engine)
    inserted = False
    try:
        try:
            async with sessions() as session, session.begin():
                session.add(
                    MemoryPolicyRevisionRow(
                        id=legacy_policy_id,
                        principal_issuer="",
                        principal_subject="",
                        agent_profile_id=profile_id,
                        revision=1,
                        max_memories=2,
                        context_budget_fraction=0.05,
                        recall_mode="off",
                        automatic_recall_threshold=0.7,
                    )
                )
            inserted = True
            store = SqlAgentStore(sessions, AgentCatalog())
            created = await store.create_memory_policy(
                "https://issuer.example",
                "owner",
                MemoryPolicy(created_policy_id, profile_id, 2),
            )
            assert created.id == created_policy_id
            assert created.revision == 2
        except (DBAPIError, OperationalError, ImportError) as exc:
            pytest.skip(f"PostgreSQL policy schema unavailable: {type(exc).__name__}")
    finally:
        if inserted:
            async with sessions() as session, session.begin():
                await session.execute(
                    text(
                        "DELETE FROM memory_policy_revisions "
                        "WHERE id IN (:legacy, :created)"
                    ),
                    {"legacy": legacy_policy_id, "created": created_policy_id},
                )
        await engine.dispose()
