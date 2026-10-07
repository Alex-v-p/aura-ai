"""Persona-owned PostgreSQL seed operations."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.interaction.personas.persistence import PersonaProfileRow, PersonaRevisionRow
from aura_core.domains.interaction.personas.public import (
    NEUTRAL_PERSONA_ID,
    NEUTRAL_PERSONA_REVISION_ID,
)


class SqlPersonaSeeder:
    """Insert the deterministic built-in persona when it is absent."""

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions

    async def seed(self) -> None:
        async with self.sessions() as session, session.begin():
            if await session.get(PersonaProfileRow, NEUTRAL_PERSONA_ID) is not None:
                return
            session.add(
                PersonaProfileRow(
                    id=NEUTRAL_PERSONA_ID,
                    slug="neutral",
                    display_name="Neutral",
                    current_revision_id=NEUTRAL_PERSONA_REVISION_ID,
                )
            )
            session.add(
                PersonaRevisionRow(
                    id=NEUTRAL_PERSONA_REVISION_ID,
                    persona_profile_id=NEUTRAL_PERSONA_ID,
                    revision=1,
                    display_name="Neutral",
                    description="A neutral, clear conversational style.",
                    instructions="Use a clear, calm and direct conversational style.",
                )
            )
