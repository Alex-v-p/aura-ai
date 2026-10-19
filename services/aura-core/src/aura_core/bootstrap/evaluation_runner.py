"""Production composition for the isolated Core evaluation runner."""

from __future__ import annotations

import hashlib
from collections.abc import Awaitable, Callable, Mapping
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import nats

from aura_core.bootstrap.document_uow import document_service
from aura_core.domains.execution.runs.public import RunCoordinator
from aura_core.domains.interaction.agents.public import AgentCatalog
from aura_core.domains.interaction.conversations.public import ConversationStore
from aura_core.platform.auth import Settings
from aura_core.platform.database.engine import make_engine, session_factory
from aura_core.platform.outbox import InMemoryOutbox
from aura_core.platform.outbox.nats import (
    EvaluationJetStreamConsumer,
    EvaluationJetStreamPublisher,
)
from aura_core.platform.telemetry import (
    MetadataMetrics,
    TelemetryLifecycle,
    make_telemetry_exporter,
)
from aura_core.providers.object_storage.evaluation import EvaluationArtifactAdapter
from aura_core.providers.object_storage.garage import GarageS3ObjectStore
from aura_core.runtime.models.ports import ModelDescriptor
from aura_core.runtime.replay.application import EvaluationRunner, RunnerConfig
from aura_core.runtime.replay.registry import (
    ReplayContext,
    ReplayError,
    ReplayRegistry,
    ReplayResult,
    RuntimeEvidenceProvider,
)
from aura_core.runtime.replay.state import SqlEvaluationRunnerLedger
from aura_core.runtime.streaming.publisher import EventPublisher
from aura_core.runtime.tools.public import ToolRegistry

_NATS_ERROR_CLASSES = frozenset(
    {
        "authorization",
        "budget",
        "provider",
        "provider_protocol",
        "timeout",
        "unknown",
    }
)


def _bounded_nats_error_class(error: BaseException) -> str:
    """Map NATS provider failures to a finite, content-free dimension.

    The pinned NATS client invokes ``error_cb`` from its reconnect loop.  The
    default callback logs ``exc_info`` and therefore leaks provider traceback
    details (which may include private connection information).  Classifying
    only the exception type name keeps the callback useful for operations while
    never reading the exception text, endpoint, or embedded identifiers.
    """

    name = type(error).__name__.lower()
    if "timeout" in name:
        return "timeout"
    if any(marker in name for marker in ("authorization", "credential", "authentication")):
        return "authorization"
    if any(marker in name for marker in ("payload", "buffer", "consumer")):
        return "budget"
    if any(marker in name for marker in ("protocol", "subject", "subscription", "header")):
        return "provider_protocol"
    if any(marker in name for marker in ("connection", "server", "socket", "eof", "stale")):
        return "provider"
    return "unknown"


def _record_nats_diagnostic(
    metrics: MetadataMetrics,
    metric: str,
    *,
    outcome: str,
    error_class: str,
) -> None:
    """Emit only allowlisted NATS diagnostics and never affect the callback."""

    if error_class not in _NATS_ERROR_CLASSES:
        error_class = "unknown"
    try:
        metrics.increment(
            "aura.evaluation.runner",
            metric,
            dependency="nats_jetstream",
            outcome=outcome,
            error_class=error_class,
        )
    except Exception:
        # A telemetry sink outage must not change NATS reconnect behavior.
        return


def _evaluation_nats_callbacks(
    metrics: MetadataMetrics,
) -> dict[str, Callable[..., Awaitable[None]]]:
    """Build privacy-safe callbacks for the pinned NATS client."""

    async def error_cb(error: Exception) -> None:
        _record_nats_diagnostic(
            metrics,
            "evaluation_runner_nats_fetch_failure_count",
            outcome="error",
            error_class=_bounded_nats_error_class(error),
        )

    async def disconnected_cb() -> None:
        _record_nats_diagnostic(
            metrics,
            "evaluation_runner_nats_fetch_failure_count",
            outcome="error",
            error_class="provider",
        )

    async def closed_cb() -> None:
        _record_nats_diagnostic(
            metrics,
            "evaluation_runner_nats_drain_failure_count",
            outcome="closed",
            error_class="provider",
        )

    async def reconnected_cb() -> None:
        _record_nats_diagnostic(
            metrics,
            "evaluation_runner_nats_recovery_count",
            outcome="ok",
            error_class="unknown",
        )

    return {
        "error_cb": error_cb,
        "disconnected_cb": disconnected_cb,
        "closed_cb": closed_cb,
        "reconnected_cb": reconnected_cb,
    }


