"""Durable PostgreSQL application boundary for owner agent configuration."""

# SQL statements and DTO construction are intentionally kept close to their
# transaction boundary; long-line lint is suppressed for this adapter only.
# ruff: noqa: E501

from datetime import UTC, datetime
from hashlib import sha256
from json import dumps
from uuid import UUID, uuid4

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.governance.audit.public import SqlAuditRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.domains.interaction.agents.persistence import (
    AgentProfileRow,
    AgentRevisionRow,
    MemoryPolicyFallbackGrantRow,
    MemoryPolicyRevisionRow,
    PromptBundleRevisionRow,
    PromptComponentRevisionRow,
)
from aura_core.domains.interaction.agents.public import (
    GENERAL_MEMORY_POLICY_ID,
    GENERAL_POLICY_ID,
    NEUTRAL_PERSONA_REVISION_ID,
    PROMPT_BUNDLE_ID,
    AgentCatalog,
    AgentProfile,
    AgentRevision,
    MemoryPolicy,
    is_platform_memory_policy,
    platform_memory_policy_id,
)
from aura_core.domains.interaction.personas.public import (
    ConfigurationDisabled,
    ConfigurationIdempotencyConflict,
    ConfigurationNotFound,
    ConfigurationStatus,
    ConfigurationVersionConflict,
    PersonaConfigurationQueryPort,
)
from aura_core.runtime.prompting.public import (
    PromptBundleRevision,
    PromptCompilation,
    PromptComponentRevision,
    PromptMetricsPort,
)


def _require_memory_policy_id(value: UUID | None) -> UUID:
    """Reject pre-0010 rows at the runtime boundary after migration head."""
    if value is None:
        raise ConfigurationNotFound("agent revision has no memory policy revision")
    return value


def _policy_owner_matches(row: MemoryPolicyRevisionRow, issuer: str, subject: str) -> bool:
    """Authorize exact owners plus only deterministic migrated defaults.

    Blank-principal rows are legacy migration artifacts.  Their deterministic
    per-agent IDs make them safe to recognize without turning arbitrary UUIDs
    into globally readable policies.
    """

    return (row.principal_issuer, row.principal_subject) == (issuer, subject) or (
        (row.principal_issuer, row.principal_subject) == ("", "")
        and is_platform_memory_policy(row.id, row.agent_profile_id)
    )


