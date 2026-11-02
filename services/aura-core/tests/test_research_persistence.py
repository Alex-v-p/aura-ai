# ruff: noqa: E501

from __future__ import annotations

import pytest
from aura_core.domains.knowledge.research.public import (
    MemoryResearchRepository,
    ResearchApplicationService,
    ResearchMode,
)


class _Providers:
    admission_enabled = True


@pytest.mark.asyncio
async def test_memory_research_repository_replays_idempotent_admission() -> None:
    service = ResearchApplicationService(MemoryResearchRepository(), provider_clients=_Providers())
    first = await service.admit("issuer", "subject", question="same", mode=ResearchMode.QUICK, idempotency_key="one")
    replay = await service.admit("issuer", "subject", question="same", mode=ResearchMode.QUICK, idempotency_key="one")
    assert replay.research_id == first.research_id
