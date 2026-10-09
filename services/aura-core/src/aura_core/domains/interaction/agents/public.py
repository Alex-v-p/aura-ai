"""Public, provider-neutral agent/persona and prompt application boundary."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, cast
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
GENERAL_MEMORY_POLICY_ID = uuid5(NAMESPACE, "general-assistant-memory-policy-1")
NEUTRAL_PERSONA_ID = uuid5(NAMESPACE, "neutral-persona")
NEUTRAL_PERSONA_REVISION_ID = uuid5(NAMESPACE, "neutral-persona-revision-1")
PLATFORM_COMPONENT_ID = uuid5(NAMESPACE, "prompt-platform-1")
GOVERNANCE_COMPONENT_ID = uuid5(NAMESPACE, "prompt-governance-1")
PROMPT_BUNDLE_ID = uuid5(NAMESPACE, "prompt-bundle-1")


def platform_memory_policy_id(agent_profile_id: UUID) -> UUID:
    """Return the deterministic blank-principal policy seeded for a legacy agent.

    Legacy agent rows predate owner association, so migration 0010 gives each
    profile an isolated platform-default policy.  Callers must still verify
    that the returned policy is pinned by the requested agent revision; this
    helper only prevents treating arbitrary blank-principal rows as defaults.
    """

    return uuid5(NAMESPACE, f"agent-memory-policy:{agent_profile_id}:1")


def is_platform_memory_policy(policy_id: UUID, agent_profile_id: UUID) -> bool:
    """Check the narrow, deterministic legacy policy identity."""

    return policy_id == platform_memory_policy_id(agent_profile_id)


class MemoryRecallMode(StrEnum):
    """How an immutable agent memory policy admits conversation context."""

    OFF = "off"
    AUTOMATIC = "automatic"


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
    # Every immutable agent revision pins the memory policy it was admitted
    # with.  The default keeps old callers source-compatible while all
    # catalog-created revisions use the deterministic seeded policy.
    memory_policy_revision_id: UUID = GENERAL_MEMORY_POLICY_ID


@dataclass(frozen=True, slots=True)
class MemoryPolicy:
    """Immutable, owner-scoped memory access policy revision."""

    id: UUID
    agent_profile_id: UUID
    revision: int
    shared_user_read: bool = True
    current_agent_read: bool = True
    fallback_relevance_threshold: float = 0.5
    max_memories: int = 8
    context_budget_fraction: float = 0.2
    allow_shared_user_promotion: bool = False
    fallback_agent_profile_ids: tuple[UUID, ...] = ()
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Recall is intentionally explicit and versioned with the immutable
    # policy.  Keeping these fields at the end preserves source compatibility
    # for older positional policy constructors and persisted revisions.
    recall_mode: MemoryRecallMode = MemoryRecallMode.AUTOMATIC
    automatic_recall_threshold: float = 0.70

    def __post_init__(self) -> None:
        if not 0 <= self.fallback_relevance_threshold <= 1:
            raise ValueError("fallback relevance threshold must be between zero and one")
        if not 1 <= self.max_memories <= 8:
            raise ValueError("memory policy maximum must be between one and eight")
        if not 0 < self.context_budget_fraction <= 0.2:
            raise ValueError("memory context fraction must be between zero and 20 percent")
        if self.agent_profile_id in self.fallback_agent_profile_ids:
            raise ValueError("an agent cannot grant itself as a fallback")
        if len(set(self.fallback_agent_profile_ids)) != len(self.fallback_agent_profile_ids):
            raise ValueError("fallback grants must be unique")
        raw_recall_mode = cast(object, self.recall_mode)
        if raw_recall_mode is None:
            object.__setattr__(self, "recall_mode", MemoryRecallMode.AUTOMATIC)
        elif not isinstance(raw_recall_mode, MemoryRecallMode):
            try:
                object.__setattr__(self, "recall_mode", MemoryRecallMode(raw_recall_mode))
            except ValueError as exc:
                raise ValueError("memory recall mode must be off or automatic") from exc
        if not 0 <= self.automatic_recall_threshold <= 1:
            raise ValueError("automatic recall threshold must be between zero and one")


# More explicit aliases make the public boundary easy to discover without
# duplicating policy semantics across the memory and agent domains.
AgentMemoryPolicy = MemoryPolicy
MemoryPolicyRevision = MemoryPolicy


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
        memory_policy_revision_id: UUID = GENERAL_MEMORY_POLICY_ID,
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
        memory_policy_revision_id: UUID | None = None,
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
    async def get_memory_policy(
        self, issuer: str, subject: str, policy_id: UUID
    ) -> MemoryPolicy: ...
    async def create_memory_policy(
        self, issuer: str, subject: str, policy: MemoryPolicy, key: str | None = None
    ) -> MemoryPolicy: ...
    async def list_memory_policies(
        self, issuer: str, subject: str, agent_profile_id: UUID
    ) -> list[MemoryPolicy]: ...
    async def attach_memory_policy(
        self,
        issuer: str,
        subject: str,
        agent_profile_id: UUID,
        policy_revision_id: UUID,
        expected_version: int,
        key: str,
    ) -> AgentProfile: ...
    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision: ...
    def resolve_revision(self, identifier: UUID) -> AgentRevision: ...
    def compile_prompt(
        self,
        revision_id: UUID,
        *,
        persona_revision_id: UUID | None = None,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation: ...
    def compilation(
        self, revision_id: UUID, persona_revision_id: UUID | None = None
    ) -> PromptCompilation: ...


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
        self.memory_policies: dict[UUID, MemoryPolicy] = {}
        self.policy_idempotency: dict[str, tuple[str, MemoryPolicy]] = {}
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
            memory_policy_revision_id=GENERAL_MEMORY_POLICY_ID,
        )
        self.memory_policies[GENERAL_MEMORY_POLICY_ID] = MemoryPolicy(
            GENERAL_MEMORY_POLICY_ID,
            GENERAL_PROFILE_ID,
            1,
            max_memories=2,
            context_budget_fraction=0.05,
            recall_mode=MemoryRecallMode.AUTOMATIC,
            automatic_recall_threshold=0.70,
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
        memory_policy_revision_id: UUID = GENERAL_MEMORY_POLICY_ID,
    ) -> AgentProfile:
        fingerprint = _canonical_fingerprint(
            "agent",
            {
                "slug": slug,
                "displayName": display_name,
                "purpose": purpose,
                "instructions": instructions,
                "personaRevisionId": str(persona_revision_id),
                "memoryPolicyRevisionId": str(memory_policy_revision_id),
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
        if memory_policy_revision_id == GENERAL_MEMORY_POLICY_ID:
            memory_policy_revision_id = uuid5(NAMESPACE, f"agent-memory-policy:{profile_id}:1")
            self.memory_policies[memory_policy_revision_id] = MemoryPolicy(
                memory_policy_revision_id,
                profile_id,
                1,
                max_memories=2,
                context_budget_fraction=0.05,
                recall_mode=MemoryRecallMode.OFF,
                automatic_recall_threshold=0.70,
            )
        policy = self.memory_policies.get(memory_policy_revision_id)
        if policy is None or policy.agent_profile_id != profile_id:
            raise ConfigurationNotFound("memory policy revision not found")
        revision = self._new_agent_revision(
            profile_id,
            1,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            model_policy_revision_id,
            memory_policy_revision_id=memory_policy_revision_id,
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
        memory_policy_revision_id: UUID | None = None,
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
                "memoryPolicyRevisionId": str(
                    memory_policy_revision_id or profile.current_revision.memory_policy_revision_id
                ),
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
        chosen_memory_policy = (
            memory_policy_revision_id or profile.current_revision.memory_policy_revision_id
        )
        policy = self.memory_policies.get(chosen_memory_policy)
        if policy is None or policy.agent_profile_id != profile_id:
            raise ConfigurationNotFound("memory policy revision not found")
        revision = self._new_agent_revision(
            profile.id,
            len(profile.revisions) + 1,
            profile.display_name,
            purpose,
            instructions,
            persona_revision_id,
            profile.current_revision.model_policy_revision_id,
            display_name or profile.display_name,
            chosen_memory_policy,
        )
        profile.revisions.append(revision)
        profile.current_revision_id = revision.id
        profile.version += 1
        profile.updated_at = datetime.now(UTC)
        if idempotency_key is not None:
            self._idempotency[idempotency_key] = (fingerprint, revision)
        return revision

    def get_memory_policy(self, policy_id: UUID) -> MemoryPolicy:
        try:
            return self.memory_policies[policy_id]
        except KeyError as exc:
            raise ConfigurationNotFound("memory policy revision not found") from exc

    def create_memory_policy(
        self,
        agent_profile_id: UUID,
        *,
        shared_user_read: bool = True,
        current_agent_read: bool = True,
        fallback_relevance_threshold: float = 0.5,
        max_memories: int = 8,
        context_budget_fraction: float = 0.2,
        allow_shared_user_promotion: bool = False,
        fallback_agent_profile_ids: tuple[UUID, ...] = (),
        recall_mode: MemoryRecallMode = MemoryRecallMode.AUTOMATIC,
        automatic_recall_threshold: float = 0.70,
        idempotency_key: str | None = None,
    ) -> MemoryPolicy:
        recall_mode = MemoryRecallMode(recall_mode)
        fingerprint = _canonical_fingerprint(
            "memory-policy",
            {
                "agentProfileId": str(agent_profile_id),
                "sharedUserRead": shared_user_read,
                "currentAgentRead": current_agent_read,
                "sharedUserPromotion": allow_shared_user_promotion,
                "fallbackRelevanceThreshold": fallback_relevance_threshold,
                "maxMemories": max_memories,
                "contextBudgetFraction": context_budget_fraction,
                "fallbackAgentProfileIds": [str(item) for item in fallback_agent_profile_ids],
                "recallMode": recall_mode.value,
                "automaticRecallThreshold": automatic_recall_threshold,
            },
        )
        if idempotency_key is not None and idempotency_key in self.policy_idempotency:
            prior_fingerprint, prior = self.policy_idempotency[idempotency_key]
            if prior_fingerprint != fingerprint:
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return prior
        if agent_profile_id not in self.agents:
            raise ConfigurationNotFound("agent not found")
        prior = [
            item
            for item in self.memory_policies.values()
            if item.agent_profile_id == agent_profile_id
        ]
        if any(item not in self.agents for item in fallback_agent_profile_ids):
            raise ConfigurationNotFound("fallback agent not found")
        # Preserve every immutable policy revision.  Collapsing by agent with
        # a dict comprehension would let a newer revision hide an older edge
        # and make a cross-revision cycle appear acyclic.
        edges: dict[UUID, set[UUID]] = {}
        for item in self.memory_policies.values():
            edges.setdefault(item.agent_profile_id, set()).update(item.fallback_agent_profile_ids)
        edges.setdefault(agent_profile_id, set()).update(fallback_agent_profile_ids)
        pending = list(fallback_agent_profile_ids)
        visited: set[UUID] = set()
        while pending:
            current = pending.pop()
            if current == agent_profile_id:
                raise ValueError("fallback grants cannot form a cycle")
            if current in visited:
                continue
            visited.add(current)
            pending.extend(edges.get(current, ()))
        number = max((item.revision for item in prior), default=0) + 1
        policy = MemoryPolicy(
            uuid4(),
            agent_profile_id,
            number,
            shared_user_read,
            current_agent_read,
            fallback_relevance_threshold,
            max_memories,
            context_budget_fraction,
            allow_shared_user_promotion,
            tuple(fallback_agent_profile_ids),
            recall_mode=recall_mode,
            automatic_recall_threshold=automatic_recall_threshold,
        )
        self.memory_policies[policy.id] = policy
        if idempotency_key is not None:
            self.policy_idempotency[idempotency_key] = (fingerprint, policy)
        return policy

    def list_memory_policies(self, agent_profile_id: UUID) -> list[MemoryPolicy]:
        return sorted(
            (
                item
                for item in self.memory_policies.values()
                if item.agent_profile_id == agent_profile_id
            ),
            key=lambda item: item.revision,
        )

    def attach_memory_policy(
        self,
        agent_profile_id: UUID,
        policy_revision_id: UUID,
        expected_version: int,
        key: str | None = None,
    ) -> AgentProfile:
        profile = self.get_agent(agent_profile_id)
        policy = self.get_memory_policy(policy_revision_id)
        if policy.agent_profile_id != agent_profile_id:
            raise ConfigurationNotFound("memory policy revision not found")
        self.revise_agent(
            agent_profile_id,
            expected_version,
            profile.current_revision.purpose,
            profile.current_revision.instructions,
            profile.current_revision.persona_revision_id,
            idempotency_key=key,
            display_name=profile.display_name,
            memory_policy_revision_id=policy_revision_id,
        )
        return self.get_agent(agent_profile_id)

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
        persona_revision_id: UUID | None = None,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation:
        """Compile one immutable agent revision with its exact provenance."""

        revision = self.resolve_revision_unchecked(revision_id)
        _, persona = self.persona_query.find_revision(
            persona_revision_id or revision.persona_revision_id
        )
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
            allow_persona_override=persona_revision_id is not None,
        ).compile(revision, persona)

    def compilation(
        self, revision_id: UUID, persona_revision_id: UUID | None = None
    ) -> PromptCompilation:
        """Compatibility query without telemetry side effects."""

        return self.compile_prompt(revision_id, persona_revision_id=persona_revision_id)

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
        memory_policy_revision_id: UUID = GENERAL_MEMORY_POLICY_ID,
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
            memory_policy_revision_id=memory_policy_revision_id,
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
        memory_policy_revision_id: UUID = GENERAL_MEMORY_POLICY_ID,
    ) -> AgentProfile:
        del issuer, subject
        return self.catalog.create_agent(
            slug,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            idempotency_key=key,
            memory_policy_revision_id=memory_policy_revision_id,
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
        memory_policy_revision_id: UUID | None = None,
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
            memory_policy_revision_id=memory_policy_revision_id,
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

    async def get_memory_policy(self, issuer: str, subject: str, policy_id: UUID) -> MemoryPolicy:
        del issuer, subject
        return self.catalog.get_memory_policy(policy_id)

    async def create_memory_policy(
        self, issuer: str, subject: str, policy: MemoryPolicy, key: str | None = None
    ) -> MemoryPolicy:
        del issuer, subject
        fingerprint = _canonical_fingerprint(
            "memory-policy",
            {
                "agentProfileId": str(policy.agent_profile_id),
                "sharedUserRead": policy.shared_user_read,
                "currentAgentRead": policy.current_agent_read,
                "sharedUserPromotion": policy.allow_shared_user_promotion,
                "fallbackRelevanceThreshold": policy.fallback_relevance_threshold,
                "maxMemories": policy.max_memories,
                "contextBudgetFraction": policy.context_budget_fraction,
                "fallbackAgentProfileIds": [
                    str(item) for item in policy.fallback_agent_profile_ids
                ],
                "recallMode": policy.recall_mode.value,
                "automaticRecallThreshold": policy.automatic_recall_threshold,
            },
        )
        if key is not None and key in self.catalog.policy_idempotency:
            prior_fingerprint, prior = self.catalog.policy_idempotency[key]
            if prior_fingerprint != fingerprint:
                raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
            return prior
        prior_revisions = self.catalog.list_memory_policies(policy.agent_profile_id)
        if policy.revision != max((item.revision for item in prior_revisions), default=0) + 1:
            raise ConfigurationVersionConflict("memory policy revision conflict")
        self.catalog.memory_policies[policy.id] = policy
        if key is not None:
            self.catalog.policy_idempotency[key] = (fingerprint, policy)
        return policy

    async def list_memory_policies(
        self, issuer: str, subject: str, agent_profile_id: UUID
    ) -> list[MemoryPolicy]:
        del issuer, subject
        self.catalog.get_agent(agent_profile_id)
        return self.catalog.list_memory_policies(agent_profile_id)

    async def attach_memory_policy(
        self,
        issuer: str,
        subject: str,
        agent_profile_id: UUID,
        policy_revision_id: UUID,
        expected_version: int,
        key: str,
    ) -> AgentProfile:
        del issuer, subject
        return self.catalog.attach_memory_policy(
            agent_profile_id, policy_revision_id, expected_version, key
        )

    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision:
        return self.catalog.resolve_revision_unchecked(identifier)

    def resolve_revision(self, identifier: UUID) -> AgentRevision:
        return self.catalog.resolve_revision(identifier)

    def compilation(
        self, revision_id: UUID, persona_revision_id: UUID | None = None
    ) -> PromptCompilation:
        return self.catalog.compilation(revision_id, persona_revision_id)

    def compile_prompt(
        self,
        revision_id: UUID,
        *,
        persona_revision_id: UUID | None = None,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation:
        return self.catalog.compile_prompt(
            revision_id,
            persona_revision_id=persona_revision_id,
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
        memory_policy_revision_id: UUID = GENERAL_MEMORY_POLICY_ID,
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
            memory_policy_revision_id,
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
        memory_policy_revision_id: UUID | None = None,
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
            memory_policy_revision_id,
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

    async def list_memory_policies(
        self, issuer: str, subject: str, agent_profile_id: UUID
    ) -> list[MemoryPolicy]:
        return await self.repository.list_memory_policies(issuer, subject, agent_profile_id)

    async def create_memory_policy(
        self, issuer: str, subject: str, policy: MemoryPolicy, key: str | None = None
    ) -> MemoryPolicy:
        result = await self.repository.create_memory_policy(issuer, subject, policy, key)
        await self._audit("agent.memory_policy.create", issuer, subject, result)
        return result

    async def attach_memory_policy(
        self,
        issuer: str,
        subject: str,
        agent_profile_id: UUID,
        policy_revision_id: UUID,
        expected_version: int,
        key: str,
    ) -> AgentProfile:
        result = await self.repository.attach_memory_policy(
            issuer, subject, agent_profile_id, policy_revision_id, expected_version, key
        )
        await self._audit("agent.memory_policy.attach", issuer, subject, result)
        return result

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
        persona_revision_id: UUID | None = None,
        metrics: PromptMetricsPort | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
    ) -> PromptCompilation:
        return self.repository.compile_prompt(
            revision_id,
            persona_revision_id=persona_revision_id,
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
    "GENERAL_MEMORY_POLICY_ID",
    "platform_memory_policy_id",
    "is_platform_memory_policy",
    "MemoryPolicy",
    "AgentMemoryPolicy",
    "MemoryPolicyRevision",
    "MemoryRecallMode",
    "GENERAL_PROFILE_ID",
    "GENERAL_REVISION_ID",
    "NEUTRAL_PERSONA_ID",
    "NEUTRAL_PERSONA_REVISION_ID",
    "PromptBundleRevision",
    "PromptCompilation",
    "PromptCompiler",
    "PromptComponentRevision",
]
