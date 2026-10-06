"""Public, provider-neutral agent/persona and prompt application boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4, uuid5

from aura_core.domains.interaction.personas.public import (
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    PersonaCatalog,
    PersonaConfigurationService,
    PersonaRevisionQueryPort,
)
from aura_core.runtime.prompting.public import (
    PromptBundleRevision,
    PromptCompilation,
    PromptCompiler,
    PromptComponentRevision,
    PromptMetricsPort,
)

NAMESPACE = UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
GENERAL_PROFILE_ID = uuid5(NAMESPACE, "general-assistant")
GENERAL_REVISION_ID = uuid5(NAMESPACE, "general-assistant-revision-1")
GENERAL_POLICY_ID = uuid5(NAMESPACE, "ollama-model-policy-1")
NEUTRAL_PERSONA_ID = uuid5(NAMESPACE, "neutral-persona")
NEUTRAL_PERSONA_REVISION_ID = uuid5(NAMESPACE, "neutral-persona-revision-1")
PLATFORM_COMPONENT_ID = uuid5(NAMESPACE, "prompt-platform-1")
GOVERNANCE_COMPONENT_ID = uuid5(NAMESPACE, "prompt-governance-1")
PROMPT_BUNDLE_ID = uuid5(NAMESPACE, "prompt-bundle-1")


@dataclass(frozen=True, slots=True)
class AgentRevision:
    id: UUID
    profile_id: UUID
    revision: int
    display_name: str
    purpose: str
    instructions: str
    persona_revision_id: UUID
    prompt_bundle_revision_id: UUID
    model_policy_revision_id: UUID
    system_prompt: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass(slots=True)
class AgentProfile:
    id: UUID
    slug: str
    display_name: str
    status: ConfigurationStatus = ConfigurationStatus.ACTIVE
    version: int = 1
    current_revision_id: UUID | None = None
    revisions: list[AgentRevision] = field(default_factory=lambda: list[AgentRevision]())
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def current_revision(self) -> AgentRevision:
        if self.current_revision_id is None or not self.revisions:
            raise LookupError("agent has no revision")
        return next(item for item in self.revisions if item.id == self.current_revision_id)


class AgentConfigurationRepository(Protocol):
    """Persistence and runtime query seam for agent configuration."""

    async def list_agents(self) -> list[AgentProfile]: ...
    async def get_agent(self, identifier: UUID) -> AgentProfile: ...
    async def create_agent(
        self,
        issuer: str,
        subject: str,
        slug: str,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        key: str,
    ) -> AgentProfile: ...
    async def revise_agent(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        key: str,
    ) -> AgentProfile: ...
    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> AgentProfile: ...
    async def require_active_revision_in_transaction(
        self, session: Any, identifier: UUID
    ) -> AgentRevision: ...
    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision: ...
    def resolve_revision(self, identifier: UUID) -> AgentRevision: ...
    def compile_prompt(
        self,
        revision_id: UUID,
        *,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation: ...
    def compilation(self, revision_id: UUID) -> PromptCompilation: ...


def _canonical_fingerprint(operation: str, payload: dict[str, object]) -> str:
    canonical = json.dumps(
        {"operation": operation, "payload": payload},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class AgentCatalog:
    """Deterministic in-memory configuration service behind inward ports."""

    def __init__(self, persona_query: PersonaRevisionQueryPort | None = None) -> None:
        self._idempotency: dict[str, tuple[str, object]] = {}
        self.persona_query = persona_query or PersonaCatalog()
        platform = PromptComponentRevision(PLATFORM_COMPONENT_ID, "platform", 1, "")
        governance = PromptComponentRevision(GOVERNANCE_COMPONENT_ID, "governance", 1, "")
        self.bundle = PromptBundleRevision(PROMPT_BUNDLE_ID, 1, platform, governance)
        self.bundles: dict[UUID, PromptBundleRevision] = {self.bundle.id: self.bundle}
        revision = AgentRevision(
            GENERAL_REVISION_ID,
            GENERAL_PROFILE_ID,
            1,
            "Aura",
            "A helpful local-first household assistant.",
            "You are Aura, a helpful local-first household assistant.",
            NEUTRAL_PERSONA_REVISION_ID,
            PROMPT_BUNDLE_ID,
            GENERAL_POLICY_ID,
            "You are Aura, a helpful local-first household assistant.",
        )
        self.agents: dict[UUID, AgentProfile] = {
            GENERAL_PROFILE_ID: AgentProfile(
                GENERAL_PROFILE_ID,
                "general-assistant",
                "Aura",
                current_revision_id=revision.id,
                revisions=[revision],
            )
        }

    def list_agents(self) -> list[AgentProfile]:
        return list(self.agents.values())

    def get_agent(self, identifier: UUID) -> AgentProfile:
        try:
            return self.agents[identifier]
        except KeyError as exc:
            raise ConfigurationNotFound("agent not found") from exc

    def resolve_revision(self, identifier: UUID) -> AgentRevision:
        for profile in self.agents.values():
            for revision in reversed(profile.revisions):
                if revision.id == identifier:
                    if profile.status != ConfigurationStatus.ACTIVE:
                        raise ConfigurationDisabled("agent is disabled")
                    return revision
        raise ConfigurationNotFound("agent revision not found")

    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision:
        for profile in self.agents.values():
            for revision in reversed(profile.revisions):
                if revision.id == identifier:
                    return revision
        raise ConfigurationNotFound("agent revision not found")

    def create_agent(
        self,
        slug: str,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID = NEUTRAL_PERSONA_REVISION_ID,
        model_policy_revision_id: UUID = GENERAL_POLICY_ID,
        idempotency_key: str | None = None,
    ) -> AgentProfile:
        fingerprint = _canonical_fingerprint(
            "agent",
            {
                "slug": slug,
                "displayName": display_name,
                "purpose": purpose,
                "instructions": instructions,
                "personaRevisionId": str(persona_revision_id),
            },
        )
        if idempotency_key is not None and idempotency_key in self._idempotency:
            prior_fingerprint, prior = self._idempotency[idempotency_key]
            if prior_fingerprint != fingerprint or not isinstance(prior, AgentProfile):
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return prior
        persona = self.persona_query.find_revision(persona_revision_id)[0]
        if persona.status != ConfigurationStatus.ACTIVE:
            raise ConfigurationDisabled("persona is disabled")
        profile_id = uuid4()
        revision = self._new_agent_revision(
            profile_id,
            1,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            model_policy_revision_id,
        )
        profile = AgentProfile(
            profile_id, slug, display_name, current_revision_id=revision.id, revisions=[revision]
        )
        self.agents[profile_id] = profile
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = (fingerprint, profile)
        return profile

    def revise_agent(
        self,
        profile_id: UUID,
        expected_version: int,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        idempotency_key: str | None = None,
        display_name: str | None = None,
    ) -> AgentRevision:
        profile = self.get_agent(profile_id)
        fingerprint = _canonical_fingerprint(
            "agent-revision",
            {
                "profileId": str(profile_id),
                "expectedVersion": expected_version,
                "displayName": display_name or profile.display_name,
                "purpose": purpose,
                "instructions": instructions,
                "personaRevisionId": str(persona_revision_id),
            },
        )
        if idempotency_key is not None and idempotency_key in self._idempotency:
            prior_fingerprint, prior = self._idempotency[idempotency_key]
            if prior_fingerprint != fingerprint or not isinstance(prior, AgentRevision):
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return prior
        self._check_version(profile.version, expected_version)
        persona = self.persona_query.find_revision(persona_revision_id)[0]
        if persona.status != ConfigurationStatus.ACTIVE:
            raise ConfigurationDisabled("persona is disabled")
        revision = self._new_agent_revision(
            profile.id,
            len(profile.revisions) + 1,
            profile.display_name,
            purpose,
            instructions,
            persona_revision_id,
            profile.current_revision.model_policy_revision_id,
            display_name or profile.display_name,
        )
        profile.revisions.append(revision)
        profile.current_revision_id = revision.id
        profile.version += 1
        profile.updated_at = datetime.now(UTC)
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = (fingerprint, revision)
        return revision

    def set_agent_status(
        self,
        profile_id: UUID,
        expected_version: int,
        status: ConfigurationStatus,
        idempotency_key: str | None = None,
    ) -> AgentProfile:
        profile = self.get_agent(profile_id)
        fingerprint = _canonical_fingerprint(
            "agent-status",
            {
                "profileId": str(profile_id),
                "expectedVersion": expected_version,
                "status": status.value,
            },
        )
        if idempotency_key is not None and idempotency_key in self._idempotency:
            prior_fingerprint, prior = self._idempotency[idempotency_key]
            if prior_fingerprint != fingerprint or not isinstance(prior, AgentProfile):
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return prior
        self._check_version(profile.version, expected_version)
        profile.status = status
        profile.version += 1
        profile.updated_at = datetime.now(UTC)
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = (fingerprint, profile)
        return profile

    def compile_prompt(
        self,
        revision_id: UUID,
        *,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation:
        """Compile one immutable agent revision with its exact provenance."""

        revision = self.resolve_revision_unchecked(revision_id)
        _, persona = self.persona_query.find_revision(revision.persona_revision_id)
        try:
            bundle = self.bundles[revision.prompt_bundle_revision_id]
        except KeyError as exc:
            raise ConfigurationNotFound("prompt bundle revision not found") from exc
        return PromptCompiler(
            bundle,
            metrics=metrics,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            run_id=run_id,
            conversation_id=conversation_id,
        ).compile(revision, persona)

    def compilation(self, revision_id: UUID) -> PromptCompilation:
        """Compatibility query without telemetry side effects."""

        return self.compile_prompt(revision_id)

    def replace_bundles(self, bundles: dict[UUID, PromptBundleRevision]) -> None:
        self.bundles = dict(bundles)
        if self.bundles:
            self.bundle = self.bundles.get(PROMPT_BUNDLE_ID, next(iter(self.bundles.values())))

    def _new_agent_revision(
        self,
        profile_id: UUID,
        number: int,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        policy_id: UUID,
        revision_display_name: str | None = None,
    ) -> AgentRevision:
        return AgentRevision(
            uuid4(),
            profile_id,
            number,
            revision_display_name or display_name,
            purpose,
            instructions,
            persona_revision_id,
            PROMPT_BUNDLE_ID,
            policy_id,
            instructions or purpose,
        )

    @staticmethod
    def _check_version(actual: int, expected: int) -> None:
        if actual != expected:
            raise ConfigurationVersionConflict("configuration version conflict")


class AgentMemoryRepository:
    """Async persistence adapter for the deterministic in-memory catalog."""

    def __init__(self, catalog: AgentCatalog) -> None:
        self.catalog = catalog

    async def list_agents(self) -> list[AgentProfile]:
        return self.catalog.list_agents()

    async def get_agent(self, identifier: UUID) -> AgentProfile:
        return self.catalog.get_agent(identifier)

    async def create_agent(
        self,
        issuer: str,
        subject: str,
        slug: str,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        key: str,
    ) -> AgentProfile:
        del issuer, subject
        return self.catalog.create_agent(
            slug,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            idempotency_key=key,
        )

    async def revise_agent(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        key: str,
    ) -> AgentProfile:
        del issuer, subject
        self.catalog.revise_agent(
            identifier,
            expected,
            purpose,
            instructions,
            persona_revision_id,
            idempotency_key=key,
            display_name=display_name,
        )
        return self.catalog.get_agent(identifier)

    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> AgentProfile:
        del issuer, subject
        return self.catalog.set_agent_status(identifier, expected, status, key)

    async def require_active_revision_in_transaction(
        self, session: object, identifier: UUID
    ) -> AgentRevision:
        del session
        return self.catalog.resolve_revision(identifier)

    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision:
        return self.catalog.resolve_revision_unchecked(identifier)

    def resolve_revision(self, identifier: UUID) -> AgentRevision:
        return self.catalog.resolve_revision(identifier)

    def compilation(self, revision_id: UUID) -> PromptCompilation:
        return self.catalog.compilation(revision_id)

    def compile_prompt(
        self,
        revision_id: UUID,
        *,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation:
        return self.catalog.compile_prompt(
            revision_id,
            metrics=metrics,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            run_id=run_id,
            conversation_id=conversation_id,
        )


class AgentConfigurationService:
    """Public agent application service over an inward repository port."""

    def __init__(
        self,
        repository: AgentConfigurationRepository,
        personas: PersonaConfigurationService,
        audit: object | None = None,
    ) -> None:
        self.repository = repository
        self.personas = personas
        self.audit = audit

    async def list_agents(self) -> list[AgentProfile]:
        return await self.repository.list_agents()

    async def get_agent(self, identifier: UUID) -> AgentProfile:
        return await self.repository.get_agent(identifier)

    async def create_agent(
        self,
        issuer: str,
        subject: str,
        slug: str,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        key: str,
    ) -> AgentProfile:
        result = await self.repository.create_agent(
            issuer,
            subject,
            slug,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            key,
        )
        await self._audit("agent.create", issuer, subject, result)
        return result

    async def revise_agent(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        display_name: str,
        purpose: str,
        instructions: str,
        persona_revision_id: UUID,
        key: str,
    ) -> AgentProfile:
        result = await self.repository.revise_agent(
            issuer,
            subject,
            identifier,
            expected,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            key,
        )
        await self._audit("agent.revise", issuer, subject, result)
        return result

    async def set_status(
        self,
        issuer: str,
        subject: str,
        identifier: UUID,
        expected: int,
        status: ConfigurationStatus,
        key: str,
    ) -> AgentProfile:
        result = await self.repository.set_status(
            issuer,
            subject,
            identifier,
            expected,
            status,
            key,
        )
        await self._audit("agent.status", issuer, subject, result)
        return result

    async def _audit(self, action: str, issuer: str, subject: str, result: object) -> None:
        import inspect

        if self.audit is None or not isinstance(result, AgentProfile):
            return
        callback = self.audit
        if callable(callback):
            value = callback(
                action,
                "ok",
                issuer=issuer,
                subject=subject,
                metadata={"agentId": str(result.id), "revision": result.current_revision.revision},
            )
            if inspect.isawaitable(value):
                await value

    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision:
        return self.repository.resolve_revision_unchecked(identifier)

    def resolve_active_revision(self, identifier: UUID) -> AgentRevision:
        return self.repository.resolve_revision(identifier)

    async def require_active_revision_in_transaction(
        self, session: object, identifier: UUID
    ) -> AgentRevision:
        return await self.repository.require_active_revision_in_transaction(session, identifier)

    def compile_prompt(
        self,
        revision_id: UUID,
        *,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation:
        return self.repository.compile_prompt(
            revision_id,
            metrics=metrics,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            run_id=run_id,
            conversation_id=conversation_id,
        )

    def compilation(self, revision_id: UUID) -> PromptCompilation:
        return self.compile_prompt(revision_id)


__all__ = [
    "AgentCatalog",
    "AgentConfigurationRepository",
    "AgentConfigurationService",
    "AgentMemoryRepository",
    "AgentProfile",
    "AgentRevision",
    "ConfigurationDisabled",
    "ConfigurationIdempotencyConflict",
    "ConfigurationNotFound",
    "ConfigurationStatus",
    "ConfigurationVersionConflict",
    "GENERAL_POLICY_ID",
    "GENERAL_PROFILE_ID",
    "GENERAL_REVISION_ID",
    "NEUTRAL_PERSONA_ID",
    "NEUTRAL_PERSONA_REVISION_ID",
    "PromptBundleRevision",
    "PromptCompilation",
    "PromptCompiler",
    "PromptComponentRevision",
]
