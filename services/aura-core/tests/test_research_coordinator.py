# ruff: noqa: E501

from __future__ import annotations

import pytest
from aura_core.domains.knowledge.research.public import (
    MemoryResearchRepository,
    ResearchApplicationService,
    ResearchCoordinator,
    ResearchMode,
    ResearchOutcome,
)


class _Providers:
    admission_enabled = True
    settings = type("Settings", (), {"search_enabled": False, "static_fetch_enabled": False, "browser_fetch_enabled": False})()


@pytest.mark.asyncio
async def test_disabled_provider_settles_without_outbound_dispatch() -> None:
    service = ResearchApplicationService(MemoryResearchRepository(), provider_clients=_Providers())
    admission = await service.admit("issuer", "subject", question="bounded", mode=ResearchMode.QUICK)
    result = await ResearchCoordinator(service, _Providers()).run_once(admission.research_id)
    assert result.outcome is ResearchOutcome.FAILED
    assert result.failure_class == "provider_disabled"
