"""AURA-0094 bounded metrics, spans, and typed-manifest coverage."""
# pyright: reportPrivateUsage=false, reportUnknownArgumentType=false, reportUnknownVariableType=false, reportUnknownMemberType=false

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
import yaml
from aura_observatory.adapters.evaluation_orchestration import OtlpComponentSpanExporter
from aura_observatory.evaluation_calibration import (
    CalibrationError,
    CalibrationLabel,
    CalibrationService,
    InMemoryCalibrationRepository,
)
from aura_observatory.metrics import ObservatoryMetrics
from test_aura0094_calibration import IDS, _label, _qualification

ROOT = Path(__file__).parents[3]
MANIFESTS = ROOT / "services/aura-observatory/resources/component-manifests"


class _SpanTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, bytes, str | None, float]] = []

    async def send(
        self, endpoint: str, payload: bytes, credential: str | None, timeout: float
    ) -> None:
        self.calls.append((endpoint, payload, credential, timeout))


def test_local_judge_and_calibration_manifests_declare_every_metric_and_evaluator() -> None:
    expected = {
        "local-judges.yaml": {
            "observatory_local_judge_request_count",
            "observatory_local_judge_duration_ms",
            "observatory_local_judge_outcome_count",
            "observatory_local_judge_provider_failure_count",
        },
        "evaluation-calibration.yaml": {
            "observatory_judge_disagreement_count",
            "observatory_calibration_review_count",
            "observatory_calibration_qualification_count",
        },
    }
    for filename, metrics in expected.items():
        value = yaml.safe_load((MANIFESTS / filename).read_text(encoding="utf-8"))
        assert isinstance(value, dict)
        assert metrics <= set(value["metrics"])
        definitions = {item["name"] for item in value["metric_definitions"]}
        assert set(value["metrics"]) <= definitions
        assert set(value["cardinality"]["metric_attributes"]) <= {
            "outcome",
            "failure_code",
            "disagreement",
            "operation",
        }
        assert {
            "prompt",
            "response",
            "rubric",
            "rationale",
            "credential",
            "endpoint",
            "owner_identity",
        } <= set(value["cardinality"]["prohibited"])


def test_judge_metrics_have_finite_cardinality_and_spans_have_no_content() -> None:
    metrics = ObservatoryMetrics()
    metrics.record("observatory_local_judge_request_count", labels={"outcome": "pass"})
    metrics.record(
        "observatory_local_judge_provider_failure_count",
        labels={"failure_code": "provider_timeout"},
    )
    metrics.record("observatory_judge_disagreement_count", labels={"disagreement": "none"})
    metrics.record("observatory_calibration_review_count", labels={"outcome": "accepted"})
    metrics.record("observatory_calibration_qualification_count", labels={"outcome": "revoked"})
    metrics.record_component_span(
        component_id="aura.observatory.local_judges",
        component_version="1.0.0",
        deployment_id="observatory",
        operation="judge",
        duration_ms=12,
        outcome="failure",
        dependency="ollama",
        error_code="provider_timeout",
    )
    rendered = metrics.prometheus().casefold()
    assert "provider_timeout" in rendered
    assert "ollama.internal" not in rendered
    for forbidden in ("prompt", "response", "rubric", "rationale", "credential", "owner_identity"):
        assert forbidden not in rendered
    span = metrics.component_spans[-1]
    assert span.dependency == "ollama"
    assert span.error_code == "provider_timeout"


def test_migration_declares_all_recovery_state_and_prompt_rubric_usage() -> None:
    migration = (
        ROOT
        / (
            "services/aura-observatory/migrations/postgresql/versions/"
            "0010_local_judges_calibration.sql"
        )
    ).read_text(encoding="utf-8")
    for table in (
        "observatory_local_judge_execution_claims",
        "observatory_local_judge_calibration_claims",
        "observatory_local_judge_calibration_results",
        "observatory_local_judge_audits",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table}" in migration
    assert "status IN ('claimed', 'completed', 'failed')" in migration
    assert "intended_use IN ('prompt', 'rubric', 'input', 'comment')" in migration
    for private in ("prompt TEXT", "response TEXT", "rationale TEXT", "owner_identity", "endpoint"):
        assert private not in migration.casefold()


