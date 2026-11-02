"""AURA-0094 terminal worker dispatch coverage."""
# pyright: reportPrivateUsage=false, reportArgumentType=false

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from aura_observatory.entrypoints.evaluation_worker import _dispatch_local_work
from aura_observatory.metrics import ObservatoryMetrics
from test_aura0094_judges import _request


@dataclass(frozen=True)
class _Work:
    kind: str
    request: object | None = None
    arguments: dict[str, Any] | None = None


class _Service:
    def __init__(self, work: _Work | None, *, metrics: ObservatoryMetrics | None = None) -> None:
        self.work = work
        self.metrics = metrics
        self.completed: list[tuple[object, object]] = []
        self.failed: list[tuple[object, str]] = []

    async def claim_local_work(self) -> _Work | None:
        if self.metrics is not None:
            self.metrics.record_component_span(
                component_id="aura.observatory.evaluation_orchestration",
                component_version="1.0.0",
                deployment_id="observatory",
                operation="dispatch",
                duration_ms=1,
                outcome="success",
                dependency="postgresql",
            )
        return self.work

    async def complete_local_work(self, work: object, result: object) -> None:
        self.completed.append((work, result))
        if self.metrics is not None:
            self.metrics.record_component_span(
                component_id="aura.observatory.evaluation_orchestration",
                component_version="1.0.0",
                deployment_id="observatory",
                operation="dispatch",
                duration_ms=1,
                outcome="success",
                dependency="postgresql",
            )

    async def fail_local_work(self, work: object, code: str) -> None:
        self.failed.append((work, code))
        if self.metrics is not None:
            self.metrics.record_component_span(
                component_id="aura.observatory.evaluation_orchestration",
                component_version="1.0.0",
                deployment_id="observatory",
                operation="dispatch",
                duration_ms=1,
                outcome="failure",
                dependency="postgresql",
                error_code="storage_failure",
            )


class _Judge:
    async def evaluate(self, request: object) -> str:
        return f"score:{request}"


class _Calibration:
    async def calibrate(self, **arguments: Any) -> dict[str, Any]:
        return arguments


@pytest.mark.asyncio
async def test_terminal_judge_claim_invokes_service_and_persists_completion() -> None:
    request = _request()
    service = _Service(_Work("judge", request=request))
    count = await _dispatch_local_work(service, _Judge(), _Calibration())
    assert count == 1
    assert service.completed == [(service.work, f"score:{request}")]


@pytest.mark.asyncio
async def test_terminal_calibration_claim_invokes_versioned_arguments() -> None:
    arguments = {"judge_id": "judge-1", "calibration_version": "1.0.0"}
    service = _Service(_Work("calibration", arguments=arguments))
    count = await _dispatch_local_work(service, _Judge(), _Calibration())
    assert count == 1
    assert service.completed == [(service.work, arguments)]


@pytest.mark.asyncio
async def test_local_work_is_not_completed_when_required_service_is_unavailable() -> None:
    service = _Service(_Work("judge", request=_request()))
    with pytest.raises(RuntimeError, match="judge_service_unavailable"):
        await _dispatch_local_work(service, None, _Calibration())
    assert service.completed == []
    assert service.failed == [(service.work, "storage_failure")]


@pytest.mark.asyncio
async def test_unknown_local_work_fails_closed_without_completion() -> None:
    service = _Service(_Work("unknown"))
    with pytest.raises(RuntimeError, match="local_work_invalid"):
        await _dispatch_local_work(service, _Judge(), _Calibration())
    assert service.completed == []


@pytest.mark.asyncio
async def test_claim_terminal_and_recovery_spans_keep_one_trace_correlation() -> None:
    metrics = ObservatoryMetrics()
    service = _Service(_Work("judge", request=_request()), metrics=metrics)
    with metrics.trace_context("aura0094-local-work"):
        await _dispatch_local_work(service, _Judge(), _Calibration())
    assert len(metrics.component_spans) == 2
    assert {span.operation for span in metrics.component_spans} == {"dispatch"}
    assert len({span.trace_id for span in metrics.component_spans}) == 1

    failed_metrics = ObservatoryMetrics()

    class _FailingJudge:
        async def evaluate(self, request: object) -> str:
            del request
            raise RuntimeError("provider failure")

    failed_service = _Service(_Work("judge", request=_request()), metrics=failed_metrics)
    with failed_metrics.trace_context("aura0094-local-work-recovery"), pytest.raises(
        RuntimeError, match="provider failure"
    ):
        await _dispatch_local_work(failed_service, _FailingJudge(), _Calibration())
    assert failed_service.failed == [(failed_service.work, "storage_failure")]
    assert len({span.trace_id for span in failed_metrics.component_spans}) == 1
    assert {span.outcome for span in failed_metrics.component_spans} == {"success", "failure"}