async def _connect_evaluation_nats(
    nats_url: str,
    metrics: MetadataMetrics,
) -> Any:
    """Connect the runner with callbacks that cannot log provider details."""

    connect: Any = getattr(nats, "connect")  # noqa: B009
    return await connect(nats_url, **_evaluation_nats_callbacks(metrics))


class ConfiguredRuntimeEvidence(RuntimeEvidenceProvider):
    """Observe only bounded deployment configuration, never request content."""

    def __init__(self, settings: Settings, registry: ReplayRegistry) -> None:
        self._settings = settings
        self._registry = registry

    def observe(self, *, component_id: str, component_version: str) -> Mapping[str, object]:
        settings = self._settings
        try:
            if component_id == "aura.evaluation.synthetic":
                adapter = self._registry.require_synthetic(
                    component_id, component_version
                )
                registered_version = adapter.scenario_version
            else:
                adapter = self._registry.require_replay(
                    component_id, component_version
                )
                registered_version = adapter.component_version
        except ReplayError:
            return {}
        fixture_revision = getattr(adapter, "fixture_revision", "")
        configured_fixture_revision = settings.evaluation_runtime_fixture_revision
        if configured_fixture_revision and configured_fixture_revision != fixture_revision:
            # A configured pin is an admission guard, never a replacement for
            # the adapter-owned identity. Fail before the adapter executes.
            raise ReplayError("runtime fixture binding mismatch")
        observed: dict[str, object] = {
            "componentVersion": registered_version,
            "codeRevision": settings.telemetry_git_commit,
            "configurationRevision": settings.telemetry_configuration_version,
            "deploymentRevision": settings.telemetry_deployment_id,
            # Fixture identity belongs to the selected adapter.  A generic
            # deployment setting cannot claim verification for two adapters
            # with different fixture sets; the request is rejected if it
            # declares a different value.
            "fixtureRevision": fixture_revision,
            "promptRevision": settings.evaluation_runtime_prompt_revision,
            "policyRevision": settings.evaluation_runtime_policy_revision,
        }
        observed = {key: value for key, value in observed.items() if value not in (None, "")}
        if settings.evaluation_runtime_image_digest:
            observed["imageDigest"] = settings.evaluation_runtime_image_digest
        if settings.evaluation_runtime_model_id:
            observed["modelId"] = settings.evaluation_runtime_model_id
        if settings.evaluation_runtime_model_version:
            observed["modelVersion"] = settings.evaluation_runtime_model_version
        if settings.evaluation_runtime_model_digest:
            observed["modelDigest"] = settings.evaluation_runtime_model_digest
        if settings.evaluation_runtime_seed is not None:
            observed["seed"] = settings.evaluation_runtime_seed
        return observed


