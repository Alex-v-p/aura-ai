"""API composition state."""

import asyncio
from typing import Any, cast
from uuid import UUID

from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.bootstrap.health import ReadinessService
from aura_core.domains.execution.runs.public import RunCoordinator
from aura_core.domains.governance.identity.application import LoginConfiguration, LoginService
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.platform.auth import (
    MemoryLoginStateBackend,
    MemorySessionBackend,
    RedisLoginStateBackend,
    RedisSessionBackend,
    SessionService,
    Settings,
)
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.oidc import OidcClient
from aura_core.platform.outbox import InMemoryOutbox, TransactionalOutboxTransport
from aura_core.platform.outbox.nats import NatsOutbox
from aura_core.platform.readiness import (
    CallableProbe,
    DatabaseProbe,
    DiagnosticProbe,
    OidcDiscoveryProbe,
    StaticProbe,
    UnknownProbe,
)
from aura_core.platform.telemetry import (
    MetadataMetrics,
    Stopwatch,
    StructuredContainerLogExporter,
    TelemetryLifecycle,
    new_span_id,
)
from aura_core.providers.models.ollama.adapter import OllamaAdapter
from aura_core.runtime.models.gateway import ModelGateway
from aura_core.runtime.models.ports import ChatModelPort, ModelDescriptor
from aura_core.runtime.streaming.publisher import EventPublisher, PersistentEventPublisher


