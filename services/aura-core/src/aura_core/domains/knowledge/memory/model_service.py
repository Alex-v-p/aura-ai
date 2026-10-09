"""Model selection and scoped memory reindex application service."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import replace
from typing import cast
from uuid import UUID

from aura_core.domains.knowledge.memory.contracts import (
    MemoryEmbeddingGeneration,
    MemoryFilters,
    MemoryModelConfiguration,
    MemoryModelConfigurationSnapshot,
    MemoryModelDescriptor,
    MemoryNotFound,
    MemoryReindexSnapshot,
    MemoryValidationError,
    MemoryVersionConflict,
)
from aura_core.domains.knowledge.memory.contracts import (
    fingerprint as _fingerprint,
)
from aura_core.domains.knowledge.memory.repository_ports import MemoryRepository
from aura_core.runtime.models.ports import ModelSelectionPort, ProviderTraceContext


class MemoryModelApplicationService:
    """Application boundary for verified model selection and scoped reindexing."""

    def __init__(
        self, repository: MemoryRepository, provider: ModelSelectionPort, worker: object | None
    ) -> None:
        self.repository = repository
        self.provider = provider
        self.worker = worker

    async def inventory(self) -> tuple[MemoryModelDescriptor, ...]:
        return tuple(self._project(item) for item in await self.provider.list_models())

    async def save_configuration(
        self,
        issuer: str,
        subject: str,
        *,
        extraction_model_id: str,
        embedding_model_id: str,
        expected_version: int,
        idempotency_key: str,
        trace: ProviderTraceContext | None = None,
    ) -> MemoryModelConfiguration:
        models = await self._fresh_inventory(trace=trace)
        extraction = self._candidate(models, extraction_model_id, "structured_output")
        if extraction is not None:
            extraction = await self._verify_selected(extraction, "structured_output", trace=trace)
        embedding = self._candidate(models, embedding_model_id, "embedding")
        if embedding is not None and embedding.dimension is None:
            embedding = await self._verify_selected(embedding, "embedding", trace=trace)
        if extraction is None or embedding is None:
            raise MemoryValidationError("selected memory model is unavailable")
        if extraction.model_digest is None or embedding.model_digest is None:
            raise MemoryValidationError("provider model identity is unavailable")
        if extraction.model_revision is None or embedding.model_revision is None:
            raise MemoryValidationError("provider model revision is unavailable")
        if embedding.dimension is None or embedding.dimension < 1:
            raise MemoryValidationError("provider embedding dimension is unavailable")
        configuration = MemoryModelConfiguration(
            issuer,
            subject,
            extraction.id,
            embedding.id,
            extraction.model_revision,
            embedding.model_revision,
            version=expected_version,
        )
        return await self.repository.save_model_configuration(
            issuer,
            subject,
            configuration,
            expected_version=expected_version,
            idempotency_key=idempotency_key,
            dimension=embedding.dimension,
            model_digest=embedding.model_digest,
        )

    async def _fresh_inventory(
        self, *, trace: ProviderTraceContext | None = None
    ) -> tuple[MemoryModelDescriptor, ...]:
        return tuple(
            self._project(item) for item in await self.provider.refresh_models(context=trace)
        )

    async def _verify_selected(
        self,
        model: MemoryModelDescriptor,
        capability: str,
        *,
        trace: ProviderTraceContext | None = None,
    ) -> MemoryModelDescriptor | None:
        try:
            verified = await self.provider.verify_model(model.id, capability, context=trace)
        except Exception:
            return None
        if verified is None:
            return None
        normalized = self._normalize(verified)
        if (
            capability not in normalized.capabilities
            or normalized.id != model.id
            or normalized.provider != model.provider
            or normalized.model_digest != model.model_digest
            or normalized.model_revision != model.model_revision
        ):
            return None
        return normalized

    @classmethod
    def _project(cls, item: object) -> MemoryModelDescriptor:
        normalized = cls._normalize(item)
        capabilities = set(normalized.capabilities)
        if {"chat", "completion"} & capabilities:
            capabilities.add("structured_output")
        if capabilities == set(normalized.capabilities):
            return normalized
        return replace(normalized, capabilities=tuple(sorted(capabilities)))

    @staticmethod
    def _candidate(
        models: Sequence[MemoryModelDescriptor], model_id: str, capability: str
    ) -> MemoryModelDescriptor | None:
        return next(
            (
                item
                for item in models
                if item.id == model_id
                and item.availability == "available"
                and item.model_digest is not None
                and item.model_revision is not None
                and capability in item.capabilities
            ),
            None,
        )

    async def status(self, issuer: str, subject: str) -> MemoryReindexSnapshot:
        generations = await self._generations(issuer, subject)
        try:
            configuration = await self.repository.get_model_configuration(issuer, subject)
        except MemoryNotFound:
            configuration = None
        active = next(
            (
                item
                for item in generations
                if configuration is not None and item.id == configuration.embedding_generation
            ),
            None,
        )
        replacement = next((item for item in generations if item.status == "building"), None)
        records = await self.repository.list_memories(
            issuer,
            subject,
            MemoryFilters(
                scope_type=None, include_all_scopes=True, include_historical=True, limit=100000
            ),
        )
        total = sum(len(item.revisions) for item in records)
        processed = (
            sum(
                sum(
                    any(
                        embedding.revision_id == revision.id
                        and embedding.generation_id == replacement.id
                        for embedding in item.embeddings
                    )
                    for revision in item.revisions
                )
                for item in records
            )
            if replacement is not None
            else 0
        )
        return MemoryReindexSnapshot(active, replacement, processed, total)

    async def configuration_snapshot(
        self, issuer: str, subject: str
    ) -> MemoryModelConfigurationSnapshot:
        configuration = await self.repository.get_model_configuration(issuer, subject)
        models = await self.inventory()
        generations = await self._generations(issuer, subject)
        extraction = next(
            (item for item in models if item.id == configuration.extraction_model_id), None
        )
        embedding = next(
            (item for item in models if item.id == configuration.embedding_model_id), None
        )
        active = next(
            (
                item
                for item in generations
                if item.id == configuration.embedding_generation and item.status == "active"
            ),
            None,
        )
        building = next((item for item in generations if item.status == "building"), None)
        return MemoryModelConfigurationSnapshot(
            configuration, extraction, embedding, active, building
        )

    async def resume(
        self,
        issuer: str,
        subject: str,
        generation_id: UUID,
        idempotency_key: str,
    ) -> None:
        generation = await self.repository.get_embedding_generation(issuer, subject, generation_id)
        if generation.status != "building":
            raise MemoryVersionConflict("embedding generation is not resumable")
        fingerprint = _fingerprint("reindex.resume", {"generationId": str(generation_id)})
        reserved = await self.repository.reserve_reindex_command(
            issuer, subject, generation_id, idempotency_key, fingerprint
        )
        if not reserved:
            return
        method = getattr(self.worker, "resume_reindex", None)
        if not callable(method):
            raise MemoryValidationError("memory reindex worker is unavailable")
        try:
            await cast(Callable[..., Awaitable[object]], method)(issuer, subject, generation_id)
            await self.repository.complete_reindex_command(
                issuer, subject, generation_id, idempotency_key, fingerprint
            )
        except Exception:
            # Keep the durable pending receipt.  A repeated identical command
            # must resume after a worker crash rather than losing the work
            # reservation between dispatch and settlement.
            raise

    async def _generations(self, issuer: str, subject: str) -> list[MemoryEmbeddingGeneration]:
        return await self.repository.list_embedding_generations(issuer, subject)

    @staticmethod
    def _normalize(item: object) -> MemoryModelDescriptor:
        model_id = str(getattr(item, "id", ""))
        capabilities = tuple(str(value) for value in getattr(item, "capabilities", ()))
        digest_value = getattr(item, "model_digest", None)
        candidate_digest = (
            str(digest_value).removeprefix("sha256:").lower() if digest_value else None
        )
        digest = (
            candidate_digest
            if candidate_digest is not None and re.fullmatch(r"[0-9a-f]{64}", candidate_digest)
            else None
        )
        dimension_value = getattr(item, "dimension", None)
        dimension = int(dimension_value) if isinstance(dimension_value, int) else None
        revision_value = getattr(item, "model_revision", None)
        revision = (
            revision_value
            if isinstance(revision_value, str)
            and 1 <= len(revision_value) <= 255
            and not any(ord(char) < 32 or ord(char) == 127 for char in revision_value)
            else None
        )
        identifier_valid = 1 <= len(model_id) <= 255 and not any(
            ord(char) < 32 or ord(char) == 127 for char in model_id
        )
        identity_available = identifier_valid and digest is not None and revision is not None
        pending_embedding_verification = (
            identity_available and "embedding" in capabilities and dimension is None
        )
        missing = (
            not identifier_valid
            or digest is None
            or revision is None
            or ("embedding" in capabilities and dimension is None)
        )
        return MemoryModelDescriptor(
            model_id,
            str(getattr(item, "display_name", model_id)),
            str(getattr(item, "provider", "")),
            capabilities,
            revision,
            digest,
            dimension,
            str(getattr(item, "availability", "available")),
            (bool(getattr(item, "selectable", True)) and not missing)
            or pending_embedding_verification,
            (
                "provider identity unavailable"
                if not identity_available
                else getattr(item, "disabled_reason", None)
                if not missing
                else None
            ),
        )

    @staticmethod
    def _require(
        models: Sequence[MemoryModelDescriptor], model_id: str, capability: str
    ) -> MemoryModelDescriptor:
        selected = next(
            (
                item
                for item in models
                if item.id == model_id
                and item.availability == "available"
                and item.selectable
                and capability in item.capabilities
            ),
            None,
        )
        if selected is None:
            raise MemoryValidationError("selected memory model is unavailable")
        return selected
