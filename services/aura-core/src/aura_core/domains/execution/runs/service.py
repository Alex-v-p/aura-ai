"""Run lifecycle use cases shared by API and worker entrypoints."""

import asyncio
import secrets
from time import perf_counter
from uuid import UUID, uuid4

from aura_core.domains.execution.runs.dto import (
    Run,
    RunClaimLost,
    RunError,
    RunStatus,
)
from aura_core.domains.execution.runs.events import RunEvent, new_event
from aura_core.domains.execution.runs.ports import (
    ChatCompletionPort,
    ChatMessage,
    MetricsPort,
    NullMetrics,
    RunCommandPort,
    RunEventPort,
    RunRepository,
)
from aura_core.domains.interaction.conversations.public import (
    Conversation,
    Message,
    MessageState,
)


class RunExecutionFailure(RuntimeError):
    """Content-free classification for a failed execution boundary."""

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


class RunCoordinator:
    def __init__(
        self,
        store: RunRepository,
        publisher: RunEventPort,
        outbox: RunCommandPort,
        metrics: MetricsPort | None = None,
        worker_id: UUID | None = None,
        lease_seconds: float = 300.0,
        timeout_seconds: float = 300.0,
        cancellation_poll_seconds: float = 0.1,
        context_token_budget: int = 8192,
    ) -> None:
        self.store = store
        self.publisher = publisher
        self.outbox = outbox
        self.metrics = metrics or NullMetrics()
        self.worker_id = worker_id or uuid4()
        self.lease_seconds = lease_seconds
        self.timeout_seconds = timeout_seconds
        self.cancellation_poll_seconds = cancellation_poll_seconds
        self.context_token_budget = context_token_budget

    async def enqueue(self, run: Run) -> None:
        try:
            await self.outbox.publish_run(run.id, run.conversation_id)
        except Exception:
            self._error(run, "queue")
            raise

    async def cancel(
        self, run_id: UUID, subject: str, issuer: str, idempotency_key: str
    ) -> Run:
        run = await self.store.request_cancel(run_id, subject, idempotency_key, issuer)
        return run

    async def execute(self, run_id: UUID, provider: ChatCompletionPort) -> None:
        """Execute one run against the execution-owned model protocol."""
        started = perf_counter()
        span_id = secrets.token_hex(8)
        try:
            await self._execute(run_id, provider, span_id)
        except Exception:
            self.metrics.record_span(
                "aura.execution.run_coordinator",
                "run.execute",
                (perf_counter() - started) * 1000,
                trace_id=run_id.hex,
                span_id=span_id,
                parent_span_id=None,
                dependency="nats_jetstream",
                outcome="error",
                error_class="persistence",
                run_id=str(run_id),
            )
            raise
        self.metrics.record_span(
            "aura.execution.run_coordinator",
            "run.execute",
            (perf_counter() - started) * 1000,
            trace_id=run_id.hex,
            span_id=span_id,
            parent_span_id=None,
            dependency="nats_jetstream",
            outcome="ok",
            run_id=str(run_id),
        )

    async def _execute(
        self,
        run_id: UUID,
        provider: ChatCompletionPort,
        worker_span_id: str,
    ) -> None:
        started = perf_counter()
        try:
            claim = await self.store.start_run(
                run_id, worker_id=self.worker_id, lease_seconds=self.lease_seconds
            )
        except Exception:
            self.metrics.increment(
                "aura.execution.run_coordinator",
                "errors",
                trace_id=run_id.hex,
                run_id=str(run_id),
                error_class="persistence",
            )
            raise
        conversation, run = claim.conversation, claim.run
        # JetStream is at-least-once.  A redelivery after a completed or
        # already-running run must not invoke the provider twice.
        if not claim.acquired:
            if run.status == RunStatus.INTERRUPTED:
                self.metrics.increment(
                    "aura.execution.run_coordinator",
                    "lease_expired",
                    **self._metadata(run),
                )
            elif run.status == RunStatus.CANCELED:
                self.metrics.increment(
                    "aura.execution.run_coordinator",
                    "cancellations",
                    status=run.status.value,
                    **self._metadata(run),
                )
            return
        attempt_id = run.attempt_id
        if attempt_id is None:
            raise RuntimeError("acquired run is missing its attempt identifier")
        self.metrics.increment(
            "aura.execution.run_coordinator",
            "run_started",
            attempt_count=str(run.attempt_count),
            **self._metadata(run),
        )
        if run.started_at is not None:
            self.metrics.observe(
                "aura.execution.run_coordinator",
                "queue_wait_ms",
                max(0.0, (run.started_at - run.created_at).total_seconds() * 1000),
                **self._metadata(run),
            )
        routing_started = perf_counter()
        provider_name = run.provider
        model_id = run.model_id
        routing_span_id = secrets.token_hex(8)
        self.metrics.increment(
            "aura.runtime.model_routing",
            "model_routed",
            provider=provider_name,
            model_id=model_id,
            agent_revision_id=str(run.agent_revision_id),
            model_policy_revision_id=str(run.model_policy_revision_id),
            **self._metadata(run),
        )
        self.metrics.record_span(
            "aura.runtime.model_routing",
            "model.route",
            (perf_counter() - routing_started) * 1000,
            trace_id=run.id.hex,
            span_id=routing_span_id,
            parent_span_id=worker_span_id,
            dependency="model_policy",
            outcome="ok",
            run_id=str(run.id),
            conversation_id=str(run.conversation_id),
            model_id=model_id,
            agent_revision_id=str(run.agent_revision_id),
            model_policy_revision_id=str(run.model_policy_revision_id),
        )
        assistant = next(
            (item for item in conversation.messages if item.id == run.assistant_message_id), None
        )
        output_characters = [0]
        try:
            await self._publish(
                new_event(
                    "run.snapshot",
                    run.id,
                    conversation.id,
                    await self._next_sequence(run.id),
                    {
                        "run": run_payload(run),
                        "assistantMessage": message_payload(assistant) if assistant else None,
                    },
                )
            )
            await self._status(conversation.id, run)
            did_complete = await asyncio.wait_for(
                self._consume_provider(
                    conversation,
                    run,
                    attempt_id,
                    provider,
                    started,
                    output_characters,
                    worker_span_id,
                ),
                timeout=self.timeout_seconds,
            )
            if not did_complete:
                return
            try:
                conversation, run, assistant = await self.store.finish_run(
                    run.id, RunStatus.COMPLETED, attempt_id=attempt_id
                )
            except RunClaimLost:
                raise
            except Exception as exc:
                raise RunExecutionFailure("persistence") from exc
            if assistant is not None:
                await self._publish(
                    new_event(
                        "assistant.snapshot",
                        run.id,
                        conversation.id,
                        await self._next_sequence(run.id),
                        {"message": message_payload(assistant)},
                    )
                )
            await self._status(conversation.id, run)
            self._terminal(run, started)
        except RunClaimLost:
            # A lease reaper has interrupted this attempt.  The stale worker
            # must not append, finish, or automatically restart the run.
            return
        except Exception as exc:  # provider details remain out of the public error
            timed_out = isinstance(exc, TimeoutError)
            error_class = (
                "timeout"
                if timed_out
                else exc.error_class
                if isinstance(exc, RunExecutionFailure)
                else "provider"
            )
            error_code = {
                "delivery": "DELIVERY_ERROR",
                "persistence": "PERSISTENCE_ERROR",
                "provider": "PROVIDER_ERROR",
                "timeout": "RUN_TIMEOUT",
            }[error_class]
            error_message = (
                "The model provider exceeded the configured run timeout."
                if timed_out
                else "The run could not be persisted."
                if error_class == "persistence"
                else "The run update could not be delivered."
                if error_class == "delivery"
                else "The model provider could not complete this run."
            )
            try:
                conversation, run, assistant = await self.store.finish_run(
                    run.id,
                    RunStatus.FAILED,
                    RunError(
                        error_code,
                        error_message,
                        True,
                        run.id.hex,
                    ),
                    attempt_id=attempt_id,
                )
            except RunClaimLost:
                return
            except Exception:
                self._error(run, "persistence")
                raise
            del exc
            self._error(run, error_class)
            if error_class in {"provider", "timeout"}:
                self.metrics.increment(
                    "aura.runtime.model_inference",
                    "provider_errors",
                    error_class=error_class,
                    provider=run.provider,
                    model_id=run.model_id,
                    **self._metadata(run),
                )
            self._terminal(run, started)
            if assistant is not None:
                await self._publish(
                    new_event(
                        "assistant.snapshot",
                        run.id,
                        conversation.id,
                        await self._next_sequence(run.id),
                        {"message": message_payload(assistant)},
                    )
                )
            await self._publish(
                new_event(
                    "run.error",
                    run.id,
                    conversation.id,
                    await self._next_sequence(run.id),
                    {
                        "code": error_code,
                        "message": error_message,
                        "retryable": True,
                        "traceId": run.error.trace_id if run.error else run.id.hex,
                    },
                )
            )
            await self._status(conversation.id, run)
        finally:
            self.metrics.observe(
                "aura.runtime.model_inference",
                "output_tokens",
                0
                if output_characters[0] == 0
                else (output_characters[0] + 3) // 4,
                token_estimator="chars_div_4_ceil",
                provider=run.provider,
                model_id=run.model_id,
                **self._metadata(run),
            )

    async def _consume_provider(
        self,
        conversation: Conversation,
        run: Run,
        attempt_id: UUID,
        provider: ChatCompletionPort,
        started: float,
        output_characters: list[int],
        worker_span_id: str,
    ) -> bool:
        try:
            context = await self.store.context(
                conversation.id,
                conversation.principal_subject,
                conversation.principal_issuer,
                self.context_token_budget,
            )
        except Exception as exc:
            raise RunExecutionFailure("persistence") from exc
        messages = [ChatMessage(role, content) for role, content in context]
        inference_started = perf_counter()
        inference_span_id = secrets.token_hex(8)
        inference_outcome = "error"
        inference_error_class: str | None = "provider"
        first_token = True
        stream = aiter(provider.stream_chat(run.model_id, messages))
        next_chunk: asyncio.Future[str] | None = None
        stop_waiter: asyncio.Task[bool] | None = None
        try:
            while True:
                next_chunk = asyncio.ensure_future(anext(stream))
                stop_waiter = asyncio.create_task(
                    self._wait_until_attempt_stops(run.id, attempt_id)
                )
                done, _ = await asyncio.wait(
                    {next_chunk, stop_waiter}, return_when=asyncio.FIRST_COMPLETED
                )
                if stop_waiter in done:
                    should_cancel = stop_waiter.result()
                    next_chunk.cancel()
                    await asyncio.gather(next_chunk, return_exceptions=True)
                    if should_cancel:
                        inference_outcome = "canceled"
                        inference_error_class = None
                        await self._finish_cancellation(run, attempt_id, started)
                        return False
                    inference_outcome = "skipped"
                    inference_error_class = None
                    raise RunClaimLost
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
                try:
                    chunk = next_chunk.result()
                except StopAsyncIteration:
                    break
                except Exception as exc:
                    raise RunExecutionFailure("provider") from exc
                if first_token:
                    self.metrics.observe(
                        "aura.runtime.model_inference",
                        "time_to_first_token_ms",
                        (perf_counter() - started) * 1000,
                        provider=run.provider,
                        model_id=run.model_id,
                        **self._metadata(run),
                    )
                    first_token = False
                output_characters[0] += len(chunk)
                try:
                    current = await self.store.find_run_any(run.id)
                except Exception as exc:
                    raise RunExecutionFailure("persistence") from exc
                if current[1].status == RunStatus.CANCEL_REQUESTED:
                    inference_outcome = "canceled"
                    inference_error_class = None
                    await self._finish_cancellation(run, attempt_id, started)
                    return False
                if (
                    current[1].status != RunStatus.RUNNING
                    or current[1].attempt_id != attempt_id
                ):
                    inference_outcome = "skipped"
                    inference_error_class = None
                    raise RunClaimLost
                try:
                    assistant = await self.store.append_assistant(
                        run.id, chunk, MessageState.PARTIAL, attempt_id=attempt_id
                    )
                except RunClaimLost:
                    raise
                except Exception as exc:
                    raise RunExecutionFailure("persistence") from exc
                self.metrics.increment(
                    "aura.interaction.conversation_persistence",
                    "checkpoint_persisted",
                    **self._metadata(run),
                )
                event = new_event(
                    "assistant.delta",
                    run.id,
                    conversation.id,
                    await self._next_sequence(run.id),
                    {
                        "messageId": assistant.id,
                        "offset": len(assistant.content) - len(chunk),
                        "text": chunk,
                    },
                )
                await self._publish(event)
            inference_outcome = "ok"
            inference_error_class = None
        except asyncio.CancelledError:
            inference_error_class = "timeout"
            raise
        except RunClaimLost:
            inference_outcome = "skipped"
            inference_error_class = None
            raise
        except RunExecutionFailure as exc:
            inference_error_class = exc.error_class
            raise
        finally:
            if next_chunk is not None and not next_chunk.done():
                next_chunk.cancel()
                await asyncio.gather(next_chunk, return_exceptions=True)
            if stop_waiter is not None and not stop_waiter.done():
                stop_waiter.cancel()
                await asyncio.gather(stop_waiter, return_exceptions=True)
            close = getattr(stream, "aclose", None)
            try:
                if close is not None:
                    await close()
            finally:
                self.metrics.record_span(
                    "aura.runtime.model_inference",
                    "model.infer",
                    (perf_counter() - inference_started) * 1000,
                    trace_id=run.id.hex,
                    span_id=inference_span_id,
                    parent_span_id=worker_span_id,
                    dependency="model_provider",
                    outcome=inference_outcome,
                    error_class=inference_error_class,
                    provider=run.provider,
                    run_id=str(run.id),
                    conversation_id=str(run.conversation_id),
                    model_id=run.model_id,
                )
        return True

    async def _wait_until_attempt_stops(self, run_id: UUID, attempt_id: UUID) -> bool:
        while True:
            await asyncio.sleep(self.cancellation_poll_seconds)
            try:
                _, current = await self.store.find_run_any(run_id)
            except Exception as exc:
                raise RunExecutionFailure("persistence") from exc
            if current.status == RunStatus.CANCEL_REQUESTED:
                return True
            if current.status != RunStatus.RUNNING or current.attempt_id != attempt_id:
                return False

    async def _finish_cancellation(
        self, run: Run, attempt_id: UUID, started: float
    ) -> None:
        try:
            canceled_conversation, canceled_run, _ = await self.store.finish_run(
                run.id, RunStatus.CANCELED, attempt_id=attempt_id
            )
        except RunClaimLost:
            raise
        except Exception as exc:
            raise RunExecutionFailure("persistence") from exc
        await self._status(canceled_conversation.id, canceled_run)
        self.metrics.increment(
            "aura.execution.run_coordinator",
            "cancellations",
            status=canceled_run.status.value,
            **self._metadata(canceled_run),
        )
        self._terminal(canceled_run, started)

    async def publish_reconciled_status(self, run: Run) -> None:
        """Publish a durable terminal status created outside this coordinator."""

        await self._status(run.conversation_id, run)

    async def _next_sequence(self, run_id: UUID) -> int:
        return len(await self.publisher.history(run_id))

    async def _status(self, conversation_id: UUID, run: Run) -> None:
        await self._publish(
            new_event(
                "run.status",
                run.id,
                conversation_id,
                await self._next_sequence(run.id),
                {
                    "status": run.status.value,
                    "startedAt": run.started_at.isoformat() if run.started_at else None,
                    "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
                },
            )
        )
        self.metrics.increment(
            "aura.execution.run_coordinator",
            "run_status",
            status=run.status.value,
            **self._metadata(run),
        )

    async def _publish(self, event: RunEvent) -> None:
        try:
            await self.publisher.publish(event)
        except Exception as exc:
            raise RunExecutionFailure("delivery") from exc

    def _metadata(self, run: Run) -> dict[str, str]:
        return {
            "trace_id": run.id.hex,
            "run_id": str(run.id),
            "conversation_id": str(run.conversation_id),
        }

    def _error(self, run: Run, error_class: str) -> None:
        self.metrics.increment(
            "aura.execution.run_coordinator",
            "errors",
            error_class=error_class,
            **self._metadata(run),
        )

    def _terminal(self, run: Run, started: float) -> None:
        self.metrics.observe(
            "aura.execution.run_coordinator",
            "run_duration_ms",
            (perf_counter() - started) * 1000,
            status=run.status.value,
            **self._metadata(run),
        )


def message_payload(message: Message) -> dict[str, object]:
    return {
        "id": str(message.id),
        "conversationId": str(message.conversation_id),
        "role": message.role.value,
        "content": message.content,
        "state": message.state.value,
        "runId": str(message.run_id) if message.run_id else None,
        "createdAt": message.created_at.isoformat(),
        "updatedAt": message.updated_at.isoformat(),
    }


def run_payload(run: Run) -> dict[str, object]:
    return {
        "id": str(run.id),
        "conversationId": str(run.conversation_id),
        "userMessageId": str(run.user_message_id),
        "assistantMessageId": str(run.assistant_message_id) if run.assistant_message_id else None,
        "status": run.status.value,
        "agentRevisionId": str(run.agent_revision_id),
        "modelPolicyRevisionId": str(run.model_policy_revision_id),
        "provider": run.provider,
        "modelId": run.model_id,
        "retryOfRunId": str(run.retry_of_run_id) if run.retry_of_run_id else None,
        "createdAt": run.created_at.isoformat(),
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "error": {
            "code": run.error.code,
            "message": run.error.message,
            "retryable": run.error.retryable,
            "traceId": run.error.trace_id,
        }
        if run.error
        else None,
    }