class DocumentProcessingEvaluationReplay:
    """Run the deployed document processing seam against isolated memory state."""

    scenario_id = "aura.evaluation.synthetic"
    component_id = "aura.knowledge.document_processing"
    component_version = "1.0.0"
    fixture_revision = "document-library-eval-fixtures-v1"

    def __init__(self) -> None:
        self._service = document_service(testing=True)

    async def replay(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
        context.checkpoint()
        if not context.input_artifacts:
            raise ReplayError("evaluation input is unavailable", dependency="object_storage")
        # The runner has already authenticated and bounded these bytes at the
        # artifact port.  Select deterministically so a replay cannot depend
        # on mapping insertion order or an untrusted caller-controlled name.
        input_content = context.input_artifacts[sorted(context.input_artifacts)[0]]
        input_digest = hashlib.sha256(input_content).hexdigest()
        document_id = uuid5(NAMESPACE_URL, f"aura-evaluation-document:{case_id}")
        document = await self._service.upload(
            "evaluation-issuer",
            "evaluation-runner",
            filename="fixture.txt",
            media_type="text/plain",
            stream=input_content,
            document_id=document_id,
        )
        # ``process`` is the public application seam used by Core's deployed
        # document worker.  Calling it over the testing composition keeps the
        # replay isolated from owner tables while exercising real parsing,
        # chunking, and persistence transitions.
        replayed = await self._service.process(document.id)
        context.checkpoint()
        status = str(replayed.status)
        document_version = replayed.version
        output_content = (
            '{"component":"aura.knowledge.document_processing","inputDigest":"'
            + input_digest
            + '","status":"'
            + status
            + '"}'
        ).encode()
        output_id = uuid5(NAMESPACE_URL, f"aura-evaluation-output:{case_id}")
        output_capture = dict(context.capture_authorization)
        output_artifacts: tuple[tuple[Mapping[str, object], bytes], ...] = ()
        if output_capture.get("captureClassification") not in {None, "metadata_only", "hashed"}:
            output_artifacts = (
                (
                    {
                        "artifactReferenceId": str(output_id),
                        "mediaClassification": "evaluation_output",
                        "mediaType": "application/json",
                        "byteCount": len(output_content),
                        "integrity": {
                            "algorithm": "sha256",
                            "digest": hashlib.sha256(output_content).hexdigest(),
                        },
                        "captureAuthorization": output_capture,
                    },
                    output_content,
                ),
            )
        return ReplayResult(
            self.component_id,
            self.component_version,
            "pass" if status == "ready" else "fail",
            {
                "document_status": status,
                "document_version": document_version,
                "input_bytes": len(input_content),
                "input_digest": input_digest,
            },
            output_artifacts=output_artifacts,
        )


class SyntheticEvaluationScenario:
    """Exercise the public Core run use case with isolated evaluation fixtures."""

    scenario_id = "aura.evaluation.synthetic"
    scenario_version = "1.0.0"
    fixture_revision = "synthetic-run-coordinator-v1"

    async def execute(self, case_id: UUID, context: ReplayContext) -> ReplayResult:
        context.checkpoint()
        dependency = _SyntheticDependencyFixture()
        model = _SyntheticModelFixture(dependency)
        policy = _SyntheticPolicyFixture()
        repository = ConversationStore(default_model=model.id, agents=policy.catalog)
        side_effects = _SyntheticSideEffectFixture()
        tools = _SyntheticToolFixture()
        coordinator = RunCoordinator(
            repository,
            side_effects.events,
            side_effects.outbox,
            tool_registry=tools.registry,
        )
        descriptor = ModelDescriptor(
            model.id,
            "Deterministic evaluation model",
            "evaluation-fixture",
            ("chat",),
            model_revision=model.revision,
            model_digest=model.digest,
        )
        try:
            _conversation, _message, run = await repository.create(
                "evaluation-runner",
                "synthetic-case",
                "controlled evaluation fixture",
                model.id,
                (descriptor,),
                f"aura-evaluation:{case_id}",
            )
            await coordinator.execute(run.id, model)
            _completed_conversation, completed = await repository.find_run_any(run.id)
        except Exception as exc:
            raise ReplayError(
                "synthetic Core orchestration failed", dependency="application_unknown"
            ) from exc
        if completed.status.value != "completed":
            raise ReplayError(
                "synthetic Core run did not complete", dependency="application_unknown"
            )
        context.checkpoint()
        return ReplayResult(
            self.scenario_id,
            self.scenario_version,
            "pass",
            {
                "run_lifecycle": completed.status.value,
                "model_calls": model.calls,
                "dependency_checks": dependency.checks,
                "tool_fixture_revision": tools.revision,
                "policy_fixture_revision": policy.revision,
                "side_effect_events": await side_effects.event_count(run.id),
            },
            model_calls=model.calls,
        )


class _SyntheticModelFixture:
    revision = "synthetic-model-fixture-v2"
    digest = "sha256:synthetic-model-fixture-v2"
    id = "synthetic-evaluation-model"

    def __init__(self, dependency: _SyntheticDependencyFixture) -> None:
        self._dependency = dependency
        self.calls = 0

    async def stream_chat(self, model_id: str, messages: object) -> Any:
        del messages
        if model_id != self.id:
            raise RuntimeError("synthetic model selection mismatch")
        self.calls += 1
        marker = self.calls
        if not await self._dependency.check(marker):
            raise RuntimeError("synthetic dependency failed")
        yield f"synthetic evaluation result {marker}"


class _SyntheticDependencyFixture:
    revision = "synthetic-dependency-fixture-v2"

    def __init__(self) -> None:
        self.checks = 0

    async def check(self, marker: int) -> bool:
        self.checks += 1
        return marker > 0


class _SyntheticToolFixture:
    revision = "synthetic-tool-fixture-v2"

    def __init__(self) -> None:
        self.registry = ToolRegistry(())


class _SyntheticPolicyFixture:
    revision = "synthetic-policy-fixture-v2"

    def __init__(self) -> None:
        self.catalog = AgentCatalog()


class _SyntheticSideEffectFixture:
    revision = "synthetic-side-effects-v2"

    def __init__(self) -> None:
        self.events = EventPublisher()
        self.outbox = InMemoryOutbox()

    async def event_count(self, run_id: UUID) -> int:
        return len(await self.events.history(run_id))


async def build_evaluation_runner(
    settings: Settings | None = None,
) -> tuple[EvaluationRunner, EvaluationJetStreamConsumer, Any, TelemetryLifecycle]:
    """Compose every production provider before the entrypoint starts."""

    effective = settings or Settings(role="evaluation-runner")
    engine = make_engine(effective.database_url)
    sessions = session_factory(engine)
    ledger = SqlEvaluationRunnerLedger(sessions)
    store = GarageS3ObjectStore(
        effective.object_storage_endpoint,
        effective.object_storage_bucket,
        access_key=effective.object_storage_access_key,
        secret_key=effective.object_storage_secret_key,
        region=effective.object_storage_region,
    )
    artifacts = EvaluationArtifactAdapter(
        store,
        max_bytes=effective.evaluation_runner_max_output_bytes,
    )
    metrics = MetadataMetrics(
        exporter=make_telemetry_exporter(
            enabled=effective.telemetry_otlp_enabled,
            endpoint=effective.telemetry_otlp_endpoint,
            credential=effective.telemetry_otlp_credential,
            queue_capacity=effective.telemetry_otlp_queue_capacity,
            max_attempts=effective.telemetry_otlp_max_attempts,
            timeout_seconds=effective.telemetry_otlp_timeout_seconds,
            deployment_id=effective.telemetry_deployment_id,
            git_commit=effective.telemetry_git_commit,
            environment=effective.environment,
            host_id=effective.telemetry_host_id,
            service_role="worker",
            image_version=effective.telemetry_image_version,
            configuration_version=effective.telemetry_configuration_version,
        )
    )
    registry = ReplayRegistry()
    registry.register_replay(DocumentProcessingEvaluationReplay())
    registry.register_synthetic(SyntheticEvaluationScenario())
    connection = await _connect_evaluation_nats(effective.nats_url, metrics)
    publisher = EvaluationJetStreamPublisher(connection, metrics=metrics)
    lifecycle = TelemetryLifecycle(metrics)
    runner = EvaluationRunner(
        ledger,
        registry,
        publisher,
        config=RunnerConfig(
            worker_id="evaluation-runner",
            max_concurrency=effective.evaluation_runner_max_concurrency,
            max_queue_depth=effective.evaluation_runner_max_queue_depth,
            max_timeout_ms=effective.evaluation_runner_max_timeout_ms,
            max_queue_time_ms=effective.evaluation_runner_max_queue_time_ms,
            max_cases=effective.evaluation_runner_max_cases,
            max_input_bytes=effective.evaluation_runner_max_input_bytes,
            max_output_bytes=effective.evaluation_runner_max_output_bytes,
            max_tokens=effective.evaluation_runner_max_tokens,
            max_model_calls=effective.evaluation_runner_max_model_calls,
            max_artifact_operations=effective.evaluation_runner_max_artifact_operations,
        ),
        artifact_adapter=artifacts,
        runtime_evidence=ConfiguredRuntimeEvidence(effective, registry),
        metrics=metrics,
    )
    consumer = EvaluationJetStreamConsumer(
        connection,
        request_durable=effective.evaluation_runner_request_durable,
        cancellation_durable=effective.evaluation_runner_cancellation_durable,
        metrics=metrics,
    )
    return runner, consumer, (connection, engine), lifecycle


__all__ = ["ConfiguredRuntimeEvidence", "build_evaluation_runner"]
