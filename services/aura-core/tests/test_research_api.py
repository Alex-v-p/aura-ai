# ruff: noqa: E501

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from aura_core.domains.knowledge.research.public import (
    ResearchBudget,
    ResearchContentState,
    ResearchMode,
    ResearchOutcome,
    ResearchStage,
    ResearchTask,
)
from aura_core.entrypoints.api.routes.research import _service_payload


def test_research_api_tombstone_excludes_private_content() -> None:
    now = datetime.now(UTC)
    task = ResearchTask(uuid4(), "issuer", "subject", "private", ResearchMode.QUICK, ResearchStage.TERMINAL, ResearchOutcome.COMPLETED, ResearchContentState.DELETED, 2, now, now, now, ResearchBudget(90, 6, 2, 1, 4))
    payload = _service_payload(task, detail=True)
    assert payload["tombstoneLabel"] == "Research source deleted"
    assert "question" not in payload
