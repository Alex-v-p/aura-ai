# ruff: noqa: E501

"""Composition root for the Core research application boundary."""

from __future__ import annotations

from typing import Any, cast

from aura_core.domains.artifacts.generated.public import GeneratedArtifactService
from aura_core.domains.execution.tasks.transport import TaskSourceInvalidationStage
from aura_core.domains.knowledge.research.public import (
    MemoryResearchRepository,
    ResearchApplicationService,
    ResearchCoordinator,
    ResearchRepository,
)
from aura_core.domains.knowledge.research.repository import SqlResearchRepository
from aura_core.platform.research_provider_settings import ResearchProviderSettings


def research_service(
    sessions: Any = None,
    *,
    provider_clients: object | None = None,
    provider_settings: ResearchProviderSettings | None = None,
    artifact_service: GeneratedArtifactService | None = None,
    metrics: Any = None,
    testing: bool = False,
    repository: ResearchRepository | None = None,
    admission_enabled: bool | None = None,
) -> ResearchApplicationService:
    if repository is None:
        repository = MemoryResearchRepository() if testing or sessions is None else SqlResearchRepository(sessions, invalidation_stage=TaskSourceInvalidationStage())
    if admission_enabled is None:
        try:
            admission_enabled = ResearchProviderSettings.from_environment().admission_enabled
        except (OSError, ValueError):
            admission_enabled = False
    if provider_settings is None:
        try:
            provider_settings = ResearchProviderSettings.from_environment()
        except (OSError, ValueError):
            provider_settings = ResearchProviderSettings()
    return ResearchApplicationService(cast(ResearchRepository, repository), provider_clients=provider_clients, provider_settings=provider_settings, artifact_service=artifact_service, admission_enabled=admission_enabled, metrics=metrics)


def research_coordinator(
    service: ResearchApplicationService,
    *,
    provider_clients: object | None = None,
    artifact_service: GeneratedArtifactService | None = None,
    metrics: Any = None,
) -> ResearchCoordinator:
    return ResearchCoordinator(service, provider_clients, artifacts=artifact_service, metrics=metrics)


__all__ = ["research_coordinator", "research_service"]
