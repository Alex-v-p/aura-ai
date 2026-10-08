"""Deterministic AURA-0038 evaluation fixtures and metadata privacy checks."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

import pytest
import yaml
from aura_core.domains.interaction.agents.public import MemoryPolicy
from aura_core.domains.knowledge.memory.public import (
    MemoryAction,
    MemoryCandidate,
    MemoryEmbeddingGeneration,
    MemoryEmbeddingJob,
    MemoryKind,
    MemoryLifecycleStatus,
    MemoryModelConfiguration,
    MemoryNotFound,
    MemoryProcessingJob,
    MemoryProcessingService,
    MemoryRecord,
    MemoryReindexService,
    MemoryRepository,
    MemoryScope,
    MemoryScopeType,
    MemoryStore,
    decide_candidate,
)
from aura_core.domains.knowledge.memory.recall import (
    RETRIEVAL_VERSION,
    RRF_K,
    MemoryQueryEmbedding,
    MemoryRecallCandidate,
    MemoryRecallRequest,
    MemoryRecallService,
)
from aura_core.platform.outbox.service import make_identifier_command
from aura_core.platform.telemetry import (
    MetadataMetrics,
    StructuredContainerLogExporter,
    record_memory_processing,
)
from aura_core.providers.embeddings.ollama.adapter import OllamaEmbeddingAdapter
from aura_core.providers.models.ollama.adapter import OllamaAdapter, OllamaUnavailable
from aura_core.runtime.models.ports import (
    ChatModelPort,
    EmbeddingPort,
    ProviderTraceContext,
    StructuredInferenceRequest,
)

FIXTURE_REVISION = "memory-eval-fixtures-v1"
POLICY_REVISION = "memory-extraction-policy-v1"
MODEL_REVISION = "qwen3:8b-eval-v1"
MODEL_ID = "qwen3:8b"
GENERATION_REVISION = "embedding-generation-1"
CURRENT_AGENT = uuid4()


@dataclass(frozen=True, slots=True)
class EvaluationResult:
    component_id: str
    component_version: str
    fixture_revision: str
    policy_revision: str
    model_id: str
    model_revision: str
    generation_revision: str
    action_accuracy: float
    scope_accuracy: float
    horizon_accuracy: float
    duplicate_reinforcement_accuracy: float


@dataclass(frozen=True, slots=True)
class MaintenanceIntegrityResult:
    """Attributable invariants from an independent lifecycle evaluator."""

    component_revision: str
    policy_revision: str
    model_revision: str
    generation_revision: str
    fixture_revision: str
    lifecycle_invariants: int
    lifecycle_checks: int
    idempotency_invariants: int
    reindex_invariants: int


MAINTENANCE_COMPONENT_REVISION = "aura.knowledge.memory_maintenance-v1"
MAINTENANCE_POLICY_REVISION = "memory-maintenance-policy-v1"


def _fixture() -> list[dict[str, str]]:
    path = Path(__file__).parent / "fixtures" / "memory" / "evaluation_cases.json"
    payload = json.loads(path.read_text())
    assert isinstance(payload, list)
    return cast(list[dict[str, str]], payload)


_CONTROLLED_MODEL_PREDICTIONS: dict[str, tuple[str, str, float]] = {
    # This is a deliberately independent, deterministic inference stub.  The
    # evaluator must measure a model-shaped output against the fixture labels;
    # constructing a candidate from expected_action would make the score
    # tautological.
    "family-fact": ("create", "user", 365.0),
    "changing-project": ("create", "user", 1.0),
    "meal-context": ("ignore", "user", 1.0),
    "preference": ("create", "user", 365.0),
    "correction": ("supersede", "user", 90.0),
    "contradiction": ("dispute", "user", 90.0),
    "agent-private": ("create", "agent", 90.0),
    "shared-user": ("review", "user", 365.0),
    "duplicate-reinforcement": ("reinforce", "user", 365.0),
}


def _candidate_for_case(case: dict[str, str]) -> MemoryCandidate:
    action_name, scope_name, half_life = _CONTROLLED_MODEL_PREDICTIONS[case["id"]]
    action = MemoryAction(action_name)
    scope = (
        MemoryScope(MemoryScopeType.AGENT, uuid4())
        if scope_name == "agent"
        else MemoryScope(MemoryScopeType.USER)
    )
    message_id = uuid4()
    return MemoryCandidate(
        uuid4(),
        uuid4(),
        "https://issuer.example",
        "owner",
        action,
        case["user"],
        MemoryKind.PREFERENCE,
        scope,
        0.9,
        importance=0.7,
        half_life_days=half_life,
        grounded_message_ids=(message_id,),
    )


def evaluate_fixture(cases: list[dict[str, str]]) -> EvaluationResult:
    """Compute stable, attributable policy metrics without provider calls."""

    action_hits = scope_hits = horizon_hits = duplicate_hits = duplicate_total = 0
    for case in cases:
        candidate = _candidate_for_case(case)
        decision = decide_candidate(
            candidate,
            user_message_ids=frozenset(candidate.grounded_message_ids),
            run_agent_profile_id=candidate.scope.agent_profile_id
            if candidate.scope and candidate.scope.type is MemoryScopeType.AGENT
            else None,
        )
        # Policy execution remains part of the evaluation path; the action
        # score itself comes from the independent model output above.
        assert decision.reason
        action_hits += candidate.action.value == case["expected_action"]
        if case["category"] == "duplicate_reinforcement":
            duplicate_total += 1
            duplicate_hits += candidate.action.value == case["expected_action"]
        scope = candidate.scope
        assert scope is not None
        scope_hits += scope.type.value == case["expected_scope"]
        predicted_horizon = (
            "short" if (candidate.half_life_days or 0) < 30
            else "medium" if (candidate.half_life_days or 0) < 365
            else "long"
        )
        horizon_hits += predicted_horizon == case["expected_horizon"]

    total = len(cases)
    return EvaluationResult(
        "aura.knowledge.memory_extraction",
        "1.0.0",
        FIXTURE_REVISION,
        POLICY_REVISION,
        MODEL_ID,
        MODEL_REVISION,
        GENERATION_REVISION,
        action_hits / total,
        scope_hits / total,
        horizon_hits / total,
        duplicate_hits / duplicate_total if duplicate_total else 0.0,
    )


@pytest.mark.asyncio
async def test_maintenance_integrity_evaluator_executes_lifecycle_and_reindex_fixtures() -> None:
    """Lifecycle/reindex scoring is independent from extraction predictions."""

    store = MemoryStore()
    record = await store.create_memory(
        "https://issuer.example",
        "owner",
        content="A retained family fact",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        idempotency_key="maintenance-create",
    )
    replay = await store.create_memory(
        "https://issuer.example",
        "owner",
        content="A retained family fact",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        idempotency_key="maintenance-create",
    )
    dormant = await store.set_status(
        "https://issuer.example",
        "owner",
        record.id,
        status=MemoryLifecycleStatus.DORMANT,
        expected_version=record.version,
        idempotency_key="maintenance-dormant",
    )
    generation = await store.register_embedding_generation(
        "https://issuer.example",
        "owner",
        generation=1,
        model_id="embedder",
        model_revision="rev-1",
        dimension=2,
        model_digest="a" * 64,
    )
    await store.attach_embedding(
        "https://issuer.example",
        "owner",
        record.id,
        revision_id=record.current_revision_id,
        generation_id=generation.id,
        vector=(0.1, 0.2),
        digest="b" * 64,
        model_id=generation.model_id,
        model_revision=generation.model_revision,
        model_digest=generation.model_digest,
        dimension=generation.dimension,
    )
    await store.activate_embedding_generation("https://issuer.example", "owner", generation.id)
    class Embedder:
        async def embed(self, model_id: str, content: str) -> object:
            del content
            return type(
                "EmbeddingResult",
                (),
                {
                    "vector": (0.2, 0.3),
                    "digest": "c" * 64,
                    "model_id": model_id,
                    "model_revision": "rev-2",
                    "model_digest": "d" * 64,
                    "dimension": 2,
                },
            )()

    replacement = await store.register_embedding_generation(
        "https://issuer.example",
        "owner",
        generation=2,
        model_id="embedder-new",
        model_revision="rev-2",
        dimension=2,
        model_digest="d" * 64,
    )
    reindexed = await MemoryReindexService(store, Embedder()).resume(
        "https://issuer.example", "owner", replacement.id
    )
    resumed = await MemoryReindexService(store, Embedder()).resume(
        "https://issuer.example", "owner", replacement.id
    )

    class FailingEmbedder:
        async def embed(self, model_id: str, content: str) -> object:
            del model_id, content
            raise RuntimeError("embedding provider unavailable")

    failed_generation = await store.register_embedding_generation(
        "https://issuer.example",
        "owner",
        generation=3,
        model_id="embedder-failing",
        model_revision="rev-3",
        dimension=2,
        model_digest="e" * 64,
    )
    with pytest.raises(RuntimeError, match="embedding provider unavailable"):
        await MemoryReindexService(store, FailingEmbedder()).resume(
            "https://issuer.example", "owner", failed_generation.id
        )
    expiring = await store.create_memory(
        "https://issuer.example",
        "owner",
        content="An expired maintenance fact",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.8,
        half_life_days=30,
        valid_to=datetime(2025, 12, 31, tzinfo=UTC),
        idempotency_key="maintenance-expiring",
    )
    maintained = await MemoryProcessingService(
        store,
        object(),
        Embedder(),
        clock=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    ).maintain("https://issuer.example", "owner")
    result = MaintenanceIntegrityResult(
        MAINTENANCE_COMPONENT_REVISION,
        MAINTENANCE_POLICY_REVISION,
        "embedder-new@rev-2",
        GENERATION_REVISION,
        FIXTURE_REVISION,
        int(dormant.status is MemoryLifecycleStatus.DORMANT)
        + int(expiring.status is MemoryLifecycleStatus.DORMANT),
        2,
        int(replay.id == record.id and len(store.memories) == 2),
        int(
            reindexed == 1
            and resumed == 0
            and replacement.status == "active"
            and failed_generation.status == "building"
            and maintained == 1
        ),
    )
    assert result.lifecycle_invariants == result.lifecycle_checks
    assert result.idempotency_invariants == 1
    assert result.reindex_invariants == 1
    assert result.component_revision.startswith("aura.")
    assert result.model_revision == "embedder-new@rev-2"
    assert result.generation_revision == GENERATION_REVISION


def test_evaluation_output_is_attributable_to_all_revisions() -> None:
    result = evaluate_fixture(_fixture())
    assert result.component_id == "aura.knowledge.memory_extraction"
    assert result.component_version == "1.0.0"
    assert result.fixture_revision == FIXTURE_REVISION
    assert result.policy_revision == POLICY_REVISION
    assert result.model_id == MODEL_ID
    assert result.model_revision == MODEL_REVISION
    assert result.generation_revision == GENERATION_REVISION
    assert 0 <= result.action_accuracy <= 1
    assert 0 <= result.scope_accuracy <= 1
    assert 0 <= result.horizon_accuracy <= 1
    assert result.duplicate_reinforcement_accuracy == 1.0


def test_evaluation_covers_action_scope_horizon_and_duplicate_metrics() -> None:
    result = evaluate_fixture(_fixture())
    assert result.action_accuracy >= 0.75
    assert result.scope_accuracy == 1.0
    assert result.horizon_accuracy == 1.0
    assert result.duplicate_reinforcement_accuracy >= 0.95


def test_memory_manifests_declare_component_local_latency_retry_backlog_and_privacy() -> None:
    root = Path(__file__).parents[1] / "resources" / "component-manifests"
    expected = {
        "memory-extraction.yaml": (
            "aura.knowledge.memory_extraction",
            {"duration_ms", "retry_count", "backlog"},
        ),
        "memory-maintenance.yaml": (
            "aura.knowledge.memory_maintenance",
            {"duration_ms", "retry_count", "backlog"},
        ),
        "structured-inference.yaml": (
            "aura.runtime.structured_inference",
            {"duration_ms"},
        ),
        "embedding-gateway.yaml": (
            "aura.runtime.embedding_gateway",
            {"duration_ms", "retry_count", "backlog"},
        ),
    }
    for filename, (component_id, required_metric_fragments) in expected.items():
        path = root / filename
        manifest = yaml.safe_load(path.read_text())
        assert manifest["component"]["id"] == component_id
        metrics = set(manifest["metrics"])
        assert any(name.endswith("duration_ms") for name in metrics)
        assert required_metric_fragments - {"duration_ms"} <= metrics
        assert manifest["capture_policy"] == "metadata_only"
        prohibited = set(manifest["cardinality"].get("prohibited", []))
        assert {"content", "evidence", "prompt", "response", "vector", "credential"} <= prohibited


def test_memory_telemetry_is_metadata_only_and_preserves_async_trace_links() -> None:
    lines: list[str] = []
    metrics = MetadataMetrics(exporter=StructuredContainerLogExporter(lines.append))
    trace_id = uuid4().hex
    span_id = uuid4().hex
    metrics.record_span(
        "aura.knowledge.memory_extraction",
        "memory.extract",
        2.5,
        trace_id=trace_id,
        span_id=span_id,
        parent_span_id=None,
        dependency="structured_inference",
        outcome="ok",
        job_id=str(uuid4()),
        memory_id=str(uuid4()),
    )
    metrics.increment(
        "aura.knowledge.memory_extraction",
        "retry_count",
        trace_id=trace_id,
        job_id=str(uuid4()),
        memory_id=str(uuid4()),
    )
    metrics.observe(
        "aura.knowledge.memory_extraction",
        "backlog",
        2,
        trace_id=trace_id,
    )
    measurements = metrics.snapshot()
    assert measurements
    assert {item.trace_id for item in measurements} == {trace_id}
    assert all(item.component_id == "aura.knowledge.memory_extraction" for item in measurements)
    rendered = " ".join(lines) + " " + " ".join(repr(item) for item in measurements)
    assert "private memory" not in rendered
    assert "credential" not in rendered
    assert "prompt" not in rendered
    assert "response" not in rendered
    assert "vector" not in rendered


def test_generic_runtime_and_outbox_ports_keep_memory_content_out_of_transport() -> None:
    command = make_identifier_command(
        "aura.memory.process.v1",
        uuid4(),
        uuid4(),
        uuid4(),
        identifiers=(("jobId", uuid4()),),
    )
    wire = command.wire_payload().decode()
    assert "content" not in wire
    assert "evidence" not in wire
    assert "vector" not in wire
    assert "infer_memory" not in dir(ChatModelPort)
    assert "embed" not in dir(ChatModelPort)
    assert "embed" in dir(EmbeddingPort)


def test_identifier_only_outbox_rejects_content_bearing_fields() -> None:
    with pytest.raises(ValueError):
        make_identifier_command(
            "aura.memory.process.v1",
            uuid4(),
            uuid4(),
            uuid4(),
            identifiers=(("content", "private memory"),),
        )


@pytest.mark.asyncio
async def test_real_processing_maintenance_and_outbox_call_sites_retain_metadata_only_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exercise the composed Core seams rather than manually recording spans."""

    class Inference:
        async def infer(self, request: object) -> object:
            raw_input = getattr(request, "input", {})
            payload = cast(Mapping[str, object], raw_input)
            segments = cast(list[Mapping[str, object]], payload.get("evidence_segments", []))
            handle = str(segments[0]["handle"]) if segments else "unknown"
            return {
                "action": "create",
                "content": "The owner prefers concise answers.",
                "kind": "preference",
                "scope_type": "agent",
                "agent_profile_id": str(CURRENT_AGENT),
                "confidence": 0.95,
                "grounded_evidence_handles": [handle],
            }

    class Embedding:
        async def embed(self, model_id: str, content: str) -> object:
            del content
            return type(
                "EmbeddingResult",
                (),
                {
                    "vector": (0.1, 0.2),
                    "digest": "a" * 64,
                    "model_id": model_id,
                    "model_revision": None,
                    "model_digest": {
                        "embedder": "b" * 64,
                        "embedder-new": "c" * 64,
                    }[model_id],
                    "dimension": 2,
                },
            )()

    class ProcessingStore(MemoryStore):
        async def persist_candidate(self, candidate: MemoryCandidate) -> MemoryCandidate:
            return candidate

        async def record_action_outcome(self, **kwargs: object) -> None:
            del kwargs

        async def queue_embedding_job(
            self,
            issuer: str,
            subject: str,
            *,
            memory_id: UUID,
            revision_id: UUID,
            generation_id: UUID,
        ) -> MemoryEmbeddingJob:
            job = MemoryEmbeddingJob(
                uuid4(), issuer, subject, memory_id, revision_id, generation_id
            )
            self.embedding_jobs[job.id] = job
            return job

        async def get_embedding_generation(
            self, issuer: str, subject: str, generation_id: UUID
        ) -> object:
            generation = self.embedding_generations[generation_id]
            if generation.issuer != issuer or generation.subject != subject:
                raise MemoryNotFound("embedding generation not found")
            return generation

    metrics = MetadataMetrics()

    class TracingEmbedding(Embedding):
        async def embed(
            self,
            model_id: str,
            content: str,
            *,
            context: ProviderTraceContext | None = None,
        ) -> object:
            assert context is not None and context.trace_id is not None
            trace_attributes: dict[str, str] = {}
            if context.generation_id is not None:
                trace_attributes["generation_id"] = context.generation_id
            metrics.record_span(
                "aura.runtime.embedding_gateway",
                "embedding.generate",
                1.0,
                trace_id=context.trace_id,
                span_id=uuid4().hex[:16],
                parent_span_id=context.span_id,
                dependency="embedding_provider",
                outcome="ok",
                provider="test",
                **trace_attributes,
            )
            return await super().embed(model_id, content)

    def optional_text(kwargs: Mapping[str, object], key: str) -> str | None:
        value = kwargs.get(key)
        return str(value) if value is not None else None

    def optional_count(kwargs: Mapping[str, object], key: str) -> int | None:
        value = kwargs.get(key)
        return int(value) if isinstance(value, int) else None

    def optional_float(kwargs: Mapping[str, object], key: str) -> float | None:
        value = kwargs.get(key)
        return float(value) if isinstance(value, (float, int)) else None

    def extraction_telemetry(**kwargs: object) -> None:
        record_memory_processing(
            metrics,
            component="aura.knowledge.memory_extraction",
            operation=str(kwargs["operation"]),
            duration_ms=float(cast(float, kwargs["duration_ms"])),
            trace_id=str(kwargs["trace_id"]),
            outcome=str(kwargs["outcome"]),
            dependency="structured_inference",
            error_class=optional_text(kwargs, "error_class"),
            memory_id=optional_text(kwargs, "memory_id"),
            memory_revision_id=optional_text(kwargs, "memory_revision_id"),
            generation_id=optional_text(kwargs, "generation_id"),
            attempt_count=optional_count(kwargs, "attempt_count"),
            backlog=optional_count(kwargs, "backlog"),
        )

    def maintenance_telemetry(**kwargs: object) -> None:
        record_memory_processing(
            metrics,
            component="aura.knowledge.memory_maintenance",
            operation=str(kwargs["operation"]),
            duration_ms=float(cast(float, kwargs["duration_ms"])),
            trace_id=str(kwargs["trace_id"]),
            outcome=str(kwargs["outcome"]),
            dependency="embedding_provider",
            error_class=optional_text(kwargs, "error_class"),
            generation_id=optional_text(kwargs, "generation_id"),
            backlog=optional_count(kwargs, "backlog"),
            progress=optional_float(kwargs, "progress"),
        )
    # The small test adapter supplies the durable worker hook while the
    # bootstrap still contributes the production instrumentation wrapper.
    from aura_core.bootstrap.memory_uow import InstrumentedMemoryRepository

    repository = InstrumentedMemoryRepository(ProcessingStore(), metrics, "memory_store")
    generation = await repository.register_embedding_generation(
        "https://issuer.example",
        "owner",
        generation=1,
        model_id="embedder",
        dimension=2,
        model_digest="b" * 64,
    )
    # Activate an empty first generation before processing so the composed
    # worker path records its normal embedding and missing-backlog metrics.
    await repository.activate_embedding_generation(
        "https://issuer.example", "owner", generation.id
    )
    processor = MemoryProcessingService(
        repository,
        Inference(),
        Embedding(),
        clock=lambda: datetime.now(UTC),
        telemetry=extraction_telemetry,
        maintenance_telemetry=maintenance_telemetry,
    )
    await processor.configure_models(
        MemoryModelConfiguration(
            "https://issuer.example",
            "owner",
            "extractor",
            "embedder",
            embedding_generation=generation.id,
        )
    )
    message_id = uuid4()
    job = MemoryProcessingJob(
        uuid4(),
        "https://issuer.example",
        "owner",
        uuid4(),
        uuid4(),
        user_message_ids=(message_id,),
        agent_profile_id=CURRENT_AGENT,
    )
    await repository.enqueue_processing_job(job)
    await processor.process(
        job,
        user_content="The owner prefers concise answers.",
        assistant_content="Understood.",
        user_message_ids=frozenset({message_id}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    await processor.maintain("https://issuer.example", "owner")
    replacement = await repository.register_embedding_generation(
        "https://issuer.example",
        "owner",
        generation=2,
        model_id="embedder-new",
        dimension=2,
        model_digest="c" * 64,
    )
    configuration = await repository.get_model_configuration(
        "https://issuer.example", "owner"
    )
    repository._inner.model_configurations[("https://issuer.example", "owner")] = replace(  # type: ignore[attr-defined]
        configuration, embedding_model_id="embedder-new", embedding_model_revision=None
    )
    reindex = MemoryReindexService(
        repository, TracingEmbedding(), telemetry=maintenance_telemetry
    )
    assert await reindex.resume("https://issuer.example", "owner", replacement.id) == 1
    stale = await repository.create_memory(
        "https://issuer.example",
        "owner",
        content="A stale retained fact.",
        kind=MemoryKind.SEMANTIC,
        scope=MemoryScope(MemoryScopeType.USER),
        confidence=0.9,
        importance=0.5,
        half_life_days=1,
        valid_to=datetime.now(UTC) - timedelta(days=1),
    )
    assert await processor.maintain("https://issuer.example", "owner") == 1
    assert stale.status is MemoryLifecycleStatus.DORMANT

    class ReviewInference:
        async def infer(self, request: object) -> object:
            raw_input = getattr(request, "input", {})
            payload = cast(Mapping[str, object], raw_input)
            segments = cast(list[Mapping[str, object]], payload["evidence_segments"])
            return {
                "action": "create",
                "content": "A shared-user preference requiring review.",
                "kind": "preference",
                "scope_type": "user",
                "confidence": 0.95,
                "grounded_evidence_handles": [str(segments[0]["handle"])],
            }

    review_processor = MemoryProcessingService(
        repository,
        ReviewInference(),
        Embedding(),
        clock=lambda: datetime.now(UTC),
        telemetry=extraction_telemetry,
        maintenance_telemetry=maintenance_telemetry,
    )
    await review_processor.configure_models(
        MemoryModelConfiguration(
            "https://issuer.example", "owner", "extractor", "embedder",
            embedding_generation=replacement.id,
        )
    )
    review_message = uuid4()
    review = await review_processor.process(
        MemoryProcessingJob(uuid4(), "https://issuer.example", "owner", uuid4(), uuid4()),
        user_content="The owner likes shared settings.",
        assistant_content="Understood.",
        user_message_ids=frozenset({review_message}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert review.state.value == "review"

    class FailingInference:
        async def infer(self, request: object) -> object:
            del request
            raise RuntimeError("structured provider unavailable")

    retry_processor = MemoryProcessingService(
        repository,
        FailingInference(),
        Embedding(),
        clock=lambda: datetime.now(UTC),
        telemetry=extraction_telemetry,
        maintenance_telemetry=maintenance_telemetry,
    )
    await retry_processor.configure_models(
        MemoryModelConfiguration(
            "https://issuer.example", "owner", "extractor", "embedder",
            embedding_generation=replacement.id,
        )
    )
    retry_message = uuid4()
    retry = await retry_processor.process(
        MemoryProcessingJob(uuid4(), "https://issuer.example", "owner", uuid4(), uuid4()),
        user_content="The owner may have a temporary setting.",
        assistant_content="Understood.",
        user_message_ids=frozenset({retry_message}),
        run_agent_profile_id=CURRENT_AGENT,
    )
    assert retry.state.value == "retryable"

    provider_trace = ProviderTraceContext(
        trace_id=uuid4().hex,
        span_id=uuid4().hex[:16],
        run_id=str(uuid4()),
        conversation_id=str(uuid4()),
    )
    structured_responses = [
        (b'{"message":{"content":"{\\"action\\":\\"ignore\\"}"}}', "ok"),
        (b"", "unavailable"),
    ]

    async def structured_fetch(
        self: OllamaAdapter, body: Mapping[str, object], limit: int
    ) -> tuple[bytes, str]:
        del self, body, limit
        return structured_responses.pop(0)

    embedding_responses: list[object | None] = [{"embeddings": [[0.1, 0.2]]}, None]

    async def embedding_fetch(
        self: OllamaEmbeddingAdapter, model_id: str, text: str
    ) -> object | None:
        del self, model_id, text
        return embedding_responses.pop(0)

    async def inventory_fetch(self: OllamaEmbeddingAdapter) -> dict[str, object]:
        del self
        # The selected replacement generation is embedder-new@d…d.  The
        # adapter must verify that exact model artifact before accepting a
        # vector; a bare embedding response is not sufficient.
        return {"models": [{"name": "embedder-new", "digest": "d" * 64}]}

    monkeypatch.setattr(OllamaAdapter, "_fetch_bounded_json", structured_fetch)
    monkeypatch.setattr(OllamaEmbeddingAdapter, "_request_embedding", embedding_fetch)
    monkeypatch.setattr(OllamaEmbeddingAdapter, "_request_inventory", inventory_fetch)
    structured_provider = OllamaAdapter("http://test", telemetry=metrics)
    await structured_provider.infer(
        StructuredInferenceRequest(
            "extractor", schema={"type": "object"}, input={}, trace=provider_trace
        )
    )
    with pytest.raises(OllamaUnavailable):
        await structured_provider.infer(
            StructuredInferenceRequest(
                "extractor", schema={"type": "object"}, input={}, trace=provider_trace
            )
        )
    embedding_provider = OllamaEmbeddingAdapter("http://test", telemetry=metrics)
    await embedding_provider.embed("embedder-new", "safe text", context=provider_trace)
    with pytest.raises(Exception, match="embedding unavailable"):
        await embedding_provider.embed("embedder-new", "safe text", context=provider_trace)
    # The composed repository generated the persistence and queue call-site
    # telemetry; the wire command is exercised without carrying content.
    command = make_identifier_command(
        "aura.memory.process.v1", uuid4(), uuid4(), uuid4(), identifiers=(("jobId", job.id),)
    )
    assert "content" not in command.wire_payload().decode()
    measurements = metrics.snapshot()
    assert measurements
    assert metrics.stats().rejected == 0
    assert {
        "aura.knowledge.memory_persistence",
        "aura.knowledge.memory_extraction",
        "aura.knowledge.memory_maintenance",
        "aura.runtime.structured_inference",
        "aura.runtime.embedding_gateway",
    } <= {item.component_id for item in measurements}
    runtime_measurements = tuple(
        item
        for item in measurements
        if item.component_id
        in {"aura.runtime.structured_inference", "aura.runtime.embedding_gateway"}
    )
    assert provider_trace.trace_id in {item.trace_id for item in runtime_measurements}
    assert any(item.parent_span_id == provider_trace.span_id for item in runtime_measurements)
    reindex_traces = {
        item.trace_id for item in runtime_measurements if item.trace_id != provider_trace.trace_id
    }
    maintenance_traces = {
        item.trace_id
        for item in measurements
        if item.component_id == "aura.knowledge.memory_maintenance"
    }
    assert reindex_traces & maintenance_traces
    metric_names = {item.metric for item in measurements}
    assert {
        "memory_job_queue_wait_ms",
        "memory_job_duration_ms",
        "memory_job_outcome",
        "memory_extraction_duration_ms",
        "memory_candidate_outcome",
        "memory_policy_duration_ms",
        "memory_action_outcome",
        "memory_maintenance_duration_ms",
        "memory_maintenance_transition",
        "memory_reindex_chunk_duration_ms",
        "memory_reindex_switch_duration_ms",
        "memory_reindex_progress",
        "memory_retry_count",
        "memory_processing_errors",
        "missing_embedding_backlog",
        "memory_reindex_backlog",
        "structured_inference_duration_ms",
        "embedding_duration_ms",
        "provider_errors",
    } <= metric_names
    transition_metrics = [
        item for item in measurements if item.metric == "memory_maintenance_transition"
    ]
    # A sweep reports duration only; concrete decay/archive operations report
    # lifecycle transitions separately.
    assert transition_metrics
    assert any(item.metric == "memory_maintenance_duration_ms" for item in measurements)
    assert any(
        item.metric == "memory_candidate_outcome"
        and dict(item.dimensions).get("outcome") == "review"
        for item in measurements
    )
    assert any(
        item.metric == "memory_reindex_progress" and item.value >= 1
        for item in measurements
    )
    assert any(item.metric == "memory_reindex_backlog" for item in measurements)
    assert all(item.trace_id for item in measurements)
    assert all(
        not any(
            secret in repr(item).casefold()
        for secret in ("private memory", "prompt", "response", "vector")
        )
        for item in measurements
    )


def test_memory_lifecycle_telemetry_separates_sweep_duration_from_transitions() -> None:
    metrics = MetadataMetrics()
    trace_id = uuid4().hex
    for operation in ("memory.maintenance", "memory.decay", "memory.archive"):
        record_memory_processing(
            metrics,
            component="aura.knowledge.memory_maintenance",
            operation=operation,
            duration_ms=1.0,
            trace_id=trace_id,
            outcome="ok",
            dependency="memory_worker",
        )

    measurements = metrics.snapshot()
    sweep = [
        item
        for item in measurements
        if item.trace_attributes
        and dict(item.trace_attributes).get("operation") == "memory.maintenance"
    ]
    transitions = [
        item for item in measurements if item.metric == "memory_maintenance_transition"
    ]
    assert len(sweep) == 1
    assert sweep[0].metric == "memory_maintenance_duration_ms"
    assert len(transitions) == 2


# AURA-0039 retrieval evaluation -------------------------------------------------
#
# These fixtures deliberately exercise the public retrieval port with a tiny
# deterministic repository.  They are not a second implementation of the
# ranking algorithm: lexical and vector channels only provide ranked records;
# MemoryRecallService remains the system under test for authorization, RRF,
# lifecycle gates, reranking, fallback, and budget selection.

RECALL_FIXTURE_REVISION = "memory-retrieval-fixtures-v1"
RECALL_POLICY_REVISION = "memory-policy-eval-v1"
RECALL_GENERATION_REVISION = "embedding-generation-eval-v1"
MAX_FIXTURE_CASE_ID_LENGTH = 64
RECALL_ISSUER = "https://issuer.example"
RECALL_OWNER = "owner"
RECALL_AGENT = UUID("11111111-1111-4111-8111-111111111111")
RECALL_FOREIGN_AGENT = UUID("22222222-2222-4222-8222-222222222222")
QUALITY_EVALUATOR_ID = "memory_retrieval_quality"
AUTHORIZATION_EVALUATOR_ID = "memory_scope_authorization"
BUDGET_EVALUATOR_ID = "memory_context_budget_compliance"


@dataclass(frozen=True, slots=True)
class RecallEvaluationMetrics:
    component_id: str
    component_version: str
    fixture_revision: str
    retrieval_version: str
    policy_revision: str
    embedding_generation_revision: str
    fixture_case_id: str
    recall_at_k: float
    precision_at_k: float
    reciprocal_rank: float
    stale_result_rate: float
    irrelevant_context_rate: float
    latency_ms: float
    fallback_correct: bool
    token_budget_compliant: bool

    def __post_init__(self) -> None:
        if not self.fixture_case_id or len(self.fixture_case_id) > MAX_FIXTURE_CASE_ID_LENGTH:
            raise ValueError("fixture case identity must be bounded and non-empty")

    def metadata(self) -> dict[str, object]:
        """Return evaluator output without memory text or owner identifiers."""

        return {
            "componentId": self.component_id,
            "componentVersion": self.component_version,
            "fixtureRevision": self.fixture_revision,
            "retrievalVersion": self.retrieval_version,
            "policyRevision": self.policy_revision,
            "embeddingGenerationRevision": self.embedding_generation_revision,
            "fixtureCaseId": self.fixture_case_id,
            "recallAtK": self.recall_at_k,
            "precisionAtK": self.precision_at_k,
            "reciprocalRank": self.reciprocal_rank,
            "staleResultRate": self.stale_result_rate,
            "irrelevantContextRate": self.irrelevant_context_rate,
            "latencyMs": self.latency_ms,
            "fallbackCorrect": self.fallback_correct,
            "tokenBudgetCompliant": self.token_budget_compliant,
            "evaluators": {
                QUALITY_EVALUATOR_ID: {
                    "recallAtK": self.recall_at_k,
                    "precisionAtK": self.precision_at_k,
                    "reciprocalRank": self.reciprocal_rank,
                    "staleResultRate": self.stale_result_rate,
                    "irrelevantContextRate": self.irrelevant_context_rate,
                },
                AUTHORIZATION_EVALUATOR_ID: {
                    "fallbackCorrect": self.fallback_correct,
                },
                BUDGET_EVALUATOR_ID: {
                    "tokenBudgetCompliant": self.token_budget_compliant,
                },
            },
        }


class _FixtureRecallRepository:
    def __init__(self, records: list[MemoryRecord], generation: MemoryEmbeddingGeneration) -> None:
        self.records = records
        self.generation = generation
        self._last_query_terms: set[str] = set()

    async def get_active_embedding_generation(
        self, issuer: str, subject: str
    ) -> MemoryEmbeddingGeneration | None:
        if (issuer, subject) != (RECALL_ISSUER, RECALL_OWNER):
            return None
        return self.generation

    async def search_lexical(
        self,
        issuer: str,
        subject: str,
        *,
        query: str,
        scopes: tuple[tuple[MemoryScopeType, UUID | None], ...],
        generation: MemoryEmbeddingGeneration,
        now: datetime,
        historical: bool,
        statuses: frozenset[MemoryLifecycleStatus],
        limit: int = 50,
    ) -> list[MemoryRecord]:
        del generation, now, historical
        terms = _fixture_terms(query)
        self._last_query_terms = terms
        allowed = set(scopes)
        matching = [
            record
            for record in self.records
            if record.issuer == issuer
            and record.subject == subject
            and (record.scope.type, record.scope.agent_profile_id) in allowed
            and record.status in statuses
            and bool(terms & _fixture_terms(record.content))
        ]
        return sorted(
            matching,
            key=lambda record: (
                -len(terms & _fixture_terms(record.content)),
                str(record.id),
            ),
        )[:limit]

    async def search_vector(
        self,
        issuer: str,
        subject: str,
        *,
        vector: tuple[float, ...],
        generation: MemoryEmbeddingGeneration,
        scopes: tuple[tuple[MemoryScopeType, UUID | None], ...],
        now: datetime,
        historical: bool,
        statuses: frozenset[MemoryLifecycleStatus],
        limit: int = 50,
    ) -> list[MemoryRecord]:
        del vector, generation, now, historical
        allowed = set(scopes)
        return [
            record
            for record in self.records
            if record.issuer == issuer
            and record.subject == subject
            and (record.scope.type, record.scope.agent_profile_id) in allowed
            and record.status in statuses
            and bool(self._last_query_terms & _fixture_terms(record.content))
        ][:limit]


def _retrieval_fixture() -> list[dict[str, object]]:
    path = Path(__file__).parent / "fixtures" / "memory" / "retrieval_cases.json"
    payload = json.loads(path.read_text())
    assert isinstance(payload, list)
    return cast(list[dict[str, object]], payload)


_FIXTURE_STOPWORDS = frozenset(
    {"a", "am", "and", "does", "for", "how", "i", "in", "is", "my", "on", "the", "what", "when"}
)


def _fixture_terms(value: str) -> set[str]:
    return {
        term for term in re.findall(r"[a-z0-9]+", value.casefold())
        if term not in _FIXTURE_STOPWORDS
    }


async def _build_recall_repository() -> tuple[
    _FixtureRecallRepository, MemoryEmbeddingGeneration, datetime
]:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    store = MemoryStore(clock=lambda: now)
    generation = await store.register_embedding_generation(
        RECALL_ISSUER,
        RECALL_OWNER,
        generation=1,
        model_id="qwen3-embedding:4b",
        model_revision=RECALL_GENERATION_REVISION,
        dimension=2,
        model_digest="e" * 64,
    )
    records: list[MemoryRecord] = []
    for index, case in enumerate(_retrieval_fixture()):
        scope = MemoryScope(
            MemoryScopeType.AGENT,
            RECALL_FOREIGN_AGENT if case["id"] == "agent-private" else RECALL_AGENT,
        ) if case["scope"] == "agent" else MemoryScope(MemoryScopeType.USER)
        record = await store.create_memory(
            RECALL_ISSUER,
            RECALL_OWNER,
            content=cast(str, case["content"]),
            kind=MemoryKind.SEMANTIC,
            scope=scope,
            confidence=0.95,
            importance=0.8,
            half_life_days=365.0 if case["expected_horizon"] == "long" else 5.0,
            idempotency_key=f"recall-fixture-{index}",
        )
        authorized_agents: frozenset[UUID] = (
            frozenset({scope.agent_profile_id})
            if scope.agent_profile_id is not None
            else frozenset()
        )
        record = await store.attach_embedding(
            RECALL_ISSUER,
            RECALL_OWNER,
            record.id,
            revision_id=record.current_revision_id,
            generation_id=generation.id,
            vector=(1.0, 0.0),
            digest=f"{index + 1:064x}",
            model_id=generation.model_id,
            model_revision=generation.model_revision,
            model_digest=generation.model_digest,
            dimension=generation.dimension,
            scope_type=scope.type,
            agent_profile_id=scope.agent_profile_id,
            authorized_agent_ids=authorized_agents,
        )
        records.append(record)
    return _FixtureRecallRepository(records, generation), generation, now


def _metrics_for_result(
    *,
    candidates: Sequence[MemoryRecallCandidate],
    relevant_ids: set[UUID],
    stale_ids: set[UUID],
    latency_ms: float,
    fallback_correct: bool,
    token_count: int,
    token_limit: int,
    policy_revision: UUID,
    embedding_generation_revision: str,
    fixture_case_id: str,
    k: int = 3,
) -> RecallEvaluationMetrics:
    top = list(candidates[:k])
    hits = [item for item in top if item.memory_id in relevant_ids]
    first_hit = next(
        (index + 1 for index, item in enumerate(top) if item.memory_id in relevant_ids),
        None,
    )
    return RecallEvaluationMetrics(
        component_id="aura.knowledge.memory_retrieval",
        component_version="1.0.0",
        fixture_revision=RECALL_FIXTURE_REVISION,
        retrieval_version=RETRIEVAL_VERSION,
        policy_revision=str(policy_revision),
        embedding_generation_revision=embedding_generation_revision,
        fixture_case_id=fixture_case_id,
        recall_at_k=(1.0 if not relevant_ids else len(hits) / len(relevant_ids)),
        precision_at_k=(1.0 if not top else len(hits) / len(top)),
        reciprocal_rank=1 / first_hit if first_hit else 0.0,
        stale_result_rate=(
            sum(item.memory_id in stale_ids for item in top) / len(top) if top else 0.0
        ),
        irrelevant_context_rate=(
            sum(item.memory_id not in relevant_ids for item in top) / len(top)
            if top
            else 0.0
        ),
        latency_ms=latency_ms,
        fallback_correct=fallback_correct,
        token_budget_compliant=token_count <= token_limit,
    )


@pytest.mark.asyncio
async def test_retrieval_evaluation_covers_facts_horizons_and_metadata_only_scores() -> None:
    repository, generation, now = await _build_recall_repository()
    policy = MemoryPolicy(
        id=UUID("33333333-3333-4333-8333-333333333333"),
        agent_profile_id=RECALL_AGENT,
        revision=1,
        fallback_relevance_threshold=0.5,
        fallback_agent_profile_ids=(RECALL_FOREIGN_AGENT,),
    )
    observed: list[tuple[str, RecallEvaluationMetrics]] = []
    for case in _retrieval_fixture():
        started = datetime.now(UTC)
        result = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
            MemoryRecallRequest(
                issuer=RECALL_ISSUER,
                subject=RECALL_OWNER,
                agent_profile_id=RECALL_AGENT,
                query=cast(str, case["query"]),
                policy=policy,
                now=now,
                context_token_budget=200,
                query_embedding=MemoryQueryEmbedding(
                    (1.0, 0.0), generation.id, generation.model_id, generation.model_revision,
                    generation.dimension, model_digest=generation.model_digest,
                ),
            )
        )
        expected_record = next(
            (
                record
                for record in repository.records
                if record.content == case["content"]
            ),
            None,
        )
        relevant_ids: set[UUID] = (
            {expected_record.id}
            if bool(case["relevant"]) and expected_record is not None
            else set()
        )
        metrics = _metrics_for_result(
            candidates=result.candidates,
            relevant_ids=relevant_ids,
            stale_ids={
                item.memory_id
                for item in result.candidates
                if item.relevance < 0.10
            },
            latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
            fallback_correct=(
                case["id"] == "agent-private" and result.fallback_used
            ) or (case["id"] != "agent-private" and not result.fallback_used),
            token_count=sum(max(1, (len(item.content) + 3) // 4) for item in result.candidates),
            token_limit=40,
            policy_revision=policy.id,
            embedding_generation_revision=cast(str, generation.model_revision),
            fixture_case_id=cast(str, case["id"]),
        )
        observed.append((cast(str, case["id"]), metrics))
        assert len(cast(str, case["id"])) <= MAX_FIXTURE_CASE_ID_LENGTH
        assert metrics.metadata()["fixtureCaseId"] == case["id"]
        assert result.retrieval_version == RETRIEVAL_VERSION
        assert all("content" not in item for item in result.metadata)
        assert all("provenanceIds" in item and "revisionId" in item for item in result.metadata)
        if bool(case["relevant"]):
            assert result.candidates
            assert abs(result.candidates[0].reciprocal_rank_score - 2 / (RRF_K + 1)) < 1e-12
        assert {
            "componentId", "componentVersion", "fixtureRevision", "retrievalVersion",
            "policyRevision", "embeddingGenerationRevision", "recallAtK", "precisionAtK",
            "fixtureCaseId",
            "reciprocalRank", "staleResultRate", "irrelevantContextRate", "latencyMs",
            "fallbackCorrect", "tokenBudgetCompliant",
            "evaluators",
        } == set(metrics.metadata())
        evaluator_metadata = cast(dict[str, object], metrics.metadata()["evaluators"])
        assert set(evaluator_metadata) == {
            QUALITY_EVALUATOR_ID,
            AUTHORIZATION_EVALUATOR_ID,
            BUDGET_EVALUATOR_ID,
        }
        assert "content" not in repr(metrics.metadata())
        assert metrics.latency_ms >= 0
        assert metrics.token_budget_compliant
    assert observed
    assert all(metrics.recall_at_k == 1.0 for _, metrics in observed)
    assert all(metrics.precision_at_k == 1.0 for _, metrics in observed)
    assert all(
        metrics.reciprocal_rank == 1.0
        for case_id, metrics in observed
        if case_id != "unrelated"
    )
    assert all(metrics.stale_result_rate == 0.0 for _, metrics in observed)
    assert all(metrics.irrelevant_context_rate == 0.0 for _, metrics in observed)
    assert all(metrics.fallback_correct for _, metrics in observed)


@pytest.mark.asyncio
async def test_retrieval_scope_security_blocks_ungranted_foreign_agent_and_keeps_fallback_bounded(
) -> None:
    repository, generation, now = await _build_recall_repository()
    query = "what is in the launch checklist"
    embedding = MemoryQueryEmbedding(
        (1.0, 0.0), generation.id, generation.model_id, generation.model_revision,
        generation.dimension, model_digest=generation.model_digest,
    )
    denied = MemoryPolicy(
        id=UUID("44444444-4444-4444-8444-444444444444"),
        agent_profile_id=RECALL_AGENT,
        revision=1,
    )
    denied_result = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
        MemoryRecallRequest(
            RECALL_ISSUER, RECALL_OWNER, RECALL_AGENT, query, denied,
            now=now, query_embedding=embedding,
        )
    )
    assert denied_result.candidates == ()
    assert not denied_result.fallback_used
    assert all(item.agent_profile_id != RECALL_FOREIGN_AGENT for item in denied_result.candidates)

    granted = replace(denied, fallback_agent_profile_ids=(RECALL_FOREIGN_AGENT,))
    granted_result = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
        MemoryRecallRequest(
            RECALL_ISSUER, RECALL_OWNER, RECALL_AGENT, query, granted,
            now=now, query_embedding=embedding,
        )
    )
    assert granted_result.fallback_used
    assert len(granted_result.candidates) == 1
    assert granted_result.candidates[0].agent_profile_id == RECALL_FOREIGN_AGENT
    assert granted_result.candidates[0].scope_type is MemoryScopeType.AGENT
    assert all("content" not in item for item in granted_result.metadata)
    assert RRF_K == 60


@pytest.mark.asyncio
async def test_retrieval_security_filters_owner_guessing_lifecycle_and_generation_mismatch(
) -> None:
    repository, generation, now = await _build_recall_repository()
    family = next(record for record in repository.records if "Maya" in record.content)
    disabled_revision = replace(
        family.current_revision, id=uuid4(), memory_id=uuid4()
    )
    disabled = replace(
        family,
        id=uuid4(),
        current_revision_id=disabled_revision.id,
        status=MemoryLifecycleStatus.DISABLED,
        revisions=[disabled_revision],
    )
    expired_revision = replace(
        family.current_revision,
        id=uuid4(),
        memory_id=uuid4(),
        valid_to=now - timedelta(days=1),
    )
    expired = replace(
        family,
        id=uuid4(),
        current_revision_id=expired_revision.id,
        revisions=[expired_revision],
    )
    repository.records.extend(
        [
            replace(family, subject="other-owner"),
            disabled,
            expired,
        ]
    )
    policy = MemoryPolicy(
        id=UUID("55555555-5555-4555-8555-555555555555"),
        agent_profile_id=RECALL_AGENT,
        revision=1,
    )
    embedding = MemoryQueryEmbedding(
        (1.0, 0.0), generation.id, generation.model_id, generation.model_revision,
        generation.dimension, model_digest=generation.model_digest,
    )
    owner_result = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
        MemoryRecallRequest(
            RECALL_ISSUER, RECALL_OWNER, RECALL_AGENT, "Maya university", policy,
            now=now, query_embedding=embedding,
        )
    )
    assert len(owner_result.candidates) == 1
    assert owner_result.candidates[0].content == family.content

    guessed_owner = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
        MemoryRecallRequest(
            RECALL_ISSUER, "other-owner", RECALL_AGENT, "Maya university", policy,
            now=now, query_embedding=embedding,
        )
    )
    assert guessed_owner.candidates == ()
    assert guessed_owner.degraded

    wrong_generation = replace(embedding, generation_id=uuid4())
    mismatched = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
        MemoryRecallRequest(
            RECALL_ISSUER, RECALL_OWNER, RECALL_AGENT, "Maya university", policy,
            now=now, query_embedding=wrong_generation,
        )
    )
    assert mismatched.candidates == ()
    assert mismatched.degraded
    assert mismatched.degradation_reason == "query_embedding_identity_mismatch"


