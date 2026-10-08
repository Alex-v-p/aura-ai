"""Thin FastAPI composition root."""

from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager
from time import monotonic
from uuid import uuid4

from fastapi import FastAPI, Request, Response

from aura_core.entrypoints.api.errors import install_error_handlers
from aura_core.entrypoints.api.routes import (
    agents,
    auth,
    conversations,
    health,
    memories,
    models,
    personas,
    runs,
)
from aura_core.entrypoints.api.state import AppState
from aura_core.platform.auth import Settings
from aura_core.platform.telemetry import new_span_id


def create_app(settings: Settings | None = None, *, testing: bool = False) -> FastAPI:
    """Build the API with production adapters by default.

    Tests must opt into the in-memory composition explicitly.  This prevents a
    production process from silently accepting writes that disappear on a
    restart when database or broker configuration is missing.
    """

    state = AppState(settings, testing=testing)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        del app
        await state.startup()
        try:
            yield
        finally:
            await state.shutdown()

    app = FastAPI(title="Aura Core API", version="1.0.0", lifespan=lifespan)
    install_error_handlers(app)
    app.state.aura = state

    @app.middleware("http")
    async def observe_memory_boundaries(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        """Record memory API boundary telemetry without capturing request content."""

        path = request.url.path
        method = request.method.upper()
        boundary = _memory_boundary(path, method)
        if boundary is None:
            return await call_next(request)
        component, duration_metric, outcome_metric, operation, dependency = boundary
        started = monotonic()
        trace_id = uuid4().hex
        try:
            response = await call_next(request)
        except Exception:
            _record_memory_boundary(
                state,
                component,
                duration_metric,
                outcome_metric,
                operation,
                dependency,
                trace_id,
                started,
                "error",
                "unknown",
            )
            raise
        outcome = "ok" if response.status_code < 400 else "error"
        if response.status_code < 400:
            error_class = None
        elif response.status_code == 404:
            error_class = "not_found"
        elif response.status_code in {401, 403}:
            error_class = "authorization"
        elif response.status_code == 409:
            error_class = "conflict"
        elif response.status_code == 422:
            error_class = "validation"
        elif response.status_code >= 500:
            error_class = "persistence"
        else:
            error_class = "validation"
        _record_memory_boundary(
            state,
            component,
            duration_metric,
            outcome_metric,
            operation,
            dependency,
            trace_id,
            started,
            outcome,
            error_class,
        )
        return response

    app.include_router(auth.router)
    app.include_router(models.router)
    app.include_router(models.memory_router)
    app.include_router(memories.router)
    app.include_router(memories.candidate_router)
    app.include_router(agents.router)
    app.include_router(personas.router)
    app.include_router(conversations.router)
    app.include_router(runs.router)
    app.include_router(health.router)
    return app


def _memory_boundary(
    path: str, method: str
) -> tuple[str, str, str, str, str] | None:
    if path.startswith("/api/v1/memory-candidates") and method in {"POST", "PATCH"}:
        operation = (
            "memory.candidate.approve"
            if path.endswith("/approve")
            else "memory.candidate.reject"
        )
        return (
            "aura.knowledge.memory_extraction",
            "memory_candidate_review_duration_ms",
            "memory_candidate_review_outcome",
            operation,
            "memory_store",
        )
    if "/memory-model-configuration" in path or "/memory-reindex" in path:
        operation = "memory.model.configure" if method in {"PUT", "POST"} else "memory.model.get"
        return (
            "aura.knowledge.memory_persistence",
            "memory_model_configuration_duration_ms",
            "memory_model_configuration_outcome",
            operation,
            "memory_store",
        )
    if "/memory-policies" in path:
        return (
            "aura.interaction.agent_configuration",
            "memory_policy_configuration_duration_ms",
            "memory_policy_configuration_outcome",
            "agent.configure",
            "configuration_store",
        )
    if path.startswith("/api/v1/memories"):
        suffix = path.removeprefix("/api/v1/memories").strip("/")
        if not suffix:
            operation = "memory.create" if method == "POST" else "memory.list"
        elif suffix == "search":
            operation = "memory.list"
        elif suffix.endswith("/revisions"):
            operation = "memory.revise"
        elif suffix.endswith("/status"):
            operation = "memory.status"
        elif suffix.endswith("/pin"):
            operation = "memory.pin"
        elif suffix.endswith("/purge"):
            operation = "memory.purge"
        else:
            operation = "memory.get"
        return (
            "aura.knowledge.memory_persistence",
            "memory_product_operation_duration_ms",
            "memory_product_operation_outcome",
            operation,
            "memory_store",
        )
    return None


def _record_memory_boundary(
    state: AppState,
    component: str,
    duration_metric: str,
    outcome_metric: str,
    operation: str,
    dependency: str,
    trace_id: str,
    started: float,
    outcome: str,
    error_class: str | None,
) -> None:
    duration = (monotonic() - started) * 1000
    state.metrics.record_span(
        component,
        operation,
        duration,
        trace_id=trace_id,
        span_id=new_span_id(),
        parent_span_id=None,
        dependency=dependency,
        outcome=outcome,
        error_class=error_class,
    )
    state.metrics.observe(
        component,
        duration_metric,
        duration,
        trace_id=trace_id,
        dependency=dependency,
        outcome=outcome,
    )
    state.metrics.increment(
        component,
        outcome_metric,
        trace_id=trace_id,
        dependency=dependency,
        outcome=outcome,
    )


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("aura_core.entrypoints.api.app:app", host="0.0.0.0", port=8000)
