"""Application-level PostgreSQL conversation/run unit of work.

Owner-scoped repositories stage their own rows in one shared SQLAlchemy session.
This coordinator owns the transaction and cross-domain sequencing, but never
imports or manipulates another domain's ORM mapping.
"""

import base64
import hashlib
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from aura_core.domains.execution.runs.events import RunEvent
from aura_core.domains.execution.runs.public import (
    Run,
    RunClaim,
    RunClaimLost,
    RunError,
    RunStatus,
    SqlRunRepository,
)
from aura_core.domains.governance.audit.public import SqlAuditRepository
from aura_core.domains.governance.identity.public import SqlIdentityRepository
from aura_core.domains.interaction.conversations.public import (
    GENERAL_AGENT,
    ActiveRunConflict,
    Conversation,
    ConversationNotFound,
    ConversationRecord,
    IdempotencyConflict,
    Message,
    MessageRole,
    MessageState,
    ModelUnavailable,
    SqlConversationRepository,
    VersionConflict,
    build_context,
    now,
)
from aura_core.platform.outbox import OutboxCommand, SqlOutboxRepository
from aura_core.runtime.models.capacity import DEFAULT_CONTEXT_TOKENS
from aura_core.runtime.models.ports import ModelDescriptor


class SqlConversationStore:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self.sessions = sessions
        self.identities = SqlIdentityRepository()
        self.audit = SqlAuditRepository()
        self.conversations = SqlConversationRepository()
        self.runs = SqlRunRepository()
        self.outbox = SqlOutboxRepository()

    async def record_audit(
        self,
        issuer: str,
        subject: str,
        action: str,
        resource: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            principal = await self.identities.resolve(session, issuer, subject)
            self.audit.stage(session, principal.id, action, resource, metadata)

    async def record_auth_audit(
        self,
        action: str,
        outcome: str,
        *,
        issuer: str | None = None,
        subject: str | None = None,
        metadata: dict[str, object] | None = None,
    ) -> None:
        async with self.sessions() as session, session.begin():
            principal_id = None
            if issuer is not None and subject is not None:
                principal = await self.identities.resolve(session, issuer, subject)
                principal_id = principal.id
            self.audit.stage(
                session,
                principal_id,
                action,
                outcome,
                metadata,
            )

    def _audit(
        self,
        session: AsyncSession,
        principal_id: UUID,
        action: str,
        resource: str,
        metadata: dict[str, object] | None = None,
    ) -> None:
        self.audit.stage(session, principal_id, action, resource, metadata)

    async def persist_event(self, event: RunEvent) -> None:
        async with self.sessions() as session, session.begin():
            await self.runs.persist_event(session, event)

    async def event_history(self, run_id: UUID) -> list[RunEvent]:
        async with self.sessions() as session:
            return await self.runs.event_history(session, run_id)

    async def event(self, event_id: UUID, run_id: UUID) -> RunEvent | None:
        """Reload one authoritative event for an identifier-only wakeup."""

        async with self.sessions() as session:
            return await self.runs.event(session, event_id, run_id)

    async def pending_commands(self, limit: int = 100) -> list[OutboxCommand]:
        """Return committed outbox commands for the API dispatcher.

        A publish can be repeated after a crash; JetStream's ``Nats-Msg-Id``
        deduplicates the stable row ID.  PostgreSQL remains the source of
        truth, so rows are marked published only after the broker accepts them.
        """

        async with self.sessions() as session:
            return await self.outbox.pending(session, limit)

    async def mark_published(self, command_id: UUID) -> None:
        async with self.sessions() as session, session.begin():
            await self.outbox.mark_published(session, command_id)

    async def _load(self, session: AsyncSession, row: ConversationRecord | None) -> Conversation:
        if row is None:
            raise ConversationNotFound
        principal = await self.identities.get(session, row.principal_id)
        if principal is None:
            raise ConversationNotFound
        messages = await self.conversations.messages(session, row.id)
        runs = await self.runs.list(session, row.id)
        conversation = Conversation(
            principal_issuer=principal.issuer,
            principal_subject=principal.subject,
            title=row.title,
            agent_profile_id=row.agent_profile_id,
            agent_revision_id=row.agent_revision_id,
            model_id=row.model_id,
            version=row.version,
            id=row.id,
            created_at=row.created_at or now(),
            updated_at=row.updated_at or now(),
        )
        conversation.messages = messages
        conversation.runs = runs
        return conversation

    async def _row(
        self,
        session: AsyncSession,
        conversation_id: UUID,
        subject: str,
        issuer: str | None = None,
        *,
        lock: bool = False,
    ) -> ConversationRecord:
        if issuer is None:
            raise ValueError("issuer is required")
        principal = await self.identities.find(session, issuer, subject)
        row = await self.conversations.get(
            session, conversation_id, principal.id if principal else UUID(int=0), lock=lock
        )
        if row is None:
            raise ConversationNotFound
        return row

    @staticmethod
    async def _lock_command(
        session: AsyncSession, issuer: str, subject: str, key: str
    ) -> None:
        digest = int.from_bytes(
            hashlib.sha256(f"{issuer}:{subject}:{key}".encode()).digest()[:8], "big"
        )
        signed = digest - (1 << 64) if digest >= (1 << 63) else digest
        await session.execute(text("SELECT pg_advisory_xact_lock(:lock_key)"), {"lock_key": signed})

    @staticmethod
    def _validate_model(model_id: str, models: Iterable[ModelDescriptor]) -> None:
        for model in models:
            if model.id == model_id:
                if not model.selectable:
                    raise ModelUnavailable(model.disabled_reason or "model is not selectable")
                return
        raise ModelUnavailable("model is unavailable")

    async def create(
        self,
        issuer: str,
        subject: str,
        message: str,
        model_id: str,
        models: Iterable[ModelDescriptor],
        idempotency_key: str,
    ) -> tuple[Conversation, Message, Run]:
        self._validate_model(model_id, models)
        fingerprint = hashlib.sha256(f"create:{message}:{model_id}".encode()).hexdigest()
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, idempotency_key)
            prior = await self.conversations.idempotency(
                session, issuer, subject, idempotency_key
            )
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise IdempotencyConflict
                response = prior.response
                row = await self._row(session, UUID(response["conversationId"]), subject, issuer)
                loaded = await self._load(session, row)
                user = next(
                    item for item in loaded.messages if item.id == UUID(response["messageId"])
                )
                run = next(item for item in loaded.runs if item.id == UUID(response["runId"]))
                return loaded, user, run
            principal = await self.identities.resolve(session, issuer, subject)
            conversation_id = UUID(
                int=UUID(
                    bytes=hashlib.sha256(f"{subject}:{idempotency_key}".encode()).digest()[:16]
                ).int
            )
            # UUIDv4 is used when the deterministic id collides with an existing row.
            if await self.conversations.exists(session, conversation_id):
                conversation_id = uuid4()
            user_id = UUID(
                bytes=hashlib.sha256(f"{conversation_id}:message".encode()).digest()[:16]
            )
            run_id = UUID(bytes=hashlib.sha256(f"{conversation_id}:run".encode()).digest()[:16])
            created = now()
            conversation_row = ConversationRecord(
                id=conversation_id,
                principal_id=principal.id,
                title=" ".join(message.split())[:255],
                agent_profile_id=GENERAL_AGENT.profile_id,
                agent_revision_id=GENERAL_AGENT.revision_id,
                model_id=model_id,
                version=1,
                created_at=created,
                updated_at=created,
            )
            user_message = Message(
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content=message,
                state=MessageState.COMPLETE,
                run_id=run_id,
                id=user_id,
                created_at=created,
                updated_at=created,
            )
            run = Run(
                conversation_id=conversation_id,
                user_message_id=user_id,
                agent_revision_id=GENERAL_AGENT.revision_id,
                model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
                provider="ollama",
                model_id=model_id,
                id=run_id,
                created_at=created,
            )
            self.conversations.stage_conversation(session, conversation_row)
            await session.flush()
            self.conversations.stage_message(session, user_message)
            await session.flush()
            self.runs.stage(session, run)
            await session.flush()
            self.outbox.stage(
                session,
                command_id=run_id,
                run_id=run_id,
                conversation_id=conversation_id,
                correlation_id=run_id,
                causation_id=user_id,
            )
            self.conversations.stage_idempotency(
                session,
                issuer,
                subject,
                idempotency_key,
                fingerprint,
                {
                    "conversationId": str(conversation_id),
                    "messageId": str(user_id),
                    "runId": str(run_id),
                },
            )
            self._audit(
                session,
                principal.id,
                "conversation.create",
                str(conversation_id),
                {"runId": str(run_id), "modelId": model_id},
            )
            await session.flush()
            loaded = await self._load(session, conversation_row)
            return loaded, loaded.messages[0], loaded.runs[0]

    async def get(
        self, conversation_id: UUID, subject: str, issuer: str | None = None
    ) -> Conversation:
        if issuer is None:
            raise ValueError("issuer is required")
        async with self.sessions() as session:
            row = await self._row(session, conversation_id, subject, issuer, lock=True)
            conversation = await self._load(session, row)
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
        async with self.sessions() as session:
            principal = await self.identities.find(session, issuer, subject)
            if principal is None:
                return [], None
            rows = await self.conversations.list(
                session,
                principal.id,
                limit + 1,
                decode_cursor(cursor) if cursor else None,
            )
            has_more = len(rows) > limit
            rows = rows[:limit]
            next_cursor = (
                encode_cursor(rows[-1].updated_at, rows[-1].id) if has_more else None
            )
            return [await self._load(session, row) for row in rows], next_cursor

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
        self._validate_model(model_id, models)
        if issuer is None:
            raise ValueError("issuer is required")
        identity_issuer = issuer
        fingerprint = hashlib.sha256(
            f"model:{conversation_id}:{model_id}:{version}".encode()
        ).hexdigest()
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, identity_issuer, subject, idempotency_key)
            prior = await self.conversations.idempotency(
                session, identity_issuer, subject, idempotency_key
            )
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise IdempotencyConflict
                response = prior.response
                row = await self._row(session, UUID(response["conversationId"]), subject, issuer)
                return await self._load(session, row)
            row = await self._row(session, conversation_id, subject, issuer, lock=True)
            if row.version != version:
                raise VersionConflict
            row.model_id, row.version, row.updated_at = model_id, version + 1, now()
            await self.conversations.update(session, row)
            self.conversations.stage_idempotency(
                session,
                identity_issuer,
                subject,
                idempotency_key,
                fingerprint,
                {"conversationId": str(row.id)},
            )
            self._audit(
                session,
                row.principal_id,
                "conversation.model.update",
                str(row.id),
                {"modelId": model_id, "version": row.version},
            )
            return await self._load(session, row)

    async def add_run(
        self,
        conversation_id: UUID,
        subject: str,
        message: str,
        expected_version: int,
        idempotency_key: str,
        issuer: str | None = None,
    ) -> tuple[Conversation, Message, Run]:
        fingerprint = hashlib.sha256(
            f"run:{conversation_id}:{message}:{expected_version}".encode()
        ).hexdigest()
        if issuer is None:
            raise ValueError("issuer is required")
        identity_issuer = issuer
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, identity_issuer, subject, idempotency_key)
            prior = await self.conversations.idempotency(
                session, identity_issuer, subject, idempotency_key
            )
            if prior is not None:
                if prior.fingerprint != fingerprint:
                    raise IdempotencyConflict
                response = prior.response
                row = await self._row(session, UUID(response["conversationId"]), subject, issuer)
                loaded = await self._load(session, row)
                user = next(
                    item for item in loaded.messages if item.id == UUID(response["messageId"])
                )
                run = next(item for item in loaded.runs if item.id == UUID(response["runId"]))
                return loaded, user, run
            row = await self._row(session, conversation_id, subject, issuer, lock=True)
            active = await self.runs.active(session, conversation_id, lock=True)
            if row.version != expected_version or active is not None:
                raise VersionConflict if row.version != expected_version else ActiveRunConflict
            user_id, run_id = uuid4(), uuid4()
            created = now()
            user_message = Message(
                conversation_id=conversation_id,
                role=MessageRole.USER,
                content=message,
                state=MessageState.COMPLETE,
                run_id=run_id,
                id=user_id,
                created_at=created,
                updated_at=created,
            )
            run = Run(
                conversation_id=conversation_id,
                user_message_id=user_id,
                agent_revision_id=row.agent_revision_id,
                model_policy_revision_id=GENERAL_AGENT.policy_revision_id,
                provider="ollama",
                model_id=row.model_id,
                id=run_id,
                created_at=created,
            )
            self.conversations.stage_message(session, user_message)
            await session.flush()
            self.runs.stage(session, run)
            await session.flush()
            self.outbox.stage(
                session,
                command_id=run_id,
                run_id=run_id,
                conversation_id=conversation_id,
                correlation_id=run_id,
                causation_id=user_id,
            )
            self.conversations.stage_idempotency(
                session,
                identity_issuer,
                subject,
                idempotency_key,
                fingerprint,
                {
                    "conversationId": str(conversation_id),
                    "messageId": str(user_id),
                    "runId": str(run_id),
                },
            )
            self._audit(
                session,
                row.principal_id,
                "conversation.run.create",
                str(conversation_id),
                {"runId": str(run_id)},
            )
            row.version, row.updated_at = row.version + 1, now()
            await self.conversations.update(session, row)
            await session.flush()
            loaded = await self._load(session, row)
            return (
                loaded,
                next(item for item in loaded.messages if item.id == user_id),
                next(item for item in loaded.runs if item.id == run_id),
            )

    async def request_cancel(
        self,
        run_id: UUID,
        subject: str,
        idempotency_key: str,
        issuer: str | None = None,
    ) -> Run:
        if issuer is None:
            raise ValueError("issuer is required")
        fingerprint = hashlib.sha256(f"cancel:{run_id}".encode()).hexdigest()
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, issuer, subject, idempotency_key)
            prior = await self.conversations.idempotency(
                session, issuer, subject, idempotency_key
            )
            if prior is not None and prior.fingerprint != fingerprint:
                raise IdempotencyConflict
            run = await self.runs.get(session, run_id, lock=True)
            if run is None:
                raise ConversationNotFound
            conversation_row = await self._row(
                session, run.conversation_id, subject, issuer, lock=True
            )
            if run.status in {RunStatus.QUEUED, RunStatus.RUNNING}:
                run.status = RunStatus.CANCEL_REQUESTED
                await self.runs.save(session, run)
            if prior is None:
                self.conversations.stage_idempotency(
                    session,
                    issuer,
                    subject,
                    idempotency_key,
                    fingerprint,
                    {"runId": str(run.id)},
                )
                self._audit(
                    session,
                    conversation_row.principal_id,
                    "conversation.run.cancel",
                    str(run.id),
                )
            return run

    async def retry(
        self, run_id: UUID, subject: str, idempotency_key: str, issuer: str | None = None
    ) -> tuple[Conversation, Message, Run]:
        fingerprint = hashlib.sha256(f"retry:{run_id}".encode()).hexdigest()
        if issuer is None:
            raise ValueError("issuer is required")
        identity_issuer = issuer
        async with self.sessions() as session, session.begin():
            await self._lock_command(session, identity_issuer, subject, idempotency_key)
            prior_command = await self.conversations.idempotency(
                session, identity_issuer, subject, idempotency_key
            )
            if prior_command is not None:
                if prior_command.fingerprint != fingerprint:
                    raise IdempotencyConflict
                response = prior_command.response
                row = await self._row(session, UUID(response["conversationId"]), subject, issuer)
                loaded = await self._load(session, row)
                user = next(
                    item for item in loaded.messages if item.id == UUID(response["messageId"])
                )
                run = next(item for item in loaded.runs if item.id == UUID(response["runId"]))
                return loaded, user, run
            prior = await self.runs.get(session, run_id)
            if prior is None:
                raise ConversationNotFound
            conversation_row = await self._row(
                session, prior.conversation_id, subject, issuer, lock=True
            )
            active = await self.runs.active(session, prior.conversation_id)
            if active is not None:
                raise ActiveRunConflict
            user_id, new_id = prior.user_message_id, uuid4()
            retried = Run(
                conversation_id=prior.conversation_id,
                user_message_id=user_id,
                agent_revision_id=prior.agent_revision_id,
                model_policy_revision_id=prior.model_policy_revision_id,
                provider=prior.provider,
                model_id=prior.model_id,
                retry_of_run_id=prior.id,
                id=new_id,
            )
            self.runs.stage(session, retried)
            await session.flush()
            conversation_row.version += 1
            conversation_row.updated_at = now()
            await self.conversations.update(session, conversation_row)
            self.outbox.stage(
                session,
                command_id=new_id,
                run_id=new_id,
                conversation_id=prior.conversation_id,
                correlation_id=prior.retry_of_run_id or prior.id,
                causation_id=prior.id,
            )
            self.conversations.stage_idempotency(
                session,
                identity_issuer,
                subject,
                idempotency_key,
                fingerprint,
                {
                    "conversationId": str(prior.conversation_id),
                    "messageId": str(user_id),
                    "runId": str(new_id),
                },
            )
            self._audit(
                session,
                conversation_row.principal_id,
                "conversation.run.retry",
                str(new_id),
                {"retryOfRunId": str(prior.id)},
            )
            await session.flush()
            loaded = await self._load(session, conversation_row)
            return (
                loaded,
                next(item for item in loaded.messages if item.id == user_id),
                next(item for item in loaded.runs if item.id == new_id),
            )

    async def find_run(
        self, run_id: UUID, subject: str, issuer: str | None = None
    ) -> tuple[Conversation, Run]:
        if issuer is None:
            raise ValueError("issuer is required")
        async with self.sessions() as session:
            run = await self.runs.get(session, run_id)
            if run is None:
                raise ConversationNotFound
            row = await self._row(session, run.conversation_id, subject, issuer)
            return await self._load(session, row), run

    async def find_run_any(self, run_id: UUID) -> tuple[Conversation, Run]:
        async with self.sessions() as session:
            run = await self.runs.get(session, run_id)
            if run is None:
                raise ConversationNotFound
            return await self._load(
                session, await self.conversations.get(session, run.conversation_id)
            ), run

    async def start_run(
        self,
        run_id: UUID,
        *,
        worker_id: UUID | None = None,
        lease_seconds: float = 300.0,
    ) -> RunClaim:
        async with self.sessions() as session, session.begin():
            run = await self.runs.get(session, run_id, lock=True)
            if run is None:
                raise ConversationNotFound
            current = now()
            acquired = False
            if run.status == RunStatus.CANCEL_REQUESTED:
                run.status, run.finished_at = RunStatus.CANCELED, current
            elif run.status == RunStatus.QUEUED:
                run.status, run.started_at = RunStatus.RUNNING, current
                run.attempt_id = worker_id or uuid4()
                run.attempt_count += 1
                run.lease_expires_at = current + timedelta(seconds=lease_seconds)
                acquired = True
            elif (
                run.status == RunStatus.RUNNING
                and run.lease_expires_at is not None
                and run.lease_expires_at <= current
            ):
                run.status, run.finished_at = RunStatus.INTERRUPTED, current
                run.error = RunError(
                    "WORKER_LEASE_EXPIRED",
                    "The worker lease expired before this run completed.",
                    True,
                    run.id.hex,
                )
                run.lease_expires_at = None
                if run.assistant_message_id:
                    await self.conversations.set_message_state(
                        session, run.assistant_message_id, MessageState.INTERRUPTED
                    )
            await self.runs.save(session, run)
            conversation = await self._load(
                session, await self.conversations.get(session, run.conversation_id)
            )
            return RunClaim(
                conversation,
                run,
                next(item for item in conversation.messages if item.id == run.user_message_id),
                acquired,
            )

    async def expire_leases(self) -> list[Run]:
        """Mark abandoned runs interrupted without restarting inference."""

        expired: list[Run] = []
        async with self.sessions() as session, session.begin():
            rows = await self.runs.expired(session, now())
            for run in rows:
                run.status, run.finished_at = RunStatus.INTERRUPTED, now()
                run.error = RunError(
                    "WORKER_LEASE_EXPIRED",
                    "The worker lease expired before this run completed.",
                    True,
                    run.id.hex,
                )
                run.lease_expires_at = None
                await self.runs.save(session, run)
                if run.assistant_message_id:
                    await self.conversations.set_message_state(
                        session, run.assistant_message_id, MessageState.INTERRUPTED
                    )
                expired.append(run)
        return expired

    async def append_assistant(
        self,
        run_id: UUID,
        text: str,
        state: MessageState = MessageState.PARTIAL,
        *,
        attempt_id: UUID | None = None,
    ) -> Message:
        async with self.sessions() as session, session.begin():
            run = await self.runs.get(session, run_id, lock=True)
            if run is None:
                raise ConversationNotFound
            if attempt_id is not None and (
                run.status != RunStatus.RUNNING or run.attempt_id != attempt_id
            ):
                raise RunClaimLost
            message = (
                await self.conversations.message(session, run.assistant_message_id)
                if run.assistant_message_id
                else None
            )
            if message is None:
                message = Message(
                    conversation_id=run.conversation_id,
                    role=MessageRole.ASSISTANT,
                    content=text,
                    state=state,
                    run_id=run_id,
                )
                self.conversations.stage_message(session, message)
                await session.flush()
                run.assistant_message_id = message.id
                await self.runs.save(session, run)
            else:
                updated = await self.conversations.append_message(session, message.id, text, state)
                if updated is not None:
                    message = updated
            return message

    async def finish_run(
        self,
        run_id: UUID,
        status: RunStatus,
        error: RunError | None = None,
        *,
        attempt_id: UUID | None = None,
    ) -> tuple[Conversation, Run, Message | None]:
        async with self.sessions() as session, session.begin():
            run = await self.runs.get(session, run_id, lock=True)
            if run is None:
                raise ConversationNotFound
            if attempt_id is not None and (
                run.attempt_id != attempt_id
                or run.status not in {RunStatus.RUNNING, RunStatus.CANCEL_REQUESTED}
            ):
                raise RunClaimLost
            run.status, run.finished_at = status, now()
            run.lease_expires_at = None
            run.error = error
            await self.runs.save(session, run)
            if run.assistant_message_id:
                message_state = (
                    MessageState.COMPLETE
                    if status == RunStatus.COMPLETED
                    else MessageState.INTERRUPTED
                    if status in {RunStatus.CANCELED, RunStatus.INTERRUPTED}
                    else MessageState.FAILED
                )
                await self.conversations.set_message_state(
                    session, run.assistant_message_id, message_state
                )
            conversation = await self._load(
                session, await self.conversations.get(session, run.conversation_id)
            )
            return (
                conversation,
                run,
                next(
                    (item for item in conversation.messages if item.id == run.assistant_message_id),
                    None,
                ),
            )

    async def context(
        self,
        conversation_id: UUID,
        subject: str,
        issuer: str,
        budget: int = DEFAULT_CONTEXT_TOKENS,
    ) -> list[tuple[str, str]]:
        conversation = await self.get(conversation_id, subject, issuer)
        return build_context(conversation, GENERAL_AGENT.system_prompt, budget)


def encode_cursor(updated_at: datetime, conversation_id: UUID) -> str:
    raw = f"{updated_at.isoformat()}|{conversation_id}"
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def decode_cursor(cursor: str) -> tuple[datetime, UUID]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        timestamp, identifier = base64.urlsafe_b64decode(padded.encode()).decode().split("|", 1)
        parsed = datetime.fromisoformat(timestamp)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed, UUID(identifier)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("invalid conversation cursor") from exc
