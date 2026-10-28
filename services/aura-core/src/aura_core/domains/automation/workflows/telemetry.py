"""Closed workflow instrumentation helpers.

Metrics carry only bounded operation/outcome dimensions.  Trace context is
accepted explicitly so the application can correlate a workflow transition
without putting workflow content in telemetry.
"""

from __future__ import annotations

from time import monotonic

from aura_core.platform.telemetry import MetadataMetrics, new_span_id

from .public import WorkflowTraceContext


def observe_duration(
    metrics: object | None,
    component: str,
    metric: str,
    started: float,
    *,
    operation: str,
    outcome: str,
    trace_context: WorkflowTraceContext | None = None,
    error_class: str | None = None,
    **dimensions: str,
) -> None:
    if not isinstance(metrics, MetadataMetrics):
        return
    context = trace_context or WorkflowTraceContext("0" * 32)
    duration = max(0.0, min((monotonic() - started) * 1000.0, 60_000.0))
    bounded_error = error_class if error_class in {
        "authorization", "conflict", "database", "database_programming",
        "idempotency", "internal", "not_found", "persistence", "serialization",
        "timeout", "validation", "unknown",
    } else None
    bounded_outcome = outcome if outcome in {
        "accepted", "canceled", "completed", "conflict", "duplicate", "error",
        "failed", "idempotent", "invalid", "ok", "replayed", "unknown",
    } else "error"
    dimensions.pop("error_class", None)
    if bounded_error is not None:
        dimensions["error_class"] = bounded_error
    try:
        metrics.observe(component, metric, duration, trace_id=context.trace_id,
                        operation=operation, outcome=bounded_outcome, **dimensions)
        span_dimensions = dict(dimensions)
        span_dimensions.pop("error_class", None)
        metrics.record_span(component, operation, duration, trace_id=context.trace_id,
                            span_id=new_span_id(), parent_span_id=context.parent_span_id,
                            dependency="workflow", outcome=bounded_outcome,
                            error_class=bounded_error,
                            metric_name=metric, **span_dimensions)
    except Exception:
        # Telemetry must never affect deterministic workflow state.
        return


__all__ = ["observe_duration"]
