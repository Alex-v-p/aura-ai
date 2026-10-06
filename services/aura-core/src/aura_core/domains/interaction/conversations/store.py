"""Conversation application service and an in-memory repository seam.

The in-memory implementation is deterministic and useful for unit/system tests. The
SQLAlchemy repository can implement the same public methods without changing routes.
"""

import asyncio
import base64
import binascii
import hashlib
import json
from collections.abc import Iterable
from datetime import datetime
from time import perf_counter
from typing import cast
from uuid import UUID, uuid5

from aura_core.domains.execution.runs.dto import (
    Run,
    RunClaim,
    RunClaimLost,
    RunError,
    RunStatus,
)
from aura_core.domains.interaction.agents.public import (
    GENERAL_POLICY_ID,
    GENERAL_PROFILE_ID,
    GENERAL_REVISION_ID,
    AgentCatalog,
    AgentRevision,
    ConfigurationDisabled,
    ConfigurationNotFound,
)
from aura_core.domains.interaction.conversations.context import build_context
from aura_core.domains.interaction.conversations.dto import (
    AgentAssignment,
    AssignmentReason,
    Conversation,
    Message,
    MessageRole,
    MessageState,
    PersonaAssignment,
    PersonaAssignmentReason,
    PersonaAssignmentSource,
    SeededAgent,
    now,
)
from aura_core.domains.interaction.personas.public import (
    ConfigurationStatus,
    PersonaCatalog,
    PersonaRevisionQueryPort,
)
from aura_core.platform.telemetry import new_span_id
from aura_core.runtime.models.capacity import DEFAULT_CONTEXT_TOKENS
from aura_core.runtime.models.ports import ModelDescriptor
from aura_core.runtime.prompting.public import PromptCompilation, PromptMetricsPort

NAMESPACE = UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
type CommandResult = tuple[Conversation, Message, Run]
GENERAL_AGENT = SeededAgent(
    profile_id=GENERAL_PROFILE_ID,
    revision_id=GENERAL_REVISION_ID,
    policy_revision_id=GENERAL_POLICY_ID,
    system_prompt="You are Aura, a helpful local-first household assistant.",
)


class ConversationNotFound(LookupError):
    pass


class ActiveRunConflict(RuntimeError):
    pass


class VersionConflict(RuntimeError):
    pass


class IdempotencyConflict(RuntimeError):
    pass


class ModelUnavailable(ValueError):
    pass


class AgentUnavailable(ValueError):
    pass


class PersonaUnavailable(ValueError):
    pass


class AgentSwitchConfirmationRequired(ValueError):
    pass


