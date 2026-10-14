"""API composition state."""

# ruff: noqa: E501

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any, cast
from uuid import UUID

from aura_core.bootstrap import memory_uow
from aura_core.bootstrap.approval_uow import approval_repository, approval_service
from aura_core.bootstrap.conversation_uow import SqlConversationStore
from aura_core.bootstrap.document_uow import SqlDocumentCommandSink, document_service
from aura_core.bootstrap.health import ReadinessService
from aura_core.bootstrap.notification_uow import notification_repository, notification_service
from aura_core.bootstrap.reminder_uow import reminder_repository, reminder_service
from aura_core.domains.automation.notifications.public import (
    NOTIFICATION_COMPONENT,
    NOTIFICATION_TOPIC,
    NotificationApplicationService,
    NotificationRepository,
)
from aura_core.domains.automation.reminders.public import (
    REMINDER_TOPIC,
    ReminderApplicationService,
    ReminderRepository,
)
from aura_core.domains.execution.runs.public import RunCoordinator
from aura_core.domains.governance.approvals.public import (
    ApprovalApplicationService,
    ApprovalRepository,
)
from aura_core.domains.governance.identity.application import LoginConfiguration, LoginService
from aura_core.domains.interaction.agents.adapters import SqlAgentStore
from aura_core.domains.interaction.agents.public import (
    AgentCatalog,
    AgentConfigurationService,
    AgentMemoryRepository,
    ToolGrant,
    ToolGrantPolicy,
    ToolGrantResolver,
    ToolPolicySnapshot,
)
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.domains.interaction.personas.adapters import SqlPersonaStore
from aura_core.domains.interaction.personas.public import (
    PersonaCatalog,
    PersonaConfigurationService,
    PersonaMemoryRepository,
    PersonaPreviewError,
    PersonaPreviewPort,
    PersonaPreviewResult,
    PersonaPreviewTrace,
)
from aura_core.domains.knowledge.documents.public import DocumentService
from aura_core.domains.knowledge.memory import public as memory_public
from aura_core.domains.knowledge.memory.public import MemoryRepository
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
from aura_core.platform.outbox import (
    APPROVAL_EVENT_TOPIC,
    RUN_RESUME_TOPIC,
    InMemoryOutbox,
    OutboxCommand,
    TransactionalOutboxTransport,
    identifier_trace_metadata,
)
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
from aura_core.providers.embeddings.ollama.adapter import OllamaEmbeddingAdapter
from aura_core.providers.models.ollama.adapter import OllamaAdapter
from aura_core.runtime.models.gateway import ModelGateway, ModelPreviewFailure
from aura_core.runtime.models.ports import (
    ChatModelPort,
    ModelDescriptor,
    ModelSelectionPort,
    ProviderTraceContext,
)
from aura_core.runtime.streaming.publisher import EventPublisher, PersistentEventPublisher
from aura_core.runtime.tools import ToolRegistry, default_tool_registry


