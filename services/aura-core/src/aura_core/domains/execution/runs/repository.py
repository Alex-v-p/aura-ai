"""Session-scoped PostgreSQL run repository."""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from aura_core.domains.execution.runs.dto import Run, RunClaimLost, RunError, RunStatus
from aura_core.domains.execution.runs.events import RunEvent, validate_tool_status
from aura_core.domains.execution.runs.persistence import RunEventRow, RunRow
from aura_core.domains.interaction.conversations.dto import AttachmentSummary, DocumentCitation, now


class SqlRunRepository:
    @staticmethod
    def _run(row: RunRow) -> Run:
        error = row.error or None
        citations = tuple(
            DocumentCitation(
                UUID(str(item["documentId"])),
                "Document source",
                str(item["locator"]),
            )
            for item in (row.citations or [])
            if {"documentId", "locator"} <= set(item)
        )
        return Run(
            conversation_id=row.conversation_id,
            user_message_id=row.user_message_id,
            agent_revision_id=row.agent_revision_id,
            model_policy_revision_id=row.model_policy_revision_id,
            provider=row.provider,
            model_id=row.model_id,
            retry_of_run_id=row.retry_of_run_id,
            status=RunStatus(row.status),
            id=row.id,
            assistant_message_id=row.assistant_message_id,
            created_at=row.created_at or now(),
            started_at=row.started_at,
            finished_at=row.finished_at,
            error=RunError(**cast(dict[str, Any], error)) if error else None,
            attempt_id=row.attempt_id,
            attempt_count=row.attempt_count,
            lease_expires_at=row.lease_expires_at,
            persona_revision_id=row.persona_revision_id,
            prompt_bundle_revision_id=row.prompt_bundle_revision_id,
            prompt_hash=row.prompt_hash,
            memory_policy_revision_id=row.memory_policy_revision_id,
            memory_embedding_generation_id=row.memory_embedding_generation_id,
            memory_recall_metadata=row.memory_recall_metadata,
            tool_policy_revision_id=row.tool_policy_revision_id,
            tool_call_limit=row.tool_call_limit,
            tool_calls_used=row.tool_calls_used,
            tool_timeout_ms=row.tool_timeout_ms,
            tool_retry_limit=row.tool_retry_limit,
            client_time_zone=row.client_time_zone,
            approval_id=row.approval_id,
            pending_tool_call_id=row.pending_tool_call_id,
            tool_lifecycle_status=row.tool_lifecycle_status,
            citations=citations,
            document_search_used=row.document_search_used,
            branch_id=row.branch_id,
            regenerated_from_run_id=row.regenerated_from_run_id,
            attachments=tuple(
                AttachmentSummary(
                    UUID(str(item["linkId"])),
                    str(item["kind"]),
                    UUID(str(item["sourceId"])),
                    str(item["displayFilename"]),
                    str(item["mediaType"]),
                    int(cast(Any, item["byteCount"])),
                    str(item.get("availability", "unavailable")),
                    int(cast(Any, item["width"])) if item.get("width") is not None else None,
                    int(cast(Any, item["height"])) if item.get("height") is not None else None,
                )
                for item in (row.attachments or [])
                if {"linkId", "kind", "sourceId", "displayFilename", "mediaType", "byteCount"}
                <= set(item)
            ),
            workflow_id=row.workflow_id,
            workflow_revision_id=row.workflow_revision_id,
            workflow_instance_id=row.workflow_instance_id,
            workflow_step_ordinal=row.workflow_step_ordinal,
            workflow_step_execution_id=row.workflow_step_execution_id,
        )

    async def get(self, session: AsyncSession, run_id: UUID, *, lock: bool = False) -> Run | None:
        row = await session.get(RunRow, run_id, with_for_update=lock)
        return self._run(row) if row else None

    async def list(self, session: AsyncSession, conversation_id: UUID) -> list[Run]:
        rows = (
            (
                await session.execute(
                    select(RunRow)
                    .where(RunRow.conversation_id == conversation_id)
                    .order_by(RunRow.created_at)
                )
            )
            .scalars()
            .all()
        )
        return [self._run(row) for row in rows]

    async def latest_statuses(
        self, session: AsyncSession, conversation_ids: Sequence[UUID]
    ) -> dict[UUID, RunStatus]:
        """Return latest durable run status for many conversations in one query."""

        if not conversation_ids:
            return {}
        ranked = (
            select(
                RunRow.conversation_id,
                RunRow.status,
                func.row_number()
                .over(
                    partition_by=RunRow.conversation_id,
                    order_by=(RunRow.created_at.desc(), RunRow.id.desc()),
                )
                .label("rank"),
            )
            .where(RunRow.conversation_id.in_(conversation_ids))
            .subquery()
        )
        rows = (
            await session.execute(
                select(ranked.c.conversation_id, ranked.c.status).where(ranked.c.rank == 1)
            )
        ).all()
        return {conversation_id: RunStatus(status) for conversation_id, status in rows}

    async def active(
        self, session: AsyncSession, conversation_id: UUID, *, lock: bool = False
    ) -> Run | None:
        query = select(RunRow).where(
            RunRow.conversation_id == conversation_id,
            RunRow.status.in_(["queued", "running", "cancel_requested"]),
        )
        if lock:
            query = query.with_for_update()
        row = (await session.execute(query)).scalar_one_or_none()
        return self._run(row) if row else None

    async def expired(self, session: AsyncSession, cutoff: datetime) -> list[Run]:
        rows = (
            (
                await session.execute(
                    select(RunRow)
                    .where(
                        RunRow.status == "running",
                        RunRow.lease_expires_at.is_not(None),
                        RunRow.lease_expires_at <= cutoff,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            .scalars()
            .all()
        )
        return [self._run(row) for row in rows]

    def stage(self, session: AsyncSession, run: Run) -> None:
        session.add(
            RunRow(
                id=run.id,
                conversation_id=run.conversation_id,
                user_message_id=run.user_message_id,
                assistant_message_id=run.assistant_message_id,
                agent_revision_id=run.agent_revision_id,
                model_policy_revision_id=run.model_policy_revision_id,
                persona_revision_id=run.persona_revision_id,
                prompt_bundle_revision_id=run.prompt_bundle_revision_id,
                prompt_hash=run.prompt_hash,
                memory_policy_revision_id=run.memory_policy_revision_id,
                memory_embedding_generation_id=run.memory_embedding_generation_id,
                memory_recall_metadata=run.memory_recall_metadata,
                tool_policy_revision_id=run.tool_policy_revision_id,
                tool_call_limit=run.tool_call_limit,
                tool_calls_used=run.tool_calls_used,
                tool_timeout_ms=run.tool_timeout_ms,
                tool_retry_limit=run.tool_retry_limit,
                client_time_zone=run.client_time_zone,
                approval_id=run.approval_id,
                pending_tool_call_id=run.pending_tool_call_id,
                tool_lifecycle_status=run.tool_lifecycle_status,
                citations=[
                    {
                        "documentId": str(item.document_id),
                        "locator": item.locator,
                    }
                    for item in run.citations
                ],
                document_search_used=run.document_search_used,
                provider=run.provider,
                model_id=run.model_id,
                retry_of_run_id=run.retry_of_run_id,
                status=run.status.value,
                error=self._error(run.error),
                attempt_id=run.attempt_id,
                attempt_count=run.attempt_count,
                lease_expires_at=run.lease_expires_at,
                created_at=run.created_at,
                started_at=run.started_at,
                finished_at=run.finished_at,
                branch_id=run.branch_id,
                regenerated_from_run_id=run.regenerated_from_run_id,
                attachments=[
                    {
                        "linkId": str(item.link_id),
                        "kind": item.kind,
                        "sourceId": str(item.source_id),
                        "displayFilename": item.display_filename,
                        "mediaType": item.media_type,
                        "byteCount": item.byte_count,
                        "availability": item.availability,
                        "width": item.width,
                        "height": item.height,
                    }
                    for item in run.attachments
                ],
                workflow_id=run.workflow_id,
                workflow_revision_id=run.workflow_revision_id,
                workflow_instance_id=run.workflow_instance_id,
                workflow_step_ordinal=run.workflow_step_ordinal,
                workflow_step_execution_id=run.workflow_step_execution_id,
            )
        )

    async def save(self, session: AsyncSession, run: Run) -> None:
        row = await session.get(RunRow, run.id, with_for_update=True)
        if row is None:
            return
        row.assistant_message_id = run.assistant_message_id
        row.status = run.status.value
        row.error = self._error(run.error)
        row.attempt_id = run.attempt_id
        row.attempt_count = run.attempt_count
        row.lease_expires_at = run.lease_expires_at
        row.started_at = run.started_at
        row.finished_at = run.finished_at
        row.memory_policy_revision_id = run.memory_policy_revision_id
        row.memory_embedding_generation_id = run.memory_embedding_generation_id
        row.memory_recall_metadata = run.memory_recall_metadata
        row.tool_policy_revision_id = run.tool_policy_revision_id
        row.tool_call_limit = run.tool_call_limit
        row.tool_calls_used = run.tool_calls_used
        row.tool_timeout_ms = run.tool_timeout_ms
        row.tool_retry_limit = run.tool_retry_limit
        row.client_time_zone = run.client_time_zone
        row.approval_id = run.approval_id
        row.pending_tool_call_id = run.pending_tool_call_id
        row.tool_lifecycle_status = run.tool_lifecycle_status
        row.citations = [
            {
                "documentId": str(item.document_id),
                "locator": item.locator,
            }
            for item in run.citations
        ]
        row.document_search_used = run.document_search_used
        row.attachments = [
            {
                "linkId": str(item.link_id),
                "kind": item.kind,
                "sourceId": str(item.source_id),
                "displayFilename": item.display_filename,
                "mediaType": item.media_type,
                "byteCount": item.byte_count,
                "availability": item.availability,
                "width": item.width,
                "height": item.height,
            }
            for item in run.attachments
        ]
        row.workflow_id = run.workflow_id
        row.workflow_revision_id = run.workflow_revision_id
        row.workflow_instance_id = run.workflow_instance_id
        row.workflow_step_ordinal = run.workflow_step_ordinal
        row.workflow_step_execution_id = run.workflow_step_execution_id

    async def increment_tool_calls(
        self, session: AsyncSession, run_id: UUID, attempt_id: UUID
    ) -> int:
        """Atomically consume the one-call run budget for the active attempt."""

        statement = (
            update(RunRow)
            .where(
                RunRow.id == run_id,
                RunRow.status == RunStatus.RUNNING.value,
                RunRow.attempt_id == attempt_id,
                RunRow.tool_calls_used < RunRow.tool_call_limit,
            )
            .values(tool_calls_used=RunRow.tool_calls_used + 1)
            .returning(RunRow.tool_calls_used)
        )
        value = (await session.execute(statement)).scalar_one_or_none()
        if value is None:
            raise RunClaimLost
        return int(value)

    @staticmethod
    def _error(error: RunError | None) -> dict[str, object] | None:
        if error is None:
            return None
        return {
            "code": error.code,
            "message": error.message,
            "retryable": error.retryable,
            "trace_id": error.trace_id,
        }

    async def append_event(self, session: AsyncSession, event: RunEvent) -> RunEvent:
        """Append an event with a transaction-serialized run-local sequence.

        Event producers may construct an event with a placeholder sequence.  The
        run row lock is the serialization point shared by the coordinator and
        memory worker, so concurrent writers cannot both observe the same max
        sequence.  Replays return the original durable event unchanged.
        """

        if event.event_type == "tool.status":
            event = RunEvent(
                event.event_id,
                event.sequence,
                event.event_type,
                event.run_id,
                event.conversation_id,
                event.occurred_at,
                validate_tool_status(event.data),
                event.schema_version,
                event.workflow_id,
                event.workflow_revision_id,
                event.workflow_instance_id,
                event.workflow_step_ordinal,
                event.workflow_step_execution_id,
            )
        existing = await session.get(RunEventRow, event.event_id)
        if existing is not None:
            return self._event(existing)
        run = await session.get(RunRow, event.run_id, with_for_update=True)
        if run is None:
            raise LookupError("run not found for event append")
        value = await session.scalar(
            select(func.max(RunEventRow.sequence)).where(RunEventRow.run_id == event.run_id)
        )
        next_sequence = 0 if value is None else int(value) + 1
        persisted = RunEvent(
            event.event_id,
            next_sequence,
            event.event_type,
            event.run_id,
            event.conversation_id,
            event.occurred_at,
            event.data,
            event.schema_version,
            event.workflow_id,
            event.workflow_revision_id,
            event.workflow_instance_id,
            event.workflow_step_ordinal,
            event.workflow_step_execution_id,
        )
        session.add(
            RunEventRow(
                id=persisted.event_id,
                run_id=persisted.run_id,
                conversation_id=persisted.conversation_id,
                sequence=persisted.sequence,
                event_type=persisted.event_type,
                payload=cast(dict[str, object], persisted.payload()),
                occurred_at=persisted.occurred_at,
                workflow_id=persisted.workflow_id,
                workflow_revision_id=persisted.workflow_revision_id,
                workflow_instance_id=persisted.workflow_instance_id,
                workflow_step_ordinal=persisted.workflow_step_ordinal,
                workflow_step_execution_id=persisted.workflow_step_execution_id,
            )
        )
        return persisted

    async def persist_event(self, session: AsyncSession, event: RunEvent) -> RunEvent:
        """Compatibility alias for the atomic append operation."""

        return await self.append_event(session, event)

    async def event_history(self, session: AsyncSession, run_id: UUID) -> list[RunEvent]:
        rows = (
            (
                await session.execute(
                    select(RunEventRow)
                    .where(RunEventRow.run_id == run_id)
                    .order_by(RunEventRow.sequence)
                )
            )
            .scalars()
            .all()
        )
        return [self._event(row) for row in rows]

    async def event(self, session: AsyncSession, event_id: UUID, run_id: UUID) -> RunEvent | None:
        row = await session.get(RunEventRow, event_id)
        return self._event(row) if row is not None and row.run_id == run_id else None

    @staticmethod
    def _event(row: RunEventRow) -> RunEvent:
        data = cast(dict[str, Any], row.payload.get("data", row.payload))
        if row.event_type == "tool.status":
            data = validate_tool_status(data)
        return RunEvent(
            event_id=row.id,
            sequence=row.sequence,
            event_type=row.event_type,
            run_id=row.run_id,
            conversation_id=row.conversation_id,
            occurred_at=row.occurred_at,
            data=data,
            schema_version=int(cast(int | str, row.payload.get("schemaVersion", 1))),
            workflow_id=row.workflow_id,
            workflow_revision_id=row.workflow_revision_id,
            workflow_instance_id=row.workflow_instance_id,
            workflow_step_ordinal=row.workflow_step_ordinal,
            workflow_step_execution_id=row.workflow_step_execution_id,
        )