class ConversationStore:
    def __init__(
        self,
        default_model: str | None = None,
        agents: AgentCatalog | None = None,
        personas: PersonaRevisionQueryPort | None = None,
    ) -> None:
        self.default_model = default_model
        self.agents = agents or AgentCatalog()
        self.personas = personas or PersonaCatalog()
        self._conversations: dict[UUID, Conversation] = {}
        self._idempotency: dict[tuple[str, str, str], object] = {}
        self._idempotency_fingerprints: dict[tuple[str, str, str], str] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self.auth_audit: list[dict[str, object]] = []
        self.prompt_metrics: PromptMetricsPort | None = None

    def set_prompt_metrics(self, metrics: PromptMetricsPort) -> None:
        """Attach the application metadata-only prompt telemetry sink."""

        self.prompt_metrics = metrics

    async def record_auth_audit(
        self,
        action: str,
        outcome: str,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.auth_audit.append(
            {
                "action": action,
                "outcome": outcome,
                "issuer": issuer,
                "subject": subject,
                "metadata": dict(metadata or {}),
            }
        )

    async def _lock_for(self, conversation_id: UUID) -> asyncio.Lock:
        async with self._global_lock:
            return self._locks.setdefault(conversation_id, asyncio.Lock())

    async def get(
        self, conversation_id: UUID, subject: str, issuer: str | None = None
    ) -> Conversation:
        if issuer is None:
            raise ValueError("issuer is required")
        conversation = self._conversations.get(conversation_id)
        if (
            conversation is None
            or conversation.principal_subject != subject
            or conversation.principal_issuer != issuer
        ):
            raise ConversationNotFound
        return conversation

    async def list(
        self,
        subject: str,
        limit: int = 30,
        cursor: str | None = None,
        issuer: str | None = None,
    ) -> tuple[list[Conversation], str | None]:
        if issuer is None:
            raise ValueError("issuer is required")
        values = [
            item
            for item in self._conversations.values()
            if item.principal_subject == subject and item.principal_issuer == issuer
        ]
        values.sort(key=lambda item: (item.updated_at, item.id), reverse=True)
        if cursor:
            try:
                padded = cursor + "=" * (-len(cursor) % 4)
                timestamp, identifier = (
                    base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
                )
                cursor_time = datetime.fromisoformat(timestamp)
                cursor_id = UUID(identifier)
                values = [
                    item for item in values if (item.updated_at, item.id) < (cursor_time, cursor_id)
                ]
            except ValueError, binascii.Error, UnicodeDecodeError:
                raise ValueError("invalid cursor") from None
        page = values[:limit]
        next_cursor = None
        if len(values) > limit:
            last = page[-1]
            raw = f"{last.updated_at.isoformat()}|{last.id}"
            next_cursor = base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")
        return page, next_cursor

    async def create(
        self,
        issuer: str,
        subject: str,
        message: str,
        model_id: str,
        models: Iterable[ModelDescriptor],
        idempotency_key: str,
        agent_revision_id: UUID | None = None,
        persona_revision_id: UUID | None = None,
    ) -> tuple[Conversation, Message, Run]:
        key = (issuer, subject, idempotency_key)
        fingerprint = _fingerprint(
            "create", message, model_id, agent_revision_id, persona_revision_id
        )
        existing = self._idempotency.get(key)
        if existing is not None:
            if self._idempotency_fingerprints.get(key) != fingerprint:
                raise IdempotencyConflict
            if not isinstance(existing, tuple) or len(cast(tuple[object, ...], existing)) != 3:
                raise IdempotencyConflict
            return cast(CommandResult, existing)
        self._validate_model(model_id, models)
        revision = self._resolve_agent(agent_revision_id)
        effective_persona = persona_revision_id or revision.persona_revision_id
        if persona_revision_id is not None:
            self._resolve_active_persona(persona_revision_id)
        conversation = Conversation(
            principal_issuer=issuer,
            principal_subject=subject,
            title=make_title(message),
            agent_profile_id=revision.profile_id,
            agent_revision_id=revision.id,
            model_id=model_id,
            persona_override_revision_id=persona_revision_id,
        )
        user_message = Message(conversation.id, MessageRole.USER, message)
        run = Run(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            agent_revision_id=revision.id,
            model_policy_revision_id=revision.model_policy_revision_id,
            provider="ollama",
            model_id=model_id,
            persona_revision_id=effective_persona,
            prompt_bundle_revision_id=revision.prompt_bundle_revision_id,
            prompt_hash=self.agents.compilation(revision.id, effective_persona).prompt_hash,
        )
        user_message.run_id = run.id
        conversation.messages.append(user_message)
        conversation.runs.append(run)
        conversation.assignments.append(
            AgentAssignment(revision.profile_id, revision.id, AssignmentReason.INITIAL, None, now())
        )
        conversation.persona_assignments.append(
            PersonaAssignment(
                effective_persona,
                PersonaAssignmentSource.CONVERSATION_OVERRIDE
                if persona_revision_id is not None
                else PersonaAssignmentSource.AGENT_DEFAULT,
                PersonaAssignmentReason.INITIAL,
                None,
                now(),
            )
        )
        self._conversations[conversation.id] = conversation
        result = (conversation, user_message, run)
        self._idempotency[key] = result
        self._idempotency_fingerprints[key] = fingerprint
        return result

    async def update_model(
        self,
        conversation_id: UUID,
        subject: str,
        model_id: str,
        version: int,
        models: Iterable[ModelDescriptor],
        idempotency_key: str,
        issuer: str | None = None,
        agent_revision_id: UUID | None = None,
        confirmation: bool = False,
        persona_revision_id: UUID | None = None,
        use_agent_default_persona: bool | None = None,
    ) -> Conversation:
        if issuer is None:
            raise ValueError("issuer is required")
        conversation = await self.get(conversation_id, subject, issuer)
        key = (issuer, subject, idempotency_key)
        fingerprint = _fingerprint(
            "update",
            conversation_id,
            model_id,
            version,
            agent_revision_id,
            confirmation,
            persona_revision_id,
            use_agent_default_persona,
        )
        persona_change = persona_revision_id is not None or use_agent_default_persona is True
        configuration_started = perf_counter() if persona_change else None
        existing = self._idempotency.get(key)
        if existing is not None:
            if self._idempotency_fingerprints.get(key) != fingerprint:
                if persona_change:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "idempotency",
                        configuration_started,
                        source=(
                            PersonaAssignmentSource.CONVERSATION_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentSource.AGENT_DEFAULT
                        ),
                        reason=(
                            PersonaAssignmentReason.MANUAL_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT
                        ),
                    )
                raise IdempotencyConflict
            if not isinstance(existing, Conversation):
                if persona_change:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "idempotency",
                        configuration_started,
                        source=(
                            PersonaAssignmentSource.CONVERSATION_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentSource.AGENT_DEFAULT
                        ),
                        reason=(
                            PersonaAssignmentReason.MANUAL_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT
                        ),
                    )
                raise IdempotencyConflict
            return existing
        configuration_change = (
            agent_revision_id is not None
            or persona_revision_id is not None
            or use_agent_default_persona is True
        )
        async with await self._lock_for(conversation_id):
            if conversation.version != version:
                if persona_change:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "version_conflict",
                        configuration_started,
                        source=(
                            PersonaAssignmentSource.CONVERSATION_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentSource.AGENT_DEFAULT
                        ),
                        reason=(
                            PersonaAssignmentReason.MANUAL_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT
                        ),
                    )
                raise VersionConflict
            if configuration_change and conversation.current_run is not None:
                if persona_change:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "active_run",
                        configuration_started,
                        source=(
                            PersonaAssignmentSource.CONVERSATION_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentSource.AGENT_DEFAULT
                        ),
                        reason=(
                            PersonaAssignmentReason.MANUAL_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT
                        ),
                    )
                raise ActiveRunConflict
            if configuration_change and not confirmation:
                if persona_change:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "validation",
                        configuration_started,
                        source=(
                            PersonaAssignmentSource.CONVERSATION_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentSource.AGENT_DEFAULT
                        ),
                        reason=(
                            PersonaAssignmentReason.MANUAL_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT
                        ),
                    )
                raise AgentSwitchConfirmationRequired
            try:
                self._validate_model(model_id, models)
            except ModelUnavailable:
                if persona_change:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "validation",
                        configuration_started,
                        source=(
                            PersonaAssignmentSource.CONVERSATION_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentSource.AGENT_DEFAULT
                        ),
                        reason=(
                            PersonaAssignmentReason.MANUAL_OVERRIDE
                            if persona_revision_id is not None
                            else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT
                        ),
                    )
                raise
            if agent_revision_id is not None:
                self._assign_agent(
                    conversation,
                    agent_revision_id,
                    AssignmentReason.MANUAL_SWITCH,
                    assign_default_persona=persona_revision_id is None
                    and not use_agent_default_persona,
                )
            if persona_revision_id is not None:
                try:
                    self._resolve_active_persona(persona_revision_id)
                except PersonaUnavailable as exc:
                    self._record_persona_configuration_rejection(
                        conversation,
                        persona_revision_id,
                        "disabled" if "disabled" in str(exc) else "not_found",
                        configuration_started,
                        source=PersonaAssignmentSource.CONVERSATION_OVERRIDE,
                        reason=PersonaAssignmentReason.MANUAL_OVERRIDE,
                    )
                    raise
                self._assign_persona(
                    conversation,
                    persona_revision_id,
                    PersonaAssignmentSource.CONVERSATION_OVERRIDE,
                    PersonaAssignmentReason.MANUAL_OVERRIDE,
                )
            elif use_agent_default_persona:
                revision = self._revision_unchecked(conversation.agent_revision_id)
                self._assign_persona(
                    conversation,
                    revision.persona_revision_id,
                    PersonaAssignmentSource.AGENT_DEFAULT,
                    PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT,
                )
            conversation.model_id = model_id
            conversation.version += 1
            conversation.updated_at = now()
        self._idempotency[key] = conversation
        self._idempotency_fingerprints[key] = fingerprint
        if persona_revision_id is not None or use_agent_default_persona:
            effective = persona_revision_id or self._revision_unchecked(
                conversation.agent_revision_id
            ).persona_revision_id
            self._record_persona_configuration(
                conversation,
                effective,
                PersonaAssignmentSource.CONVERSATION_OVERRIDE
                if persona_revision_id is not None
                else PersonaAssignmentSource.AGENT_DEFAULT,
                PersonaAssignmentReason.MANUAL_OVERRIDE
                if persona_revision_id is not None
                else PersonaAssignmentReason.RESET_TO_AGENT_DEFAULT,
                "ok",
                duration_ms=(perf_counter() - configuration_started) * 1000
                if configuration_started is not None
                else 0.0,
            )
            await self.record_auth_audit(
                "conversation.persona.reset"
                if use_agent_default_persona
                else "conversation.persona.override",
                "ok",
                issuer=issuer,
                subject=subject,
                metadata={
                    "conversationId": str(conversation.id),
                    "personaProfileId": str(self.personas.find_revision(effective)[0].id),
                    "personaRevisionId": str(effective),
                    "revision": self.personas.find_revision(effective)[1].revision,
                    "source": "agent_default"
                    if use_agent_default_persona
                    else "conversation_override",
                    "reason": "reset_to_agent_default"
                    if use_agent_default_persona
                    else "manual_override",
                },
            )
        return conversation

    async def switch_agent(
        self,
        conversation_id: UUID,
        subject: str,
        agent_revision_id: UUID,
        version: int,
        idempotency_key: str,
        issuer: str | None = None,
        *,
        confirmation: bool = False,
        upgrade: bool = False,
    ) -> Conversation:
        if issuer is None:
            raise ValueError("issuer is required")
        if not confirmation:
            raise AgentSwitchConfirmationRequired
        conversation = await self.get(conversation_id, subject, issuer)
        key = (issuer, subject, idempotency_key)
        fingerprint = hashlib.sha256(
            f"agent:{conversation_id}:{agent_revision_id}:{version}:{upgrade}".encode()
        ).hexdigest()
        existing = self._idempotency.get(key)
        if existing is not None:
            if self._idempotency_fingerprints.get(key) != fingerprint or not isinstance(
                existing, Conversation
            ):
                raise IdempotencyConflict
            return existing
        async with await self._lock_for(conversation_id):
            if conversation.version != version:
                raise VersionConflict
            if conversation.current_run is not None:
                raise ActiveRunConflict
            self._assign_agent(
                conversation,
                agent_revision_id,
                AssignmentReason.REVISION_UPGRADE
                if upgrade
                else AssignmentReason.MANUAL_SWITCH,
            )
            conversation.version += 1
            conversation.updated_at = now()
        self._idempotency[key] = conversation
        self._idempotency_fingerprints[key] = fingerprint
        return conversation

    async def add_run(
        self,
        conversation_id: UUID,
        subject: str,
        message: str,
        expected_version: int,
        idempotency_key: str,
        issuer: str | None = None,
    ) -> tuple[Conversation, Message, Run]:
        if issuer is None:
            raise ValueError("issuer is required")
        conversation = await self.get(conversation_id, subject, issuer)
        key = (issuer, subject, idempotency_key)
        existing = self._idempotency.get(key)
        if existing is not None:
            fingerprint = hashlib.sha256(
                f"run:{conversation_id}:{message}:{expected_version}".encode()
            ).hexdigest()
            if self._idempotency_fingerprints.get(key) != fingerprint:
                raise IdempotencyConflict
            if not isinstance(existing, tuple) or len(cast(tuple[object, ...], existing)) != 3:
                raise IdempotencyConflict
            return cast(CommandResult, existing)
        async with await self._lock_for(conversation_id):
            if conversation.version != expected_version:
                raise VersionConflict
            if conversation.current_run is not None:
                raise ActiveRunConflict
            current_revision = self._resolve_agent(conversation.agent_revision_id)
            effective_persona = (
                conversation.persona_override_revision_id or current_revision.persona_revision_id
            )
            user_message = Message(conversation.id, MessageRole.USER, message)
            run = Run(
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                agent_revision_id=conversation.agent_revision_id,
                model_policy_revision_id=current_revision.model_policy_revision_id,
                provider="ollama",
                model_id=conversation.model_id,
                persona_revision_id=effective_persona,
                prompt_bundle_revision_id=current_revision.prompt_bundle_revision_id,
                prompt_hash=self.agents.compilation(
                    current_revision.id, effective_persona
                ).prompt_hash,
            )
            user_message.run_id = run.id
            conversation.messages.append(user_message)
            conversation.runs.append(run)
            conversation.version += 1
            conversation.updated_at = now()
        result = (conversation, user_message, run)
        self._idempotency[key] = result
        self._idempotency_fingerprints[key] = hashlib.sha256(
            f"run:{conversation_id}:{message}:{expected_version}".encode()
        ).hexdigest()
        return result

    async def retry(
        self, run_id: UUID, subject: str, idempotency_key: str, issuer: str | None = None
    ) -> tuple[Conversation, Message, Run]:
        if issuer is None:
            raise ValueError("issuer is required")
        key = (issuer, subject, idempotency_key)
        existing = self._idempotency.get(key)
        if existing is not None:
            fingerprint = hashlib.sha256(f"retry:{run_id}".encode()).hexdigest()
            if self._idempotency_fingerprints.get(key) != fingerprint:
                raise IdempotencyConflict
            if not isinstance(existing, tuple) or len(cast(tuple[object, ...], existing)) != 3:
                raise IdempotencyConflict
            return cast(CommandResult, existing)
        conversation = next(
            (
                item
                for item in self._conversations.values()
                if any(run.id == run_id for run in item.runs)
            ),
            None,
        )
        if (
            conversation is None
            or conversation.principal_subject != subject
            or conversation.principal_issuer != issuer
        ):
            raise ConversationNotFound
        prior = next(run for run in conversation.runs if run.id == run_id)
        self._resolve_agent(prior.agent_revision_id)
        # Legacy runs may predate persisted prompt provenance.  Retry remains
        # pinned to the historical revision/model policy, while the new run
        # receives freshly derived provenance for provider verification.
        persona_revision_id = prior.persona_revision_id
        prompt_bundle_revision_id = prior.prompt_bundle_revision_id
        prompt_hash = prior.prompt_hash
        if (
            persona_revision_id is None
            or prompt_bundle_revision_id is None
            or prompt_hash is None
        ):
            persona_revision_id, prompt_bundle_revision_id, prompt_hash = self._provenance(
                prior.agent_revision_id, prior.persona_revision_id
            )
        async with await self._lock_for(conversation.id):
            if conversation.current_run is not None:
                raise ActiveRunConflict
            user_message = next(
                message for message in conversation.messages if message.id == prior.user_message_id
            )
            run = Run(
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                agent_revision_id=prior.agent_revision_id,
                model_policy_revision_id=prior.model_policy_revision_id,
                provider="ollama",
                model_id=conversation.model_id,
                persona_revision_id=persona_revision_id,
                prompt_bundle_revision_id=prompt_bundle_revision_id,
                prompt_hash=prompt_hash,
                retry_of_run_id=prior.id,
            )
            conversation.runs.append(run)
            conversation.version += 1
            conversation.updated_at = now()
        result = (conversation, user_message, run)
        self._idempotency[key] = result
        self._idempotency_fingerprints[key] = hashlib.sha256(f"retry:{run_id}".encode()).hexdigest()
        return result

    async def request_cancel(
        self,
        run_id: UUID,
        subject: str,
        idempotency_key: str,
        issuer: str | None = None,
    ) -> Run:
        if issuer is None:
            raise ValueError("issuer is required")
        key = (issuer, subject, idempotency_key)
        fingerprint = hashlib.sha256(f"cancel:{run_id}".encode()).hexdigest()
        existing = self._idempotency.get(key)
        if existing is not None:
            if self._idempotency_fingerprints.get(key) != fingerprint:
                raise IdempotencyConflict
            if not isinstance(existing, Run):
                raise IdempotencyConflict
            return existing
        conversation, run = await self._find_run(run_id, subject, issuer)
        if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
            run.status = RunStatus.CANCEL_REQUESTED
            conversation.updated_at = now()
        self._idempotency[key] = run
        self._idempotency_fingerprints[key] = fingerprint
        return run

    async def _find_run(self, run_id: UUID, subject: str, issuer: str) -> tuple[Conversation, Run]:
        for conversation in self._conversations.values():
            if conversation.principal_subject != subject or conversation.principal_issuer != issuer:
                continue
            for run in conversation.runs:
                if run.id == run_id:
                    return conversation, run
        raise ConversationNotFound

    async def find_run(
        self, run_id: UUID, subject: str, issuer: str | None = None
    ) -> tuple[Conversation, Run]:
        if issuer is None:
            raise ValueError("issuer is required")
        return await self._find_run(run_id, subject, issuer)

    async def start_run(
        self,
        run_id: UUID,
        *,
        worker_id: UUID | None = None,
        lease_seconds: float = 300.0,
    ) -> RunClaim:
        for conversation in self._conversations.values():
            for run in conversation.runs:
                if run.id == run_id:
                    async with await self._lock_for(conversation.id):
                        acquired = False
                        current = now()
                        if run.status == RunStatus.CANCEL_REQUESTED:
                            run.status = RunStatus.CANCELED
                            run.finished_at = current
                        elif run.status == RunStatus.QUEUED:
                            run.status = RunStatus.RUNNING
                            run.started_at = current
                            run.attempt_id = worker_id or uuid5(NAMESPACE, f"attempt:{run.id}")
                            run.attempt_count += 1
                            from datetime import timedelta

                            run.lease_expires_at = current + timedelta(seconds=lease_seconds)
                            acquired = True
                        elif (
                            run.status == RunStatus.RUNNING
                            and run.lease_expires_at is not None
                            and run.lease_expires_at <= current
                        ):
                            run.status = RunStatus.INTERRUPTED
                            run.finished_at = current
                            run.lease_expires_at = None
                            run.error = RunError(
                                "WORKER_LEASE_EXPIRED",
                                "The worker lease expired before this run completed.",
                                True,
                                run.id.hex,
                            )
                        message = next(
                            item for item in conversation.messages if item.id == run.user_message_id
                        )
                        return RunClaim(conversation, run, message, acquired)
        raise ConversationNotFound

    async def append_assistant(
        self,
        run_id: UUID,
        text: str,
        state: MessageState = MessageState.PARTIAL,
        *,
        attempt_id: UUID | None = None,
    ) -> Message:
        conversation, run = await self._find_run_any(run_id)
        if attempt_id is not None and (
            run.status != RunStatus.RUNNING or run.attempt_id != attempt_id
        ):
            raise RunClaimLost
        if run.assistant_message_id is None:
            assistant = Message(conversation.id, MessageRole.ASSISTANT, text, state, run.id)
            run.assistant_message_id = assistant.id
            conversation.messages.append(assistant)
        else:
            assistant = next(
                item for item in conversation.messages if item.id == run.assistant_message_id
            )
            assistant.content += text
            assistant.state = state
            assistant.updated_at = now()
        conversation.updated_at = now()
        return assistant

    async def finish_run(
        self,
        run_id: UUID,
        status: RunStatus,
        error: RunError | None = None,
        *,
        attempt_id: UUID | None = None,
    ) -> tuple[Conversation, Run, Message | None]:
        conversation, run = await self._find_run_any(run_id)
        if attempt_id is not None and (
            run.attempt_id != attempt_id
            or run.status not in {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}
        ):
            raise RunClaimLost
        run.status = status
        run.error = error
        run.finished_at = now()
        assistant = next(
            (item for item in conversation.messages if item.id == run.assistant_message_id), None
        )
        if assistant is not None:
            assistant.state = (
                MessageState.COMPLETE
                if status == RunStatus.COMPLETED
                else MessageState.INTERRUPTED
                if status in {RunStatus.CANCELED, RunStatus.INTERRUPTED}
                else MessageState.FAILED
            )
            assistant.updated_at = now()
        conversation.updated_at = now()
        return conversation, run, assistant

    async def _find_run_any(self, run_id: UUID) -> tuple[Conversation, Run]:
        for conversation in self._conversations.values():
            for run in conversation.runs:
                if run.id == run_id:
                    return conversation, run
        raise ConversationNotFound

    async def find_run_any(self, run_id: UUID) -> tuple[Conversation, Run]:
        return await self._find_run_any(run_id)

    async def context(
        self,
        conversation_id: UUID,
        subject: str,
        issuer: str,
        budget: int = DEFAULT_CONTEXT_TOKENS,
        agent_revision_id: UUID | None = None,
        trace_id: str | None = None,
        persona_revision_id: UUID | None = None,
        parent_span_id: str | None = None,
    ) -> list[tuple[str, str]]:
        conversation = await self.get(conversation_id, subject, issuer)
        # The conversation assignment is mutable.  A queued run is not: its
        # pinned revision is the only authority for provider context.
        revision = self._revision_unchecked(
            agent_revision_id or conversation.agent_revision_id
        )
        prompt = self._compile_prompt(
            revision.id,
            persona_revision_id=persona_revision_id,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            run_id=trace_id,
            conversation_id=str(conversation.id),
        ).text
        return build_context(conversation, prompt, budget)

    def prompt_provenance(
        self, revision_id: UUID, persona_revision_id: UUID | None = None
    ) -> dict[str, str]:
        revision = self._revision_unchecked(revision_id)
        effective_persona = persona_revision_id or revision.persona_revision_id
        compilation = self._compile_prompt(
            revision_id, persona_revision_id=effective_persona, instrument=False
        )
        return {
            "agent_revision_id": str(revision.id),
            "persona_revision_id": str(effective_persona),
            "prompt_bundle_revision_id": str(revision.prompt_bundle_revision_id),
            "prompt_hash": compilation.prompt_hash,
            "prompt_component_count": str(compilation.component_count),
            "compiled_prompt_size": str(len(compilation.text)),
        }

    def _resolve_agent(self, identifier: UUID | None) -> AgentRevision:
        try:
            return self.agents.resolve_revision(identifier or GENERAL_AGENT.revision_id)
        except (ConfigurationDisabled, ConfigurationNotFound) as exc:
            raise AgentUnavailable(str(exc)) from exc

    def _compile_prompt(
        self,
        revision_id: UUID,
        *,
        persona_revision_id: UUID | None = None,
        trace_id: str | None = None,
        run_id: str | None = None,
        conversation_id: str | None = None,
        parent_span_id: str | None = None,
        instrument: bool = True,
    ) -> PromptCompilation:
        metrics = self.prompt_metrics if instrument else None
        return self.agents.compile_prompt(
            revision_id,
            persona_revision_id=persona_revision_id,
            metrics=metrics,
            trace_id=trace_id,
            run_id=run_id,
            conversation_id=conversation_id,
            parent_span_id=parent_span_id,
        )

    def _provenance(
        self, revision_id: UUID, persona_revision_id: UUID | None = None
    ) -> tuple[UUID, UUID, str]:
        revision = self._revision_unchecked(revision_id)
        effective_persona = persona_revision_id or revision.persona_revision_id
        compilation = self._compile_prompt(
            revision_id, persona_revision_id=effective_persona, instrument=False
        )
        return (
            effective_persona,
            revision.prompt_bundle_revision_id,
            compilation.prompt_hash,
        )

    def _revision_unchecked(self, identifier: UUID) -> AgentRevision:
        try:
            return self.agents.resolve_revision_unchecked(identifier)
        except ConfigurationNotFound as exc:
            raise AgentUnavailable(str(exc)) from exc

    def _assign_agent(
        self,
        conversation: Conversation,
        identifier: UUID,
        reason: AssignmentReason,
        *,
        assign_default_persona: bool = True,
    ) -> None:
        revision = self._resolve_agent(identifier)
        if (
            conversation.agent_profile_id == revision.profile_id
            and conversation.agent_revision_id != revision.id
            and reason in {AssignmentReason.MANUAL_SWITCH}
        ):
            reason = AssignmentReason.REVISION_UPGRADE
        conversation.agent_profile_id = revision.profile_id
        conversation.agent_revision_id = revision.id
        conversation.assignments.append(
            AgentAssignment(
                revision.profile_id,
                revision.id,
                reason,
                self._latest_message_id(conversation),
                now(),
            )
        )
        if assign_default_persona and conversation.persona_override_revision_id is None:
            self._assign_persona(
                conversation,
                revision.persona_revision_id,
                PersonaAssignmentSource.AGENT_DEFAULT,
                PersonaAssignmentReason.AGENT_REVISION_UPGRADE
                if reason == AssignmentReason.REVISION_UPGRADE
                else PersonaAssignmentReason.AGENT_SWITCH,
            )

    def _assign_persona(
        self,
        conversation: Conversation,
        revision_id: UUID,
        source: PersonaAssignmentSource,
        reason: PersonaAssignmentReason,
    ) -> None:
        conversation.persona_override_revision_id = (
            revision_id if source == PersonaAssignmentSource.CONVERSATION_OVERRIDE else None
        )
        if conversation.persona_assignments:
            latest = conversation.persona_assignments[-1]
            if latest.persona_revision_id == revision_id and latest.source == source:
                return
        conversation.persona_assignments.append(
            PersonaAssignment(
                revision_id,
                source,
                reason,
                self._latest_message_id(conversation),
                now(),
            )
        )

    def _resolve_active_persona(self, identifier: UUID):
        try:
            profile, revision = self.personas.find_revision(identifier)
        except Exception as exc:
            raise PersonaUnavailable("persona revision not found") from exc
        if profile.status != ConfigurationStatus.ACTIVE:
            raise PersonaUnavailable("persona is disabled")
        return revision

    def _record_persona_configuration(
        self,
        conversation: Conversation,
        persona_revision_id: UUID,
        source: PersonaAssignmentSource,
        reason: PersonaAssignmentReason,
        outcome: str,
        error_class: str | None = None,
        duration_ms: float = 0.0,
    ) -> None:
        if self.prompt_metrics is None:
            return
        try:
            bounded_error_class = {
                "version_conflict": "conflict",
                "active_run": "conflict",
                "confirmation_required": "validation",
            }.get(error_class or "", error_class)
            attributes = {
                "persona_revision_id": str(persona_revision_id),
                "configuration_source": source.value,
                "configuration_reason": reason.value,
            }
            try:
                profile, revision = self.personas.find_revision(persona_revision_id)
            except Exception:
                profile = revision = None
            if profile is not None and revision is not None:
                attributes.update(
                    {
                        "profile_id": str(profile.id),
                        "persona_revision_number": str(revision.revision),
                    }
                )
            self.prompt_metrics.record_span(
                "aura.interaction.agent_configuration",
                "agent.configure",
                max(0.0, duration_ms),
                trace_id=conversation.id.hex,
                span_id=new_span_id(),
                parent_span_id=None,
                dependency="configuration_store",
                outcome=outcome,
                error_class=bounded_error_class,
                conversation_id=str(conversation.id),
                **attributes,
            )
            increment = getattr(self.prompt_metrics, "increment", None)
            if callable(increment):
                increment(
                    "aura.interaction.agent_configuration",
                    "configuration_outcome",
                    trace_id=conversation.id.hex,
                    conversation_id=str(conversation.id),
                    outcome=outcome,
                )
        except Exception:
            return

    def _record_persona_configuration_rejection(
        self,
        conversation: Conversation,
        persona_revision_id: UUID | None,
        error_class: str,
        started: float | None,
        *,
        source: PersonaAssignmentSource = PersonaAssignmentSource.CONVERSATION_OVERRIDE,
        reason: PersonaAssignmentReason = PersonaAssignmentReason.MANUAL_OVERRIDE,
    ) -> None:
        if started is None:
            return
        effective = persona_revision_id
        if effective is None:
            try:
                effective = self._revision_unchecked(
                    conversation.agent_revision_id
                ).persona_revision_id
            except AgentUnavailable:
                return
        self._record_persona_configuration(
            conversation,
            effective,
            source,
            reason,
            "error",
            error_class,
            duration_ms=(perf_counter() - started) * 1000,
        )

    @staticmethod
    def _latest_message_id(conversation: Conversation) -> UUID | None:
        return conversation.messages[-1].id if conversation.messages else None

    @staticmethod
    def _validate_model(model_id: str, models: Iterable[ModelDescriptor]) -> None:
        for model in models:
            if model.id == model_id:
                if not model.selectable:
                    raise ModelUnavailable(model.disabled_reason or "model is not selectable")
                return
        raise ModelUnavailable("model is unavailable")


def make_title(message: str) -> str:
    normalized = " ".join(message.split())
    return normalized[:252] + "..." if len(normalized) > 255 else normalized


def _fingerprint(*parts: object) -> str:
    """Hash structured command fields without delimiter-collision ambiguity."""

    canonical = json.dumps(
        [str(part) if part is not None else None for part in parts],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode()).hexdigest()