class _ToolRegistryGrantResolver(ToolGrantResolver):
    """Resolve client grant identifiers from the composition-owned registry."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry

    def resolve(self, grants: Sequence[ToolGrant]) -> ToolPolicySnapshot:
        if len(set(grants)) != len(grants):
            raise ValueError("tool grants must be unique")
        resolved: list[ToolGrantPolicy] = []
        for grant in grants:
            descriptor = self.registry.get(grant.tool_id, grant.tool_version)
            if descriptor is None:
                raise ValueError("tool grant is not registered")
            resolved.append(
                ToolGrantPolicy(
                    grant=grant,
                    permission_scope=descriptor.permission_scope,
                    confirmation=descriptor.confirmation,
                    timeout_ms=descriptor.timeout_ms,
                    retry_limit=descriptor.retry_limit,
                )
            )
        return ToolPolicySnapshot(
            grants=tuple(resolved),
            permission_scopes=frozenset(item.permission_scope for item in resolved),
            max_calls_per_run=1 if resolved else 0,
            timeout_ms=max((item.timeout_ms for item in resolved), default=30_000),
            retry_limit=max((item.retry_limit for item in resolved), default=0),
        )


class _GatewayPersonaPreviewPort(PersonaPreviewPort):
    def __init__(self, gateway: ModelGateway) -> None:
        self.gateway = gateway

    async def preview(
        self,
        model_id: str,
        system_prompt: str,
        prompt: str,
        *,
        trace: PersonaPreviewTrace,
    ) -> PersonaPreviewResult:
        try:
            result = await self.gateway.preview_with_metadata(
                model_id,
                system_prompt,
                prompt,
                context=ProviderTraceContext(trace_id=trace.trace_id, span_id=trace.span_id),
            )
        except ModelPreviewFailure as exc:
            provider = exc.provider
            if provider is None and exc.error_class in {"provider", "timeout", "unknown"}:
                provider = "unknown"
            raise PersonaPreviewError(exc.error_class, provider=provider) from exc
        return PersonaPreviewResult(result.content, result.provider)


class AppState:
    @property
    def provider(self) -> ChatModelPort:
        return self._provider

    @provider.setter
    def provider(self, value: ChatModelPort) -> None:
        self._provider = value
        if hasattr(self, "gateway"):
            self.gateway = ModelGateway(value, self.metrics)
        memory_service = getattr(self, "memory_model_service", None)
        if memory_service is not None:
            memory_service.provider = value

    async def resume_approval(self, approval: object) -> object:
        """Queue a waiting run after one durable approval decision."""

        run_id = getattr(approval, "run_id", None)
        approval_id = getattr(approval, "id", None)
        if not isinstance(run_id, UUID) or not isinstance(approval_id, UUID):
            raise ValueError("approval identifiers are required")
        status = getattr(
            getattr(approval, "status", None), "value", getattr(approval, "status", None)
        )
        approved = status == "approved"
        run = await self.store.resume_waiting_run(run_id, approval_id, approved=approved)
        await self.coordinator.enqueue(run)
        return run

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        testing: bool = False,
        provider: ChatModelPort | None = None,
    ) -> None:
        self.settings = settings or Settings()
        self.testing = testing
        self.metrics = MetadataMetrics(
            exporter=None if testing else StructuredContainerLogExporter()
        )
        self.telemetry = TelemetryLifecycle(self.metrics)
        self.provider = provider or OllamaAdapter(
            self.settings.ollama_url,
            self.settings.ollama_run_timeout_seconds,
            telemetry=self.metrics,
            inventory_timeout_seconds=float(
                getattr(self.settings, "ollama_inventory_timeout_seconds", 5.0)
            ),
            verification_timeout_seconds=float(
                getattr(self.settings, "ollama_verification_timeout_seconds", 60.0)
            ),
            inventory_cache_ttl_seconds=float(
                getattr(self.settings, "ollama_inventory_cache_ttl_seconds", 15.0)
            ),
        )
        self.gateway = ModelGateway(self.provider, self.metrics)
        self.tool_registry = default_tool_registry()
        self.tool_grant_resolver = _ToolRegistryGrantResolver(self.tool_registry)
        self.engine = None
        self.sessions_factory = None
        self.sql_store = None
        self.agent_store = None
        self.persona_store = None
        self.agent_service: AgentConfigurationService
        self.persona_service: PersonaConfigurationService
        self.memory_repository: MemoryRepository
        self.reminder_repository: ReminderRepository
        self.reminder_service: ReminderApplicationService
        self.notification_repository: NotificationRepository
        self.notification_service: NotificationApplicationService
        self.approval_repository: ApprovalRepository
        self.approval_service: ApprovalApplicationService
        self.memory_worker: object | None = None
        self.document_service: DocumentService
        self.memory_model_service: memory_public.MemoryModelApplicationService
        self.nats: NatsOutbox | None = None
        self.dispatcher_task: asyncio.Task[None] | None = None
        self.startup_errors: dict[str, str] = {}
        self.persona_catalog = PersonaCatalog()
        self.personas = self.persona_catalog
        self.agents = AgentCatalog(self.persona_catalog)
        if self.testing:
            self.oidc_states = MemoryLoginStateBackend()
            self.store = ConversationStore(
                self.settings.default_model or None, self.agents, self.personas
            )
            self.persona_repository = PersonaMemoryRepository(self.persona_catalog)
            self.persona_service = PersonaConfigurationService(
                self.persona_repository,
                preview_port=_GatewayPersonaPreviewPort(self.gateway),
                metrics=self.metrics,
            )
            self.agent_repository = AgentMemoryRepository(self.agents)
            self.agent_service = AgentConfigurationService(
                self.agent_repository,
                self.persona_service,
                tool_grant_resolver=self.tool_grant_resolver,
            )
            self.persona_service.audit = self.store.record_auth_audit
            self.agent_service.audit = self.store.record_auth_audit
            self.publisher = EventPublisher(metrics=self.metrics)
            self.outbox = InMemoryOutbox()
            self.sessions = SessionService(MemorySessionBackend(), self.settings)
            self.memory_repository = memory_uow.memory_repository(
                None, testing=True, metrics=self.metrics
            )
            self.reminder_repository = reminder_repository(None, testing=True, metrics=self.metrics)
            self.reminder_service = reminder_service(self.reminder_repository)
            self.notification_repository = notification_repository(
                None,
                testing=True,
                reminder_repository=self.reminder_repository,
                metrics=self.metrics,
            )
            self.notification_service = notification_service(self.notification_repository)
            self.approval_repository = approval_repository(None, testing=True, metrics=self.metrics)
            self.approval_service = approval_service(
                self.approval_repository,
            )

            async def get_memory_activity(run_id: UUID, issuer: str, subject: str) -> object:
                return await self.memory_repository.get_run_memory_activity(run_id, issuer, subject)

            self.store.get_run_memory_activity = get_memory_activity  # type: ignore[attr-defined]
            self.memory_worker = memory_uow.memory_processing_service(
                None, settings=self.settings, metrics=self.metrics
            )
            self.document_service = document_service(
                None, settings=self.settings, testing=True, metrics=self.metrics
            )
            self.store.set_memory_recall(
                memory_uow.memory_recall_service(
                    self.memory_repository, settings=self.settings, metrics=self.metrics
                )
            )
            command_factory = getattr(memory_uow, "memory_command_factory", None)
            if callable(command_factory):
                self.store.set_memory_command_factory(
                    cast(Callable[..., OutboxCommand], command_factory), self.outbox
                )
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
            self.persona_store = SqlPersonaStore(self.sessions_factory, self.persona_catalog)
            self.persona_service = PersonaConfigurationService(
                self.persona_store,
                preview_port=_GatewayPersonaPreviewPort(self.gateway),
                metrics=self.metrics,
            )
            self.sql_store = SqlConversationStore(
                self.sessions_factory,
                self.agents,
                persona_query=self.personas,
                persona_admission=self.persona_service,
            )
            self.agent_store = SqlAgentStore(
                self.sessions_factory,
                self.agents,
                self.persona_service,  # type: ignore[arg-type]
            )
            self.agent_service = AgentConfigurationService(
                self.agent_store,
                self.persona_service,
                tool_grant_resolver=self.tool_grant_resolver,
            )
            self.sql_store.agent_store = self.agent_store
            self.store = self.sql_store
            memory_topic = str(
                getattr(memory_public, "MEMORY_PROCESSING_TOPIC", "aura.memory.process.v1")
            )
            self.nats = NatsOutbox(
                self.settings.nats_url,
                additional_subjects=(
                    memory_topic,
                    REMINDER_TOPIC,
                    NOTIFICATION_TOPIC,
                    APPROVAL_EVENT_TOPIC,
                    RUN_RESUME_TOPIC,
                    "aura.knowledge.documents.events.v1",
                ),
                metrics=self.metrics,
            )
            self.outbox = TransactionalOutboxTransport()
            self.publisher = PersistentEventPublisher(
                self.sql_store.append_event,
                metrics=self.metrics,
            )
            import redis.asyncio

            redis_factory = cast(
                Any,
                redis.asyncio.from_url,  # pyright: ignore[reportUnknownMemberType]
            )

            redis_client = redis_factory(self.settings.valkey_url, decode_responses=True)
            self.sessions = SessionService(RedisSessionBackend(redis_client), self.settings)
            self.oidc_states = RedisLoginStateBackend(redis_client)
            self.memory_repository = memory_uow.memory_repository(
                self.sessions_factory, metrics=self.metrics
            )
            self.reminder_repository = reminder_repository(
                self.sessions_factory, metrics=self.metrics
            )
            self.reminder_service = reminder_service(self.reminder_repository)
            self.notification_repository = notification_repository(
                self.sessions_factory,
                reminder_delivery=self.reminder_repository,
                metrics=self.metrics,
            )
            self.notification_service = notification_service(self.notification_repository)
            self.approval_repository = approval_repository(
                self.sessions_factory, metrics=self.metrics, execution_port=self.store
            )
            self.approval_service = approval_service(
                self.approval_repository, execution_port=self.store
            )
            cast(Any, self.memory_repository).set_run_recall_metadata_loader(
                self.sql_store.get_run_memory_recall_metadata
            )
            agent_policy_loader = memory_uow.make_agent_policy_loader(self.agent_service.repository)
            cast(Any, self.memory_repository).set_candidate_evidence_loader(
                memory_uow.memory_evidence_loader(
                    self.sessions_factory,
                    self.memory_repository,
                    agent_policy_loader=agent_policy_loader,
                )
            )
            self.sql_store.set_memory_activity_repository(self.memory_repository)
            self.memory_worker = memory_uow.memory_processing_service(
                self.sessions_factory,
                settings=self.settings,
                metrics=self.metrics,
                agent_policy_loader=agent_policy_loader,
            )
            self.document_service = document_service(
                self.sessions_factory,
                settings=self.settings,
                metrics=self.metrics,
                commands=SqlDocumentCommandSink(self.sessions_factory),
                embedder=OllamaEmbeddingAdapter(
                    self.settings.ollama_url,
                    self.settings.ollama_run_timeout_seconds,
                    telemetry=self.metrics,
                    dimension=self.settings.document_embedding_dimension,
                ),
            )
            self.sql_store.set_memory_recall(
                memory_uow.memory_recall_service(
                    self.memory_repository, settings=self.settings, metrics=self.metrics
                )
            )
            command_factory = getattr(memory_uow, "memory_command_factory", None)
            if callable(command_factory):
                self.sql_store.set_memory_command_factory(
                    cast(Callable[..., OutboxCommand], command_factory)
                )
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
        self.memory_model_service = memory_public.MemoryModelApplicationService(
            self.memory_repository, cast(ModelSelectionPort, self.provider), self.memory_worker
        )
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
        thinking_publisher = self.publisher if self.testing else self.nats
        self.coordinator = RunCoordinator(
            self.store,
            self.publisher,
            self.outbox,
            self.metrics,
            lease_seconds=self.settings.ollama_run_timeout_seconds + 30.0,
            timeout_seconds=self.settings.ollama_run_timeout_seconds,
            context_token_budget=self.settings.context_token_budget,
            tool_registry=self.tool_registry,
            approval_service=getattr(self, "approval_service", None),
            reminder_service=self.reminder_service,
            document_service=self.document_service,
            thinking_publisher=thinking_publisher,
        )

    async def startup(self) -> None:
        if self.testing:
            return
        await self.telemetry.start()
        try:
            if self.agent_store is not None:
                if self.persona_store is not None:
                    await self.persona_store.refresh()
                await self.agent_store.refresh()
            assert self.nats is not None
            await self.nats.connect()
            await self.nats.subscribe_events(self._ingest_event_wakeup)
            thinking_subscribe = getattr(self.nats, "subscribe_thinking_events", None)
            if callable(thinking_subscribe):
                subscribe = cast(
                    Callable[
                        [Callable[[UUID, UUID, int, str], Awaitable[None]]], Awaitable[object]
                    ],
                    thinking_subscribe,
                )
                await subscribe(self._ingest_thinking_wakeup)
            notification_subscribe = getattr(self.nats, "subscribe_notification_events", None)
            if callable(notification_subscribe):
                subscribe = cast(
                    Callable[[Callable[[UUID], Awaitable[None]]], Awaitable[object]],
                    notification_subscribe,
                )
                await subscribe(self._ingest_notification_wakeup)
            approval_subscribe = getattr(self.nats, "subscribe_approval_events", None)
            if callable(approval_subscribe):
                subscribe = cast(
                    Callable[[Callable[[UUID], Awaitable[None]]], Awaitable[object]],
                    approval_subscribe,
                )
                await subscribe(self._ingest_approval_wakeup)
            resumption_subscribe = getattr(self.nats, "subscribe_resumption_events", None)
            if callable(resumption_subscribe):
                subscribe = cast(
                    Callable[[Callable[[UUID], Awaitable[None]]], Awaitable[object]],
                    resumption_subscribe,
                )
                await subscribe(self._ingest_resumption_wakeup)
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
        active_component = "aura.execution.run_coordinator"
        reminder_persistence_failure = False
        while True:
            try:
                active_component = "aura.execution.run_coordinator"
                reminder_persistence_failure = False
                commands = await self.sql_store.pending_commands()
                for command in commands:
                    is_reminder = command.aggregate_type == "automation.reminder"
                    is_approval = command.aggregate_type == "governance.approval"
                    is_resumption = command.topic == RUN_RESUME_TOPIC
                    is_approval_command = is_approval or is_resumption
                    # Run commands retain their established ownership. Generic
                    # reminder commands belong to the reminder observable
                    # component and never acquire fabricated run identities.
                    component = (
                        "aura.automation.reminder_management"
                        if is_reminder
                        else "aura.governance.approval_workflow"
                        if is_approval_command
                        else "aura.execution.run_coordinator"
                    )
                    active_component = component
                    trace_identifiers = identifier_trace_metadata(command)
                    # Generic automation events use lifecycle identifiers that
                    # are trace attributes, not metric dimensions.  Keep the
                    # legacy run/memory envelopes unchanged while mapping the
                    # generic status/version keys to bounded trace names.
                    if command.aggregate_type is not None:
                        if "status" in trace_identifiers:
                            trace_identifiers["event_status"] = trace_identifiers.pop("status")
                        if "version" in trace_identifiers:
                            trace_identifiers["event_version"] = trace_identifiers.pop("version")
                        if is_reminder and command.aggregate_id is not None:
                            trace_identifiers["reminder_id"] = str(command.aggregate_id)
                    command_run_id = command.run_id
                    command_conversation_id = command.conversation_id
                    metadata = {
                        "trace_id": command.correlation_id.hex
                        if command.correlation_id
                        else (command.aggregate_id or command.id).hex,
                        "command_id": str(command.id),
                        "correlation_id": str(
                            command.correlation_id or command.aggregate_id or command.id
                        ),
                        **trace_identifiers,
                    }
                    metadata["causation_id"] = str(
                        command.causation_id or command.aggregate_id or command.id
                    )
                    if command_run_id is not None:
                        metadata["run_id"] = str(command_run_id)
                    elif trace_identifiers.get("run_id") is not None:
                        metadata["run_id"] = trace_identifiers["run_id"]
                    if command_conversation_id is not None:
                        metadata["conversation_id"] = str(command_conversation_id)
                    elif trace_identifiers.get("conversation_id") is not None:
                        metadata["conversation_id"] = trace_identifiers["conversation_id"]
                    trace_metadata = dict(trace_identifiers)
                    trace_metadata["causation_id"] = metadata["causation_id"]
                    # These are passed explicitly to record_span below.  Do
                    # not expand them twice for generic approval aggregates,
                    # whose run/conversation identifiers live in metadata.
                    span_trace_metadata = {
                        key: value
                        for key, value in trace_metadata.items()
                        if key
                        not in {
                            "trace_id",
                            "command_id",
                            "correlation_id",
                            "causation_id",
                            "run_id",
                            "conversation_id",
                        }
                    }
                    dispatch_timer = Stopwatch()
                    dispatch_span_id = new_span_id()
                    try:
                        duplicate = await self.nats.publish(command)
                        wakeup_id = command.identifier("eventId")
                        publish_wakeup = getattr(self.nats, "publish_notification_event", None)
                        if is_reminder and wakeup_id is not None and callable(publish_wakeup):
                            publish = cast(Callable[[UUID], Awaitable[None]], publish_wakeup)
                            await publish(UUID(wakeup_id))
                        if is_approval_command:
                            self.metrics.increment(
                                component,
                                "approval_outbox_publish_outcome",
                                outcome="duplicate" if duplicate else "ok",
                                dependency="nats_jetstream",
                                **metadata,
                            )
                    except Exception:
                        if is_approval_command:
                            self.metrics.increment(
                                component,
                                "approval_outbox_publish_outcome",
                                outcome="error",
                                error_class="delivery",
                                dependency="nats_jetstream",
                                **metadata,
                            )
                        self.metrics.record_span(
                            component,
                            "outbox.dispatch",
                            dispatch_timer.elapsed_ms(),
                            trace_id=metadata["trace_id"],
                            span_id=dispatch_span_id,
                            parent_span_id=None,
                            dependency="nats_jetstream",
                            outcome="error",
                            error_class="delivery",
                            run_id=metadata.get("run_id"),
                            conversation_id=metadata.get("conversation_id"),
                            command_id=metadata["command_id"],
                            correlation_id=metadata["correlation_id"],
                            causation_id=metadata["causation_id"],
                            **span_trace_metadata,
                        )
                        raise
                    self.metrics.record_span(
                        component,
                        "outbox.dispatch",
                        dispatch_timer.elapsed_ms(),
                        trace_id=metadata["trace_id"],
                        span_id=dispatch_span_id,
                        parent_span_id=None,
                        dependency="nats_jetstream",
                        outcome="duplicate" if duplicate else "ok",
                        run_id=metadata.get("run_id"),
                        conversation_id=metadata.get("conversation_id"),
                        command_id=metadata["command_id"],
                        correlation_id=metadata["correlation_id"],
                        causation_id=metadata["causation_id"],
                        **span_trace_metadata,
                    )
                    persistence_timer = Stopwatch()
                    persistence_span_id = new_span_id()
                    persistence_component = (
                        component
                        if is_reminder or is_approval_command
                        else "aura.interaction.conversation_persistence"
                    )
                    persistence_operation = (
                        "outbox.persist"
                        if is_reminder or is_approval_command
                        else "conversation.persist"
                    )
                    try:
                        await self.sql_store.mark_published(command.id)
                    except Exception:
                        if is_reminder:
                            reminder_persistence_failure = True
                        if is_approval_command:
                            self.metrics.increment(
                                persistence_component,
                                "approval_outbox_persistence_outcome",
                                outcome="error",
                                error_class="persistence",
                                dependency="postgresql",
                                **metadata,
                            )
                        self.metrics.record_span(
                            persistence_component,
                            persistence_operation,
                            persistence_timer.elapsed_ms(),
                            trace_id=metadata["trace_id"],
                            span_id=persistence_span_id,
                            parent_span_id=dispatch_span_id,
                            dependency="postgresql",
                            outcome="error",
                            error_class="persistence",
                            run_id=metadata.get("run_id"),
                            conversation_id=metadata.get("conversation_id"),
                            command_id=metadata["command_id"],
                            correlation_id=metadata["correlation_id"],
                            causation_id=metadata["causation_id"],
                            **span_trace_metadata,
                        )
                        raise
                    if is_approval_command:
                        self.metrics.increment(
                            persistence_component,
                            "approval_outbox_persistence_outcome",
                            outcome="ok",
                            dependency="postgresql",
                            **metadata,
                        )
                    self.metrics.record_span(
                        persistence_component,
                        persistence_operation,
                        persistence_timer.elapsed_ms(),
                        trace_id=metadata["trace_id"],
                        span_id=persistence_span_id,
                        parent_span_id=dispatch_span_id,
                        dependency="postgresql",
                        outcome="ok",
                        run_id=metadata.get("run_id"),
                        conversation_id=metadata.get("conversation_id"),
                        command_id=metadata["command_id"],
                        correlation_id=metadata["correlation_id"],
                        causation_id=metadata["causation_id"],
                        **span_trace_metadata,
                    )
                    if not is_approval_command:
                        self.metrics.increment(
                            component,
                            "reminder_outbox_dispatched" if is_reminder else "outbox_dispatched",
                            **metadata,
                        )
                    if duplicate and not is_approval_command:
                        self.metrics.increment(
                            component,
                            "reminder_outbox_duplicates" if is_reminder else "outbox_duplicates",
                            **metadata,
                        )
                await asyncio.sleep(0.2 if commands else 1.0)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Readiness exposes the dependency failure; the dispatcher
                # keeps retrying so a transient NATS outage is recoverable.
                if (
                    active_component == "aura.automation.reminder_management"
                    and not reminder_persistence_failure
                ):
                    self.metrics.increment(
                        active_component,
                        "reminder_command_outcome",
                        dependency="nats_jetstream",
                        outcome="error",
                    )
                elif active_component != "aura.automation.reminder_management":
                    self.metrics.increment(
                        active_component,
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

    async def _ingest_thinking_wakeup(
        self, run_id: UUID, conversation_id: UUID, offset: int, text: str
    ) -> None:
        """Fan out live thinking only to already-authorized active SSE streams."""

        publish = getattr(self.publisher, "publish_thinking_delta", None)
        if callable(publish):
            typed_publish = cast(
                Callable[..., Awaitable[None]],
                publish,
            )
            await typed_publish(
                run_id=run_id,
                conversation_id=conversation_id,
                offset=offset,
                text=text,
            )

    async def _ingest_notification_wakeup(self, event_id: UUID) -> None:
        """Wake notification SSE consumers; PostgreSQL remains authoritative."""

        self.metrics.record_span(
            NOTIFICATION_COMPONENT,
            "notification.sse.replay",
            0.0,
            trace_id=event_id.hex,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency="nats_jetstream",
            outcome="ok",
            event_id=str(event_id),
        )
        wake = getattr(self.notification_repository, "notify_wakeup", None)
        if callable(wake):
            notify = cast(Callable[[UUID], Awaitable[None]], wake)
            await notify(event_id)

    async def _ingest_approval_wakeup(self, event_id: UUID) -> None:
        """Wake approval SSE consumers; approval PostgreSQL replay is authoritative."""

        self.metrics.record_span(
            "aura.governance.approval_workflow",
            "approval.sse.replay",
            0.0,
            trace_id=event_id.hex,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency="nats_jetstream",
            outcome="ok",
            event_id=str(event_id),
        )
        await self.approval_repository.notify_wakeup(event_id)

    async def _ingest_resumption_wakeup(self, event_id: UUID) -> None:
        """Forward durable run-resumption wakeups without carrying run data."""

        self.metrics.record_span(
            "aura.governance.approval_workflow",
            "approval.resume",
            0.0,
            trace_id=event_id.hex,
            span_id=new_span_id(),
            parent_span_id=None,
            dependency="nats_jetstream",
            outcome="ok",
            event_id=str(event_id),
        )
        await self.approval_repository.notify_resume_wakeup(event_id)

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
