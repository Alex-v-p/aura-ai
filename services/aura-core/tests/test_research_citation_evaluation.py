"""Task-scoped evidence and citation evaluations for governed research."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from aura_core.domains.knowledge.research.public import (
    ResearchCitation,
    ResearchContentState,
    ResearchEvidence,
    ResearchMode,
    ResearchOutcome,
    ResearchStage,
    ResearchValidationError,
)
from test_research_lifecycle_evaluation import OWNER, _providers, _service

NOW = datetime(2026, 10, 30, tzinfo=UTC)


def _evidence(task_id, *, suffix: str = "one") -> ResearchEvidence:
    source_id = uuid4()
    return ResearchEvidence(
        source_id=source_id,
        task_id=task_id,
        label=f"Synthetic {suffix}",
        url=f"https://example.org/{suffix}",
        publisher="Example",
        retrieved_at=NOW,
        media_type="text/html",
        extraction_method="static",
        freshness_at=None,
        excerpts=("bounded supporting excerpt",),
        content_sha256="b" * 64,
    )


@pytest.mark.asyncio
async def test_citations_must_resolve_to_current_task_evidence() -> None:
    service, _ = _service(_providers())
    first = await service.admit(*OWNER, question="first", mode=ResearchMode.QUICK)
    second = await service.admit(*OWNER, question="second", mode=ResearchMode.QUICK)
    first_task = await service.get(*OWNER, first.research_id)
    second_task = await service.get(*OWNER, second.research_id)
    first_source = _evidence(first_task.id)

    completed = await service.settle(
        first_task,
        ResearchOutcome.COMPLETED,
        evidence=(first_source,),
        citations=(first_source.citation(),),
    )
    assert completed.citations[0].source_id == first_source.source_id

    foreign = ResearchCitation(
        first_source.source_id,
        first_source.label,
        first_source.url,
        first_source.retrieved_at,
    )
    with pytest.raises(ResearchValidationError):
        await service.settle(
            second_task,
            ResearchOutcome.COMPLETED,
            citations=(foreign,),
        )


@pytest.mark.asyncio
async def test_invented_and_stale_citations_fail_closed() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="citation", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    invented = ResearchCitation(uuid4(), "invented", "https://example.org/invented", NOW)

    with pytest.raises(ResearchValidationError):
        await service.settle(task, ResearchOutcome.COMPLETED, citations=(invented,))

    evidence = _evidence(task.id)
    settled = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        evidence=(evidence,),
        citations=(evidence.citation(),),
    )
    assert settled.content_state is ResearchContentState.ACTIVE
    assert settled.stage is ResearchStage.TERMINAL


@pytest.mark.asyncio
async def test_public_citation_serialization_excludes_private_evidence_fields() -> None:
    service, _ = _service(_providers())
    admission = await service.admit(*OWNER, question="privacy", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    evidence = _evidence(task.id)
    settled = await service.settle(
        task,
        ResearchOutcome.COMPLETED,
        evidence=(evidence,),
        citations=(evidence.citation(),),
    )

    public = settled.citations[0].as_dict()
    assert set(public) == {"kind", "sourceId", "label", "url", "retrievedAt", "locator"}
    assert "excerpts" not in public
    assert "contentSha256" not in public
    assert "taskId" not in public