@pytest.mark.asyncio
async def test_calibration_services_emit_label_insufficient_revoke_and_failure_telemetry() -> None:
    metrics = ObservatoryMetrics()
    repository = InMemoryCalibrationRepository()
    service = CalibrationService(repository, metrics=metrics)
    await service.label(_label(501))
    report = await service.calibrate(
        judge_id=IDS["judge"],
        judge_version="1.0.0",
        prompt_version="prompt-1",
        rubric_version="rubric-1",
        calibration_version="calibration-1",
    )
    with pytest.raises(CalibrationError, match="qualification_denied"):
        await service.qualify(
            _qualification(),
            report,
            owner_authorized=False,
            idempotency_key=IDS["qualification"],
        )
    repository.qualifications[IDS["qualification"]] = _qualification()
    await service.revoke(IDS["qualification"], authorization_record_id=IDS["result"])

    durations = {
        dict(labels)["operation"]: dict(labels)["outcome"]
        for (name, labels), _value in metrics.values.items()
        if name == "observatory_calibration_duration_ms"
    }
    assert durations["label"] == "success"
    assert durations["calibrate"] == "failure"
    assert durations["revoke"] == "success"
    assert {span.operation for span in metrics.component_spans} >= {
        "label",
        "calibrate",
        "revoke",
    }
    assert any(
        span.operation == "calibrate" and span.error_code == "insufficient"
        for span in metrics.component_spans
    )
    assert any(
        span.operation == "qualify" and span.error_code == "qualification_denied"
        for span in metrics.component_spans
    )
    assert any(
        name == "observatory_calibration_qualification_count"
        and dict(labels).get("outcome") == "denied"
        for (name, labels), _value in metrics.values.items()
    )

    class _BrokenRepository(InMemoryCalibrationRepository):
        async def put_label(self, label: CalibrationLabel) -> CalibrationLabel:
            del label
            raise RuntimeError("database should not escape")

    broken_metrics = ObservatoryMetrics()
    with pytest.raises(CalibrationError, match="database_failure"):
        await CalibrationService(_BrokenRepository(), metrics=broken_metrics).label(_label(502))
    assert any(
        span.operation == "label" and span.error_code == "database_failure"
        for span in broken_metrics.component_spans
    )


@pytest.mark.asyncio
async def test_dependency_spans_share_correlation_and_exporter_flushes_all_boundaries() -> None:
    transport = _SpanTransport()
    exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/traces",
        credential="file-injected-test-secret",
        transport=transport,
        max_attempts=1,
    )
    metrics = ObservatoryMetrics(span_exporter=exporter)
    with metrics.trace_context("aura0094-correlation"):
        metrics.record_component_span(
            component_id="aura.observatory.evaluation_orchestration",
            component_version="1.0.0",
            deployment_id="observatory",
            operation="publish",
            duration_ms=1,
            outcome="failure",
            dependency="evaluation_nats",
            error_code="transport_unavailable",
        )
        metrics.record_component_span(
            component_id="aura.observatory.evaluation_orchestration",
            component_version="1.0.0",
            deployment_id="observatory",
            operation="result_apply",
            duration_ms=2,
            outcome="failure",
            dependency="postgresql",
            error_code="database_failure",
        )
        metrics.record_component_span(
            component_id="aura.observatory.local_judges",
            component_version="1.0.0",
            deployment_id="observatory",
            operation="generate",
            duration_ms=3,
            outcome="failure",
            dependency="ollama",
            error_code="provider_timeout",
        )

    spans = metrics.component_spans
    assert {span.dependency for span in spans} == {"evaluation_nats", "postgresql", "ollama"}
    assert {span.trace_id for span in spans} == {"aura0094-correlation"}
    assert len({span.span_id for span in spans}) == 3
    await metrics.flush()
    assert exporter.stats["accepted"] == 3
    assert exporter.stats["dropped"] == 0
    assert exporter.stats["failures"] == 0
    assert len(transport.calls) == 3
    payloads = b"".join(
        payload for _endpoint, payload, _credential, _timeout in transport.calls
    ).decode()
    assert all(
        marker not in payloads.casefold()
        for marker in ("prompt", "response", "credential", "endpoint")
    )


@pytest.mark.asyncio
async def test_exporter_flush_drains_error_and_cancelled_adapter_tasks() -> None:
    class _FailingTransport:
        async def send(
            self, endpoint: str, payload: bytes, credential: str | None, timeout: float
        ) -> None:
            del endpoint, payload, credential, timeout
            raise RuntimeError("collector unavailable")

    failed_transport = _FailingTransport()
    failed_exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/traces",
        transport=failed_transport,
        max_attempts=1,
    )
    failed_metrics = ObservatoryMetrics(span_exporter=failed_exporter)
    failed_metrics.record_component_span(
        component_id="aura.observatory.local_judges",
        component_version="1.0.0",
        deployment_id="observatory",
        operation="generate",
        duration_ms=1,
        outcome="failure",
        dependency="ollama",
        error_code="provider_failure",
    )
    await failed_metrics.flush()
    assert failed_exporter.stats == {"accepted": 1, "failures": 1, "dropped": 1, "retries": 0}

    class _BlockingTransport:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def send(
            self, endpoint: str, payload: bytes, credential: str | None, timeout: float
        ) -> None:
            del endpoint, payload, credential, timeout
            self.started.set()
            await self.release.wait()

    blocking_transport = _BlockingTransport()
    cancelled_exporter = OtlpComponentSpanExporter(
        "http://collector:4318/v1/traces",
        transport=blocking_transport,
        max_attempts=1,
    )
    cancelled_metrics = ObservatoryMetrics(span_exporter=cancelled_exporter)
    cancelled_metrics.record_component_span(
        component_id="aura.observatory.evaluation_orchestration",
        component_version="1.0.0",
        deployment_id="observatory",
        operation="publish",
        duration_ms=1,
        outcome="failure",
        dependency="evaluation_nats",
        error_code="transport_unavailable",
    )
    await blocking_transport.started.wait()
    assert cancelled_exporter._task is not None
    cancelled_exporter._task.cancel()
    await cancelled_metrics.flush(timeout_seconds=0.5)
    assert cancelled_exporter.stats["dropped"] == 1
    assert not cancelled_exporter.running
