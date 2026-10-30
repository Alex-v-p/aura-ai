from __future__ import annotations

from uuid import uuid4

import pytest
from aura_core.entrypoints.worker.research import run_research_once


class _Coordinator:
    async def run_once(self, research_id, *, owner=None):
        return research_id, owner


@pytest.mark.asyncio
async def test_worker_dispatches_only_the_identifier_and_owner() -> None:
    identifier = uuid4()
    result = await run_research_once(_Coordinator(), identifier, owner=("issuer", "subject"))
    assert result == (identifier, ("issuer", "subject"))