class AppState:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        testing: bool = False,
        provider: ChatModelPort | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.testing = testing
        self.provider = provider or OllamaAdapter(
            self.settings.ollama_url, self.settings.ollama_run_timeout_seconds
        )
        self.gateway = ModelGateway(self.provider)
        self.engine = None
        self.sessions_factory = None
        self.sql_store = None
        self.nats: NatsOutbox | None = None
        self.dispatcher_task: asyncio.Task[None] | None = None
        self.startup_errors: dict[str, str] = {}
        self.metrics = MetadataMetrics(
            exporter=None if testing else StructuredContainerLogExporter()
        )
        self.telemetry = TelemetryLifecycle(self.metrics)
        if self.testing:
            self.oidc_states = MemoryLoginStateBackend()
            self.store = ConversationStore(self.settings.default_model or None)
            self.publisher = EventPublisher()
            self.outbox = InMemoryOutbox()
            self.sessions = SessionService(MemorySessionBackend(), self.settings)
            probes = {
                "postgres": UnknownProbe(),
                "nats": UnknownProbe(),
                "oidc": StaticProbe(bool(self.settings.oidc_issuer)),
                "valkey": UnknownProbe(),
                "owner": StaticProbe(
                    bool(self.settings.owner_subject),
                    None if self.settings.owner_subject else "owner subject is not configured",
                ),
                "defaultModel": StaticProbe(
                    bool(self.settings.default_model),
                    None if self.settings.default_model else "default model is not configured",
                ),
                "ollama": CallableProbe(self._model_ready, "provider unavailable"),
            }
        else:
            self.engine = make_engine(self.settings.database_url)
            self.sessions_factory = session_factory(self.engine)
            self.sql_store = SqlConversationStore(self.sessions_factory)
            self.store = self.sql_store
            self.nats = NatsOutbox(self.settings.nats_url)
            self.outbox = TransactionalOutboxTransport()
            self.publisher = PersistentEventPublisher(self.sql_store.persist_event)
            import redis.asyncio

            redis_factory = cast(
                Any,
                redis.asyncio.from_url,  # pyright: ignore[reportUnknownMemberType]
            )

            redis_client = redis_factory(self.settings.valkey_url, decode_responses=True)
            self.sessions = SessionService(RedisSessionBackend(redis_client), self.settings)
            self.oidc_states = RedisLoginStateBackend(redis_client)
            probes = {
                "postgres": DatabaseProbe(self.engine),
                "nats": DiagnosticProbe(self._nats_readiness, "transport unavailable"),
                "oidc": OidcDiscoveryProbe(self.settings.oidc_issuer),
                "valkey": CallableProbe(self._valkey_ready, "Valkey unavailable"),
                "owner": StaticProbe(
                    bool(self.settings.owner_subject),
                    None if self.settings.owner_subject else "owner subject is not configured",
                ),
                "defaultModel": StaticProbe(
                    bool(self.settings.default_model),
                    None if self.settings.default_model else "default model is not configured",
                ),
                "ollama": CallableProbe(self._model_ready, "provider unavailable"),
            }
        self.login = LoginService(
            LoginConfiguration(
                self.settings.oidc_issuer,
                self.settings.oidc_client_id,
                self.settings.oidc_redirect_uri,
                bool(
                    self.settings.oidc_client_id
                    and self.settings.oidc_client_secret
                    and self.settings.oidc_audience
                ),
            ),
            self.oidc_states,
            self.sessions,
            OidcClient(
                issuer=self.settings.oidc_issuer,
                client_id=self.settings.oidc_client_id,
                client_secret=self.settings.oidc_client_secret,
                redirect_uri=self.settings.oidc_redirect_uri,
                audience=self.settings.oidc_audience,
                owner_subject=self.settings.owner_subject,
            ),
            self.store,
        )
        self.readiness = ReadinessService(probes)
        self.coordinator = RunCoordinator(
            self.store,
            self.publisher,
            self.outbox,
            self.metrics,
            lease_seconds=self.settings.ollama_run_timeout_seconds + 30.0,
            timeout_seconds=self.settings.ollama_run_timeout_seconds,
            context_token_budget=self.settings.context_token_budget,
        )

    async def startup(self) -> None:
        if self.testing:
            return
        await self.telemetry.start()
        try:
            assert self.nats is not None
            await self.nats.connect()
            await self.nats.subscribe_events(self._ingest_event_wakeup)
            self.dispatcher_task = asyncio.create_task(self._dispatch_outbox())
        except Exception:
            self.startup_errors["nats"] = "transport unavailable"

    async def shutdown(self) -> None:
        if self.testing:
            return
        if self.dispatcher_task is not None:
            self.dispatcher_task.cancel()
            await asyncio.gather(self.dispatcher_task, return_exceptions=True)
            self.dispatcher_task = None
        if self.nats is not None:
            await self.nats.close()
        backend = self.sessions.backend
        client = getattr(backend, "client", None)
        close = getattr(client, "aclose", None)
        if close is not None:
            await close()
        if self.engine is not None:
            await self.engine.dispose()
        await self.telemetry.stop()

    async def _dispatch_outbox(self) -> None:
        """Publish committed identifier-only commands until shutdown."""

        assert self.sql_store is not None
        assert self.nats is not None
        while True:
            try:
                commands = await self.sql_store.pending_commands()
                for command in commands:
                    metadata = {
                        "trace_id": command.correlation_id.hex
                        if command.correlation_id
                        else command.run_id.hex,
                        "run_id": str(command.run_id),
                        "conversation_id": str(command.conversation_id),
                        "command_id": str(command.id),
                        "correlation_id": str(command.correlation_id or command.run_id),
                    }
                    if command.causation_id is not None:
                        metadata["causation_id"] = str(command.causation_id)
                    dispatch_timer = Stopwatch()
                    dispatch_span_id = new_span_id()
                    try:
                        duplicate = await self.nats.publish(command)
                    except Exception:
                        self.metrics.record_span(
                            "aura.execution.run_coordinator",
                            "outbox.dispatch",
                            dispatch_timer.elapsed_ms(),
                            trace_id=metadata["trace_id"],
                            span_id=dispatch_span_id,
                            parent_span_id=None,
                            dependency="nats_jetstream",
                            outcome="error",
                            error_class="delivery",
                            run_id=metadata["run_id"],
                            conversation_id=metadata["conversation_id"],
                            command_id=metadata["command_id"],
                            correlation_id=metadata["correlation_id"],
                        )
                        raise
                    self.metrics.record_span(
                        "aura.execution.run_coordinator",
                        "outbox.dispatch",
                        dispatch_timer.elapsed_ms(),
                        trace_id=metadata["trace_id"],
                        span_id=dispatch_span_id,
                        parent_span_id=None,
                        dependency="nats_jetstream",
                        outcome="duplicate" if duplicate else "ok",
                        run_id=metadata["run_id"],
                        conversation_id=metadata["conversation_id"],
                        command_id=metadata["command_id"],
                        correlation_id=metadata["correlation_id"],
                    )
                    persistence_timer = Stopwatch()
                    persistence_span_id = new_span_id()
                    await self.sql_store.mark_published(command.id)
                    self.metrics.record_span(
                        "aura.interaction.conversation_persistence",
                        "conversation.persist",
                        persistence_timer.elapsed_ms(),
                        trace_id=metadata["trace_id"],
                        span_id=persistence_span_id,
                        parent_span_id=dispatch_span_id,
                        dependency="postgresql",
                        outcome="ok",
                        run_id=metadata["run_id"],
                        conversation_id=metadata["conversation_id"],
                        command_id=metadata["command_id"],
                    )
                    self.metrics.increment(
                        "aura.execution.run_coordinator",
                        "outbox_dispatched",
                        **metadata,
                    )
                    if duplicate:
                        self.metrics.increment(
                            "aura.execution.run_coordinator",
                            "outbox_duplicates",
                            **metadata,
                        )
                await asyncio.sleep(0.2 if commands else 1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Readiness exposes the dependency failure; the dispatcher
                # keeps retrying so a transient NATS outage is recoverable.
                self.metrics.increment(
                    "aura.execution.run_coordinator",
                    "errors",
                    error_class="delivery",
                )
                await asyncio.sleep(1.0)

    async def _ingest_event_wakeup(self, event_id: UUID, run_id: UUID) -> None:
        """Reload persisted event content before process-local SSE fan-out."""

        if self.sql_store is None:
            return
        event = await self.sql_store.event(event_id, run_id)
        if event is not None:
            await self.publisher.ingest_external(event)

    async def models(self) -> tuple[list[ModelDescriptor], str | None]:
        try:
            models = list(await self.gateway.inventory())
        except Exception:
            models = []
        default = self.settings.default_model or None
        return models, default

    async def _model_ready(self) -> bool:
        return await self.gateway.ready(self.settings.default_model or None)

    async def _nats_ready(self) -> bool:
        return self.nats is not None and await self.nats.is_ready()

    async def _nats_readiness(self) -> tuple[bool, str | None]:
        if self.nats is None:
            return False, "transport unavailable"
        return await self.nats.readiness()

    async def _valkey_ready(self) -> bool:
        backend = self.sessions.backend
        client = getattr(backend, "client", None)
        if client is None:
            return False
        return bool(await client.ping())
