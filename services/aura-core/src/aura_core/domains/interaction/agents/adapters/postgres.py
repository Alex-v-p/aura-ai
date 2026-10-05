"""PostgreSQL adapter for explicit agent seed commands."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.interaction.agents.persistence import (
    AgentProfileRow,
    AgentRevisionRow,
    ModelPolicyRevisionRow,
)
from aura_core.domains.interaction.conversations.public import GENERAL_AGENT
from aura_core.runtime.models.capacity import DEFAULT_CONTEXT_TOKENS


class SqlAgentSeeder:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def seed(self) -> None:
        async with self.sessions() as session, session.begin():
            if await session.get(AgentProfileRow, GENERAL_AGENT.profile_id) is not None:
                return
            session.add(
                AgentProfileRow(
                    id=GENERAL_AGENT.profile_id,
                    slug="general-assistant",
                    display_name="Aura",
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
                AgentRevisionRow(
                    id=GENERAL_AGENT.revision_id,
                    agent_profile_id=GENERAL_AGENT.profile_id,
                    revision=1,
                    system_prompt=GENERAL_AGENT.system_prompt,
                    model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
                )
            )