class SqlAgentStore:
    """SQL-authoritative agent store with a public persona port."""

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        catalog: AgentCatalog,
        persona_port: PersonaConfigurationQueryPort | None = None,
    ) -> None:
        self.sessions = sessions
        self.catalog = catalog
        self.persona_port = persona_port
        self.audit = SqlAuditRepository()
        self.identities = SqlIdentityRepository()

    async def refresh(self) -> None:
        personas = (
            await self.persona_port.list_personas()
            if self.persona_port is not None
            else self.catalog.persona_query.list_personas()
        )
        self.catalog.persona_query.replace(personas)
        async with self.sessions() as session:
            profiles = (await session.execute(select(AgentProfileRow))).scalars().all()
            revisions = (await session.execute(select(AgentRevisionRow))).scalars().all()
            memory_policies = (
                (await session.execute(select(MemoryPolicyRevisionRow))).scalars().all()
            )
            fallback_grants = (
                (await session.execute(select(MemoryPolicyFallbackGrantRow))).scalars().all()
            )
            components = (await session.execute(select(PromptComponentRevisionRow))).scalars().all()
            bundles = (await session.execute(select(PromptBundleRevisionRow))).scalars().all()
        if not profiles:
            self.catalog.agents = {}
            return
        component_map = {
            row.id: PromptComponentRevision(row.id, row.component, row.revision, row.content)
            for row in components
        }
        hydrated_bundles: dict[UUID, PromptBundleRevision] = {}
        for bundle in bundles:
            platform = component_map.get(bundle.platform_component_revision_id)
            governance = component_map.get(bundle.governance_component_revision_id)
            if platform is None or governance is None:
                continue
            hydrated_bundles[bundle.id] = PromptBundleRevision(
                bundle.id, bundle.revision, platform, governance
            )
        if hydrated_bundles:
            self.catalog.replace_bundles(hydrated_bundles)
        self.catalog.agents = {
            row.id: AgentProfile(
                id=row.id,
                slug=row.slug,
                display_name=row.display_name,
                status=ConfigurationStatus(row.status),
                version=row.version,
                current_revision_id=row.current_revision_id,
                revisions=[
                    AgentRevision(
                        id=item.id,
                        profile_id=item.agent_profile_id,
                        revision=item.revision,
                        display_name=item.display_name or row.display_name,
                        purpose=item.purpose,
                        instructions=item.instructions,
                        persona_revision_id=item.persona_revision_id or NEUTRAL_PERSONA_REVISION_ID,
                        prompt_bundle_revision_id=item.prompt_bundle_revision_id
                        or PROMPT_BUNDLE_ID,
                        model_policy_revision_id=item.model_policy_revision_id,
                        memory_policy_revision_id=_require_memory_policy_id(
                            item.memory_policy_revision_id
                        ),
                        system_prompt=item.system_prompt,
                        created_at=item.created_at or datetime.now(UTC),
                    )
                    for item in revisions
                    if item.agent_profile_id == row.id
                ],
                created_at=row.created_at or datetime.now(UTC),
                updated_at=row.updated_at or datetime.now(UTC),
            )
            for row in profiles
        }
        self.catalog.memory_policies = {
            item.id: MemoryPolicy(
                id=item.id,
                agent_profile_id=item.agent_profile_id,
                revision=item.revision,
                shared_user_read=item.shared_user_read,
                current_agent_read=item.current_agent_read,
                fallback_relevance_threshold=item.fallback_relevance_threshold,
                max_memories=item.max_memories,
                context_budget_fraction=item.context_budget_fraction,
                allow_shared_user_promotion=item.allow_shared_user_promotion,
                fallback_agent_profile_ids=tuple(
                    grant.foreign_agent_profile_id
                    for grant in fallback_grants
                    if grant.policy_id == item.id
                ),
                created_at=item.created_at or datetime.now(UTC),
            )
            for item in memory_policies
        }

    async def list_agents(self) -> list[AgentProfile]:
        await self.refresh()
        return self.catalog.list_agents()

    async def get_agent(self, identifier: UUID) -> AgentProfile:
        await self.refresh()
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
        fingerprint = self._fingerprint(
            "agent",
            slug,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            memory_policy_revision_id,
        )
        async with self.sessions() as session, session.begin():
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            if prior is None:
                await self._require_active_persona_in_transaction(session, persona_revision_id)
                identifier, revision_id, created = uuid4(), uuid4(), datetime.now(UTC)
                selected_memory_policy = memory_policy_revision_id
                if selected_memory_policy == GENERAL_MEMORY_POLICY_ID:
                    selected_memory_policy = platform_memory_policy_id(identifier)
                    session.add(
                        MemoryPolicyRevisionRow(
                            id=selected_memory_policy,
                            principal_issuer=issuer,
                            principal_subject=subject,
                            agent_profile_id=identifier,
                            revision=1,
                        )
                    )
                else:
                    policy_row = await session.get(MemoryPolicyRevisionRow, selected_memory_policy)
                    if (
                        policy_row is None
                        or policy_row.agent_profile_id != identifier
                        or (
                            not _policy_owner_matches(policy_row, issuer, subject)
                        )
                    ):
                        raise ConfigurationNotFound("memory policy revision not found")
                session.add(
                    AgentProfileRow(
                        id=identifier,
                        slug=slug,
                        display_name=display_name,
                        current_revision_id=revision_id,
                        created_at=created,
                        updated_at=created,
                    )
                )
                session.add(
                    AgentRevisionRow(
                        id=revision_id,
                        agent_profile_id=identifier,
                        revision=1,
                        display_name=display_name,
                        system_prompt=instructions,
                        purpose=purpose,
                        instructions=instructions,
                        persona_revision_id=persona_revision_id,
                        prompt_bundle_revision_id=PROMPT_BUNDLE_ID,
                        model_policy_revision_id=GENERAL_POLICY_ID,
                        memory_policy_revision_id=selected_memory_policy,
                        created_at=created,
                    )
                )
                await self._record(session, issuer, subject, key, fingerprint, identifier)
                await self._stage_audit(session, issuer, subject, "agent.create", identifier, 1)
            else:
                identifier = UUID(prior["profileId"])
        await self.refresh()
        return self.catalog.get_agent(identifier)

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
        fingerprint = self._fingerprint(
            "agent-revision",
            identifier,
            expected,
            display_name,
            purpose,
            instructions,
            persona_revision_id,
            memory_policy_revision_id,
        )
        async with self.sessions() as session, session.begin():
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            if prior is None:
                row = await session.get(AgentProfileRow, identifier, with_for_update=True)
                if row is None:
                    raise ConfigurationNotFound("agent not found")
                if row.version != expected:
                    raise ConfigurationVersionConflict("configuration version conflict")
                await self._require_active_persona_in_transaction(session, persona_revision_id)
                selected_memory_policy = memory_policy_revision_id
                if selected_memory_policy is None:
                    current_revision = await session.get(AgentRevisionRow, row.current_revision_id)
                    if current_revision is None:
                        raise ConfigurationNotFound("agent revision not found")
                    selected_memory_policy = current_revision.memory_policy_revision_id
                policy_row = await session.get(MemoryPolicyRevisionRow, selected_memory_policy)
                if (
                    policy_row is None
                    or policy_row.agent_profile_id != identifier
                    or (
                        not _policy_owner_matches(policy_row, issuer, subject)
                    )
                ):
                    raise ConfigurationNotFound("memory policy revision not found")
                number = (
                    await session.execute(
                        select(AgentRevisionRow.revision)
                        .where(AgentRevisionRow.agent_profile_id == identifier)
                        .order_by(AgentRevisionRow.revision.desc())
                        .limit(1)
                    )
                ).scalar_one() + 1
                revision_id, now = uuid4(), datetime.now(UTC)
                session.add(
                    AgentRevisionRow(
                        id=revision_id,
                        agent_profile_id=identifier,
                        revision=number,
                        display_name=display_name,
                        system_prompt=instructions,
                        purpose=purpose,
                        instructions=instructions,
                        persona_revision_id=persona_revision_id,
                        prompt_bundle_revision_id=PROMPT_BUNDLE_ID,
                        model_policy_revision_id=GENERAL_POLICY_ID,
                        memory_policy_revision_id=selected_memory_policy,
                        created_at=now,
                    )
                )
                row.current_revision_id, row.version, row.updated_at = (
                    revision_id,
                    row.version + 1,
                    now,
                )
                await self._record(session, issuer, subject, key, fingerprint, identifier)
                await self._stage_audit(
                    session, issuer, subject, "agent.revise", identifier, number
                )
            else:
                identifier = UUID(prior["profileId"])
        await self.refresh()
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
        fingerprint = self._fingerprint("agent-status", identifier, expected, status.value)
        async with self.sessions() as session, session.begin():
            prior = await self._replay(session, issuer, subject, key, fingerprint)
            row = await session.get(AgentProfileRow, identifier, with_for_update=True)
            if row is None:
                raise ConfigurationNotFound("agent not found")
            if prior is None:
                if row.version != expected:
                    raise ConfigurationVersionConflict("configuration version conflict")
                row.status, row.version, row.updated_at = (
                    status.value,
                    row.version + 1,
                    datetime.now(UTC),
                )
                await self._record(session, issuer, subject, key, fingerprint, identifier)
                await self._stage_audit(session, issuer, subject, "agent.status", identifier, None)
        await self.refresh()
        return self.catalog.get_agent(identifier)

    async def _require_active_persona_in_transaction(
        self, session: AsyncSession, revision_id: UUID
    ) -> None:
        if self.persona_port is not None:
            await self.persona_port.require_active_revision_in_transaction(session, revision_id)
            return
        for profile in self.catalog.persona_query.list_personas():
            if any(revision.id == revision_id for revision in profile.revisions):
                if profile.status != ConfigurationStatus.ACTIVE:
                    raise ConfigurationVersionConflict("persona is disabled")
                return
        raise ConfigurationNotFound("persona revision not found")

    async def require_active_revision_in_transaction(
        self, session: AsyncSession, identifier: UUID
    ) -> AgentRevision:
        """Lock an agent profile and admit only active pinned revisions."""

        revision = await session.get(AgentRevisionRow, identifier)
        if revision is None:
            raise ConfigurationNotFound("agent revision not found")
        profile = await session.get(
            AgentProfileRow, revision.agent_profile_id, with_for_update=True
        )
        if profile is None:
            raise ConfigurationNotFound("agent profile not found")
        if profile.status != ConfigurationStatus.ACTIVE.value:
            raise ConfigurationDisabled("agent is disabled")
        return AgentRevision(
            id=revision.id,
            profile_id=revision.agent_profile_id,
            revision=revision.revision,
            display_name=revision.display_name or profile.display_name,
            purpose=revision.purpose,
            instructions=revision.instructions,
            persona_revision_id=revision.persona_revision_id or NEUTRAL_PERSONA_REVISION_ID,
            prompt_bundle_revision_id=revision.prompt_bundle_revision_id or PROMPT_BUNDLE_ID,
            model_policy_revision_id=revision.model_policy_revision_id,
            system_prompt=revision.system_prompt,
            created_at=revision.created_at or datetime.now(UTC),
            memory_policy_revision_id=_require_memory_policy_id(revision.memory_policy_revision_id),
        )

    async def get_memory_policy(self, issuer: str, subject: str, policy_id: UUID) -> MemoryPolicy:
        async with self.sessions() as session:
            row = await session.get(MemoryPolicyRevisionRow, policy_id)
            if row is None or not _policy_owner_matches(row, issuer, subject):
                raise ConfigurationNotFound("memory policy revision not found")
            grants = (
                (
                    await session.execute(
                        select(MemoryPolicyFallbackGrantRow.foreign_agent_profile_id).where(
                            MemoryPolicyFallbackGrantRow.policy_id == policy_id
                        )
                    )
                )
                .scalars()
                .all()
            )
            return MemoryPolicy(
                row.id,
                row.agent_profile_id,
                row.revision,
                row.shared_user_read,
                row.current_agent_read,
                row.fallback_relevance_threshold,
                row.max_memories,
                row.context_budget_fraction,
                row.allow_shared_user_promotion,
                tuple(grants),
                row.created_at or datetime.now(UTC),
            )

    async def create_memory_policy(
        self, issuer: str, subject: str, policy: MemoryPolicy
    ) -> MemoryPolicy:
        if not issuer or not subject:
            raise ConfigurationNotFound("memory policy owner is required")
        async with self.sessions() as session, session.begin():
            prior = await session.get(MemoryPolicyRevisionRow, policy.id)
            if prior is not None:
                if not _policy_owner_matches(prior, issuer, subject):
                    raise ConfigurationVersionConflict(
                        "memory policy identifier is owned by another principal"
                    )
                return policy
            owner_policies = (
                await session.execute(
                    select(MemoryPolicyRevisionRow).where(
                        MemoryPolicyRevisionRow.principal_issuer == issuer,
                        MemoryPolicyRevisionRow.principal_subject == subject,
                    )
                )
            ).scalars().all()
            migrated_defaults = (
                await session.execute(
                    select(MemoryPolicyRevisionRow).where(
                        MemoryPolicyRevisionRow.principal_issuer == "",
                        MemoryPolicyRevisionRow.principal_subject == "",
                    )
                )
            ).scalars().all()
            policies = [
                *owner_policies,
                *[
                    item
                    for item in migrated_defaults
                    if is_platform_memory_policy(item.id, item.agent_profile_id)
                ],
            ]
            policy_ids = [item.id for item in policies]
            grants = (
                await session.execute(
                    select(MemoryPolicyFallbackGrantRow).where(
                        MemoryPolicyFallbackGrantRow.policy_id.in_(policy_ids)
                    )
                )
            ).scalars().all()
            edges: dict[UUID, set[UUID]] = {item.agent_profile_id: set() for item in policies}
            by_policy = {item.id: item for item in policies}
            for grant in grants:
                owner_policy = by_policy.get(grant.policy_id)
                if owner_policy is not None:
                    edges.setdefault(owner_policy.agent_profile_id, set()).add(
                        grant.foreign_agent_profile_id
                    )
            edges.setdefault(policy.agent_profile_id, set()).update(
                policy.fallback_agent_profile_ids
            )
            pending = list(policy.fallback_agent_profile_ids)
            visited: set[UUID] = set()
            while pending:
                current = pending.pop()
                if current == policy.agent_profile_id:
                    raise ConfigurationVersionConflict("fallback grants cannot form a cycle")
                if current in visited:
                    continue
                visited.add(current)
                pending.extend(edges.get(current, ()))
            session.add(
                MemoryPolicyRevisionRow(
                    id=policy.id,
                    principal_issuer=issuer,
                    principal_subject=subject,
                    agent_profile_id=policy.agent_profile_id,
                    revision=policy.revision,
                    shared_user_read=policy.shared_user_read,
                    current_agent_read=policy.current_agent_read,
                    fallback_relevance_threshold=policy.fallback_relevance_threshold,
                    max_memories=policy.max_memories,
                    context_budget_fraction=policy.context_budget_fraction,
                    allow_shared_user_promotion=policy.allow_shared_user_promotion,
                )
            )
            for foreign_agent in policy.fallback_agent_profile_ids:
                if foreign_agent == policy.agent_profile_id:
                    raise ConfigurationVersionConflict("self fallback grant is not allowed")
                if await session.get(AgentProfileRow, foreign_agent) is None:
                    raise ConfigurationNotFound("fallback agent not found")
                session.add(
                    MemoryPolicyFallbackGrantRow(
                        policy_id=policy.id, foreign_agent_profile_id=foreign_agent
                    )
                )
        return policy

    def resolve_revision_unchecked(self, identifier: UUID) -> AgentRevision:
        """Resolve a pinned revision from the refreshed read model."""

        return self.catalog.resolve_revision_unchecked(identifier)

    def resolve_revision(self, identifier: UUID) -> AgentRevision:
        """Resolve an active pinned revision from the refreshed read model."""

        return self.catalog.resolve_revision(identifier)

    def compilation(
        self, revision_id: UUID, persona_revision_id: UUID | None = None
    ) -> PromptCompilation:
        """Compile a pinned revision using the refreshed prompt read model."""

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

    async def _stage_audit(
        self,
        session: AsyncSession,
        issuer: str,
        subject: str,
        action: str,
        identifier: UUID,
        revision: int | None,
    ) -> None:
        principal = await self.identities.resolve(session, issuer, subject)
        metadata: dict[str, object] = {"agentId": str(identifier)}
        if revision is not None:
            metadata["revision"] = revision
        self.audit.stage(session, principal.id, action, "ok", metadata)

    @staticmethod
    def _fingerprint(*parts: object) -> str:
        canonical = dumps(
            {
                "operation": str(parts[0]) if parts else "configuration",
                "arguments": [str(part) for part in parts[1:]],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return sha256(canonical.encode()).hexdigest()

    async def _replay(
        self, session: AsyncSession, issuer: str, subject: str, key: str, fingerprint: str
    ):
        await session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:command_key, 0))"),
            {"command_key": f"{issuer}:{subject}:{key}"},
        )
        row = (
            (
                await session.execute(
                    text(
                        "SELECT fingerprint, response FROM command_idempotency "
                        "WHERE principal_issuer=:issuer AND principal_subject=:subject "
                        "AND idempotency_key=:key FOR UPDATE"
                    ),
                    {"issuer": issuer, "subject": subject, "key": key},
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        if row["fingerprint"] != fingerprint:
            raise ConfigurationIdempotencyConflict("idempotency key payload conflict")
        return row["response"]

    async def _record(
        self,
        session: AsyncSession,
        issuer: str,
        subject: str,
        key: str,
        fingerprint: str,
        identifier: UUID,
    ) -> None:
        await session.execute(
            text(
                "INSERT INTO command_idempotency "
                "(principal_issuer, principal_subject, idempotency_key, fingerprint, response) "
                "VALUES (:issuer,:subject,:key,:fingerprint,CAST(:response AS JSONB))"
            ),
            {
                "issuer": issuer,
                "subject": subject,
                "key": key,
                "fingerprint": fingerprint,
                "response": dumps({"profileId": str(identifier)}),
            },
        )
