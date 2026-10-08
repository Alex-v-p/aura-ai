"""Focused authorization tests for legacy blank-principal memory policies."""

from uuid import uuid4

from aura_core.domains.interaction.agents.adapters.sql_store import _policy_owner_matches
from aura_core.domains.interaction.agents.persistence import MemoryPolicyRevisionRow
from aura_core.domains.interaction.agents.public import (
    GENERAL_MEMORY_POLICY_ID,
    GENERAL_PROFILE_ID,
    platform_memory_policy_id,
)


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


def test_arbitrary_blank_principal_policy_remains_unauthorized() -> None:
    row = _blank_policy(uuid4(), uuid4())

    assert not _policy_owner_matches(row, "issuer", "owner")
