"""Conversation application service and an in-memory repository seam.

The in-memory implementation is deterministic and useful for unit/system tests. The
SQLAlchemy repository can implement the same public methods without changing routes.
"""

import asyncio
import base64
import binascii
import hashlib
from collections.abc import Iterable
from datetime import datetime
from typing import cast
from uuid import UUID, uuid5

from aura_core.domains.execution.runs.dto import (
    Run,
    RunClaim,
    RunClaimLost,
    RunError,
    RunStatus,
)
from aura_core.domains.interaction.conversations.context import build_context
from aura_core.domains.interaction.conversations.dto import (
    Conversation,
    Message,
    MessageRole,
    MessageState,
    SeededAgent,
    now,
)
from aura_core.runtime.models.capacity import DEFAULT_CONTEXT_TOKENS
from aura_core.runtime.models.ports import ModelDescriptor

NAMESPACE = UUID("a8a6b450-20fb-4c6a-b0af-e7cb0f9c7b8a")
type CommandResult = tuple[Conversation, Message, Run]
GENERAL_AGENT = SeededAgent(
    profile_id=uuid5(NAMESPACE, "general-assistant"),
    revision_id=uuid5(NAMESPACE, "general-assistant-revision-1"),
    policy_revision_id=uuid5(NAMESPACE, "ollama-model-policy-1"),
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


class ConversationStore:
    def __init__(self, default_model: str | None = None) -> None:
        self.default_model = default_model
        self._conversations: dict[UUID, Conversation] = {}
        self._idempotency: dict[tuple[str, str, str], object] = {}
        self._idempotency_fingerprints: dict[tuple[str, str, str], str] = {}
        self._locks: dict[UUID, asyncio.Lock] = {}
        self._global_lock = asyncio.Lock()
        self.auth_audit: list[dict[str, object]] = []

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
            if item.principal_subject == subject
            and item.principal_issuer == issuer
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
    ) -> tuple[Conversation, Message, Run]:
        key = (issuer, subject, idempotency_key)
        fingerprint = hashlib.sha256(f"create:{message}:{model_id}".encode()).hexdigest()
        existing = self._idempotency.get(key)
        if existing is not None:
            if self._idempotency_fingerprints.get(key) != fingerprint:
                raise IdempotencyConflict
            if not isinstance(existing, tuple) or len(cast(tuple[object, ...], existing)) != 3:
                raise IdempotencyConflict
            return cast(CommandResult, existing)
        self._validate_model(model_id, models)
        conversation = Conversation(
            principal_issuer=issuer,
            principal_subject=subject,
            title=make_title(message),
            agent_profile_id=GENERAL_AGENT.profile_id,
            agent_revision_id=GENERAL_AGENT.revision_id,
            model_id=model_id,
        )
        user_message = Message(conversation.id, MessageRole.USER, message)
        run = Run(
            conversation_id=conversation.id,
            user_message_id=user_message.id,
            agent_revision_id=GENERAL_AGENT.revision_id,
            model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
            provider="ollama",
            model_id=model_id,
        )
        user_message.run_id = run.id
        conversation.messages.append(user_message)
        conversation.runs.append(run)
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
    ) -> Conversation:
        if issuer is None:
            raise ValueError("issuer is required")
        conversation = await self.get(conversation_id, subject, issuer)
        key = (issuer, subject, idempotency_key)
        existing = self._idempotency.get(key)
        if existing is not None:
            fingerprint = hashlib.sha256(
                f"update:{conversation_id}:{model_id}:{version}".encode()
            ).hexdigest()
            if self._idempotency_fingerprints.get(key) != fingerprint:
                raise IdempotencyConflict
            if not isinstance(existing, Conversation):
                raise IdempotencyConflict
            return existing
        async with await self._lock_for(conversation_id):
            if conversation.version != version:
                raise VersionConflict
            self._validate_model(model_id, models)
            conversation.model_id = model_id
            conversation.version += 1
            conversation.updated_at = now()
        self._idempotency[key] = conversation
        self._idempotency_fingerprints[key] = hashlib.sha256(
            f"update:{conversation_id}:{model_id}:{version}".encode()
        ).hexdigest()
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
            user_message = Message(conversation.id, MessageRole.USER, message)
            run = Run(
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                agent_revision_id=conversation.agent_revision_id,
                model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
                provider="ollama",
                model_id=conversation.model_id,
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
        async with await self._lock_for(conversation.id):
            if conversation.current_run is not None:
                raise ActiveRunConflict
            user_message = next(
                message for message in conversation.messages if message.id == prior.user_message_id
            )
            run = Run(
                conversation_id=conversation.id,
                user_message_id=user_message.id,
                agent_revision_id=conversation.agent_revision_id,
                model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
                provider="ollama",
                model_id=conversation.model_id,
                retry_of_run_id=prior.id,
            )
            conversation.runs.append(run)
            conversation.version += 1
            conversation.updated_at = now()
        result = (conversation, user_message, run)
        self._idempotency[key] = result
        self._idempotency_fingerprints[key] = hashlib.sha256(
            f"retry:{run_id}".encode()
        ).hexdigest()
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

    async def _find_run(
        self, run_id: UUID, subject: str, issuer: str
    ) -> tuple[Conversation, Run]:
        for conversation in self._conversations.values():
            if (
                conversation.principal_subject != subject
                or conversation.principal_issuer != issuer
            ):
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
    ) -> list[tuple[str, str]]:
        conversation = await self.get(conversation_id, subject, issuer)
        return build_context(conversation, GENERAL_AGENT.system_prompt, budget)

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