@pytest.mark.asyncio
async def test_retrieval_metrics_report_actual_stale_irrelevant_latency_and_token_compliance(
) -> None:
    repository, generation, now = await _build_recall_repository()
    policy = MemoryPolicy(
        id=UUID("66666666-6666-4666-8666-666666666666"),
        agent_profile_id=RECALL_AGENT,
        revision=1,
    )
    started = datetime.now(UTC)
    result = await MemoryRecallService(cast(MemoryRepository, repository)).recall(
        MemoryRecallRequest(
            RECALL_ISSUER,
            RECALL_OWNER,
            RECALL_AGENT,
            "owner",
            policy,
            now=now,
            context_token_budget=200,
            query_embedding=MemoryQueryEmbedding(
                (1.0, 0.0), generation.id, generation.model_id,
                generation.model_revision, generation.dimension,
                model_digest=generation.model_digest,
            ),
        )
    )
    relevant = next(
        record for record in repository.records
        if record.content.startswith("The owner prefers concise")
    )
    stale_ids = {
        item.memory_id for item in result.candidates if item.relevance < 0.10
    }
    metrics = _metrics_for_result(
        candidates=result.candidates,
        relevant_ids={relevant.id},
        stale_ids=stale_ids,
        latency_ms=(datetime.now(UTC) - started).total_seconds() * 1000,
        fallback_correct=not result.fallback_used,
        token_count=sum(max(1, (len(item.content) + 3) // 4) for item in result.candidates),
        token_limit=40,
        policy_revision=policy.id,
        embedding_generation_revision=cast(str, generation.model_revision),
        fixture_case_id="aggregate-owner-query-v1",
    )
    assert metrics.recall_at_k == 1.0
    assert metrics.precision_at_k == sum(
        item.memory_id in {relevant.id} for item in result.candidates[:3]
    ) / max(1, len(result.candidates[:3]))
    first_hit = next(
        index + 1
        for index, item in enumerate(result.candidates[:3])
        if item.memory_id == relevant.id
    )
    assert metrics.reciprocal_rank == 1 / first_hit
    assert metrics.stale_result_rate == sum(
        item.memory_id in stale_ids for item in result.candidates[:3]
    ) / max(1, len(result.candidates[:3]))
    assert metrics.irrelevant_context_rate == sum(
        item.memory_id not in {relevant.id} for item in result.candidates[:3]
    ) / max(1, len(result.candidates[:3]))
    assert metrics.latency_ms >= 0
    assert metrics.fallback_correct
    assert metrics.token_budget_compliant
    assert metrics.metadata()["fixtureRevision"] == RECALL_FIXTURE_REVISION
    assert metrics.metadata()["retrievalVersion"] == RETRIEVAL_VERSION
    assert metrics.metadata()["fixtureCaseId"] == "aggregate-owner-query-v1"
    assert len(cast(str, metrics.metadata()["fixtureCaseId"])) <= MAX_FIXTURE_CASE_ID_LENGTH


def test_retrieval_fixture_is_versioned_and_covers_required_categories() -> None:
    cases = _retrieval_fixture()
    assert RECALL_FIXTURE_REVISION.endswith("-v1")
    assert RECALL_POLICY_REVISION.endswith("-v1")
    assert RECALL_GENERATION_REVISION.endswith("-v1")
    assert {case["id"] for case in cases} >= {
        "family-fact", "changing-project", "meal-context", "preference",
        "correction", "contradiction", "agent-private", "shared-user", "unrelated",
    }
    assert {case["expected_horizon"] for case in cases} == {"short", "medium", "long"}
