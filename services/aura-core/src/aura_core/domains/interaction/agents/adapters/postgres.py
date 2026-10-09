"""PostgreSQL adapter for seeded and durable agent configuration."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.interaction.agents.persistence import (
    AgentProfileRow,
    AgentRevisionRow,
    MemoryPolicyRevisionRow,
    ModelPolicyRevisionRow,
    PromptBundleRevisionRow,
    PromptComponentRevisionRow,
)
from aura_core.domains.interaction.agents.public import (
    GENERAL_MEMORY_POLICY_ID,
    GOVERNANCE_COMPONENT_ID,
    NEUTRAL_PERSONA_REVISION_ID,
    PLATFORM_COMPONENT_ID,
    PROMPT_BUNDLE_ID,
)
from aura_core.domains.interaction.conversations.public import GENERAL_AGENT
from aura_core.runtime.models.capacity import DEFAULT_CONTEXT_TOKENS


class SqlAgentSeeder:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def seed(self) -> None:
        async with self.sessions() as session, session.begin():
            if await session.get(PromptComponentRevisionRow, PLATFORM_COMPONENT_ID) is None:
                session.add(
                    PromptComponentRevisionRow(
                        id=PLATFORM_COMPONENT_ID, component="platform", revision=1, content=""
                    )
                )
                session.add(
                    PromptComponentRevisionRow(
                        id=GOVERNANCE_COMPONENT_ID, component="governance", revision=1, content=""
                    )
                )
                session.add(
                    PromptBundleRevisionRow(
                        id=PROMPT_BUNDLE_ID,
                        revision=1,
                        platform_component_revision_id=PLATFORM_COMPONENT_ID,
                        governance_component_revision_id=GOVERNANCE_COMPONENT_ID,
                    )
                )
            if await session.get(AgentProfileRow, GENERAL_AGENT.profile_id) is not None:
                return
            session.add(
                AgentProfileRow(
                    id=GENERAL_AGENT.profile_id,
                    slug="general-assistant",
                    display_name="Aura",
                    current_revision_id=GENERAL_AGENT.revision_id,
                )
            )
            session.add(
                ModelPolicyRevisionRow(
                    id=GENERAL_AGENT.policy_revision_id,
                    revision=1,
                    provider="ollama",
                    policy={"fallback": False, "contextTokens": DEFAULT_CONTEXT_TOKENS},
                )
            )
            session.add(
                MemoryPolicyRevisionRow(
                    id=GENERAL_MEMORY_POLICY_ID,
                    principal_issuer="",
                    principal_subject="",
                    agent_profile_id=GENERAL_AGENT.profile_id,
                    revision=1,
                    shared_user_read=True,
                    current_agent_read=True,
                    fallback_relevance_threshold=0.5,
                    max_memories=2,
                    context_budget_fraction=0.05,
                    # Aura's built-in profile is the only seeded agent that
                    # may automatically promote well-grounded personal facts
                    # into shared-user memory.  Custom agents receive their
                    # own conservative policy in the SQL store.
                    allow_shared_user_promotion=True,
                    recall_mode="automatic",
                    automatic_recall_threshold=0.7,
                )
            )
            session.add(
                AgentRevisionRow(
                    id=GENERAL_AGENT.revision_id,
                    agent_profile_id=GENERAL_AGENT.profile_id,
                    revision=1,
                    display_name="Aura",
                    system_prompt=GENERAL_AGENT.system_prompt,
                    purpose="A helpful local-first household assistant.",
                    instructions=GENERAL_AGENT.system_prompt,
                    persona_revision_id=NEUTRAL_PERSONA_REVISION_ID,
                    prompt_bundle_revision_id=PROMPT_BUNDLE_ID,
                    model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
                    memory_policy_revision_id=GENERAL_MEMORY_POLICY_ID,
                )
            )
