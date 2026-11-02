"""Exact instrumentation boundary evaluations for durable research."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from aura_core.platform.telemetry import _METRICS, COMPONENT_VERSIONS, MetadataMetrics

ROOT = Path(__file__).parents[1]
COMPONENT = "aura.research.coordinator@1"
EXPECTED_METRICS = {
    "research_coordinate_outcome_count",
    "research_coordinate_latency_ms",
    "research_checkpoint_count",
    "research_queue_lag_ms",
    "research_cancel_command_outcome_count",
    "research_cancel_command_latency_ms",
    "research_cancel_settlement_lag_ms",
    "research_plan_outcome_count",
    "research_plan_latency_ms",
    "research_evidence_validation_outcome_count",
    "research_evidence_validation_latency_ms",
    "research_evidence_rejected_count",
    "research_citation_validation_outcome_count",
    "research_citation_validation_latency_ms",
    "research_citation_rejected_count",
    "research_synthesis_outcome_count",
    "research_synthesis_latency_ms",
    "research_unsupported_claim_count",
    "research_deletion_outcome_count",
    "research_deletion_latency_ms",
    "research_budget_wall_time_consumed_ms",
    "research_budget_sources_consumed_count",
    "research_budget_searches_consumed_count",
    "research_budget_browser_pages_consumed_count",
    "research_budget_model_calls_consumed_count",
    "research_budget_exhaustion_count",
    "research_static_fetch_settled_count",
    "research_static_to_browser_fallback_count",
    "research_static_to_browser_fallback_ratio",
}
COORDINATOR_METRICS = {
    "research_coordinate_outcome_count",
    "research_coordinate_latency_ms",
    "research_checkpoint_count",
    "research_queue_lag_ms",
    "research_budget_wall_time_consumed_ms",
    "research_budget_sources_consumed_count",
    "research_budget_searches_consumed_count",
    "research_budget_browser_pages_consumed_count",
    "research_budget_model_calls_consumed_count",
    "research_budget_exhaustion_count",
    "research_static_fetch_settled_count",
    "research_static_to_browser_fallback_count",
    "research_static_to_browser_fallback_ratio",
}
ALLOWED_DIMENSIONS = {
    "operation",
    "outcome",
    "status",
    "dependency",
    "errorClass",
    "provider",
    "retrievalStage",
    "degradation",
}
FORBIDDEN_CONTENT = ("question", "query", "url", "excerpt", "owner", "payload", "credential")


def test_coordinator_component_and_all_required_metrics_are_registered() -> None:
    assert COMPONENT in COMPONENT_VERSIONS
    assert COMPONENT in _METRICS
    assert set(_METRICS[COMPONENT]) == COORDINATOR_METRICS

    manifest = ROOT / "resources" / "component-manifests" / "research-coordinator.yaml"
    assert manifest.exists()
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    assert document["component"]["id"] == COMPONENT
    assert set(document["metrics"]) == COORDINATOR_METRICS
    assert document["capture_policy"] == "metadata_only"


def test_research_measurements_accept_only_metadata_dimensions() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        status="terminal",
        dependency="core_postgresql",
    )
    record = metrics.measurements[-1]
    allowed_wire_dimensions = {
        key.replace("errorClass", "error_class").replace("retrievalStage", "retrieval_stage")
        for key in ALLOWED_DIMENSIONS
    }
    assert {key for key, _ in record.dimensions} <= allowed_wire_dimensions
    assert not any(
        forbidden in repr(record.dimensions).casefold() for forbidden in FORBIDDEN_CONTENT
    )


def test_research_metrics_reject_correlation_identifiers_and_unknown_outcomes() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        trace_id="a" * 32,
        operation="research.coordinate",
        outcome="success",
        dependency="none",
    )
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="not-a-research-outcome",
        dependency="none",
    )
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 2


def test_research_metrics_reject_unknown_failure_classes() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="failed",
        dependency="core_postgresql",
        error_class="invented_failure_class",
    )
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 1


@pytest.mark.parametrize(
    ("metric", "attributes"),
    (
        ("not_registered", {"operation": "research.coordinate", "outcome": "success"}),
        (
            "research_coordinate_outcome_count",
            {"operation": "research.unknown", "outcome": "success", "dependency": "none"},
        ),
        (
            "research_coordinate_outcome_count",
            {"operation": "research.coordinate", "outcome": "success", "dependency": "wrong"},
        ),
        (
            "research_coordinate_outcome_count",
            {
                "operation": "research.coordinate",
                "outcome": "success",
                "dependency": "none",
                "error_class": "made_up",
            },
        ),
        (
            "research_coordinate_outcome_count",
            {
                "operation": "research.coordinate",
                "outcome": "success",
                "dependency": "none",
                "question": "private",
            },
        ),
    ),
)
def test_metadata_metrics_reject_every_invalid_research_sample(
    metric: str, attributes: dict[str, str]
) -> None:
    metrics = MetadataMetrics()
    metrics.increment(COMPONENT, metric, **attributes)
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 1


@pytest.mark.asyncio
async def test_exporter_outage_does_not_raise_or_change_retained_research_observations() -> None:
    class FailingExporter:
        async def export(self, batch: object) -> None:
            del batch
            raise RuntimeError("synthetic exporter outage")

    metrics = MetadataMetrics(exporter=FailingExporter(), max_export_attempts=1)
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        dependency="none",
    )
    assert await metrics.flush() is False
    assert metrics.stats().export_failures == 1
    assert metrics.stats().abandoned_batches == 1


@pytest.mark.asyncio
async def test_fallback_telemetry_exporter_outage_does_not_change_research_outcome() -> None:
    from aura_core.domains.knowledge.research.public import (
        ResearchCoordinator,
        ResearchMode,
    )
    from test_research_lifecycle_evaluation import (
        OWNER,
        _providers,
        _service,
        _TrustedClosedSynthesizer,
    )

    class FailingExporter:
        async def export(self, batch: object) -> None:
            del batch
            raise RuntimeError("synthetic telemetry outage")

    providers = _providers()
    baseline_service, _ = _service(_providers())
    baseline_admission = await baseline_service.admit(
        *OWNER, question="telemetry outage", mode=ResearchMode.QUICK
    )
    baseline = await ResearchCoordinator(
        baseline_service,
        baseline_service.provider_clients,
        synthesizer=_TrustedClosedSynthesizer(),
    ).run_once(baseline_admission.research_id)

    metrics = MetadataMetrics(exporter=FailingExporter(), max_export_attempts=1)
    service, _ = _service(providers)
    admission = await service.admit(*OWNER, question="telemetry outage", mode=ResearchMode.QUICK)
    result = await ResearchCoordinator(
        service,
        providers,
        metrics=metrics,
        synthesizer=_TrustedClosedSynthesizer(),
    ).run_once(admission.research_id)
    await metrics.flush()

    assert result.outcome is baseline.outcome
    assert metrics.stats().export_failures == 1


@pytest.mark.asyncio
async def test_fallback_repository_outage_preserves_outcome_without_ratio_sample() -> None:
    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
        ResearchCoordinator,
        ResearchMode,
    )
    from test_research_lifecycle_evaluation import (
        OWNER,
        _providers,
        _service,
        _TrustedClosedSynthesizer,
    )

    class BrokenFallbackRepository(MemoryResearchRepository):
        async def record_fallback(self, now, *, accepted: bool):
            del now, accepted
            raise RuntimeError("synthetic fallback repository outage")

    baseline_service, _ = _service(_providers())
    baseline_admission = await baseline_service.admit(
        *OWNER, question="fallback outage", mode=ResearchMode.QUICK
    )
    baseline = await ResearchCoordinator(
        baseline_service,
        baseline_service.provider_clients,
        synthesizer=_TrustedClosedSynthesizer(),
    ).run_once(baseline_admission.research_id)

    metrics = MetadataMetrics()
    providers = _providers()
    repository = BrokenFallbackRepository()
    service = ResearchApplicationService(repository, provider_clients=providers)
    admission = await service.admit(*OWNER, question="fallback outage", mode=ResearchMode.QUICK)
    result = await ResearchCoordinator(
        service,
        providers,
        metrics=metrics,
        synthesizer=_TrustedClosedSynthesizer(),
    ).run_once(admission.research_id)

    assert result.outcome is baseline.outcome
    assert not any(
        item.metric == "research_static_to_browser_fallback_ratio"
        for item in metrics.measurements
    )


def test_research_manifests_declare_the_release_evaluator_set() -> None:
    required = {
        "research_source_relevance@1",
        "research_claim_support@1",
        "research_citation_precision@1",
        "research_freshness@1",
        "research_source_diversity@1",
        "research_unsupported_claims@1",
        "research_budget_compliance@1",
        "research_evidence_sufficiency@1",
        "research_prompt_injection_resistance@1",
        "research_failure_isolation@1",
        "research_privacy@1",
        "research_deletion_truth@1",
    }
    declared: set[str] = set()
    for path in (ROOT / "resources" / "component-manifests").glob("research-*.yaml"):
        declared.update(yaml.safe_load(path.read_text(encoding="utf-8")).get("evaluators", ()))
    assert required <= declared


@pytest.mark.parametrize("dimension", sorted(FORBIDDEN_CONTENT))
def test_private_research_fields_cannot_be_telemetry_dimensions(dimension: str) -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        **{dimension: "private"},
    )
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 1


def test_required_metric_units_and_clock_boundaries_are_declared() -> None:
    manifest_path = ROOT / "resources" / "component-manifests" / "research-coordinator.yaml"
    document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    definitions = {item["name"]: item for item in document["metric_definitions"]}
    assert definitions["research_queue_lag_ms"]["unit"] == "milliseconds"
    assert definitions["research_budget_wall_time_consumed_ms"]["unit"] == "milliseconds"
    assert definitions["research_static_to_browser_fallback_ratio"]["unit"] == "ratio"
    assert definitions["research_budget_sources_consumed_count"]["unit"] == "count"


def test_research_component_dependency_and_operation_matrix_is_closed() -> None:
    expected = {
        "aura.research.coordinator@1": (
            {"core_postgresql", "transactional_outbox"},
            "research.coordinate",
            "core_postgresql",
        ),
        "aura.research.cancellation@1": (
            {"core_postgresql", "static_fetcher_cancel_port", "browser_fetcher_cancel_port"},
            "research.cancel_command",
            "core_postgresql",
        ),
        "aura.research.planner@1": (
            {"model_gateway", "core_postgresql"},
            "research.plan",
            "model_gateway",
        ),
        "aura.research.search@1": ({"searxng_port"}, "research.search", "searxng_port"),
        "aura.research.static_fetch@1": (
            {"static_fetcher_port", "url_policy", "content_policy"},
            "research.fetch_static",
            "static_fetcher_port",
        ),
        "aura.research.browser_fetch@1": (
            {"browser_fetcher_port", "url_policy", "content_policy"},
            "research.fetch_browser",
            "browser_fetcher_port",
        ),
        "aura.research.extraction@1": ({"content_policy"}, "research.extract", "content_policy"),
        "aura.research.evidence_validation@1": (
            {"url_policy", "content_policy", "evidence_repository"},
            "research.evidence_validate",
            "content_policy",
        ),
        "aura.research.citation_validation@1": (
            {"evidence_repository", "citation_validator"},
            "research.citation_validate",
            "citation_validator",
        ),
        "aura.research.synthesis@1": (
            {"model_gateway", "citation_validator", "artifacts_public_api"},
            "research.synthesize",
            "model_gateway",
        ),
        "aura.research.deletion@1": (
            {"core_postgresql", "artifacts_public_api"},
            "research.delete",
            "core_postgresql",
        ),
    }
    found: set[str] = set()
    for path in (ROOT / "resources" / "component-manifests").glob("research-*.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        component = document["component"]["id"]
        if component not in expected:
            continue
        found.add(component)
        dependencies, operation, span_dependency = expected[component]
        assert set(document["dependencies"]) == dependencies
        assert document["span"]["operation"] == operation
        assert document["span"]["dependency"] == span_dependency
    assert found == set(expected)


@pytest.mark.asyncio
async def test_terminal_budget_ledger_emits_zero_valued_counters() -> None:
    from aura_core.domains.knowledge.research.public import ResearchCoordinator, ResearchMode
    from aura_core.providers.research.contracts import SearchResult
    from test_research_lifecycle_evaluation import OWNER, _providers

    providers = _providers()
    providers.search.result = SearchResult(candidates=())
    metrics = MetadataMetrics()
    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
    )

    service = ResearchApplicationService(
        MemoryResearchRepository(), provider_clients=providers, metrics=metrics
    )
    admission = await service.admit(*OWNER, question="zero counters", mode=ResearchMode.QUICK)
    await ResearchCoordinator(service, providers, metrics=metrics).run_once(admission.research_id)

    observations = {
        item.metric: item.value
        for item in metrics.measurements
        if item.metric.startswith("research_budget_")
        and item.metric.endswith(("_ms", "_count"))
    }
    assert observations["research_budget_sources_consumed_count"] == 0
    assert observations["research_budget_browser_pages_consumed_count"] == 0
    assert observations["research_budget_model_calls_consumed_count"] == 0
    assert observations["research_budget_wall_time_consumed_ms"] == 0


@pytest.mark.asyncio
async def test_fallback_ratio_is_zero_without_browser_fallback_and_resets_after_five_minutes(
) -> None:
    from types import SimpleNamespace

    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
        ResearchCoordinator,
        ResearchMode,
    )
    from test_research_lifecycle_evaluation import OWNER, _providers, _Settings

    providers = _providers()
    metrics = MetadataMetrics()
    service = ResearchApplicationService(
        MemoryResearchRepository(), provider_clients=providers, metrics=metrics
    )
    first = await service.admit(*OWNER, question="cohort one", mode=ResearchMode.QUICK)
    clock_value = 0.0
    coordinator = ResearchCoordinator(
        service,
        providers,
        metrics=metrics,
        clock=lambda: clock_value,
    )
    await coordinator.run_once(first.research_id)
    first_ratio = [
        item.value
        for item in metrics.measurements
        if item.metric == "research_static_to_browser_fallback_ratio"
    ]
    assert first_ratio[-1] == 0

    class Inadequate:
        async def fetch(self, request: object) -> object:
            del request
            return SimpleNamespace(outcome="inadequate_extraction")

    providers.static_fetch = Inadequate()  # type: ignore[assignment]
    providers.browser_fetch = _providers().static_fetch
    providers.settings = _Settings(browser_fetch_enabled=True)
    second = await service.admit(*OWNER, question="cohort two", mode=ResearchMode.QUICK)
    clock_value = 301.0
    await coordinator.run_once(second.research_id)
    ratios = [
        item.value
        for item in metrics.measurements
        if item.metric == "research_static_to_browser_fallback_ratio"
    ]
    assert ratios[-1] == 1


@pytest.mark.asyncio
async def test_fallback_ratio_is_shared_across_coordinators_for_five_minutes() -> None:
    from types import SimpleNamespace

    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
        ResearchCoordinator,
        ResearchMode,
    )
    from test_research_lifecycle_evaluation import OWNER, _providers, _Settings

    providers = _providers()

    class Inadequate:
        async def fetch(self, request: object) -> object:
            del request
            return SimpleNamespace(outcome="inadequate_extraction")

    providers.static_fetch = Inadequate()  # type: ignore[assignment]
    providers.browser_fetch = _providers().static_fetch
    providers.settings = _Settings(browser_fetch_enabled=True)
    metrics = MetadataMetrics()
    service = ResearchApplicationService(
        MemoryResearchRepository(), provider_clients=providers, metrics=metrics
    )
    admission_one = await service.admit(
        *OWNER, question="shared fallback one", mode=ResearchMode.QUICK
    )
    admission_two = await service.admit(
        *OWNER, question="shared fallback two", mode=ResearchMode.QUICK
    )
    clock_value = 0.0

    def clock() -> float:
        return clock_value

    coordinator_one = ResearchCoordinator(service, providers, metrics=metrics, clock=clock)
    coordinator_two = ResearchCoordinator(service, providers, metrics=metrics, clock=clock)
    await coordinator_one.run_once(admission_one.research_id)
    providers.static_fetch = _providers().static_fetch
    clock_value = 10.0
    await coordinator_two.run_once(admission_two.research_id)

    ratios = [
        item.value
        for item in metrics.measurements
        if item.metric == "research_static_to_browser_fallback_ratio"
    ]
    assert ratios[-2:] == [1.0, 0.5]
    fallback_counts = [
        item
        for item in metrics.measurements
        if item.metric == "research_static_to_browser_fallback_count"
    ]
    static_settlements = [
        item
        for item in metrics.measurements
        if item.metric == "research_static_fetch_settled_count"
    ]
    assert len(fallback_counts) == 1
    assert len(static_settlements) == 2


@pytest.mark.asyncio
async def test_fallback_ratio_is_deployment_cohort_not_coordinator_instance_state() -> None:
    from types import SimpleNamespace

    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
        ResearchCoordinator,
        ResearchMode,
    )
    from test_research_lifecycle_evaluation import OWNER, _providers, _Settings

    providers = _providers()

    class Inadequate:
        async def fetch(self, request: object) -> object:
            del request
            return SimpleNamespace(outcome="inadequate_extraction")

    providers.static_fetch = Inadequate()  # type: ignore[assignment]
    providers.browser_fetch = _providers().static_fetch
    providers.settings = _Settings(browser_fetch_enabled=True)
    second_providers = _providers()
    metrics_one = MetadataMetrics()
    metrics_two = MetadataMetrics()
    repository = MemoryResearchRepository()
    service = ResearchApplicationService(
        repository, provider_clients=providers, metrics=metrics_one
    )
    second_service = ResearchApplicationService(
        repository, provider_clients=second_providers, metrics=metrics_two
    )
    first = await service.admit(*OWNER, question="deployment cohort one", mode=ResearchMode.QUICK)
    second = await second_service.admit(
        *OWNER, question="deployment cohort two", mode=ResearchMode.QUICK
    )

    def first_clock() -> float:
        return 100.0

    def second_clock() -> float:
        return 101.0

    coordinator_one = ResearchCoordinator(
        service, providers, metrics=metrics_one, clock=first_clock
    )
    coordinator_two = ResearchCoordinator(
        second_service, second_providers, metrics=metrics_two, clock=second_clock
    )
    await coordinator_one.run_once(first.research_id)
    await coordinator_two.run_once(second.research_id)

    ratios = [
        item.value
        for metrics in (metrics_one, metrics_two)
        for item in metrics.measurements
        if item.metric == "research_static_to_browser_fallback_ratio"
    ]
    assert ratios[-2:] == [1.0, 0.5]


@pytest.mark.asyncio
async def test_runtime_phase_spans_wrap_each_active_research_operation() -> None:
    from aura_core.domains.knowledge.research.public import ResearchCoordinator, ResearchMode
    from test_research_lifecycle_evaluation import OWNER, _providers, _service

    metrics = MetadataMetrics()
    providers = _providers()
    service, _ = _service(providers)
    admission = await service.admit(*OWNER, question="phase span runtime", mode=ResearchMode.QUICK)
    await ResearchCoordinator(service, providers, metrics=metrics).run_once(admission.research_id)

    operations = {
        dict(item.trace_attributes).get("operation")
        for item in metrics.measurements
        if item.kind == "span"
    }
    active_spans = [item for item in metrics.measurements if item.kind == "span"]
    assert {
        "research.coordinate",
        "research.plan",
        "research.search",
        "research.fetch_static",
        "research.extract",
        "research.evidence_validate",
        "research.citation_validate",
        "research.synthesize",
    } <= operations
    assert all(item.value > 0 for item in active_spans)
    assert "research.checkpoint" not in operations
    assert all(
        dict(item.trace_attributes).get("operation") != "research.checkpoint"
        for item in active_spans
    )


def test_research_metrics_reject_wrong_component_metric_dependency_error_and_dimension() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        "aura.research.search@1",
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        dependency="core_postgresql",
    )
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        dependency="not-a-dependency",
    )
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="failed",
        dependency="core_postgresql",
        error_class="not-an-error-class",
    )
    metrics.increment(
        COMPONENT,
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        dependency="core_postgresql",
        unexpected_dimension="private",
    )
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 4


def test_provider_telemetry_classifications_and_declared_manifest_metrics_round_trip() -> None:
    from aura_core.platform.telemetry import (
        _RESEARCH_COMPONENT_DEPENDENCIES,
        _RESEARCH_METRIC_DEPENDENCIES,
        _RESEARCH_METRIC_OPERATIONS,
    )
    from aura_core.providers.research.client import (
        _TELEMETRY_CLASSIFICATIONS,
        telemetry_classification,
    )
    from aura_core.providers.research.contracts import ProviderErrorCode

    for code, classification in _TELEMETRY_CLASSIFICATIONS.items():
        assert telemetry_classification(code) == classification
    assert {code.value for code in ProviderErrorCode} <= set(_TELEMETRY_CLASSIFICATIONS)

    for manifest_path in (ROOT / "resources" / "component-manifests").glob(
        "research-*.yaml"
    ):
        document = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
        component = document["component"]["id"]
        if component not in _METRICS:
            continue
        for definition in document.get("metric_definitions", ()):
            metric = definition["name"]
            assert metric in _METRICS[component]
            labels = set(definition.get("labels", ()))
            attributes: dict[str, str] = {}
            if "operation" in labels:
                declared_operation = document.get("span", {}).get("operation")
                operations = _RESEARCH_METRIC_OPERATIONS.get(
                    metric, ("research.coordinate",)
                )
                attributes["operation"] = next(
                    (
                        operation
                        for operation in operations
                        if operation == declared_operation
                    ),
                    next(iter(operations)),
                )
            if "outcome" in labels:
                # Use the universally declared success tuple for the smoke
                # round-trip; set iteration must not select a deletion-only
                # partial/failed tuple lacking its required error class.
                attributes["outcome"] = "success"
            if "status" in labels:
                attributes["status"] = "running"
            if "dependency" in labels:
                component_dependencies = _RESEARCH_COMPONENT_DEPENDENCIES.get(
                    component, ()
                )
                metric_dependencies = _RESEARCH_METRIC_DEPENDENCIES.get(
                    metric, component_dependencies
                )
                dependency = next(
                    iter(
                        component_dependency
                        for component_dependency in component_dependencies
                        if component_dependency in metric_dependencies
                    ),
                    None,
                )
                if dependency is None and None not in metric_dependencies:
                    dependency = next(iter(metric_dependencies), None)
                if dependency is not None:
                    attributes["dependency"] = dependency
            if "provider" in labels:
                attributes["provider"] = "static_fetcher"
            if "degradation" in labels:
                attributes["degradation"] = "none"
            metrics = MetadataMetrics()
            metrics.increment(component, metric, **attributes)
            assert metrics.measurements, (manifest_path, metric, attributes)


def test_research_telemetry_matrix_rejects_cross_component_operation_status_and_domain_errors(
) -> None:
    metrics = MetadataMetrics()
    # Planner-owned metric with coordinator operation/status and deletion error.
    metrics.increment(
        "aura.research.planner@1",
        "research_plan_outcome_count",
        operation="research.delete",
        outcome="success",
        status="deleted",
        dependency="model_gateway",
    )
    # Evidence validation explicitly accepts redirect_blocked.
    metrics.increment(
        "aura.research.evidence_validation@1",
        "research_evidence_validation_outcome_count",
        operation="research.evidence_validate",
        outcome="rejected",
        status="failed",
        error_class="redirect_blocked",
        dependency="content_policy",
    )
    # Deletion does not accept provider cleanup_unknown as a domain error.
    metrics.increment(
        "aura.research.deletion@1",
        "research_deletion_outcome_count",
        operation="research.delete",
        outcome="partial",
        status="failed",
        error_class="cleanup_unknown",
        dependency="core_postgresql",
    )
    # Partial/canceled/resource-exhausted are valid closed outcomes where their
    # owning component advertises them; malformed cross-domain labels are not.
    metrics.increment(
        "aura.research.coordinator@1",
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="partial",
        status="running",
        dependency="core_postgresql",
    )
    metrics.increment(
        "aura.research.coordinator@1",
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="canceled",
        status="running",
        dependency="core_postgresql",
    )
    metrics.increment(
        "aura.research.static_fetch@1",
        "research_static_fetch_outcome_count",
        operation="research.fetch_static",
        outcome="rejected",
        status="fetching",
        error_class="resource_exhausted",
        dependency="static_fetcher_port",
    )
    assert len(metrics.measurements) == 4
    assert metrics.stats().rejected == 2


def test_research_telemetry_exact_tuple_matrix_preserves_owner_and_domain_rules() -> None:
    cases = (
        (
            "aura.research.planner@1",
            "research_plan_outcome_count",
            {
                "operation": "research.plan",
                "outcome": "success",
                "status": "fetching",
                "dependency": "model_gateway",
            },
            False,
        ),
        (
            "aura.research.planner@1",
            "research_plan_outcome_count",
            {
                "operation": "research.plan",
                "outcome": "success",
                "status": "completed",
                "dependency": "model_gateway",
            },
            False,
        ),
        (
            "aura.research.planner@1",
            "research_plan_outcome_count",
            {
                "operation": "research.plan",
                "outcome": "success",
                "status": "running",
                "dependency": "model_gateway",
            },
            False,
        ),
        (
            "aura.research.static_fetch@1",
            "research_static_fetch_outcome_count",
            {
                "operation": "research.fetch_static",
                "outcome": "success",
                "status": "fetching",
                "error_class": "resource_exhausted",
                "dependency": "static_fetcher_port",
            },
            False,
        ),
        (
            "aura.research.static_fetch@1",
            "research_static_fetch_outcome_count",
            {
                "operation": "research.fetch_static",
                "outcome": "failed",
                "status": "fetching",
                "error_class": "resource_exhausted",
                "dependency": "static_fetcher_port",
            },
            True,
        ),
        (
            "aura.research.evidence_validation@1",
            "research_evidence_validation_outcome_count",
            {
                "operation": "research.evidence_validate",
                "outcome": "rejected",
                "status": "failed",
                "error_class": "redirect_blocked",
                "dependency": "content_policy",
            },
            True,
        ),
        (
            "aura.research.deletion@1",
            "research_deletion_outcome_count",
            {
                "operation": "research.delete",
                "outcome": "partial",
                "status": "cleanup_unknown",
                "error_class": "cleanup_unknown",
                "dependency": "core_postgresql",
            },
            False,
        ),
        (
            "aura.research.coordinator@1",
            "research_coordinate_outcome_count",
            {
                "operation": "research.coordinate",
                "outcome": "partial",
                "status": "partial",
                "dependency": "core_postgresql",
            },
            True,
        ),
        (
            "aura.research.coordinator@1",
            "research_coordinate_outcome_count",
            {
                "operation": "research.coordinate",
                "outcome": "canceled",
                "status": "canceled",
                "dependency": "core_postgresql",
            },
            True,
        ),
    )
    for component, metric, attributes, accepted in cases:
        metrics = MetadataMetrics()
        metrics.increment(component, metric, **attributes)
        assert bool(metrics.measurements) is accepted, (component, metric, attributes)
        assert metrics.stats().rejected == (0 if accepted else 1)


@pytest.mark.parametrize(
    ("outcome", "error_class", "accepted"),
    (
        ("failed", "storage_unavailable", True),
        ("success", "storage_unavailable", False),
        ("partial", "storage_unavailable", False),
        ("unknown", "storage_unavailable", False),
    ),
)
def test_deletion_storage_unavailable_metric_and_span_round_trip(
    outcome: str, error_class: str, accepted: bool
) -> None:
    attributes = {
        "operation": "research.delete",
        "outcome": outcome,
        "error_class": error_class,
        "dependency": "core_postgresql",
    }
    metrics = MetadataMetrics()
    metrics.increment(
        "aura.research.deletion@1",
        "research_deletion_outcome_count",
        **attributes,
    )
    assert bool(metrics.measurements) is accepted
    assert metrics.stats().rejected == (0 if accepted else 1)

    metrics = MetadataMetrics()
    metrics.record_span(
        "aura.research.deletion@1",
        "research.delete",
        1.0,
        dependency="core_postgresql",
        outcome=outcome,
        error_class=error_class,
        trace_id="a" * 32,
        span_id="b" * 16,
        parent_span_id=None,
    )
    assert bool(metrics.measurements) is accepted
    assert metrics.stats().rejected == (0 if accepted else 1)


def test_deletion_manifest_errors_and_runtime_tuples_are_exhaustive() -> None:
    from aura_core.platform.telemetry import _RESEARCH_DELETION_TUPLES

    manifest = ROOT / "resources" / "component-manifests" / "research-deletion.yaml"
    document = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    declared_outcomes = set(document["outcomes"])
    declared_errors = set(document["errors"])
    runtime_tuples = set(_RESEARCH_DELETION_TUPLES)
    runtime_errors = {error for _, _, error in runtime_tuples if error is not None}

    assert declared_errors <= runtime_errors
    assert all(outcome in declared_outcomes for _, outcome, _ in runtime_tuples)
    assert all(error is None or error in declared_errors for _, _, error in runtime_tuples)


def test_research_span_exact_tuple_matrix_matches_metric_policy() -> None:
    cases = (
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "success",
            "core_postgresql",
            None,
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "partial",
            "core_postgresql",
            "cleanup_unknown",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "failed",
            "core_postgresql",
            "deletion_failed",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "rejected",
            "core_postgresql",
            "authorization_denied",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "rejected",
            "core_postgresql",
            "conflict",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "timeout",
            "core_postgresql",
            "timeout",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            None,
            "unknown",
            "core_postgresql",
            "cleanup_unknown",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            "active",
            "failed",
            "core_postgresql",
            "deletion_failed",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            "deletion_pending",
            "partial",
            "core_postgresql",
            "cleanup_unknown",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            "deletion_failed",
            "failed",
            "core_postgresql",
            "deletion_failed",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            "cleanup_unknown",
            "unknown",
            "core_postgresql",
            "cleanup_unknown",
            True,
        ),
        (
            "aura.research.deletion@1",
            "research.delete",
            "deleted",
            "success",
            "core_postgresql",
            None,
            True,
        ),
        (
            "aura.research.static_fetch@1",
            "research.fetch_static",
            None,
            "success",
            "static_fetcher_port",
            "resource_exhausted",
            False,
        ),
        (
            "aura.research.static_fetch@1",
            "research.fetch_static",
            None,
            "failed",
            "static_fetcher_port",
            "resource_exhausted",
            True,
        ),
        (
            "aura.research.static_fetch@1",
            "research.fetch_static",
            None,
            "rejected",
            "static_fetcher_port",
            "resource_exhausted",
            True,
        ),
        (
            "aura.research.static_fetch@1",
            "research.delete",
            None,
            "failed",
            "static_fetcher_port",
            "resource_exhausted",
            False,
        ),
        (
            "aura.research.static_fetch@1",
            "research.fetch_static",
            None,
            "failed",
            "static_fetcher_port",
            "cleanup_unknown",
            False,
        ),
    )
    for index, (
        component,
        operation,
        status,
        outcome,
        dependency,
        error_class,
        accepted,
    ) in enumerate(cases, start=1):
        metrics = MetadataMetrics()
        attributes = {
            "dependency": dependency,
            "outcome": outcome,
            "error_class": error_class,
            "trace_id": f"{index:032x}",
            "span_id": f"{index:016x}",
            "parent_span_id": None,
        }
        metrics.record_span(component, operation, 1.0, **attributes)
        assert bool(metrics.measurements) is accepted, (
            component,
            operation,
            status,
            outcome,
            error_class,
        )
        assert metrics.stats().rejected == (0 if accepted else 1)


def test_internal_and_cross_component_metrics_are_rejected_by_closed_ownership_matrix() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        "internal.research.coordinator",
        "research_coordinate_outcome_count",
        operation="research.coordinate",
        outcome="success",
        dependency="core_postgresql",
    )
    metrics.increment(
        "aura.research.search@1",
        "research_static_fetch_settled_count",
        operation="research.fetch_static",
        outcome="success",
        dependency="static_fetcher_port",
    )
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 2


def test_child_and_cross_phase_metric_ownership_is_closed() -> None:
    metrics = MetadataMetrics()
    metrics.increment(
        COMPONENT,
        "research_search_outcome_count",
        operation="research.search",
        outcome="success",
        dependency="searxng_port",
    )
    metrics.increment(
        "aura.research.planner@1",
        "research_plan_outcome_count",
        operation="research.delete",
        outcome="deleted",
        dependency="model_gateway",
    )
    assert metrics.measurements == ()
    assert metrics.stats().rejected == 2


def test_phase_spans_preserve_declared_operation_and_dependency_matrix() -> None:
    expected = {
        "aura.research.coordinator@1": ("research.coordinate", "core_postgresql"),
        "aura.research.cancellation@1": ("research.cancel_command", "core_postgresql"),
        "aura.research.planner@1": ("research.plan", "model_gateway"),
        "aura.research.search@1": ("research.search", "searxng_port"),
        "aura.research.static_fetch@1": ("research.fetch_static", "static_fetcher_port"),
        "aura.research.browser_fetch@1": ("research.fetch_browser", "browser_fetcher_port"),
        "aura.research.extraction@1": ("research.extract", "content_policy"),
        "aura.research.evidence_validation@1": (
            "research.evidence_validate",
            "content_policy",
        ),
        "aura.research.citation_validation@1": (
            "research.citation_validate",
            "citation_validator",
        ),
        "aura.research.synthesis@1": ("research.synthesize", "model_gateway"),
        "aura.research.deletion@1": ("research.delete", "core_postgresql"),
    }
    metrics = MetadataMetrics()
    for index, (component, (operation, dependency)) in enumerate(expected.items(), start=1):
        metrics.record_span(
            component,
            operation,
            1.0,
            trace_id=f"{index:032x}",
            span_id=f"{index:016x}",
            parent_span_id=None,
            dependency=dependency,
            outcome="success",
        )
    assert len(metrics.measurements) == len(expected)
    assert {
        (
            item.component_id,
            dict(item.dimensions)["dependency"],
            dict(item.trace_attributes)["operation"],
        )
        for item in metrics.measurements
    } == {
        (component, dependency, operation)
        for component, (operation, dependency) in expected.items()
    }


def test_typed_research_evaluators_have_positive_negative_and_privacy_contracts() -> None:
    from aura_core.providers.research import evaluation

    required = {
        "research_source_relevance@1",
        "research_claim_support@1",
        "research_citation_precision@1",
        "research_freshness@1",
        "research_source_diversity@1",
        "research_unsupported_claims@1",
        "research_budget_compliance@1",
        "research_evidence_sufficiency@1",
        "research_prompt_injection_resistance@1",
        "research_failure_isolation@1",
        "research_privacy@1",
        "research_deletion_truth@1",
    }
    assert required <= set(evaluation.EVALUATORS)
    suite = evaluation.run_research_evaluation_fixtures(
        evaluation.DeterministicResearchEvaluationCaptureAdapter()
    )
    assert {result.evaluator_id for result in suite.baseline} == required
    assert all(result.outcome == "pass" and result.score == 1.0 for result in suite.baseline)
    assert len({type(result.score_v1) for result in suite.baseline}) == len(required)
    assert all(result.score_v1.passed for result in suite.baseline)
    assert {result.evaluator_id for result in suite.regressions} == required
    assert all(result.outcome == "fail" and result.score == 0.0 for result in suite.regressions)
    assert all(not result.score_v1.passed for result in suite.regressions)
    for result in (*suite.baseline, *suite.regressions):
        payload = result.payload()
        assert set(payload) == {
            "evaluatorId", "evaluatorVersion", "componentId", "componentVersion",
            "fixtureRevision", "replayKey", "score", "outcome", "metadata", "scoreV1",
        }
        assert isinstance(payload["scoreV1"], dict)
        assert payload["scoreV1"]
        rendered = repr(payload).casefold()
        assert not any(secret in rendered for secret in FORBIDDEN_CONTENT)


def test_quality_evaluators_fail_closed_without_captured_execution_evidence() -> None:
    from aura_core.providers.research import evaluation

    with pytest.raises(ValueError, match="captured execution evidence"):
        evaluation._quality_fixture_results(regression=False)


def test_evaluation_runner_invokes_the_supplied_capture_adapter() -> None:
    from aura_core.providers.research import evaluation

    delegate = evaluation.DeterministicResearchEvaluationCaptureAdapter()
    calls: list[tuple[bool, object]] = []

    class Adapter:
        async def capture(self, *, regression: bool, metrics: object):
            calls.append((regression, metrics))
            return await delegate.capture(regression=regression, metrics=metrics)

    suite = evaluation.run_research_evaluation_fixtures(Adapter())
    assert suite.baseline and suite.regressions
    assert [regression for regression, _ in calls] == [False, True]
    assert calls[0][1] is not calls[1][1]


def test_deterministic_capture_is_explicitly_marked_non_release_evidence() -> None:
    import asyncio

    from aura_core.providers.research import evaluation

    capture = asyncio.run(
        evaluation.DeterministicResearchEvaluationCaptureAdapter().capture(
            regression=False, metrics=MetadataMetrics()
        )
    )
    assert capture.provenance == "offline_fixture"


@pytest.mark.parametrize("missing_sink", ("metrics", "spans", "events", "logs", "browser_state"))
def test_capture_runner_never_passes_privacy_without_complete_sink_capture(
    missing_sink: str,
) -> None:
    from aura_core.providers.research import evaluation

    delegate = evaluation.DeterministicResearchEvaluationCaptureAdapter()

    class IncompleteAdapter:
        async def capture(self, *, regression: bool, metrics: object):
            captured = await delegate.capture(regression=regression, metrics=metrics)
            sinks = tuple(
                record
                for record in captured.privacy_sinks
                if record.get("sink") != missing_sink
            )
            return evaluation.ResearchEvaluationCapture(
                quality=captured.quality,
                privacy_sinks=sinks,
            )

    suite = evaluation.run_research_evaluation_fixtures(IncompleteAdapter())
    baseline = next(
        item for item in suite.baseline if item.evaluator_id == "research_privacy@1"
    )
    assert baseline.outcome == "fail"
    assert baseline.score == 0.0


def test_required_evaluator_components_are_declared_by_their_manifest() -> None:
    from aura_core.providers.research.evaluation import EVALUATOR_COMPONENTS

    declared: dict[str, set[str]] = {}
    for path in (ROOT / "resources" / "component-manifests").glob("research-*.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        declared[document["component"]["id"]] = set(document.get("evaluators", ()))
    required = {
        "research_source_relevance@1",
        "research_claim_support@1",
        "research_citation_precision@1",
        "research_freshness@1",
        "research_source_diversity@1",
        "research_unsupported_claims@1",
        "research_budget_compliance@1",
        "research_evidence_sufficiency@1",
        "research_prompt_injection_resistance@1",
        "research_failure_isolation@1",
        "research_privacy@1",
        "research_deletion_truth@1",
    }
    assert all(
        evaluator in declared.get(EVALUATOR_COMPONENTS[evaluator], set())
        for evaluator in required
    )


@pytest.mark.asyncio
async def test_runtime_coordinator_emits_stage_budget_and_terminal_observations() -> None:
    from aura_core.domains.knowledge.research.public import ResearchCoordinator, ResearchMode
    from test_research_lifecycle_evaluation import OWNER, _providers, _service

    class Sink:
        def __init__(self) -> None:
            self.records: list[tuple[str, str, dict[str, str]]] = []

        def increment(self, component: str, name: str, **attributes: str) -> None:
            self.records.append((component, name, attributes))

        def observe(self, component: str, name: str, value: float, **attributes: str) -> None:
            del value
            self.records.append((component, name, attributes))

        def record_span(
            self, component: str, operation: str, duration_ms: float, **attributes: object
        ) -> None:
            del duration_ms
            self.records.append(
                (component, operation, {key: str(value) for key, value in attributes.items()})
            )

    providers = _providers()
    service, _ = _service(providers)
    sink = Sink()
    admission = await service.admit(*OWNER, question="runtime clocks", mode=ResearchMode.QUICK)
    result = await ResearchCoordinator(service, providers, metrics=sink).run_once(
        admission.research_id
    )
    assert result.outcome is not None
    names = {name for _, name, _ in sink.records}
    assert {
        "research_coordinate_outcome_count",
        "research_checkpoint_count",
        "research_budget_wall_time_consumed_ms",
        "research_budget_sources_consumed_count",
    } <= names
    assert all(
        not any(secret in repr(attributes).casefold() for secret in FORBIDDEN_CONTENT)
        for _, _, attributes in sink.records
    )


@pytest.mark.asyncio
async def test_queue_cancel_and_settlement_clocks_are_distinct_and_nonzero() -> None:
    from datetime import UTC, datetime, timedelta

    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
        ResearchCoordinator,
        ResearchMode,
    )
    from test_research_lifecycle_evaluation import OWNER, _providers

    class WallClock:
        value = datetime(2026, 10, 30, tzinfo=UTC)

        def __call__(self) -> datetime:
            current = self.value
            self.value += timedelta(seconds=1)
            return current

    providers = _providers()
    metrics = MetadataMetrics()
    service = ResearchApplicationService(
        MemoryResearchRepository(), provider_clients=providers, metrics=metrics, clock=WallClock()
    )
    admission = await service.admit(*OWNER, question="clock boundaries", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    canceled = await service.cancel(*OWNER, task.id, expected_version=task.version)
    await ResearchCoordinator(service, providers, metrics=metrics).run_once(canceled.id)

    measurements = {
        item.metric: item.value
        for item in metrics.measurements
        if item.metric
        in {
            "research_queue_lag_ms",
            "research_cancel_command_latency_ms",
            "research_cancel_settlement_lag_ms",
        }
    }
    assert set(measurements) == {
        "research_queue_lag_ms",
        "research_cancel_command_latency_ms",
        "research_cancel_settlement_lag_ms",
    }
    assert all(value > 0 for value in measurements.values())


@pytest.mark.asyncio
async def test_cancellation_and_deletion_emit_component_local_spans() -> None:
    from aura_core.domains.knowledge.research.public import (
        MemoryResearchRepository,
        ResearchApplicationService,
        ResearchMode,
        ResearchOutcome,
    )
    from test_research_lifecycle_evaluation import OWNER, _providers

    providers = _providers()
    metrics = MetadataMetrics()
    service = ResearchApplicationService(
        MemoryResearchRepository(), provider_clients=providers, metrics=metrics
    )
    admission = await service.admit(*OWNER, question="cancel span", mode=ResearchMode.QUICK)
    task = await service.get(*OWNER, admission.research_id)
    await service.cancel(*OWNER, task.id, expected_version=task.version)
    second = await service.admit(*OWNER, question="delete span", mode=ResearchMode.QUICK)
    second_task = await service.get(*OWNER, second.research_id)
    terminal = await service.settle(second_task, ResearchOutcome.COMPLETED)
    await service.delete(*OWNER, terminal.id, expected_version=terminal.version)

    spans = {
        (item.component_id, dict(item.trace_attributes).get("operation"))
        for item in metrics.measurements
        if item.kind == "span"
    }
    assert ("aura.research.cancellation@1", "research.cancel_command") in spans
    assert ("aura.research.deletion@1", "research.delete") in spans
